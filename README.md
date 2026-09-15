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
-   UK GMT/BST local-time handling
-   BLE configuration console using the Nordic UART Service (NUS)
-   BLE startup status/welcome screen
-   Configurable command echo (`localecho`)
-   Configuration stored in `config.json`
-   Wi-Fi configuration can be changed over BLE
-   Stored history can be cleared from the BLE console

## Files

  -----------------------------------------------------------------------
  File                                Purpose
  ----------------------------------- -----------------------------------
  `main.py`                           Main logger, sampling, binary
                                      history, web server, event
                                      detection and BLE command handling

  `ble_console.py`                    Nordic UART Service BLE terminal
                                      implementation

  `config_manager.py`                 Configuration defaults, loading,
                                      validation and saving

  `config.json`                       User configuration
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

> **Automotive use:** A resistor divider alone is not sufficient
> protection against automotive electrical transients. Add suitable
> input protection such as transient suppression/clamping and filtering
> before using the logger permanently in a vehicle. Never apply vehicle
> battery voltage directly to a Pico ADC pin.

## ADC Calibration

The supplied firmware uses the following calibration:

``` python
CAL_SLOPE = 0.00050893
CAL_OFFSET = -0.13633
```

with:

``` text
Vin = CAL_SLOPE × ADC_read_u16 + CAL_OFFSET
```

These values were derived from measurements on the development hardware.
Component tolerances and ADC characteristics mean that you should
calibrate your own unit if accurate absolute voltage measurements are
important.

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
mpremote connect /dev/cu.usbmodem1451201 fs cp ble_console.py :ble_console.py
mpremote connect /dev/cu.usbmodem1451201 fs cp config_manager.py :config_manager.py
mpremote connect /dev/cu.usbmodem1451201 fs cp config.json :config.json
mpremote connect /dev/cu.usbmodem1451201 reset
```

Change the serial device name as appropriate for your system.

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

The web interface provides live voltage information, a table of recorded
samples, historical graph navigation, event information, and controls
for clearing stored history.

The graph supports multiple time windows and navigation through the
locally stored history.

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

A useful `.gitignore` entry is:

``` gitignore
config.json
voltage.bin
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
See the [LICENSE](LICENSE) file for details.
