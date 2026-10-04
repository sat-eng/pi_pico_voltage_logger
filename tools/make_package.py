#!/usr/bin/env python3
"""Build a signed over-the-air update package for the Pico voltage logger.

The package carries app.py, ble_console.py and/or config_manager.py
(main.py, the bootstrap, can only be updated over USB with deploy.py).
It is signed with HMAC-SHA256 using the key in .update-key, which must
match the device's update_key setting.

Usage:
  tools/make_package.py --gen-key
        create .update-key and print the BLE commands to install it

  tools/make_package.py
        package all updatable files into dist/

  tools/make_package.py --device http://192.168.0.102
        package only the files that differ from what the device runs

  tools/make_package.py --device http://192.168.0.102 --upload
        ...and upload it, then wait for the device to confirm or roll back

The resulting .pvlpkg file can also be uploaded from the device's
"Firmware update" web page.
"""

import argparse
import hashlib
import hmac
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

# Repository root: the firmware files, key and caches live there; this
# script lives in tools/.
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UPDATABLE_FILES = ["app.py", "ble_console.py", "config_manager.py"]
MAGIC = b"PVLPKG1"
KEY_FILE = os.path.join(REPO, ".update-key")
MAX_PACKAGE_BYTES = 256 * 1024     # must match app.py


def fail(msg):
    print("error:", msg, file=sys.stderr)
    sys.exit(1)


def read_key(path):
    try:
        with open(path) as f:
            key = f.read().strip()
    except OSError:
        fail("no signing key at {} (create one with --gen-key)".format(path))
    if not key:
        fail("signing key file {} is empty".format(path))
    return key


def gen_key(path):
    if os.path.exists(path):
        fail("{} already exists; delete it first to replace the key".format(path))
    key = secrets.token_hex(32)
    with open(path, "w") as f:
        f.write(key + "\n")
    os.chmod(path, 0o600)
    print("Wrote", path, "(git-ignored; keep it private)")
    print("Install it on the logger from a BLE terminal:")
    print("  set update_key", key)
    print("  save")


def git_version():
    try:
        rev = subprocess.run(["git", "-C", REPO, "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "-C", REPO, "status", "--porcelain",
                                "--"] + UPDATABLE_FILES,
                               capture_output=True, text=True).stdout.strip()
        return rev + ("-dirty" if dirty else "")
    except (OSError, subprocess.CalledProcessError):
        return time.strftime("%Y%m%d-%H%M%S")


def curl_json(url, data, timeout):
    # macOS Local Network privacy can block Python from reaching LAN
    # devices (errno 65, EHOSTUNREACH) while the system curl is allowed.
    cmd = ["curl", "-sS", "--max-time", str(timeout), url]
    if data:
        cmd += ["-H", "Expect:", "-H", "Content-Type: application/octet-stream",
                "--data-binary", "@-"]
    r = subprocess.run(cmd, input=data, capture_output=True)
    if r.returncode != 0:
        raise OSError(r.stderr.decode(errors="replace").strip())
    try:
        return json.loads(r.stdout)
    except ValueError:
        fail("unexpected reply from {}".format(url))


def http_json(url, data=None, timeout=30):
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    if data:
        req.add_header("Content-Type", "application/octet-stream")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except ValueError:
            fail("HTTP {} from {}".format(e.code, url))
    except urllib.error.URLError as e:
        if getattr(e.reason, "errno", None) == 65 and shutil.which("curl"):
            return curl_json(url, data, timeout)
        raise


def device_status(device):
    try:
        return http_json(device.rstrip("/") + "/api/update/status")
    except (urllib.error.URLError, OSError) as e:
        fail("cannot reach {}: {}".format(device, e))


def build(files, src, version, key):
    entries, blobs = [], []
    for name in files:
        with open(os.path.join(src, name), "rb") as f:
            data = f.read()
        entries.append({"name": name, "size": len(data),
                        "sha256": hashlib.sha256(data).hexdigest()})
        blobs.append(data)
    header = {"version": version, "created": int(time.time()), "files": entries}
    body = json.dumps(header, separators=(",", ":")).encode() + b"\n" + b"".join(blobs)
    sig = hmac.new(key.encode(), body, hashlib.sha256).hexdigest().encode()
    return MAGIC + b" " + sig + b"\n" + body


def wait_for_outcome(device, timeout=300):
    print("waiting for the logger to reboot and confirm (about a minute)...")
    start = time.time()
    seen_pending = False
    while time.time() - start < timeout:
        time.sleep(5)
        try:
            st = http_json(device.rstrip("/") + "/api/update/status", timeout=5)
        except (urllib.error.URLError, OSError):
            continue          # rebooting
        state = st.get("state")
        if state == "pending":
            if not seen_pending:
                print("  running new version, not yet confirmed")
            seen_pending = True
        elif state == "good" and seen_pending:
            print("confirmed:", st.get("version"))
            return True
        elif state == "rolled_back":
            print("ROLLED BACK:", st.get("reason"))
            return False
    print("no confirmation after {} s; check update-status over BLE".format(timeout))
    return False


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("files", nargs="*", help="files to include (default: see --device)")
    p.add_argument("--device", help="logger URL, e.g. http://192.168.0.102; "
                   "package only files that differ from the device")
    p.add_argument("--upload", action="store_true", help="upload to --device and wait")
    p.add_argument("--src", default=REPO, help="directory to take files from")
    p.add_argument("--out", help="output file (default dist/update-<version>.pvlpkg)")
    p.add_argument("--version", help="version label (default: git revision)")
    p.add_argument("--key-file", default=KEY_FILE)
    p.add_argument("--gen-key", action="store_true", help="create a new signing key")
    args = p.parse_args()

    if args.gen_key:
        gen_key(args.key_file)
        return
    if args.upload and not args.device:
        fail("--upload needs --device")

    files = args.files or list(UPDATABLE_FILES)
    for name in files:
        if name == "main.py":
            fail("main.py is the bootstrap and can only be updated over USB (deploy.py)")
        if name not in UPDATABLE_FILES:
            fail("not an updatable file: " + name)
        if not os.path.isfile(os.path.join(args.src, name)):
            fail("missing: " + os.path.join(args.src, name))

    key = read_key(args.key_file)

    if args.device:
        st = device_status(args.device)
        if not st.get("enabled"):
            fail("updates are disabled on the device: set update_key over BLE")
        if st.get("state") == "pending":
            fail("the device's previous update is not confirmed yet")
        if not args.files:
            remote = st.get("files", {})
            files = [n for n in files if remote.get(n) != hashlib.sha256(
                open(os.path.join(args.src, n), "rb").read()).hexdigest()]
            if not files:
                print("device already runs these files; nothing to package")
                return

    version = args.version or git_version()
    package = build(files, args.src, version, key)
    if len(package) > MAX_PACKAGE_BYTES:
        fail("package is {} bytes; the device accepts at most {}".format(
            len(package), MAX_PACKAGE_BYTES))

    out = args.out or os.path.join(REPO, "dist", "update-{}.pvlpkg".format(version))
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "wb") as f:
        f.write(package)
    print("built {} ({} bytes): {}".format(out, len(package), ", ".join(files)))

    if args.upload:
        print("uploading to", args.device)
        r = http_json(args.device.rstrip("/") + "/api/update", data=package, timeout=120)
        if not r.get("ok"):
            fail("device rejected the package: {}".format(r.get("error")))
        print("staged:", ", ".join(r.get("files", [])))
        sys.exit(0 if wait_for_outcome(args.device) else 2)


if __name__ == "__main__":
    main()
