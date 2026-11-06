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

The bridge also advertises itself on the local network over mDNS (see docs/zeroconf.md), so a
client can find the charger without being told its address by hand. Advertising is on by
default, and the zeroconf package is imported lazily, so a stripped install without it still
serves, only without advertising.
"""

import socket
import sys
import threading
from typing import Callable, Dict, List, Optional

import isdttool

from .charger.charger import MODEL_ALIASES
from .charger.representation import parse_packet
from .charger.transport import Transport, encode_tcp_packet, take_tcp_packet

SERIAL_REQUEST_OPCODE = 0xc8
SERIAL_REQUEST = bytes([SERIAL_REQUEST_OPCODE])
SERIAL_RESPONSE_OPCODE = 0xc9
SERIAL_LENGTH = 12
VERSION_REQUEST = bytes([0xe0])
VERSION_RESPONSE_OPCODE = 0xe1

SERVICE_TYPE = '_isdt._tcp.local.'


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


def local_addresses() -> List[bytes]:
    """
    The host's own IP addresses, packed, for an mDNS advertisement. Finds the addresses a peer
    on the LAN would reach by opening throwaway sockets towards documentation addresses, and
    adds whatever the host name resolves to. Loopback and IPv6 link-local addresses are left
    out: a peer can't use them to reach the bridge.
    """
    found = set()
    for family, probe in ((socket.AF_INET, ('192.0.2.1', 9)),
                          (socket.AF_INET6, ('2001:db8::1', 9))):
        try:
            probe_socket = socket.socket(family, socket.SOCK_DGRAM)
            try:
                probe_socket.connect(probe)
                found.add(probe_socket.getsockname()[0])
            finally:
                probe_socket.close()
        except OSError:
            pass
    try:
        for _, _, _, _, sockaddr in socket.getaddrinfo(socket.gethostname(), None):
            found.add(sockaddr[0])
    except OSError:
        pass

    packed = []
    for address in found:
        address = address.split('%')[0]  # Drop any IPv6 zone id.
        if address.startswith('127.') or address == '::1' or address.lower().startswith('fe80'):
            continue
        family = socket.AF_INET6 if ':' in address else socket.AF_INET
        try:
            packed.append(socket.inet_pton(family, address))
        except OSError:
            pass
    return packed


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
        # Discovery. probe_serial and probe_model fill these; serve advertises them.
        self.effective_serial: Optional[str] = None
        self.model: Optional[str] = None
        self.advertise_enabled = True
        self.instance_name: Optional[str] = None
        # Lets tests supply a fake registrar. None means the real zeroconf.Zeroconf.
        self.zeroconf_factory: Optional[Callable[[], object]] = None
        self.__zeroconf__: Optional[object] = None
        self.__service_info__: Optional[object] = None

    def stop(self) -> None:
        """Makes serve return within about a second."""
        self.__stopped__.set()

    def probe_serial(self) -> None:
        """
        Asks the charger for its serial number. If it doesn't answer, the bridge will answer
        serial number requests with the USB serial number. Records the serial a client would
        read, for advertising. Must run before serving.
        """
        self.__transport__.send(SERIAL_REQUEST)
        answer: Optional[bytes] = None
        while True:
            packet = self.__transport__.receive(500)
            if packet is None:
                break
            if packet[:1] == bytes([SERIAL_RESPONSE_OPCODE]):
                answer = packet

        self.answer_serial = answer is None and bool(self.__usb_serial__)
        if answer is not None:
            self.effective_serial = bytes(answer[1:]).hex() or None
            log('The charger answers the serial command itself.')
        elif self.answer_serial:
            self.effective_serial = serial_to_bytes(self.__usb_serial__).hex()
            log('The charger does not answer the serial command. '
                'Answering it with the USB serial number', self.__usb_serial__)
        else:
            log('The charger does not answer the serial command, and has no USB serial number.')

    def probe_model(self) -> None:
        """
        Asks the charger for its model name, for advertising. Best effort: leaves model unset
        if the charger does not answer, or the answer can't be read. Must run before serving.
        """
        self.__transport__.send(VERSION_REQUEST)
        while True:
            packet = self.__transport__.receive(500)
            if packet is None:
                break
            if packet[:1] == bytes([VERSION_RESPONSE_OPCODE]):
                try:
                    model_name = parse_packet(bytearray(packet), 'ignore')['model name']
                    self.model = MODEL_ALIASES.get(model_name, model_name) or None
                except (KeyError, ValueError, IndexError):
                    pass
                break
        if self.model is not None:
            log('The charger is a', self.model + '.')

    def __build_service_info__(self):
        """Builds the mDNS service description from what the probes found."""
        from zeroconf import ServiceInfo

        name = self.instance_name or 'ISDT {} {}'.format(
            self.model or 'charger', self.effective_serial or 'unknown')
        properties: Dict[str, str] = {'version': getattr(isdttool, '__version__', 'unknown')}
        if self.effective_serial is not None:
            properties['serial'] = self.effective_serial
        if self.model is not None:
            properties['model'] = self.model

        return ServiceInfo(
            SERVICE_TYPE,
            '{}.{}'.format(name, SERVICE_TYPE),
            port=self.port,
            properties=properties,
            addresses=local_addresses(),
            server='{}.local.'.format(socket.gethostname().split('.')[0]))

    def advertise(self) -> None:
        """Registers the bridge on mDNS, unless advertising is off, or zeroconf is missing."""
        if not self.advertise_enabled:
            return
        try:
            from zeroconf import Zeroconf
        except ImportError:
            log('The zeroconf package is not installed. Serving without mDNS advertising.')
            return
        try:
            info = self.__build_service_info__()
            zeroconf = (self.zeroconf_factory or Zeroconf)()
            zeroconf.register_service(info)
            self.__zeroconf__ = zeroconf
            self.__service_info__ = info
            log('Advertising as', info.name, 'over mDNS.')
        except Exception as exception:  # noqa: Advertising must never stop the bridge serving.
            log('Could not advertise over mDNS:', exception)
            self.__zeroconf__ = None

    def unadvertise(self) -> None:
        """Withdraws the mDNS registration, if there is one."""
        zeroconf = self.__zeroconf__
        if zeroconf is None:
            return
        self.__zeroconf__ = None
        try:
            if self.__service_info__ is not None:
                zeroconf.unregister_service(self.__service_info__)
        except Exception:  # noqa: Best effort on the way out.
            pass
        try:
            zeroconf.close()
        except Exception:  # noqa
            pass

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

        self.advertise()
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
            self.unadvertise()
            server.close()
            self.__transport__.close()
