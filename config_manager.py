import json
import os

CONFIG_FILE = "config.json"
TEMP_FILE = "config.tmp"

DEFAULTS = {
    "wifi_ssid": "WIFI_SID",
    "wifi_password": "WIFI_PW",
    "ntp_server": "192.168.0.16",
    "ntp_resync_seconds": 3600,
    "sample_interval_ms": 10,
    "record_interval_ms": 10000,
    "history_seconds": 86400,
    "http_port": 80,
    "event_low_voltage": 11.5,
    "event_drop_volts": 0.50,
    "max_events": 100,
    "max_table_rows": 100,
    "max_graph_points": 720,
    "device_name": "Pico-Voltage-Logger",
    "ble_enabled": True,
    "localecho": True,
    # Arm the hardware watchdog on every boot (hang protection for an
    # unattended unit). Off by default: it resets the board ~8 s after a USB
    # REPL/mpremote session stops the program.
    "watchdog_enabled": False,
    # Shared secret for signed over-the-air updates; empty disables them.
    "update_key": ""
}


MAX_HISTORY_RECORDS = 20000


def _finite(value, fallback):
    value = float(value)
    if value != value or value in (float("inf"), float("-inf")):
        return float(fallback)
    return value


def _copy_defaults():
    return {k: v for k, v in DEFAULTS.items()}


def validate(cfg):
    out = _copy_defaults()
    if isinstance(cfg, dict):
        for key in out:
            if key in cfg:
                out[key] = cfg[key]

    out["wifi_ssid"] = str(out["wifi_ssid"])
    out["wifi_password"] = str(out["wifi_password"])
    out["ntp_server"] = str(out["ntp_server"])
    out["device_name"] = str(out["device_name"])[:24]
    out["ble_enabled"] = bool(out["ble_enabled"])
    out["localecho"] = bool(out["localecho"])
    out["watchdog_enabled"] = bool(out["watchdog_enabled"])
    out["update_key"] = str(out["update_key"]).strip()[:128]

    out["sample_interval_ms"] = max(1, int(out["sample_interval_ms"]))
    out["record_interval_ms"] = max(1000, int(out["record_interval_ms"]))
    # Ring indexing works in whole seconds.
    out["record_interval_ms"] = (out["record_interval_ms"] // 1000) * 1000
    # The history file is created at full size, so cap it (240 KB) to
    # stay well inside the Pico's flash.
    record_seconds = out["record_interval_ms"] // 1000
    out["history_seconds"] = min(record_seconds * MAX_HISTORY_RECORDS,
                                 max(record_seconds,
                                     int(out["history_seconds"])))
    out["ntp_resync_seconds"] = max(60, int(out["ntp_resync_seconds"]))
    out["http_port"] = min(65535, max(1, int(out["http_port"])))
    out["max_events"] = min(500, max(1, int(out["max_events"])))
    out["max_table_rows"] = min(500, max(10, int(out["max_table_rows"])))
    out["max_graph_points"] = min(1440, max(60, int(out["max_graph_points"])))
    out["event_low_voltage"] = _finite(out["event_low_voltage"],
                                       DEFAULTS["event_low_voltage"])
    out["event_drop_volts"] = max(0.0, _finite(out["event_drop_volts"],
                                               DEFAULTS["event_drop_volts"]))
    return out


def load():
    try:
        with open(CONFIG_FILE, "r") as f:
            cfg = validate(json.load(f))
        print("Configuration loaded from", CONFIG_FILE)
        return cfg
    except Exception as e:
        print("Using default configuration:", e)
        cfg = _copy_defaults()
        try:
            save(cfg)
        except Exception as save_error:
            print("Could not create config.json:", save_error)
        return cfg


def save(cfg):
    cfg = validate(cfg)
    with open(TEMP_FILE, "w") as f:
        json.dump(cfg, f)
        f.flush()
    # Rename is the commit point. Remove old target first for ports whose
    # rename() does not replace an existing file.
    try:
        os.remove(CONFIG_FILE + ".bak")
    except OSError:
        pass
    try:
        os.rename(CONFIG_FILE, CONFIG_FILE + ".bak")
    except OSError:
        pass
    try:
        os.rename(TEMP_FILE, CONFIG_FILE)
    except Exception:
        try:
            os.rename(CONFIG_FILE + ".bak", CONFIG_FILE)
        except OSError:
            pass
        raise
    try:
        os.remove(CONFIG_FILE + ".bak")
    except OSError:
        pass
    return cfg


def defaults():
    return _copy_defaults()
