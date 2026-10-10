#!/usr/bin/env python3
"""Deploy changed firmware files to the Pico voltage logger.

The device keeps a manifest (MANIFEST_NAME) recording the SHA-256 of
every file this script deployed. On each run the script:

  1. checks that mpremote is available
  2. finds the logger: the connected board whose manifest belongs to
     this project (the board's USB serial is cached locally so other
     MicroPython boards are not interrupted on later runs)
  3. compares local file hashes with the manifest
  4. copies only the changed files, writes the new manifest and
     restarts the board

Any mpremote command (even reading the manifest) stops the running
program, so the board is always hard-reset at the end, whether or not
anything changed.

config.json and calibration.data are live device state and are never
overwritten. On --init, config.json is copied only if the device has none.

Usage:
  tools/deploy.py              deploy changed files
  tools/deploy.py --dry-run    show what would change (board still restarts)
  tools/deploy.py --force      copy every managed file
  tools/deploy.py --list       list connected boards (touches no board)
  tools/deploy.py --init --port /dev/cu.usbmodemXXXX
                               first deployment to a board with no manifest
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

PROJECT = "pi_pico_voltage_logger"
MANIFEST_NAME = ".deploy-manifest.json"
DEVICE_CACHE = ".deploy-device"     # local, untracked: last known board serial
MANAGED_FILES = [
    "main.py", "app.py", "ble_console.py", "config_manager.py",
    "index.html", "calibrate.html", "update.html", "settings.html",
    "ui.css", "ui.js",
]
INIT_ONLY_IF_MISSING = ["config.json"]

# Run on the device after a USB deploy: drop over-the-air backups and any
# staged update, so a later BLE "rollback" cannot restore files older than
# what was just deployed. The replay-protection timestamp is kept.
CLEAR_OTA_STATE = """
import os, json
for n in ('app.py', 'ble_console.py', 'config_manager.py', 'index.html',
          'calibrate.html', 'update.html', 'settings.html', 'ui.css', 'ui.js'):
    for s in ('.old', '.new'):
        try: os.remove(n + s)
        except OSError: pass
for n in ('update_ready.json', 'update_rollback.json', 'update.pkg'):
    try: os.remove(n)
    except OSError: pass
try:
    with open('update_state.json') as f: last = json.load(f).get('last_created', 0)
except (OSError, ValueError): last = 0
with open('update_state.json', 'w') as f:
    json.dump({'state': 'usb_deployed', 'last_created': last}, f)
"""

# Repository root: the firmware files, key and caches live there; this
# script lives in tools/.
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def fail(msg):
    print("error:", msg, file=sys.stderr)
    sys.exit(1)


def find_mpremote():
    exe = shutil.which("mpremote")
    if exe:
        return [exe]
    # Fall back to the module, in case only the package is installed.
    try:
        subprocess.run([sys.executable, "-m", "mpremote", "--version"],
                       check=True, capture_output=True, timeout=20)
        return [sys.executable, "-m", "mpremote"]
    except (subprocess.SubprocessError, OSError):
        fail("mpremote not found. Install it with: python3 -m pip install mpremote")


def mp(mpremote, args, check=True, timeout=90):
    result = subprocess.run(mpremote + args, capture_output=True,
                            text=True, timeout=timeout)
    if check and result.returncode != 0:
        fail("mpremote {} failed:\n{}".format(
            " ".join(args), (result.stderr or result.stdout).strip()))
    return result


def list_boards(mpremote):
    # Each line: port serial vid:pid manufacturer product
    boards = []
    for line in mp(mpremote, ["connect", "list"]).stdout.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[2] != "0000:0000":
            boards.append({"port": parts[0], "serial": parts[1]})
    return boards


def read_manifest(mpremote, port):
    r = mp(mpremote, ["connect", port, "fs", "cat", ":" + MANIFEST_NAME],
           check=False)
    if r.returncode != 0:
        return None
    try:
        manifest = json.loads(r.stdout)
    except ValueError:
        return None
    if manifest.get("project") != PROJECT:
        return None
    return manifest


def reset(mpremote, port):
    mp(mpremote, ["connect", port, "reset"], check=False)


def load_cache():
    try:
        with open(os.path.join(REPO, DEVICE_CACHE)) as f:
            return json.load(f).get("serial")
    except (OSError, ValueError):
        return None


def save_cache(serial):
    with open(os.path.join(REPO, DEVICE_CACHE), "w") as f:
        json.dump({"serial": serial}, f)
        f.write("\n")


def find_device(mpremote):
    boards = list_boards(mpremote)
    if not boards:
        fail("no MicroPython board connected")

    cached = load_cache()
    for b in boards:
        if b["serial"] == cached:
            manifest = read_manifest(mpremote, b["port"])
            if manifest is not None:
                return b["port"], manifest
            print("cached board {} has no {} manifest; probing all boards"
                  .format(b["port"], PROJECT))
            break

    # Probe every board. Reading stops whatever a board is running, so
    # restart the ones that turn out not to be the logger.
    found = None
    for b in boards:
        manifest = read_manifest(mpremote, b["port"])
        if manifest is not None and found is None:
            found = (b, manifest)
        else:
            reset(mpremote, b["port"])
    if found is None:
        fail("no board has a {} manifest. For a first deployment run:\n"
             "  tools/deploy.py --init --port <port>".format(PROJECT))
    save_cache(found[0]["serial"])
    return found[0]["port"], found[1]


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def local_hashes():
    hashes = {}
    for name in MANAGED_FILES:
        path = os.path.join(REPO, name)
        if not os.path.isfile(path):
            fail("managed file missing locally: " + name)
        hashes[name] = sha256(path)
    return hashes


def device_has_file(mpremote, port, name):
    r = mp(mpremote, ["connect", port, "fs", "ls", ":"])
    return any(line.split()[-1] == name
               for line in r.stdout.splitlines()[1:] if line.split())


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--port", help="use this serial port instead of searching")
    p.add_argument("--init", action="store_true",
                   help="first deployment to a board without a manifest")
    p.add_argument("--force", action="store_true",
                   help="copy every managed file, changed or not")
    p.add_argument("--dry-run", action="store_true",
                   help="report changes without copying (board still restarts)")
    p.add_argument("--list", action="store_true",
                   help="list connected boards without touching them")
    args = p.parse_args()

    mpremote = find_mpremote()

    if args.list:
        cached = load_cache()
        for b in list_boards(mpremote):
            print(b["port"], b["serial"],
                  "(cached logger)" if b["serial"] == cached else "")
        return

    if args.init:
        if not args.port:
            fail("--init needs --port, so no other board is touched")
        port, manifest = args.port, {"files": {}}
    elif args.port:
        port, manifest = args.port, read_manifest(mpremote, args.port)
        if manifest is None:
            reset(mpremote, port)
            fail("{} has no {} manifest (use --init for a first deployment)"
                 .format(port, PROJECT))
    else:
        port, manifest = find_device(mpremote)

    if args.port:
        # Remember this board for searches on later runs.
        for b in list_boards(mpremote):
            if b["port"] == port:
                save_cache(b["serial"])

    print("logger on", port)
    current = local_hashes()
    deployed = manifest.get("files", {})
    changed = [n for n in MANAGED_FILES
               if args.force or deployed.get(n) != current[n]]

    if not changed:
        print("all files up to date")
    for name in changed:
        print("  changed:" if deployed.get(name) else "  new:    ", name)

    if args.dry_run or not changed:
        print("restarting logger (reading the device stopped it)")
        reset(mpremote, port)
        return

    cmd = ["connect", port]
    for name in changed:
        cmd += ["fs", "cp", os.path.join(REPO, name), ":" + name, "+"]
    if args.init:
        for name in INIT_ONLY_IF_MISSING:
            if not device_has_file(mpremote, port, name):
                print("  init:    ", name, "(device had none)")
                cmd += ["fs", "cp", os.path.join(REPO, name), ":" + name, "+"]

    # Record only what has been copied: the manifest goes last, so a
    # failed copy leaves the old hashes and the file is retried next run.
    new_manifest = {
        "project": PROJECT,
        "deployed": time.strftime("%Y-%m-%d %H:%M:%S"),
        "files": current,
    }
    with tempfile.NamedTemporaryFile("w", suffix=".json",
                                     delete=False) as tmp:
        json.dump(new_manifest, tmp, indent=1)
    try:
        cmd += ["fs", "cp", tmp.name, ":" + MANIFEST_NAME, "+",
                "exec", CLEAR_OTA_STATE, "+", "reset"]
        mp(mpremote, cmd, timeout=180)
    finally:
        os.unlink(tmp.name)

    print("deployed {} file(s); logger restarting".format(len(changed)))


if __name__ == "__main__":
    main()
