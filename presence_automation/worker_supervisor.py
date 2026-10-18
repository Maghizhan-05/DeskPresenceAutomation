"""Worker process lifecycle for the menu-bar app.

Pure process plumbing: no camera, no UI. The menu-bar app calls only into this
module, and every OS interaction goes through a seam (`_popen`, `_run`,
`_signal`, `_process_identity`) so the rules can be regression-tested against a
fake process table instead of real processes.

Ownership is by process *identity*, not bare PID. We persist
`{pid, start_time, comm}`; `Popen.poll()` is used only while this process is the
worker's direct parent. After a menu-app restart the worker is a non-child, so
liveness is `kill(pid, 0)` plus an identity match against
`ps -o lstart=,comm= -p <pid>`. A recycled PID fails the match and is reported
as "not running" -- it is never signalled.
"""

import json
import os
import signal
import subprocess
import sys
import time

from . import config

WORKER_MODULE = "presence_automation.main"


# --- seams -------------------------------------------------------------------
def _run(args, timeout=10):
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


def _popen(args):
    """Spawn the worker detached from the UI's terminal. Returns Popen or None."""
    try:
        return subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        return None


def _signal(pid, sig):
    """Send a signal. True if delivered, False if the process is gone."""
    try:
        os.kill(int(pid), sig)
        return True
    except (OSError, ProcessLookupError, PermissionError):
        return False


def _process_identity(pid):
    """(start_time, comm) for any PID, parent or not, or None if it is gone.

    `ps -o lstart=,comm=` works for non-children, which is what makes identity
    verification possible after the menu-bar app restarts.
    """
    result = _run(["/bin/ps", "-o", "lstart=,comm=", "-p", str(int(pid))])
    if result.returncode != 0:
        return None
    line = result.stdout.strip()
    if not line:
        return None
    # lstart is a fixed 24-char-ish date ("Sun Oct 18 11:52:33 2026"), then comm.
    parts = line.split()
    if len(parts) < 6:
        return None
    start_time = " ".join(parts[:5])
    comm = " ".join(parts[5:])
    return start_time, comm


# --- persisted state ---------------------------------------------------------
def _state_path():
    return os.path.expanduser(config.MENUBAR_STATE_FILE)


def _heartbeat_path():
    return os.path.expanduser(config.WORKER_HEARTBEAT_FILE)


def _atomic_write(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as handle:
        json.dump(payload, handle, indent=2)
    os.replace(tmp, path)


def _read_json(path, default):
    try:
        with open(path, "r") as handle:
            data = json.load(handle)
    except (IOError, OSError, ValueError):
        return default
    if not isinstance(data, dict):
        return default
    return data


def _valid_worker_record(worker):
    """Return a normalised worker record, or None if the schema is not valid.

    Fail closed: a record missing a usable PID or a nonempty `start_time`
    cannot prove ownership of anything, so it is rejected outright and is
    never used to signal a process.
    """
    if not isinstance(worker, dict):
        return None
    pid = worker.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int):
        return None
    if pid <= 0:
        return None
    start_time = worker.get("start_time")
    if not isinstance(start_time, str) or not start_time.strip():
        return None
    started_at = worker.get("started_at")
    if not isinstance(started_at, (int, float)) or isinstance(started_at, bool):
        started_at = None
    return {
        "pid": pid,
        "start_time": start_time,
        "comm": worker.get("comm"),
        "started_at": started_at,
    }


def load_state():
    state = _read_json(_state_path(), {})
    enabled = state.get("enabled", False)
    worker = _valid_worker_record(state.get("worker"))
    return {"enabled": bool(enabled) if not isinstance(enabled, str) else False,
            "worker": worker}


def save_state(enabled, worker=None):
    _atomic_write(_state_path(), {"enabled": bool(enabled), "worker": worker})


def discard_invalid_state():
    """Clear a persisted record that fails schema validation. True if cleared."""
    raw = _read_json(_state_path(), {})
    if raw.get("worker") is None:
        return False
    if _valid_worker_record(raw.get("worker")) is not None:
        return False
    save_state(False, None)
    return True


# --- heartbeat (written by the worker, read by the menu) ---------------------
def write_heartbeat(state, present):
    """Called once per tick by the worker. Atomic: readers never see a partial."""
    pid = os.getpid()
    identity = _process_identity(pid)
    _atomic_write(
        _heartbeat_path(),
        {
            "state": state,
            "present": bool(present),
            "pid": pid,
            "start_time": identity[0] if identity else None,
            "comm": identity[1] if identity else None,
            "ts": time.time(),
        },
    )


def clear_heartbeat():
    try:
        os.remove(_heartbeat_path())
    except (IOError, OSError):
        pass


# --- liveness ----------------------------------------------------------------
# Trusted in-memory identity for the child WE spawned in this process. This is
# authoritative and independent of the persisted file: a corrupted menubar.json
# must never make us lose track of (or fail to terminate) our own live child.
_CHILD = {"popen": None, "pid": None, "start_time": None}  # type: dict


def _child_if_alive():
    """The Popen we own if it is still running, else None (and the slot clears)."""
    child = _CHILD["popen"]
    if child is None:
        return None
    try:
        if child.poll() is None:
            return child
    except OSError:
        return None
    _clear_child()  # reaped
    return None


def _clear_child():
    _CHILD["popen"] = None
    _CHILD["pid"] = None
    _CHILD["start_time"] = None


def _valid_heartbeat(beat):
    """Normalised heartbeat, or None when the payload cannot be trusted.

    Fail closed: a heartbeat with no numeric `ts`, no usable PID, or a
    nonstring `start_time` proves nothing about a live worker and must never
    be rendered as presence.
    """
    if not isinstance(beat, dict):
        return None
    ts = beat.get("ts")
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        return None
    pid = beat.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    start_time = beat.get("start_time")
    # Full identity is mandatory: without a nonempty start_time a heartbeat
    # cannot be tied to the tracked worker, so it can never show "Running".
    if not isinstance(start_time, str) or not start_time.strip():
        return None
    return {
        "ts": float(ts),
        "pid": pid,
        "start_time": start_time,
        "present": bool(beat.get("present")),
        "state": beat.get("state"),
    }


def read_heartbeat():
    return _read_json(_heartbeat_path(), None) or None


# --- liveness ----------------------------------------------------------------
_CHILD = {"popen": None}  # type: dict


def _identity_matches(worker):
    """True if the recorded PID is alive AND is provably the same process.

    Fail closed on every axis: the record must pass schema validation, and its
    nonempty `start_time` must match the live process exactly. A missing or
    mismatched `start_time` means we cannot prove ownership, so the PID is
    treated as NOT ours and is never signalled.

    `comm` is recorded for diagnostics but is NOT a rejection criterion --
    macOS rewrites it shortly after exec (a venv `python` shim first reports
    the venv path, then the real framework binary), so comparing it strictly
    would make a healthy worker look like a stranger.
    """
    record = _valid_worker_record(worker)
    if record is None:
        return False
    identity = _process_identity(record["pid"])
    if identity is None:
        return False
    start_time, _comm = identity
    if not isinstance(start_time, str) or not start_time.strip():
        return False
    # Exact match required -- no "absent means fine" path.
    return record["start_time"] == start_time


def is_worker_running():
    """Liveness without ever signalling an unverified PID.

    Our own live child counts regardless of what the persisted file says.
    """
    if _child_if_alive() is not None:
        return True
    return _identity_matches(load_state()["worker"])


def worker_pid():
    """PID of the worker we can prove we own, or None.

    The trusted in-memory child wins over the persisted record, so corrupted
    state cannot make us lose the PID of a child we spawned ourselves.
    """
    if _child_if_alive() is not None and _CHILD["pid"]:
        return int(_CHILD["pid"])
    worker = load_state()["worker"]
    if worker and _identity_matches(worker):
        return int(worker["pid"])
    return None


def _tracked_start_time():
    """start_time of the worker we are tracking (in-memory first), or None."""
    if _child_if_alive() is not None and _CHILD["start_time"]:
        return _CHILD["start_time"]
    worker = load_state()["worker"]
    return worker["start_time"] if worker else None


def worker_status():
    """Label for the menu: Stopped / Starting... / Running (Present|Away) / Unresponsive.

    Never raises: every malformed persisted value degrades to Stopped or
    Unresponsive rather than throwing or showing stale presence as live.
    """
    tracked = worker_pid()
    if tracked is None and not is_worker_running():
        return "Stopped"

    starting = _within_startup_grace()
    degraded = "Starting..." if starting else "Unresponsive"

    beat = _valid_heartbeat(read_heartbeat())
    if beat is None:
        # Missing, malformed, or identity-free heartbeat proves nothing.
        return degraded
    if time.time() - beat["ts"] > config.HEARTBEAT_STALE_SECONDS:
        # The worker has already proved it can write a heartbeat for this PID,
        # so it is no longer "starting" -- a stale beat means it is stuck.
        if beat["pid"] == tracked:
            return "Unresponsive"
        return degraded
    if tracked is None:
        return degraded
    # The heartbeat must belong to the exact worker we are tracking: same PID
    # AND same start_time. Both are mandatory before any "Running" label.
    if beat["pid"] != tracked:
        return degraded
    tracked_start = _tracked_start_time()
    if not tracked_start or beat["start_time"] != tracked_start:
        return degraded
    return "Running (Present)" if beat["present"] else "Running (Away)"


def _within_startup_grace():
    """True while a just-started worker may legitimately have no heartbeat yet."""
    worker = load_state()["worker"]
    if not worker:
        return False
    started_at = worker.get("started_at")
    if started_at is None:
        return False
    return (time.time() - float(started_at)) <= config.WORKER_STARTUP_GRACE_SECONDS


# --- start / stop ------------------------------------------------------------
def start_worker():
    """Spawn the worker if it is not already running. True if one is running."""
    if is_worker_running():
        save_state(True, load_state()["worker"])
        return True

    child = _popen([sys.executable, "-m", WORKER_MODULE, "--verbose"])
    if child is None:
        save_state(False, None)
        return False

    identity = _process_identity(child.pid)
    if identity is None:
        # Died instantly: claim nothing.
        save_state(False, None)
        return False

    _CHILD["popen"] = child
    _CHILD["pid"] = int(child.pid)
    _CHILD["start_time"] = identity[0]
    save_state(
        True,
        {
            "pid": int(child.pid),
            "start_time": identity[0],
            "comm": identity[1],
            "started_at": time.time(),
        },
    )
    return True


def _terminate_child(child, timeout):
    """Terminate OUR Popen directly. True only once it is confirmed exited.

    This path does not consult the persisted file at all: a corrupted
    menubar.json must never prevent us from stopping a child we own.
    """
    try:
        child.terminate()
    except OSError:
        pass
    deadline = time.time() + timeout
    while time.time() < deadline:
        if child.poll() is not None:
            return True
        time.sleep(0.2)
    try:
        child.kill()
    except OSError:
        pass
    hard_deadline = time.time() + timeout
    while time.time() < hard_deadline:
        if child.poll() is not None:
            return True
        time.sleep(0.2)
    return child.poll() is not None


def stop_worker(timeout=None):
    """SIGTERM, then SIGKILL after the grace window. True once no worker lives.

    The return value is process-exit verification -- the OS reclaims the camera
    device when the process dies. It is not a query of camera hardware state.
    """
    if timeout is None:
        timeout = config.WORKER_STOP_GRACE_SECONDS

    # 1. A child we own is handled through its Popen, ignoring persisted state.
    child = _child_if_alive()
    if child is not None:
        if not _terminate_child(child, timeout):
            # Still alive: never report success and never drop the handle.
            return False
        _clear_child()
        save_state(False, None)
        clear_heartbeat()
        return True

    # 2. Otherwise only a fully verified persisted identity may be signalled.
    pid = worker_pid()
    if pid is None:
        _clear_child()
        save_state(False, None)
        clear_heartbeat()
        return True

    _signal(pid, signal.SIGTERM)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not is_worker_running():
            break
        time.sleep(0.2)

    if is_worker_running():
        _signal(pid, signal.SIGKILL)
        hard_deadline = time.time() + timeout
        while time.time() < hard_deadline and is_worker_running():
            time.sleep(0.2)

    stopped = not is_worker_running()
    if stopped:
        _clear_child()
        save_state(False, None)
        clear_heartbeat()
    return stopped


# --- Launch at Login ---------------------------------------------------------
def _login_service_target():
    return "gui/%d/%s" % (os.getuid(), config.MENUBAR_LAUNCHD_LABEL)


def is_login_item_enabled():
    """Parse `launchctl print-disabled` -- the authoritative persisted state.

    `launchctl enable|disable` is used instead of bootstrap/bootout precisely so
    toggling never unloads the job currently hosting this menu-bar UI.
    """
    result = _run(["/bin/launchctl", "print-disabled", "gui/%d" % os.getuid()])
    if result.returncode != 0:
        return False
    needle = '"%s"' % config.MENUBAR_LAUNCHD_LABEL
    for line in result.stdout.splitlines():
        if needle in line:
            return "=> disabled" not in line.replace("  ", " ")
    # Absent from the disabled map means not explicitly disabled; it counts as
    # enabled only if the agent is actually installed.
    return login_item_installed()


def login_item_installed():
    return os.path.exists(
        os.path.expanduser(
            "~/Library/LaunchAgents/%s.plist" % config.MENUBAR_LAUNCHD_LABEL
        )
    )


def _set_login_item(enabled):
    verb = "enable" if enabled else "disable"
    _run(["/bin/launchctl", verb, _login_service_target()])
    return is_login_item_enabled() == enabled  # verified by re-query, not exit code


def enable_login_item():
    return _set_login_item(True)


def disable_login_item():
    return _set_login_item(False)
