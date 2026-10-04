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
# Web page
# ============================================================

HTML = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pico Voltage Logger</title>
<link rel="stylesheet" href="/ui.css">
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
</head>
<body data-page="dashboard" data-title="Pico Voltage Logger">
<main>
<div class="tiles">
  <div class="tile hero stale" id="liveTile">
    <div class="label">Live voltage</div>
    <div class="value"><span id="live">--</span><span class="unit">V</span></div>
    <div class="small">Instant reading, every second</div>
  </div>
  <div class="tile" style="--tile:var(--accent)">
    <div class="label">Average</div>
    <div class="value"><span id="avg">--</span><span class="unit">V</span></div>
  </div>
  <div class="tile" style="--tile:var(--band)">
    <div class="label">Minimum</div>
    <div class="value"><span id="min">--</span><span class="unit">V</span></div>
  </div>
  <div class="tile" style="--tile:var(--band)">
    <div class="label">Maximum</div>
    <div class="value"><span id="max">--</span><span class="unit">V</span></div>
  </div>
  <div class="tile">
    <div class="label">Samples / 10 s</div>
    <div class="value"><span id="samples">--</span></div>
  </div>
</div>

<div class="card">
  <div class="toolbar">
    <h2>Voltage history</h2>
    <div class="seg" role="group" aria-label="Time window">
      <button data-window="600">10 min</button><button data-window="1800">30 min</button>
      <button data-window="3600">1 h</button><button data-window="10800">3 h</button>
      <button data-window="21600">6 h</button><button data-window="43200">12 h</button>
      <button data-window="86400">24 h</button>
    </div>
  </div>
  <div class="toolbar">
    <span class="small" id="range"></span>
    <div class="seg" role="group" aria-label="Navigate">
      <button onclick="back()">&lsaquo; Earlier</button>
      <button onclick="forward()">Later &rsaquo;</button>
      <button onclick="goNow()">Now</button>
    </div>
  </div>
  <div id="chartWrap"><canvas id="chart" role="img" aria-label="Voltage history: average with minimum to maximum range"></canvas></div>
  <div class="small" style="margin-top:8px">Click the chart to centre the measurements table on that time.</div>
</div>

<div class="grid2">
  <div class="card">
    <h2>Detected events</h2>
    <div class="small" style="margin-bottom:10px">Minimum below <b id="lowThreshold">--</b> V or a
      10-second excursion of at least <b id="dropThreshold">--</b> V. Select an event to centre it.</div>
    <div class="scroll" id="events"><div class="empty">No events detected.</div></div>
  </div>
  <div class="card">
    <h2>Measurements</h2>
    <div class="scroll" id="tableScroll">
      <table>
        <thead><tr><th>Time</th><th>Average</th><th>Minimum</th><th>Maximum</th><th>Samples</th></tr></thead>
        <tbody id="measurementTable"></tbody>
      </table>
    </div>
  </div>
</div>
</main>

<script src="/ui.js"></script>
<script>
let graphWindow=3600, graphEnd=0, tableCenter=0, selectedEpoch=0, lastData=null;
const css=n=>getComputedStyle(document.documentElement).getPropertyValue(n).trim();
function rgba(hex,a){const v=parseInt(hex.slice(1),16);
  return 'rgba('+(v>>16&255)+','+(v>>8&255)+','+(v&255)+','+a+')';}

let voltageChart=null;
if(window.Chart){
  Chart.defaults.font.family='system-ui,-apple-system,"Segoe UI",Roboto,sans-serif';
  voltageChart=new Chart(document.getElementById('chart'),{
    type:'line',
    data:{labels:[],datasets:[
      {label:'Maximum',data:[],pointRadius:0,borderWidth:1,fill:false},
      {label:'Minimum',data:[],pointRadius:0,borderWidth:1,fill:'-1'},
      {label:'Average',data:[],pointRadius:0,pointHoverRadius:4,borderWidth:2},
      {label:'Low threshold',data:[],pointRadius:0,borderWidth:1.5,borderDash:[6,4]}
    ]},
    options:{
      responsive:true,maintainAspectRatio:false,animation:false,
      interaction:{mode:'index',intersect:false},
      onClick:function(event,elements,chart){
        const p=chart.getElementsAtEventForMode(event,'index',{intersect:false},false);
        if(!p.length||!lastData)return;
        const g=lastData.graph[p[0].index];
        if(g)selectGraphPoint(g.epoch);
      },
      plugins:{
        legend:{position:'bottom',onClick:()=>{},labels:{usePointStyle:true,boxWidth:24,
          generateLabels:c=>Chart.defaults.plugins.legend.labels.generateLabels(c)
            .filter(l=>l.datasetIndex!==0)
            .map(l=>{if(l.datasetIndex===1){l.text='Min to max range';l.pointStyle='rect';
              l.fillStyle=c.data.datasets[1].backgroundColor;}else{l.pointStyle='line';}return l;})}},
        tooltip:{callbacks:{label:c=>' '+c.dataset.label+': '+Number(c.parsed.y).toFixed(3)+' V'}}
      },
      scales:{x:{ticks:{maxTicksLimit:innerWidth<520?4:8,maxRotation:0}},
              y:{title:{display:true,text:'Voltage (V)'}}}
    }
  });
  applyChartTheme();
  window.addEventListener('load',()=>{applyChartTheme();voltageChart.update();});
  if(window.matchMedia)matchMedia('(prefers-color-scheme: dark)')
    .addEventListener('change',()=>{applyChartTheme();voltageChart.update();});
}else{
  document.getElementById('chartWrap').innerHTML='<div class="empty">The chart needs Chart.js, '+
    'which loads from the internet. Events and measurements still work offline.</div>';
}

function applyChartTheme(){
  const dark=window.matchMedia&&matchMedia('(prefers-color-scheme: dark)').matches;
  const acc=css('--accent')||(dark?'#3987e5':'#2a78d6'),crit=css('--crit')||'#d03b3b',
    ink2=css('--ink-2')||(dark?'#c3c7d0':'#4d5260'),muted=css('--muted')||'#7d828d',
    line=css('--line')||(dark?'#2b303b':'#e1e5ee');
  const d=voltageChart.data.datasets;
  d[0].borderColor=rgba(acc,.45);
  d[1].borderColor=rgba(acc,.45);d[1].backgroundColor=rgba(acc,.16);
  d[2].borderColor=acc;d[2].backgroundColor=acc;
  d[3].borderColor=crit;
  const o=voltageChart.options;
  o.plugins.legend.labels.color=ink2;
  for(const k of ['x','y']){o.scales[k].grid={color:line};o.scales[k].border={color:line};
    o.scales[k].ticks.color=muted;}
  o.scales.y.title.color=muted;
}

function updateButtons(){
  document.querySelectorAll('[data-window]').forEach(b=>{
    const on=Number(b.dataset.window)===graphWindow;
    b.classList.toggle('active',on);b.setAttribute('aria-pressed',on?'true':'false');
  });
}
document.querySelectorAll('[data-window]').forEach(b=>{
  b.onclick=function(){graphWindow=Number(this.dataset.window);
    graphEnd=0;tableCenter=0;selectedEpoch=0;updateButtons();refreshData();};
});

function back(){
  const base=graphEnd||(lastData?lastData.latest_epoch:0);
  if(base){graphEnd=Math.floor(base-graphWindow*0.8);refreshData();}
}
function forward(){
  if(!lastData||!graphEnd)return;
  graphEnd=Math.floor(graphEnd+graphWindow*0.8);
  if(graphEnd>=lastData.latest_epoch)graphEnd=0;
  refreshData();
}
function goNow(){graphEnd=0;tableCenter=0;selectedEpoch=0;refreshData();}

function scrollTableToEpoch(epoch){
  const rows=Array.from(document.querySelectorAll('#measurementTable tr[data-epoch]'));
  if(!rows.length)return;
  let nearest=rows[0],d=Math.abs(Number(nearest.dataset.epoch)-epoch);
  rows.forEach(r=>{const nd=Math.abs(Number(r.dataset.epoch)-epoch);
    if(nd<d){nearest=r;d=nd;}r.classList.remove('sel');});
  nearest.classList.add('sel');
  const sc=document.getElementById('tableScroll');
  sc.scrollTop=nearest.offsetTop-sc.clientHeight/2+nearest.clientHeight/2;
}
function selectGraphPoint(epoch){
  tableCenter=epoch;selectedEpoch=epoch;
  refreshData().then(()=>setTimeout(()=>scrollTableToEpoch(epoch),80));
}
function selectEvent(epoch){
  selectedEpoch=epoch;tableCenter=epoch;graphEnd=Math.floor(epoch+graphWindow/2);
  refreshData().then(()=>{
    document.getElementById('chartWrap').scrollIntoView({behavior:'smooth',block:'center'});
    setTimeout(()=>scrollTableToEpoch(epoch),150);
  });
}

function setText(id,v){document.getElementById(id).textContent=v;}
function render(data){
  lastData=data;
  ui.setStatus(data.clock_valid?'Logging · '+(data.now_text||'').slice(-8):'Waiting for NTP',
    data.clock_valid?'good':'warn');
  const has=data.latest_epoch>0;
  setText('avg',has?data.latest_avg.toFixed(3):'--');
  setText('min',has?data.latest_min.toFixed(3):'--');
  setText('max',has?data.latest_max.toFixed(3):'--');
  setText('samples',has?data.latest_samples:'--');
  setText('lowThreshold',data.event_low_voltage.toFixed(2));
  setText('dropThreshold',data.event_drop_volts.toFixed(2));
  setText('range',has?data.range_start_text+' – '+data.range_end_text:'No data recorded yet');

  if(voltageChart){
    const ds=voltageChart.data.datasets;
    voltageChart.data.labels=data.graph.map(x=>x.time);
    ds[0].data=data.graph.map(x=>x.max);
    ds[1].data=data.graph.map(x=>x.min);
    ds[2].data=data.graph.map(x=>x.avg);
    ds[3].data=data.graph.map(()=>data.event_low_voltage);
    voltageChart.update();
  }

  let th='';
  data.table.forEach(r=>{
    th+='<tr data-epoch="'+r.epoch+'"'+(r.epoch===selectedEpoch?' class="sel"':'')+'>'+
      '<td>'+r.time+'</td><td>'+r.avg.toFixed(3)+'</td><td>'+r.min.toFixed(3)+'</td>'+
      '<td>'+r.max.toFixed(3)+'</td><td>'+r.samples+'</td></tr>';
  });
  document.getElementById('measurementTable').innerHTML=th||
    '<tr><td colspan="5" class="empty">No measurements yet.</td></tr>';

  let eh='';
  data.events.forEach(e=>{
    const low=e.reason.indexOf('Low')===0;
    eh+='<div class="event" role="button" tabindex="0" onclick="selectEvent('+e.epoch+')" '+
      'onkeydown="if(event.key===\'Enter\')selectEvent('+e.epoch+')">'+
      '<span class="ico" aria-hidden="true">'+(low?'▼':'↕')+'</span><div>'+
      '<b>'+e.reason+'</b><div class="small">'+e.time+'<br>avg '+e.avg.toFixed(3)+
      ' · min '+e.min.toFixed(3)+' · max '+e.max.toFixed(3)+' V</div></div></div>';
  });
  document.getElementById('events').innerHTML=eh||'<div class="empty">No events detected.</div>';
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
  }catch(e){ui.setStatus('Connection lost','crit');}
}

let liveBusy=false;
async function refreshLive(){
  if(liveBusy)return;
  liveBusy=true;
  const tile=document.getElementById('liveTile');
  try{
    const r=await fetch('/api/live',{cache:'no-store'});
    if(!r.ok)throw new Error('HTTP '+r.status);
    const d=await r.json();
    setText('live',d.volts.toFixed(3));
    tile.classList.remove('stale');
  }catch(e){tile.classList.add('stale');}
  finally{liveBusy=false;}
}

document.addEventListener('datacleared',goNow);
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
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Calibrate - Pico Voltage Logger</title>
<link rel="stylesheet" href="/ui.css">
</head>
<body data-page="calibrate" data-title="Calibrate ADC">
<main>
<div class="tiles">
  <div class="tile hero">
    <div class="label">Live voltage</div>
    <div class="value"><span id="liveV">--</span><span class="unit">V</span></div>
    <div class="small">Raw ADC <span id="liveRaw">--</span></div>
  </div>
  <div class="tile wide" style="--tile:var(--accent)">
    <div class="label">Current calibration <span id="curSource" style="text-transform:none;letter-spacing:0;font-weight:400"></span></div>
    <div class="value" style="font-size:1.15rem;margin-top:8px">Vin = <span id="curSlope">--</span> &times; ADC + <span id="curOffset">--</span></div>
    <div class="small">Factory default: <span id="defSlope">--</span> &times; ADC + <span id="defOffset">--</span></div>
  </div>
</div>

<div class="card">
  <h2>Reference points</h2>
  <ol class="small" style="margin:0 0 12px;padding-left:20px">
    <li>Connect a variable supply to the logger input.</li>
    <li>Set it to roughly the target voltage and measure it with a multimeter.</li>
    <li>Enter the multimeter reading as <b>Actual V</b>, then press <b>Capture</b>.
        Keep the supply steady while it samples.</li>
    <li>Repeat for each row (at least two), then press <b>Calculate</b>.</li>
  </ol>
  <div class="scroll" style="max-height:none">
  <table class="pts">
    <thead><tr><th>Target</th><th>Actual V</th><th></th><th>Raw ADC</th>
      <th>Noise (min&ndash;max)</th><th>Fitted V</th><th>Error</th></tr></thead>
    <tbody id="points"></tbody>
  </table>
  </div>
  <div class="row" style="margin-top:12px">
    <button class="btn" onclick="addPoint()">Add point</button>
    <button class="btn primary" onclick="calculate()">Calculate</button>
  </div>
  <div id="result" style="margin-top:12px"></div>
  <div class="row" style="margin-top:12px">
    <button class="btn primary" id="saveBtn" onclick="saveCal()" disabled>Save new calibration</button>
    <button class="btn danger" onclick="resetCal()">Restore factory default</button>
    <span id="action"></span>
  </div>
</div>
</main>

<script src="/ui.js"></script>
<script>
let points=[3,6,9,12].map(t=>({target:t,actual:t,raw:null,lo:null,hi:null}));
let fit=null;

function fmt(x,d){return x===null||x===undefined?'--':Number(x).toFixed(d);}

function drawPoints(){
  let h='';
  points.forEach((p,i)=>{
    let fv='--',err='--',cls='';
    if(fit&&p.raw!==null){
      const v=fit.slope*p.raw+fit.offset, e=v-p.actual;
      fv=v.toFixed(3);err=(e>=0?'+':'')+e.toFixed(3);
      cls=Math.abs(e)<0.05?'ok':'bad';
    }
    h+='<tr><td>'+p.target+' V</td>'+
      '<td><input type="number" step="0.001" style="width:7em;text-align:right" value="'+p.actual+'" '+
      'aria-label="Actual volts for '+p.target+' V point" onchange="setActual('+i+',this.value)"></td>'+
      '<td><button class="btn" onclick="capture('+i+',this)">Capture</button></td>'+
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
  }catch(e){ui.toast('Capture failed: '+e.message);}
  drawPoints();
}

function calculate(){
  const p=points.filter(x=>x.raw!==null&&!isNaN(x.actual));
  const res=document.getElementById('result');
  if(p.length<2){res.innerHTML='<span class="bad">Capture at least two points.</span>';return;}
  // Least-squares straight line: actual = slope*raw + offset.
  const n=p.length;
  const mx=p.reduce((s,x)=>s+x.raw,0)/n, my=p.reduce((s,x)=>s+x.actual,0)/n;
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
    offset.toFixed(5)+'</b><br><span class="small">Worst-case error at the reference points: '+
    worst.toFixed(3)+' V</span>';
  document.getElementById('saveBtn').disabled=false;
  drawPoints();
}

async function saveCal(){
  if(!fit||!confirm('Save and apply the new calibration?'))return;
  await post('/api/calibration?slope='+fit.slope.toPrecision(10)+
    '&offset='+fit.offset.toPrecision(10),'Calibration saved');
}

async function resetCal(){
  if(!confirm('Restore the factory default calibration?'))return;
  await post('/api/calibration/reset','Factory default restored');
}

async function post(u,msg){
  try{
    const r=await fetch(u,{method:'POST'});
    const d=await r.json();
    if(!r.ok||!d.ok)throw new Error(d.error||('HTTP '+r.status));
    ui.toast(msg);
    loadCurrent();
  }catch(e){ui.toast('Failed: '+e.message);}
}

async function loadCurrent(){
  try{
    const d=await (await fetch('/api/calibration',{cache:'no-store'})).json();
    document.getElementById('curSlope').textContent=d.slope.toPrecision(6);
    document.getElementById('curOffset').textContent=d.offset.toFixed(5);
    document.getElementById('curSource').textContent=
      d.source==='file'?'(calibrated)':'(factory default)';
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


# Shared by all three pages (served as /ui.css and /ui.js) so the
# styling and menu exist once in RAM rather than in every page.
UI_CSS = r''':root{color-scheme:light;--plane:#eef2f8;--surface:#fff;--surface-2:#f5f7fb;--ink:#151821;--ink-2:#4d5260;--muted:#7d828d;--line:#e1e5ee;--accent:#2a78d6;--band:#86b6ef;--accent-soft:#e7f0fc;--good:#0ca30c;--warn:#fab219;--crit:#d03b3b;--hdr-a:#123f7a;--hdr-b:#2a78d6;--shadow:0 1px 2px rgba(16,24,40,.05),0 6px 18px rgba(16,24,40,.06)}
@media (prefers-color-scheme:dark){:root{color-scheme:dark;--plane:#0d1015;--surface:#171a21;--surface-2:#1f232c;--ink:#f1f3f7;--ink-2:#c3c7d0;--muted:#8b909b;--line:#2b303b;--accent:#3987e5;--band:#5598e7;--accent-soft:rgba(57,135,229,.16);--hdr-a:#0b2b55;--hdr-b:#1c5cab;--shadow:0 1px 2px rgba(0,0,0,.4)}}
*{box-sizing:border-box}
[hidden]{display:none!important}
body{margin:0;font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;background:var(--plane);color:var(--ink);line-height:1.45;-webkit-text-size-adjust:100%}
a{color:var(--accent)}
.topbar{position:sticky;top:0;z-index:20;display:flex;align-items:center;gap:12px;padding:10px 16px;background:linear-gradient(115deg,var(--hdr-a),var(--hdr-b));color:#fff;box-shadow:0 2px 12px rgba(0,0,0,.18)}
.topbar h1{flex:1;margin:0;font-size:1.05rem;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.icon-btn{display:inline-flex;align-items:center;justify-content:center;width:40px;height:40px;flex:none;border:0;border-radius:10px;background:rgba(255,255,255,.12);color:#fff;cursor:pointer}
.icon-btn:hover{background:rgba(255,255,255,.24)}
.icon-btn:focus-visible,.btn:focus-visible,.seg button:focus-visible,.drawer a:focus-visible,.drawer button:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.pill{display:inline-flex;align-items:center;gap:7px;padding:5px 11px;border-radius:999px;background:rgba(255,255,255,.15);font-size:.85rem;white-space:nowrap;font-variant-numeric:tabular-nums}
.dot{width:8px;height:8px;border-radius:50%;background:#c3c2b7;flex:none}
.dot.good{background:#3ad13a}.dot.warn{background:var(--warn)}.dot.crit{background:#ff6b6b}
.drawer{position:fixed;top:0;bottom:0;left:0;z-index:40;display:flex;flex-direction:column;width:min(290px,86vw);background:var(--surface);box-shadow:6px 0 28px rgba(0,0,0,.25);transform:translateX(-105%);transition:transform .22s ease;visibility:hidden}
.drawer.open{transform:none;visibility:visible}
.drawer-head{display:flex;align-items:center;justify-content:space-between;padding:14px 12px 14px 18px;background:linear-gradient(115deg,var(--hdr-a),var(--hdr-b));color:#fff;font-weight:600}
.drawer nav{padding:10px;overflow-y:auto}
.drawer a,.drawer button.item{display:flex;align-items:center;gap:12px;width:100%;padding:11px 12px;border:0;border-radius:10px;background:none;color:var(--ink);font:inherit;text-align:left;text-decoration:none;cursor:pointer}
.drawer a:hover,.drawer button.item:hover{background:var(--surface-2)}
.drawer a.current{background:var(--accent-soft);color:var(--accent);font-weight:600}
.drawer svg{flex:none}
.drawer hr{margin:8px 4px;border:0;border-top:1px solid var(--line)}
.drawer .danger{color:var(--crit)}
.drawer .foot{margin-top:auto;padding:12px 18px;font-size:.8rem;color:var(--muted)}
.scrim{position:fixed;inset:0;z-index:30;background:rgba(8,12,20,.45);opacity:0;pointer-events:none;transition:opacity .22s}
.scrim.open{opacity:1;pointer-events:auto}
main{max-width:1180px;margin:0 auto;padding:16px}
.card{margin-bottom:16px;padding:16px;background:var(--surface);border:1px solid var(--line);border-radius:14px;box-shadow:var(--shadow)}
.card h2{margin:0 0 10px;font-size:1rem;font-weight:600}
.small{font-size:.85rem;color:var(--muted)}
.btn{padding:8px 14px;border:1px solid var(--line);border-radius:10px;background:var(--surface);color:var(--ink);font:inherit;font-size:.9rem;cursor:pointer}
.btn:hover{background:var(--surface-2)}
.btn.primary{background:var(--accent);border-color:var(--accent);color:#fff}
.btn.primary:hover{filter:brightness(1.08)}
.btn.danger{color:var(--crit)}
.btn:disabled{opacity:.5;cursor:default;filter:none}
.row{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.ok{color:var(--good)}.bad{color:var(--crit)}
table{width:100%;border-collapse:collapse;font-size:.9rem;font-variant-numeric:tabular-nums}
th,td{padding:8px 10px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}
th:first-child,td:first-child{text-align:left}
thead th{position:sticky;top:0;background:var(--surface-2);color:var(--ink-2);font-size:.82rem;font-weight:600}
tbody tr:hover td{background:var(--surface-2)}
tr.sel td{background:var(--accent-soft)}
input{font:inherit;color:var(--ink);background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:6px 8px}
code{font-size:.9em;word-break:break-all}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:16px}
.tile{position:relative;overflow:hidden;padding:14px 16px 14px 18px;background:var(--surface);border:1px solid var(--line);border-radius:14px;box-shadow:var(--shadow)}
.tile::before{content:"";position:absolute;top:0;bottom:0;left:0;width:4px;background:var(--tile,var(--line))}
.tile .label{font-size:.76rem;font-weight:600;letter-spacing:.05em;text-transform:uppercase;color:var(--ink-2)}
.tile .value{margin-top:4px;font-size:1.75rem;font-weight:650;font-variant-numeric:tabular-nums}
.unit{margin-left:3px;font-size:.9rem;font-weight:500;color:var(--muted)}
.tile.hero,.tile.wide{grid-column:span 2}
.tile.hero{background:linear-gradient(135deg,var(--hdr-b),var(--hdr-a));border:0;color:#fff}
.tile.hero::before{display:none}
.tile.hero .label,.tile.hero .unit,.tile.hero .small{color:rgba(255,255,255,.8)}
.tile.hero .value{font-size:2.6rem;line-height:1.15}
.tile.stale .value{opacity:.45}
.toolbar{display:flex;flex-wrap:wrap;gap:8px 12px;align-items:center;justify-content:space-between;margin-bottom:10px}
.toolbar h2{margin:0}
.seg{display:inline-flex;flex-wrap:wrap;gap:2px;padding:3px;background:var(--surface-2);border:1px solid var(--line);border-radius:10px}
.seg button{padding:6px 10px;border:0;border-radius:8px;background:none;color:var(--ink-2);font:inherit;font-size:.84rem;cursor:pointer}
.seg button:hover{color:var(--ink)}
.seg button.active{background:var(--surface);color:var(--accent);font-weight:600;box-shadow:0 1px 3px rgba(0,0,0,.14)}
#chartWrap{position:relative;height:340px}
.grid2{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.4fr);gap:16px}
.grid2 .card{margin-bottom:0}
.scroll{max-height:380px;overflow:auto;border:1px solid var(--line);border-radius:10px}
.event{display:flex;gap:10px;padding:10px 12px;border-left:3px solid var(--crit);border-bottom:1px solid var(--line);cursor:pointer}
.event:hover{background:var(--surface-2)}
.event .ico{color:var(--crit);font-weight:700}
.empty{padding:28px 16px;text-align:center;color:var(--muted)}
.kv{width:auto}
.kv td{text-align:left;padding:6px 14px 6px 0;border:0}
.kv td:first-child{color:var(--ink-2)}
.form{display:grid;grid-template-columns:minmax(150px,230px) minmax(0,1fr);gap:12px 18px;align-items:start}
.form>label{padding-top:7px;font-size:.9rem;color:var(--ink-2)}
.field input[type=text],.field input[type=password]{width:100%;max-width:340px}
.field input[type=number]{width:9em}
.field .hint{margin-top:4px;font-size:.8rem;color:var(--muted)}
.field .warn{margin-top:4px;font-size:.8rem;color:var(--crit)}
.field.changed input{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
.switch{display:inline-flex;align-items:center;gap:8px;padding-top:6px;color:var(--ink)}
.switch input{width:18px;height:18px;accent-color:var(--accent)}
.savebar{position:sticky;bottom:12px;z-index:10;display:flex;flex-wrap:wrap;gap:10px;align-items:center;justify-content:space-between;box-shadow:0 6px 24px rgba(16,24,40,.18)}
.toast{position:fixed;left:50%;bottom:22px;z-index:50;max-width:calc(100% - 32px);padding:10px 16px;border-radius:10px;background:var(--ink);color:var(--surface);box-shadow:var(--shadow);transform:translateX(-50%);opacity:0;pointer-events:none;transition:opacity .2s}
.toast.show{opacity:1}
@media (max-width:600px){.form{grid-template-columns:minmax(0,1fr);gap:4px}.form>label{padding-top:10px}}
@media (max-width:900px){.grid2{grid-template-columns:minmax(0,1fr)}}
@media (max-width:520px){.tile.hero,.tile.wide{grid-column:1/-1}.seg button{padding:6px 7px}#chartWrap{height:260px}}
'''


UI_JS = r'''(function(){
// Shared header, slide-out menu and notifications for every page.
// A page sets <body data-page="..." data-title="...">.
const B=document.body,page=B.dataset.page||'';
const svg=d=>'<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'+d+'</svg>';
const I={
  menu:svg('<path d="M4 6h16M4 12h16M4 18h16"/>'),
  close:svg('<path d="M6 6l12 12M18 6L6 18"/>'),
  chart:svg('<path d="M3 3v18h18"/><path d="M7 15l4-4 3 3 5-6"/>'),
  cal:svg('<path d="M12 3v3M12 18v3M3 12h3M18 12h3"/><circle cx="12" cy="12" r="5"/>'),
  up:svg('<path d="M12 16V4M7 9l5-5 5 5"/><path d="M4 20h16"/>'),
  gear:svg('<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>'),
  trash:svg('<path d="M4 7h16M10 11v6M14 11v6"/><path d="M6 7l1 13h10l1-13M9 7V4h6v3"/>')
};
const link=(href,id,icon,text)=>'<a href="'+href+'"'+(page===id?' class="current" aria-current="page"':'')+'>'+icon+text+'</a>';

const hdr=document.createElement('header');
hdr.className='topbar';
hdr.innerHTML='<button class="icon-btn" id="menuBtn" aria-label="Open menu" aria-expanded="false" aria-controls="drawer">'+I.menu+'</button>'+
  '<h1>'+(B.dataset.title||'Pico Voltage Logger')+'</h1>'+
  '<span class="pill" id="statusPill" hidden><span class="dot" id="statusDot"></span><span class="txt" id="statusText"></span></span>';
B.prepend(hdr);

const dr=document.createElement('aside');
dr.className='drawer';dr.id='drawer';dr.setAttribute('aria-label','Menu');
dr.innerHTML='<div class="drawer-head"><span>Pico Voltage Logger</span>'+
  '<button class="icon-btn" id="menuClose" aria-label="Close menu">'+I.close+'</button></div>'+
  '<nav>'+link('/','dashboard',I.chart,'Dashboard')+link('/calibrate','calibrate',I.cal,'Calibrate ADC')+
  link('/update','update',I.up,'Firmware update')+link('/settings','settings',I.gear,'Settings')+'<hr>'+
  '<button class="item danger" id="clearBtn">'+I.trash+'Clear stored data</button></nav>'+
  '<div class="foot">24-hour voltage logger</div>';
const scrim=document.createElement('div');scrim.className='scrim';
const toastEl=document.createElement('div');toastEl.className='toast';toastEl.setAttribute('role','status');
B.append(dr,scrim,toastEl);

const btn=document.getElementById('menuBtn');
function setOpen(o){
  dr.classList.toggle('open',o);scrim.classList.toggle('open',o);
  btn.setAttribute('aria-expanded',o?'true':'false');
  if(o)(dr.querySelector('a,button.item')||dr).focus();else btn.focus();
}
btn.onclick=()=>setOpen(true);
document.getElementById('menuClose').onclick=()=>setOpen(false);
scrim.onclick=()=>setOpen(false);
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&dr.classList.contains('open'))setOpen(false);});

let toastTimer=0;
function toast(msg){
  toastEl.textContent=msg;toastEl.classList.add('show');
  clearTimeout(toastTimer);toastTimer=setTimeout(()=>toastEl.classList.remove('show'),3500);
}

document.getElementById('clearBtn').onclick=async()=>{
  setOpen(false);
  if(!confirm('Clear all stored voltage history? This cannot be undone.'))return;
  try{
    const r=await fetch('/api/clear',{method:'POST'});
    if(!r.ok)throw new Error('HTTP '+r.status);
    toast('Stored data cleared');
    document.dispatchEvent(new Event('datacleared'));
  }catch(e){toast('Clear failed: '+e.message);}
};

window.ui={
  toast:toast,
  setStatus:function(text,kind){
    document.getElementById('statusPill').hidden=false;
    document.getElementById('statusText').textContent=text;
    document.getElementById('statusDot').className='dot '+(kind||'');
  }
};
})();
'''


UPDATE_HTML = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Firmware update - Pico Voltage Logger</title>
<link rel="stylesheet" href="/ui.css">
</head>
<body data-page="update" data-title="Firmware update">
<main>
<div class="card">
  <h2>Status</h2>
  <table class="kv">
    <tr><td>State</td><td id="state">--</td></tr>
    <tr><td>Version</td><td id="version">--</td></tr>
    <tr><td>Rollback available</td><td id="previous">--</td></tr>
    <tr><td>Updates</td><td id="enabled">--</td></tr>
    <tr><td>Watchdog</td><td id="watchdog">--</td></tr>
  </table>
  <div class="small" id="reason"></div>
</div>

<div class="card">
  <h2>Upload package</h2>
  <p class="small" style="margin-top:0">Choose a <code>.pvlpkg</code> file built with
  <code>make_package.py</code>. The logger checks its signature, stages the files and
  reboots. The new version is confirmed after it has run for a minute with the web
  server up; if it fails first, the logger rolls back to the previous version.</p>
  <div class="row">
    <input type="file" id="pkg" accept=".pvlpkg" aria-label="Update package">
    <button class="btn primary" onclick="upload()">Upload and install</button>
  </div>
  <div id="action" style="margin-top:12px"></div>
</div>
</main>

<script src="/ui.js"></script>
<script>
const STATES={none:'No update installed',pending:'Pending confirmation',good:'Confirmed',
  rolled_back:'Rolled back',usb_deployed:'Deployed over USB'};

async function loadStatus(){
  try{
    const d=await (await fetch('/api/update/status',{cache:'no-store'})).json();
    document.getElementById('state').textContent=STATES[d.state]||d.state;
    document.getElementById('version').textContent=d.version||'--';
    document.getElementById('previous').textContent=d.previous.length?d.previous.join(', '):'no';
    document.getElementById('enabled').innerHTML=d.enabled?'<span class="ok">✓ enabled</span>':
      '<span class="bad">✕ disabled: set update_key over BLE</span>';
    document.getElementById('watchdog').textContent=d.watchdog||'--';
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
    a.innerHTML='<span class="ok">Installed '+(d.version||'')+' ('+d.files.join(', ')+
      '). Rebooting; this page will show when the update is confirmed.</span>';
    waitForConfirm();
  }catch(e){a.innerHTML='<span class="bad">Upload failed: '+e.message+'</span>';}
}

function waitForConfirm(){
  const a=document.getElementById('action');
  const start=Date.now();
  const t=setInterval(async()=>{
    const d=await loadStatus();
    if(d&&d.state==='good'){clearInterval(t);a.innerHTML='<span class="ok">✓ Update confirmed.</span>';}
    else if(d&&d.state==='rolled_back'){clearInterval(t);
      a.innerHTML='<span class="bad">The new version failed and was rolled back: '+d.reason+'</span>';}
    else if(Date.now()-start>300000){clearInterval(t);
      a.innerHTML='<span class="bad">No confirmation after 5 minutes; check update-status over BLE.</span>';}
  },5000);
}

loadStatus();
</script>
</body>
</html>
'''


SETTINGS_HTML = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Settings - Pico Voltage Logger</title>
<link rel="stylesheet" href="/ui.css">
</head>
<body data-page="settings" data-title="Settings">
<main>
<div id="groups"><div class="card"><div class="empty">Loading settings...</div></div></div>
<div class="card savebar">
  <span class="small" id="dirty">No unsaved changes.</span>
  <div class="row">
    <button class="btn" id="discardBtn" onclick="discard()" disabled>Discard</button>
    <button class="btn" id="saveBtn" onclick="save(false)" disabled>Save</button>
    <button class="btn primary" id="saveRestartBtn" onclick="save(true)" disabled>Save and restart</button>
    <button class="btn" onclick="restartOnly()">Restart device</button>
  </div>
</div>
</main>

<script src="/ui.js"></script>
<script>
// [key, label, type, options]; a one-element entry starts a group.
const F=[
 ['Network'],
 ['wifi_ssid','Wi-Fi network (SSID)','text',{}],
 ['wifi_password','Wi-Fi password','password',{hint:'Leave blank to keep the current password.'}],
 ['http_port','Web server port','int',{min:1,max:65535,warn:'After restarting, the page moves to the new port.'}],
 ['Time'],
 ['ntp_server','NTP server','text',{hint:'An IP address avoids DNS delays (needed with the watchdog on).'}],
 ['ntp_resync_seconds','Clock resync interval','int',{min:60,unit:'s',dur:1}],
 ['Logging'],
 ['sample_interval_ms','Sample interval','int',{min:1,unit:'ms'}],
 ['record_interval_ms','Record interval','int',{min:1000,step:1000,unit:'ms',
   warn:'Changing this erases stored history at restart.'}],
 ['history_seconds','History length','int',{min:60,unit:'s',dur:1,
   hint:'At most 20,000 records.',warn:'Changing this erases stored history at restart.'}],
 ['Events'],
 ['event_low_voltage','Low-voltage threshold','float',{step:0.01,unit:'V'}],
 ['event_drop_volts','Excursion threshold','float',{min:0,step:0.01,unit:'V'}],
 ['max_events','Events listed','int',{min:1,max:500}],
 ['Display'],
 ['max_table_rows','Measurement table rows','int',{min:10,max:500}],
 ['max_graph_points','Chart points','int',{min:60,max:1440}],
 ['Device'],
 ['device_name','BLE device name','text',{maxlength:24}],
 ['ble_enabled','BLE console','bool',{warn:'With BLE off there is no fallback if Wi-Fi fails.'}],
 ['localecho','BLE command echo','bool',{}],
 ['watchdog_enabled','Permanent watchdog','bool',{warn:'While on, USB/mpremote sessions are reset after about 8 s.'}]
];
const META={};F.forEach(f=>{if(f.length>1)META[f[0]]=f;});
let original={};

function dur(s){s=Number(s);if(!(s>0))return '';
  return s>=3600?'= '+(s/3600).toFixed(s%3600?1:0)+' h':s>=60?'= '+(s/60).toFixed(s%60?1:0)+' min':'';}
function esc(v){return String(v).replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;');}

function render(cfg){
  let h='',open=false;
  F.forEach(f=>{
    if(f.length===1){if(open)h+='</div></div>';h+='<div class="card"><h2>'+f[0]+'</h2><div class="form">';open=true;return;}
    const [k,label,type,o]=f, id='f_'+k;
    let input;
    if(type==='bool')input='<label class="switch"><input type="checkbox" id="'+id+'"'+(cfg[k]?' checked':'')+'> On</label>';
    else if(type==='password')input='<input type="password" id="'+id+'" autocomplete="new-password" placeholder="'+
      (cfg.wifi_password_set?'unchanged':'not set')+'">';
    else input='<input type="'+(type==='text'?'text':'number')+'" id="'+id+'" value="'+esc(cfg[k])+'"'+
      (o.min!==undefined?' min="'+o.min+'"':'')+(o.max!==undefined?' max="'+o.max+'"':'')+
      ' step="'+(o.step||(type==='float'?'any':1))+'"'+(o.maxlength?' maxlength="'+o.maxlength+'"':'')+'>'+
      (o.unit?'<span class="unit">'+o.unit+'</span>':'')+(o.dur?'<span class="small" id="d_'+k+'"> '+dur(cfg[k])+'</span>':'');
    h+='<label for="'+id+'">'+label+'</label><div class="field" id="r_'+k+'">'+input+
      (o.hint?'<div class="hint">'+o.hint+'</div>':'')+(o.warn?'<div class="warn" hidden>⚠ '+o.warn+'</div>':'')+'</div>';
  });
  h+='</div></div>';
  document.getElementById('groups').innerHTML=h;
  document.querySelectorAll('#groups input').forEach(i=>i.addEventListener('input',update));
}

function value(k){
  const el=document.getElementById('f_'+k), t=META[k][2];
  if(t==='bool')return el.checked;
  if(t==='int')return el.value===''?NaN:Math.round(Number(el.value));
  if(t==='float')return el.value===''?NaN:Number(el.value);
  return el.value;
}

function changes(){
  const c={};
  Object.keys(META).forEach(k=>{
    const v=value(k);
    if(META[k][2]==='password'){if(v!=='')c[k]=v;}
    else if(v!==original[k])c[k]=v;
  });
  return c;
}

function update(){
  const c=changes(), n=Object.keys(c).length;
  Object.keys(META).forEach(k=>{
    const row=document.getElementById('r_'+k), w=row.querySelector('.warn');
    row.classList.toggle('changed',k in c);
    if(w)w.hidden=!(k in c);
    const d=document.getElementById('d_'+k); if(d)d.textContent=' '+dur(value(k));
  });
  document.getElementById('dirty').textContent=n?n+' unsaved change'+(n>1?'s':'')+'.':'No unsaved changes.';
  ['discardBtn','saveBtn','saveRestartBtn'].forEach(id=>document.getElementById(id).disabled=!n);
}

async function load(){
  try{
    const r=await fetch('/api/config',{cache:'no-store'});
    if(!r.ok)throw new Error('HTTP '+r.status);
    original=await r.json();
    render(original);update();
  }catch(e){document.getElementById('groups').innerHTML=
    '<div class="card"><div class="empty">Could not load settings: '+e.message+'</div></div>';}
}

function discard(){render(original);update();}

async function save(restart){
  const c=changes();
  for(const k in c){
    const t=META[k][2];
    if((t==='int'||t==='float')&&!isFinite(c[k])){ui.toast(META[k][1]+' needs a number');return;}
  }
  const warns=Object.keys(c).filter(k=>META[k][3].warn).map(k=>'• '+META[k][1]+': '+META[k][3].warn);
  const msg=(restart?'Save and restart the logger?':'Save these settings? They take effect at the next restart.')+
    (warns.length?'\n\n'+warns.join('\n'):'');
  if(!confirm(msg))return;
  if(restart)c._restart=true;
  try{
    const r=await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(c)});
    const d=await r.json();
    if(!d.ok)throw new Error(d.error||('HTTP '+r.status));
    if(d.adjusted.length)ui.toast('Saved. Adjusted to valid values: '+d.adjusted.map(k=>META[k]?META[k][1]:k).join(', '));
    else ui.toast(restart?'Saved; restarting...':'Saved. Restart to apply.');
    if(restart)waitForRestart(c.http_port||null);
    else load();
  }catch(e){ui.toast('Save failed: '+e.message);}
}

async function restartOnly(){
  if(!confirm('Restart the logger now? It is unavailable for about 20 seconds.'))return;
  try{await fetch('/api/restart',{method:'POST'});ui.toast('Restarting...');waitForRestart(null);}
  catch(e){ui.toast('Restart failed: '+e.message);}
}

function waitForRestart(newPort){
  document.querySelectorAll('.savebar button').forEach(b=>b.disabled=true);
  const dirty=document.getElementById('dirty');
  if(newPort&&String(newPort)!==(location.port||'80')){
    const url=location.protocol+'//'+location.hostname+(newPort==80?'':':'+newPort)+'/settings';
    dirty.innerHTML='Restarting. The logger will be at <a href="'+url+'">'+url+'</a>.';
    return;
  }
  dirty.textContent='Restarting... waiting for the logger to come back.';
  const start=Date.now();
  setTimeout(function poll(){
    fetch('/api/live',{cache:'no-store'}).then(r=>{if(r.ok)location.reload();else throw 0;})
      .catch(()=>{if(Date.now()-start<120000)setTimeout(poll,3000);
        else dirty.textContent='The logger has not come back after 2 minutes. Check it over BLE.';});
  },6000);
}

load();
</script>
</body>
</html>
'''


# Browsers cache /ui.css and /ui.js for a day (fewer requests to this
# single-connection server). A content hash added to their URLs as pages are
# sent makes browsers fetch new copies after an update. Hashing in small
# slices and inserting the tag while streaming avoids copying whole pages,
# which can fail on a fragmented heap.
def _asset_version():
    h = hashlib.sha256()
    for text in (UI_CSS, UI_JS):
        for i in range(0, len(text), 512):
            h.update(text[i:i + 512].encode())
    return binascii.hexlify(h.digest()[:4]).decode()


UI_VERSION = _asset_version()
ASSET_URLS = ("/ui.css", "/ui.js")


def send_page(client, page, content_type="text/html; charset=utf-8",
              max_age=0):
    send_headers(client, content_type, cache=True, max_age=max_age)

    # Where to insert "?v=<version>" (just after each asset URL).
    cuts = []
    for url in ASSET_URLS:
        i = page.find(url)
        if i >= 0:
            cuts.append(i + len(url))
    cuts.sort()
    cuts.append(len(page))

    # Avoid page.encode() of the complete page. Send modest slices.
    pos = 0
    chunk = 768
    for cut in cuts:
        while pos < cut:
            end = min(pos + chunk, cut)
            send_all(client, page[pos:end])
            pos = end
            gc.collect()
        if cut < len(page):
            send_all(client, "?v=" + UI_VERSION)


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

    elif path == "/settings":
        send_page(client, SETTINGS_HTML)

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

    elif path == "/ui.css":
        send_page(client, UI_CSS, "text/css; charset=utf-8", max_age=86400)

    elif path == "/ui.js":
        send_page(client, UI_JS, "application/javascript; charset=utf-8",
                  max_age=86400)

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
