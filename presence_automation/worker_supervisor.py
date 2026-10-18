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


def load_state():
    state = _read_json(_state_path(), {})
    return {
        "enabled": bool(state.get("enabled", False)),
        "worker": state.get("worker") or None,
    }


def save_state(enabled, worker=None):
    _atomic_write(_state_path(), {"enabled": bool(enabled), "worker": worker})


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


def read_heartbeat():
    return _read_json(_heartbeat_path(), None) or None


# --- liveness ----------------------------------------------------------------
_CHILD = {"popen": None}  # type: dict


def _identity_matches(worker):
    """True if the recorded PID is alive AND is still the same process.

    `start_time` is the authoritative anchor: a recycled PID necessarily has a
    later start time, so a match rules out PID reuse. `comm` is recorded for
    diagnostics but is NOT a rejection criterion -- macOS rewrites it shortly
    after exec (a venv `python` shim first reports the venv path, then the real
    framework binary), so comparing it strictly would make a healthy worker
    look like a stranger.
    """
    if not worker or not worker.get("pid"):
        return False
    identity = _process_identity(worker["pid"])
    if identity is None:
        return False
    start_time, _comm = identity
    if worker.get("start_time") and worker["start_time"] != start_time:
        return False  # PID recycled by a different process
    return True


def is_worker_running():
    """Liveness without ever signalling an unverified PID."""
    child = _CHILD["popen"]
    if child is not None:
        if child.poll() is None:
            return True
        _CHILD["popen"] = None  # reaped; fall through to the persisted record
    return _identity_matches(load_state()["worker"])


def worker_pid():
    state = load_state()
    worker = state["worker"]
    if worker and _identity_matches(worker):
        return int(worker["pid"])
    return None


def worker_status():
    """Label for the menu: Stopped / Starting... / Running (Present|Away) / Unresponsive."""
    if not is_worker_running():
        return "Stopped"
    beat = read_heartbeat()
    starting = _within_startup_grace()
    if not beat or not beat.get("ts"):
        # A freshly spawned worker has not written its first heartbeat yet;
        # reporting "Unresponsive" there would be a false alarm.
        return "Starting..." if starting else "Unresponsive"
    if time.time() - float(beat["ts"]) > config.HEARTBEAT_STALE_SECONDS:
        return "Starting..." if starting else "Unresponsive"
    # The heartbeat must belong to the worker we are tracking.
    tracked = worker_pid()
    if tracked is not None and beat.get("pid") not in (None, tracked):
        return "Starting..." if starting else "Unresponsive"
    return "Running (Present)" if beat.get("present") else "Running (Away)"


def _within_startup_grace():
    """True while a just-started worker may legitimately have no heartbeat yet."""
    worker = load_state()["worker"] or {}
    started_at = worker.get("started_at")
    if not started_at:
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


def stop_worker(timeout=None):
    """SIGTERM, then SIGKILL after the grace window. True once no worker lives.

    The return value is process-exit verification -- the OS reclaims the camera
    device when the process dies. It is not a query of camera hardware state.
    """
    if timeout is None:
        timeout = config.WORKER_STOP_GRACE_SECONDS

    pid = worker_pid()
    if pid is None:
        _CHILD["popen"] = None
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
        _CHILD["popen"] = None
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
