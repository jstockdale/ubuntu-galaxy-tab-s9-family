"""The device package's cups-browsed drop-in keeps the printer browser, which
waits for network-online.target, off multi-user.target's boot path."""
import configparser
import pathlib
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
DROPIN = (REPO / "packaging/ubuntu-gts9u-device/usr/lib/systemd/system"
          / "cups-browsed.service.d/50-gts9u-off-the-boot-path.conf")


def unit_section(path):
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str
    parser.read_string(path.read_text())
    return {key: value.split() for key, value in parser["Unit"].items()}


class CupsBrowsedBootPath(unittest.TestCase):
    def setUp(self):
        self.unit = unit_section(DROPIN)

    def test_targets_do_not_wait_for_it(self):
        # A target adds After= on what it wants unless that unit has
        # DefaultDependencies=no.
        self.assertEqual(self.unit["DefaultDependencies"], ["no"])

    def test_the_other_service_defaults_are_restored(self):
        self.assertEqual(self.unit["Requires"], ["sysinit.target"])
        self.assertEqual(set(self.unit["After"]), {"sysinit.target", "basic.target"})
        self.assertEqual(self.unit["Conflicts"], ["shutdown.target"])
        self.assertEqual(self.unit["Before"], ["shutdown.target"])

    def test_it_still_waits_for_the_network_itself(self):
        # The stock unit's Wants=/After=network-online.target must stay: the
        # drop-in only moves it off the boot path.
        self.assertNotIn("network-online.target", self.unit.get("After", []))
        self.assertNotIn("Wants", self.unit)


if __name__ == "__main__":
    unittest.main()
