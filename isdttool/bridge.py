# coding=utf-8

"""The bridge exposes one charger on a local USB port to the network.

It forwards raw 64-byte HID reports between the charger and one TCP client, in both
directions, without looking at them (see transport.py for the wire format). A new connection
replaces the current one, so a client that went away without closing its connection never
locks the charger.

There is one exception to "without looking at them": some chargers, like the N24, don't
answer the serial command. A client then has no way to tell two chargers apart. For those
chargers, the bridge answers the serial command itself, with the USB serial number of the
charger.
"""

import socket
import sys
import threading
from typing import Optional

import isdttool

from .charger.transport import REPORT_SIZE, UsbTransport

SERIAL_REQUEST_OPCODE = 0xc8
SERIAL_RESPONSE_OPCODE = 0xc9
SERIAL_LENGTH = 12


def log(*args) -> None:
    """Bridge events go to stderr, so that they end up in the journal when run by systemd."""
    print(*args, file=sys.stderr, flush=True)


def debug(*args) -> None:
    """Routine events, only logged with --debug. A client that polls connects every time."""
    if isdttool.DEBUG_MODE:
        log(*args)


def is_serial_request(report: bytes) -> bool:
    """
    Checks whether a report from a client is a serial number request.
    :param report: A 64-byte report, as written by Charger.write_to_charger.
    """
    # 0x01 request, frame length, 0xAA sync, 0x12 computer to charger, payload length 1, opcode
    return len(report) >= 6 and report[0] == 0x01 and report[2] == 0xaa and \
        report[3] == 0x12 and report[4] == 0x01 and report[5] == SERIAL_REQUEST_OPCODE


def serial_to_bytes(usb_serial: str) -> bytes:
    """
    Turns a USB serial number into the 12 bytes of a serial number response.
    A hexadecimal serial number is stored as its value, so that isdttool shows it as it is,
    right aligned, e.g. 00000000011C becomes 00000000000000000000011c. Other serial numbers are
    stored as text.
    """
    try:
        if len(usb_serial) <= 2 * SERIAL_LENGTH:
            value = int(usb_serial, 16)
            return value.to_bytes(SERIAL_LENGTH, byteorder='big')
    except ValueError:
        pass
    return usb_serial.encode('utf8')[:SERIAL_LENGTH].ljust(SERIAL_LENGTH, b'\x00')


def serial_response(usb_serial: str) -> bytes:
    """
    Builds the report a charger would send in reply to a serial number request.
    :param usb_serial: The USB serial number of the charger.
    """
    packet = bytearray([0x21, 1 + SERIAL_LENGTH, SERIAL_RESPONSE_OPCODE])
    packet.extend(serial_to_bytes(usb_serial))
    packet.append(sum(packet) & 0xff)

    escaped = bytearray([0xaa])
    for b in packet:
        if b == 0xaa:
            escaped.append(0xaa)
        escaped.append(b)

    report = bytes([0x02, len(escaped)]) + bytes(escaped)
    return report.ljust(REPORT_SIZE, b'\x00')


class Bridge:
    """Forwards reports between one USB charger and the current TCP client."""

    def __init__(self, transport: UsbTransport, usb_serial: Optional[str]) -> None:
        self.__transport__ = transport
        self.__usb_serial__ = usb_serial
        self.__client__: Optional[socket.socket] = None
        self.__client_lock__ = threading.Lock()
        self.__write_lock__ = threading.Lock()
        self.__stopped__ = threading.Event()
        self.answer_serial = False
        self.port: Optional[int] = None
        self.ready = threading.Event()

    def stop(self) -> None:
        """Makes serve return within about a second."""
        self.__stopped__.set()

    def probe_serial(self) -> None:
        """
        Asks the charger for its serial number. If it doesn't answer, the bridge will answer
        serial number requests with the USB serial number. Must run before serving.
        """
        request = bytes([0x01, 0x05, 0xaa, 0x12, 0x01, SERIAL_REQUEST_OPCODE,
                         (0x12 + 0x01 + SERIAL_REQUEST_OPCODE) & 0xff])
        self.__transport__.write(request.ljust(REPORT_SIZE, b'\x00'))
        answered = False
        while True:
            report = self.__transport__.read(REPORT_SIZE, 500)
            if not report:
                break
            if len(report) > 5 and report[5] == SERIAL_RESPONSE_OPCODE:
                answered = True

        self.answer_serial = not answered and bool(self.__usb_serial__)
        if answered:
            log('The charger answers the serial command itself.')
        elif self.answer_serial:
            log('The charger does not answer the serial command. '
                'Answering it with the USB serial number', self.__usb_serial__)
        else:
            log('The charger does not answer the serial command, and has no USB serial number.')

    def __send_to_client__(self, report: bytes) -> None:
        with self.__client_lock__:
            client = self.__client__
        if client is None:
            return  # Nobody is listening. Reports from the charger are dropped.
        try:
            client.sendall(report)
        except OSError:
            pass  # The client reader notices, and cleans up.

    def __charger_reader__(self) -> None:
        """Forwards reports from the charger to the current client."""
        try:
            while not self.__stopped__.is_set():
                report = self.__transport__.read(REPORT_SIZE, 200)
                if report:
                    self.__send_to_client__(report.ljust(REPORT_SIZE, b'\x00'))
        except OSError as exception:
            log('Lost the charger:', exception)
        self.__stopped__.set()

    def __client_reader__(self, client: socket.socket, address) -> None:
        """Forwards reports from one client to the charger, until it disconnects, or is
        replaced."""
        buffer = b''
        try:
            while not self.__stopped__.is_set():
                chunk = client.recv(REPORT_SIZE - len(buffer))
                if not chunk:
                    break
                buffer += chunk
                if len(buffer) < REPORT_SIZE:
                    continue
                report, buffer = buffer, b''

                if self.answer_serial and is_serial_request(report):
                    self.__send_to_client__(serial_response(self.__usb_serial__))
                    continue

                with self.__write_lock__:
                    self.__transport__.write(report)
        except OSError:
            pass

        with self.__client_lock__:
            if self.__client__ is client:
                self.__client__ = None
                debug('Client', address[0], 'disconnected.')
        client.close()

    def serve(self, bind: str, port: int) -> None:
        """
        Serves the charger until it is unplugged.
        :param bind: The address to listen on. Empty listens on all addresses, IPv6, and IPv4.
        :param port: The TCP port to listen on.
        """
        if bind:
            family = socket.AF_INET6 if ':' in bind else socket.AF_INET
            server = socket.socket(family, socket.SOCK_STREAM)
        else:
            try:
                server = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
                server.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
                bind = '::'
            except OSError:  # No IPv6 on this host
                server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                bind = '0.0.0.0'
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((bind, port))
        server.listen(1)
        server.settimeout(1)
        self.port = server.getsockname()[1]
        port = self.port
        self.ready.set()
        log('Serving the charger on', '[{}]:{}'.format(bind, port) if ':' in bind
            else '{}:{}'.format(bind, port))

        threading.Thread(target=self.__charger_reader__, daemon=True).start()
        last_address: Optional[str] = None

        try:
            while not self.__stopped__.is_set():
                try:
                    client, address = server.accept()
                except socket.timeout:
                    continue
                client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                with self.__client_lock__:
                    previous, self.__client__ = self.__client__, client
                if previous is not None:
                    log('Client', address[0], 'replaces the previous client.')
                    try:
                        previous.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                elif address[0] != last_address:
                    log('Client', address[0], 'connected. Further connections from it are only '
                        'logged with --debug.')
                else:
                    debug('Client', address[0], 'connected.')
                last_address = address[0]
                threading.Thread(target=self.__client_reader__, args=(client, address),
                                 daemon=True).start()
        finally:
            self.__stopped__.set()
            server.close()
            self.__transport__.close()
