import network
import socket
import struct
import time
import os
import gc
import json
import machine
import hashlib
import binascii

from machine import ADC, RTC, Pin
from config_manager import load as load_config, save as save_config, defaults as default_config, validate as validate_config


# ============================================================
# Configuration
# ============================================================

CONFIG = load_config()

WIFI_SSID = CONFIG["wifi_ssid"]
WIFI_PASSWORD = CONFIG["wifi_password"]
NTP_SERVER = CONFIG["ntp_server"]

ADC_PIN = 26

# Calibration lives in its own file so it can be redone from the web UI
# without touching config.json. Vin = slope * read_u16 + offset.
CAL_FILE = "calibration.data"
CAL_TEMP_FILE = "calibration.tmp"

# Original bench calibration, used only if calibration.data is missing
# or unreadable (and written out to create the file on first boot).
DEFAULT_CAL_SLOPE = 0.00050893
DEFAULT_CAL_OFFSET = -0.13633

CAL_SLOPE = DEFAULT_CAL_SLOPE
CAL_OFFSET = DEFAULT_CAL_OFFSET
CAL_SOURCE = "default"

SAMPLE_INTERVAL_MS = CONFIG["sample_interval_ms"]
RECORD_INTERVAL_MS = CONFIG["record_interval_ms"]
RECORD_SECONDS = RECORD_INTERVAL_MS // 1000
HISTORY_SECONDS = CONFIG["history_seconds"]
MAX_RECORDS = HISTORY_SECONDS // RECORD_SECONDS

HTTP_PORT = CONFIG["http_port"]
NTP_RESYNC_SECONDS = CONFIG["ntp_resync_seconds"]

EVENT_LOW_ENABLED = CONFIG["event_low_enabled"]
EVENT_LOW_VOLTAGE = CONFIG["event_low_voltage"]
EVENT_EXCURSION_ENABLED = CONFIG["event_excursion_enabled"]
EVENT_DROP_VOLTS = CONFIG["event_drop_volts"]
EVENT_LOW_MV = int(EVENT_LOW_VOLTAGE * 1000)
EVENT_DROP_MV = int(EVENT_DROP_VOLTS * 1000)
MAX_EVENTS = CONFIG["max_events"]
MAX_TABLE_ROWS = CONFIG["max_table_rows"]
MAX_GRAPH_POINTS = CONFIG["max_graph_points"]

DATA_FILE = "voltage.bin"

# Fixed record: Unix timestamp, avg/min/max millivolts, sample count.
RECORD_FORMAT = "<IHHHH"
RECORD_SIZE = 12
FILE_SIZE = MAX_RECORDS * RECORD_SIZE

# ============================================================
# Global state -- deliberately small and bounded
# ============================================================

adc = ADC(ADC_PIN)
rtc = RTC()
wlan = None
server = None
ble_console = None
status_led = None
led_is_on = False
led_toggle_ticks = 0

clock_valid = False
last_ntp_sync_unix = 0

sum_raw = 0
sample_count = 0
min_raw = 65535
max_raw = 0
last_sample_ticks = 0
last_record_ticks = 0

latest_epoch = 0
latest_avg_mv = 0
latest_min_mv = 0
latest_max_mv = 0
latest_sample_count = 0

# Epochs of the newest MAX_EVENTS events (newest first), found by scanning
# history up to event_scan_epoch (0 = rescan). Only records newer than that
# are scanned on later requests, instead of the whole buffer every poll.
event_epochs = []
event_scan_epoch = 0

# ============================================================
# Time
# ============================================================

NTP_DELTA_1970 = 2208988800
UNIX_TO_2000 = 946684800
PORT_EPOCH_YEAR = time.gmtime(0)[0]


def port_to_unix(t):
    if PORT_EPOCH_YEAR == 2000:
        return int(t) + UNIX_TO_2000
    return int(t)


def unix_to_port(t):
    if PORT_EPOCH_YEAR == 2000:
        return int(t) - UNIX_TO_2000
    return int(t)


def now_unix():
    return port_to_unix(time.time())


def is_leap_year(year):
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def days_in_month(year, month):
    d = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
    if month == 2 and is_leap_year(year):
        return 29
    return d[month - 1]


def weekday(year, month, day):
    # Monday=0 ... Sunday=6
    if month < 3:
        month += 12
        year -= 1
    k = year % 100
    j = year // 100
    h = (day + (13 * (month + 1)) // 5 + k + k // 4 +
         j // 4 + 5 * j) % 7
    return (h + 5) % 7


def last_sunday(year, month):
    day = days_in_month(year, month)
    return day - ((weekday(year, month, day) - 6) % 7)


def uk_offset_seconds(unix_epoch):
    # Determine UK BST from UTC calendar time.
    g = time.gmtime(unix_to_port(unix_epoch))
    year, month, day, hour = g[0], g[1], g[2], g[3]

    if month < 3 or month > 10:
        return 0
    if 3 < month < 10:
        return 3600

    last = last_sunday(year, month)

    if month == 3:
        if day > last or (day == last and hour >= 1):
            return 3600
        return 0

    # October: BST ends at 01:00 UTC on the last Sunday.
    if day < last or (day == last and hour < 1):
        return 3600
    return 0


def local_tuple(unix_epoch):
    return time.gmtime(
        unix_to_port(unix_epoch + uk_offset_seconds(unix_epoch))
    )


def format_datetime(unix_epoch):
    if not unix_epoch:
        return "--"
    t = local_tuple(unix_epoch)
    return "{:02d}/{:02d}/{:04d} {:02d}:{:02d}:{:02d}".format(
        t[2], t[1], t[0], t[3], t[4], t[5]
    )


def sync_ntp():
    global clock_valid, last_ntp_sync_unix

    print("Synchronising NTP with", NTP_SERVER)

    addr = socket.getaddrinfo(NTP_SERVER, 123)[0][-1]
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    try:
        s.settimeout(3)
        request = bytearray(48)
        request[0] = 0x1B
        s.sendto(request, addr)
        response = s.recv(48)

        if len(response) < 48:
            raise OSError("short NTP response")

        ntp_seconds = struct.unpack("!I", response[40:44])[0]
        unix_seconds = ntp_seconds - NTP_DELTA_1970

        # Convert the Unix timestamp to calendar fields using the port's
        # own epoch convention, then set the RTC directly.
        g = time.gmtime(unix_to_port(unix_seconds))
        rtc.datetime((
            g[0], g[1], g[2],
            weekday(g[0], g[1], g[2]),
            g[3], g[4], g[5], 0
        ))

        clock_valid = True
        last_ntp_sync_unix = unix_seconds
        print("NTP OK:", format_datetime(unix_seconds),
              "UK local; port epoch", PORT_EPOCH_YEAR)
        return True

    except Exception as e:
        print("NTP error:", e)
        return False

    finally:
        s.close()


# ============================================================
# ADC sampling
# ============================================================

def raw_to_mv(raw):
    v = raw * CAL_SLOPE + CAL_OFFSET
    if v < 0:
        v = 0.0
    mv = int(v * 1000.0 + 0.5)
    if mv > 65535:
        mv = 65535
    return mv


def sample_if_due():
    global sum_raw, sample_count, min_raw, max_raw, last_sample_ticks

    now = time.ticks_ms()

    if time.ticks_diff(now, last_sample_ticks) < SAMPLE_INTERVAL_MS:
        return

    # We intentionally take one real sample "now" rather than performing
    # artificial catch-up samples after a blocking operation.
    last_sample_ticks = now
    raw = adc.read_u16()

    sum_raw += raw
    sample_count += 1

    if raw < min_raw:
        min_raw = raw
    if raw > max_raw:
        max_raw = raw


def service_sampling():
    # Called from file/HTTP loops so long requests continue sampling as
    # often as practical. Also feeds the watchdog, which main.py arms
    # while an over-the-air update is unconfirmed.
    sample_if_due()
    feed_watchdog()


LIVE_BURST = 16


def read_live_raw():
    # Instantaneous reading for the web UI. A short burst average takes
    # well under a millisecond and smooths ADC noise. It does not touch
    # the logging accumulators, so recorded statistics are unaffected.
    total = 0
    for _ in range(LIVE_BURST):
        total += adc.read_u16()
    return total // LIVE_BURST


# ============================================================
# Calibration
# ============================================================

CAL_CAPTURE_SAMPLES = 2000


def valid_calibration(slope, offset):
    # Generous bounds: catch typos/garbage, not plausible hardware.
    return 1e-6 < slope < 1e-2 and -5.0 < offset < 5.0


def save_calibration(slope, offset):
    # Same temp-file + .bak pattern as config_manager.save().
    with open(CAL_TEMP_FILE, "w") as f:
        f.write('{{"slope":{:.10g},"offset":{:.10g}}}'.format(slope, offset))
    try:
        os.remove(CAL_FILE + ".bak")
    except OSError:
        pass
    try:
        os.rename(CAL_FILE, CAL_FILE + ".bak")
    except OSError:
        pass
    try:
        os.rename(CAL_TEMP_FILE, CAL_FILE)
    except Exception:
        try:
            os.rename(CAL_FILE + ".bak", CAL_FILE)
        except OSError:
            pass
        raise
    try:
        os.remove(CAL_FILE + ".bak")
    except OSError:
        pass


def apply_calibration(slope, offset, source):
    global CAL_SLOPE, CAL_OFFSET, CAL_SOURCE
    CAL_SLOPE = slope
    CAL_OFFSET = offset
    CAL_SOURCE = source


def load_calibration():
    try:
        with open(CAL_FILE, "r") as f:
            d = json.load(f)
        slope = float(d["slope"])
        offset = float(d["offset"])
        if not valid_calibration(slope, offset):
            raise ValueError("values out of range")
        is_default = (slope == DEFAULT_CAL_SLOPE and
                      offset == DEFAULT_CAL_OFFSET)
        apply_calibration(slope, offset, "default" if is_default else "file")
        print("Calibration loaded from", CAL_FILE)
    except Exception as e:
        print("Using default calibration:", e)
        apply_calibration(DEFAULT_CAL_SLOPE, DEFAULT_CAL_OFFSET, "default")
        try:
            save_calibration(DEFAULT_CAL_SLOPE, DEFAULT_CAL_OFFSET)
        except Exception as save_error:
            print("Could not create", CAL_FILE + ":", save_error)
    print("Calibration: slope={} offset={}".format(CAL_SLOPE, CAL_OFFSET))


def capture_raw():
    # Average many raw ADC reads for one calibration point. Keep the
    # logger sampling while we do it.
    total = 0
    lo = 65535
    hi = 0
    for i in range(CAL_CAPTURE_SAMPLES):
        raw = adc.read_u16()
        total += raw
        if raw < lo:
            lo = raw
        if raw > hi:
            hi = raw
        if (i & 63) == 0:
            service_sampling()
    return total / CAL_CAPTURE_SAMPLES, lo, hi


# ============================================================
# Fixed binary 24-hour ring
# ============================================================

def ensure_data_file():
    try:
        size = os.stat(DATA_FILE)[6]
    except:
        size = -1

    if size == FILE_SIZE:
        return

    print("Creating binary history file:", FILE_SIZE, "bytes")
    with open(DATA_FILE, "wb") as f:
        zero = bytes(256)
        remaining = FILE_SIZE
        while remaining > 0:
            n = min(remaining, len(zero))
            f.write(zero[:n])
            remaining -= n
            service_sampling()

    gc.collect()


def slot_for_epoch(epoch):
    return (epoch // RECORD_SECONDS) % MAX_RECORDS


def write_record(epoch, avg_mv, minimum_mv, maximum_mv, count):
    slot = slot_for_epoch(epoch)
    packed = struct.pack(
        RECORD_FORMAT,
        int(epoch),
        int(avg_mv),
        int(minimum_mv),
        int(maximum_mv),
        min(int(count), 65535)
    )

    with open(DATA_FILE, "r+b") as f:
        f.seek(slot * RECORD_SIZE)
        f.write(packed)


def read_slot(f, slot):
    f.seek(slot * RECORD_SIZE)
    b = f.read(RECORD_SIZE)
    if len(b) != RECORD_SIZE:
        return None
    r = struct.unpack(RECORD_FORMAT, b)
    if r[0] == 0:
        return None
    return r


def read_epoch(f, epoch):
    r = read_slot(f, slot_for_epoch(epoch))
    if r and abs(int(r[0]) - int(epoch)) <= RECORD_SECONDS:
        return r
    return None


def find_latest_record():
    global latest_epoch, latest_avg_mv, latest_min_mv
    global latest_max_mv, latest_sample_count
    global event_epochs, event_scan_epoch

    if not clock_valid:
        return

    now = now_unix()
    oldest = now - HISTORY_SECONDS

    best = None
    # Events are collected in the same pass, so the first dashboard load
    # does not have to scan the whole history again.
    events = []

    with open(DATA_FILE, "rb") as f:
        for slot in range(MAX_RECORDS):
            r = read_slot(f, slot)
            if r and oldest <= r[0] <= now + RECORD_SECONDS:
                if best is None or r[0] > best[0]:
                    best = r
                if is_event(r):
                    events.append(r[0])
                    if len(events) >= 2 * MAX_EVENTS:
                        events.sort(reverse=True)
                        events = events[:MAX_EVENTS]
            if (slot & 63) == 0:
                service_sampling()

    if best:
        latest_epoch = best[0]
        latest_avg_mv = best[1]
        latest_min_mv = best[2]
        latest_max_mv = best[3]
        latest_sample_count = best[4]
        events.sort(reverse=True)
        event_epochs = events[:MAX_EVENTS]
        event_scan_epoch = latest_epoch


def record_interval():
    global sum_raw, sample_count, min_raw, max_raw
    global latest_epoch, latest_avg_mv, latest_min_mv
    global latest_max_mv, latest_sample_count

    if sample_count == 0:
        return

    avg_raw = sum_raw // sample_count

    avg_mv = raw_to_mv(avg_raw)
    minimum_mv = raw_to_mv(min_raw)
    maximum_mv = raw_to_mv(max_raw)
    count = sample_count

    if clock_valid:
        epoch = now_unix()
        # Align storage timestamps to 10-second boundaries.
        epoch -= epoch % RECORD_SECONDS
        write_record(epoch, avg_mv, minimum_mv, maximum_mv, count)

        latest_epoch = epoch
        latest_avg_mv = avg_mv
        latest_min_mv = minimum_mv
        latest_max_mv = maximum_mv
        latest_sample_count = count

        print("{} AVG={:.3f} MIN={:.3f} MAX={:.3f} SAMPLES={}".format(
            format_datetime(epoch),
            avg_mv / 1000.0,
            minimum_mv / 1000.0,
            maximum_mv / 1000.0,
            count
        ))
    else:
        print("Clock invalid; interval not stored. AVG={:.3f}".format(
            avg_mv / 1000.0
        ))

    sum_raw = 0
    sample_count = 0
    min_raw = 65535
    max_raw = 0


def clear_history():
    global latest_epoch, latest_avg_mv, latest_min_mv
    global latest_max_mv, latest_sample_count
    global event_epochs, event_scan_epoch

    with open(DATA_FILE, "r+b") as f:
        zero = bytes(256)
        f.seek(0)
        remaining = FILE_SIZE
        while remaining > 0:
            n = min(remaining, len(zero))
            f.write(zero[:n])
            remaining -= n
            service_sampling()

    latest_epoch = 0
    latest_avg_mv = 0
    latest_min_mv = 0
    latest_max_mv = 0
    latest_sample_count = 0
    event_epochs = []
    event_scan_epoch = 0
    gc.collect()


# ============================================================
# BLE configuration console
# ============================================================

def _config_value_text(key, value):
    if key in ("wifi_password", "update_key"):
        return "********" if value else ""
    return str(value)


def _parse_config_value(key, text):
    current = CONFIG[key]
    if isinstance(current, bool):
        v = text.strip().lower()
        if v in ("1", "true", "yes", "on"):
            return True
        if v in ("0", "false", "no", "off"):
            return False
        raise ValueError("expected true/false")
    if isinstance(current, int):
        return int(text)
    if isinstance(current, float):
        return float(text)
    return text


def config_command(line):
    global CONFIG
    parts = line.strip().split(None, 2)
    if not parts:
        return ""
    cmd = parts[0].lower()

    if cmd == "help":
        return ("Commands: help, show, get <key>, set <key> <value>, save, "
                "status, defaults, clear-data, reboot, apply, apply --clear, "
                "update-status, rollback")

    if cmd == "update-status":
        return update_status_text()

    if cmd == "rollback":
        if not request_rollback():
            return "No previous version to roll back to."
        if ble_console:
            ble_console.write("Rolling back to the previous version; rebooting...\r\n")
        time.sleep_ms(300)
        machine.reset()

    if cmd == "show":
        lines = []
        for key in sorted(CONFIG):
            lines.append("{} = {}".format(key, _config_value_text(key, CONFIG[key])))
        return "\r\n".join(lines)

    if cmd == "get":
        if len(parts) < 2 or parts[1] not in CONFIG:
            return "ERROR: unknown/missing key"
        key = parts[1]
        return "{} = {}".format(key, _config_value_text(key, CONFIG[key]))

    if cmd == "set":
        if len(parts) < 3 or parts[1] not in CONFIG:
            return "ERROR: use set <key> <value>"
        key = parts[1]
        candidate = {k: v for k, v in CONFIG.items()}
        candidate[key] = _parse_config_value(key, parts[2])
        CONFIG = validate_config(candidate)
        return "OK: {} = {} (not saved)".format(
            key, _config_value_text(key, CONFIG[key]))

    if cmd == "save":
        CONFIG = save_config(CONFIG)
        return "Configuration saved. Use 'reboot' to activate all changes."

    if cmd == "status":
        ip = "not connected"
        if wlan and wlan.isconnected():
            ip = wlan.ifconfig()[0]
        return ("Wi-Fi: {}\r\nClock: {}\r\nLatest record: {}\r\nFree RAM: {}".format(
            ip, "valid" if clock_valid else "invalid",
            format_datetime(latest_epoch), gc.mem_free()))

    if cmd == "defaults":
        CONFIG = default_config()
        return "Defaults loaded in memory (not saved). Use 'save' or 'apply'."

    if cmd == "clear-data":
        clear_history()
        return "Stored voltage history cleared."

    if cmd == "reboot":
        if ble_console:
            ble_console.write("Rebooting...\r\n")
        time.sleep_ms(250)
        machine.reset()

    if cmd == "apply" or (cmd == "apply" and len(parts) > 1):
        do_clear = len(parts) > 1 and parts[1] == "--clear"
        CONFIG = save_config(CONFIG)
        if do_clear:
            clear_history()
        if ble_console:
            ble_console.write("Saved{}; rebooting...\r\n".format(
                " and data cleared" if do_clear else ""))
        time.sleep_ms(300)
        machine.reset()

    return "ERROR: unknown command; type 'help'"




def ble_local_echo_enabled():
    return bool(CONFIG.get("localecho", True))


def ble_welcome():
    ip = "not connected"
    if wlan and wlan.isconnected():
        ip = wlan.ifconfig()[0]

    ssid = CONFIG.get("wifi_ssid", "")
    ntp = CONFIG.get("ntp_server", "")
    low = "{} V".format(CONFIG.get("event_low_voltage", EVENT_LOW_VOLTAGE))
    if not CONFIG.get("event_low_enabled", True):
        low = "off"
    drop = "{} V".format(CONFIG.get("event_drop_volts", EVENT_DROP_VOLTS))
    if not CONFIG.get("event_excursion_enabled", False):
        drop = "off"

    return (
        "Pico Voltage Logger\r\n"
        "-------------------\r\n"
        "Wi-Fi SSID : {}\r\n"
        "Wi-Fi      : {}\r\n"
        "NTP server : {}\r\n"
        "Clock      : {}\r\n"
        "Event low  : {}\r\n"
        "Event delta: {}\r\n"
        "\r\n"
        "Quick commands:\r\n"
        "  status              current logger status\r\n"
        "  show                show all settings\r\n"
        "  set wifi_ssid NAME  change Wi-Fi network\r\n"
        "  set wifi_password P change Wi-Fi password\r\n"
        "  set ntp_server IP   change NTP server\r\n"
        "  set localecho false disable command echo\r\n"
        "  save                save changes\r\n"
        "  apply               save and reboot\r\n"
        "  help                all commands"
    ).format(
        ssid if ssid else "(not set)",
        ip,
        ntp,
        "valid" if clock_valid else "invalid",
        low,
        drop
    )


def start_ble_console():
    global ble_console
    if not CONFIG.get("ble_enabled", True):
        print("BLE console disabled in config")
        return
    try:
        from ble_console import BLEConsole
        ble_console = BLEConsole(
            CONFIG.get("device_name", "Pico-Voltage-Logger"),
            config_command,
            ble_welcome,
            ble_local_echo_enabled
        )
    except Exception as e:
        ble_console = None
        print("BLE console unavailable:", e)


def service_ble():
    if ble_console:
        ble_console.poll()


# ============================================================
# Status LED
# ============================================================

LED_FLASH_MS = 250


def start_status_led():
    global status_led
    try:
        # On the Pico W the on-board LED is driven by the CYW43 chip.
        status_led = Pin("LED", Pin.OUT)
        status_led.off()
    except Exception as e:
        status_led = None
        print("Status LED unavailable:", e)


def service_led():
    # Steady once Wi-Fi is up and the clock has been set by NTP;
    # flashing at any other time (connecting, retrying, waiting for NTP).
    global led_toggle_ticks, led_is_on
    if status_led is None:
        return

    if clock_valid and wlan and wlan.isconnected():
        if not led_is_on:
            led_is_on = True
            status_led.on()
        return

    now = time.ticks_ms()
    if time.ticks_diff(now, led_toggle_ticks) >= LED_FLASH_MS:
        led_toggle_ticks = now
        led_is_on = not led_is_on
        status_led.value(led_is_on)


# ============================================================
# Wi-Fi
# ============================================================

def connect_wifi():
    global wlan

    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)

    if wlan.isconnected():
        print("Wi-Fi:", wlan.ifconfig()[0])
        return True

    print("Connecting Wi-Fi...")
    wlan.connect(WIFI_SSID, WIFI_PASSWORD)

    start = time.ticks_ms()

    while not wlan.isconnected():
        service_sampling()
        service_led()

        if time.ticks_diff(time.ticks_ms(), start) > 20000:
            print("Wi-Fi connection timed out")
            return False

        time.sleep_ms(20)

    print("Wi-Fi connected:", wlan.ifconfig()[0])
    return True


# ============================================================
# HTTP helpers -- all output is sent in small chunks
# ============================================================

def send_all(client, data):
    if isinstance(data, str):
        data = data.encode()

    view = memoryview(data)
    sent = 0

    while sent < len(data):
        n = client.send(view[sent:])
        if not n:
            raise OSError("socket closed")
        sent += n
        service_sampling()


class SendBuffer:
    # Stands in for the socket in send_all() and collects small writes into
    # larger sends: one send per JSON record made /api/data take seconds.
    def __init__(self, client, size=1024):
        self.client = client
        self.buf = bytearray(size)
        self.n = 0

    def send(self, data):
        n = len(data)
        if self.n + n > len(self.buf):
            self.flush()
        if n > len(self.buf):
            send_all(self.client, data)
        else:
            self.buf[self.n:self.n + n] = data
            self.n += n
        return n

    def flush(self):
        if self.n:
            send_all(self.client, memoryview(self.buf)[:self.n])
            self.n = 0


def send_headers(client, content_type="application/json",
                 status="200 OK", cache=False, max_age=0):
    send_all(client, "HTTP/1.1 {}\r\n".format(status))
    send_all(client, "Content-Type: {}\r\n".format(content_type))
    send_all(client, "Connection: close\r\n")
    if max_age:
        send_all(client, "Cache-Control: public, max-age={}\r\n".format(max_age))
    elif not cache:
        send_all(client, "Cache-Control: no-store\r\n")
    send_all(client, "\r\n")


def json_string(s):
    # Strings generated here contain only timestamps/reasons, but keep
    # this safe for JSON without importing/building json.dumps objects.
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def query_value(query, key, default=None):
    if not query:
        return default
    for part in query.split("&"):
        pieces = part.split("=", 1)
        if len(pieces) == 2 and pieces[0] == key:
            return pieces[1]
    return default


# ============================================================
# Streaming API
# ============================================================

ALLOWED_WINDOWS = (600, 1800, 3600, 10800, 21600, 43200, 86400)


# Records read per flash access when building the graph.
GRAPH_READ_RECORDS = 32


def stream_graph(client, window_seconds, end_epoch):
    if latest_epoch == 0:
        send_all(client, "[]")
        return

    newest = latest_epoch
    oldest_allowed = max(newest - HISTORY_SECONDS + RECORD_SECONDS, 0)

    if not end_epoch:
        end_epoch = newest

    if end_epoch > newest:
        end_epoch = newest
    if end_epoch < oldest_allowed:
        end_epoch = oldest_allowed

    start_epoch = max(oldest_allowed, end_epoch - window_seconds)

    # Number of raw 10-second intervals per graph bucket.
    raw_points = max(1, (end_epoch - start_epoch) // RECORD_SECONDS + 1)
    stride = max(1, (raw_points + MAX_GRAPH_POINTS - 1) // MAX_GRAPH_POINTS)

    first = True

    send_all(client, "[")

    # Consecutive slots are read in blocks: one seek and read per record
    # made a 24-hour graph take several seconds.
    buf = bytearray(GRAPH_READ_RECORDS * RECORD_SIZE)
    mv = memoryview(buf)

    with open(DATA_FILE, "rb") as f:
        bucket_start = start_epoch - (start_epoch % RECORD_SECONDS)
        t = bucket_start

        while t <= end_epoch:
            b_count = 0
            b_sum = 0
            b_min = 65535
            b_max = 0
            b_epoch = 0
            left = stride

            while left > 0 and t <= end_epoch:
                slot = slot_for_epoch(t)
                n = min(left, GRAPH_READ_RECORDS, MAX_RECORDS - slot,
                        (end_epoch - t) // RECORD_SECONDS + 1)
                f.seek(slot * RECORD_SIZE)
                got = (f.readinto(mv[:n * RECORD_SIZE]) or 0) // RECORD_SIZE

                for i in range(got):
                    # Same checks as read_epoch().
                    r = struct.unpack_from(RECORD_FORMAT, buf, i * RECORD_SIZE)
                    e = t + i * RECORD_SECONDS
                    if (r[0] and abs(r[0] - e) <= RECORD_SECONDS and
                            start_epoch <= r[0] <= end_epoch):
                        b_count += 1
                        b_sum += r[1]
                        if r[2] < b_min:
                            b_min = r[2]
                        if r[3] > b_max:
                            b_max = r[3]
                        if not b_epoch:
                            b_epoch = r[0]

                t += n * RECORD_SECONDS
                left -= n
                service_sampling()

            if b_count:
                avg = b_sum // b_count

                # % formatting is about twice as fast as str.format here.
                send_all(client, "%s[%d,%d,%d,%d]" % (
                    "" if first else ",", b_epoch, avg, b_min, b_max
                ))
                first = False

    send_all(client, "]")


def stream_table(client, center_epoch):
    if latest_epoch == 0:
        send_all(client, "[]")
        return

    if center_epoch:
        start = center_epoch - (MAX_TABLE_ROWS // 2) * RECORD_SECONDS
    else:
        start = latest_epoch - (MAX_TABLE_ROWS - 1) * RECORD_SECONDS

    first = True
    send_all(client, "[")

    with open(DATA_FILE, "rb") as f:
        # Newest first, matching the previous UI.
        for i in range(MAX_TABLE_ROWS - 1, -1, -1):
            epoch = start + i * RECORD_SECONDS
            r = read_epoch(f, epoch)

            if r:
                send_all(client, "%s[%d,%d,%d,%d,%d]" % (
                    "" if first else ",", r[0], r[1], r[2], r[3], r[4]
                ))
                first = False

            service_sampling()

    send_all(client, "]")


def is_event(r):
    if not r:
        return False
    return ((EVENT_LOW_ENABLED and r[2] < EVENT_LOW_MV) or
            (EVENT_EXCURSION_ENABLED and r[3] - r[2] >= EVENT_DROP_MV))


def update_event_cache(f):
    global event_epochs, event_scan_epoch

    if latest_epoch < event_scan_epoch:
        # The clock moved backwards: the cached scan no longer fits.
        event_epochs = []
        event_scan_epoch = 0

    stop = latest_epoch - MAX_RECORDS * RECORD_SECONDS
    if not (EVENT_LOW_ENABLED or EVENT_EXCURSION_ENABLED):
        stop = latest_epoch             # detection off: nothing to scan
    if event_scan_epoch > stop:
        stop = event_scan_epoch

    # Scan newest backwards to the previous scan, at most MAX_EVENTS.
    found = []
    epoch = latest_epoch
    while epoch > stop and len(found) < MAX_EVENTS:
        if is_event(read_epoch(f, epoch)):
            found.append(epoch)
        epoch -= RECORD_SECONDS
        service_sampling()

    if found:
        event_epochs = (found + event_epochs)[:MAX_EVENTS]
    event_scan_epoch = latest_epoch


def stream_events(client):
    first = True
    send_all(client, "[")

    if latest_epoch:
        with open(DATA_FILE, "rb") as f:
            update_event_cache(f)

            for epoch in event_epochs:
                # Re-check: the slot may have been overwritten since.
                r = read_epoch(f, epoch)
                if not is_event(r):
                    continue

                send_all(client, "%s[%d,%d,%d,%d]" % (
                    "" if first else ",", r[0], r[1], r[2], r[3]
                ))
                first = False
                service_sampling()

    send_all(client, "]")


def stream_api(client, query):
    # graph, table and events are arrays of plain integers (Unix epoch, then
    # millivolts): [epoch, avg, min, max], plus samples in table rows. The
    # browser formats times and event reasons; formatting floats and UK times
    # here took several milliseconds per record on the RP2040.
    try:
        window = int(query_value(query, "window", "3600"))
    except:
        window = 3600

    if window not in ALLOWED_WINDOWS:
        window = 3600

    try:
        end_epoch = int(query_value(query, "end", "0"))
    except:
        end_epoch = 0

    try:
        table_center = int(query_value(query, "table_center", "0"))
    except:
        table_center = 0

    send_headers(client)

    now = now_unix() if clock_valid else 0

    send_all(client, "{")
    send_all(client, '"now_epoch":{},'.format(now))
    send_all(client, '"now_text":{},'.format(
        json_string(format_datetime(now)) if now else '""'
    ))
    send_all(client, '"clock_valid":{},'.format(
        "true" if clock_valid else "false"
    ))
    send_all(client, '"latest_epoch":{},'.format(latest_epoch))
    send_all(client, '"latest_avg":{:.3f},'.format(latest_avg_mv / 1000.0))
    send_all(client, '"latest_min":{:.3f},'.format(latest_min_mv / 1000.0))
    send_all(client, '"latest_max":{:.3f},'.format(latest_max_mv / 1000.0))
    send_all(client, '"latest_samples":{},'.format(latest_sample_count))
    send_all(client, '"record_seconds":{},'.format(RECORD_SECONDS))
    send_all(client, '"event_low_enabled":{},'.format(
        "true" if EVENT_LOW_ENABLED else "false"))
    send_all(client, '"event_low_voltage":{:.3f},'.format(EVENT_LOW_VOLTAGE))
    send_all(client, '"event_excursion_enabled":{},'.format(
        "true" if EVENT_EXCURSION_ENABLED else "false"))
    send_all(client, '"event_drop_volts":{:.3f},'.format(EVENT_DROP_VOLTS))

    # Return text range labels from the Pico. JavaScript therefore never
    # has to interpret the RP2040/MicroPython epoch convention.
    actual_end = end_epoch if end_epoch else latest_epoch
    if actual_end > latest_epoch:
        actual_end = latest_epoch
    actual_start = max(0, actual_end - window)

    send_all(client, '"range_start_text":{},'.format(
        json_string(format_datetime(actual_start))
    ))
    send_all(client, '"range_end_text":{},'.format(
        json_string(format_datetime(actual_end))
    ))

    send_all(client, '"graph":')
    stream_graph(client, window, end_epoch)

    send_all(client, ',"table":')
    stream_table(client, table_center)

    send_all(client, ',"events":')
    stream_events(client)

    send_all(client, "}")


# ============================================================
# Over-the-air update
#
# app.py only receives, verifies and stages a package (<name>.new files
# plus update_ready.json). main.py, the bootstrap, swaps the files in at
# the next boot, keeps the previous versions as <name>.old, and rolls
# back if the new code fails before this module confirms it.
#
# Package format (built by make_package.py):
#   b"PVLPKG1 <hex HMAC-SHA256 of everything after this line>\n"
#   JSON header line: {"version", "created", "files": [{name, size, sha256}]}
#   the files' bytes, concatenated in header order
# ============================================================

UPDATABLE_FILES = (
    "app.py", "ble_console.py", "config_manager.py",
    "index.html", "calibrate.html", "update.html", "settings.html",
    "ui.css", "ui.js",
)
UPDATE_STATE_FILE = "update_state.json"
UPDATE_READY_FILE = "update_ready.json"
ROLLBACK_REQUEST_FILE = "update_rollback.json"
UPDATE_PACKAGE_FILE = "update.pkg"
PACKAGE_MAGIC = b"PVLPKG1"
MAX_PACKAGE_BYTES = 256 * 1024
# How long new code must run, with the web server up, before it is
# confirmed and the watchdog released.
UPDATE_COMMIT_MS = 60000

WATCHDOG = None
# main.py arms the watchdog on every boot when this was set in config.json
# at boot (later BLE changes take effect after a reboot).
PERMANENT_WATCHDOG = bool(CONFIG.get("watchdog_enabled", False))
update_state = {}
reboot_requested = False
loop_start_ticks = 0


def feed_watchdog():
    if WATCHDOG is not None:
        WATCHDOG.feed()


def file_exists(path):
    try:
        os.stat(path)
        return True
    except OSError:
        return False


def remove_file(path):
    try:
        os.remove(path)
    except OSError:
        pass


def read_json_file(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_json_file(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    try:
        os.rename(tmp, path)
    except OSError:
        remove_file(path)
        os.rename(tmp, path)


def hex_digest(h):
    # MicroPython's hashlib has no hexdigest().
    return binascii.hexlify(h.digest()).decode()


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024)
            if not chunk:
                break
            h.update(chunk)
            service_sampling()
    return hex_digest(h)


class HmacSha256:
    # Streaming HMAC-SHA256 (MicroPython has no hmac module).
    def __init__(self, key):
        if len(key) > 64:
            key = hashlib.sha256(key).digest()
        key = key + bytes(64 - len(key))
        inner = bytearray(64)
        self._outer = bytearray(64)
        for i in range(64):
            inner[i] = key[i] ^ 0x36
            self._outer[i] = key[i] ^ 0x5C
        self._inner = hashlib.sha256(inner)

    def update(self, data):
        self._inner.update(data)

    def digest(self):
        outer = hashlib.sha256(self._outer)
        outer.update(self._inner.digest())
        return outer.digest()


def digests_equal(a, b):
    # Constant-time comparison.
    if len(a) != len(b):
        return False
    diff = 0
    for x, y in zip(a, b):
        diff |= x ^ y
    return diff == 0


def clear_staged_update():
    for name in UPDATABLE_FILES:
        remove_file(name + ".new")
    remove_file(UPDATE_READY_FILE)
    remove_file(UPDATE_PACKAGE_FILE)


def drain_body(client, remaining):
    # Read and discard a rejected upload so the browser gets the reply.
    while remaining > 0:
        chunk = client.recv(min(1024, remaining))
        if not chunk:
            break
        remaining -= len(chunk)
        service_sampling()


def read_header_line(client, limit):
    # Read up to and including the first b"\n", but never more than
    # `limit` bytes, so a body without a newline is rejected at once
    # instead of waiting for the socket to time out.
    line = b""
    while len(line) < limit:
        c = client.recv(1)
        if not c:
            break
        line += c
        if c == b"\n":
            break
    return line


def stage_package():
    # Unpack the verified package into <name>.new files and write the
    # update_ready.json marker that main.py acts on at the next boot.
    with open(UPDATE_PACKAGE_FILE, "rb") as f:
        header = json.loads(f.readline())
        created = int(header.get("created", 0))
        if created <= int(update_state.get("last_created", 0)):
            raise ValueError("package is not newer than the last one applied")
        entries = header.get("files", [])
        if not entries:
            raise ValueError("package contains no files")

        hashes = {}
        for entry in entries:
            name = entry["name"]
            if name not in UPDATABLE_FILES:
                raise ValueError("file cannot be updated over the air: " + name)
            left = int(entry["size"])
            h = hashlib.sha256()
            with open(name + ".new", "wb") as out:
                while left > 0:
                    chunk = f.read(min(1024, left))
                    if not chunk:
                        raise ValueError("package truncated")
                    h.update(chunk)
                    out.write(chunk)
                    left -= len(chunk)
                    service_sampling()
            if hex_digest(h) != entry["sha256"]:
                raise ValueError("hash mismatch for " + name)
            hashes[name] = entry["sha256"]

        if f.read(1):
            raise ValueError("unexpected data after the last file")

    write_json_file(UPDATE_READY_FILE, {
        "version": str(header.get("version", "")),
        "created": created,
        "files": hashes,
    })
    return str(header.get("version", "")), sorted(hashes)


def receive_update(client, length):
    # Returns (version, files). Raises ValueError with a message for the
    # browser if the upload is rejected.
    key = CONFIG.get("update_key", "")
    if length <= 0 or length > MAX_PACKAGE_BYTES:
        raise ValueError("package size must be 1..{} bytes".format(
            MAX_PACKAGE_BYTES))

    reason = None
    if not key:
        reason = "updates are disabled: set update_key over BLE"
    elif update_state.get("state") == "pending":
        reason = "the previous update is not confirmed yet"
    else:
        s = os.statvfs("/")
        if s[0] * s[3] < length * 3 + 32768:
            reason = "not enough flash space"
    if reason:
        drain_body(client, length)
        raise ValueError(reason)

    first = read_header_line(client, min(length, 100))
    parts = first.strip().split()
    if len(parts) != 2 or parts[0] != PACKAGE_MAGIC:
        drain_body(client, length - len(first))
        raise ValueError("not an update package")
    try:
        signature = binascii.unhexlify(parts[1])
    except ValueError:
        drain_body(client, length - len(first))
        raise ValueError("malformed signature")

    clear_staged_update()
    mac = HmacSha256(key.encode())
    remaining = length - len(first)
    with open(UPDATE_PACKAGE_FILE, "wb") as f:
        while remaining > 0:
            chunk = client.recv(min(1024, remaining))
            if not chunk:
                raise ValueError("connection closed during upload")
            mac.update(chunk)
            f.write(chunk)
            remaining -= len(chunk)
            service_sampling()

    try:
        if not digests_equal(mac.digest(), signature):
            raise ValueError("signature check failed (wrong update_key?)")
        return stage_package()
    except Exception:
        clear_staged_update()
        raise
    finally:
        remove_file(UPDATE_PACKAGE_FILE)


def update_status_json():
    st = update_state
    files = {}
    for name in UPDATABLE_FILES:
        if file_exists(name):
            files[name] = file_sha256(name)
    return json.dumps({
        "state": st.get("state", "none"),
        "version": st.get("version", ""),
        "attempts": st.get("attempts", 0),
        "reason": st.get("reason", ""),
        "previous": [n for n in UPDATABLE_FILES if file_exists(n + ".old")],
        "enabled": bool(CONFIG.get("update_key", "")),
        "watchdog": watchdog_status(),
        "free_ram": gc.mem_free(),
        "files": files,
    })


def update_status_text():
    st = update_state
    lines = ["Update state: {}".format(st.get("state", "none"))]
    if st.get("version"):
        lines.append("Version: {}".format(st["version"]))
    if st.get("reason"):
        lines.append("Reason: {}".format(st["reason"]))
    previous = [n for n in UPDATABLE_FILES if file_exists(n + ".old")]
    lines.append("Rollback available: {}".format(
        ", ".join(previous) if previous else "no"))
    lines.append("Updates: {}".format(
        "enabled" if CONFIG.get("update_key", "") else
        "disabled (set update_key)"))
    lines.append("Watchdog: {}".format(watchdog_status()))
    return "\r\n".join(lines)


def request_rollback():
    # main.py performs the rollback at boot.
    if not any(file_exists(n + ".old") for n in UPDATABLE_FILES):
        return False
    write_json_file(ROLLBACK_REQUEST_FILE, {"requested": True})
    return True


def check_update_commit():
    # Confirm a pending update once the new code has run for a while with
    # the web server up (so a fix can still be uploaded), then restart to
    # release the watchdog.
    if update_state.get("state") != "pending" or server is None:
        return
    if time.ticks_diff(time.ticks_ms(), loop_start_ticks) < UPDATE_COMMIT_MS:
        return
    update_state["state"] = "good"
    update_state["attempts"] = 0
    write_json_file(UPDATE_STATE_FILE, update_state)
    if PERMANENT_WATCHDOG:
        # The watchdog stays armed anyway, so there is nothing to release.
        print("Update {} confirmed".format(update_state.get("version", "")))
        return
    print("Update {} confirmed; restarting to release the watchdog".format(
        update_state.get("version", "")))
    time.sleep_ms(200)
    machine.reset()


def watchdog_status():
    if WATCHDOG is None:
        if CONFIG.get("watchdog_enabled"):
            return "off (enabled in config; reboot to arm)"
        return "off"
    if PERMANENT_WATCHDOG:
        return "on (permanent)"
    return "on (until the update is confirmed)"


# ============================================================
# Settings (web page)
# ============================================================

# update_key is never exposed on the web: it authorises firmware updates,
# and the web server has no login. The Wi-Fi password is write-only.
WEB_HIDDEN_KEYS = ("update_key",)
WEB_SECRET_KEYS = ("wifi_password",)
MAX_SETTINGS_BODY = 2048


def settings_json():
    out = {}
    for key, value in CONFIG.items():
        if key in WEB_HIDDEN_KEYS:
            continue
        if key in WEB_SECRET_KEYS:
            out[key + "_set"] = bool(value)
        else:
            out[key] = value
    return json.dumps(out)


def read_body(client, length, limit):
    if length <= 0 or length > limit:
        raise ValueError("request body must be 1..{} bytes".format(limit))
    chunks = []
    remaining = length
    while remaining > 0:
        chunk = client.recv(min(512, remaining))
        if not chunk:
            raise ValueError("connection closed")
        chunks.append(chunk)
        remaining -= len(chunk)
        service_sampling()
    return b"".join(chunks)


def apply_settings(changes):
    # Validate and save changed settings; returns the keys whose values
    # validate() adjusted (clamped or rounded). Most settings are read at
    # startup, so they take effect after a restart.
    global CONFIG
    if not isinstance(changes, dict) or not changes:
        raise ValueError("no settings given")
    candidate = {k: v for k, v in CONFIG.items()}
    for key, value in changes.items():
        if key not in CONFIG or key in WEB_HIDDEN_KEYS:
            raise ValueError("unknown setting: {}".format(key))
        if key in WEB_SECRET_KEYS and value == "":
            continue                    # blank keeps the current value
        current = CONFIG[key]
        try:
            if isinstance(current, bool):
                if not isinstance(value, bool):
                    raise ValueError()
            elif isinstance(current, int):
                value = int(value)
            elif isinstance(current, float):
                value = float(value)
            else:
                value = str(value)
        except (ValueError, TypeError):
            raise ValueError("invalid value for {}".format(key))
        candidate[key] = value
    validated = validate_config(candidate)
    adjusted = [k for k in changes if k not in WEB_SECRET_KEYS
                and validated.get(k) != candidate.get(k)]
    CONFIG = save_config(validated)
    return adjusted


# ============================================================
# Web pages
# ============================================================

# The pages, stylesheet and script are files in flash (index.html,
# calibrate.html, update.html, settings.html, ui.css, ui.js), streamed as
# they are sent. Held as string constants they took about 45 KB of RAM, and
# app.py with them inlined no longer compiled on the Pico's heap.
PAGE_FILES = {
    "/": "index.html",
    "/calibrate": "calibrate.html",
    "/update": "update.html",
    "/settings": "settings.html",
}
ASSET_FILES = {
    "/ui.css": ("ui.css", "text/css; charset=utf-8"),
    "/ui.js": ("ui.js", "application/javascript; charset=utf-8"),
}


# Browsers cache /ui.css and /ui.js for a day (fewer requests to this
# single-connection server). A content hash added to their URLs as pages are
# sent makes browsers fetch new copies after an update.
def _asset_version():
    h = hashlib.sha256()
    for name in ("ui.css", "ui.js"):
        try:
            with open(name, "rb") as f:
                while True:
                    chunk = f.read(512)
                    if not chunk:
                        break
                    h.update(chunk)
        except OSError:
            pass
    return binascii.hexlify(h.digest()[:4]).decode()


UI_VERSION = _asset_version()
ASSET_URLS = (b"/ui.css", b"/ui.js")
VERSION_TAG = b"?v=" + UI_VERSION.encode()


def send_page(client, name):
    # Line by line, so "?v=<UI_VERSION>" can be added after asset URLs.
    with open(name, "rb") as f:
        send_headers(client, "text/html; charset=utf-8", cache=True)
        out = SendBuffer(client)
        while True:
            line = f.readline()
            if not line:
                break
            for url in ASSET_URLS:
                i = line.find(url)
                if i >= 0:
                    i += len(url)
                    line = line[:i] + VERSION_TAG + line[i:]
            send_all(out, line)
        out.flush()


def send_file(client, name, content_type, max_age=0):
    with open(name, "rb") as f:
        send_headers(client, content_type, cache=True, max_age=max_age)
        buf = bytearray(768)
        view = memoryview(buf)
        while True:
            n = f.readinto(buf)
            if not n:
                break
            send_all(client, view[:n])


# ============================================================
# HTTP server
# ============================================================

def parse_request(client):
    # Only the first request line is required. Limit it explicitly.
    line = client.readline()
    if not line:
        return "", "", "", 0

    try:
        line = line.decode().strip()
    except:
        return "", "", "", 0

    parts = line.split()
    if len(parts) < 2:
        return "", "", "", 0

    method = parts[0]
    target = parts[1]

    # Consume headers without storing them; only Content-Length is kept
    # (needed to read an uploaded update package).
    length = 0
    while True:
        h = client.readline()
        if not h or h == b"\r\n" or h == b"\n":
            break
        if h[:1] in (b"C", b"c"):
            try:
                name, value = h.decode().split(":", 1)
                if name.strip().lower() == "content-length":
                    length = int(value.strip())
            except Exception:
                pass

    if "?" in target:
        path, query = target.split("?", 1)
    else:
        path, query = target, ""

    return method, path, query, length


def handle_client(client):
    global reboot_requested
    method, path, query, length = parse_request(client)

    if path in PAGE_FILES:
        send_page(client, PAGE_FILES[path])

    elif path == "/api/config" and method == "GET":
        send_headers(client)
        send_all(client, settings_json())

    elif path == "/api/config" and method == "POST":
        try:
            changes = json.loads(read_body(client, length,
                                           MAX_SETTINGS_BODY).decode())
            restart = isinstance(changes, dict) and \
                changes.pop("_restart", False) is True
            adjusted = apply_settings(changes)
        except Exception as e:
            print("Settings rejected:", e)
            send_headers(client, status="400 Bad Request")
            send_all(client, '{{"ok":false,"error":{}}}'.format(
                json_string(e)))
        else:
            # Log which settings changed, never their values.
            print("Settings saved:", ", ".join(sorted(changes)))
            send_headers(client)
            send_all(client, json.dumps(
                {"ok": True, "adjusted": adjusted, "restart": restart}))
            reboot_requested = restart

    elif path == "/api/restart" and method == "POST":
        print("Restart requested from the web page")
        send_headers(client)
        send_all(client, '{"ok":true}')
        reboot_requested = True

    elif path in ASSET_FILES:
        name, content_type = ASSET_FILES[path]
        send_file(client, name, content_type, max_age=86400)

    elif path == "/api/update/status":
        send_headers(client)
        send_all(client, update_status_json())

    elif path == "/api/update" and method == "POST":
        try:
            version, files = receive_update(client, length)
        except Exception as e:
            print("Update rejected:", e)
            send_headers(client, status="400 Bad Request")
            send_all(client, '{{"ok":false,"error":{}}}'.format(
                json_string(e)))
        else:
            print("Update {} staged: {}; rebooting".format(version, files))
            send_headers(client)
            send_all(client, json.dumps(
                {"ok": True, "version": version, "files": files}))
            reboot_requested = True

    elif path == "/api/data":
        out = SendBuffer(client)
        stream_api(out, query)
        out.flush()

    elif path == "/api/live":
        raw = read_live_raw()
        send_headers(client)
        send_all(client, '{{"volts":{:.3f},"raw":{}}}'.format(
            raw_to_mv(raw) / 1000.0, raw))

    elif path == "/api/raw":
        avg, lo, hi = capture_raw()
        send_headers(client)
        send_all(client, '{{"raw":{:.2f},"min":{},"max":{}}}'.format(
            avg, lo, hi))

    elif path == "/api/calibration" and method == "GET":
        send_headers(client)
        send_all(client,
            '{{"slope":{:.10g},"offset":{:.10g},"source":{},'
            '"default_slope":{:.10g},"default_offset":{:.10g}}}'.format(
                CAL_SLOPE, CAL_OFFSET, json_string(CAL_SOURCE),
                DEFAULT_CAL_SLOPE, DEFAULT_CAL_OFFSET))

    elif path == "/api/calibration" and method == "POST":
        try:
            slope = float(query_value(query, "slope", ""))
            offset = float(query_value(query, "offset", ""))
        except ValueError:
            slope = offset = None
        if slope is None or not valid_calibration(slope, offset):
            send_headers(client, status="400 Bad Request")
            send_all(client, '{"ok":false,"error":"invalid slope/offset"}')
        else:
            save_calibration(slope, offset)
            apply_calibration(slope, offset, "file")
            print("Calibration updated: slope={} offset={}".format(
                slope, offset))
            send_headers(client)
            send_all(client, '{"ok":true}')

    elif path == "/api/calibration/reset" and method == "POST":
        save_calibration(DEFAULT_CAL_SLOPE, DEFAULT_CAL_OFFSET)
        apply_calibration(DEFAULT_CAL_SLOPE, DEFAULT_CAL_OFFSET, "default")
        print("Calibration reset to factory default")
        send_headers(client)
        send_all(client, '{"ok":true}')

    elif path == "/api/clear" and method == "POST":
        clear_history()
        send_headers(client)
        send_all(client, '{"ok":true}')

    else:
        send_headers(client, "text/plain", "404 Not Found")
        send_all(client, "Not found")


def start_server():
    global server

    addr = socket.getaddrinfo("0.0.0.0", HTTP_PORT)[0][-1]
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(addr)
    # A page load fetches the page, ui.css, ui.js and two API calls at
    # once; connections are served one at a time, so let them queue.
    server.listen(5)
    server.settimeout(0.05)

    print("Web server listening on http://{}/".format(
        wlan.ifconfig()[0]
    ))


# ============================================================
# Main
# ============================================================

def main(watchdog=None):
    global last_sample_ticks, last_record_ticks
    global WATCHDOG, update_state, loop_start_ticks

    WATCHDOG = watchdog
    update_state = read_json_file(UPDATE_STATE_FILE) or {}

    print()
    print("Pico voltage logger starting")
    if update_state.get("state"):
        print("Update state: {} {}".format(
            update_state.get("state"), update_state.get("version", "")))
    print("Watchdog:", watchdog_status())
    print("Binary ring: {} records, {} bytes".format(
        MAX_RECORDS, FILE_SIZE
    ))

    load_calibration()

    gc.collect()
    print("Free RAM at startup:", gc.mem_free())

    last_sample_ticks = time.ticks_ms()
    last_record_ticks = last_sample_ticks

    start_status_led()
    start_ble_console()
    ensure_data_file()

    wifi_ok = connect_wifi()
    if wifi_ok:
        # NTP must succeed before timestamped history is interpreted.
        sync_ntp()
        if clock_valid:
            find_latest_record()
        start_server()
    else:
        print("Wi-Fi unavailable; BLE configuration console remains available")
    gc.collect()
    print("Free RAM before main loop:", gc.mem_free())

    last_wifi_retry_ticks = time.ticks_ms()
    loop_start_ticks = last_wifi_retry_ticks

    while True:
        sample_if_due()
        feed_watchdog()
        service_ble()
        service_led()
        check_update_commit()

        now_ticks = time.ticks_ms()

        if time.ticks_diff(now_ticks, last_record_ticks) >= RECORD_INTERVAL_MS:
            # Advance by one interval rather than setting to now, but do
            # not manufacture samples for time spent blocked.
            last_record_ticks = now_ticks
            record_interval()

        if wlan and wlan.isconnected():
            if clock_valid:
                now = now_unix()
                if now - last_ntp_sync_unix >= NTP_RESYNC_SECONDS:
                    sync_ntp()
            else:
                if time.ticks_diff(now_ticks, last_wifi_retry_ticks) >= 60000:
                    last_wifi_retry_ticks = now_ticks
                    sync_ntp()
        elif time.ticks_diff(now_ticks, last_wifi_retry_ticks) >= 60000:
            last_wifi_retry_ticks = now_ticks
            print("Retrying Wi-Fi...")
            if connect_wifi():
                sync_ntp()
                if clock_valid:
                    find_latest_record()
                if server is None:
                    start_server()

        if server is not None:
            try:
                client, addr = server.accept()
            except OSError:
                client = None
        else:
            client = None

        if client:
            try:
                client.settimeout(3)
                gc.collect()
                handle_client(client)
            except Exception as e:
                print("HTTP error:", e)
            finally:
                try:
                    client.close()
                except:
                    pass
                gc.collect()

            if reboot_requested:
                # After a staged update (main.py applies it at boot) or a
                # restart requested from the Settings page.
                time.sleep_ms(500)
                machine.reset()

        time.sleep_ms(1)


# main.py (the bootstrap) imports this module and calls main(); running
# app.py directly also works for development.
if __name__ == "__main__":
    main()
