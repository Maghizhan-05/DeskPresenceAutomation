"""macOS menu-bar controller for the presence automation.

This process never touches the camera -- it only supervises the worker
subprocess, which is the single camera client (one TCC grant, not two).

UI conventions: text labels with native checkmark state (`item.state`), never
emoji or icon glyphs as structural controls, so Enable/Launch-at-Login read
unambiguously and on/off state is not conveyed by colour alone. The status line
is a disabled (non-clickable) label.
"""

import sys

try:
    import rumps
except ImportError:  # pragma: no cover - exercised only without the GUI dep
    rumps = None

from . import config, worker_supervisor

TITLE = "Desk Presence"
# Status-item title: short, text-only, no emoji (see module docstring).
SHORT_TITLES = {
    "Stopped": "DP off",
    "Unresponsive": "DP ?",
    "Starting...": "DP ...",
}
DEFAULT_SHORT_TITLE = "DP on"

STATUS_PREFIX = "Status: "
ENABLE_LABEL = "Enable Automation"
DISABLE_LABEL = "Disable Automation"
PREVIEW_LABEL = "Show Camera Preview"
LOGIN_LABEL = "Launch at Login"
QUIT_LABEL = "Quit"


def _short_title(status):
    return SHORT_TITLES.get(status, DEFAULT_SHORT_TITLE)


class DeskPresenceMenuBar(object):
    """Thin controller: all process logic lives in worker_supervisor.

    Written as a plain class around a rumps.App so the menu logic is testable
    without a GUI event loop.
    """

    def __init__(self, app=None, supervisor=worker_supervisor, quit_hook=None,
                 preview_opener=None):
        self.supervisor = supervisor
        self._injected_app = app is not None
        # Injectable so tests never open a real window server connection.
        self._preview_opener = preview_opener
        self._preview = None
        # Only a real rumps.App may terminate the GUI event loop; tests inject
        # a fake app and a no-op hook so Quit never calls NSApplication.
        if quit_hook is not None:
            self._quit_hook = quit_hook
        elif self._injected_app:
            self._quit_hook = lambda: None
        else:
            self._quit_hook = getattr(rumps, "quit_application", lambda: None)
        if app is not None:
            self.app = app
        else:
            if rumps is None:
                raise RuntimeError(
                    "rumps is not installed: pip install -r requirements.txt"
                )
            self.app = rumps.App(  # type: ignore[arg-type]
                TITLE, title=DEFAULT_SHORT_TITLE, quit_button=None
            )
        self._build_menu()
        self.refresh()

    # -- menu construction -------------------------------------------------
    def _item(self, title, callback=None):
        if rumps is None:
            return _PlainItem(title, callback)
        return rumps.MenuItem(title, callback=callback)

    def _build_menu(self):
        self.status_item = self._item(STATUS_PREFIX + "...")  # no callback = greyed
        self.toggle_item = self._item(ENABLE_LABEL, callback=self.on_toggle)
        self.preview_item = self._item(PREVIEW_LABEL, callback=self.on_toggle_preview)
        self.login_item = self._item(LOGIN_LABEL, callback=self.on_toggle_login)
        self.quit_item = self._item(QUIT_LABEL, callback=self.on_quit)
        self.app.menu = [
            self.status_item,
            None,
            self.toggle_item,
            self.preview_item,
            self.login_item,
            None,
            self.quit_item,
        ]

    # -- state rendering ---------------------------------------------------
    def refresh(self, _timer=None):
        status = self.supervisor.worker_status()
        running = status != "Stopped"

        self.status_item.title = STATUS_PREFIX + status
        self.toggle_item.title = DISABLE_LABEL if running else ENABLE_LABEL
        self.toggle_item.state = 1 if running else 0
        self.login_item.state = 1 if self.supervisor.is_login_item_enabled() else 0
        # Preview is only meaningful while a worker is producing frames.
        self.preview_item.state = 1 if self._preview is not None else 0
        self.app.title = _short_title(status)
        self.redraw_preview()
        return status

    def redraw_preview(self):
        """Pull one frame into the open preview window, if any. No-op if closed."""
        handle = self._preview
        if not isinstance(handle, tuple) or len(handle) < 4:
            return False
        _window, image_view, label, controller = handle[:4]
        try:
            data, box, text = controller.tick()
            label.setStringValue_(text)
            if data is not None:
                from .preview_window import frame_to_nsimage

                image_view.setImage_(frame_to_nsimage(data))
            return True
        except Exception:
            return False

    # -- actions -----------------------------------------------------------
    def on_toggle(self, _sender=None):
        if self.supervisor.is_worker_running():
            # Disable: worker process is terminated (SIGTERM -> SIGKILL), which
            # is what releases the camera device back to the OS.
            self.supervisor.stop_worker()
        else:
            self.supervisor.start_worker()
        return self.refresh()

    def on_toggle_login(self, _sender=None):
        if self.supervisor.is_login_item_enabled():
            self.supervisor.disable_login_item()
        else:
            self.supervisor.enable_login_item()
        return self.refresh()

    def on_toggle_preview(self, _sender=None):
        """Open/close the opt-in preview. Never starts or stops the worker."""
        if self._preview is not None:
            self._close_preview()
            return self.refresh()
        opener = self._preview_opener
        if opener is None:
            from .preview_window import show_window

            opener = show_window
        try:
            self._preview = opener()
        except Exception:
            # A preview failure must never take the menu down.
            self._preview = None
        return self.refresh()

    def _close_preview(self):
        handle = self._preview
        self._preview = None
        if handle is None:
            return
        # show_window returns (window, image_view, label, controller).
        controller = handle[3] if isinstance(handle, tuple) and len(handle) > 3 else None
        window = handle[0] if isinstance(handle, tuple) else handle
        try:
            if controller is not None:
                controller.close()
        except Exception:
            pass
        try:
            if hasattr(window, "close"):
                window.close()
        except Exception:
            pass

    def on_quit(self, _sender=None):
        # Never leave an orphaned worker holding the camera after Quit.
        self._close_preview()
        if self.supervisor.is_worker_running():
            self.supervisor.stop_worker()
        self._quit_hook()
        return "quit"

    # -- lifecycle ---------------------------------------------------------
    def autostart_if_enabled(self):
        """Honour the persisted preference on login."""
        state = self.supervisor.load_state()
        if state["enabled"] and not self.supervisor.is_worker_running():
            self.supervisor.start_worker()
        return self.refresh()

    def run(self):
        self.autostart_if_enabled()
        timer = rumps.Timer(self.refresh, config.SAMPLE_INTERVAL_SECONDS)
        timer.start()
        self.app.run()


class _PlainItem(object):
    """Stand-in menu item used when rumps is unavailable (tests/headless)."""

    def __init__(self, title, callback=None):
        self.title = title
        self.callback = callback
        self.state = 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if rumps is None:
        sys.stderr.write(
            "rumps is required for the menu bar: "
            "./.venv/bin/pip install -r requirements.txt\n"
        )
        return 1
    DeskPresenceMenuBar().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
