"""Tab Companion's double tap to wake switch: the setting, the hardware
service that applies it to the touchscreen driver, and the Touch page."""

import ast
from glob import glob
from pathlib import Path
import os
import re
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "packaging/ubuntu-gts9u-companion"
HARDWARE = PKG / "usr/libexec/tab-companion-hardware"
WINDOW = PKG / "usr/lib/tab-companion/tab_companion/window.py"
SCHEMA = PKG / "usr/share/glib-2.0/schemas/io.github.agcarbajo.TabCompanion.gschema.xml"
DRIVER = ROOT / "kernel/drivers/samsung_fts1ba90a.c"
GOODIX_PATCH = ROOT / "kernel/patches/support-goodix-samsung-double-tap-wake.patch"
BUILD = ROOT / "scripts/build-mainline-kernel.sh"
DEVICE = ROOT / "packaging/ubuntu-gts9u-device"
RULES = DEVICE / "usr/lib/udev/rules.d/71-gts9u-touchscreen.rules"
DESKTOP_USER = DEVICE / "usr/libexec/ubuntu-gts9u-desktop-user"
# Both touch drivers, as the kernel names them (the Goodix one is mainline's
# goodix_berlin_i2c.c).
TOUCH_DRIVERS = ("samsung_fts1ba90a", "goodix-berlin-i2c")


def read_text(path, fallback=""):
    try:
        return Path(path).read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        return fallback


class FakeSettings:
    def __init__(self, user_value=None):
        self.user_value = user_value

    def get_user_value(self, key):
        assert key == "double-tap-to-wake"
        return self.user_value

    def get_boolean(self, key):
        assert key == "double-tap-to-wake"
        return bool(self.user_value)

    def set_boolean(self, key, value):
        assert key == "double-tap-to-wake"
        self.user_value = value


class FakeService:
    def __init__(self, settings, owner=True):
        self.settings = settings
        self.touch_option_errors = set()
        self.touch_setting_adopted = False
        self.owner = owner

    def _owns_devices(self):
        return self.owner


def load_apply(attribute_glob):
    """The service's _apply_touch_options, run against a fake sysfs."""
    source = HARDWARE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    func = next(n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == "_apply_touch_options")
    namespace = {"Path": Path, "glob": glob, "read_text": read_text,
                 "DOUBLE_TAP_ATTRIBUTE": attribute_glob}
    exec(compile(ast.Module(body=[func], type_ignores=[]), str(HARDWARE), "exec"), namespace)
    return namespace["_apply_touch_options"]


class HardwareServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        device = Path(self.tmp.name) / "9-0049"
        device.mkdir()
        self.attribute = device / "double_tap_to_wake"
        self.apply = load_apply(str(Path(self.tmp.name) / "*/double_tap_to_wake"))

    def run_service(self, settings, start="0", rounds=2):
        self.attribute.write_text(start + "\n")
        service = FakeService(settings)
        for _ in range(rounds):
            self.apply(service)
        return read_text(self.attribute)

    def test_off_by_default(self):
        self.assertEqual(self.run_service(FakeSettings(None), start="0"), "0")

    def test_switch_on_and_off(self):
        self.assertEqual(self.run_service(FakeSettings(True), start="0"), "1")
        self.assertEqual(self.run_service(FakeSettings(False), start="1"), "0")

    def test_adopts_an_existing_udev_rule_once(self):
        # Armed by a udev rule before the switch existed, and the switch never
        # used: kept on, and the setting records it.
        settings = FakeSettings(None)
        self.assertEqual(self.run_service(settings, start="1"), "1")
        self.assertIs(settings.user_value, True)

    def test_the_switch_wins_over_a_udev_rule(self):
        self.assertEqual(self.run_service(FakeSettings(False), start="1"), "0")

    def test_no_touchscreen_is_fine(self):
        apply = load_apply(str(Path(self.tmp.name) / "nothing/*/double_tap_to_wake"))
        apply(FakeService(FakeSettings(True)))

    def test_only_the_desktop_users_service_applies_it(self):
        # Another account's service must not fight over the one attribute,
        # nor adopt the owner's choice into its own settings.
        settings = FakeSettings(None)
        self.attribute.write_text("1\n")
        other = FakeService(settings, owner=False)
        self.apply(other)
        self.assertEqual(read_text(self.attribute), "1")
        self.assertIsNone(settings.user_value)
        self.apply(FakeService(FakeSettings(False), owner=False))
        self.assertEqual(read_text(self.attribute), "1")

    def test_attribute_appearing_later_is_still_adopted(self):
        # A service that starts before the touchscreen binds adopts once the
        # attribute is there, not on the first (empty) sample.
        apply = load_apply(str(Path(self.tmp.name) / "late/*/double_tap_to_wake"))
        settings = FakeSettings(None)
        service = FakeService(settings)
        apply(service)
        late = Path(self.tmp.name) / "late/9-0049"
        late.mkdir(parents=True)
        (late / "double_tap_to_wake").write_text("1\n")
        apply(service)
        self.assertIs(settings.user_value, True)

    def test_refused_write_is_logged_once_and_retried(self):
        settings = FakeSettings(True)
        service = FakeService(settings)
        self.attribute.write_text("0\n")
        self.attribute.chmod(0o444)
        if os.access(self.attribute, os.W_OK):
            self.skipTest("running as root")
        with mock.patch("builtins.print") as printed:
            self.apply(service)
            self.apply(service)
        self.assertEqual(printed.call_count, 1)
        self.assertIn(self.attribute, service.touch_option_errors)
        self.attribute.chmod(0o644)
        self.apply(service)
        self.assertEqual(read_text(self.attribute), "1")
        self.assertNotIn(self.attribute, service.touch_option_errors)

    def test_owner_check_guards_both_option_kinds(self):
        source = HARDWARE.read_text(encoding="utf-8")
        self.assertIn('DESKTOP_USER_FILE = "/run/ubuntu-gts9u/desktop-user"', source)
        pen = source[source.index("    def _apply_pen_options(self):"):]
        self.assertIn("if self.wacom is None or not self._owns_devices():", pen[:200])
        touch = source[source.index("    def _apply_touch_options(self):"):]
        self.assertIn("if not paths or not self._owns_devices():", touch[:900])

    def test_called_every_sample(self):
        source = HARDWARE.read_text(encoding="utf-8")
        sample = source[source.index("    def sample(self):"):]
        self.assertIn("self._apply_touch_options()", sample[:400])
        self.assertIn('DOUBLE_TAP_ATTRIBUTE = "/sys/bus/i2c/devices/*/double_tap_to_wake"', source)


class SettingsAndWindowTests(unittest.TestCase):
    def test_schema_default_off(self):
        schema = SCHEMA.read_text(encoding="utf-8")
        self.assertRegex(schema, r'<key name="double-tap-to-wake" type="b"><default>false</default></key>')

    def test_touch_page_only_where_supported(self):
        source = WINDOW.read_text(encoding="utf-8")
        tree = ast.parse(source)
        consts = {t.id: n.value.value for n in ast.walk(tree) if isinstance(n, ast.Assign)
                  for t in n.targets if isinstance(t, ast.Name) and isinstance(n.value, ast.Constant)}
        self.assertEqual(consts["DOUBLE_TAP_ATTRIBUTE"], "/sys/bus/i2c/devices/*/double_tap_to_wake")
        self.assertRegex(source, r"if glob\(DOUBLE_TAP_ATTRIBUTE\):\n\s+self\.view_stack\.add_titled_with_icon\(\n\s+self\._touch_page\(\)")
        page = source[source.index("    def _touch_page(self):"):source.index("    def _haptics_page(self):")]
        self.assertIn('"double-tap-to-wake", double_tap, "active"', page)

    def test_driver_offers_the_attribute(self):
        driver = DRIVER.read_text(encoding="utf-8")
        self.assertIn("static DEVICE_ATTR_RW(double_tap_to_wake);", driver)
        self.assertIn("&dev_attr_double_tap_to_wake.attr,", driver)


class GoodixDoubleTapTests(unittest.TestCase):
    """The SM-X910's Goodix patch keeps the SM-X710 driver's contract."""

    def setUp(self):
        lines = GOODIX_PATCH.read_text(encoding="utf-8").splitlines()
        self.added = "\n".join(line[1:] for line in lines
                               if line.startswith("+") and not line.startswith("+++"))

    def test_patch_offers_the_attribute(self):
        self.assertIn("static DEVICE_ATTR_RW(double_tap_to_wake);", self.added)
        self.assertIn("&dev_attr_double_tap_to_wake.attr,", self.added)
        self.assertIn('return sysfs_emit(buf, "%d\\n", READ_ONCE(cd->double_tap));', self.added)
        self.assertIn("error = kstrtobool(buf, &enable);", self.added)

    def test_off_by_default(self):
        self.assertNotRegex(self.added, r"cd->double_tap\s*=\s*true")
        self.assertNotRegex(self.added, r"WRITE_ONCE\(cd->double_tap, true\)")

    def test_wakeup_capable_without_the_dt(self):
        # The pinned DTB has no wakeup-source; power/wakeup must still exist
        # and default to enabled, and the IRQ is armed only with the gesture.
        self.assertIn("devm_device_init_wakeup(dev)", self.added)
        self.assertNotIn("dev_pm_set_wake_irq", self.added)
        self.assertIn("device_may_wakeup(dev)", self.added)
        self.assertIn("enable_irq_wake(cd->irq)", self.added)
        self.assertIn("disable_irq_wake(cd->irq)", self.added)

    def test_wake_key_on_its_own_device(self):
        self.assertIn("input_set_capability(wake_input, EV_KEY, KEY_WAKEUP);", self.added)
        for value in (1, 0):
            self.assertIn(f"input_report_key(cd->wake_input, KEY_WAKEUP, {value});", self.added)
        self.assertIn("pm_wakeup_dev_event(cd->dev, GOODIX_BERLIN_WAKE_EVENT_MS, true);", self.added)
        self.assertIn('dev_info(cd->dev, "double tap to wake\\n");', self.added)

    def test_samsung_protocol(self):
        # sec_input.h SEC_TS_MODE_SPONGE_DOUBLETAP_TO_WAKEUP, goodix_brl_hw.c
        # brl_gesture(), goodix_ts_core.h gesture code and id.
        for name, value in (("SPONGE_DOUBLE_TAP_BIT", r"BIT\(5\)"),
                            ("SPONGE_UTC", "0x10"),
                            ("CMD_GESTURE_MODE", "0xa6"),
                            ("CMD_COORD_MODE", "0xa7"),
                            ("SAMSUNG_DOUBLE_TAP_TYPE", "1"),
                            ("SAMSUNG_GESTURE_ID_WAKEUP", "1")):
            self.assertRegex(self.added, rf"#define GOODIX_BERLIN_{name}\s+{value}\n")

    def test_build_applies_it_after_the_fod_patches(self):
        build = BUILD.read_text(encoding="utf-8")
        block = build[build.index('if [ "$fingerprint_touch" = 1 ]; then'):]
        block = block[:block.index("\nfi\n") + 1]
        patch = "support-goodix-samsung-double-tap-wake.patch"
        self.assertIn(f"\t\t{patch}\n", block)
        for earlier in ("support-goodix-samsung-fod.patch",
                        "cleanup-goodix-fod-on-suspend.patch",
                        "retain-goodix-fod-contact-until-release.patch"):
            self.assertLess(block.index(earlier), block.index(patch))
        # The apply_unless marker must be something the patch adds.
        marker = re.search(r"apply_unless '([^']+)' \\\n\t\tdrivers/input/touchscreen/"
                           r"goodix_berlin_core\.c \\\n\t\t" + re.escape(patch), block).group(1)
        self.assertIn(marker, self.added)


class TouchPermissionTests(unittest.TestCase):
    def test_udev_rule_covers_both_drivers(self):
        rules = [line for line in RULES.read_text(encoding="utf-8").splitlines()
                 if line and not line.startswith("#")]
        self.assertEqual(len(rules), 1)
        match = re.search(r'DRIVER=="([^"]+)"', rules[0])
        self.assertEqual(tuple(match.group(1).split("|")), TOUCH_DRIVERS)
        self.assertIn('RUN+="/usr/libexec/ubuntu-gts9u-touch-permissions %p"', rules[0])
        # power/wakeup stays with root.
        self.assertNotIn("wakeup", rules[0])

    def test_desktop_user_gets_both_switches(self):
        source = DESKTOP_USER.read_text(encoding="utf-8")
        for driver in TOUCH_DRIVERS:
            self.assertIn(f"/sys/bus/i2c/drivers/{driver}/*/double_tap_to_wake", source)


if __name__ == "__main__":
    unittest.main()
