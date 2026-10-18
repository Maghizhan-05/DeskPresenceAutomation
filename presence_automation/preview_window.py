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


def clamp_box(box, width=None, height=None):
    """Validate/clamp a face box to the preview bounds.

    Returns None when the box is absent, malformed, or has no area, so the
    renderer never draws garbage coordinates. Pure -- unit-testable.
    """
    width = width or config.PREVIEW_WIDTH
    height = height or config.PREVIEW_HEIGHT
    if not box or len(box) != 4:
        return None
    try:
        x, y, w, h = (int(v) for v in box)
    except (TypeError, ValueError):
        return None
    if w <= 0 or h <= 0:
        return None
    # Clip to the frame; drop it entirely if it lies fully outside.
    x0 = max(0, min(x, width))
    y0 = max(0, min(y, height))
    x1 = max(0, min(x + w, width))
    y1 = max(0, min(y + h, height))
    if x1 <= x0 or y1 <= y0:
        return None
    return (x0, y0, x1 - x0, y1 - y0)


def draw_box(data, box, width=None, height=None, thickness=2):
    """Burn a green rectangle into a copy of the BGR bytes. In memory only.

    Returns the original bytes untouched when the box is invalid.
    """
    width = width or config.PREVIEW_WIDTH
    height = height or config.PREVIEW_HEIGHT
    clamped = clamp_box(box, width, height)
    if clamped is None:
        return data
    x, y, w, h = clamped
    buf = bytearray(data)
    stride = width * 3

    def _row(py, px0, px1):
        for px in range(px0, px1):
            off = py * stride + px * 3
            if 0 <= off <= len(buf) - 3:
                buf[off] = 0        # B
                buf[off + 1] = 255  # G
                buf[off + 2] = 0    # R

    for t in range(thickness):
        top, bottom = y + t, y + h - 1 - t
        if 0 <= top < height:
            _row(top, x, x + w)
        if 0 <= bottom < height:
            _row(bottom, x, x + w)
    for py in range(y, y + h):
        if not (0 <= py < height):
            continue
        for t in range(thickness):
            for px in (x + t, x + w - 1 - t):
                if 0 <= px < width:
                    off = py * stride + px * 3
                    if 0 <= off <= len(buf) - 3:
                        buf[off] = 0
                        buf[off + 1] = 255
                        buf[off + 2] = 0
    return bytes(buf)


# --- AppKit window (lazy, never imported during unit tests) ------------------
def show_window(controller=None, on_close=None):  # pragma: no cover - requires a window server
    """Open the preview NSWindow. Returns (window, image_view, label, controller).

    `on_close` is invoked when the user clicks the window's red close button,
    so closing the window disables frame publishing exactly like the menu
    toggle does.
    """
    import AppKit
    import objc
    from Foundation import NSMakeRect, NSObject

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

    class _CloseDelegate(NSObject):
        def windowWillClose_(self, _notification):
            # Red close button: stop publishing and let the menu clear state.
            try:
                controller.close()
            except Exception:
                pass
            if on_close is not None:
                try:
                    on_close()
                except Exception:
                    pass

    delegate = _CloseDelegate.alloc().init()
    window.setDelegate_(delegate)
    # The delegate is a weak reference on NSWindow: keep it alive by hanging it
    # off the window object, or it is collected and the callback never fires.
    objc.setAssociatedObject(
        window, b"dp_close_delegate", delegate,
        objc.OBJC_ASSOCIATION_RETAIN,
    )

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
