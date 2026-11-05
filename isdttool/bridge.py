# coding=utf-8

"""The bridge exposes one charger to the network.

It forwards packets between the charger and one TCP client, in both directions, without
looking at them (see transport.py for the wire format). A new connection replaces the current
one, so a client that went away without closing its connection never locks the charger.

The bridge reaches the charger through a transport, so the network side doesn't depend on
how the charger is attached. Today that is USB.

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

from .charger.transport import Transport, encode_tcp_packet, take_tcp_packet

SERIAL_REQUEST_OPCODE = 0xc8
SERIAL_REQUEST = bytes([SERIAL_REQUEST_OPCODE])
SERIAL_RESPONSE_OPCODE = 0xc9
SERIAL_LENGTH = 12


def log(*args) -> None:
    """Bridge events go to stderr, so that they end up in the journal when run by systemd."""
    print(*args, file=sys.stderr, flush=True)


def debug(*args) -> None:
    """Routine events, only logged with --debug. A client that polls connects every time."""
    if isdttool.DEBUG_MODE:
        log(*args)


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
    Builds the packet a charger would send in reply to a serial number request.
    :param usb_serial: The USB serial number of the charger.
    """
    return bytes([SERIAL_RESPONSE_OPCODE]) + serial_to_bytes(usb_serial)


class Bridge:
    """Forwards packets between one charger and the current TCP client."""

    def __init__(self, transport: Transport, usb_serial: Optional[str]) -> None:
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
        self.__transport__.send(SERIAL_REQUEST)
        answered = False
        while True:
            packet = self.__transport__.receive(500)
            if packet is None:
                break
            if packet[:1] == bytes([SERIAL_RESPONSE_OPCODE]):
                answered = True

        self.answer_serial = not answered and bool(self.__usb_serial__)
        if answered:
            log('The charger answers the serial command itself.')
        elif self.answer_serial:
            log('The charger does not answer the serial command. '
                'Answering it with the USB serial number', self.__usb_serial__)
        else:
            log('The charger does not answer the serial command, and has no USB serial number.')

    def __send_to_client__(self, packet: bytes) -> None:
        with self.__client_lock__:
            client = self.__client__
        if client is None:
            return  # Nobody is listening. Packets from the charger are dropped.
        try:
            client.sendall(encode_tcp_packet(packet))
        except OSError:
            pass  # The client reader notices, and cleans up.

    def __charger_reader__(self) -> None:
        """Forwards packets from the charger to the current client."""
        try:
            while not self.__stopped__.is_set():
                packet = self.__transport__.receive(200)
                if packet is not None:
                    self.__send_to_client__(packet)
        except OSError as exception:
            log('Lost the charger:', exception)
        self.__stopped__.set()

    def __client_reader__(self, client: socket.socket, address) -> None:
        """Forwards packets from one client to the charger, until it disconnects, or is
        replaced."""
        buffer = bytearray()
        try:
            while not self.__stopped__.is_set():
                chunk = client.recv(4096)
                if not chunk:
                    break
                buffer.extend(chunk)
                while True:
                    packet = take_tcp_packet(buffer)
                    if packet is None:
                        break
                    if self.answer_serial and packet == SERIAL_REQUEST:
                        self.__send_to_client__(serial_response(self.__usb_serial__))
                        continue
                    with self.__write_lock__:
                        self.__transport__.send(packet)
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
