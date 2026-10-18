"""Orchestration loop: detector -> state machine -> app/music side effects.

Side effects fire on transition *edges* only (Event.ARRIVED / Event.DEPARTED),
never per tick. Only state transitions are logged -- timestamps and bundle IDs,
never image data.
"""

import argparse
import logging
import os
import signal
import sys
import time

from . import app_manager, config, spotify_control, worker_supervisor
from .state_machine import Event, PresenceStateMachine

LOG = logging.getLogger("deskpresence")
_RUNNING = True


def _configure_logging(verbose=False):
    log_path = os.path.expanduser(config.LOG_FILE)
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    handlers = [logging.FileHandler(log_path), logging.StreamHandler(sys.stderr)]
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
    )


def _install_signal_handlers():
    def _stop(signum, _frame):
        global _RUNNING
        _RUNNING = False
        LOG.info("signal %s received, shutting down", signum)

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)


def on_arrived():
    launched = app_manager.launch_tracked(config.TRACKED_BUNDLE_IDS)
    LOG.info("ARRIVED: launched=%s (already-running apps untouched)", launched or "[]")
    if spotify_control.play_configured_item():
        LOG.info("ARRIVED: playing %s", config.SPOTIFY_URI)
    else:
        LOG.warning("ARRIVED: Spotify playback request failed")


def on_departed():
    spotify_control.pause()
    closed = app_manager.close_tracked()
    LOG.info("DEPARTED: paused music, quit=%s (only automation-launched)", closed or "[]")


def run(interval=None, max_ticks=None, detector=None):
    from .detector import Detector, DetectorUnavailable

    if interval is None:
        interval = config.SAMPLE_INTERVAL_SECONDS
    machine = PresenceStateMachine()
    owns_detector = detector is None
    if owns_detector:
        try:
            detector = Detector()
        except DetectorUnavailable as exc:
            LOG.error("detector unavailable: %s", exc)
            return 1

    LOG.info(
        "started: arrival=%s samples, absence=%ss, cooldown=%ss, apps=%s",
        config.ARRIVAL_CONSECUTIVE_SAMPLES,
        config.ABSENCE_SECONDS,
        config.COOLDOWN_SECONDS,
        list(config.TRACKED_BUNDLE_IDS),
    )

    ticks = 0
    try:
        while _RUNNING and (max_ticks is None or ticks < max_ticks):
            ticks += 1
            try:
                present = detector.sample()
            except DetectorUnavailable as exc:
                LOG.error("detector error: %s", exc)
                time.sleep(interval)
                continue

            previous = machine.state
            event = machine.update(present, time.monotonic())
            if machine.state is not previous:
                LOG.debug("%s -> %s (present=%s)", previous.value, machine.state.value, present)

            if event is Event.ARRIVED:
                on_arrived()
            elif event is Event.DEPARTED:
                on_departed()

            # Liveness + presence for the menu bar. Atomic, one write per tick.
            worker_supervisor.write_heartbeat(machine.state.value, present)

            time.sleep(interval)
    finally:
        if owns_detector:
            detector.close()
        worker_supervisor.clear_heartbeat()
        LOG.info("stopped after %s samples", ticks)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Desk presence automation")
    parser.add_argument("--interval", type=float, default=config.SAMPLE_INTERVAL_SECONDS)
    parser.add_argument("--max-ticks", type=int, default=None,
                        help="exit after N samples (manual testing)")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--self-test", action="store_true",
                        help="probe camera + app detection and exit, no automation")
    args = parser.parse_args(argv)

    _configure_logging(args.verbose)
    _install_signal_handlers()

    if args.self_test:
        return _self_test()
    return run(interval=args.interval, max_ticks=args.max_ticks)


def _self_test():
    from .detector import Detector, DetectorUnavailable

    running = app_manager.running_bundle_ids()
    for bundle_id in list(config.TRACKED_BUNDLE_IDS) + [config.SPOTIFY_BUNDLE_ID]:
        LOG.info("app %s running=%s", bundle_id, bundle_id in running)
    LOG.info("tracked-as-ours (session file): %s", app_manager.tracked_bundle_ids())
    try:
        with Detector() as detector:
            samples = [detector.sample() for _ in range(3)]
        LOG.info("camera ok, 3 samples -> %s", samples)
    except DetectorUnavailable as exc:
        LOG.error("camera FAILED: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
