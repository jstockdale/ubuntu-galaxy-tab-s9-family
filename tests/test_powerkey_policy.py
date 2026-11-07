"""Drive ubuntu-gts9u-powerkey through press sequences against a simulated
logind, panel and kernel: no input device, no bus and no root needed."""
import importlib.machinery
import importlib.util
import os
import pathlib
import pwd
import struct
import sys
import tempfile
import types
import unittest

HELPER = pathlib.Path(__file__).resolve().parents[1] / (
    "packaging/ubuntu-gts9u-device/usr/libexec/ubuntu-gts9u-powerkey"
)

EV_KEY = 1
KEY_POWER = 116
PWR_IRQ = 21


def load_helper():
    # The helper has no .py suffix; keep its bytecode out of the package tree.
    sys.dont_write_bytecode = True
    loader = importlib.machinery.SourceFileLoader("ubuntu_gts9u_powerkey", str(HELPER))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class Done(Exception):
    """The scripted presses have all been delivered."""


class Tablet:
    """What the helper sees: clocks, logind, GNOME, the panel and the kernel.

    The script is a list of steps.  ("wait", s) lets time pass while awake,
    ("event", v) delivers KEY_POWER with value v, ("do", f) changes the world.
    """

    def __init__(self, module, can_suspend):
        self.module = module
        self.mono = 1000.0
        self.boot = 1000.0
        self.can = can_suspend
        self.preparing = False
        self.screen_off = False
        self.locked = False
        self.cycles = 1
        self.irq = None
        self.suspend_ok = True
        self.on_suspend = None      # what follows logind accepting a suspend
        self.steps = []
        self.calls = []
        self.logs = []
        self.power_save = []
        self.cancels = 0
        self.long_presses = 0
        self.pending_event = b""

    def press(self, after=1.0, hold=0.1):
        self.steps += [("wait", after), ("event", 1), ("wait", hold), ("event", 0)]

    def do(self, action):
        self.steps.append(("do", action))

    def advance(self, seconds):
        self.mono += seconds
        self.boot += seconds

    def sleep(self, seconds, irq):
        """A real suspend: CLOCK_BOOTTIME runs on, CLOCK_MONOTONIC does not."""
        self.boot += seconds
        self.cycles += 1
        self.irq = irq

    def suspends(self):
        return sum(1 for argv in self.calls if argv[:2] == ["systemctl", "suspend"])

    def locks(self):
        return sum(1 for argv in self.calls if argv[:2] == ["loginctl", "lock-sessions"])

    def run(self):
        tablet = self
        module = self.module

        module.time = types.SimpleNamespace(
            CLOCK_BOOTTIME=7,
            monotonic=lambda: tablet.mono,
            clock_gettime=lambda clock: tablet.boot,
            sleep=tablet.advance,
        )

        class Device:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self, size):
                return tablet.pending_event

        def fake_select(rlist, wlist, xlist, timeout=None):
            while True:
                if not tablet.steps:
                    raise Done()
                kind, arg = tablet.steps[0]
                if kind == "do":
                    tablet.steps.pop(0)
                    arg(tablet)
                elif kind == "wait":
                    if timeout is not None and arg > timeout:
                        tablet.steps[0] = ("wait", arg - timeout)
                        tablet.advance(timeout)
                        return [], [], []
                    tablet.steps.pop(0)
                    tablet.advance(arg)
                else:
                    tablet.steps.pop(0)
                    tablet.pending_event = struct.pack(
                        module.EVENT_FORMAT, 0, 0, EV_KEY, KEY_POWER, arg)
                    return rlist, [], []

        def fake_run(argv, timeout=5, **kwargs):
            tablet.calls.append(argv)
            if argv[:2] == ["loginctl", "lock-sessions"]:
                if not tablet.locked:
                    # GNOME blanks a session itself when it locks it.
                    tablet.locked = True
                    tablet.screen_off = True
                return types.SimpleNamespace(returncode=0, stdout="", stderr="")
            if argv[:2] == ["systemctl", "suspend"]:
                if not tablet.suspend_ok:
                    return types.SimpleNamespace(
                        returncode=1, stdout="",
                        stderr="Call to Suspend failed: Access denied")
                if tablet.on_suspend:
                    tablet.on_suspend(tablet)
                return types.SimpleNamespace(returncode=0, stdout="", stderr="")
            raise AssertionError("unexpected command %r" % (argv,))

        def fake_set_power_save(mode, front=None):
            tablet.power_save.append(mode)
            tablet.screen_off = mode == module.POWER_SAVE_OFF
            return True

        def fake_cancel():
            tablet.cancels += 1

        def fake_long_press():
            tablet.long_presses += 1

        module.select = types.SimpleNamespace(select=fake_select)
        module.open = lambda path, *args, **kwargs: Device()
        module.find_power_key = lambda: "/dev/input/event0"
        module.power_key_irq = lambda: PWR_IRQ
        module.wake_irq = lambda: tablet.irq
        module.suspend_cycles = lambda: tablet.cycles
        module.can_suspend = lambda: tablet.can
        module.preparing_for_sleep = lambda: tablet.preparing
        module.screen_is_off = lambda: tablet.screen_off
        module.front_session = lambda: ("2", "1000", "owner", "user", "wayland")
        module.session_locked = lambda session: tablet.locked
        module.set_power_save = fake_set_power_save
        module.run = fake_run
        module.cancel_pending_suspend = fake_cancel
        module.long_press = fake_long_press
        module.log = tablet.logs.append
        try:
            module.main()
        except Done:
            pass
        return self


class SuspendOffTest(unittest.TestCase):
    """CanSuspend is not "yes": the gts9wifi default, or a masked target."""

    def setUp(self):
        self.module = load_helper()

    def test_lock_does_not_swallow_the_next_press(self):
        """PM-6/PMW-3: a lock at 08:45:29 made the press at 08:56:25 vanish."""
        t = Tablet(self.module, "no")
        t.press()                       # unlocked desktop: GNOME locks and blanks
        t.do(lambda t: setattr(t, "screen_off", False))     # woken by a touch
        t.press(after=656)              # lit lock screen: turn it off again
        t.run()
        self.assertEqual(t.suspends(), 0)
        self.assertEqual(t.locks(), 2)
        self.assertEqual(t.cancels, 0)
        # Only the lock screen that was already up needed turning off by hand.
        self.assertEqual(t.power_save, [self.module.POWER_SAVE_OFF])
        self.assertTrue(t.screen_off)

    def test_press_on_dark_panel_turns_it_on_without_locking(self):
        t = Tablet(self.module, "no")
        t.locked = True
        t.screen_off = True
        t.press()
        t.run()
        self.assertEqual(t.locks(), 0)
        self.assertEqual(t.power_save, [self.module.POWER_SAVE_ON])

    def test_presses_toggle_the_panel(self):
        t = Tablet(self.module, "no")
        t.locked = True                 # lit lock screen
        t.press()
        t.press(after=5)
        t.press(after=5)
        t.run()
        off, on = self.module.POWER_SAVE_OFF, self.module.POWER_SAVE_ON
        self.assertEqual(t.power_save, [off, on, off])
        self.assertEqual(t.suspends(), 0)
        self.assertEqual(t.cancels, 0)

    def test_masked_target_is_not_a_dead_key(self):
        """Ultra B0: 'Unit suspend.target is masked', then swallowed presses."""
        t = Tablet(self.module, "no")
        t.press()                       # lock; GNOME blanks
        t.press(after=30)               # wakes the panel instead of vanishing
        t.run()
        self.assertEqual(t.suspends(), 0)
        self.assertEqual(t.locks(), 1)
        self.assertEqual(t.cancels, 0)
        self.assertEqual(t.power_save, [self.module.POWER_SAVE_ON])

    def test_unknown_answer_does_not_suspend(self):
        t = Tablet(self.module, "unknown")
        t.press()
        t.run()
        self.assertEqual(t.suspends(), 0)
        self.assertEqual(t.locks(), 1)

    def test_long_press_is_unchanged(self):
        t = Tablet(self.module, "no")
        t.press(hold=2.0)
        t.run()
        self.assertEqual(t.long_presses, 1)
        self.assertEqual(t.locks(), 0)


class SuspendOnTest(unittest.TestCase):
    """CanSuspend is "yes": the Ultra, or gts9wifi in s2idle or deep mode."""

    def setUp(self):
        self.module = load_helper()

    def test_dark_panel_still_suspends(self):
        """Unchanged: with suspend available the panel state is not consulted."""
        t = Tablet(self.module, "yes")
        t.screen_off = True
        t.press()
        t.run()
        self.assertEqual(t.suspends(), 1)
        self.assertEqual(t.power_save, [])

    def test_wake_press_after_real_sleep_is_swallowed(self):
        t = Tablet(self.module, "yes")
        t.on_suspend = lambda t: t.steps.insert(
            0, ("do", lambda t: t.sleep(3600, PWR_IRQ)))
        t.press()
        t.press(after=0.2)              # the press that woke it
        t.press(after=20)               # then a real command again
        t.run()
        self.assertEqual(t.suspends(), 2)
        self.assertTrue(any("ignoring the press that woke us" in m for m in t.logs))
        self.assertEqual(t.cancels, 0)

    def test_non_key_wake_leaves_the_next_press_a_command(self):
        t = Tablet(self.module, "yes")
        t.on_suspend = lambda t: t.steps.insert(
            0, ("do", lambda t: t.sleep(300, 140)))
        t.press()
        t.press(after=2)
        t.run()
        self.assertEqual(t.suspends(), 2)
        self.assertTrue(any("non-key wake" in m for m in t.logs))

    def test_refused_suspend_falls_back_and_next_press_is_new(self):
        """PM-6: a refused request used to leave suspended_at set."""
        t = Tablet(self.module, "yes")
        t.suspend_ok = False
        t.press()                       # refused: lock and blank
        t.do(lambda t: setattr(t, "screen_off", False))
        t.press(after=10800)            # refused again: lock and blank again
        t.run()
        self.assertEqual(t.suspends(), 2)
        self.assertEqual(t.locks(), 2)
        self.assertEqual(t.cancels, 0)

    def test_bounced_suspend_does_not_swallow_the_next_press(self):
        """PM-6: the kernel came straight back without sleeping."""
        def bounce(t):
            t.cycles += 1
            t.irq = 120                 # not the power key
        t = Tablet(self.module, "yes")
        t.on_suspend = bounce
        t.press()
        t.press(after=1800)
        t.run()
        self.assertEqual(t.suspends(), 2)
        self.assertEqual(t.cancels, 0)
        self.assertTrue(any("came straight back" in m for m in t.logs))

    def test_press_during_entry_is_swallowed(self):
        t = Tablet(self.module, "yes")
        t.on_suspend = lambda t: setattr(t, "preparing", True)
        t.press()
        t.press(after=1.5)              # logind still waiting for inhibitors
        t.run()
        self.assertEqual(t.suspends(), 1)
        self.assertEqual(t.cancels, 1)

    def test_request_that_never_ran_does_not_swallow(self):
        t = Tablet(self.module, "yes")
        t.press()                       # accepted, then dropped on the way
        t.press(after=4)                # PreparingForSleep already false
        t.run()
        self.assertEqual(t.suspends(), 2)
        self.assertEqual(t.cancels, 0)
        self.assertTrue(any("never reached the kernel" in m for m in t.logs))

    def test_unknown_preparing_falls_back_to_a_window(self):
        t = Tablet(self.module, "yes")
        t.preparing = None
        t.press()
        t.press(after=2)                # inside the window: entry
        t.press(after=60)               # well outside it: a new press
        t.run()
        self.assertEqual(t.cancels, 1)
        self.assertEqual(t.suspends(), 2)

    def test_no_second_request_while_one_is_under_way(self):
        t = Tablet(self.module, "yes")
        t.preparing = True              # GNOME's menu asked a moment ago
        t.press()
        t.run()
        self.assertEqual(t.suspends(), 0)
        self.assertEqual(t.locks(), 0)

    def test_wake_press_after_someone_elses_sleep_is_swallowed(self):
        t = Tablet(self.module, "yes")
        t.do(lambda t: t.sleep(600, PWR_IRQ))   # GNOME's idle suspend
        t.press(after=0.1)
        t.run()
        self.assertEqual(t.suspends(), 0)


class SessionBusTest(unittest.TestCase):
    """The panel is switched in the graphical session in front, as its owner."""

    def setUp(self):
        self.module = load_helper()
        self.calls = []
        self.me = pwd.getpwuid(os.getuid())

        def fake_run(argv, timeout=5, **kwargs):
            self.calls.append((argv, kwargs))
            return types.SimpleNamespace(returncode=0, stdout="()\n", stderr="")

        self.module.run = fake_run
        self.module.log = lambda message: None

    def front(self, *session):
        self.module.front_session = lambda: session

    def test_power_save_is_set_as_the_owner_on_their_bus(self):
        self.front("2", str(self.me.pw_uid), self.me.pw_name, "user", "wayland")
        self.assertTrue(self.module.set_power_save(self.module.POWER_SAVE_OFF))
        argv, kwargs = self.calls[0]
        self.assertEqual(argv[:3], ["gdbus", "call", "--session"])
        self.assertEqual(argv[-3:], ["'org.gnome.Mutter.DisplayConfig'",
                                     "'PowerSaveMode'", "<int32 3>"])
        self.assertEqual(kwargs["user"], self.me.pw_uid)
        self.assertEqual(kwargs["group"], self.me.pw_gid)
        self.assertEqual(kwargs["extra_groups"], [])
        self.assertEqual(kwargs["env"]["DBUS_SESSION_BUS_ADDRESS"],
                         "unix:path=/run/user/%d/bus" % self.me.pw_uid)

    def test_greeter_and_text_console_are_left_alone(self):
        self.front("c1", "108", "gdm", "greeter", "wayland")
        self.assertFalse(self.module.set_power_save(self.module.POWER_SAVE_OFF))
        self.front("3", str(self.me.pw_uid), self.me.pw_name, "user", "tty")
        self.assertFalse(self.module.set_power_save(self.module.POWER_SAVE_OFF))
        self.assertEqual(self.calls, [])

    def test_a_name_that_does_not_match_the_uid_is_refused(self):
        self.front("2", "4000000", self.me.pw_name, "user", "wayland")
        self.assertFalse(self.module.set_power_save(self.module.POWER_SAVE_ON))
        self.assertEqual(self.calls, [])


class ScreenStateTest(unittest.TestCase):
    def test_only_connected_enabled_lit_outputs_count(self):
        module = load_helper()
        real_glob = module.glob.glob
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)

            def connector(name, status, enabled, dpms):
                path = root / name
                path.mkdir(exist_ok=True)
                (path / "status").write_text(status + "\n")
                (path / "enabled").write_text(enabled + "\n")
                (path / "dpms").write_text(dpms + "\n")

            module.glob = types.SimpleNamespace(
                glob=lambda pattern: real_glob(str(root / "card*-*")))
            connector("card1-DSI-1", "connected", "disabled", "Off")
            connector("card1-DP-1", "disconnected", "disabled", "On")
            connector("card1-Writeback-1", "unknown", "disabled", "On")
            self.assertIs(module.screen_is_off(), True)
            connector("card1-DSI-1", "connected", "enabled", "On")
            self.assertIs(module.screen_is_off(), False)
            # Docked with the cover closed: the panel is off, the monitor is not.
            connector("card1-DSI-1", "connected", "disabled", "Off")
            connector("card1-DP-1", "connected", "enabled", "On")
            self.assertIs(module.screen_is_off(), False)
            module.glob = types.SimpleNamespace(glob=lambda pattern: [])
            self.assertIsNone(module.screen_is_off())


if __name__ == "__main__":
    unittest.main()
