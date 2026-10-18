"""Launch/quit tracked apps, never touching apps the user opened themselves.

Ownership is per *process instance*, not per bundle ID. `launch_tracked()`
records the PID of the process it actually started; `close_tracked()` terminates
only that exact PID and discards the record if that process is already gone. A
bundle ID alone is not ownership: if our instance exits and the user later opens
the same app, the replacement has a different PID and is never touched.

Seams `_running_app_table()`, `_spawn()` and `_terminate()` exist so the
ownership rules can be regression-tested without launching real apps.
"""

import json
import os
import subprocess
import time

from . import config

# A cold-started GUI app can take ~10s to appear in NSWorkspace.
LAUNCH_GRACE_SECONDS = 20.0


def _session_path():
    return os.path.expanduser(config.SESSION_FILE)


def _run(args, timeout=15):
    try:
        return subprocess.run(
            args, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (subprocess.TimeoutExpired, OSError):
        class _Failed(object):
            returncode = -1
            stdout = ""
            stderr = "timeout"

        return _Failed()


# --- running-app inspection --------------------------------------------------
def _running_app_table():
    """List of (bundle_id, pid) for every running GUI application.

    NSWorkspace.runningApplications() is served from a cache that only
    refreshes when the process pumps its run loop -- in a plain script it
    otherwise returns the snapshot taken at first access, so a just-launched
    app looks absent forever. Pump briefly before reading.
    """
    try:
        from AppKit import NSWorkspace  # type: ignore
        from Foundation import NSDate, NSRunLoop  # type: ignore

        NSRunLoop.currentRunLoop().runUntilDate_(
            NSDate.dateWithTimeIntervalSinceNow_(0.2)
        )
        table = []
        for app in NSWorkspace.sharedWorkspace().runningApplications():
            bundle_id = app.bundleIdentifier()
            if bundle_id:
                table.append((bundle_id, int(app.processIdentifier())))
        return table
    except Exception:
        return _running_app_table_fallback()


def _running_app_table_fallback():
    """pyobjc-free fallback: parse lsappinfo for bundle IDs and PIDs."""
    result = _run(["/usr/bin/lsappinfo", "list"])
    table = []
    if result.returncode != 0:
        return table
    pid = None
    for line in result.stdout.splitlines():
        if "pid = " in line:
            try:
                pid = int(line.split("pid = ")[1].split()[0])
            except (IndexError, ValueError):
                pid = None
        if 'bundleID="' in line:
            bundle_id = line.split('bundleID="')[1].split('"')[0]
            if bundle_id and pid is not None:
                table.append((bundle_id, pid))
                pid = None
    return table


def running_bundle_ids():
    """Set of bundle IDs with at least one running instance."""
    return {bundle_id for bundle_id, _pid in _running_app_table()}


def running_pids(bundle_id):
    """PIDs currently running for this bundle ID."""
    return {pid for bid, pid in _running_app_table() if bid == bundle_id}


def is_running(bundle_id):
    return bundle_id in running_bundle_ids()


def is_pid_running(bundle_id, pid):
    """True only if that exact PID is alive AND still owned by this bundle ID."""
    if not pid:
        return False
    return (bundle_id, int(pid)) in _running_app_table()


# --- process control seams ---------------------------------------------------
def _spawn(bundle_id):
    """Launch an app in the background. True if the launch request succeeded."""
    return _run(["/usr/bin/open", "-g", "-b", bundle_id]).returncode == 0


def _terminate(bundle_id, pid):
    """Gracefully quit one exact process instance. True if it was asked to quit."""
    try:
        from AppKit import NSRunningApplication  # type: ignore

        app = NSRunningApplication.runningApplicationWithProcessIdentifier_(int(pid))
        if app is None or app.bundleIdentifier() != bundle_id:
            return False  # PID reused by something else -- never touch it
        return bool(app.terminate())
    except Exception:
        # Fallback: AppleScript can only target a bundle, so verify first that
        # our PID is the only instance before using it.
        if running_pids(bundle_id) != {int(pid)}:
            return False
        script = 'tell application id "%s" to quit' % bundle_id
        return _run(["/usr/bin/osascript", "-e", script]).returncode == 0


# --- session bookkeeping (PIDs + timestamps only, never frames) --------------
def _load_session():
    path = _session_path()
    try:
        with open(path, "r") as handle:
            data = json.load(handle)
    except (IOError, OSError, ValueError):
        return {"launched": []}
    if not isinstance(data, dict) or not isinstance(data.get("launched"), list):
        return {"launched": []}
    return data


def _save_session(data):
    path = _session_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as handle:
        json.dump(data, handle, indent=2)
    os.replace(tmp, path)


def record_ownership(bundle_id, pid):
    """Claim one process instance as automation-launched."""
    session = _load_session()
    session["launched"] = [
        entry
        for entry in session["launched"]
        if not (entry.get("bundle_id") == bundle_id and entry.get("pid") == pid)
    ]
    session["launched"].append(
        {"bundle_id": bundle_id, "pid": int(pid), "launched_at": time.time()}
    )
    _save_session(session)


def release_ownership(bundle_id):
    """Drop every ownership record for a bundle ID (rollback on failed launch)."""
    session = _load_session()
    before = len(session["launched"])
    session["launched"] = [
        entry for entry in session["launched"] if entry.get("bundle_id") != bundle_id
    ]
    if len(session["launched"]) != before:
        _save_session(session)


def tracked_bundle_ids():
    return [entry["bundle_id"] for entry in _load_session()["launched"]]


def tracked_instances():
    """[(bundle_id, pid)] currently claimed as ours."""
    return [
        (entry.get("bundle_id"), entry.get("pid"))
        for entry in _load_session()["launched"]
    ]


def clear_session():
    data = _load_session()
    data["launched"] = []
    _save_session(data)


# --- launch / quit -----------------------------------------------------------
def _await_new_pid(bundle_id, known_pids, deadline, poll=1.0):
    """Wait for a PID for this bundle that was not running before we launched."""
    while True:
        fresh = running_pids(bundle_id) - known_pids
        if fresh:
            return min(fresh)
        if time.time() >= deadline:
            return None
        time.sleep(poll)


def launch_tracked(bundle_ids=None):
    """Launch apps that are not already running; record the exact PIDs we start.

    Ownership is claimed only after the launch succeeds AND a new PID is
    observed. If either fails, nothing is recorded, so a later user-opened
    instance can never be mistaken for ours.
    """
    if bundle_ids is None:
        bundle_ids = config.TRACKED_BUNDLE_IDS

    launched = []
    for bundle_id in bundle_ids:
        existing = running_pids(bundle_id)
        if existing:
            continue  # user's own instance -- hands off, and never recorded
        if not _spawn(bundle_id):
            continue
        pid = _await_new_pid(
            bundle_id, existing, time.time() + LAUNCH_GRACE_SECONDS
        )
        if pid is None:
            continue  # launch never materialised: claim nothing
        record_ownership(bundle_id, pid)
        launched.append(bundle_id)
    return launched


def close_tracked():
    """Quit only the exact instances we launched. Returns the quit bundle IDs.

    A record whose PID is no longer alive (or whose PID now belongs to another
    app) is discarded without quitting anything -- that is the user's app.
    """
    session = _load_session()
    entries = session.get("launched", [])
    if not entries:
        return []

    quit_ids = []
    survivors = []
    for entry in entries:
        bundle_id = entry.get("bundle_id")
        pid = entry.get("pid")
        if not bundle_id or not pid:
            continue  # malformed/legacy record: no verifiable identity, drop it
        if not is_pid_running(bundle_id, pid):
            continue  # our instance is gone; any replacement is NOT ours
        if _terminate(bundle_id, pid):
            quit_ids.append(bundle_id)
        else:
            survivors.append(entry)  # still ours, retry on the next departure

    session["launched"] = survivors
    _save_session(session)
    return quit_ids
