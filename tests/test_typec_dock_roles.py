"""The roles the tablet takes on a powered USB-C dock.

Two TCPM patches and the SM5714 policy that switches them on:

- request_dfp_as_sink: as PD sink, ask for the USB host role before discovery
  (a charge-through dock otherwise flaps over its own DR_Swap), a few times
  per attach at most;
- try_snk_extra_wait_ms: keep waiting for Rp in Try.SNK (the SM5714 reports a
  source after TCPM's 100 ms have run out, so a dual-role dock ended up being
  powered by the tablet), as one deadline, so that an Rp which goes away
  again does not end the attempt.

The driver sets tcpc_dev fields that only these patches declare, so a kernel
without the hooks does not compile; the tests keep driver, patches and hooks
in step.  With the pinned kernel checkout at hand (KERNEL_SRC, or the build
script's default place) the patches are also applied in build order and the
resulting state machine is checked; the checkout is only read.  The write-up
is held to the code as well: its log lines, its numbers and its links.
"""

from pathlib import Path
import os
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
PATCHES = ROOT / "kernel/patches"
BUILD = ROOT / "scripts/build-mainline-kernel.sh"
DRIVER = ROOT / "kernel/drivers/sm5714_usbpd.c"
DOC = ROOT / "docs/usb-c-dock-roles.md"
TCPM_C = "drivers/usb/typec/tcpm/tcpm.c"
TCPM_H = "include/linux/usb/tcpm.h"

# tcpc_dev field -> the patch that declares it, in build order.
FIELDS = [
    ("adopt_retained_source_ufp", "tcpm-adopt-retained-source-ufp-role.patch"),
    ("consume_retained_sink_dfp", "tcpm-use-retained-sink-data-role.patch"),
    ("request_dfp_as_sink", "tcpm-request-dfp-as-sink-before-discovery.patch"),
    ("try_snk_extra_wait_ms", "tcpm-try-snk-extra-wait.patch"),
]


def added(patch, path):
    """The lines a patch adds to one file."""
    lines, current = [], None
    for line in (PATCHES / patch).read_text().splitlines():
        if line.startswith("+++ "):
            current = line[4:].split("\t")[0]
        elif line.startswith("+") and current == "b/" + path:
            lines.append(line[1:])
    return "\n".join(lines)


def hooks():
    """[(marker, file, patch)] for every apply_unless call, in order."""
    text = BUILD.read_text().replace("\\\n", " ")
    return re.findall(r"^apply_unless '([^']+)'\s+(\S+)\s+(\S+)\s*$", text, re.M)


def function(source, name):
    """Body of a function defined at column 0, up to its closing brace."""
    match = re.search(r"^[a-z].*\b%s\(.*\n(?:.*\n)*?\{\n" % re.escape(name), source, re.M)
    if not match:
        raise AssertionError("no definition of %s()" % name)
    return source[match.end():source.index("\n}\n", match.end())]


def case(body, label, following):
    """Text between `case label:` and the next `case following:`."""
    start = body.index("case %s:" % label)
    return body[start:body.index("case %s:" % following, start)]


class BuildHookTests(unittest.TestCase):
    def test_every_field_is_hooked_in_dependency_order(self):
        tcpm = [(marker, path, patch) for marker, path, patch in hooks()
                if patch.startswith("tcpm-")]
        self.assertEqual(tcpm, [(field, TCPM_H, patch) for field, patch in FIELDS])

    def test_marker_means_this_patch_and_no_earlier_one(self):
        # apply_unless skips a patch whose marker is already in the file: the
        # marker has to come from the patch itself and from none before it.
        for number, (field, patch) in enumerate(FIELDS):
            self.assertTrue((PATCHES / patch).is_file(), patch)
            self.assertIn(field, added(patch, TCPM_H), patch)
            for _, earlier in FIELDS[:number]:
                self.assertNotIn(field, added(earlier, TCPM_H), earlier)

    def test_tcpm_patches_touch_only_tcpm(self):
        for _, patch in FIELDS:
            files = re.findall(r"^\+\+\+ b/(\S+)", (PATCHES / patch).read_text(), re.M)
            self.assertEqual(sorted(files), [TCPM_C, TCPM_H], patch)

    def test_usb_power_domain_patch_is_applied_where_it_is_carried(self):
        # What lets the USB host live through the suspend cycle of a boot
        # with a dock attached.  The SM-X910's branch carries it; a branch
        # without the patch file has no hook for it either.
        patch = "gcc-sm8550-usb-gdscs-retention.patch"
        path = "drivers/clk/qcom/gcc-sm8550.c"
        mine = [(marker, target) for marker, target, name in hooks() if name == patch]
        if not (PATCHES / patch).is_file():
            self.assertEqual(mine, [])
            return
        self.assertEqual(mine, [("Retention across system suspend, as on sm8650", path)])
        text = (PATCHES / patch).read_text()
        self.assertEqual(re.findall(r"^\+\+\+ b/(\S+)", text, re.M), [path])
        # The marker is a line of the patch's own; the change is the power
        # states of the two USB domains and nothing else.
        self.assertEqual(added(patch, path).count(mine[0][0]), 1)
        changed = [line for line in text[text.index("\n--- a/"):].splitlines()
                   if re.match(r"[+-]\t", line)]
        self.assertEqual(changed, ["-\t.pwrsts = PWRSTS_OFF_ON,", "+\t.pwrsts = PWRSTS_RET_ON,"] * 2)
        for name in ("usb30_prim_gdsc", "usb3_phy_gdsc"):
            self.assertIn('.name = "%s",' % name, text)


class PatchTests(unittest.TestCase):
    def setUp(self):
        self.code = added("tcpm-request-dfp-as-sink-before-discovery.patch", TCPM_C)
        self.helper = function(self.code + "\n", "tcpm_request_dfp_as_sink")

    def test_request_is_opt_in_and_only_towards_a_source_that_offers_the_swap(self):
        # Nothing else in the helper runs for a controller without the flag.
        self.assertTrue(self.helper.lstrip().startswith(
            "if (!port->tcpc->request_dfp_as_sink || port->sink_dfp_done ||\n"
            "\t    port->sink_dfp_tries >= SINK_DFP_SWAP_TRIES)\n"
            "\t\treturn 0;"), self.helper[:200])
        for condition in ("port->typec_caps.data != TYPEC_PORT_DRD",
                          "port->pwr_role != TYPEC_SINK", "port->data_role != TYPEC_DEVICE",
                          "!port->pd_capable", "!port->explicit_contract",
                          "!port->nr_source_caps",
                          "!(port->source_caps[0] & PDO_FIXED_DATA_SWAP)"):
            self.assertIn(condition, self.helper)
        # source_caps[0] is only read when there are capabilities.
        self.assertLess(self.helper.index("!port->nr_source_caps"),
                        self.helper.index("port->source_caps[0]"))
        self.assertIn("if (tcpm_request_dfp_as_sink(port) <= 0)", self.code)

    def test_request_goes_through_the_ams_machinery_and_waits_its_turn(self):
        # tcpm_ams_start() is what keeps PD 3.0 collision avoidance; a port
        # that is busy, or inside tDRSwapWait, is asked again later.  So is
        # one with a state change pending: upcoming_state would be replaced,
        # and a delayed state would be dropped by the move to AMS_START.
        self.assertRegex(self.helper,
                         r"if \(port->state != SNK_READY \|\| port->vdm_sm_running \|\|\n"
                         r"\t    port->upcoming_state != INVALID_STATE \|\|\n"
                         r"\t    port->delayed_state != INVALID_STATE \|\|\n"
                         r"\t    ktime_before\(ktime_get\(\), port->sink_dfp_not_before\)\)\n"
                         r"\t\treturn -EAGAIN;")
        self.assertLess(self.helper.index("port->upcoming_state != INVALID_STATE"),
                        self.helper.index("port->upcoming_state = DR_SWAP_SEND;"))
        start = self.helper.index("tcpm_ams_start(port, DATA_ROLE_SWAP)")
        self.assertLess(self.helper.index("port->upcoming_state = DR_SWAP_SEND;"), start)
        # A request that could not start is not counted as a try.
        counted = self.helper.index("port->sink_dfp_tries++;")
        self.assertLess(start, counted)
        self.assertLess(self.helper.index("port->upcoming_state = INVALID_STATE;"), counted)
        self.assertEqual(self.code.count("port->sink_dfp_tries++;"), 1)

    def test_requests_per_attach_are_bounded_and_wait_out_tdrswapwait(self):
        tries = int(re.search(r"#define SINK_DFP_SWAP_TRIES\s+(\d+)", self.code).group(1))
        wait = int(re.search(r"#define SINK_DFP_SWAP_WAIT_MS\s+(\d+)", self.code).group(1))
        self.assertTrue(1 <= tries <= 5, tries)
        # tDRSwapWait: a request repeated sooner than 100 ms after Wait is
        # a protocol error of its own.
        self.assertGreaterEqual(wait, 100)

    def test_wait_is_not_final_and_reject_is(self):
        self.assertRegex(self.code,
                         r"if \(type == PD_CTRL_WAIT\)\n"
                         r"\t+port->sink_dfp_not_before =\n"
                         r"\t+ktime_add_ms\(ktime_get\(\), SINK_DFP_SWAP_WAIT_MS\);\n"
                         r"\t+else\n"
                         r"\t+port->sink_dfp_done = true;")

    def test_new_fields_are_documented_where_the_struct_is(self):
        # struct tcpc_dev has a kernel-doc block; the two fields go there.
        for field, patch in FIELDS[2:]:
            self.assertRegex(added(patch, TCPM_H), r"(?m)^ \* @%s:$" % field, patch)

    def test_extra_wait_is_one_deadline_and_the_only_change_to_try_snk(self):
        code = added("tcpm-try-snk-extra-wait.patch", TCPM_C)
        # tDRPTry is used once, in the deadline: the first wait stays as it
        # is, and the deadline is set in one place.
        self.assertEqual(code.count("PD_T_DRP_TRY"), 1)
        self.assertRegex(code, r"port->try_snk_wait_until = jiffies \+\n"
                               r"\t+msecs_to_jiffies\(PD_T_DRP_TRY \+\n"
                               r"\t+ +port->tcpc->try_snk_extra_wait_ms\);")
        self.assertEqual(code.count("try_snk_wait_until ="), 1)
        self.assertEqual(code.count("case SNK_TRY_WAIT:"), 3)
        # Every wait the patch adds comes from the helper; it reads the
        # controller's field in no other place.
        self.assertEqual(code.count("tcpm_try_snk_wait_left(port)"), 3)
        self.assertEqual(code.count("port->tcpc->try_snk_extra_wait_ms"), 2)

    def test_time_left_is_zero_without_the_extra_wait_and_after_the_deadline(self):
        helper = function(added("tcpm-try-snk-extra-wait.patch", TCPM_C) + "\n",
                          "tcpm_try_snk_wait_left")
        # With the field at 0 the deadline is never looked at, so the three
        # callers do what upstream does.
        self.assertRegex(helper, r"if \(!port->tcpc->try_snk_extra_wait_ms \|\|\n"
                                 r"\t    !time_before\(now, port->try_snk_wait_until\)\)\n"
                                 r"\t\treturn 0;\n")
        # One reading of the clock for the check and for the difference: the
        # subtraction cannot run on a deadline that passed in between.
        self.assertEqual(len(re.findall(r"\bjiffies\b", helper)), 1)
        self.assertIn("unsigned long now = jiffies;", helper)
        self.assertTrue(helper.rstrip().endswith(
            "return jiffies_to_msecs(port->try_snk_wait_until - now);"), helper)


class DriverTests(unittest.TestCase):
    def setUp(self):
        self.source = DRIVER.read_text()

    def test_every_field_the_driver_sets_has_its_patch(self):
        for field, _ in FIELDS:
            self.assertRegex(self.source, r"tcpc\.%s\s*=" % field)

    def test_defaults_and_runtime_switches(self):
        self.assertIn("static bool sink_host_swap = true;", self.source)
        self.assertIn("module_param(sink_host_swap, bool, 0644);", self.source)
        self.assertIn("static unsigned int try_snk_extra_wait_ms = 600;", self.source)
        self.assertIn("module_param(try_snk_extra_wait_ms, uint, 0644);", self.source)

    def test_policy_is_bounded(self):
        policy = function(self.source, "sm5714_usbpd_update_policy")
        self.assertIn("sm->tcpc.request_dfp_as_sink = sink_host_swap;", policy)
        self.assertRegex(policy, r"sm->tcpc\.try_snk_extra_wait_ms =\s+"
                                 r"min\(try_snk_extra_wait_ms, SM5714_TRY_SNK_EXTRA_WAIT_MAX_MS\);")
        limit = int(re.search(r"#define SM5714_TRY_SNK_EXTRA_WAIT_MAX_MS\s+(\d+)",
                              self.source).group(1))
        default = int(re.search(r"try_snk_extra_wait_ms = (\d+);", self.source).group(1))
        # 100 ms is tDRPTry.  453 ms is the latest report of a source measured
        # after Rd (port forced to sink); inside Try.SNK the reports came 124
        # to 147 ms after Rd, and the estimate for a dock that tries the sink
        # role first itself is about 515 ms.
        self.assertGreater(100 + default, 515 + 150)
        self.assertLessEqual(default, limit)
        self.assertLessEqual(limit, 2000)

    def test_policy_is_set_before_tcpm_runs_and_refreshed_per_attach(self):
        probe = function(self.source, "sm5714_usbpd_probe")
        self.assertLess(probe.index("sm5714_usbpd_update_policy(sm);"),
                        probe.index("sm->port = tcpm_register_port("))
        # start_toggling() runs under the TCPM port lock each time the port
        # goes back to unattached: a changed parameter applies from there.
        toggling = function(self.source, "sm5714_usbpd_start_toggling")
        self.assertLess(toggling.index("sm5714_usbpd_update_policy(sm);"),
                        toggling.index("port_type =="))

    def test_both_boards_ask_for_the_sink_role_on_a_dual_role_port(self):
        # Without try-power-role TCPM never enters Try.SNK, and the DR_Swap
        # request is limited to dual-role-data ports.
        for board in ("gts9uwifi", "gts9wifi"):
            dts = (ROOT / "kernel/dts" / ("sm8550-samsung-%s.dts" % board)).read_text()
            start = dts.index('compatible = "usb-c-connector";')
            connector = dts[start:dts.index("source-pdos", start)]
            for wanted in ('power-role = "dual";', 'data-role = "dual";',
                           'try-power-role = "sink";'):
                self.assertIn(wanted, connector, board)


def kernel_source():
    base = os.environ.get("UBUNTU_WORKDIR", str(Path.home() / "ubuntu-gts9u"))
    source = Path(os.environ.get("KERNEL_SRC", base + "/linux-mainline"))
    return source if (source / ".git").exists() and shutil.which("patch") else None


@unittest.skipUnless(kernel_source(), "no pinned kernel checkout")
class AppliedTests(unittest.TestCase):
    """The four TCPM patches on the pinned kernel, applied as the build does."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        tree = Path(cls.tmp.name)
        for path in (TCPM_C, TCPM_H):
            (tree / path).parent.mkdir(parents=True)
            (tree / path).write_bytes(subprocess.run(
                ["git", "-C", str(kernel_source()), "show", "HEAD:" + path],
                check=True, capture_output=True).stdout)
        cls.log = {}
        for field, patch in FIELDS:
            assert field not in (tree / TCPM_H).read_text(), field
            with open(PATCHES / patch, "rb") as stream:
                cls.log[patch] = subprocess.run(
                    ["patch", "-d", str(tree), "-p1", "--no-backup-if-mismatch"],
                    stdin=stream, check=True, capture_output=True, text=True).stdout
        cls.source = (tree / TCPM_C).read_text()
        cls.header = (tree / TCPM_H).read_text()

    def test_new_patches_apply_exactly(self):
        # Fuzz or an offset in a state machine full of look-alike case blocks
        # is how a hunk lands in the wrong state.
        for _, patch in FIELDS[2:]:
            self.assertNotRegex(self.log[patch], r"fuzz|offset", patch)

    def test_header_declares_every_field_once(self):
        for field, _ in FIELDS:
            # One member each; consume_retained_sink_dfp is a callback.
            self.assertEqual(len(re.findall(r"^\t[a-z].*\b%s\b.*;$" % field,
                                            self.header, re.M)), 1, field)

    def test_sink_ready_requests_the_swap_instead_of_discovery(self):
        machine = function(self.source, "run_state_machine")
        ready = case(machine, "SNK_READY", "SNK_HARD_RESET_SINK_OFF")
        self.assertRegex(ready, r"if \(tcpm_request_dfp_as_sink\(port\) <= 0\)\n"
                                r"\t+mod_send_discover_delayed_work\(port, 0\);")
        self.assertEqual(ready.count("mod_send_discover_delayed_work("), 1)
        # After a queued state change has been acted on (the helper leaves
        # one alone), under an explicit contract only, and in the place of
        # the discovery it precedes.
        request = ready.index("tcpm_request_dfp_as_sink(port)")
        self.assertLess(ready.index("tcpm_set_state(port, upcoming_state, 0);"), request)
        self.assertLess(ready.index("if (port->explicit_contract) {"), request)
        self.assertLess(request, ready.index("} else {\n\t\t\tport->send_discover = false;"))
        self.assertEqual(ready.count("tcpm_request_dfp_as_sink("), 1)

    def test_discover_work_puts_the_request_before_anything_of_its_own(self):
        # Also before the send_discover check: a request that is still due
        # must not be dropped because discovery is not.
        work = function(self.source, "tcpm_send_discover_work")
        request = work.index("ret = tcpm_request_dfp_as_sink(port);")
        self.assertLess(work.index("mutex_lock(&port->lock);"), request)
        self.assertLess(request, work.index("!port->send_discover && !port->send_discover_prime"))
        self.assertLess(request, work.index("port->negotiated_rev < PD_REV30"))
        self.assertLess(request, work.index("CMD_DISCOVER_IDENT"))
        # With the opt-in, the work does nothing for a port that is no longer
        # attached: neither a request nor the 100 ms retry it used to keep
        # up after an unplug.
        self.assertRegex(work, r"mutex_lock\(&port->lock\);\n"
                               r"\t/\*.*\*/\n"
                               r"\tif \(port->tcpc->request_dfp_as_sink && !port->attached\)\n"
                               r"\t\tgoto unlock;\n")
        self.assertRegex(work, r"if \(ret > 0\)\n\t+goto unlock;\n"
                               r"\t+if \(ret == -EAGAIN\) \{\n"
                               r"\t+mod_send_discover_delayed_work\(port, SEND_DISCOVER_RETRY_MS\);\n"
                               r"\t+goto unlock;")

    def test_attach_state_starts_over_on_detach(self):
        reset = function(self.source, "tcpm_reset_port")
        for line in ("port->sink_dfp_tries = 0;", "port->sink_dfp_done = false;",
                     "port->sink_dfp_not_before = 0;", "port->data_role_swapped = false;"):
            self.assertIn(line, reset)
        self.assertEqual(self.source.count("port->sink_dfp_tries = 0;"), 1)
        # A request queued for the old partner (upcoming_state, which the
        # next SNK_READY would act on unchecked) goes with it; only with the
        # opt-in, other ports keep what upstream does.
        self.assertRegex(reset, r"if \(port->tcpc->request_dfp_as_sink\)\n"
                                r"\t\tport->upcoming_state = INVALID_STATE;\n")

    def test_request_queued_before_a_hard_reset_goes_with_it(self):
        # tcpm_request_dfp_as_sink() leaves DR_SWAP_SEND in upcoming_state
        # until the AMS starts.  A Hard Reset that comes in between does not
        # clear it upstream, and the next SNK_READY would send the swap
        # without any of the helper's checks.  Only with the opt-in.
        machine = function(self.source, "run_state_machine")
        start = case(machine, "HARD_RESET_START", "SRC_HARD_RESET_VBUS_OFF")
        self.assertRegex(start, r"port->sink_dfp_done = false;\n"
                                r"\t+if \(port->tcpc->request_dfp_as_sink\)\n"
                                r"\t+port->upcoming_state = INVALID_STATE;\n")
        self.assertLess(start.index("port->upcoming_state = INVALID_STATE;"),
                        start.index("tcpm_set_state(port, SRC_HARD_RESET_VBUS_OFF,"))
        # The two places: detach and Hard Reset.
        self.assertEqual(self.source.count("if (port->tcpc->request_dfp_as_sink)\n"), 2)

    def test_what_settles_the_data_role_and_what_reopens_it(self):
        machine = function(self.source, "run_state_machine")
        # Settled: Reject or Not_Supported (not Wait), and any completed swap,
        # the partner's included, so that a role just handed over is not
        # asked back.
        control = function(self.source, "tcpm_pd_ctrl_request")
        refused = control[control.index("case DR_SWAP_SEND:"):]
        refused = refused[:refused.index("case PR_SWAP_SEND:")]
        self.assertRegex(refused, r"else\n\t+port->sink_dfp_done = true;\n"
                                  r"\t+tcpm_set_state\(port, DR_SWAP_CANCEL, 0\);")
        changed = case(machine, "DR_SWAP_CHANGE_DR", "FR_SWAP_SEND")
        self.assertLess(changed.index("port->sink_dfp_done = true;"),
                        changed.index("tcpm_unregister_altmodes(port);"))
        # And the port's own Discover Identity: no request once it is queued.
        work = function(self.source, "tcpm_send_discover_work")
        self.assertRegex(work, r"\tport->sink_dfp_done = true;\n"
                               r"\ttcpm_send_vdm\(port, USB_SID_PD, CMD_DISCOVER_IDENT,")
        self.assertLess(work.index("Retry if the port is not idle"),
                        work.index("port->sink_dfp_done = true;"))
        self.assertEqual(self.source.count("port->sink_dfp_done = true;"), 3)
        # Reopened: a hard reset puts the data role back to its default.
        self.assertIn("port->sink_dfp_done = false;",
                      case(machine, "HARD_RESET_START", "SRC_HARD_RESET_VBUS_OFF"))
        self.assertEqual(self.source.count("port->sink_dfp_done = false;"), 2)

    def test_unanswered_request_still_discovers(self):
        # DR_SWAP_SEND sets send_discover and the timeout used to clear it,
        # which fits a swap asked for after discovery.  With the opt-in the
        # timeout puts back what DR_SWAP_SEND found, whoever asked for the
        # swap; without it the flag is cleared as upstream does.
        machine = function(self.source, "run_state_machine")
        send = case(machine, "DR_SWAP_SEND", "DR_SWAP_ACCEPT")
        self.assertLess(send.index("port->send_discover_before_swap = port->send_discover;"),
                        send.index("port->send_discover = true;"))
        timeout = case(machine, "DR_SWAP_SEND_TIMEOUT", "DR_SWAP_CHANGE_DR")
        self.assertRegex(timeout,
                         r"port->send_discover = port->tcpc->request_dfp_as_sink &&\n"
                         r"\s+port->send_discover_before_swap;\n"
                         r"\t+port->send_discover_prime = false;")
        self.assertNotIn("swap_pending", timeout)
        self.assertEqual(self.source.count("port->send_discover_before_swap ="), 1)

    def test_host_role_from_a_swap_does_not_outlive_a_hard_reset(self):
        # A Hard Reset puts both ends back to their default data roles.  The
        # earlier patch keeps a sink that is host in that role, for a partner
        # that retained UFP; a role that came from a DR_Swap must go back, or
        # the partner's next message is a data role mismatch.
        machine = function(self.source, "run_state_machine")
        sink_off = case(machine, "SNK_HARD_RESET_SINK_OFF", "SNK_HARD_RESET_WAIT_VBUS")
        self.assertRegex(sink_off,
                         r"port->tcpc->adopt_retained_source_ufp &&\n"
                         r"\s+port->data_role == TYPEC_HOST &&\n"
                         r"\s+!\(port->tcpc->request_dfp_as_sink &&\n"
                         r"\s+port->data_role_swapped\) \?\n"
                         r"\s+TYPEC_HOST : tcpm_data_role_for_sink\(port\)\);\n"
                         r"\t+port->data_role_swapped = false;")
        # Set by a completed swap and by nothing else; cleared wherever the
        # role goes back to a default: detach and both hard reset paths.
        self.assertEqual(self.source.count("port->data_role_swapped = true;"), 1)
        self.assertIn("port->data_role_swapped = true;",
                      case(machine, "DR_SWAP_CHANGE_DR", "FR_SWAP_SEND"))
        self.assertEqual(self.source.count("port->data_role_swapped = false;"), 3)
        self.assertIn("port->data_role_swapped = false;",
                      case(machine, "SRC_HARD_RESET_VBUS_OFF", "SRC_HARD_RESET_VBUS_ON"))
        self.assertIn("port->data_role_swapped = false;",
                      function(self.source, "tcpm_reset_port"))

    def test_try_snk_waits_on_in_its_own_state(self):
        machine = function(self.source, "run_state_machine")
        wait = case(machine, "SNK_TRY_WAIT", "SNK_TRY_WAIT_DEBOUNCE")
        self.assertRegex(wait, r"^case SNK_TRY_WAIT:\n"
                               r"\t+if \(tcpm_port_is_sink\(port\)\) \{\n"
                               r"\t+tcpm_set_state\(port, SNK_TRY_WAIT_DEBOUNCE, 0\);\n"
                               r"\t+\} else \{\n"
                               r"(?:\t+ ?/?\*.*\n)+"
                               r"\t+tcpm_set_state\(port, SRC_TRYWAIT,\n"
                               r"\t+ +tcpm_try_snk_wait_left\(port\)\);\n"
                               r"\t+port->max_wait = 0;\n"
                               r"\t+\}\n\t+break;\s*$")
        # The first tDRPTry is untouched, in the state machine and in what
        # a CC change does during it.  The deadline is set as Rd goes up, so
        # that it covers tDRPTry and the extra wait together.
        first = case(machine, "SNK_TRY", "SNK_TRY_WAIT")
        self.assertRegex(first, r"tcpm_set_cc\(port, TYPEC_CC_RD\);\n"
                                r"\t+port->try_snk_wait_until = jiffies \+\n"
                                r"\t+msecs_to_jiffies\(PD_T_DRP_TRY \+\n"
                                r"\t+ +port->tcpc->try_snk_extra_wait_ms\);\n"
                                r"\t+tcpm_set_state\(port, SNK_TRY_WAIT, PD_T_DRP_TRY\);\n"
                                r"\t+break;\s*$")
        self.assertEqual(self.source.count("try_snk_wait_until ="), 1)
        self.assertRegex(case(function(self.source, "_tcpm_cc_change"), "SNK_TRY", "SNK_TRY_WAIT"),
                         r"^case SNK_TRY:\n\t+/\* Do nothing, waiting for timeout \*/\n"
                         r"\t+break;\s*$")
        # The states after an Rp are upstream's: debounce, then VBUS.
        self.assertRegex(case(machine, "SNK_TRY_WAIT_DEBOUNCE", "SNK_TRY_WAIT_DEBOUNCE_CHECK_VBUS"),
                         r"^case SNK_TRY_WAIT_DEBOUNCE:\n"
                         r"\t+tcpm_set_state\(port, SNK_TRY_WAIT_DEBOUNCE_CHECK_VBUS,\n"
                         r"\t+ +PD_T_TRY_CC_DEBOUNCE\);\n\t+break;\s*$")

    def test_an_rp_that_goes_away_sends_the_port_back_to_waiting(self):
        # A dock that tries the sink role first itself shows Rp, takes it
        # away and shows it again for good.  With time left the port goes
        # back to SNK_TRY_WAIT (whose entry code arms what is left of the
        # deadline); without, both debounce states give up as upstream does,
        # with upstream's delays.
        change = function(self.source, "_tcpm_cc_change")
        self.assertRegex(case(change, "SNK_TRY_WAIT_DEBOUNCE", "SRC_TRY_WAIT"),
                         r"^case SNK_TRY_WAIT_DEBOUNCE:\n"
                         r"\t+if \(!tcpm_port_is_sink\(port\)\) \{\n"
                         r"\t+port->max_wait = 0;\n"
                         r"\t+/\*.*\*/\n"
                         r"\t+if \(tcpm_try_snk_wait_left\(port\)\)\n"
                         r"\t+tcpm_set_state\(port, SNK_TRY_WAIT, 0\);\n"
                         r"\t+else\n"
                         r"\t+tcpm_set_state\(port, SRC_TRYWAIT, PD_T_PD_DEBOUNCE\);\n"
                         r"\t+\}\n\t+break;\s*$")
        self.assertRegex(case(change, "SNK_TRY_WAIT_DEBOUNCE_CHECK_VBUS", "SNK_TRYWAIT"),
                         r"^case SNK_TRY_WAIT_DEBOUNCE_CHECK_VBUS:\n"
                         r"\t+/\*.*\*/\n"
                         r"\t+if \(tcpm_port_is_sink\(port\)\)\n"
                         r"\t+tcpm_set_state\(port, SNK_TRY_WAIT_DEBOUNCE_CHECK_VBUS, 0\);\n"
                         r"\t+else if \(tcpm_try_snk_wait_left\(port\)\)\n"
                         r"\t+tcpm_set_state\(port, SNK_TRY_WAIT, 0\);\n"
                         r"\t+else\n"
                         r"\t+tcpm_set_state\(port, SRC_TRYWAIT, PD_T_TRY_CC_DEBOUNCE\);\n"
                         r"\t+break;\s*$")
        # The helper has its three callers and nothing else moves a port
        # back into the wait.
        self.assertEqual(self.source.count("tcpm_try_snk_wait_left(port)"), 3)
        self.assertEqual(self.source.count("tcpm_set_state(port, SNK_TRY_WAIT, 0);"), 2)
        self.assertEqual(len(re.findall(r"tcpm_set_state\(port, SNK_TRY_WAIT,", self.source)), 3)

    def test_late_rp_is_taken_and_nothing_else_ends_the_wait(self):
        change = function(self.source, "_tcpm_cc_change")
        # Without its own case the port would take the default one, which
        # sends a port that reads disconnected back to unattached.
        wait = case(change, "SNK_TRY_WAIT", "SNK_DISCOVERY")
        self.assertRegex(wait, r"\*/\n"
                               r"\t+if \(tcpm_port_is_sink\(port\)\)\n"
                               r"\t+tcpm_set_state\(port, SNK_TRY_WAIT_DEBOUNCE, 0\);\n"
                               r"\t+break;\s*$")
        # VBUS coming or going while waiting must not reach the default
        # cases, which treat the port as attached.
        for handler in ("_tcpm_pd_vbus_on", "_tcpm_pd_vbus_off"):
            body = function(self.source, handler)
            self.assertRegex(body, r"case SNK_TRY:\n\t+case SNK_TRY_WAIT:\n"
                                   r"\t+/\* Do nothing, waiting for timeout \*/", handler)

    def test_states_the_write_up_names_exist(self):
        names = set(re.findall(r"\b[A-Z]+(?:_[A-Z]+)+\b", DOC.read_text()))
        states = set(re.findall(r"^\tS\(([A-Z_]+)\),", self.source, re.M))
        for name in names:
            if re.match(r"(SNK|SRC|DR_SWAP|PR_SWAP|AMS|ERROR|PORT|HARD|SOFT)_", name):
                self.assertIn(name, states)


def block_after(text, marker):
    """The first fenced block after a marker."""
    start = text.index("```", text.index(marker))
    start = text.index("\n", start) + 1
    return text[start:text.index("```", start)].splitlines()


class WriteUpTests(unittest.TestCase):
    """What the write-up tells a tester to look for is what the code logs."""

    def setUp(self):
        self.doc = DOC.read_text()
        self.code = added("tcpm-request-dfp-as-sink-before-discovery.patch", TCPM_C)

    def test_log_line_is_the_one_the_patch_emits(self):
        logged = re.findall(r'tcpm_log\(port, "([^"]+)"\)', self.code)
        self.assertEqual(logged, ["Requesting DFP before discovery"])
        self.assertIn(logged[0], block_after(self.doc, "In the log, after the contract"))

    def test_log_block_is_in_the_order_the_code_runs(self):
        # tcpm_ams_start() logs the AMS and leaves SNK_READY before the
        # helper logs its own line; the swap states follow from AMS_START.
        helper = function(self.code + "\n", "tcpm_request_dfp_as_sink")
        self.assertLess(helper.index("tcpm_ams_start(port, DATA_ROLE_SWAP)"),
                        helper.index('tcpm_log(port, "Requesting DFP before discovery")'))
        block = block_after(self.doc, "In the log, after the contract")
        order = [block.index(line) for line in (
            "state change SNK_TRANSITION_SINK -> SNK_READY",
            "AMS DATA_ROLE_SWAP start",
            "state change SNK_READY -> AMS_START",
            "Requesting DFP before discovery",
            "state change AMS_START -> DR_SWAP_SEND",
            "state change DR_SWAP_SEND -> DR_SWAP_CHANGE_DR",
            "state change DR_SWAP_CHANGE_DR -> SNK_READY")]
        self.assertEqual(order, sorted(order))

    def test_numbers_are_the_code_s(self):
        driver = DRIVER.read_text()
        default = re.search(r"try_snk_extra_wait_ms = (\d+);", driver).group(1)
        limit = re.search(r"#define SM5714_TRY_SNK_EXTRA_WAIT_MAX_MS\s+(\d+)", driver).group(1)
        row = next(line for line in self.doc.splitlines()
                   if line.startswith("| `/sys/module/sm5714_usbpd/parameters/try_snk_extra_wait_ms`"))
        self.assertIn("| `%s` |" % default, row)
        self.assertIn("values above %s count as %s" % (limit, limit), row)
        self.assertIn("pending state change SNK_TRY_WAIT -> SRC_TRYWAIT @ %s ms" % default,
                      block_after(self.doc, "A dock seen as a sink first"))
        tries = int(re.search(r"#define SINK_DFP_SWAP_TRIES\s+(\d+)", self.code).group(1))
        wait = re.search(r"#define SINK_DFP_SWAP_WAIT_MS\s+(\d+)", self.code).group(1)
        word = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five"}[tries]
        self.assertIn("up to %s requests per attach" % word, self.doc)
        self.assertIn("repeated once %s ms (tDRSwapWait)" % wait, self.doc)

    def test_the_wait_that_is_left_is_shown_as_less_than_the_whole(self):
        # After an Rp that did not stay the port waits for what is left of
        # the deadline, in steps of a jiffy (4 ms at this kernel's HZ=250).
        default = int(re.search(r"try_snk_extra_wait_ms = (\d+);", DRIVER.read_text()).group(1))
        block = block_after(self.doc, "should now show the port")
        self.assertEqual(block[0], "state change SNK_TRY_WAIT_DEBOUNCE_CHECK_VBUS -> SNK_TRY_WAIT")
        left = [int(n) for line in block
                for n in re.findall(r"^pending state change SNK_TRY_WAIT -> SRC_TRYWAIT @ (\d+) ms$", line)]
        self.assertEqual(len(left), 1, block)
        self.assertTrue(0 < left[0] < default and left[0] % 4 == 0, left)
        self.assertEqual(block[-1], "state change SNK_TRY_WAIT_DEBOUNCE_CHECK_VBUS -> SNK_ATTACHED")

    def test_it_does_not_claim_more_than_was_measured(self):
        # By hand one hub accepted the tablet's swap, twice.  With the patch
        # both docks did, on three attaches; the write-up gives the counts
        # and names what has not run on a tablet.
        flat = " ".join(self.doc.split())
        self.assertIn("both times it was tried (the Anker hub)", flat)
        self.assertIn("twice by the Satechi dock and once by the Anker hub", flat)
        self.assertNotIn("Both docks did this on every attach", flat)
        self.assertNotIn("once per attach", flat)
        self.assertNotIn("they have not run on a tablet yet", flat)
        self.assertIn("**Both are compiled into test kernels and have not run on a tablet yet.**", flat)
        self.assertIn("**This revision has not run on a tablet yet.**", flat)
        self.assertIn("**It has not run on a tablet yet**", flat)
        self.assertIn("This is read from the timing; nothing has watched the CC line itself.", flat)

    def test_links_resolve(self):
        for target in re.findall(r"\]\(([^)#]+)(?:#[^)]*)?\)", self.doc):
            if "://" not in target:
                self.assertTrue((DOC.parent / target).exists(), target)
        readme = (ROOT / "README.md").read_text()
        if "usb-c-dock-roles" in readme:
            self.assertIn("(docs/usb-c-dock-roles.md)", readme)


if __name__ == "__main__":
    unittest.main()
