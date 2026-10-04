# Bootstrap for the Pico voltage logger.
#
# This file is only ever updated over USB (deploy.py), never over the air,
# so a bad remote update can always be recovered. On every boot it:
#
#   1. finishes an interrupted rollback, if any
#   2. applies a staged over-the-air update: app.py writes <name>.new files
#      and update_ready.json; they are swapped in here, keeping the
#      replaced versions as <name>.old
#   3. performs a rollback requested over BLE ("rollback" command)
#   4. runs app.main(). While an update is unconfirmed ("pending") a
#      hardware watchdog is armed. app.py confirms the update once it has
#      run for a while; an exception, or MAX_PENDING_BOOTS boots without
#      confirmation (hangs reset via the watchdog), restores the .old files.
#
# Every step is written to survive power loss: state is recorded before
# files are renamed, and renames are repeated safely on the next boot.

import os
import sys
import json
import time
import machine

UPDATABLE_FILES = ("app.py", "ble_console.py", "config_manager.py")
STATE_FILE = "update_state.json"
READY_FILE = "update_ready.json"
ROLLBACK_REQUEST_FILE = "update_rollback.json"
DEPLOY_MANIFEST = ".deploy-manifest.json"
ERROR_LOG = "boot_error.txt"
MAX_PENDING_BOOTS = 3
WATCHDOG_MS = 8000          # RP2040 maximum is about 8.3 s


def exists(path):
    try:
        os.stat(path)
        return True
    except OSError:
        return False


def remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    try:
        os.rename(tmp, path)
    except OSError:
        remove(path)
        os.rename(tmp, path)


def set_manifest_hashes(hashes):
    # Keep deploy.py's manifest in step with files changed over the air.
    manifest = read_json(DEPLOY_MANIFEST)
    if manifest is None:
        return
    files = manifest.get("files", {})
    for name, digest in hashes.items():
        if digest:
            files[name] = digest
        else:
            files.pop(name, None)
    manifest["files"] = files
    write_json(DEPLOY_MANIFEST, manifest)


def apply_update(state):
    ready = read_json(READY_FILE)
    if ready is None:
        return state
    hashes = ready.get("files", {})
    names = [n for n in hashes if n in UPDATABLE_FILES]

    if state.get("state") != "applying":
        # Fresh start: the .old set must hold exactly the version being
        # replaced, so discard backups from any earlier update.
        for name in UPDATABLE_FILES:
            remove(name + ".old")
        previous = (read_json(DEPLOY_MANIFEST) or {}).get("files", {})
        state = {
            "state": "applying",
            "version": ready.get("version", ""),
            "previous_version": state.get("version", ""),
            "previous_hashes": dict((n, previous.get(n)) for n in names),
            "last_created": max(int(state.get("last_created", 0)),
                                int(ready.get("created", 0))),
        }
        write_json(STATE_FILE, state)

    for name in names:
        if exists(name + ".new"):
            if exists(name):
                remove(name + ".old")
                os.rename(name, name + ".old")
            os.rename(name + ".new", name)

    set_manifest_hashes(dict((n, hashes[n]) for n in names))
    state["state"] = "pending"
    state["attempts"] = 0
    write_json(STATE_FILE, state)
    remove(READY_FILE)
    print("Update {} applied: {}".format(state["version"], ", ".join(names)))
    return state


def rollback(state, reason):
    if state.get("state") != "rolling_back":
        state["state"] = "rolling_back"
        state["reason"] = reason
        write_json(STATE_FILE, state)

    restored = []
    for name in UPDATABLE_FILES:
        if exists(name + ".old"):
            remove(name)
            os.rename(name + ".old", name)
            restored.append(name)
    if restored:
        set_manifest_hashes(state.get("previous_hashes", {}))

    new_state = {
        "state": "rolled_back",
        "reason": state.get("reason", reason),
        "version": state.get("previous_version", ""),
        "rejected_version": state.get("version", ""),
        "last_created": state.get("last_created", 0),
    }
    write_json(STATE_FILE, new_state)
    print("Rolled back ({}): {}".format(new_state["reason"],
                                        ", ".join(restored) or "nothing"))
    return new_state


def log_exception(exc):
    try:
        with open(ERROR_LOG, "w") as f:
            sys.print_exception(exc, f)
    except OSError:
        pass
    sys.print_exception(exc)


def boot():
    state = read_json(STATE_FILE) or {}

    if state.get("state") == "rolling_back":
        state = rollback(state, state.get("reason", "interrupted rollback"))
    state = apply_update(state)

    if exists(ROLLBACK_REQUEST_FILE):
        remove(ROLLBACK_REQUEST_FILE)
        state = rollback(state, "requested over BLE")

    watchdog = None
    if state.get("state") == "pending":
        state["attempts"] = state.get("attempts", 0) + 1
        write_json(STATE_FILE, state)
        if state["attempts"] > MAX_PENDING_BOOTS:
            rollback(state, "not confirmed after {} boots".format(
                MAX_PENDING_BOOTS))
            machine.reset()
        watchdog = machine.WDT(timeout=WATCHDOG_MS)

    try:
        import app
        app.main(watchdog=watchdog)
    except KeyboardInterrupt:
        # Ctrl-C from a USB REPL/mpremote: drop to the REPL.
        raise
    except Exception as exc:
        log_exception(exc)
        if state.get("state") == "pending":
            rollback(state, "new version raised {}: {}".format(
                type(exc).__name__, exc))
            machine.reset()
        # Confirmed code failed (e.g. a transient error). This unit runs
        # unattended, so restart rather than stopping at the REPL.
        time.sleep(10)
        machine.reset()


boot()
