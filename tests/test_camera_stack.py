"""Run the camera table, loopback loader and udev refresh against a fake root:
board, bound sensors, v4l2loopback and udevadm are simulated, nothing on the
host is touched."""
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
LIBEXEC = REPO / "packaging/ubuntu-gts9u-device/usr/libexec"
PROFILE_SCRIPT = LIBEXEC / "ubuntu-gts9u-camera-profile"
LOADER_SCRIPT = LIBEXEC / "ubuntu-gts9u-camera-loopback"
REFRESH_SCRIPT = LIBEXEC / "ubuntu-gts9u-camera-udev-refresh"
ENABLE_SCRIPT = LIBEXEC / "ubuntu-gts9u-camera-enable"
RELAY_UNIT = REPO / "packaging/ubuntu-gts9u-device/usr/lib/systemd/system/ubuntu-gts9u-camera-relays.service"
LUA = REPO / "packaging/ubuntu-gts9u-device/usr/share/wireplumber/main.lua.d/51-gts9u-camera-backends.lua"
UDEV_RULES = REPO / "packaging/ubuntu-gts9u-device/usr/lib/udev/rules.d/70-gts9u-camera.rules"

GTS9WIFI = b"qcom,kalama-mtp\0qcom,kalama\0qcom,mtp\0samsung,gts9wifi\0qcom,sm8550\0"
GTS9UWIFI = b"qcom,kalama-mtp\0qcom,kalama\0qcom,mtp\0samsung,gts9uwifi\0qcom,sm8550\0"
OTHER = b"qcom,sm8550-qrd\0qcom,sm8550\0"

FRONT_DT = "soc@0/cci@ac16000/i2c-bus@1/camera@20"
REAR_DT = "soc@0/cci@ac15000/i2c-bus@1/camera@21"

ULTRA_TABLE = (
    "20 GTS9U-Front-Ultra-Wide libcamera_input._base_soc_0_geniqup_8c0000_i2c_884000_camera_21 front-ultrawide\n"
    "21 GTS9U-Front-Main libcamera_input._base_soc_0_cci_ac16000_i2c-bus_1_camera_20 front-main\n"
    "22 GTS9U-Rear-Main libcamera_input._base_soc_0_cci_ac15000_i2c-bus_1_camera_21 rear-main\n"
    "23 GTS9U-Rear-Ultra-Wide libcamera_input._base_soc_0_cci_ac15000_i2c-bus_0_camera_21 rear-ultrawide\n")

# The options the removed /usr/lib/modprobe.d/ubuntu-gts9u-cameras.conf gave
# the SM-X910; its loader must still pass exactly these.
ULTRA_OPTIONS = ("v4l2loopback devices=4 video_nr=20,21,22,23 "
                 "card_label=GTS9U-Front-Ultra-Wide,GTS9U-Front-Main,GTS9U-Rear-Main,GTS9U-Rear-Ultra-Wide "
                 "exclusive_caps=1,1,1,1 max_buffers=4 max_openers=16")

# A v4l2loopback stand-in: loading creates the virtual nodes the options ask
# for, removing deletes them unless something "holds" one.
STUB_MODPROBE = r"""#!/bin/sh
echo "$*" >> "$FAKE_ROOT/modprobe.log"
sys=$FAKE_ROOT/sys
if [ "$1" = -r ]; then
	[ -e "$FAKE_ROOT/loopback-busy" ] && exit 1
	rm -rf "$sys/module/v4l2loopback" "$sys/devices/virtual/video4linux"
	exit 0
fi
[ -d "$sys/module/v4l2loopback" ] && exit 0
nrs= labels=
for arg; do
	case "$arg" in
	video_nr=*) nrs=${arg#video_nr=} ;;
	card_label=*) labels=${arg#card_label=} ;;
	esac
done
mkdir -p "$sys/module/v4l2loopback"
while [ -n "$nrs" ]; do
	nr=${nrs%%,*}; label=${labels%%,*}
	mkdir -p "$sys/devices/virtual/video4linux/video$nr"
	echo "$label" > "$sys/devices/virtual/video4linux/video$nr/name"
	[ "$nrs" = "${nrs#*,}" ] && break
	nrs=${nrs#*,}; labels=${labels#*,}
done
"""

STUB_UDEVADM = """#!/bin/sh
echo "$*" >> "$FAKE_ROOT/udevadm.log"
"""

TOOLS = ("sh", "tr", "grep", "readlink", "cat", "awk", "sort", "wc", "mktemp",
         "chmod", "mv", "rm", "install", "mkdir", "printf", "echo", "sed")


class CameraStackTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        for directory in ("proc/device-tree", "sys/bus/i2c/drivers/hi1337-gts9u",
                          "sys/firmware/devicetree/base", "sys/class/video4linux",
                          "run", "bin", "tools", "usr/libexec"):
            (self.root / directory).mkdir(parents=True, exist_ok=True)
        for tool in TOOLS:
            found = shutil.which(tool)
            if found:
                os.symlink(found, self.root / "tools" / tool)
        self.stub("modprobe", STUB_MODPROBE)
        self.stub("udevadm", STUB_UDEVADM)
        self.stub("sleep", "#!/bin/sh\nexit 0\n")
        # The loader calls the table helper by its absolute path.
        helper = self.root / "usr/libexec/ubuntu-gts9u-camera-profile"
        helper.write_text(self.rewrite(PROFILE_SCRIPT.read_text()))
        helper.chmod(0o755)
        self.board(GTS9WIFI)

    def tearDown(self):
        self.tmp.cleanup()

    def stub(self, name, body):
        path = self.root / "bin" / name
        path.write_text(body)
        path.chmod(0o755)

    def board(self, compatible):
        (self.root / "proc/device-tree/compatible").write_bytes(compatible)

    def bind(self, dt_path, i2c_name):
        node = self.root / "sys/firmware/devicetree/base" / dt_path
        node.mkdir(parents=True, exist_ok=True)
        dev = self.root / "sys/bus/i2c/drivers/hi1337-gts9u" / i2c_name
        dev.mkdir(parents=True)
        os.symlink(node, dev / "of_node")

    def loaded(self, nodes):
        """A v4l2loopback already loaded with {nr: label}."""
        (self.root / "sys/module/v4l2loopback").mkdir(parents=True, exist_ok=True)
        for nr, label in nodes.items():
            node = self.root / "sys/devices/virtual/video4linux" / ("video%d" % nr)
            node.mkdir(parents=True)
            (node / "name").write_text(label + "\n")

    def rewrite(self, text):
        prefixes = ("/proc/device-tree/compatible", "/sys/", "/run/ubuntu-gts9u",
                    "/etc/ubuntu-gts9u", "/usr/libexec/ubuntu-gts9u-camera-profile")
        pattern = re.compile("|".join(re.escape(p) for p in prefixes))
        return pattern.sub(lambda match: str(self.root) + match.group(0), text)

    def run_script(self, script):
        env = dict(os.environ, FAKE_ROOT=str(self.root),
                   PATH="%s:%s" % (self.root / "bin", self.root / "tools"))
        return subprocess.run(["/bin/sh", "-s"], input=self.rewrite(script.read_text()),
                              env=env, text=True, capture_output=True, timeout=20)

    def profile(self):
        result = self.run_script(PROFILE_SCRIPT)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def load(self, expect=0):
        result = self.run_script(LOADER_SCRIPT)
        self.assertEqual(result.returncode, expect, result.stderr)
        return result

    def modprobe_log(self):
        log = self.root / "modprobe.log"
        return log.read_text().splitlines() if log.exists() else []

    def table(self):
        return (self.root / "run/ubuntu-gts9u/camera-profile").read_text()

    # --- the table ------------------------------------------------------------
    def test_ultra_table_is_the_fixed_four(self):
        self.board(GTS9UWIFI)
        self.assertEqual(self.profile(), ULTRA_TABLE)

    def test_x710_without_bound_cameras_has_none(self):
        self.assertEqual(self.profile(), "")

    def test_x710_lists_bound_cameras_front_first(self):
        self.bind(REAR_DT, "1-0021")
        self.assertEqual(self.profile(),
                         "20 GTS9-Rear libcamera_input._base_soc_0_cci_ac15000_i2c-bus_1_camera_21 rear\n")
        self.bind(FRONT_DT, "3-0020")
        self.assertEqual(self.profile(),
                         "20 GTS9-Front libcamera_input._base_soc_0_cci_ac16000_i2c-bus_1_camera_20 front\n"
                         "21 GTS9-Rear libcamera_input._base_soc_0_cci_ac15000_i2c-bus_1_camera_21 rear\n")

    def test_x710_ignores_ultra_only_sensor_paths(self):
        self.bind("soc@0/geniqup@8c0000/i2c@884000/camera@21", "9-0021")
        self.assertEqual(self.profile(), "")

    def test_other_boards_have_no_cameras(self):
        self.board(OTHER)
        self.bind(REAR_DT, "1-0021")
        self.assertEqual(self.profile(), "")

    # --- the loader -------------------------------------------------------------
    def test_ultra_loads_with_the_old_fixed_options(self):
        self.board(GTS9UWIFI)
        self.load()
        self.assertEqual(self.modprobe_log(), [ULTRA_OPTIONS])
        self.assertEqual(self.table(), ULTRA_TABLE)

    def test_ultra_already_loaded_is_left_alone(self):
        self.board(GTS9UWIFI)
        self.loaded({20: "GTS9U-Front-Ultra-Wide", 21: "GTS9U-Front-Main",
                     22: "GTS9U-Rear-Main", 23: "GTS9U-Rear-Ultra-Wide"})
        (self.root / "loopback-busy").touch()
        self.assertIn("already match", self.load().stderr)
        self.assertEqual(self.modprobe_log(), [])

    def test_x710_without_cameras_loads_nothing(self):
        self.assertIn("no loopback nodes", self.load().stderr)
        self.assertEqual(self.modprobe_log(), [])
        self.assertEqual(self.table(), "")

    def test_x710_removes_the_phantom_ultra_nodes(self):
        self.loaded({20: "GTS9U-Front-Ultra-Wide", 21: "GTS9U-Front-Main",
                     22: "GTS9U-Rear-Main", 23: "GTS9U-Rear-Ultra-Wide"})
        self.load()
        self.assertEqual(self.modprobe_log(), ["-r v4l2loopback"])
        self.assertFalse((self.root / "sys/module/v4l2loopback").exists())

    def test_x710_busy_phantom_nodes_fail_loudly(self):
        self.loaded({20: "GTS9U-Front-Ultra-Wide", 21: "GTS9U-Front-Main"})
        (self.root / "loopback-busy").touch()
        self.assertIn("in use", self.load(expect=1).stderr)

    def test_x710_foreign_loopback_is_never_removed(self):
        self.loaded({0: "OBS Virtual Camera"})
        self.load()
        self.assertEqual(self.modprobe_log(), [])
        self.bind(REAR_DT, "1-0021")
        self.load(expect=1)
        self.assertEqual(self.modprobe_log(), [])

    def test_x710_two_cameras(self):
        self.bind(FRONT_DT, "3-0020")
        self.bind(REAR_DT, "1-0021")
        self.load()
        self.assertEqual(self.modprobe_log(), [
            "v4l2loopback devices=2 video_nr=20,21 card_label=GTS9-Front,GTS9-Rear "
            "exclusive_caps=1,1 max_buffers=4 max_openers=16"])

    def test_x710_camera_enabled_later_rebuilds_the_nodes(self):
        self.bind(REAR_DT, "1-0021")
        self.load()
        self.bind(FRONT_DT, "3-0020")
        self.load()
        self.assertEqual(self.modprobe_log(), [
            "v4l2loopback devices=1 video_nr=20 card_label=GTS9-Rear "
            "exclusive_caps=1 max_buffers=4 max_openers=16",
            "-r v4l2loopback",
            "v4l2loopback devices=2 video_nr=20,21 card_label=GTS9-Front,GTS9-Rear "
            "exclusive_caps=1,1 max_buffers=4 max_openers=16"])

    # --- the udev refresh -------------------------------------------------------
    def test_refresh_touches_only_the_table_nodes(self):
        self.bind(REAR_DT, "1-0021")
        self.load()
        (self.root / "sys/class/video4linux/video20").mkdir()
        result = self.run_script(REFRESH_SCRIPT)
        self.assertEqual(result.returncode, 0, result.stderr)
        log = (self.root / "udevadm.log").read_text().splitlines()
        self.assertEqual(len(log), 2)
        self.assertTrue(log[0].startswith("trigger --action=change "))
        self.assertTrue(log[0].endswith("/sys/class/video4linux/video20"))
        self.assertEqual(log[1], "settle")

    # --- the opt-in switch ----------------------------------------------------------
    def switch(self, state="none"):
        param = self.root / "sys/module/gts9wifi_camera/parameters/enable"
        param.parent.mkdir(parents=True, exist_ok=True)
        param.write_text(state + "\n")
        return param

    def camss(self):
        (self.root / "sys/bus/platform/drivers/qcom-camss/acb7000.isp").mkdir(parents=True)

    def enable(self, *args, expect=0):
        env = dict(os.environ, FAKE_ROOT=str(self.root),
                   PATH="%s:%s" % (self.root / "bin", self.root / "tools"))
        result = subprocess.run(["/bin/sh", "-s", *args],
                                input=self.rewrite(ENABLE_SCRIPT.read_text()),
                                env=env, text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, expect, result.stderr)
        return result

    def test_enable_does_nothing_without_a_choice(self):
        param = self.switch()
        self.assertIn("cameras stay off", self.enable().stderr)
        self.assertEqual(param.read_text(), "none\n")

    def test_enable_reads_the_choice_from_etc(self):
        param = self.switch()
        config = self.root / "etc/ubuntu-gts9u/cameras"
        config.parent.mkdir(parents=True)
        config.write_text("# tested 2026-11-08\n  rear   # front later\n")
        self.bind(REAR_DT, "1-0021")
        self.camss()
        self.assertIn("ready after 0s: rear", self.enable().stderr)
        self.assertEqual(param.read_text(), "rear\n")

    def test_enable_waits_for_every_chosen_sensor(self):
        self.switch()
        self.bind(REAR_DT, "1-0021")
        self.camss()
        self.assertIn("not ready after 20s", self.enable("both", expect=1).stderr)

    def test_enable_is_one_shot(self):
        param = self.switch("rear")
        self.assertIn("reboot to choose", self.enable("front", expect=1).stderr)
        self.assertEqual(param.read_text(), "rear\n")

    def test_enable_needs_the_kernel_switch(self):
        self.assertIn("no gts9wifi_camera switch", self.enable("rear", expect=1).stderr)

    def test_enable_ignores_other_boards(self):
        self.board(GTS9UWIFI)
        param = self.switch()
        self.assertIn("not an SM-X710", self.enable("rear").stderr)
        self.assertEqual(param.read_text(), "none\n")

    def test_enable_rejects_unknown_choices(self):
        self.switch()
        self.enable("all", expect=2)

    # --- static wiring ------------------------------------------------------------
    def test_relay_unit_needs_a_non_empty_table(self):
        unit = RELAY_UNIT.read_text()
        self.assertIn("ConditionFileNotEmpty=/run/ubuntu-gts9u/camera-profile", unit)
        self.assertIn("Wants=ubuntu-gts9u-camera-loopback.service", unit)
        self.assertRegex(unit, r"(?m)^After=.*ubuntu-gts9u-camera-loopback\.service")
        self.assertIn("RestartPreventExitStatus=78", unit)

    def test_labels_agree_across_layers(self):
        lua = LUA.read_text()
        rules = UDEV_RULES.read_text()
        self.assertRegex(lua, r'(?m)^local gts9u_board = "gts9uwifi"$')
        for label in ("GTS9-Front", "GTS9-Rear", "GTS9U-Front-Ultra-Wide",
                      "GTS9U-Front-Main", "GTS9U-Rear-Main", "GTS9U-Rear-Ultra-Wide"):
            self.assertIn('ATTR{name}=="%s"' % label, rules)
        for node in re.findall(r"libcamera_input\.\S+", PROFILE_SCRIPT.read_text()):
            self.assertIn('"%s"' % node, lua)


if __name__ == "__main__":
    unittest.main()
