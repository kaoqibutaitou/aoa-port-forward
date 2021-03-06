""" 
Same as android accessory, but uses sync io
"""
#! python3
# android_accessory_test.py
#

# pylint: disable=no-member
# pylint: disable=W0511,W0622,W0613,W0603,R0902

from struct import pack, unpack
import sys
import time
import signal
import threading
import os
import socket
import select
import usb1
from constants import *
import bytebuffer
if sys.version_info > (3, 5):  # Python 3.5+
    import selectors
else:  # Python 2.6 - 3.4
    import selectors2 as selectors


class ReadCallback(object):
    """
    TODO: Docstring
    """
    def __init__(self, acc):
        self._accessory = acc
        self._command = None
        self._payload_size = 0
        self._split_header = False
        self._split_payload = False

        self._split_header_buffer = bytebuffer.allocate(4)
        self._split_payload_buffer = bytebuffer.allocate(8192)

    def __call__(self, in_buffer):
        """
        TODO: Docstring
        """
        data = bytebuffer.wrap(in_buffer)
        eprint("Packet Length: {0}".format(len(in_buffer)))
        # Loop while able to process header
        while data.remaining() >= 4:
            if self._split_header:
                eprint("Split Header Detected")
                if self._split_header_buffer.hasRemaining():
                    self._split_header_buffer.fill(data)
                self._split_header_buffer.flip()
                self._command = self._split_header_buffer.getBytes(2)
                self._payload_size = self._split_header_buffer.getShort()
                self._split_header = False
                self._split_header_buffer.clear()
            elif self._split_payload:
                eprint("Split Payload Detected")
                self._split_payload_buffer.fill(data)
                if self._split_payload_buffer.hasRemaining():
                    # data packet didn't contain entire payload
                    break
                else:
                    self._split_payload_buffer.flip()
                    self._process_packet(self._split_payload_buffer)
                    self._split_payload_buffer.clear()
                    self._split_payload = False
                    continue
            else:
                self._command = data.getBytes(2)
                self._payload_size = data.getShort()

            if self._payload_size == 0:
                # packet has no payload, process it
                self._process_packet(None)
            elif self._payload_size <= data.remaining():
                # entire payload is in packet, process it
                payload = data.duplicate()
                payload.limit = payload.position + self._payload_size
                self._process_packet(payload)
                data.position = data.position + self._payload_size
            else:
                eprint("Remaining packet not contained in current buffer")
                if data.hasRemaining():
                    self._split_payload_buffer.put(data)
                self._split_payload_buffer.limit = self._payload_size
                self._split_payload = True

        if data.hasRemaining():
            if data.remaining() <= self._split_header_buffer.remaining():
                self._split_header_buffer.put(data)
                self._split_header = True
            else:
                # fill remaining header, process it, then put
                # what is left of the data in split packet
                self._split_header_buffer.fill(data)
                self._split_header_buffer.flip()
                self._command = self._split_header_buffer.getBytes(2)
                self._payload_size = self._split_header_buffer.getShort()
                self._split_header = False
                self._split_header_buffer.clear()
                if data.hasRemaining():
                    self._split_payload_buffer.limit = self._payload_size
                    self._split_payload_buffer.put(data)
                    self._split_payload = True

    def _process_packet(self, payload):
        if self._command == CMD_CONNECT_SOCKET:
            socket_id = payload.getShort()
            self._accessory.connect_socket(socket_id)
        elif self._command == CMD_DISCONNECT_SOCKET:
            socket_id = payload.getShort()
            self._accessory.disconnect_socket(socket_id)
        elif self._command == CMD_DATA_PACKET:
            # Demux and write to socket
            socket_id = payload.getShort()
            sock = self._accessory.get_socket(socket_id)
            if sock:
                while payload.hasRemaining():
                    r, w, x = select.select([], [sock.fileno()], [])
                    if w:
                        bytes_sent = sock.send(payload[payload.position:payload.limit])
                        if bytes_sent == 0:
                            eprint("Write error, socket broken")
                            self._accessory.disconnect_socket(socket_id)
                            # Error on server end, let app know
                            self._accessory.send_accessory_command(CMD_DISCONNECT_SOCKET,
                                                                   socket_id)
                            payload.position = payload.limit
                            break
                        else:
                            payload.position = payload.position + bytes_sent
            else:
                eprint("Socket not valid: {0}".format(socket_id))
        elif self._command == CMD_ACCESSORY_CONNECTED:
            port = payload.getInt()
            self._accessory.app_connected = True
            self._accessory.port = port
            eprint("App connected, fowarding port: {0}".format(port))
        elif self._command == CMD_CLOSE_ACCESSORY:
            eprint("Close accessory request recieved")
            self._accessory.signal_app_exit()
        else:
            eprint("Unknown Command:")
            eprint(self._command)


class AndroidAccessory(object):
    """docstring for AndroidAccessory."""
    def __init__(self, usb_context, vendor_id=None, product_id=None):
        self._context = usb_context
        isconfigured, self._handle = self._find_handle(vendor_id, product_id)

        if isconfigured:
            print("Device already in accessory mode, attempting reset")
            # TODO: should I reset the device?
            #self._handle.claimInterface(0)
            #self._handle.resetDevice()
            #time.sleep(2)
            #isconfigured, self._handle = self._find_handle(vendor_id, product_id)
        else:
            self._handle = self._configure_accessory_mode()

        self._handle.claimInterface(0)

        # pause for one second so the android device can react to changes
        time.sleep(1)

        device = self._handle.getDevice()
        config = device[0]
        interface = config[0]
        self._in_endpoint, self._out_endpoint = self._get_endpoints(interface[0])
        if self._in_endpoint is None or self._out_endpoint is None:
            self._handle.releaseInterface(0)
            raise usb1.USBError(
                'Unable to retreive endpoints for accessory device'
            )

        self.port = 8000  # port to forward sockets to
        self.app_connected = False
        self._is_running = True

        self._read_callback = ReadCallback(self)
        self._accessory_read_thread = threading.Thread(target=self._accessory_read_thread_proc)
        self._accessory_read_thread.start()

        self._socket_dict = {}
        self._socket_selector = selectors.DefaultSelector()
        self._socket_read_thread = threading.Thread(target=self._socket_read_thread_proc)
        self._socket_read_thread.start()

    def _find_handle(self, vendor_id=None, product_id=None, attempts_left=5):
        handle = None
        found_dev = None
        for device in self._context.getDeviceList():
            if vendor_id and product_id:
                # match by vendor and product id
                if (device.getVendorID() == vendor_id and
                        device.getProductID() == product_id):
                    handle = self._open_device(device)
                    if handle:
                        found_dev = device
                        break
            elif device.getVendorID() in COMPATIBLE_VIDS:
                # attempt to get the first compatible vendor id
                handle = self._open_device(device)
                if handle:
                    found_dev = device
                    break

        if handle:
            eprint("Device Found: {0}".format(found_dev))
            eprint("Product: {0}".format(
                handle.getASCIIStringDescriptor(found_dev.device_descriptor.iProduct)))
            if found_dev.getProductID() in ACCESSORY_PID:
                return True, handle
            else:
                return False, handle
        elif attempts_left:
            time.sleep(1)
            return self._find_handle(vendor_id, product_id, attempts_left-1)
        else:
            raise usb1.USBError('Device not available')

    def _open_device(self, device):
        eprint("Open attempt: {0}".format(device))
        eprint("Class: {0:x}, Subclass: {1:x}".format(
            device.getDeviceClass(), device.getDeviceSubClass()
        ))
        try:
            handle = device.open()
        except usb1.USBError as err:
            eprint("Unable to get device handle: %s" % err)
            return None
        else:
            return handle

    def _configure_accessory_mode(self):
        # Don't need to claim interface to do control read/write, and the
        # original driver prevents it
        # self._handle.claimInterface(0)

        version = self._handle.controlRead(
            usb1.TYPE_VENDOR | usb1.RECIPIENT_DEVICE,
            51, 0, 0, 2
        )

        adk_ver = unpack('<H', version)[0]
        print("ADK version is: %d" % adk_ver)

        # enter accessory information
        for i, data in enumerate((MANUFACTURER, MODEL_NAME, DESCRIPTION,
                                  VERSION, URI, SERIAL_NUMBER)):
            assert self._handle.controlWrite(
                usb1.TYPE_VENDOR | usb1.RECIPIENT_DEVICE,
                52, 0, i, data.encode()
            ) == len(data)

        if adk_ver == 2 and sys.platform == 'linux':
            # enable 2 channel audio
            assert self._handle.controlWrite(
                usb1.TYPE_VENDOR | usb1.RECIPIENT_DEVICE,
                58, 1, 0, b''
            ) == 0

        # start device in accessory mode
        self._handle.controlWrite(
            usb1.TYPE_VENDOR | usb1.RECIPIENT_DEVICE,
            53, 0, 0, b''
        )

        time.sleep(1)

        isconfigured, newhandle = self._find_handle()
        if isconfigured:
            return newhandle
        else:
            raise usb1.USBError('Error configuring accessory mode')

    def _get_endpoints(self, interface):
        inep = None
        outep = None
        for endpoint in interface:
            addr = endpoint.getAddress()
            if (addr & 0x80) == 0x80:
                inep = addr
                print('In endpoint address: %02x' % addr)
            elif (addr & 0x80) == 0x00:
                outep = addr
                print('Out endpoint address: %02x' % addr)
        return inep, outep

    def _accessory_read_thread_proc(self):
        while self._is_running:
            try:
                data = self._handle.bulkRead(self._in_endpoint, 16384, timeout=1000)
            except usb1.USBError as err:
                if err.value == -7:  # timeout
                    continue
                eprint(err)
                break
            except OSError:
                break
            else:
                self._read_callback(data)

    def _socket_read_thread_proc(self):
        """
        Uses a selector to loop through all connected sockets, listening
        for data.  If data is found, it is muxed and sent back to the
        android device via usb
        """
        def _win_select():
            while len(self._socket_dict) == 0:
                time.sleep(.001)
                if not self._is_running:
                    return []
            return self._socket_selector.select(timeout=1)

        def _nix_select():
            return self._socket_selector.select(timeout=1)

        if sys.platform == 'win32':
            select_func = _win_select
        else:
            select_func = _nix_select

        buffer = bytearray(8192)
        buff_view = memoryview(buffer)
        buff_view[0:2] = CMD_DATA_PACKET
        while self._is_running:
            events = select_func()
            if len(events) == 0:
                continue
            for key, event in events:
                if event & selectors.EVENT_READ:
                    try:
                        bytes_read = key.fileobj.recv_into(buff_view[6:])
                    except EOFError:
                        # This socket has been closed, disconnect it
                        eprint("Read error, EOF Reached")
                        self.disconnect_socket(key.data)
                        self._accessory.send_accessory_command(CMD_DISCONNECT_SOCKET,
                                                               key.data)
                        continue
                    if bytes_read > 0:
                        # payload size (socket id is part of payload)
                        payload_size = bytes_read + 2
                        payload_bytes = pack('>H', payload_size)
                        id_bytes = pack('>H', key.data)
                        buff_view[2:4] = payload_bytes
                        buff_view[4:6] = id_bytes
                        length = bytes_read + 6
                        try:
                            self._handle.bulkWrite(self._out_endpoint, buff_view[:length])
                        except usb1.USBError as err:
                            eprint("Error writing data: %s" % err)
                    else:
                        # TODO: disconnect?
                        pass

    def connect_socket(self, session_id):
        """
        Attempts to connect a new socket on the requested port.  If successful,
        to socket is registered to the selector, with its session ID, and the
        socket is added to the dictionary
        """
        eprint("Connecting socket {0} on port {1}".format(session_id, self.port))
        new_sock = socket.socket()
        try:
            new_sock.connect(('localhost', self.port))
        except socket.error as err:
            eprint("Unable to connect to socket:")
            eprint(err)
            resp = pack('>HH', session_id, 0)
            self.send_accessory_command(CMD_CONNECTION_RESP, resp)
            return False
        else:
            eprint("Socket Connected")
            new_sock.setblocking(False)
            try:
                # store the socket Id in the selector
                self._socket_selector.register(new_sock, selectors.EVENT_READ, session_id)
            except KeyError:
                # somehow selector already registered
                pass

            # Add to map associating socket IDs with sockets
            self._socket_dict[session_id] = new_sock
            resp = pack('>HH', session_id, 1)
            self.send_accessory_command(CMD_CONNECTION_RESP, resp)
            return True


    def disconnect_socket(self, session_id):
        eprint("Disconnecting socket: {0}".format(session_id))
        try:
            sock = self._socket_dict[session_id]
            self._socket_selector.unregister(sock)
            del self._socket_dict[session_id]
        except KeyError:
            pass
        finally:
            # TODO: send command back to android?
            if sock:
                sock.close()

    def get_socket(self, session_id):
        """
        Retreives a socket from the stored dictionary
        """
        return self._socket_dict.get(session_id)

    def send_accessory_command(self, command, data=None):
        if not data:
            # empty payload
            packet = command + pack('>H', 0)
        elif isinstance(data, bytes) or isinstance(data, bytearray):
            # payload size is variable
            length = len(data)
            packet = command + pack('>H', length) + data
        elif isinstance(data, int):
            # payload is unsigned short
            packet = command + pack('>H', 2) + pack('>H', data)
        else:
            eprint('Data type not acceptable')
            return

        self._handle.bulkWrite(self._out_endpoint, packet)

    def signal_app_exit(self):
        """
        Sends an exit command to the application.  This is necessary for
        Android to cleanly exit.
        """
        if self.app_connected:
            eprint("Sending termination command to android")
            self.send_accessory_command(CMD_CLOSE_ACCESSORY)
            self.app_connected = False

    def stop(self):
        """
        Signals device if connected, Stops all threads, disconnects all sockets
        """
        if self._is_running:
            eprint("Stopping Accessory")
            self.signal_app_exit()
            self._is_running = False
            # give one second for transfers to complete
            time.sleep(1)
            for sock in self._socket_dict.values():
                try:
                    self._socket_selector.unregister(sock)
                except KeyError:
                    pass
                finally:
                    sock.close()

            self._socket_dict.clear()
            self._socket_selector.close()
            eprint("Waiting for socket thread to close...")
            self._socket_read_thread.join()
            self._handle.releaseInterface(0)
            eprint("Waiting for accessory thread to close...")
            self._accessory_read_thread.join()

    def run(self):
        try:
            # TODO: Should do something here
            while self._is_running:
                time.sleep(1)
        except SystemExit:
            pass
        finally:
            if self._handle:
                self.stop()

SHUTDOWN = False

def parse_uevent(data):
    data = data.decode('utf-8')
    lines = data.split('\0')
    attrs = {}
    for line in lines:
        val = line.split('=')
        if len(val) == 2:
            attrs[val[0]] = val[1]

    if 'ACTION' in attrs and 'PRODUCT' in attrs:
        if attrs['ACTION'] == 'add':
            parts = attrs['PRODUCT'].split('/')
            eprint(parts)
            return (int(parts[0], 16), int(parts[1], 16))

    return None, None

def check_uevent():
    try:
        sock = socket.socket(socket.AF_NETLINK, socket.SOCK_DGRAM,
                             NETLINK_KOBJECT_UEVENT)

        sock.bind((os.getpid(), -1))
        vid = 0
        pid = 0
        while True:
            try:
                data = sock.recv(512)
            except (InterruptedError, SystemExit):
                break

            try:
                vid, pid = parse_uevent(data)
            except ValueError:
                eprint("unable to parse uevent")
            else:
                if vid and pid:
                    break

        sock.close()
        return vid, pid
    except ValueError as err:
        eprint(err)

def setup_signal_exit():
    def _exit(sig, stack):
        eprint('Exiting...')
        global SHUTDOWN
        SHUTDOWN = True
        raise SystemExit

    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, _exit)


def open_accessory(vid, pid):
    with usb1.USBContext() as context:
        try:
            accessory = AndroidAccessory(context, vid, pid)
        except usb1.USBError as err:
            eprint(err)
        except SystemExit:
            pass
        else:
            accessory.run()

def main():
    # check args
    setup_signal_exit()
    vid = None
    pid = None
    if len(sys.argv) == 3:
        # Arguments should be hex vendor id and product id, convert
        # them to int
        vid = int(sys.argv[1], 16)
        pid = int(sys.argv[2], 16)
        if vid not in COMPATIBLE_VIDS:
            eprint("Vendor Id not a compatible Android Device")
            return
        elif vid == ACCESSORY_VID and pid in ACCESSORY_PID:
            eprint("Requested vid:pid combination is an accessory.\n \
                    Please use the device's standard vid/pid")
            return


    while not SHUTDOWN:
        # Initial Attempt to open
        open_accessory(vid, pid)

        if sys.platform == 'linux':
            while not SHUTDOWN:
                nvid, npid = check_uevent()
                if nvid in COMPATIBLE_VIDS:
                    if (not vid and not pid) or (vid == nvid and pid == npid):
                        open_accessory(nvid, npid)
                else:
                    eprint("Vid: {0:x} not compatible".format(nvid))
        elif not SHUTDOWN:
            # sleep for 5 seconds between connection attempts
            time.sleep(5)


if __name__ == '__main__':
    main()
