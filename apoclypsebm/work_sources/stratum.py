import asynchat
import asyncore
import socket
from binascii import hexlify, unhexlify
from hashlib import sha256
from json import dumps, loads
from struct import pack
from threading import Lock, Thread, Timer
from time import sleep, time, monotonic

import socks

from apoclypsebm.log import say_exception, say_line
from apoclypsebm.util import Object, chunks
from apoclypsebm.work_sources.base import Source

# import ssl


BASE_DIFFICULTY = 0x00000000FFFF0000000000000000000000000000000000000000000000000000
MIN_DIFFICULTY = 0x00000000FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF


def detect_stratum_proxy(host):
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, 0)
        s.sendto(
            dumps({"id": 0, "method": "mining.get_upstream", "params": []}),
            ('239.3.3.3', 3333))

        say_line('Searching stratum proxy for %s', host)

        s.settimeout(2)

        try:
            while True:
                response, address = s.recvfrom(128)
                try:
                    response = loads(response)
                    response_host = response['result'][0][0] + ':' + str(
                        response['result'][0][1])
                    if response_host == host:
                        proxy_address = address[0] + ':' + str(
                            response['result'][1])
                        say_line('Using stratum proxy at %s', proxy_address)
                        return proxy_address
                except ValueError:
                    pass
        except socket.timeout:
            pass

    finally:
        if s != None:
            s.close()


class StratumSource(Source):
    def __init__(self, switch):
        super(StratumSource, self).__init__(switch)
        self.handler = None
        self.socket = None
        self.channel_map = {}
        self.subscribed = False
        self.authorized = None
        self.submits = {}
        self.last_submits_cleanup = monotonic()
        self.server_difficulty = BASE_DIFFICULTY
        self.jobs = {}
        self.current_job = None
        self.extranonce = ''
        self.extranonce2_size = 4
        self.send_lock = Lock()

    def loop(self):
        super(StratumSource, self).loop()

        self.switch.update_time = True

        while True:
            if self.should_stop: return

            if self.current_job:
                miner = self.switch.updatable_miner()
                while miner:
                    self.current_job = self.refresh_job(self.current_job)
                    self.queue_work(self.current_job, miner)
                    miner = self.switch.updatable_miner()

            if self.check_failback():
                return True

            if not self.handler:
                try:
                    # socket = ssl.wrap_socket(socket)
                    address, port = self.server().host.split(':', 1)

                    if not self.options.proxy:
                        self.socket = socket.socket(socket.AF_INET,
                                                    socket.SOCK_STREAM)
                        self.socket.connect((address, int(port)))
                    else:
                        self.socket = socks.socksocket()
                        p = self.options.proxy
                        self.socket.setproxy(p.type, p.host, p.port, True,
                                             p.user, p.pwd)
                        try:
                            self.socket.connect((address, int(port)))
                        except socks.Socks5AuthError:
                            say_exception('Proxy error:')
                            self.stop()

                    self.handler = Handler(self.socket, self.channel_map, self)
                    thread = Thread(target=self.asyncore_thread)
                    thread.daemon = True
                    thread.start()

                    if not self.subscribe():
                        say_line('Failed to subscribe')
                        self.stop()
                    elif not self.authorize():
                        self.stop()

                except socket.error:
                    say_exception()
                    self.stop()
                    continue

            with self.send_lock:
                self.process_result_queue()
            sleep(1)

    def asyncore_thread(self):
        asyncore.loop(map=self.channel_map)

    def stop(self):
        self.should_stop = True
        if self.handler:
            self.handler.close()

    def refresh_job(self, j):
        j.extranonce2 = self.increment_nonce(j.extranonce2)
        coinbase = j.coinbase1 + self.extranonce + j.extranonce2 + j.coinbase2
        merkle_root = sha256(sha256(unhexlify(coinbase)).digest()).digest()
        for hash_ in j.merkle_branch:
            merkle_root = sha256(
                sha256(merkle_root + unhexlify(hash_)).digest()).digest()
        merkle_root_bak = ''.join(['%02x' % b for b in merkle_root])
        merkle_root_reversed = b''
        for word in chunks(merkle_root, 4):
            merkle_root_reversed += word[::-1]
        merkle_root = merkle_root_reversed.hex()

        j.block_header = ''.join(
            [j.version, j.prevhash, merkle_root, j.ntime, j.nbits])
        j.time = time()
        return j

    def increment_nonce(self, nonce):
        next_nonce = int(nonce, 16) + 1
        if len('%x' % next_nonce) > (self.extranonce2_size * 2):
            return '00' * self.extranonce2_size
        return ('%0' + str(self.extranonce2_size * 2) + 'x') % next_nonce

    def handle_message(self, message):

        # Miner API
        if 'method' in message:

            # mining.notify
            if message['method'] == 'mining.notify':
                params = message['params']

                j = Object()

                j.job_id = params[0]
                j.prevhash = params[1]

                #john
                j.prevhash = ''.join([j.prevhash[i]+j.prevhash[i+1] for i in range(0,len(j.prevhash),2)][::-1])
                prev_block_hash_words = bytearray()
                for word in chunks(unhexlify(j.prevhash), 4):
                    # Prepend because template items are in RPC byte order.
                    prev_block_hash_words[0:0] = word
                j.prevhash = ''.join(['%02x' % b for b in prev_block_hash_words])

                j.coinbase1 = params[2]
                j.coinbase2 = params[3]
                j.merkle_branch = params[4]
                j.version = params[5]
                j.nbits = params[6]
                j.ntime = params[7]
                clear_jobs = params[8]
                if clear_jobs:
                    self.jobs.clear()
                j.extranonce2 = self.extranonce2_size * '00'

                j = self.refresh_job(j)

                self.jobs[j.job_id] = j
                self.current_job = j

                self.queue_work(j)
                self.switch.connection_ok()

            # mining.get_version
            if message['method'] == 'mining.get_version':
                with self.send_lock:
                    self.send_message({"error": None, "id": message['id'],
                                       "result": self.user_agent})

            # mining.set_difficulty
            elif message['method'] == 'mining.set_difficulty':
                say_line("Setting new difficulty: %s", message['params'][0])
                self.server_difficulty = min(MIN_DIFFICULTY, int(BASE_DIFFICULTY //
                                             message['params'][0]))

            # client.reconnect
            elif message['method'] == 'client.reconnect':
                address, port = self.server().host.split(':', 1)
                (new_address, new_port, timeout) = message['params'][:3]
                if new_address:
                    address = new_address
                if new_port is not None:
                    port = new_port
                say_line("%s asked us to reconnect to %s:%d in %d seconds",
                         (self.server().name, address, port, timeout))
                self.server().host = address + ':' + str(port)
                Timer(timeout, self.reconnect).start()

            # client.add_peers
            elif message['method'] == 'client.add_peers':
                hosts = [{'host': host[0], 'port': host[1]} for host in
                         message['params'][0]]
                self.switch.add_servers(hosts)

        # responses to server API requests
        elif 'result' in message:

            # response to mining.subscribe
            # store extranonce and extranonce2_size
            if message['id'] == 's':
                self.extranonce = message['result'][1]
                self.extranonce2_size = message['result'][2]
                self.subscribed = True

            # check if this is submit confirmation (message id should be in submits dictionary)
            # cleanup if necessary
            elif message['id'] in self.submits:
                miner, nonce = self.submits[message['id']][:2]
                accepted = message['result']
                self.switch.report(miner, nonce, accepted)
                del self.submits[message['id']]
                if monotonic() - self.last_submits_cleanup > 3600:
                    now = monotonic()
                    for key, value in self.submits.items():
                        if now - value[2] > 3600:
                            del self.submits[key]
                    self.last_submits_cleanup = now

            # response to mining.authorize
            elif message['id'] == self.server().user:
                if not message['result']:
                    say_line('authorization failed with %s:%s@%s', (
                    self.server().user, self.server().pwd, self.server().host))
                    self.authorized = False
                else:
                    self.authorized = True

    def reconnect(self):
        say_line("%s reconnecting to %s",
                 (self.server().name, self.server().host))
        self.handler.close()

    def subscribe(self):
        self.send_message(
            {'id': 's', 'method': 'mining.subscribe', 'params': []})
        for i in range(10):
            sleep(1)
            if self.subscribed: break
        return self.subscribed

    def authorize(self):
        self.send_message(
            {'id': self.server().user, 'method': 'mining.authorize',
             'params': [self.server().user, self.server().pwd]})
        for i in range(10):
            sleep(1)
            if self.authorized != None: break
        return self.authorized

    def send_internal(self, result, nonce):
        job_id = result.job_id
        if not job_id in self.jobs:
            return True
        extranonce2 = result.extranonce2
        #john
        #ntime = hexlify(pack('<I', int(result.time)))
        ntime = pack('<I', int(result.time))   #john
        ntime = ''.join(['%02x' % b for b in ntime])
        hex_nonce = hexlify(pack('<I', int(nonce))) 
        hex_nonce = pack('<I', int(nonce))  #john
        hex_nonce = ''.join(['%02x' % b for b in hex_nonce]) 
        id_ = job_id + hex_nonce
        self.submits[id_] = (result.miner, nonce, time())
        return self.send_message({'params': [self.server().user, job_id,
                                             extranonce2, ntime, hex_nonce],
                                  'id': id_, 'method': u'mining.submit'})

    def send_message(self, message):
        data = dumps(message) + '\n'
        print(data)
        data = data.encode('utf-8')
        try:
            # self.handler.push(data)

            # there is some bug with asyncore's send mechanism
            # so we send data 'manually'
            # note that this is not thread safe
            if not self.handler:
                return False
            while data:
                sent = self.handler.send(data)
                data = data[sent:]
            return True
        except AttributeError:
            self.stop()
        except Exception:
            say_exception()
            self.stop()

    def queue_work(self, work, miner=None):
        target = ''.join(
            list(chunks('%064x' % self.server_difficulty, 2))[::-1])
        self.switch.queue_work(self, work.block_header, target, work.job_id,
                               work.extranonce2, miner)


class Handler(asynchat.async_chat):
    def __init__(self, socket, map_, parent):
        asynchat.async_chat.__init__(self, socket, map_)
        self.parent = parent
        self.data = b''
        self.set_terminator(b'\n')

    def handle_close(self):
        self.close()
        self.parent.handler = None
        self.parent.socket = None

    #def handle_error(self):
    #    say_exception()
    #    self.parent.stop()

    def collect_incoming_data(self, data):
        self.data += data

    def found_terminator(self):
        message = loads(self.data)
        self.parent.handle_message(message)
        self.data = b''
