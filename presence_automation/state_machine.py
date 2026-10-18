"""Pure presence state machine.

Deliberately dependency-free: no camera, no subprocess, no clock reads. The
caller supplies each sample and the timestamp, which makes every debounce and
cooldown edge unit-testable without hardware.

    IDLE --(N consecutive positives)--> ARRIVING --> PRESENT
    PRESENT --(negative)--> LEAVING --(absence >= ABSENCE_SECONDS)--> COOLDOWN
    LEAVING --(M consecutive positives)--> PRESENT
    COOLDOWN --(cooldown elapsed)--> IDLE

The return path out of LEAVING is debounced symmetrically with arrival: a lone
false-positive frame must not cancel an absence countdown that is already
running, and it must not rewind the absence already accrued.
"""

from enum import Enum

from . import config


class State(Enum):
    IDLE = "idle"
    ARRIVING = "arriving"
    PRESENT = "present"
    LEAVING = "leaving"
    COOLDOWN = "cooldown"


class Event(Enum):
    """Edge-triggered side-effect requests. Emitted once per transition."""

    ARRIVED = "arrived"
    DEPARTED = "departed"


class PresenceStateMachine:
    def __init__(
        self,
        arrival_samples=None,
        absence_seconds=None,
        cooldown_seconds=None,
        return_samples=None,
    ):
        # Resolved at call time, not at def time: binding config values as
        # default arguments would freeze them at import and silently ignore
        # any later config change.
        if arrival_samples is None:
            arrival_samples = config.ARRIVAL_CONSECUTIVE_SAMPLES
        if absence_seconds is None:
            absence_seconds = config.ABSENCE_SECONDS
        if cooldown_seconds is None:
            cooldown_seconds = config.COOLDOWN_SECONDS
        if return_samples is None:
            return_samples = config.RETURN_CONFIRM_SAMPLES
        if arrival_samples < 1:
            raise ValueError("arrival_samples must be >= 1")
        if return_samples < 1:
            raise ValueError("return_samples must be >= 1")
        self.arrival_samples = arrival_samples
        self.absence_seconds = absence_seconds
        self.cooldown_seconds = cooldown_seconds
        self.return_samples = return_samples

        self.state = State.IDLE
        self._positive_streak = 0
        self._return_streak = 0
        self._absence_since = None
        self._cooldown_until = None

    # -- introspection helpers (tests / logging only) -------------------------
    @property
    def positive_streak(self):
        return self._positive_streak

    @property
    def return_streak(self):
        return self._return_streak

    def seconds_absent(self, now):
        if self._absence_since is None:
            return 0.0
        return now - self._absence_since

    def absence_remaining(self, now):
        """Seconds of continuous absence still required before DEPARTED."""
        if self._absence_since is None:
            return 0.0
        return max(0.0, self.absence_seconds - self.seconds_absent(now))

    def cooldown_remaining(self, now):
        if self._cooldown_until is None:
            return 0.0
        return max(0.0, self._cooldown_until - now)

    # -- the only mutator ----------------------------------------------------
    def update(self, present_now, now):
        """Feed one sample. Returns an Event on a transition edge, else None."""
        present_now = bool(present_now)

        if self.state is State.COOLDOWN:
            if self.cooldown_remaining(now) > 0.0:
                # Hard block: samples during cooldown cannot cause a re-launch.
                self._positive_streak = 0
                return None
            # Cooldown expired -> fall through to IDLE and honour this sample.
            self.state = State.IDLE
            self._cooldown_until = None

        if self.state is State.IDLE:
            if not present_now:
                self._positive_streak = 0
                return None
            self._positive_streak = 1
            if self._positive_streak >= self.arrival_samples:
                return self._enter_present()
            self.state = State.ARRIVING
            return None

        if self.state is State.ARRIVING:
            if not present_now:
                # Streak broken: a single glance never reaches PRESENT.
                self.state = State.IDLE
                self._positive_streak = 0
                return None
            self._positive_streak += 1
            if self._positive_streak >= self.arrival_samples:
                return self._enter_present()
            return None

        if self.state is State.PRESENT:
            if present_now:
                return None
            self.state = State.LEAVING
            self._absence_since = now
            self._return_streak = 0
            return None

        if self.state is State.LEAVING:
            if present_now:
                # A positive sample does NOT cancel the countdown on its own:
                # one false-positive frame used to reset the entire 30s timer,
                # so sustained absence never accumulated. Require the same
                # number of consecutive positives that arrival requires.
                self._return_streak += 1
                if self._return_streak >= self.return_samples:
                    self.state = State.PRESENT
                    self._absence_since = None
                    self._return_streak = 0
                return None
            # Flicker broken. Absence keeps accruing from the ORIGINAL
            # timestamp -- a stray frame must not rewind time already served.
            self._return_streak = 0
            if self.seconds_absent(now) >= self.absence_seconds:
                self.state = State.COOLDOWN
                self._absence_since = None
                self._cooldown_until = now + self.cooldown_seconds
                self._positive_streak = 0
                return Event.DEPARTED
            return None

        raise AssertionError("unreachable state: %r" % (self.state,))

    def _enter_present(self):
        self.state = State.PRESENT
        self._positive_streak = 0
        self._return_streak = 0
        self._absence_since = None
        return Event.ARRIVED
