# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

A 24-hour voltage logger for the Raspberry Pi Pico W, written in MicroPython. It samples an
analogue voltage through a resistor divider, stores compact binary history on the Pico's own
flash filesystem, and exposes both a browser-based monitoring UI (served from the Pico itself)
and a BLE configuration console. It was built for monitoring automotive 12 V circuits, and is
intended to run unattended on battery out of USB reach, so it supports signed over-the-air
updates with automatic rollback.

There is no build system, package manager, or test suite — this is firmware that runs directly
on the Pico's MicroPython interpreter, not a hosted Python application.

## Deploying to hardware

For a first install, copy the project files to the Pico's filesystem (commonly via `mpremote`):

```bash
python3 -m pip install mpremote
mpremote connect /dev/cu.usbmodemXXXX fs cp main.py :main.py
mpremote connect /dev/cu.usbmodemXXXX fs cp app.py :app.py
mpremote connect /dev/cu.usbmodemXXXX fs cp ble_console.py :ble_console.py
mpremote connect /dev/cu.usbmodemXXXX fs cp config_manager.py :config_manager.py
mpremote connect /dev/cu.usbmodemXXXX fs cp config.json :config.json
mpremote connect /dev/cu.usbmodemXXXX reset
```

For USB updates, use `./deploy.py`. It finds the board holding this project's
`.deploy-manifest.json` (SHA-256 per managed file), copies only changed files from
`MANAGED_FILES`, rewrites the manifest, clears over-the-air backups/staging (`CLEAR_OTA_STATE`)
and hard-resets the board. Run `--init --port <port>` once per board; `--dry-run`, `--force` and
`--list` are also available. When adding a new firmware file, add it to `MANAGED_FILES` in
`deploy.py`, and if it should be updatable over the air, to `UPDATABLE_FILES` in `app.py`,
`main.py` and `make_package.py`, and to the file tuple in `deploy.py`'s `CLEAR_OTA_STATE`
(all four must agree).

For over-the-air updates, use `./make_package.py --device http://<ip> --upload` (see
"Over-the-air updates" below).

The device's own `config.json` (real Wi-Fi credentials, `update_key`) and `calibration.data`
(created on first boot, updated from the `/calibrate` web page) are live state. Neither updater
ever overwrites them; if copying by hand, copy only the changed `.py` files.

Any `mpremote` command (even `fs ls`/`fs cat`) interrupts the running logger. Restart it with
`mpremote … reset`. `mpremote … soft-reset` leaves the board in raw REPL, so `main.py` does
**not** run. While an over-the-air update is pending, or on every boot if `watchdog_enabled` is
true in the device's `config.json`, the watchdog is armed and will reset the board about 8 s
after the program is interrupted, which breaks `mpremote`/`deploy.py` sessions. Check
`/api/update/status` (`watchdog` field) before USB work, and turn it off over BLE first.

There is no local way to "run" this project off-device; verifying changes means flashing to a
Pico W (or reasoning carefully about MicroPython semantics, which differ from CPython — see
below). Pure-logic parts (package signing/staging, bootstrap apply/rollback) can be exercised
under CPython with stubbed `machine`/`time` modules.

## Architecture

- **`main.py`** — the bootstrap, deliberately small and updated over USB only (never over the
  air, so recovery code can't be broken remotely). Each boot it finishes an interrupted
  rollback, applies a staged update (`update_ready.json` + `<name>.new` → live, previous kept as
  `<name>.old`), honours a BLE rollback request (`update_rollback.json`), then imports `app` and
  calls `app.main(watchdog=…)`. State lives in `update_state.json` (`applying` → `pending` →
  `good`, or `rolling_back` → `rolled_back`, plus `usb_deployed` after `deploy.py`); state is
  written before files are renamed so every step is safe to repeat after power loss. While
  `pending` it counts boots, and it arms `machine.WDT` while `pending` or on every boot when
  `watchdog_enabled` is true in `config.json` (read directly with `json`, not via
  `config_manager`, and only a real JSON `true` counts). A WDT cannot be stopped once created,
  only by a reset, so "disabling" means not creating it on the next boot. While `pending`, an
  exception from the app, or more than `MAX_PENDING_BOOTS` unconfirmed boots, restores the
  `.old` files. An exception from confirmed
  code is logged to `boot_error.txt` and the board restarts after 10 s (unattended unit), except
  `KeyboardInterrupt`, which drops to the REPL.

- **`app.py`** — the application (formerly `main.py`): ADC sampling, the binary ring-buffer
  history store, NTP sync and UK GMT/BST local-time conversion, the BLE command dispatcher
  (`config_command`), Wi-Fi connection management, the over-the-air update receiver, and a
  hand-rolled HTTP server (raw sockets, no `uasyncio`/framework) that serves an inlined
  HTML+JS+Chart.js single-page UI, a calibration page (`/calibrate`), an update page (`/update`),
  shared assets (`/ui.css`, `/ui.js`), and a JSON API: `/api/data` (10 s poll), `/api/live` (1 s instant reading + raw ADC),
  `/api/raw` (2000-sample average for calibration), `/api/calibration` (GET/POST),
  `/api/calibration/reset`, `/api/clear` (POST), `/api/update/status` (GET) and `/api/update`
  (POST, raw package body). Small POST parameters go in the query string; only `/api/update`
  reads a request body (via the `Content-Length` that `parse_request` returns). The bottom of the
  file has an `if __name__ == "__main__"` guard: `main.py` imports it and calls `main()`.

- **`config_manager.py`** — the single source of truth for configuration shape. `DEFAULTS` is
  the canonical key set; `validate()` clamps/coerces any loaded or set config back into that
  shape (ranges, types, lengths) and silently drops unknown keys. `load()`/`save()` do an
  atomic-ish write via a temp file + `.bak` rename dance (flash filesystems don't guarantee
  atomic rename, hence the fallback restore-from-`.bak` on failure). Any new config key must be
  added to `DEFAULTS` *and* given validation/coercion logic in `validate()`, or `set`/`save` will
  silently ignore or mis-type it (this is why `localecho` was once dropped on every `save`).

- **`ble_console.py`** — a minimal Nordic UART Service (NUS) BLE terminal, framework-free
  (direct `bluetooth` module IRQ handling). It is transport-only: line framing (handles CR, LF,
  CRLF, and terminals with no line ending via a 500 ms idle timeout), MTU-safe notification
  chunking (20 bytes), and deferred connection welcome (sent from `poll()`, not the IRQ handler,
  since BLE IRQ callbacks must stay short and terminals need time to subscribe to notifications).
  It calls back into `app.py` via injected `line_handler`/`connect_handler`/`echo_handler`
  functions — it has no knowledge of what commands mean.

- **`deploy.py`** / **`make_package.py`** — host-side tools (CPython): USB updater and
  over-the-air package builder/uploader. On macOS, Local Network privacy can block Python's
  HTTP to the LAN (errno 65) while the system `curl` is allowed, so `make_package.py` falls
  back to `curl` in that case.

### Cooperative single-threaded main loop

There is one thread. `main()`'s `while True` loop calls `sample_if_due()`, `feed_watchdog()`,
`service_ble()`, `service_led()` (on-board LED: flashing until `clock_valid` and Wi-Fi
connected, then steady; also called from the `connect_wifi()` wait loop),
`check_update_commit()`, periodic record/NTP/Wi-Fi-retry checks, and `server.accept()` every
iteration with short/no blocking. Any code that can take nontrivially long (writing the
ring-buffer file, streaming an HTTP response, connecting to Wi-Fi, receiving an update) calls
`service_sampling()` periodically from inside its own loop. That keeps ADC sampling going *and
feeds the watchdog* — see calls scattered through `ensure_data_file()`, `clear_history()`,
`find_latest_record()`, `stream_graph()`, `stream_table()`, `stream_events()`, `send_all()`,
`connect_wifi()`, `capture_raw()`, `file_sha256()` and the update receiver. When adding any new
loop that does I/O or iterates over many ring-buffer slots, thread `service_sampling()` calls
through it the same way. Otherwise sampling cadence degrades, and whenever the watchdog is armed
(update trial or `watchdog_enabled`) a gap over ~8 s triggers a reset. During a trial, that
reset also counts as a failed boot. Blocking calls without a loop (e.g. DNS in `sync_ntp()`)
need to stay well under 8 s, which is why `ntp_server` should be an IP address when the
watchdog is on.

### Over-the-air updates

`make_package.py` builds `PVLPKG1 <hex HMAC-SHA256>\n` + a JSON header line (`version`,
`created`, `files` with name/size/sha256) + the concatenated file bytes, signed with the key in
`.update-key` (git-ignored), which must equal the device's `update_key` config value (empty =
updates disabled). `app.py` (`receive_update` → `stage_package`) streams the body to
`update.pkg` while computing the HMAC (`HmacSha256`, since MicroPython has no `hmac`; and no
`hashlib.hexdigest()`, hence `hex_digest()`). It rejects bad signatures, files outside
`UPDATABLE_FILES`, packages not newer than `last_created` (replay protection) and uploads while
an update is `pending`. It then writes `<name>.new` files after per-file hash checks, writes
`update_ready.json`, replies and reboots; `main.py` does the rest. `check_update_commit()`
confirms a pending update once the loop has run `UPDATE_COMMIT_MS` with the web server up, then
resets once to release the watchdog (which cannot be stopped once armed). It skips that reset
when `PERMANENT_WATCHDOG` (captured from `CONFIG` at import, matching what `main.py` read at
boot) keeps the watchdog armed anyway. `main.py` keeps
`.deploy-manifest.json` in step with files changed over the air, so `deploy.py` stays accurate.

### Binary ring buffer (`voltage.bin`)

Fixed-size file, no dynamic growth. `MAX_RECORDS = HISTORY_SECONDS // RECORD_SECONDS` records of
12 bytes each (`RECORD_FORMAT = "<IHHHH"`: unix timestamp, avg/min/max millivolts, sample count).
Slot for a given epoch is `(epoch // RECORD_SECONDS) % MAX_RECORDS` — writes wrap around and
overwrite the oldest data once the buffer fills, so there's no unbounded memory or flash growth
over time. This is deliberate: the project avoids ever materializing the full day's history as
Python objects (comment in `app.py` and README's "Development Notes" section explain the RAM
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
reads the request line and skips headers, keeping only `Content-Length`). Responses are streamed
in small chunks via `send_all()`/`send_headers()` rather than building full strings in memory,
for the same low-RAM reason as the ring buffer. The web UI lives in string constants in `app.py`: `HTML`
(dashboard), `CAL_HTML` (calibration) and `UPDATE_HTML` (firmware update), plus `UI_CSS` and
`UI_JS`, which all pages share via `/ui.css` and `/ui.js`. `UI_JS` builds the header, the
slide-out menu (including "Clear stored data") and toasts; pages set
`<body data-page=… data-title=…>` and call `ui.setStatus()`/`ui.toast()`. Colours are CSS custom
properties with a `prefers-color-scheme: dark` set; the dashboard's Chart.js colours are read from
them in `applyChartTheme()` (with fallbacks). `send_page()` streams pages in slices and inserts
`?v=<UI_VERSION>` after the asset URLs as it goes; the assets are sent with a one-day
`max-age`, so later page loads skip them. There are no separate template or static-asset files.
**Don't build large strings at import or per request** (e.g. `str.replace` on a whole page):
after the pages grew to ~32 KB, an 11 KB allocation at import raised `MemoryError` on the
fragmented heap (the update rolled back). Work in slices instead. `server.listen(5)` lets a page
load's parallel requests queue, since connections are served one at a time. Large JSON is
hand-assembled with f-string-style formatting and the `json_string()` escaper, not `json.dumps`,
to avoid building a large in-memory structure before sending (`json.dumps` is used only for small
replies such as update status).

### BLE command surface

`config_command()` in `app.py` is the single dispatcher for all BLE console commands (`help`,
`show`, `get`, `set`, `save`, `status`, `defaults`, `clear-data`, `reboot`, `apply`,
`apply --clear`, `update-status`, `rollback`). `set` mutates the in-memory `CONFIG` dict only
(via `validate_config`); `save` persists it to `config.json`; `apply` saves and reboots so config
read at startup takes effect. BLE is the recovery path when Wi-Fi is unavailable, so keep it
working. Adding a new BLE command means adding a branch here, and updating the `help` text and
the README's BLE command list together. Secrets (`wifi_password`, `update_key`) are masked in
`_config_value_text()`.

## Secrets and config.json

`config.json` in this repo contains placeholder Wi-Fi credentials (`WIFI_SID`/`WIFI_PW`) and an
empty `update_key`. Do not commit real Wi-Fi credentials or the update key — the README
recommends keeping real credentials out of git entirely (see its "Configuration and Secrets"
section). The signing key on the host is `.update-key` (git-ignored, mode 600).
