"""The S Pen test sheet, the loopback helper that serves it, and the side
button split as the driver, the hardware service and the settings share it."""

import ast
import http.client
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import types
import unittest
from html.parser import HTMLParser
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "packaging/ubuntu-gts9u-companion"
PAGE = PKG / "usr/share/tab-companion/spen-test.html"
HELPER = PKG / "usr/libexec/tab-companion-spen-test"
WINDOW = PKG / "usr/lib/tab-companion/tab_companion/window.py"
HARDWARE = PKG / "usr/libexec/tab-companion-hardware"
PERMISSIONS = PKG / "usr/libexec/tab-companion-spen-permissions"
SCHEMA = PKG / "usr/share/glib-2.0/schemas/io.github.agcarbajo.TabCompanion.gschema.xml"
DRIVER = ROOT / "kernel/drivers/samsung_wacom_w90xx.c"
SPLIT_ATTRIBUTES = ("side_button_split", "side_button_near_height", "side_button_far_height")


class Elements(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def load_helper():
    loader = importlib.machinery.SourceFileLoader("tab_companion_spen_test", str(HELPER))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def get(port, path="/", host=None, method="GET"):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.putrequest(method, path, skip_host=True)
    conn.putheader("Host", host or f"127.0.0.1:{port}")
    conn.endheaders()
    resp = conn.getresponse()
    body = resp.read()
    conn.close()
    return resp, body


def page_strings(text):
    """The STRINGS table: English -> five translations."""
    table = {}
    for m in re.finditer(r"^  '(.+?)': \[(.*)\],$", text, re.M):
        table[m.group(1)] = re.findall(r"'((?:[^'\\]|\\.)*)'", m.group(2))
    return table


def fake_sysfs(root, height="37 near", split="1", near="36", far="44", pen_height=True):
    device = Path(root) / "bus/i2c/devices/8-0056"
    device.mkdir(parents=True, exist_ok=True)
    (device / "pen_docked").write_text("1\n")
    if pen_height:
        (device / "pen_height").write_text(height + "\n")
    (device / "side_button_split").write_text(split + "\n")
    (device / "side_button_near_height").write_text(near + "\n")
    (device / "side_button_far_height").write_text(far + "\n")
    return Path(root)


class PageTests(unittest.TestCase):
    def setUp(self):
        self.text = PAGE.read_text(encoding="utf-8")
        self.parsed = Elements()
        self.parsed.feed(self.text)

    def test_self_contained(self):
        # Nothing from the network: every src/href is inline data, and the
        # only request is to the helper that served the page.
        for tag, attrs in self.parsed.tags:
            for name in ("src", "href"):
                if name in attrs:
                    self.assertTrue(attrs[name].startswith("data:"), (tag, attrs))
        self.assertNotRegex(self.text, r"https?://")
        self.assertNotRegex(self.text, r"\b(XMLHttpRequest|WebSocket|localStorage|sessionStorage|indexedDB)\b")
        self.assertEqual(re.findall(r"fetch\(([^,)]+)", self.text), ["'/pen'"])

    def test_policy_matches_the_helper(self):
        meta = [a for t, a in self.parsed.tags
                if t == "meta" and a.get("http-equiv") == "Content-Security-Policy"]
        self.assertEqual(len(meta), 1)
        self.assertEqual(meta[0]["content"], load_helper().CSP)
        self.assertIn("connect-src 'self'", meta[0]["content"])

    def test_side_button_mappings(self):
        # GNOME's default (middle click, 4) and a right click (2), which is
        # also what a split tap arrives as.
        self.assertIn("b & 4 ? 'middle click' : b & 2 ? 'right click'", self.text)
        self.assertIn("e.buttons & 32", self.text)

    def test_alignment_modes(self):
        # Free taps go to the nearest crosshair within HIT_RADIUS, which a
        # mirrored axis can pass; Check alignment measures each landing
        # against the highlighted crosshair, however far off, and starts the
        # orientation afresh.
        self.assertIn("target = targets[align.index];", self.text)
        self.assertIn("> HIT_RADIUS) return;", self.text)
        self.assertIn("guided: align.armed,", self.text)
        arm = self.text[self.text.index("function arm()"):self.text.index("function disarm()")]
        self.assertIn("seen.hits.delete(key)", arm)
        self.assertIn("ictx.clearRect", arm)

    def test_hit_dots_follow_their_crosshair(self):
        # Stored relative to the crosshair, so a relayout does not strand them.
        self.assertIn("dx: e.clientX - target.x, dy: e.clientY - target.y", self.text)
        self.assertIn("target.x + hit.dx", self.text)
        self.assertNotRegex(self.text, r"hit\.[xy]\b")

    def test_pressure_needs_to_vary(self):
        self.assertIn("seen.pressureMax - seen.pressureMin > 0.05", self.text)

    def test_translations_complete(self):
        table = page_strings(self.text)
        self.assertGreater(len(table), 30)
        placeholders = lambda s: sorted(re.findall(r"\{\w+\}", s))
        for english, values in table.items():
            self.assertEqual(len(values), 5, english)
            for value in values:
                self.assertTrue(value.strip(), english)
                self.assertEqual(placeholders(value), placeholders(english), (english, value))
        # Every literal the page translates is in the table, and so are the
        # ones it builds: the side button kinds and the orientation names.
        literals = set(re.findall(r"\bt\('((?:[^'\\]|\\.)*)'", self.text))
        literals |= {"middle click", "right click", "landscape", "portrait",
                     "landscape, turned over", "portrait, turned over", "near", "far",
                     "side button: near, tap to right-click; far, its usual action",
                     "side button split off"}
        self.assertEqual(literals - set(table), set())

    def test_spdx(self):
        self.assertIn("SPDX-License-Identifier: MIT", self.text)


class HelperTests(unittest.TestCase):
    @staticmethod
    def stop(proc):
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        proc.stdout.close()
        proc.stderr.close()

    def start(self, *extra, env=None):
        proc = subprocess.Popen(
            [sys.executable, "-I", str(HELPER), "--no-open", "--page", str(PAGE), *extra],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        self.addCleanup(self.stop, proc)
        url = proc.stdout.readline().strip()
        m = re.fullmatch(r"http://127\.0\.0\.1:(\d+)/\?lang=(\w+)", url)
        self.assertIsNotNone(m, url)
        return proc, int(m.group(1)), m.group(2)

    def test_interpreter(self):
        # build-companion-package.sh makes everything in libexec executable.
        self.assertTrue(HELPER.read_text().startswith("#!/usr/bin/python3 -I\n"))

    def test_serves_only_the_page_and_pen(self):
        proc, port, _ = self.start("--idle", "30")
        page = PAGE.read_bytes()
        for path in ("/", "/index.html", "/spen-test.html", "/?lang=fr"):
            resp, body = get(port, path)
            self.assertEqual(resp.status, 200, path)
            self.assertEqual(body, page)
            self.assertEqual(resp.getheader("Content-Type"), "text/html; charset=utf-8")
            self.assertEqual(resp.getheader("Cache-Control"), "no-store")
            self.assertTrue(resp.getheader("Content-Security-Policy").startswith("default-src 'none'"))
        # ("//spen-test.html" is folded to "/spen-test.html" by http.server
        # itself, so it is the page again, not a way out.)
        for path in ("/etc/passwd", "/../spen-test.html", "/usr/share/tab-companion/spen-test.html",
                     "/favicon.ico", "/spen-test.html/x", "/pen/x", "/sys"):
            resp, _ = get(port, path)
            self.assertEqual(resp.status, 404, path)
        resp, body = get(port, "/", method="HEAD")
        self.assertEqual((resp.status, body), (200, b""))
        self.assertEqual(resp.getheader("Content-Length"), str(len(page)))

    def test_other_host_names_refused(self):
        proc, port, _ = self.start("--idle", "30")
        for path in ("/", "/pen"):
            for host in ("localhost:%d" % port, "attacker.example:%d" % port, "127.0.0.1"):
                resp, body = get(port, path, host=host)
                self.assertEqual(resp.status, 421, (path, host))
                self.assertNotIn(b"S Pen", body)
                self.assertNotIn(b"height", body)

    def test_pen_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = fake_sysfs(tmp)
            proc, port, _ = self.start("--idle", "30", "--sysfs", str(root))
            resp, body = get(port, "/pen")
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.getheader("Content-Type"), "application/json")
            self.assertEqual(json.loads(body), {
                "available": True, "in_range": True, "height": 37, "near": True,
                "split": True, "near_height": 36, "far_height": 44,
            })
            fake_sysfs(tmp, height="out", split="0")
            state = json.loads(get(port, "/pen")[1])
            self.assertEqual((state["in_range"], state["height"], state["near"], state["split"]),
                             (False, None, None, False))
            fake_sysfs(tmp, height="52 far")
            state = json.loads(get(port, "/pen")[1])
            self.assertEqual((state["height"], state["near"]), (52, False))

    def test_pen_state_unavailable(self):
        helper = load_helper()
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(helper.pen_state(Path(tmp)), {"available": False})
            # A kernel from before pen_height (fix4) has the device but not
            # the file.
            root = fake_sysfs(tmp, pen_height=False)
            self.assertEqual(helper.pen_state(root), {"available": False})

    def test_language(self):
        helper = load_helper()
        cases = [({"LANG": "fr_FR.UTF-8"}, "fr"), ({"LANGUAGE": "de:en", "LANG": "es_ES.UTF-8"}, "de"),
                 ({"LC_ALL": "pt_PT.UTF-8"}, "pt"), ({"LANG": "ja_JP.UTF-8"}, "en"), ({}, "en"),
                 ({"LC_MESSAGES": "it-IT"}, "it")]
        for env, expected in cases:
            with mock.patch.dict(os.environ, env, clear=True):
                self.assertEqual(helper.language(), expected, env)
        env = {k: v for k, v in os.environ.items() if k not in ("LANGUAGE", "LC_ALL", "LC_MESSAGES")}
        env["LANG"] = "es_ES.UTF-8"
        _, _, lang = self.start("--idle", "30", env=env)
        self.assertEqual(lang, "es")

    def test_only_loopback(self):
        proc, port, _ = self.start("--idle", "30")
        out = subprocess.run(["ss", "-ltnH", f"sport = :{port}"], capture_output=True, text=True)
        if out.returncode != 0:
            self.skipTest("ss unavailable")
        self.assertIn(f"127.0.0.1:{port}", out.stdout)
        self.assertNotIn(f"0.0.0.0:{port}", out.stdout)

    def test_exits_when_idle(self):
        proc, port, _ = self.start("--idle", "1")
        get(port, "/")
        self.assertEqual(proc.wait(timeout=15), 0)

    def test_missing_page(self):
        out = subprocess.run(
            [sys.executable, "-I", str(HELPER), "--no-open", "--page", "/nonexistent/page.html"],
            capture_output=True, text=True, timeout=10)
        self.assertEqual(out.returncode, 1)
        self.assertIn("tab-companion-spen-test:", out.stderr)

    def test_print_url_detaches(self):
        # Tab Companion reads stdout to the end, so the parent must return
        # with the address while the detached child keeps serving.
        start = time.monotonic()
        out = subprocess.run(
            [sys.executable, "-I", str(HELPER), "--print-url", "--page", str(PAGE), "--idle", "3"],
            capture_output=True, text=True, timeout=20)
        self.assertLess(time.monotonic() - start, 10)
        self.assertEqual(out.returncode, 0, out.stderr)
        m = re.fullmatch(r"http://127\.0\.0\.1:(\d+)/\?lang=\w+", out.stdout.strip())
        self.assertIsNotNone(m, out.stdout)
        resp, body = get(int(m.group(1)), "/")
        self.assertEqual((resp.status, body), (200, PAGE.read_bytes()))

    def test_open_in_browser_reports_failure(self):
        helper = load_helper()

        class GLibError(Exception):
            def __init__(self, message):
                super().__init__(message)
                self.message = message

        opened = []

        def launch(url, context):
            opened.append(url)
            if "fail" in url:
                raise GLibError("No application is registered as handling this file")

        gio = types.SimpleNamespace(AppInfo=types.SimpleNamespace(launch_default_for_uri=launch))
        repository = types.ModuleType("gi.repository")
        repository.Gio = gio
        repository.GLib = types.SimpleNamespace(Error=GLibError)
        with mock.patch.dict(sys.modules, {"gi": types.ModuleType("gi"), "gi.repository": repository}):
            self.assertIsNone(helper.open_in_browser("http://127.0.0.1:1/?lang=en"))
            self.assertIn("registered", helper.open_in_browser("http://127.0.0.1:1/fail"))
        self.assertEqual(len(opened), 2)


class CompanionTests(unittest.TestCase):
    def test_window_runs_the_shipped_helper(self):
        source = WINDOW.read_text(encoding="utf-8")
        tree = ast.parse(source)
        consts = {t.id: n.value.value for n in ast.walk(tree) if isinstance(n, ast.Assign)
                  for t in n.targets if isinstance(t, ast.Name) and isinstance(n.value, ast.Constant)}
        self.assertEqual(PKG / consts["PEN_TEST"].lstrip("/"), HELPER)
        self.assertEqual(consts["SIDE_SPLIT_ATTRIBUTE"], "/sys/bus/i2c/devices/*/side_button_split")
        # The window opens the address itself (activation token, no xdg-open).
        self.assertIn('[PEN_TEST, "--print-url"]', source)
        self.assertIn("Gtk.UriLauncher.new(url).launch(self, None, self._pen_test_opened, url)", source)
        self.assertIn('"/usr/share/tab-companion/spen-test.html"', HELPER.read_text())
        self.assertTrue(PAGE.is_file())

    def test_versions_agree(self):
        control = (PKG / "DEBIAN/control").read_text()
        version = re.search(r"^Version: (\S+)$", control, re.M).group(1)
        init = (PKG / "usr/lib/tab-companion/tab_companion/__init__.py").read_text()
        self.assertIn(f'VERSION = "{version}"', init)
        meta = (PKG / "usr/share/metainfo/io.github.agcarbajo.TabCompanion.metainfo.xml").read_text()
        self.assertIn(f'<release version="{version}"', meta)


class SideButtonSplitTests(unittest.TestCase):
    """The driver's attributes, and everything in userspace that names them."""

    def setUp(self):
        self.driver = DRIVER.read_text(encoding="utf-8")

    def test_driver_attributes(self):
        for name in SPLIT_ATTRIBUTES:
            self.assertIn(f"static DEVICE_ATTR_RW({name});", self.driver)
            self.assertIn(f"&dev_attr_{name}.attr,", self.driver)
        self.assertIn("static DEVICE_ATTR_RO(pen_height);", self.driver)
        self.assertIn("&dev_attr_pen_height.attr,", self.driver)
        self.assertIn("input_set_capability(input, EV_KEY, BTN_STYLUS2);", self.driver)

    def test_report_layout_matches_samsung(self):
        # Bytes 7-9 are height, tilt X, tilt Y (wacom_i2c_coord_handler()).
        self.assertRegex(self.driver, r"#define WACOM_REPORT_HEIGHT\s+7\n")
        self.assertRegex(self.driver, r"#define WACOM_REPORT_TILT_X\s+8\n")
        self.assertRegex(self.driver, r"#define WACOM_REPORT_TILT_Y\s+9\n")
        self.assertRegex(self.driver, r"#define WACOM_PRESSURE_MASK\s+GENMASK\(11, 0\)\n")
        self.assertIn("input_report_abs(input, ABS_DISTANCE, height);", self.driver)

    def test_button_held_into_range_counts_as_near(self):
        # Judged on the entry height (about 85) it would be a far press: a
        # stray middle click, then left clicks instead of right-click taps.
        self.assertIn("(first || tip || wacom->near) ?", self.driver)

    def test_split_settings_readable_after_boot(self):
        # At coldplug the desktop-user file does not exist yet, so the helper
        # (outside the input group) can only read them if they are 0664.
        permissions = PERMISSIONS.read_text(encoding="utf-8")
        self.assertIn('chmod 0664 "$path"', permissions)
        self.assertNotIn("chmod 0660", permissions)

    def test_defaults_agree(self):
        near = re.search(r"#define WACOM_SIDE_NEAR_HEIGHT\s+(\d+)", self.driver).group(1)
        far = re.search(r"#define WACOM_SIDE_FAR_HEIGHT\s+(\d+)", self.driver).group(1)
        schema = SCHEMA.read_text(encoding="utf-8")
        self.assertRegex(schema, r'<key name="spen-side-button-split" type="b"><default>false</default>')
        self.assertRegex(schema, rf'<key name="spen-side-button-near-height" type="i">.*<default>{near}</default>')
        self.assertRegex(schema, rf'<key name="spen-side-button-far-height" type="i">.*<default>{far}</default>')
        self.assertLess(int(near), int(far))

    def test_userspace_names_the_attributes(self):
        hardware = HARDWARE.read_text(encoding="utf-8")
        permissions = PERMISSIONS.read_text(encoding="utf-8")
        helper = HELPER.read_text(encoding="utf-8")
        for name in SPLIT_ATTRIBUTES:
            self.assertIn(f'"{name}": self.settings.get_', hardware)
            self.assertIn(name, permissions)
            self.assertIn(f'"{name}"', helper)
        for key in ("spen-side-button-split", "spen-side-button-near-height", "spen-side-button-far-height"):
            self.assertIn(f'"{key}"', hardware)


if __name__ == "__main__":
    unittest.main()
