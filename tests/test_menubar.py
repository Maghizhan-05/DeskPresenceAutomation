"""Menu-bar controller tests: labels, checkmark state, and action wiring.

The rumps GUI is never started. A fake supervisor stands in for all process
work, so these tests assert only the controller's rendering and call sequence.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from presence_automation import menubar  # noqa: E402


class FakeApp(object):
    """Minimal stand-in for rumps.App."""

    def __init__(self):
        self.title = ""
        self.menu = []
        self.ran = False

    def run(self):
        self.ran = True


class FakeSupervisor(object):
    def __init__(self, status="Stopped", login=False, enabled=False):
        self.status = status
        self.login = login
        self.enabled = enabled
        self.running = status != "Stopped"
        self.calls = []

    # -- supervisor API ----------------------------------------------------
    def worker_status(self):
        return self.status

    def is_worker_running(self):
        return self.running

    def start_worker(self):
        self.calls.append("start")
        self.running = True
        self.enabled = True
        self.status = "Running (Away)"
        return True

    def stop_worker(self, timeout=None):
        self.calls.append("stop")
        self.running = False
        self.enabled = False
        self.status = "Stopped"
        return True

    def is_login_item_enabled(self):
        return self.login

    def enable_login_item(self):
        self.calls.append("login_enable")
        self.login = True
        return True

    def disable_login_item(self):
        self.calls.append("login_disable")
        self.login = False
        return True

    def load_state(self):
        return {"enabled": self.enabled, "worker": None}


def build(status="Stopped", login=False, enabled=False, preview_opener=None):
    supervisor = FakeSupervisor(status=status, login=login, enabled=enabled)
    quit_calls = []
    controller = menubar.DeskPresenceMenuBar(
        app=FakeApp(),
        supervisor=supervisor,  # type: ignore[arg-type]
        quit_hook=lambda: quit_calls.append(True),
        preview_opener=preview_opener,
    )
    controller.quit_calls = quit_calls
    return controller, supervisor


class FakeWindow(object):
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class FakeImageView(object):
    def __init__(self):
        self.images = []

    def setImage_(self, image):
        self.images.append(image)


class FakeLabel(object):
    def __init__(self):
        self.text = None

    def setStringValue_(self, value):
        self.text = value


class FakePreviewController(object):
    def __init__(self, data=None, text="At desk"):
        self.data = data
        self.text = text
        self.closed = False
        self.ticks = 0

    def tick(self):
        self.ticks += 1
        return self.data, (0, 0, 0, 0), self.text

    def close(self):
        self.closed = True


def fake_preview_handle(data=None, text="At desk"):
    """Mimic show_window()'s (window, image_view, label, controller) tuple."""
    return (
        FakeWindow(),
        FakeImageView(),
        FakeLabel(),
        FakePreviewController(data=data, text=text),
    )


class MenuStructureTests(unittest.TestCase):
    def test_menu_has_status_toggle_preview_login_and_quit(self):
        controller, _ = build()
        titles = [
            item.title for item in controller.app.menu if item is not None
        ]
        self.assertEqual(
            titles,
            [
                "Status: Stopped",
                menubar.ENABLE_LABEL,
                menubar.PREVIEW_LABEL,
                menubar.LOGIN_LABEL,
                menubar.QUIT_LABEL,
            ],
        )

    def test_status_item_has_no_callback_so_it_renders_disabled(self):
        controller, _ = build()
        self.assertIsNone(controller.status_item.callback)

    def test_labels_are_text_not_emoji_or_icon_glyphs(self):
        controller, _ = build()
        for item in controller.app.menu:
            if item is None:
                continue
            self.assertTrue(
                all(ord(ch) < 0x2190 for ch in item.title),
                "menu label must be plain text: %r" % item.title,
            )


class StatusRenderingTests(unittest.TestCase):
    def test_stopped_shows_enable_with_no_checkmark(self):
        controller, _ = build(status="Stopped")
        self.assertEqual(controller.status_item.title, "Status: Stopped")
        self.assertEqual(controller.toggle_item.title, menubar.ENABLE_LABEL)
        self.assertEqual(controller.toggle_item.state, 0)
        self.assertEqual(controller.app.title, "DP off")

    def test_running_present_shows_disable_with_checkmark(self):
        controller, _ = build(status="Running (Present)")
        self.assertEqual(controller.status_item.title, "Status: Running (Present)")
        self.assertEqual(controller.toggle_item.title, menubar.DISABLE_LABEL)
        self.assertEqual(controller.toggle_item.state, 1)
        self.assertEqual(controller.app.title, "DP on")

    def test_running_away_is_rendered_distinctly_from_present(self):
        away, _ = build(status="Running (Away)")
        present, _ = build(status="Running (Present)")
        self.assertNotEqual(away.status_item.title, present.status_item.title)
        self.assertEqual(away.toggle_item.state, 1)

    def test_unresponsive_is_shown_and_not_presented_as_live_presence(self):
        controller, _ = build(status="Unresponsive")
        self.assertEqual(controller.status_item.title, "Status: Unresponsive")
        self.assertNotIn("Present", controller.status_item.title)
        self.assertEqual(controller.app.title, "DP ?")

    def test_starting_is_shown_distinctly_and_counts_as_running(self):
        controller, _ = build(status="Starting...")
        self.assertEqual(controller.status_item.title, "Status: Starting...")
        self.assertEqual(controller.app.title, "DP ...")
        self.assertEqual(controller.toggle_item.title, menubar.DISABLE_LABEL)
        self.assertEqual(controller.toggle_item.state, 1)

    def test_login_checkmark_reflects_supervisor(self):
        on, _ = build(login=True)
        off, _ = build(login=False)
        self.assertEqual(on.login_item.state, 1)
        self.assertEqual(off.login_item.state, 0)


class ActionTests(unittest.TestCase):
    def test_enable_starts_worker_and_flips_to_disable(self):
        controller, supervisor = build(status="Stopped")
        controller.on_toggle()
        self.assertEqual(supervisor.calls, ["start"])
        self.assertEqual(controller.toggle_item.title, menubar.DISABLE_LABEL)
        self.assertEqual(controller.toggle_item.state, 1)

    def test_disable_stops_worker_and_flips_to_enable(self):
        controller, supervisor = build(status="Running (Present)")
        controller.on_toggle()
        self.assertEqual(supervisor.calls, ["stop"])
        self.assertEqual(controller.toggle_item.title, menubar.ENABLE_LABEL)
        self.assertEqual(controller.toggle_item.state, 0)
        self.assertEqual(controller.status_item.title, "Status: Stopped")

    def test_toggle_is_idempotent_round_trip(self):
        controller, supervisor = build(status="Stopped")
        controller.on_toggle()
        controller.on_toggle()
        self.assertEqual(supervisor.calls, ["start", "stop"])
        self.assertEqual(controller.toggle_item.state, 0)

    def test_login_toggle_enables_then_disables_and_requeries(self):
        controller, supervisor = build(login=False)
        controller.on_toggle_login()
        self.assertEqual(supervisor.calls, ["login_enable"])
        self.assertEqual(controller.login_item.state, 1)
        controller.on_toggle_login()
        self.assertEqual(supervisor.calls, ["login_enable", "login_disable"])
        self.assertEqual(controller.login_item.state, 0)

    def test_login_toggle_does_not_touch_the_worker(self):
        controller, supervisor = build(status="Running (Present)", login=False)
        controller.on_toggle_login()
        self.assertNotIn("stop", supervisor.calls)
        self.assertNotIn("start", supervisor.calls)
        self.assertTrue(supervisor.is_worker_running())

    def test_quit_stops_a_running_worker_first(self):
        controller, supervisor = build(status="Running (Present)")
        controller.on_quit()
        self.assertEqual(supervisor.calls, ["stop"])
        self.assertFalse(supervisor.is_worker_running())
        self.assertEqual(len(controller.quit_calls), 1)  # app really exits

    def test_quit_with_no_worker_does_not_signal_anything(self):
        controller, supervisor = build(status="Stopped")
        controller.on_quit()
        self.assertEqual(supervisor.calls, [])
        self.assertEqual(len(controller.quit_calls), 1)


class AutostartTests(unittest.TestCase):
    def test_autostart_starts_worker_when_preference_is_enabled(self):
        controller, supervisor = build(status="Stopped", enabled=True)
        controller.autostart_if_enabled()
        self.assertEqual(supervisor.calls, ["start"])
        self.assertEqual(controller.toggle_item.state, 1)

    def test_autostart_does_nothing_when_preference_is_disabled(self):
        controller, supervisor = build(status="Stopped", enabled=False)
        controller.autostart_if_enabled()
        self.assertEqual(supervisor.calls, [])
        self.assertEqual(controller.toggle_item.state, 0)

    def test_autostart_does_not_double_start_a_running_worker(self):
        controller, supervisor = build(status="Running (Away)", enabled=True)
        controller.autostart_if_enabled()
        self.assertEqual(supervisor.calls, [])


class PreviewMenuTests(unittest.TestCase):
    def test_preview_is_closed_by_default(self):
        controller, _ = build(status="Running (Present)")
        self.assertEqual(controller.preview_item.state, 0)
        self.assertIsNone(controller._preview)

    def test_opening_preview_sets_checkmark_and_never_touches_worker(self):
        handle = fake_preview_handle()
        controller, supervisor = build(
            status="Running (Present)", preview_opener=lambda: handle
        )
        supervisor.calls = []
        controller.on_toggle_preview()
        self.assertEqual(controller.preview_item.state, 1)
        self.assertEqual(
            supervisor.calls, [], "preview must not start/stop the worker"
        )

    def test_closing_preview_clears_checkmark_and_leaves_worker_running(self):
        handle = fake_preview_handle()
        controller, supervisor = build(
            status="Running (Present)", preview_opener=lambda: handle
        )
        controller.on_toggle_preview()
        supervisor.calls = []
        controller.on_toggle_preview()
        self.assertEqual(controller.preview_item.state, 0)
        self.assertTrue(handle[3].closed, "controller.close() must be called")
        self.assertTrue(handle[0].closed, "window must be closed")
        self.assertEqual(supervisor.calls, [])
        self.assertTrue(supervisor.is_worker_running())

    def test_refresh_redraws_an_open_preview(self):
        handle = fake_preview_handle(data=b"frame", text="Away - closing apps in 7s")
        controller, _ = build(
            status="Running (Away)", preview_opener=lambda: handle
        )
        controller.on_toggle_preview()
        before = handle[3].ticks
        controller.refresh()
        self.assertGreater(handle[3].ticks, before, "refresh must pull a frame")
        self.assertEqual(handle[2].text, "Away - closing apps in 7s")

    def test_refresh_without_preview_does_not_tick(self):
        controller, _ = build(status="Running (Present)")
        self.assertFalse(controller.redraw_preview())

    def test_preview_open_failure_does_not_break_the_menu(self):
        def boom():
            raise RuntimeError("no window server")

        controller, _ = build(status="Running (Present)", preview_opener=boom)
        status = controller.on_toggle_preview()
        self.assertEqual(status, "Running (Present)")
        self.assertIsNone(controller._preview)
        self.assertEqual(controller.preview_item.state, 0)

    def test_quit_closes_the_preview_before_stopping_the_worker(self):
        handle = fake_preview_handle()
        controller, supervisor = build(
            status="Running (Present)", preview_opener=lambda: handle
        )
        controller.on_toggle_preview()
        controller.on_quit()
        self.assertTrue(handle[3].closed)
        self.assertTrue(handle[0].closed)
        self.assertEqual(supervisor.calls, ["stop"])
        self.assertEqual(len(controller.quit_calls), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
