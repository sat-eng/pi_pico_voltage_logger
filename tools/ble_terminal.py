#!/usr/bin/env python3
"""Interactive terminal for the Pico voltage logger's BLE console.

Connects over Bluetooth LE to the logger's Nordic UART Service (NUS) and
lets you type console commands (help, status, show, set, save, ...).

Requires bleak:  python3 -m pip install bleak
On macOS, allow Bluetooth access for your terminal app when asked.

Usage:
  tools/ble_terminal.py                  connect to "Pico-Voltage-Logger"
  tools/ble_terminal.py --name NAME      connect to a logger with another device_name
  tools/ble_terminal.py --scan           list nearby Bluetooth LE devices
  tools/ble_terminal.py --address ADDR   connect to a specific device (on macOS
                                         addresses are UUIDs, as shown by --scan)
  echo status | tools/ble_terminal.py    run commands non-interactively

Quit with Ctrl-D (or Ctrl-C).
"""

import argparse
import asyncio
import sys
import threading

try:
    from bleak import BleakClient, BleakScanner
except ImportError:
    sys.exit("bleak is not installed. Install it with: python3 -m pip install bleak")

DEFAULT_NAME = "Pico-Voltage-Logger"
NUS_RX = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"   # we write commands here
NUS_TX = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"   # the logger notifies output here
# The logger's RX characteristic holds 20 bytes per write, and it treats
# 500 ms without input as the end of a command, so send each line as
# 20-byte writes in quick succession.
CHUNK = 20


async def scan(timeout):
    print("Scanning for {:.0f} s...".format(timeout))
    found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    rows = sorted(((adv.rssi, d.name or adv.local_name or "", d.address)
                   for d, adv in found.values()), reverse=True)
    named = [r for r in rows if r[1]]
    if not named:
        print("No named devices found.")
    for rssi, name, address in named:
        mark = "  <- logger?" if name.startswith("Pico") else ""
        print("{:4d} dBm  {:28s} {}{}".format(rssi, name, address, mark))


class EchoFilter:
    # The logger echoes each command back (localecho). The terminal already
    # shows what was typed, so drop that echo unless --show-echo is given.
    def __init__(self, enabled):
        self.enabled = enabled
        self.expect = b""

    def sent(self, line):
        line = line.strip()
        self.expect = (line + "\r\n").encode() if self.enabled and line else b""

    def filter(self, data):
        if not self.expect:
            return data
        n = 0
        limit = min(len(data), len(self.expect))
        while n < limit and data[n] == self.expect[n]:
            n += 1
        if n == limit:                      # still matching the echo
            self.expect = self.expect[n:]
            return data[n:]
        self.expect = b""                   # not an echo after all
        return data


def watch_stdin(loop, queue):
    # Deliver stdin lines to the queue; "" means end of input.
    def ready():
        line = sys.stdin.readline()
        if line == "":
            loop.remove_reader(sys.stdin.fileno())
        queue.put_nowait(line)
    try:
        loop.add_reader(sys.stdin.fileno(), ready)
    except (NotImplementedError, ValueError):
        # No add_reader for stdin (e.g. Windows): read in a daemon thread.
        def reader():
            while True:
                line = sys.stdin.readline()
                loop.call_soon_threadsafe(queue.put_nowait, line)
                if line == "":
                    return
        threading.Thread(target=reader, daemon=True).start()


async def run(args):
    if args.address:
        device = await BleakScanner.find_device_by_address(args.address, timeout=args.timeout)
    else:
        device = await BleakScanner.find_device_by_name(args.name, timeout=args.timeout)
    if device is None:
        target = args.address or args.name
        print("{} not found. Check it is powered, in range and not connected to "
              "another app, or try --scan.".format(target), file=sys.stderr)
        return 1

    loop = asyncio.get_running_loop()
    disconnected = asyncio.Event()
    echo = EchoFilter(not args.show_echo)
    interactive = sys.stdin.isatty()

    def on_notify(_, data):
        text = echo.filter(bytes(data)).decode("utf-8", "replace")
        sys.stdout.write(text.replace("\r", ""))   # the logger sends CRLF
        sys.stdout.flush()

    def on_disconnect(_):
        loop.call_soon_threadsafe(disconnected.set)

    async with BleakClient(device, disconnected_callback=on_disconnect) as client:
        if interactive:
            print("Connected to {} ({}). Type 'help' for commands; Ctrl-D to quit."
                  .format(device.name or args.name, device.address))
        await client.start_notify(NUS_TX, on_notify)

        lines = asyncio.Queue()
        watch_stdin(loop, lines)
        while True:
            get = asyncio.ensure_future(lines.get())
            gone = asyncio.ensure_future(disconnected.wait())
            done, pending = await asyncio.wait({get, gone},
                                               return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            if gone in done:
                print("\nThe logger disconnected.", file=sys.stderr)
                return 1
            line = get.result()
            if line == "":
                # End of input: give the last command time to answer.
                await asyncio.sleep(args.linger)
                break
            echo.sent(line)
            data = (line.rstrip("\r\n") + "\n").encode()
            for i in range(0, len(data), CHUNK):
                await client.write_gatt_char(NUS_RX, data[i:i + CHUNK], response=True)
    if interactive:
        print("\nDisconnected.")
    return 0


def main():
    p = argparse.ArgumentParser(
        description="Terminal for the Pico voltage logger's BLE console.")
    p.add_argument("--name", default=DEFAULT_NAME,
                   help="device name to connect to (default: %(default)s)")
    p.add_argument("--address", help="connect to this device address instead")
    p.add_argument("--scan", action="store_true", help="list nearby devices and exit")
    p.add_argument("--timeout", type=float, default=10.0,
                   help="seconds to search for the device (default: %(default)s)")
    p.add_argument("--show-echo", action="store_true",
                   help="also show the logger's echo of each command")
    p.add_argument("--linger", type=float, default=3.0,
                   help="seconds to wait for output after the last piped command")
    args = p.parse_args()
    try:
        if args.scan:
            asyncio.run(scan(args.timeout))
            return 0
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\nDisconnected.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
