"""Fixed-size in-memory preview buffer (opt-in).

Privacy contract, deliberately narrow:

* The segment is a FIXED number of bytes, allocated once and overwritten in
  place. It never grows, never appends, and holds exactly one downscaled frame.
* Nothing here writes to disk, encodes, serialises, or transmits a frame. The
  only consumer is the local menu-bar process mapping the same segment.
* The segment is unlinked when the worker stops, so no frame data outlives the
  worker process. A stale segment from a crashed worker is unlinked and
  recreated at startup rather than reused.

The header carries a monotonically increasing sequence number so a reader can
tell a fresh frame from a stale one, plus the face box for the overlay.
"""

import struct
from multiprocessing import shared_memory

from . import config

# seq (Q) | present (i) | x, y, w, h (4i) = 8 + 4 + 16 = 28 bytes
_HEADER = struct.Struct("<Qi4i")
HEADER_SIZE = _HEADER.size


def frame_bytes():
    return config.PREVIEW_WIDTH * config.PREVIEW_HEIGHT * config.PREVIEW_CHANNELS


def segment_size():
    """Total fixed allocation. Never varies at runtime."""
    return HEADER_SIZE + frame_bytes()


class PreviewUnavailable(RuntimeError):
    """Shared memory could not be created or mapped."""


def unlink_stale(name=None):
    """Remove an orphaned segment left behind by a crashed worker.

    Returns True if a segment existed and was unlinked.
    """
    if name is None:
        name = config.PREVIEW_SHM_NAME
    try:
        existing = shared_memory.SharedMemory(name=name)
    except FileNotFoundError:
        return False
    except Exception:
        return False
    try:
        existing.close()
        existing.unlink()
    except Exception:
        return False
    return True


class PreviewWriter:
    """Worker side. Creates the segment and overwrites it in place."""

    def __init__(self, name=None):
        self.name = name or config.PREVIEW_SHM_NAME
        # A crashed worker may have left a segment behind: never reuse it.
        unlink_stale(self.name)
        try:
            self._shm = shared_memory.SharedMemory(
                name=self.name, create=True, size=segment_size()
            )
        except Exception as exc:  # pragma: no cover - platform dependent
            raise PreviewUnavailable("cannot create preview buffer: %s" % exc)
        self._seq = 0
        self.closed = False

    @property
    def size(self):
        """Actual mapped size. The OS rounds up to a page boundary, so this
        may exceed segment_size(); what matters is that it NEVER changes after
        creation -- there is exactly one slot, overwritten in place."""
        return self._shm.size

    @property
    def requested_size(self):
        return segment_size()

    def write(self, frame_bytes_in, present, box=None):
        """Overwrite the single slot. `frame_bytes_in` must be exactly sized."""
        if self.closed:
            raise PreviewUnavailable("preview buffer already closed")
        expected = frame_bytes()
        if len(frame_bytes_in) != expected:
            raise ValueError(
                "frame must be exactly %d bytes, got %d"
                % (expected, len(frame_bytes_in))
            )
        x, y, w, h = box if box else (0, 0, 0, 0)
        self._seq += 1
        buf = self._shm.buf
        buf[:HEADER_SIZE] = _HEADER.pack(
            self._seq, 1 if present else 0, int(x), int(y), int(w), int(h)
        )
        buf[HEADER_SIZE:HEADER_SIZE + expected] = frame_bytes_in
        return self._seq

    def close(self, unlink=True):
        """Release the mapping and (by default) destroy the segment."""
        if self.closed:
            return
        self.closed = True
        try:
            self._shm.close()
        finally:
            if unlink:
                try:
                    self._shm.unlink()
                except FileNotFoundError:
                    pass


class PreviewReader:
    """Menu-bar side. Maps the existing segment read-only; never creates it."""

    def __init__(self, name=None):
        self.name = name or config.PREVIEW_SHM_NAME
        try:
            self._shm = shared_memory.SharedMemory(name=self.name)
        except FileNotFoundError:
            raise PreviewUnavailable("no preview buffer (is the worker running?)")
        # A reader never owns the segment: drop it from this process's
        # resource tracker so shutdown does not warn about (or try to clean
        # up) a segment the writer is responsible for.
        try:
            from multiprocessing import resource_tracker

            resource_tracker.unregister(self._shm._name, "shared_memory")
        except Exception:
            pass
        if self._shm.size < segment_size():
            self._shm.close()
            raise PreviewUnavailable("preview buffer has unexpected size")
        self.closed = False

    def read(self):
        """Return (seq, present, box, frame_bytes) or None if never written."""
        if self.closed:
            raise PreviewUnavailable("preview reader already closed")
        buf = self._shm.buf
        seq, present, x, y, w, h = _HEADER.unpack(bytes(buf[:HEADER_SIZE]))
        if seq == 0:
            return None
        data = bytes(buf[HEADER_SIZE:HEADER_SIZE + frame_bytes()])
        return seq, bool(present), (x, y, w, h), data

    def close(self):
        if self.closed:
            return
        self.closed = True
        self._shm.close()  # readers never unlink
