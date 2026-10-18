"""Live camera preview window (opt-in, in-memory only).

Reads the worker's fixed shared-memory slot and draws it into an NSWindow at
the sample cadence, with a face box overlay and the live absence countdown.

Nothing here writes to disk, encodes a file, or sends a frame anywhere: it maps
the worker's existing segment read-only and blits it to the screen. Closing the
window stops polling and never stops the worker.

The PyObjC imports are deliberately lazy so this module can be imported (and
its pure logic tested) on a headless machine without a window server.
"""

from . import config, worker_supervisor
from .preview_buffer import PreviewReader, PreviewUnavailable

WINDOW_TITLE = "Desk Presence - Camera Preview"
NO_WORKER_TEXT = "Automation is off - enable it to see the camera."
WAITING_TEXT = "Waiting for the first frame..."


def countdown_text(beat):
    """Human-readable status line from the worker's heartbeat.

    Pure function: no UI, no camera, no clock -- unit-testable as-is.
    """
    if not isinstance(beat, dict):
        return NO_WORKER_TEXT
    state = beat.get("state")
    present = bool(beat.get("present"))
    threshold = beat.get("absence_threshold", config.ABSENCE_SECONDS)
    if state == "leaving":
        remaining = beat.get("absence_seconds_remaining")
        if isinstance(remaining, (int, float)):
            return "Away - closing apps in %.0fs" % max(0.0, float(remaining))
        return "Away"
    if state == "cooldown":
        remaining = beat.get("cooldown_seconds_remaining")
        if isinstance(remaining, (int, float)):
            return "Left - cooldown %.0fs" % max(0.0, float(remaining))
        return "Left - cooling down"
    if state == "present":
        return "At desk - face detected" if present else "At desk"
    if state == "arriving":
        return "Confirming you are here..."
    if state == "idle":
        return "Desk empty (threshold %.0fs)" % float(threshold)
    return "Unknown state"


class PreviewController(object):
    """Owns the reader; the window layer calls tick() at the sample cadence."""

    def __init__(self, supervisor=worker_supervisor, reader_factory=PreviewReader):
        self.supervisor = supervisor
        self._reader_factory = reader_factory
        self._reader = None
        self.last_seq = None

    def open(self):
        """Ask the worker to publish frames. Idempotent."""
        self.supervisor.set_preview_requested(True)

    def close(self):
        """Stop publishing and drop the mapping. Never stops the worker."""
        self.supervisor.set_preview_requested(False)
        if self._reader is not None:
            try:
                self._reader.close()
            finally:
                self._reader = None

    def tick(self):
        """Return (frame_bytes_or_None, box, status_text) for the UI layer."""
        beat = self.supervisor.read_heartbeat()
        text = countdown_text(beat)
        if not self.supervisor.is_worker_running():
            self._drop_reader()
            return None, (0, 0, 0, 0), NO_WORKER_TEXT
        if self._reader is None:
            try:
                self._reader = self._reader_factory()
            except PreviewUnavailable:
                return None, (0, 0, 0, 0), WAITING_TEXT
        try:
            frame = self._reader.read()
        except Exception:
            self._drop_reader()
            return None, (0, 0, 0, 0), WAITING_TEXT
        if frame is None:
            return None, (0, 0, 0, 0), WAITING_TEXT
        seq, _present, box, data = frame
        self.last_seq = seq
        return data, box, text

    def _drop_reader(self):
        if self._reader is not None:
            try:
                self._reader.close()
            except Exception:
                pass
            self._reader = None


# --- AppKit window (lazy, never imported during unit tests) ------------------
def show_window(controller=None):  # pragma: no cover - requires a window server
    """Open the preview NSWindow. Returns the window object."""
    import AppKit
    import objc  # noqa: F401  (ensures the bridge is initialised)
    from Foundation import NSMakeRect

    controller = controller or PreviewController()
    controller.open()

    width, height = config.PREVIEW_WIDTH, config.PREVIEW_HEIGHT
    rect = NSMakeRect(0, 0, width, height + 28)
    style = (
        AppKit.NSWindowStyleMaskTitled
        | AppKit.NSWindowStyleMaskClosable
        | AppKit.NSWindowStyleMaskMiniaturizable
    )
    window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        rect, style, AppKit.NSBackingStoreBuffered, False
    )
    window.setTitle_(WINDOW_TITLE)
    window.setReleasedWhenClosed_(False)

    image_view = AppKit.NSImageView.alloc().initWithFrame_(
        NSMakeRect(0, 28, width, height)
    )
    image_view.setImageScaling_(AppKit.NSImageScaleAxesIndependently)
    label = AppKit.NSTextField.alloc().initWithFrame_(NSMakeRect(6, 4, width - 12, 20))
    label.setBezeled_(False)
    label.setDrawsBackground_(False)
    label.setEditable_(False)
    label.setSelectable_(False)
    label.setStringValue_(WAITING_TEXT)

    content = window.contentView()
    content.addSubview_(image_view)
    content.addSubview_(label)
    window.makeKeyAndOrderFront_(None)
    return window, image_view, label, controller


def frame_to_nsimage(data, width=None, height=None):  # pragma: no cover - AppKit
    """Wrap raw BGR bytes in an NSImage without touching the filesystem."""
    import AppKit

    width = width or config.PREVIEW_WIDTH
    height = height or config.PREVIEW_HEIGHT
    # BGR -> RGB for display; purely in memory, no encoding, no file.
    rgb = bytearray(len(data))
    rgb[0::3] = data[2::3]
    rgb[1::3] = data[1::3]
    rgb[2::3] = data[0::3]
    rep = AppKit.NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, width, height, 8, 3, False, False,
        AppKit.NSDeviceRGBColorSpace, width * 3, 24,
    )
    # bitmapData() is a writable memoryview straight into the rep's buffer.
    rep.bitmapData()[:] = bytes(rgb)
    image = AppKit.NSImage.alloc().initWithSize_((width, height))
    image.addRepresentation_(rep)
    return image
