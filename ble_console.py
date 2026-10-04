# Minimal BLE UART-style console for MicroPython on Pico W.
# Uses the Nordic UART Service UUIDs so common BLE terminal applications
# can connect without a custom client.
import bluetooth
import struct
import time

_IRQ_CENTRAL_CONNECT = 1
_IRQ_CENTRAL_DISCONNECT = 2
_IRQ_GATTS_WRITE = 3

_FLAG_READ = bluetooth.FLAG_READ
_FLAG_WRITE = bluetooth.FLAG_WRITE
_FLAG_WRITE_NO_RESPONSE = bluetooth.FLAG_WRITE_NO_RESPONSE
_FLAG_NOTIFY = bluetooth.FLAG_NOTIFY

_UART_UUID = bluetooth.UUID("6E400001-B5A3-F393-E0A9-E50E24DCCA9E")
_UART_TX = (bluetooth.UUID("6E400003-B5A3-F393-E0A9-E50E24DCCA9E"), _FLAG_READ | _FLAG_NOTIFY)
_UART_RX = (bluetooth.UUID("6E400002-B5A3-F393-E0A9-E50E24DCCA9E"), _FLAG_WRITE | _FLAG_WRITE_NO_RESPONSE)
_UART_SERVICE = (_UART_UUID, (_UART_TX, _UART_RX))


def _advertising_payload(name):
    name = name.encode()
    payload = bytearray(b"\x02\x01\x06")
    # Complete local name (AD type 0x09).
    payload += bytes((len(name) + 1, 0x09)) + name
    return payload


class BLEConsole:
    def __init__(self, name, line_handler, connect_handler=None, echo_handler=None):
        self.ble = bluetooth.BLE()
        self.ble.active(True)
        self.ble.irq(self._irq)
        ((self.tx_handle, self.rx_handle),) = self.ble.gatts_register_services((_UART_SERVICE,))
        self.ble.gatts_set_buffer(self.rx_handle, 256, True)
        self.connections = set()
        self.line_handler = line_handler
        self.connect_handler = connect_handler
        self.echo_handler = echo_handler
        self.rx = bytearray()
        self.pending_welcome = False
        # Some BLE terminal apps send a command without CR/LF.
        self.rx_last_write_ms = None
        self.rx_idle_command_ms = 500
        self.name = name
        self._advertise()
        print("BLE console advertising as", name)

    def _advertise(self):
        self.ble.gap_advertise(250000, adv_data=_advertising_payload(self.name))

    def _irq(self, event, data):
        if event == _IRQ_CENTRAL_CONNECT:
            conn_handle, _, _ = data
            self.connections.add(conn_handle)
            # Defer the welcome until poll(), outside the IRQ callback.
            # A short delay also gives the terminal time to subscribe to
            # the TX notification characteristic.
            self.pending_welcome = True
            self.welcome_due_ms = time.ticks_add(time.ticks_ms(), 2000)
        elif event == _IRQ_CENTRAL_DISCONNECT:
            conn_handle, _, _ = data
            self.connections.discard(conn_handle)
            self._advertise()
        elif event == _IRQ_GATTS_WRITE:
            conn_handle, value_handle = data
            if value_handle == self.rx_handle:
                incoming = self.ble.gatts_read(self.rx_handle)
                if incoming:
                    self.rx.extend(incoming)
                    self.rx_last_write_ms = time.ticks_ms()

    def write(self, text):
        if not self.connections:
            return
        data = text.encode() if isinstance(text, str) else text
        # Keep notifications comfortably below common BLE MTUs.
        for pos in range(0, len(data), 20):
            part = data[pos:pos + 20]
            for conn in tuple(self.connections):
                try:
                    self.ble.gatts_notify(conn, self.tx_handle, part)
                except OSError:
                    pass

    def _run_line(self, raw):
        line = raw.decode().strip()
        if not line:
            self.write("> " )
            return
        try:
            if self.echo_handler and self.echo_handler():
                self.write(line + "\r\n")
            response = self.line_handler(line)
            if response:
                self.write(response.rstrip() + "\r\n")
        except Exception as e:
            self.write("ERROR: {}\r\n".format(e))
        self.write("> " )

    def poll(self):
        # Send the connection banner outside the BLE IRQ and after a short
        # delay so terminal applications have time to enable notifications.
        if self.pending_welcome and self.connections:
            if time.ticks_diff(time.ticks_ms(), self.welcome_due_ms) >= 0:
                self.pending_welcome = False
                try:
                    if self.connect_handler:
                        text = self.connect_handler()
                    else:
                        text = ("Pico configuration console\r\n"
                                "Type 'help' for commands.")
                    self.write("\r\n" + text.rstrip() + "\r\n> ")
                except Exception as e:
                    self.write("\r\nPico configuration console\r\n"
                               "Welcome error: {}\r\n> ".format(e))

        # Accept LF, CR, CRLF, LFCR, and terminals that send no line ending.
        while True:
            cr = -1
            lf = -1
            try:
                cr = self.rx.index(13)
            except ValueError:
                pass
            try:
                lf = self.rx.index(10)
            except ValueError:
                pass
            if cr < 0 and lf < 0:
                break
            if cr < 0:
                i = lf
            elif lf < 0:
                i = cr
            else:
                i = min(cr, lf)
            raw = bytes(self.rx[:i])
            consume = i + 1
            if consume < len(self.rx):
                first = self.rx[i]
                second = self.rx[consume]
                if ((first == 13 and second == 10) or
                        (first == 10 and second == 13)):
                    consume += 1
            # MicroPython's bytearray has no slice deletion (del rx[:n]
            # raises TypeError), so keep the remainder as a new buffer.
            self.rx = self.rx[consume:]
            self.rx_last_write_ms = time.ticks_ms() if self.rx else None
            self._run_line(raw)

        # BLE Serial Tiny can submit a complete GATT write without CR/LF.
        # Execute such buffered input after 500 ms of inactivity.
        if self.rx and self.rx_last_write_ms is not None:
            if time.ticks_diff(time.ticks_ms(), self.rx_last_write_ms) >= self.rx_idle_command_ms:
                raw = bytes(self.rx)
                self.rx = bytearray()
                self.rx_last_write_ms = None
                self._run_line(raw)
