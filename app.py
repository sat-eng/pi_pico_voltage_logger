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

EVENT_LOW_VOLTAGE = CONFIG["event_low_voltage"]
EVENT_DROP_VOLTS = CONFIG["event_drop_volts"]
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


def format_time(unix_epoch):
    if not unix_epoch:
        return "--"
    t = local_tuple(unix_epoch)
    return "{:02d}:{:02d}:{:02d}".format(t[3], t[4], t[5])


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

    if not clock_valid:
        return

    now = now_unix()
    oldest = now - HISTORY_SECONDS

    best = None

    with open(DATA_FILE, "rb") as f:
        for slot in range(MAX_RECORDS):
            r = read_slot(f, slot)
            if r and oldest <= r[0] <= now + RECORD_SECONDS:
                if best is None or r[0] > best[0]:
                    best = r
            if (slot & 63) == 0:
                service_sampling()

    if best:
        latest_epoch = best[0]
        latest_avg_mv = best[1]
        latest_min_mv = best[2]
        latest_max_mv = best[3]
        latest_sample_count = best[4]


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
    low = CONFIG.get("event_low_voltage", EVENT_LOW_VOLTAGE)
    drop = CONFIG.get("event_drop_volts", EVENT_DROP_VOLTS)

    return (
        "Pico Voltage Logger\r\n"
        "-------------------\r\n"
        "Wi-Fi SSID : {}\r\n"
        "Wi-Fi      : {}\r\n"
        "NTP server : {}\r\n"
        "Clock      : {}\r\n"
        "Event low  : {} V\r\n"
        "Event delta: {} V\r\n"
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


def send_headers(client, content_type="application/json",
                 status="200 OK", cache=False):
    send_all(client, "HTTP/1.1 {}\r\n".format(status))
    send_all(client, "Content-Type: {}\r\n".format(content_type))
    send_all(client, "Connection: close\r\n")
    if not cache:
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

    with open(DATA_FILE, "rb") as f:
        bucket_start = start_epoch - (start_epoch % RECORD_SECONDS)
        t = bucket_start

        while t <= end_epoch:
            b_count = 0
            b_sum = 0
            b_min = 65535
            b_max = 0
            b_epoch = 0

            for _ in range(stride):
                if t > end_epoch:
                    break

                r = read_epoch(f, t)

                if r and start_epoch <= r[0] <= end_epoch:
                    b_count += 1
                    b_sum += r[1]
                    if r[2] < b_min:
                        b_min = r[2]
                    if r[3] > b_max:
                        b_max = r[3]
                    if not b_epoch:
                        b_epoch = r[0]

                t += RECORD_SECONDS
                service_sampling()

            if b_count:
                avg = b_sum // b_count

                if not first:
                    send_all(client, ",")
                first = False

                send_all(client,
                    '{{"epoch":{},"time":{},"avg":{:.3f},'
                    '"min":{:.3f},"max":{:.3f}}}'.format(
                        b_epoch,
                        json_string(format_time(b_epoch)),
                        avg / 1000.0,
                        b_min / 1000.0,
                        b_max / 1000.0
                    )
                )

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
                if not first:
                    send_all(client, ",")
                first = False

                send_all(client,
                    '{{"epoch":{},"time":{},"avg":{:.3f},'
                    '"min":{:.3f},"max":{:.3f},"samples":{}}}'.format(
                        r[0],
                        json_string(format_datetime(r[0])),
                        r[1] / 1000.0,
                        r[2] / 1000.0,
                        r[3] / 1000.0,
                        r[4]
                    )
                )

            service_sampling()

    send_all(client, "]")


def is_event(r):
    if not r:
        return False
    return (r[2] < int(EVENT_LOW_VOLTAGE * 1000) or
            (r[3] - r[2]) >= int(EVENT_DROP_VOLTS * 1000))


def stream_events(client):
    first = True
    count = 0
    send_all(client, "[")

    if latest_epoch:
        with open(DATA_FILE, "rb") as f:
            epoch = latest_epoch

            # Scan newest backwards and stop after MAX_EVENTS.
            for _ in range(MAX_RECORDS):
                if count >= MAX_EVENTS:
                    break

                r = read_epoch(f, epoch)

                if is_event(r):
                    reason = ""

                    if r[2] < int(EVENT_LOW_VOLTAGE * 1000):
                        reason = "Low {:.3f} V".format(r[2] / 1000.0)

                    excursion = r[3] - r[2]

                    if excursion >= int(EVENT_DROP_VOLTS * 1000):
                        if reason:
                            reason += " / "
                        reason += "Excursion {:.3f} V".format(
                            excursion / 1000.0
                        )

                    if not first:
                        send_all(client, ",")
                    first = False

                    send_all(client,
                        '{{"epoch":{},"time":{},"avg":{:.3f},'
                        '"min":{:.3f},"max":{:.3f},"reason":{}}}'.format(
                            r[0],
                            json_string(format_datetime(r[0])),
                            r[1] / 1000.0,
                            r[2] / 1000.0,
                            r[3] / 1000.0,
                            json_string(reason)
                        )
                    )
                    count += 1

                epoch -= RECORD_SECONDS
                service_sampling()

    send_all(client, "]")


def stream_api(client, query):
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
    send_all(client, '"event_low_voltage":{:.3f},'.format(EVENT_LOW_VOLTAGE))
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

UPDATABLE_FILES = ("app.py", "ble_console.py", "config_manager.py")
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
    print("Update {} confirmed; restarting to release the watchdog".format(
        update_state.get("version", "")))
    time.sleep_ms(200)
    machine.reset()


# ============================================================
# Web page
# ============================================================

HTML = r'''<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pico Voltage Logger</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>
body{font-family:Arial,sans-serif;margin:18px;background:#f5f5f5;color:#222}
.card{background:white;padding:16px;margin-bottom:16px;border-radius:8px}
h1,h2{margin-top:0}
.metrics{display:flex;gap:24px;flex-wrap:wrap}
.metric b{font-size:1.4em}
button{padding:7px 10px;margin:2px;cursor:pointer}
button.active{font-weight:bold;border:2px solid #333}
#chartWrap{height:360px}
.table-scroll{max-height:360px;overflow-y:auto;border:1px solid #ddd}
table{width:100%;border-collapse:collapse}
th,td{padding:6px;border-bottom:1px solid #ddd;text-align:right}
th:first-child,td:first-child{text-align:left}
thead th{position:sticky;top:0;background:white;z-index:1}
.measurement-selected{background:#fff2a8}
.event-list{max-height:240px;overflow-y:auto;border:1px solid #ddd}
.event-row{padding:8px;border-bottom:1px solid #ddd;cursor:pointer}
.event-row:hover{background:#f0f0f0}
.small{font-size:.9em;color:#555}
.live{font-size:2.6em;font-weight:bold}
.live.stale{color:#999}
</style>
</head>
<body>
<h1>Pico Voltage Logger</h1>

<div class="card">
  <h2>Live voltage</h2>
  <div><span id="live" class="live stale">--</span> V</div>
  <div class="small">Instantaneous reading, updated every second.</div>
</div>

<div class="card">
  <div><b>Time:</b> <span id="now">--</span></div>
  <div><b>Status:</b> <span id="status">Starting...</span></div>
  <div class="metrics">
    <div class="metric">Average<br><b id="avg">--</b> V</div>
    <div class="metric">Minimum<br><b id="min">--</b> V</div>
    <div class="metric">Maximum<br><b id="max">--</b> V</div>
    <div class="metric">Samples/10s<br><b id="samples">--</b></div>
  </div>
</div>

<div class="card">
  <h2>Detected events</h2>
  <div class="small">
    Event: minimum &lt; <span id="lowThreshold">--</span> V or
    10-second excursion &ge; <span id="dropThreshold">--</span> V.
    Click an event to centre it in the graph and table.
  </div>
  <div id="events" class="event-list">No events detected.</div>
</div>

<div class="card">
  <h2>Voltage history</h2>
  <div>
    <button data-window="600">10 min</button>
    <button data-window="1800">30 min</button>
    <button data-window="3600">1 hour</button>
    <button data-window="10800">3 hours</button>
    <button data-window="21600">6 hours</button>
    <button data-window="43200">12 hours</button>
    <button data-window="86400">Full</button>
    <button onclick="back()">Back</button>
    <button onclick="forward()">Forward</button>
    <button onclick="goNow()">Now</button>
  </div>
  <div id="range" class="small"></div>
  <div id="chartWrap"><canvas id="chart"></canvas></div>
  <div class="small">
    Click anywhere near a graph point to centre the table on that time.
  </div>
</div>

<div class="card">
  <h2>Measurements</h2>
  <div id="tableScroll" class="table-scroll">
    <table>
      <thead>
        <tr><th>Time</th><th>Average</th><th>Minimum</th>
            <th>Maximum</th><th>Samples</th></tr>
      </thead>
      <tbody id="measurementTable"></tbody>
    </table>
  </div>
</div>

<div class="card">
  <button onclick="clearData()">Clear stored data</button>
  <a href="/calibrate">Calibrate ADC</a> &middot;
  <a href="/update">Firmware update</a>
  <span id="action"></span>
</div>

<script>
let graphWindow=3600;
let graphEnd=0;
let tableCenter=0;
let selectedEpoch=0;
let lastData=null;

const ctx=document.getElementById('chart').getContext('2d');
const voltageChart=new Chart(ctx,{
  type:'line',
  data:{labels:[],datasets:[
    {label:'Average',data:[],pointRadius:0,borderWidth:2},
    {label:'Minimum',data:[],pointRadius:0,borderWidth:1},
    {label:'Maximum',data:[],pointRadius:0,borderWidth:1}
  ]},
  options:{
    responsive:true,maintainAspectRatio:false,animation:false,
    interaction:{mode:'index',intersect:false},
    onClick:function(event,elements,chart){
      const p=chart.getElementsAtEventForMode(
        event,'index',{intersect:false},false);
      if(!p.length||!lastData)return;
      const g=lastData.graph[p[0].index];
      if(g)selectGraphPoint(g.epoch);
    },
    scales:{y:{title:{display:true,text:'Voltage (V)'}}}
  }
});

function updateButtons(){
  document.querySelectorAll('[data-window]').forEach(b=>{
    b.classList.toggle('active',Number(b.dataset.window)===graphWindow);
  });
}

document.querySelectorAll('[data-window]').forEach(b=>{
  b.onclick=function(){
    graphWindow=Number(this.dataset.window);
    graphEnd=0;tableCenter=0;selectedEpoch=0;
    updateButtons();refreshData();
  };
});

function back(){
  const base=graphEnd||(lastData?lastData.latest_epoch:0);
  if(base){graphEnd=Math.floor(base-graphWindow*0.8);refreshData();}
}
function forward(){
  if(!lastData)return;
  if(!graphEnd){return;}
  graphEnd=Math.floor(graphEnd+graphWindow*0.8);
  if(graphEnd>=lastData.latest_epoch)graphEnd=0;
  refreshData();
}
function goNow(){
  graphEnd=0;tableCenter=0;selectedEpoch=0;refreshData();
}

function scrollTableToEpoch(epoch){
  const rows=Array.from(
    document.querySelectorAll('#measurementTable tr[data-epoch]'));
  if(!rows.length)return;
  let nearest=rows[0],d=Math.abs(Number(nearest.dataset.epoch)-epoch);
  rows.forEach(r=>{
    const nd=Math.abs(Number(r.dataset.epoch)-epoch);
    if(nd<d){nearest=r;d=nd;}
    r.classList.remove('measurement-selected');
  });
  nearest.classList.add('measurement-selected');
  const sc=document.getElementById('tableScroll');
  sc.scrollTop=nearest.offsetTop-sc.clientHeight/2+nearest.clientHeight/2;
}

function selectGraphPoint(epoch){
  tableCenter=epoch;selectedEpoch=epoch;
  refreshData().then(()=>setTimeout(()=>scrollTableToEpoch(epoch),80));
}

function selectEvent(epoch){
  selectedEpoch=epoch;tableCenter=epoch;
  graphEnd=Math.floor(epoch+graphWindow/2);
  refreshData().then(()=>{
    document.getElementById('chartWrap').scrollIntoView(
      {behavior:'smooth',block:'center'});
    setTimeout(()=>scrollTableToEpoch(epoch),150);
  });
}

function render(data){
  lastData=data;
  document.getElementById('now').textContent=data.now_text||'Clock unavailable';
  document.getElementById('status').textContent=
    data.clock_valid?'Logging':'Waiting for NTP';
  document.getElementById('avg').textContent=data.latest_avg.toFixed(3);
  document.getElementById('min').textContent=data.latest_min.toFixed(3);
  document.getElementById('max').textContent=data.latest_max.toFixed(3);
  document.getElementById('samples').textContent=data.latest_samples;
  document.getElementById('lowThreshold').textContent=
    data.event_low_voltage.toFixed(2);
  document.getElementById('dropThreshold').textContent=
    data.event_drop_volts.toFixed(2);
  document.getElementById('range').textContent=
    data.range_start_text+' — '+data.range_end_text;

  voltageChart.data.labels=data.graph.map(x=>x.time);
  voltageChart.data.datasets[0].data=data.graph.map(x=>x.avg);
  voltageChart.data.datasets[1].data=data.graph.map(x=>x.min);
  voltageChart.data.datasets[2].data=data.graph.map(x=>x.max);
  voltageChart.update();

  let th='';
  data.table.forEach(r=>{
    const cls=(r.epoch===selectedEpoch)?' class="measurement-selected"':'';
    th+='<tr data-epoch="'+r.epoch+'"'+cls+'>'+
      '<td>'+r.time+'</td><td>'+r.avg.toFixed(3)+'</td>'+
      '<td>'+r.min.toFixed(3)+'</td><td>'+r.max.toFixed(3)+'</td>'+
      '<td>'+r.samples+'</td></tr>';
  });
  document.getElementById('measurementTable').innerHTML=th;

  let eh='';
  data.events.forEach(e=>{
    eh+='<div class="event-row" onclick="selectEvent('+e.epoch+')">'+
      '<b>'+e.time+'</b> — '+e.reason+'<br>'+
      '<span class="small">Avg '+e.avg.toFixed(3)+' V, Min '+
      e.min.toFixed(3)+' V, Max '+e.max.toFixed(3)+' V</span></div>';
  });
  document.getElementById('events').innerHTML=eh||'No events detected.';
}

async function refreshData(){
  try{
    let u='/api/data?window='+graphWindow;
    if(graphEnd>0)u+='&end='+graphEnd;
    if(tableCenter>0)u+='&table_center='+tableCenter;
    const r=await fetch(u,{cache:'no-store'});
    if(!r.ok)throw new Error('HTTP '+r.status);
    const d=await r.json();
    render(d);
    return d;
  }catch(e){
    document.getElementById('status').textContent='Web error: '+e;
  }
}

async function clearData(){
  if(!confirm('Clear all stored voltage history?'))return;
  const a=document.getElementById('action');
  try{
    const r=await fetch('/api/clear',{method:'POST'});
    if(!r.ok)throw new Error('HTTP '+r.status);
    graphEnd=0;tableCenter=0;selectedEpoch=0;
    a.textContent='Data cleared';
    await refreshData();
  }catch(e){a.textContent='Clear failed: '+e;}
}

let liveBusy=false;
async function refreshLive(){
  // Skip a tick rather than queueing requests behind a slow /api/data.
  if(liveBusy)return;
  liveBusy=true;
  const el=document.getElementById('live');
  try{
    const r=await fetch('/api/live',{cache:'no-store'});
    if(!r.ok)throw new Error('HTTP '+r.status);
    const d=await r.json();
    el.textContent=d.volts.toFixed(3);
    el.classList.remove('stale');
  }catch(e){
    el.classList.add('stale');
  }finally{
    liveBusy=false;
  }
}

updateButtons();
refreshData();
refreshLive();
setInterval(refreshData,10000);
setInterval(refreshLive,1000);
</script>
</body>
</html>
'''


CAL_HTML = r'''<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Calibrate - Pico Voltage Logger</title>
<style>
body{font-family:Arial,sans-serif;margin:18px;background:#f5f5f5;color:#222}
.card{background:white;padding:16px;margin-bottom:16px;border-radius:8px}
h1,h2{margin-top:0}
button{padding:7px 10px;margin:2px;cursor:pointer}
table{width:100%;border-collapse:collapse}
th,td{padding:6px;border-bottom:1px solid #ddd;text-align:right}
th:first-child,td:first-child{text-align:left}
input{width:7em;padding:4px;text-align:right}
.small{font-size:.9em;color:#555}
.live{font-size:2em;font-weight:bold}
.ok{color:#1a7f37}.bad{color:#c62828}
.table-scroll{overflow-x:auto}
</style>
</head>
<body>
<h1>Calibration</h1>
<p><a href="/">&larr; Back to logger</a></p>

<div class="card">
  <h2>Current calibration</h2>
  <div>Vin = <b id="curSlope">--</b> &times; ADC + <b id="curOffset">--</b>
    <span class="small" id="curSource"></span></div>
  <div class="small">Factory default: <span id="defSlope">--</span> &times; ADC +
    <span id="defOffset">--</span></div>
  <p><span class="live" id="liveV">--</span> V
     &nbsp; <span class="small">raw ADC <span id="liveRaw">--</span></span></p>
</div>

<div class="card">
  <h2>Reference points</h2>
  <ol class="small">
    <li>Connect a variable supply to the logger input.</li>
    <li>Set it to roughly the target voltage and measure it with a
        multimeter.</li>
    <li>Type the multimeter reading in <b>Actual V</b>, then press
        <b>Capture</b>. Keep the supply steady while it samples.</li>
    <li>Repeat for each row (at least two), then press <b>Calculate</b>.</li>
  </ol>
  <div class="table-scroll">
  <table>
    <thead><tr><th>Target</th><th>Actual V</th><th></th><th>Raw ADC</th>
      <th>Noise (min&ndash;max)</th><th>Fitted V</th><th>Error</th></tr></thead>
    <tbody id="points"></tbody>
  </table>
  </div>
  <button onclick="addPoint()">Add point</button>
  <button onclick="calculate()">Calculate</button>
  <div id="result" style="margin-top:10px"></div>
  <div style="margin-top:10px">
    <button id="saveBtn" onclick="saveCal()" disabled>Save new calibration</button>
    <button onclick="resetCal()">Restore factory default</button>
    <span id="action"></span>
  </div>
</div>

<script>
let points=[3,6,9,12].map(t=>({target:t,actual:t,raw:null,lo:null,hi:null}));
let fit=null;

function fmt(x,d){return x===null||x===undefined?'--':Number(x).toFixed(d);}

function drawPoints(){
  let h='';
  points.forEach((p,i)=>{
    let fv='--',err='--',cls='';
    if(fit&&p.raw!==null){
      const v=fit.slope*p.raw+fit.offset;
      const e=v-p.actual;
      fv=v.toFixed(3);err=(e>=0?'+':'')+e.toFixed(3);
      cls=Math.abs(e)<0.05?'ok':'bad';
    }
    h+='<tr><td>'+p.target+' V</td>'+
      '<td><input type="number" step="0.001" value="'+p.actual+'" '+
      'onchange="setActual('+i+',this.value)"></td>'+
      '<td><button onclick="capture('+i+',this)">Capture</button></td>'+
      '<td>'+fmt(p.raw,1)+'</td>'+
      '<td>'+(p.lo===null?'--':p.lo+'&ndash;'+p.hi)+'</td>'+
      '<td>'+fv+'</td><td class="'+cls+'">'+err+'</td></tr>';
  });
  document.getElementById('points').innerHTML=h;
}

function setActual(i,v){points[i].actual=Number(v);fit=null;drawPoints();
  document.getElementById('saveBtn').disabled=true;}

function addPoint(){
  const t=prompt('Target voltage for the new point?','14');
  if(t===null||isNaN(Number(t)))return;
  points.push({target:Number(t),actual:Number(t),raw:null,lo:null,hi:null});
  drawPoints();
}

async function capture(i,btn){
  btn.disabled=true;btn.textContent='Sampling...';
  try{
    const r=await fetch('/api/raw',{cache:'no-store'});
    if(!r.ok)throw new Error('HTTP '+r.status);
    const d=await r.json();
    Object.assign(points[i],{raw:d.raw,lo:d.min,hi:d.max});
    fit=null;document.getElementById('saveBtn').disabled=true;
  }catch(e){alert('Capture failed: '+e);}
  drawPoints();
}

function calculate(){
  const p=points.filter(x=>x.raw!==null&&!isNaN(x.actual));
  const res=document.getElementById('result');
  if(p.length<2){res.innerHTML='<span class="bad">Capture at least two points.</span>';return;}
  // Least-squares straight line: actual = slope*raw + offset.
  const n=p.length;
  const mx=p.reduce((s,x)=>s+x.raw,0)/n;
  const my=p.reduce((s,x)=>s+x.actual,0)/n;
  let sxy=0,sxx=0;
  p.forEach(x=>{sxy+=(x.raw-mx)*(x.actual-my);sxx+=(x.raw-mx)*(x.raw-mx);});
  if(sxx===0){res.innerHTML='<span class="bad">Points have identical ADC readings.</span>';return;}
  const slope=sxy/sxx, offset=my-slope*mx;
  if(!(slope>1e-6&&slope<1e-2&&offset>-5&&offset<5)){
    res.innerHTML='<span class="bad">Result out of range (slope '+slope+
      ', offset '+offset+'). Check the readings.</span>';
    fit=null;drawPoints();return;
  }
  fit={slope:slope,offset:offset};
  let worst=0;
  p.forEach(x=>{worst=Math.max(worst,Math.abs(slope*x.raw+offset-x.actual));});
  res.innerHTML='New: Vin = <b>'+slope.toPrecision(6)+'</b> &times; ADC + <b>'+
    offset.toFixed(5)+'</b><br><span class="small">Worst-case error at '+
    'the reference points: '+worst.toFixed(3)+' V</span>';
  document.getElementById('saveBtn').disabled=false;
  drawPoints();
}

async function saveCal(){
  if(!fit)return;
  if(!confirm('Save and apply the new calibration?'))return;
  await post('/api/calibration?slope='+fit.slope.toPrecision(10)+
    '&offset='+fit.offset.toPrecision(10),'Saved');
}

async function resetCal(){
  if(!confirm('Restore the factory default calibration?'))return;
  await post('/api/calibration/reset','Factory default restored');
}

async function post(u,msg){
  const a=document.getElementById('action');
  try{
    const r=await fetch(u,{method:'POST'});
    const d=await r.json();
    if(!r.ok||!d.ok)throw new Error(d.error||('HTTP '+r.status));
    a.innerHTML='<span class="ok">'+msg+'</span>';
    loadCurrent();
  }catch(e){a.innerHTML='<span class="bad">Failed: '+e+'</span>';}
}

async function loadCurrent(){
  try{
    const d=await (await fetch('/api/calibration',{cache:'no-store'})).json();
    document.getElementById('curSlope').textContent=d.slope.toPrecision(6);
    document.getElementById('curOffset').textContent=d.offset.toFixed(5);
    document.getElementById('curSource').textContent=
      d.source==='file'?'(from calibration.data)':'(factory default)';
    document.getElementById('defSlope').textContent=d.default_slope.toPrecision(6);
    document.getElementById('defOffset').textContent=d.default_offset.toFixed(5);
  }catch(e){}
}

let liveBusy=false;
async function refreshLive(){
  if(liveBusy)return;
  liveBusy=true;
  try{
    const d=await (await fetch('/api/live',{cache:'no-store'})).json();
    document.getElementById('liveV').textContent=d.volts.toFixed(3);
    document.getElementById('liveRaw').textContent=d.raw;
  }catch(e){}finally{liveBusy=false;}
}

drawPoints();
loadCurrent();
refreshLive();
setInterval(refreshLive,1000);
</script>
</body>
</html>
'''


UPDATE_HTML = r'''<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Firmware update - Pico Voltage Logger</title>
<style>
body{font-family:Arial,sans-serif;margin:18px;background:#f5f5f5;color:#222}
.card{background:white;padding:16px;margin-bottom:16px;border-radius:8px}
h1,h2{margin-top:0}
button{padding:7px 10px;margin:2px;cursor:pointer}
td{padding:3px 12px 3px 0;vertical-align:top}
.small{font-size:.9em;color:#555}
.ok{color:#1a7f37}.bad{color:#c62828}
code{word-break:break-all}
</style>
</head>
<body>
<h1>Firmware update</h1>
<p><a href="/">&larr; Back to logger</a></p>

<div class="card">
  <h2>Status</h2>
  <table>
    <tr><td>State</td><td id="state">--</td></tr>
    <tr><td>Version</td><td id="version">--</td></tr>
    <tr><td>Rollback available</td><td id="previous">--</td></tr>
    <tr><td>Updates</td><td id="enabled">--</td></tr>
  </table>
  <div class="small" id="reason"></div>
</div>

<div class="card">
  <h2>Upload package</h2>
  <p class="small">Choose a <code>.pvlpkg</code> file built with
  <code>make_package.py</code>. The logger checks its signature, stages
  the files and reboots. The new version is confirmed after it has run
  for a minute with the web server up; if it fails first, the logger
  rolls back to the previous version.</p>
  <input type="file" id="pkg" accept=".pvlpkg">
  <button onclick="upload()">Upload and install</button>
  <div id="action" style="margin-top:10px"></div>
</div>

<script>
const STATES={none:'No update installed',pending:'Pending confirmation',
  good:'Confirmed',rolled_back:'Rolled back',usb_deployed:'Deployed over USB'};

async function loadStatus(){
  try{
    const d=await (await fetch('/api/update/status',{cache:'no-store'})).json();
    document.getElementById('state').textContent=STATES[d.state]||d.state;
    document.getElementById('version').textContent=d.version||'--';
    document.getElementById('previous').textContent=
      d.previous.length?d.previous.join(', '):'no';
    document.getElementById('enabled').innerHTML=d.enabled?
      '<span class="ok">enabled</span>':
      '<span class="bad">disabled: set update_key over BLE</span>';
    document.getElementById('reason').textContent=d.reason?('Reason: '+d.reason):'';
    return d;
  }catch(e){return null;}
}

async function upload(){
  const f=document.getElementById('pkg').files[0];
  const a=document.getElementById('action');
  if(!f){a.innerHTML='<span class="bad">Choose a package first.</span>';return;}
  a.textContent='Uploading '+f.name+' ('+f.size+' bytes)...';
  try{
    const r=await fetch('/api/update',{method:'POST',body:f});
    const d=await r.json();
    if(!d.ok)throw new Error(d.error||('HTTP '+r.status));
    a.innerHTML='<span class="ok">Installed '+(d.version||'')+' ('+
      d.files.join(', ')+'). Rebooting; this page will show when the '+
      'update is confirmed.</span>';
    waitForConfirm();
  }catch(e){a.innerHTML='<span class="bad">Upload failed: '+e.message+'</span>';}
}

function waitForConfirm(){
  const a=document.getElementById('action');
  const start=Date.now();
  const t=setInterval(async()=>{
    const d=await loadStatus();
    if(d&&d.state==='good'){clearInterval(t);
      a.innerHTML='<span class="ok">Update confirmed.</span>';}
    else if(d&&d.state==='rolled_back'){clearInterval(t);
      a.innerHTML='<span class="bad">The new version failed and was rolled back: '+
        d.reason+'</span>';}
    else if(Date.now()-start>300000){clearInterval(t);
      a.innerHTML='<span class="bad">No confirmation after 5 minutes; '+
        'check update-status over BLE.</span>';}
  },5000);
}

loadStatus();
</script>
</body>
</html>
'''


def send_page(client, page):
    send_headers(client, "text/html; charset=utf-8", cache=True)

    # Avoid page.encode() of the complete page. Send modest slices.
    pos = 0
    chunk = 768
    while pos < len(page):
        send_all(client, page[pos:pos + chunk])
        pos += chunk
        gc.collect()


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

    if path == "/":
        send_page(client, HTML)

    elif path == "/calibrate":
        send_page(client, CAL_HTML)

    elif path == "/update":
        send_page(client, UPDATE_HTML)

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
        stream_api(client, query)

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
    server.listen(2)
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
    if WATCHDOG is not None:
        print("Watchdog armed until the update is confirmed")
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
                # A verified update is staged; main.py applies it at boot.
                time.sleep_ms(500)
                machine.reset()

        time.sleep_ms(1)


# main.py (the bootstrap) imports this module and calls main(); running
# app.py directly also works for development.
if __name__ == "__main__":
    main()
