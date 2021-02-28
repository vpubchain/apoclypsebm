from binascii import unhexlify
from queue import Empty
from struct import error, pack, unpack
from sys import maxsize
from time import sleep, time

import serial
from serial.serialutil import SerialException

from apoclypsebm.ioutil import find_com_ports, find_serial_by_id, find_udev
from apoclypsebm.log import say_exception, say_line
from apoclypsebm.mining.base import Miner
from apoclypsebm.util import Object, bytereverse, uint32

CHECK_INTERVAL = 0.01


def open_device(port):
    return serial.Serial(port, 115200, serial.EIGHTBITS, serial.PARITY_NONE,
                         serial.STOPBITS_ONE, 1, False, False, 5, False, None)


def is_good_init(response):
    return response and response[
                        :31] == b'>>>ID: BitFORCE SHA256 Version ' and response[
                                                                       -4:] == b'>>>\n'


def init_device(device):
    return request(device, b'ZGX')


def request(device, message):
    if device:
        device.flushInput()
        device.write(message)
        return device.readline()


def check(port, likely=True):
    result = False
    try:
        device = open_device(port)
        response = init_device(device)
        device.close()
        result = is_good_init(response)
    except SerialException:
        if likely:
            say_exception()
    if not likely and result:
        say_line('Found BitFORCE on %s', port)
    elif likely and not result:
        say_line('No valid response from BitFORCE on %s', port)
    return result


def initialize(options):
    ports = find_udev(check, 'BitFORCE*SHA256') or find_serial_by_id(check,
                                                                     'BitFORCE_SHA256') or find_com_ports(
        check)

    if not options.device and ports:
        print('\nBFL devices on ports:\n')
        for i in range(len(ports)):
            print('[%d]\t%s' % (i, ports[i]))

    miners = [
        BFLMiner(i, ports[i], options)
        for i in range(len(ports))
        if (
                (not options.device) or
                (i in options.device)
        )
    ]

    for i in range(len(miners)):
        miners[i].cutoff_temp = options.cutoff_temp[
            min(i, len(options.cutoff_temp) - 1)]
        miners[i].cutoff_interval = options.cutoff_interval[
            min(i, len(options.cutoff_interval) - 1)]
    return miners


class BFLMiner(Miner):
    def __init__(self, device_idx, port, options):
        super(BFLMiner, self).__init__(device_idx, options)
        self.port = port
        self.device_name = f'BFL:{self.device_idx}'

        self.check_interval = CHECK_INTERVAL
        self.last_job = None
        self.min_interval = maxsize

    def id(self):
        return self.device_name

    def is_ok(self, response):
        return response and response == b'OK\n'

    def put_job(self):
        if self.busy: return

        temperature = self.get_temperature()
        if temperature < self.cutoff_temp:
            response = request(self.device, b'ZDX')
            if self.is_ok(response):
                if self.switch.update_time:
                    self.job.time = bytereverse(
                        uint32(int(time())) - self.job.time_delta)
                data = b''.join([pack('<8I', *self.job.state),
                                 pack('<3I', self.job.merkle_end, self.job.time,
                                      self.job.difficulty)])
                response = request(self.device,
                                   b''.join([b'>>>>>>>>', data, b'>>>>>>>>']))
                if self.is_ok(response):
                    self.busy = True
                    self.job_started = time()

                    self.last_job = Object()
                    self.last_job.header = self.job.header
                    self.last_job.merkle_end = self.job.merkle_end
                    self.last_job.time = self.job.time
                    self.last_job.difficulty = self.job.difficulty
                    self.last_job.target = self.job.target
                    self.last_job.state = self.job.state
                    self.last_job.job_id = self.job.job_id
                    self.last_job.extranonce2 = self.job.extranonce2
                    self.last_job.server = self.job.server
                    self.last_job.miner = self

                    self.check_interval = CHECK_INTERVAL
                    if not self.switch.update_time or bytereverse(
                            self.job.time) - bytereverse(
                            self.job.original_time) > 55:
                        self.update = True
                        self.job = None
                else:
                    say_line('%s: bad response when sending block data: %s',
                             (self.id(), response))
            else:
                say_line('%s: bad response when submitting job (ZDX): %s',
                         (self.id(), response))
        else:
            say_line('%s: temperature exceeds cutoff, waiting...', self.id())

    def get_temperature(self):
        response = request(self.device, b'ZLX')
        if len(response) < 23 or response[0] != b'T' or response[-1:] != b'\n':
            say_line('%s: bad response for temperature: %s',
                     (self.id(), response))
            return 0
        return float(response[23:-1])

    def check_result(self):
        response = request(self.device, b'ZFX')
        if response.startswith(b'B'): return False
        if response == b'NO-NONCE\n': return response
        if response[:12] != 'NONCE-FOUND:' or response[-1:] != '\n':
            say_line('%s: bad response checking result: %s',
                     (self.id(), response))
            return None
        return response[12:-1]

    def nonce_generator(self, nonces):
        for nonce in nonces.split(b','):
            if len(nonce) != 8: continue
            try:
                yield unpack('<I', unhexlify(nonce)[::-1])[0]
            except error:
                pass

    def mining_thread(self):
        say_line('started BFL miner on %s', (self.id()))

        while not self.should_stop:
            try:
                self.device = open_device(self.port)
                response = init_device(self.device)
                if not is_good_init(response):
                    say_line(
                        'Failed to initialize %s (response: %s), retrying...',
                        (self.id(), response))
                    self.device.close()
                    self.device = None
                    sleep(1)
                    continue

                last_rated = time()
                iterations = 0

                self.job = None
                self.busy = False
                while not self.should_stop:
                    if (not self.job) or (not self.work_queue.empty()):
                        try:
                            self.job = self.work_queue.get(True, 1)
                        except Empty:
                            if not self.busy:
                                continue
                        else:
                            if not self.job and not self.busy:
                                continue
                            targetQ = self.job.targetQ
                            self.job.original_time = self.job.time
                            self.job.time_delta = uint32(
                                int(time())) - bytereverse(self.job.time)

                    if not self.busy:
                        self.put_job()
                    else:
                        result = self.check_result()
                        if result:
                            now = time()

                            self.busy = False
                            r = self.last_job
                            job_duration = now - self.job_started
                            self.put_job()

                            self.min_interval = min(self.min_interval,
                                                    job_duration)

                            iterations += 4294967296
                            t = now - last_rated
                            if t > self.options.rate:
                                self.update_rate(now, iterations, t, targetQ)
                                last_rated = now
                                iterations = 0

                            if result != b'NO-NONCE\n':
                                r.nonces = result
                                self.switch.put(r)

                            sleep(self.min_interval - (CHECK_INTERVAL * 2))
                        else:
                            if result is None:
                                self.check_interval = min(
                                    self.check_interval * 2, 1)

                    sleep(self.check_interval)
            except Exception:
                say_exception()
                if self.device:
                    self.device.close()
                    self.device = None
                sleep(1)
