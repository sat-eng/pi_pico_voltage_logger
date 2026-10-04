# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

A 24-hour voltage logger for the Raspberry Pi Pico W, written in MicroPython. It samples an
analogue voltage through a resistor divider, stores compact binary history on the Pico's own
flash filesystem, and exposes both a browser-based monitoring UI (served from the Pico itself)
and a BLE configuration console. It was built for monitoring automotive 12 V circuits.

There is no build system, package manager, or test suite — this is firmware that runs directly
on the Pico's MicroPython interpreter, not a hosted Python application.

## Deploying to hardware

For a first install, copy the four project files to the Pico's filesystem (commonly via
`mpremote`):

```bash
python3 -m pip install mpremote
mpremote connect /dev/cu.usbmodemXXXX fs cp main.py :main.py
mpremote connect /dev/cu.usbmodemXXXX fs cp ble_console.py :ble_console.py
mpremote connect /dev/cu.usbmodemXXXX fs cp config_manager.py :config_manager.py
mpremote connect /dev/cu.usbmodemXXXX fs cp config.json :config.json
mpremote connect /dev/cu.usbmodemXXXX reset
```

The device's own `config.json` (real Wi-Fi credentials) and `calibration.data` (created on
first boot, updated from the `/calibrate` web page) are live state. When redeploying code, copy
only the changed `.py` files and don't overwrite these two.

Any `mpremote` command (even `fs ls`/`fs cat`) interrupts the running logger. Restart it with
`mpremote … reset`. `mpremote … soft-reset` leaves the board in raw REPL, so `main.py` does
**not** run.

`main.py` runs `main()` unconditionally at import time (bottom of the file) — it starts as soon
as it's imported/executed on the Pico, there is no `if __name__ == "__main__"` guard.

There is no local way to "run" this project off-device; verifying changes means flashing to a
Pico W (or reasoning carefully about MicroPython semantics, which differ from CPython — see
below).

## Architecture

Three files, each with a distinct responsibility:

- **`config_manager.py`** — the single source of truth for configuration shape. `DEFAULTS` is
  the canonical key set; `validate()` clamps/coerces any loaded or set config back into that
  shape (ranges, types, lengths) and silently drops unknown keys. `load()`/`save()` do an
  atomic-ish write via a temp file + `.bak` rename dance (flash filesystems don't guarantee
  atomic rename, hence the fallback restore-from-`.bak` on failure). Any new config key must be
  added to `DEFAULTS` *and* given validation/coercion logic in `validate()`, or `set`/`save` will
  silently ignore or mis-type it.

- **`ble_console.py`** — a minimal Nordic UART Service (NUS) BLE terminal, framework-free
  (direct `bluetooth` module IRQ handling). It is transport-only: line framing (handles CR, LF,
  CRLF, and terminals with no line ending via a 500 ms idle timeout), MTU-safe notification
  chunking (20 bytes), and deferred connection welcome (sent from `poll()`, not the IRQ handler,
  since BLE IRQ callbacks must stay short and terminals need time to subscribe to notifications).
  It calls back into `main.py` via injected `line_handler`/`connect_handler`/`echo_handler`
  functions — it has no knowledge of what commands mean.

- **`main.py`** — everything else: ADC sampling, the binary ring-buffer history store, NTP sync
  and UK GMT/BST local-time conversion, the BLE command dispatcher (`config_command`), Wi-Fi
  connection management, and a hand-rolled HTTP server (raw sockets, no `uasyncio`/framework)
  that serves an inlined HTML+JS+Chart.js single-page UI, a calibration page (`/calibrate`),
  and a JSON API: `/api/data` (10 s poll), `/api/live` (1 s instant reading + raw ADC),
  `/api/raw` (2000-sample average for calibration), `/api/calibration` (GET/POST),
  `/api/calibration/reset` and `/api/clear` (POST). POST parameters go in the query string,
  because `parse_request` discards headers and never reads a body.

### Cooperative single-threaded main loop

There is one thread. `main()`'s `while True` loop calls `sample_if_due()`, `service_ble()`,
periodic record/NTP/Wi-Fi-retry checks, and `server.accept()` every iteration with short/no
blocking. Any code that can take nontrivially long (writing the ring-buffer file, streaming an
HTTP response, connecting to Wi-Fi) calls `service_sampling()` periodically from inside its own
loop so ADC sampling doesn't stall — see calls to `service_sampling()` scattered through
`ensure_data_file()`, `clear_history()`, `find_latest_record()`, `stream_graph()`,
`stream_table()`, `stream_events()`, `send_all()`, and `connect_wifi()`. When adding any new
loop that does I/O or iterates over many ring-buffer slots, thread `service_sampling()` calls
through it the same way, or sampling cadence degrades.

### Binary ring buffer (`voltage.bin`)

Fixed-size file, no dynamic growth. `MAX_RECORDS = HISTORY_SECONDS // RECORD_SECONDS` records of
12 bytes each (`RECORD_FORMAT = "<IHHHH"`: unix timestamp, avg/min/max millivolts, sample count).
Slot for a given epoch is `(epoch // RECORD_SECONDS) % MAX_RECORDS` — writes wrap around and
overwrite the oldest data once the buffer fills, so there's no unbounded memory or flash growth
over time. This is deliberate: the project avoids ever materializing the full day's history as
Python objects (comment in `main.py` and README's "Development Notes" section explain the RAM
rationale). When touching history read/write code, preserve this: never build an in-memory list
of all records, and always read through `read_slot`/`read_epoch`, which validate against a zeroed
(empty) slot and an epoch/timestamp match respectively.

### ADC calibration (`calibration.data`)

`Vin = CAL_SLOPE × read_u16 + CAL_OFFSET`. The constants live in `calibration.data` (small JSON,
`{"slope":…,"offset":…}`), loaded by `load_calibration()` at the start of `main()`. If the file is
missing or out of range (`valid_calibration()`), `DEFAULT_CAL_SLOPE`/`DEFAULT_CAL_OFFSET` are
used and written out to create the file. `save_calibration()` uses the same temp-file + `.bak`
pattern as `config_manager.save()`. `apply_calibration()` updates the globals at runtime, so a
new calibration takes effect without a reboot (`raw_to_mv()` reads the globals on each call).
The least-squares fit runs in the browser (`CAL_HTML`); the device only captures raw averages and
validates/stores the result. Calibration deliberately stays out of `config.json`/`config_manager`.

### Time handling

MicroPython's `time.gmtime(0)` epoch varies by port (1970 vs. 2000). `port_to_unix()` /
`unix_to_port()` bridge this so the rest of the code always works in true Unix time
(`now_unix()`). UK local time (GMT/BST) is computed manually in `uk_offset_seconds()` (no
`zoneinfo`/`pytz` available in MicroPython) using a hand-rolled last-Sunday-of-month DST rule —
don't replace this with a timezone library call, none is available on-device.

### HTTP server

Hand-rolled over raw `socket`, not a framework. Requests are parsed minimally (`parse_request`
reads only the request line, discards headers). Responses are streamed in small chunks via
`send_all()`/`send_headers()` rather than building full strings in memory, for the same low-RAM
reason as the ring buffer. The web UI lives in two string constants, `HTML` (main page) and
`CAL_HTML` (calibration page), both in `main.py` and both sent by `send_page()`. There are no
separate template or static-asset files. JSON is hand-assembled with
f-string-style formatting and the `json_string()` escaper, not `json.dumps`, to avoid building a
large in-memory structure before sending.

### BLE command surface

`config_command()` in `main.py` is the single dispatcher for all BLE console commands (`help`,
`show`, `get`, `set`, `save`, `status`, `defaults`, `clear-data`, `reboot`, `apply`,
`apply --clear`). `set` mutates the in-memory `CONFIG` dict only (via `validate_config`); `save`
persists it to `config.json`; `apply` saves and reboots so config read at startup takes effect.
Adding a new BLE command means adding a branch here, and updating the `help` text and the
README's BLE command list together.

## Secrets and config.json

`config.json` in this repo contains placeholder Wi-Fi credentials (`WIFI_SID`/`WIFI_PW`). Do not
commit real Wi-Fi credentials to `config.json` — the README recommends keeping real credentials
out of git entirely (see its "Configuration and Secrets" section).
