"""Launch/quit tracked apps, never touching apps the user opened themselves.

Guard: before launching, each bundle ID is checked against the currently running
applications. Only the ones we actually started are recorded in a session file,
and `close_tracked()` quits *only* what is in that file. If the user had an app
open already, it is never recorded and therefore never quit.
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
    return subprocess.run(
        args, capture_output=True, text=True, timeout=timeout, check=False
    )


# --- running-app inspection --------------------------------------------------
def running_bundle_ids():
    """Set of bundle IDs with at least one running instance.

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
        return {
            app.bundleIdentifier()
            for app in NSWorkspace.sharedWorkspace().runningApplications()
            if app.bundleIdentifier()
        }
    except Exception:
        return _running_bundle_ids_fallback()


def _running_bundle_ids_fallback():
    """pyobjc-free fallback: ask lsappinfo, else pgrep per known app path."""
    result = _run(["/usr/bin/lsappinfo", "list"])
    found = set()
    if result.returncode == 0:
        for line in result.stdout.splitlines():
            if "bundleID=" in line:
                found.add(line.split('bundleID="')[-1].split('"')[0])
    return found


def is_running(bundle_id):
    return bundle_id in running_bundle_ids()


# --- session bookkeeping (timestamps + bundle IDs only, never frames) --------
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


def tracked_bundle_ids():
    return [entry["bundle_id"] for entry in _load_session()["launched"]]


def clear_session():
    data = _load_session()
    data["launched"] = []
    _save_session(data)


# --- launch / quit -----------------------------------------------------------
def launch_tracked(bundle_ids=None):
    """Launch only the apps that are not already running. Returns launched IDs."""
    if bundle_ids is None:
        bundle_ids = config.TRACKED_BUNDLE_IDS
    already = running_bundle_ids()
    session = _load_session()
    recorded = {entry["bundle_id"] for entry in session["launched"]}
    launched = []

    for bundle_id in bundle_ids:
        if bundle_id in already:
            continue  # user's own instance -- hands off, and never recorded
        result = _run(["/usr/bin/open", "-g", "-b", bundle_id])
        if result.returncode != 0:
            continue
        launched.append(bundle_id)
        if bundle_id not in recorded:
            session["launched"].append(
                {"bundle_id": bundle_id, "launched_at": time.time()}
            )
            recorded.add(bundle_id)

    if launched:
        _save_session(session)
    return launched


def _await_appearance(bundle_id, deadline, poll=1.0):
    """Wait for a cold-starting app to register, until `deadline`."""
    while time.time() < deadline:
        if is_running(bundle_id):
            return True
        time.sleep(poll)
    return is_running(bundle_id)


def close_tracked():
    """Quit only apps this automation launched. Returns the quit bundle IDs."""
    session = _load_session()
    entries = session.get("launched", [])
    if not entries:
        return []

    quit_ids = []
    for entry in entries:
        bundle_id = entry.get("bundle_id")
        if not bundle_id:
            continue
        if not is_running(bundle_id):
            # A cold-starting app can take ~10s to register with NSWorkspace.
            # Without this wait we would drop the record and orphan the window.
            launched_at = entry.get("launched_at", 0.0)
            grace_end = launched_at + LAUNCH_GRACE_SECONDS
            if time.time() < grace_end and _await_appearance(bundle_id, grace_end):
                pass  # it showed up -- fall through and quit it
            else:
                quit_ids.append(bundle_id)  # genuinely gone; clear the record
                continue
        script = 'tell application id "%s" to quit' % bundle_id
        result = _run(["/usr/bin/osascript", "-e", script])
        if result.returncode == 0:
            quit_ids.append(bundle_id)

    session["launched"] = [
        entry for entry in entries if entry.get("bundle_id") not in quit_ids
    ]
    _save_session(session)
    return quit_ids
