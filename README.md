# Pico W Voltage Logger

A lightweight 24-hour voltage logger for the **Raspberry Pi Pico W**,
written in MicroPython.

The logger samples an analogue voltage through a resistor divider,
stores compact history locally on the Pico, and provides both a
browser-based monitoring interface and a Bluetooth Low Energy (BLE)
configuration console.

It was originally developed for monitoring automotive 12 V circuits, but
it can be used for other low-frequency voltage monitoring applications
when an appropriate input divider and protection circuit are used.

## Features

-   Raspberry Pi Pico W / MicroPython
-   ADC voltage sampling on GP26 / ADC0
-   Calibrated ADC-to-voltage conversion
-   Fixed-size binary ring buffer for approximately 24 hours of history
-   Low RAM usage: history is not retained as thousands of Python
    objects
-   Browser-based live voltage display
-   Historical graph with selectable time windows
-   Event detection for low-voltage and sudden voltage-drop conditions
-   NTP time synchronisation
-   On-board LED status: flashing until Wi-Fi is connected and the
    clock is set by NTP, then steady
-   UK GMT/BST local-time handling
-   BLE configuration console using the Nordic UART Service (NUS)
-   BLE startup status/welcome screen
-   Configurable command echo (`localecho`)
-   Configuration stored in `config.json`
-   Wi-Fi configuration can be changed over BLE
-   Stored history can be cleared from the BLE console
-   Signed over-the-air updates over Wi-Fi with automatic rollback
-   Optional permanent hardware watchdog for unattended installations

## Files

  -----------------------------------------------------------------------
  File                                Purpose
  ----------------------------------- -----------------------------------
  `main.py`                           Bootstrap: applies staged
                                      over-the-air updates, runs the app
                                      and rolls back failed updates.
                                      Updated over USB only

  `app.py`                            Main logger, sampling, binary
                                      history, web server, event
                                      detection, BLE command handling
                                      and over-the-air update receiver

  `ble_console.py`                    Nordic UART Service BLE terminal
                                      implementation

  `config_manager.py`                 Configuration defaults, loading,
                                      validation and saving

  `config.json`                       User configuration

  `deploy.py`                         USB updater: copies changed files
                                      to the Pico and restarts it

  `make_package.py`                   Builds signed over-the-air update
                                      packages

  `calibration.data`                  ADC calibration (slope/offset).
                                      Created on the device at first
                                      boot; updated from the web
                                      calibration page
  -----------------------------------------------------------------------

## Hardware

The default input is:

-   **GP26 / ADC0** --- Pico physical pin 31
-   **GND** --- Pico physical pin 33

The Pico ADC must **not** be connected directly to a 12 V source.

The development circuit uses a resistor divider:

``` text
Measured voltage +
       |
      38 kΩ
       |
       +---------- GP26 / ADC0
       |
      4.3 kΩ
       |
Measured voltage - ----- Pico GND
```

An optional 100 nF capacitor can be connected from the ADC node to
ground for additional filtering.

The divider ratio is approximately:

``` text
4.3 / (38 + 4.3) = 0.10165
```

so a 14 V input produces approximately 1.42 V at the ADC.

### Input protection

The development board adds a small amount of protection around the
divider:

-   a series resistor between the divider node and GP26, to limit any
    clamp current into the pin
-   a 3.3 V zener from GP26 to GND (cathode to GP26). It clamps positive
    excursions at about 3.3 V and, conducting forward, negative ones at
    about −0.6 V
-   a reverse-polarity diode across the input (cathode to the input,
    anode to GND)

> **Do not put a zener between GP26 and 3V3.** A diode in that position
> (anode to GP26, cathode to 3V3) only clamps *positive* overvoltage.
> If it is a 3.3 V zener, it sits reverse-biased right at its breakdown
> voltage whenever the input is low, and leaks current from 3V3 into the
> ADC node. On the development board this read about 1.1 V with the
> input at 0 V and squashed the calibration slope to about a third. If a
> clamp to 3V3 is wanted, use a Schottky diode (e.g. BAT85/BAT54). For a
> tighter negative clamp, add a Schottky from GND to GP26 (anode to GND).

With the input at 0 V, the raw ADC reading should be in the low
hundreds. A much higher value points to leakage into the ADC node.

> **Automotive use:** A resistor divider alone is not sufficient
> protection against automotive electrical transients. Add suitable
> input protection such as transient suppression/clamping and filtering
> before using the logger permanently in a vehicle. Never apply vehicle
> battery voltage directly to a Pico ADC pin.

## ADC Calibration

Voltage is calculated as:

``` text
Vin = slope × ADC_read_u16 + offset
```

The slope and offset are stored on the Pico in `calibration.data`, a
small JSON file:

``` json
{"slope":0.00050893,"offset":-0.13633}
```

If the file is missing or invalid, the firmware falls back to the
factory default above (derived from measurements on the development
hardware) and writes it out to create the file.

Component tolerances and ADC characteristics mean that you should
calibrate your own unit if accurate absolute voltage measurements are
important.

### Calibrating from the web interface

Open **Calibrate ADC** from the main page (or browse to `/calibrate`).

1.  Connect a variable bench supply to the logger input.
2.  For each reference point (3, 6, 9 and 12 V by default; more can be
    added), set the supply to roughly that voltage and measure it with a
    multimeter.
3.  Type the multimeter reading into **Actual V** and press
    **Capture**. The Pico averages 2000 raw ADC reads and shows the
    result along with its min–max spread.
4.  Once at least two points are captured, press **Calculate**. A
    least-squares straight-line fit gives the new slope and offset, and
    the per-point error is shown.
5.  Press **Save new calibration**. It takes effect immediately (no
    reboot) and is written to `calibration.data`.

**Restore factory default** puts back the built-in values. The
calibration page never touches `config.json`.

Recorded history is stored in calibrated millivolts, so a new
calibration applies only to readings taken after it is saved.

## Installing MicroPython

Install a current MicroPython build for the Raspberry Pi Pico W, then
copy the project files to the Pico filesystem.

One convenient method is `mpremote`:

``` bash
python3 -m pip install mpremote
```

For example, if the Pico appears on macOS as `/dev/cu.usbmodem1451201`:

``` bash
mpremote connect /dev/cu.usbmodem1451201 fs cp main.py :main.py
mpremote connect /dev/cu.usbmodem1451201 fs cp app.py :app.py
mpremote connect /dev/cu.usbmodem1451201 fs cp ble_console.py :ble_console.py
mpremote connect /dev/cu.usbmodem1451201 fs cp config_manager.py :config_manager.py
mpremote connect /dev/cu.usbmodem1451201 fs cp config.json :config.json
mpremote connect /dev/cu.usbmodem1451201 reset
```

Change the serial device name as appropriate for your system.

### Updating the firmware

Once the unit is configured, its `config.json` holds your real settings
and `calibration.data` holds its calibration. Use `deploy.py` to update
the code without overwriting either:

``` bash
./deploy.py --init --port /dev/cu.usbmodem1451201   # once per board
./deploy.py                                         # every update after that
```

The device stores a manifest (`.deploy-manifest.json`) with the SHA-256
of each deployed file (`main.py`, `app.py`, `ble_console.py`,
`config_manager.py`).
`deploy.py` finds the board that holds this project's manifest, copies
only the files whose hashes have changed, writes the new manifest and
restarts the board. It never overwrites `config.json` or
`calibration.data`. With `--init`, `config.json` is copied only if the
board has none.

Other options: `--dry-run` (report changes only), `--force` (copy every
file), `--list` (show connected boards without touching them) and
`--port` (skip the search). The logger's USB serial number is cached
in `.deploy-device`, which is git-ignored, so other MicroPython boards
are not interrupted on later runs.

Any `mpremote` command, even reading a file, stops the running logger,
so `deploy.py` always restarts the board, even when nothing changed. If
you use `mpremote` by hand, finish with `reset`. `mpremote soft-reset`
is not enough: it leaves the board at the raw REPL and `main.py` does
not start.

A USB deploy also removes any over-the-air backups (`*.old`) and staged
files, so a later BLE `rollback` cannot restore code older than what
was just deployed.

## Over-the-Air Updates

For a logger installed out of reach of USB, `app.py`,
`ble_console.py` and `config_manager.py` can be updated over Wi-Fi.
`main.py`, the bootstrap that applies updates and recovers from bad
ones, is deliberately never updated over the air, so a broken remote
update can always be undone.

### One-time setup (over USB, before installing the logger)

1.  Deploy the current firmware with `./deploy.py`. This installs the
    bootstrap.
2.  Create a signing key and install it on the logger:

    ``` bash
    ./make_package.py --gen-key
    ```

    This writes `.update-key` (git-ignored; keep it private) and prints
    the BLE commands to run, `set update_key <key>` then `save`. Updates
    are refused until a key is set.

3.  Test an update and a rollback while USB is still connected (see
    below).

### Sending an update

``` bash
./make_package.py --device http://192.168.0.102 --upload
```

This asks the logger which files it runs, packages only the ones that
differ, signs the package, uploads it and waits for the result. You can
also build a package with `./make_package.py` (written to `dist/`) and
upload the `.pvlpkg` file from the **Firmware update** page linked from
the main page.

On macOS, the Local Network privacy setting can stop Python reaching
devices on your LAN ("No route to host"). `make_package.py` then falls
back to the system `curl`. You can also allow Python under **System
Settings › Privacy & Security › Local Network**.

### What the logger does

1.  **Verify and stage.** The upload is streamed to flash and its
    HMAC-SHA256 signature checked against `update_key`. The package must
    be newer than the last one applied, which blocks replays of old
    packages. Each file's SHA-256 is checked, then the files are staged
    as `<name>.new` and the logger reboots. Nothing running is
    overwritten.
2.  **Apply.** At boot, `main.py` moves the staged files into place and
    keeps the replaced versions as `<name>.old`. Every step survives a
    power cut.
3.  **Trial.** The update is *pending*. A hardware watchdog is armed, so
    a hang causes a reset.
4.  **Confirm or roll back.**
    -   The update is confirmed once the new code has run for 60 seconds
        with the web server up, so a fix can still be uploaded. The
        logger then restarts once to release the watchdog, unless the
        [permanent watchdog](#permanent-watchdog-optional) is on, in
        which case it stays armed.
    -   If the new code raises an exception, or fails to confirm within
        3 boots (for example, it keeps hanging), `main.py` restores the
        `.old` files and reboots into the previous version.

While an update is pending, a new upload is refused. The last update can
also be undone by hand with the BLE `rollback` command, and
`update-status` shows the current state. The cause of the last crash is
saved on the device in `boot_error.txt`.

### Permanent watchdog (optional)

By default the hardware watchdog only runs during an update trial. For
an unattended installation you can arm it on every boot, so a hang in
confirmed code also resets the logger instead of leaving it stuck until
the battery is disconnected:

``` text
set watchdog_enabled true
apply
```

The watchdog can't be switched off while it is running; only a reset
stops it. To disable it, run `set watchdog_enabled false`, then `apply`.
It is then not armed on the next boot.

Leave it **off on the bench**. While it is on, anything that stops the
logger's program resets the board about 8 seconds later. That includes
`mpremote` and `deploy.py` (which interrupt the program to use the USB
REPL) and pressing Ctrl-C at the REPL. If you need USB access to a unit
with the watchdog on, switch it off over BLE first. `update-status`, and
the Firmware update page, show whether the watchdog is armed.

With the watchdog on, use an IP address for `ntp_server` rather than a
hostname. A DNS lookup that times out can block for longer than the
watchdog allows.

Security notes: the key never crosses the network, but the web server
uses plain HTTP and the signature only proves who built the package. Use
a long random key (as `--gen-key` does) and keep the logger on a network
you trust.

## Initial Configuration

Edit `config.json` before copying it to the Pico, or configure the unit
later using BLE.

Important settings include:

``` json
{
  "wifi_ssid": "your-network",
  "wifi_password": "your-password",
  "ntp_server": "192.168.0.16",
  "ble_enabled": true,
  "localecho": true
}
```

Two further settings are normally set later over BLE: `update_key`, the
signing key for [over-the-air updates](#over-the-air-updates) (empty
disables updates), and `watchdog_enabled`, the
[permanent watchdog](#permanent-watchdog-optional) (default `false`).

The project contains additional sampling, history, web-server and
event-detection settings. The BLE `show` command displays the complete
active configuration.

Do not commit a real Wi-Fi password to a public Git repository. Replace
credentials in the repository copy of `config.json` with placeholders
before publishing.

## BLE Configuration Console

The Pico advertises by default as:

``` text
Pico-Voltage-Logger
```

The console implements the standard **Nordic UART Service (NUS)**:

``` text
Service: 6E400001-B5A3-F393-E0A9-E50E24DCCA9E
RX:      6E400002-B5A3-F393-E0A9-E50E24DCCA9E
TX:      6E400003-B5A3-F393-E0A9-E50E24DCCA9E
```

This means it can be used with BLE terminal applications that support
Nordic UART.

After connection the Pico waits briefly for the terminal to subscribe to
notifications and then displays a welcome/status screen containing the
Wi-Fi SSID, current IP status, NTP server, clock state and important
event settings.

The console accepts CR, LF, CRLF and terminals that send a BLE write
without a line-ending character.

### BLE Commands

``` text
help
show
status
get <key>
set <key> <value>
save
defaults
clear-data
reboot
apply
apply --clear
update-status
rollback
```

Examples:

``` text
status
show
get ntp_server
set ntp_server 192.168.0.16
set wifi_ssid MyNetwork
set wifi_password MyPassword
set localecho false
save
reboot
```

`set` changes the in-memory configuration. `save` persists it. `apply`
saves the configuration and reboots the Pico so settings that are read
during startup become active.

`apply --clear` additionally clears the stored voltage history.

`update-status` shows the over-the-air update state, and `rollback`
restores the version replaced by the last over-the-air update and
reboots (see [Over-the-Air Updates](#over-the-air-updates)). The signing
key is set with `set update_key <key>`; `show` masks it.

### Command Echo

`localecho` controls whether the Pico echoes a received command before
displaying its response:

``` text
set localecho true
set localecho false
```

It is enabled by default. This makes BLE terminal interaction look more
like a conventional serial console.

## Web Interface

Once Wi-Fi is connected, open the Pico's IP address in a web browser.

The dashboard shows:

-   **Live voltage**: an instantaneous reading, refreshed every second
    from the lightweight `/api/live` endpoint. It is independent of the
    10-second logging interval and does not affect recorded data.
-   **Average, minimum, maximum and sample count** for the latest
    10-second record.
-   **Voltage history**: the average as a line inside a shaded
    minimum-to-maximum band, with the low-voltage event threshold as a
    dashed line. Choose a window from 10 minutes to 24 hours and step
    earlier or later through the stored history. Clicking the chart
    centres the measurements table on that time.
-   **Detected events** and the **measurements** table.

The header shows the logger's status (logging, waiting for NTP, or
connection lost) and the current time. The menu button at the top left
opens a side menu with **Dashboard**, **Calibrate ADC** (see
[Calibrating from the web interface](#calibrating-from-the-web-interface)),
**Firmware update** (see [Over-the-Air Updates](#over-the-air-updates)),
**Settings** (see below) and **Clear stored data**, which asks for
confirmation before erasing the history.

The pages follow the light or dark setting of your phone or computer
and adapt to phone screens.

### Settings page

**Settings** edits the configuration in `config.json`, grouped as
Network, Time, Logging, Events, Display and Device. Changed fields are
highlighted, and you can **Save** (applied at the next restart), **Save
and restart**, or **Restart device** without changes. Out-of-range values
are adjusted to the nearest valid value, and the page tells you which.

Some changes warn you and ask for confirmation first:

-   Changing the **record interval** or **history length** erases stored
    history at restart, because the history file is resized. History is
    capped at 20,000 records (240 KB of flash).
-   Changing the **web server port** moves the page to the new port.
-   Turning **BLE off** removes the fallback if Wi-Fi fails.
-   Turning on the **permanent watchdog** affects USB sessions (see
    [Permanent watchdog](#permanent-watchdog-optional)).

The web server has no login, so two settings are protected:
-   The **Wi-Fi password** is write-only. It is never shown, and leaving
    the field blank keeps the current one.
-   **`update_key`** is not available on the web page at all, because
    it authorises firmware updates. Set it over BLE.

The chart uses Chart.js, which loads from the internet. Without internet
access the dashboard says so, and the readings, events and measurements
still work.

## Data Storage

Voltage history is stored in:

``` text
voltage.bin
```

rather than CSV.

The logger uses a fixed-size binary ring buffer. Each record is 12 bytes
and contains:

``` text
Unix timestamp
average voltage (mV)
minimum voltage (mV)
maximum voltage (mV)
sample count
```

This design keeps RAM usage bounded while retaining approximately 24
hours of measurements. Minimum voltage is stored separately from the
average so short voltage dips are less likely to disappear through
averaging.

## Status LED

The Pico W's on-board LED shows the network state:

-   **Flashing** (about twice a second): connecting to Wi-Fi, waiting
    for the first NTP time sync, or the connection has dropped and the
    logger is retrying
-   **Steady on**: Wi-Fi connected and the clock set by NTP, so
    readings are being stored with valid timestamps

The LED is updated from the main loop, so it may pause briefly during a
long operation such as an NTP request.

## Time Synchronisation

The logger obtains time using the configured NTP server. The default
project configuration uses a local NTP server.

Local display time includes UK GMT/BST handling.

Change the NTP server with:

``` text
set ntp_server 192.168.0.16
apply
```

or edit `config.json`.

## Configuration and Secrets

For a public GitHub repository, it is strongly recommended that the
committed `config.json` contain only example values, for example:

``` json
{
  "wifi_ssid": "YOUR_WIFI_SSID",
  "wifi_password": "YOUR_WIFI_PASSWORD",
  "ntp_server": "pool.ntp.org"
}
```

Alternatively, keep the real `config.json` out of Git and provide a
`config.example.json` in the repository.

The over-the-air signing key is a second secret. It lives on the logger
as `update_key` in `config.json`, and on your computer in `.update-key`.
The repository's `config.json` keeps `update_key` empty, and
`.update-key` is git-ignored. Anyone with the key can install code on
the logger, so never commit it or share it. If it leaks, create a new
one with `make_package.py --gen-key` (after deleting `.update-key`) and
install it over BLE.

A useful `.gitignore` entry is:

``` gitignore
config.json
calibration.data
voltage.bin
.update-key
```

## Development Notes

The project deliberately avoids retaining a full day's history as Python
tuples or dictionaries. On a microcontroller this can exhaust heap
memory surprisingly quickly.

The binary ring-buffer design provides predictable storage usage and
allows HTTP responses to be generated without constructing a large
in-memory JSON representation of the entire history.

The sampling records also contain a sample count, which is useful for
identifying periods where web or BLE activity may have reduced the
number of ADC samples collected during an interval.

## Safety

This software is provided for experimentation and monitoring.

When measuring vehicle electrical systems:

-   Do not connect 12 V directly to the Pico.
-   Use a correctly designed voltage divider.
-   Ensure the ADC input remains safely below 3.3 V under all expected
    conditions.
-   Use appropriate automotive transient protection for permanent
    installations.
-   Ensure the Pico and measured circuit have an appropriate common
    reference/ground.
-   Fuse any connection to a vehicle electrical circuit appropriately.

## License

This project is licensed under the MIT License.

Copyright © 2026 Satish Kunapuli.
See the [LICENCE](LICENCE) file for details.
