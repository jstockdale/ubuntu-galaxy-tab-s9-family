"""The roles the tablet takes on a powered USB-C dock.

Two TCPM patches and the SM5714 policy that switches them on, and three fixes
for a swap asked for from userspace:

- request_dfp_as_sink: as PD sink, ask for the USB host role before discovery
  (a charge-through dock otherwise flaps over its own DR_Swap), a few times
  per attach at most;
- try_snk_extra_wait_ms: keep waiting for Rp in Try.SNK (the SM5714 reports a
  source after TCPM's 100 ms have run out, so a dual-role dock ended up being
  powered by the tablet), as one deadline, so that an Rp which goes away
  again does not end the attempt;
- no DATA_ROLE_SWAP is started while a VDM waits for its reply, a write to
  data_role that changed no role fails, and a swap that is still queued
  behind a message of the partner's is neither reported done before it is
  sent nor sent after the partner's own swap of the same kind (all for
  every TCPM port).

Two patches for what a boot with a dock attached showed on both tablets: the
USB power domains kept in retention across suspend, and a NULL check in the
dwc3-qcom glue for the hcd that a failed xHCI probe leaves without.

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
GCC_C = "drivers/clk/qcom/gcc-sm8550.c"
DWC3_C = "drivers/usb/dwc3/dwc3-qcom.c"
# (marker, patch) of the patches for the USB host after a docked boot.
GDSC = ("Retention across system suspend, as on sm8650", "gcc-sm8550-usb-gdscs-retention.patch")
DWC3_GUARD = ("no hcd and no root hub", "dwc3-qcom-check-hcd-before-reading-port-speed.patch")
# The SM-X710 line's patch to the same file, hooked before the guard there.
DWC3_OPPS = ("dwc3_qcom_set_pstate", "dwc3-qcom-drop-required-opps-vote-in-system-suspend.patch")

# tcpc_dev field -> the patch that declares it, in build order.
FIELDS = [
    ("adopt_retained_source_ufp", "tcpm-adopt-retained-source-ufp-role.patch"),
    ("consume_retained_sink_dfp", "tcpm-use-retained-sink-data-role.patch"),
    ("request_dfp_as_sink", "tcpm-request-dfp-as-sink-before-discovery.patch"),
    ("try_snk_extra_wait_ms", "tcpm-try-snk-extra-wait.patch"),
]

# The log line a later patch adds to tcpm.c -> that patch, in build order.
# These declare no field: they are for every TCPM port.
LATER = [
    ("VDM in progress, DATA_ROLE_SWAP not started", "tcpm-no-dr-swap-during-vdm.patch"),
    ("Data role swap ended with the role unchanged", "tcpm-dr-set-checks-data-role.patch"),
    ("Swap is still queued, left pending", "tcpm-keep-queued-swap-pending.patch"),
]
# The last one's second log line, where the partner's DR_Swap came first.
PARTNER_FIRST = "Partner swapped first, queued DR_SWAP_SEND dropped"
# And its lines for the other two kinds, where the port accepts the
# partner's PR_Swap or VCONN_Swap: (state, the next case, queued swap, line).
PARTNER_FIRST_ACCEPT = [
    ("PR_SWAP_ACCEPT", "PR_SWAP_SEND", "PR_SWAP_SEND",
     "Partner swapped first, queued PR_SWAP_SEND dropped"),
    ("VCONN_SWAP_ACCEPT", "VCONN_SWAP_SEND", "VCONN_SWAP_SEND",
     "Partner swapped first, queued VCONN_SWAP_SEND dropped"),
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
        self.assertEqual(tcpm, [(field, TCPM_H, patch) for field, patch in FIELDS] +
                         [(line, TCPM_C, patch) for line, patch in LATER])

    def test_marker_means_this_patch_and_no_earlier_one(self):
        # apply_unless skips a patch whose marker is already in the file: the
        # marker has to come from the patch itself and from none before it.
        for number, (field, patch) in enumerate(FIELDS):
            self.assertTrue((PATCHES / patch).is_file(), patch)
            self.assertIn(field, added(patch, TCPM_H), patch)
            for _, earlier in FIELDS[:number]:
                self.assertNotIn(field, added(earlier, TCPM_H), earlier)
        # The later ones are marked by their log line, in tcpm.c.  The marker
        # is a grep pattern: plain text, added once, by this patch alone.
        for number, (line, patch) in enumerate(LATER):
            self.assertTrue((PATCHES / patch).is_file(), patch)
            self.assertRegex(line, r"^[A-Za-z_, ]+$")
            self.assertEqual(added(patch, TCPM_C).count('tcpm_log(port, "%s");' % line), 1, patch)
            for _, earlier in FIELDS + LATER[:number]:
                self.assertNotIn(line, (PATCHES / earlier).read_text(), earlier)

    def test_tcpm_patches_touch_only_tcpm(self):
        for _, patch in FIELDS:
            files = re.findall(r"^\+\+\+ b/(\S+)", (PATCHES / patch).read_text(), re.M)
            self.assertEqual(sorted(files), [TCPM_C, TCPM_H], patch)
        for _, patch in LATER:
            files = re.findall(r"^\+\+\+ b/(\S+)", (PATCHES / patch).read_text(), re.M)
            self.assertEqual(files, [TCPM_C], patch)

    def test_usb_power_domain_patch_is_applied(self):
        # What lets the USB host live through the suspend cycle of a boot
        # with a dock attached.  Both tablets' branches carry it: the
        # SM-X710 lost its xHCI after such a cycle as the SM-X910 did.
        marker, patch = GDSC
        path = GCC_C
        self.assertTrue((PATCHES / patch).is_file())
        mine = [(marker, target) for marker, target, name in hooks() if name == patch]
        self.assertEqual(mine, [(marker, path)])
        # Nothing else patches the clock controller.
        self.assertEqual([name for _, target, name in hooks() if target == path], [patch])
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


class Dwc3GuardTests(unittest.TestCase):
    """The NULL check of the hcd in the dwc3-qcom glue, for both tablets."""

    def test_the_guard_is_hooked_last_on_its_file_and_after_the_power_domains(self):
        mine = [hook for hook in hooks() if hook[2] == DWC3_GUARD[1]]
        self.assertEqual(mine, [(DWC3_GUARD[0], DWC3_C, DWC3_GUARD[1])])
        # On the SM-X710 line the opps patch changes the file first and the
        # guard goes right after it; on the SM-X910 line nothing else
        # patches the file and the guard follows the power domain patch.
        order = hooks()
        guard = order.index(mine[0])
        on_file = [name for _, target, name in order if target == DWC3_C]
        if (PATCHES / DWC3_OPPS[1]).is_file():
            self.assertEqual(on_file, [DWC3_OPPS[1], DWC3_GUARD[1]])
            self.assertEqual(order[guard - 1], (DWC3_OPPS[0], DWC3_C, DWC3_OPPS[1]))
        else:
            self.assertEqual(on_file, [DWC3_GUARD[1]])
            self.assertEqual(order[guard - 1], (GDSC[0], GCC_C, GDSC[1]))

    def test_the_marker_is_this_patch_s_alone(self):
        # A grep pattern: plain text, added once, in the guard's comment, by
        # this patch and by nothing else in the checkout.
        marker, patch = DWC3_GUARD
        self.assertRegex(marker, r"^[A-Za-z ]+$")
        lines = added(patch, DWC3_C).splitlines()
        self.assertEqual([line for line in lines if marker in line], ["\t * there is no hcd and no root hub to read the speed from."])
        self.assertEqual([path.name for path in PATCHES.glob("*.patch") if marker in path.read_text()],
                         [patch])
        for source in (ROOT / "kernel/drivers").rglob("*"):
            if source.is_file():
                self.assertNotIn(marker, source.read_text(errors="replace"), source)

    def test_the_guard_changes_dwc3_qcom_alone(self):
        text = (PATCHES / DWC3_GUARD[1]).read_text()
        self.assertEqual(re.findall(r"^\+\+\+ b/(\S+)", text, re.M), [DWC3_C])
        # It only adds: no line of the file is removed or changed.
        body = text[text.index("\n--- a/"):].splitlines()[2:]
        self.assertEqual([line for line in body if line.startswith("-")], [])


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
        # after Rd (port forced to sink); inside Try.SNK the reports came 123
        # to 147 ms after Rd.  For a dock that tries the sink role first
        # itself the estimate was about 515 ms; the one time it was seen the
        # lasting Rp came 407 ms after Rd.
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
        self.assertRegex(toggling, r"\tsm5714_usbpd_update_policy\(sm\);\n"
                                   r"\tsm->policy_open = true;\n")

    def test_a_parameter_written_at_an_empty_port_applies_from_the_next_attach(self):
        # start_toggling() has run by the time the port sits empty, so what is
        # written then would come one attach late.  Every get_cc() up to the
        # first that reports a partner refreshes the copies; that one closes
        # them, so that nothing changes under an attach.
        latch = function(self.source, "sm5714_usbpd_latch_policy")
        self.assertRegex(latch, r"^\tif \(!sm->policy_open\)\n"
                                r"\t\treturn;\n\n"
                                r"\tsm5714_usbpd_update_policy\(sm\);\n"
                                r"\tif \(attached\)\n"
                                r"\t\tsm->policy_open = false;$")
        # Opened where the port starts to toggle and closed by the latch, in
        # no other place.
        self.assertEqual(len(re.findall(r"policy_open = ", self.source)), 2)
        # get_cc() goes through the latch on every way out that has read the
        # CC state; an audio accessory is a partner, an open CC line is none.
        get_cc = function(self.source, "sm5714_usbpd_get_cc")
        latched = re.findall(r"\tsm5714_usbpd_latch_policy\(sm, (.+)\);\n\t+return 0;", get_cc)
        self.assertEqual(latched, ["true", "active != TYPEC_CC_OPEN"])
        self.assertEqual(get_cc.count("return 0;"), 2)
        self.assertEqual(self.source.count("sm5714_usbpd_latch_policy(sm, "), 2)
        # The policy is read and written under the TCPM port lock only: from
        # probe before the port exists, from start_toggling() and from the
        # latch.
        self.assertEqual(self.source.count("sm5714_usbpd_update_policy(sm);"), 3)

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
    """The seven TCPM patches on the pinned kernel, applied as the build does."""

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
        for marker, patch in FIELDS + LATER:
            marked = TCPM_H if (marker, patch) in FIELDS else TCPM_C
            assert marker not in (tree / marked).read_text(), marker
            with open(PATCHES / patch, "rb") as stream:
                cls.log[patch] = subprocess.run(
                    ["patch", "-d", str(tree), "-p1", "--no-backup-if-mismatch"],
                    stdin=stream, check=True, capture_output=True, text=True).stdout
        cls.source = (tree / TCPM_C).read_text()
        cls.header = (tree / TCPM_H).read_text()

    def test_new_patches_apply_exactly(self):
        # Fuzz or an offset in a state machine full of look-alike case blocks
        # is how a hunk lands in the wrong state.
        for _, patch in FIELDS[2:] + LATER:
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

    def test_no_data_role_swap_is_started_into_a_running_vdm(self):
        # A VDM exchange is an interruptible AMS; the DR_Swap sent into one
        # meets the VDM's reply and ends in a Soft Reset.  The refusal comes
        # after the check for an AMS that cannot be interrupted and before
        # anything is done for the new one, on either power role.
        start = function(self.source, "tcpm_ams_start")
        self.assertRegex(start, r"\tif \(ams == DATA_ROLE_SWAP && port->vdm_sm_running\) \{\n"
                                r"\t\tport->upcoming_state = INVALID_STATE;\n"
                                r"\t\ttcpm_log\(port, \"%s\"\);\n"
                                r"\t\treturn -EAGAIN;\n"
                                r"\t\}\n" % LATER[0][0])
        refusal = start.index("if (ams == DATA_ROLE_SWAP && port->vdm_sm_running) {")
        self.assertLess(start.index("not interruptible, aborting"), refusal)
        self.assertLess(refusal, start.index("if (port->pwr_role == TYPEC_SOURCE) {"))
        self.assertLess(refusal, start.index("port->ams = ams;"))
        self.assertEqual(self.source.count(LATER[0][0]), 1)
        # Two callers start this AMS: the request before discovery, which
        # waits for the VDM state machine itself, and a write to data_role.
        # A swap the partner asks for does not come this way.
        self.assertEqual(self.source.count("tcpm_ams_start(port, DATA_ROLE_SWAP)"), 2)
        self.assertIn("tcpm_ams_start(port, DATA_ROLE_SWAP)",
                      function(self.source, "tcpm_request_dfp_as_sink"))
        self.assertIn("ret = tcpm_ams_start(port, DATA_ROLE_SWAP);\n"
                      "\t\tif (ret == -EAGAIN) {\n"
                      "\t\t\tport->upcoming_state = INVALID_STATE;\n"
                      "\t\t\tgoto port_unlock;",
                      function(self.source, "tcpm_dr_set"))

    def test_a_write_to_data_role_that_changed_no_role_fails(self):
        # A ready state completes a pending swap with status 0, whatever
        # became of it.  Only a status of 0 is looked into, after the wait
        # and under the port lock, which the wait had given up.
        dr_set = function(self.source, "tcpm_dr_set")
        self.assertRegex(dr_set, r"\t\tret = port->swap_status;\n\n"
                                 r"(?:\t ?/?\*.*\n)+"
                                 r"\tif \(!ret\) \{\n"
                                 r"\t\tmutex_lock\(&port->lock\);\n"
                                 r"\t\tif \(port->data_role != data\) \{\n"
                                 r"\t\t\ttcpm_log\(port, \"%s\"\);\n"
                                 r"\t\t\tret = -EAGAIN;\n"
                                 r"\t\t\}\n"
                                 r"\t\tmutex_unlock\(&port->lock\);\n"
                                 r"\t\}\n\n"
                                 r"\tport->non_pd_role_swap = false;\n" % LATER[1][0])
        self.assertLess(dr_set.index("mutex_unlock(&port->lock);\n\n\tif (!wait_for_completion_timeout("),
                        dr_set.index("if (!ret) {"))
        self.assertEqual(self.source.count(LATER[1][0]), 1)
        # Power role and VCONN writes complete the same way and are left as
        # upstream has them.
        for name in ("tcpm_pr_set", "tcpm_vconn_set"):
            self.assertNotIn("if (!ret) {", function(self.source, name), name)

    def test_a_queued_swap_is_not_completed_before_it_is_sent(self):
        # A write puts its swap in upcoming_state and starts the AMS.  A
        # message of the partner's that is handled before AMS_START makes
        # the port leave the ready state and come back to it with the swap
        # still queued: that ready state must not tell the write that it is
        # done.  Left pending only while a write waits and a swap is what
        # is queued (its own, or the partner's request that took its
        # place); completed as upstream does in every other case.
        helper = function(self.source, "tcpm_swap_complete_ready")
        self.assertEqual(helper,
                         "\tif (tcpm_swap_is_queued(port)) {\n"
                         '\t\ttcpm_log(port, "%s");\n'
                         "\t\treturn;\n"
                         "\t}\n\n"
                         "\ttcpm_swap_complete(port, 0);" % LATER[2][0])
        self.assertEqual(self.source.count(LATER[2][0]), 1)
        # Queued means a swap state while a write waits: which ones, the
        # next test says.
        queued = function(self.source, "tcpm_swap_is_queued")
        self.assertRegex(queued, r"^\tif \(!port->swap_pending\)\n"
                                 r"\t\treturn false;\n\n"
                                 r"\tswitch \(port->upcoming_state\) \{\n"
                                 r"(?:\tcase [A-Z_]+:\n)+"
                                 r"\t\treturn true;\n"
                                 r"\tdefault:\n"
                                 r"\t\treturn false;\n"
                                 r"\t\}$")
        self.assertLess(self.source.index("static bool tcpm_swap_is_queued("),
                        self.source.index("static void tcpm_swap_complete_ready("))
        # Asked in the two ready states (through the helper) and where a
        # VCONN swap that turned VCONN on ends, nowhere else.
        self.assertEqual(self.source.count("tcpm_swap_is_queued(port)"), 2)
        # Both ready states complete through the helper, before they take
        # the queued state out of upcoming_state, and nothing else completes
        # a swap with status 0.
        machine = function(self.source, "run_state_machine")
        for label, following in (("SRC_READY", "SRC_WAIT_NEW_CAPABILITIES"),
                                 ("SNK_READY", "SNK_HARD_RESET_SINK_OFF")):
            ready = case(machine, label, following)
            self.assertEqual(ready.count("tcpm_swap_complete_ready(port);"), 1, label)
            self.assertLess(ready.index("tcpm_swap_complete_ready(port);"),
                            ready.index("upcoming_state = port->upcoming_state;"), label)
            self.assertNotIn("tcpm_swap_complete(port", ready, label)
        self.assertEqual(self.source.count("tcpm_swap_complete_ready(port);"), 2)
        self.assertEqual(self.source.count("tcpm_swap_complete(port, 0);"), 1)
        # A swap that was sent is completed by the states it ends in: the
        # refusal, the timeout, a cut connection.  Those are upstream's.
        for label, following, status in (("DR_SWAP_CANCEL", "FR_SWAP_CANCEL", "port->swap_status"),
                                         ("DR_SWAP_SEND_TIMEOUT", "DR_SWAP_CHANGE_DR", "-ETIMEDOUT")):
            self.assertIn("tcpm_swap_complete(port, %s);" % status,
                          case(machine, label, following), label)

    def test_a_write_waits_behind_the_partner_s_swap_request(self):
        # The write waits while its own swap is queued and about to be
        # sent, and while a swap request of the partner's took its place
        # there and is about to be accepted: answered before that swap, the
        # role check in tcpm_dr_set() runs while the Accept is sent and sees
        # the old role.  Every other queued state completes the write.
        queued = function(self.source, "tcpm_swap_is_queued")
        self.assertEqual(re.findall(r"^\tcase ([A-Z_]+):$", queued, re.M),
                         ["DR_SWAP_SEND", "PR_SWAP_SEND", "VCONN_SWAP_SEND",
                          "DR_SWAP_ACCEPT", "PR_SWAP_ACCEPT", "VCONN_SWAP_ACCEPT"])
        # A partner's request gets there through tcpm_pd_handle_state()
        # alone, which queues it when the port is not in a ready state.
        for state in ("DR_SWAP_ACCEPT", "PR_SWAP_ACCEPT", "VCONN_SWAP_ACCEPT"):
            with self.subTest(state):
                self.assertEqual(self.source.count("tcpm_pd_handle_state(port, %s," % state), 1)
                self.assertEqual(self.source.count("port->upcoming_state = %s;" % state), 0)

    def test_a_queued_dr_swap_does_not_undo_the_partner_s(self):
        # When the message that got in first is the partner's DR_Swap, the
        # port accepts it and has the role its queued DR_Swap was for; sent
        # after it, that one would put the role back.  Dropped where a swap
        # changes the role, before the next ready state acts on what is
        # queued.  A queued state of another kind stays.
        machine = function(self.source, "run_state_machine")
        changed = case(machine, "DR_SWAP_CHANGE_DR", "FR_SWAP_SEND")
        self.assertRegex(changed, r"\t\tif \(port->upcoming_state == DR_SWAP_SEND\) \{\n"
                                  r"\t\t\ttcpm_log\(port, \"%s\"\);\n"
                                  r"\t\t\tport->upcoming_state = INVALID_STATE;\n"
                                  r"\t\t\}\n" % PARTNER_FIRST)
        self.assertEqual(changed.count("upcoming_state"), 2)
        self.assertLess(changed.index("port->upcoming_state = INVALID_STATE;"),
                        changed.index("tcpm_set_state(port, ready_state(port), 0);"))
        self.assertEqual(self.source.count(PARTNER_FIRST), 1)
        self.assertEqual(added(LATER[2][1], TCPM_C).count('tcpm_log(port, "%s");' % PARTNER_FIRST), 1)
        # The port's own swap gets there with nothing queued: AMS_START takes
        # the state out of upcoming_state before it moves to it, and so do
        # the ready states.
        start = machine[machine.index("case AMS_START:"):]
        start = start[:start.index("break;\n\n")]
        self.assertTrue(start.rstrip().endswith(
            "upcoming_state = port->upcoming_state;\n"
            "\t\tport->upcoming_state = INVALID_STATE;\n"
            "\t\ttcpm_set_state(port, upcoming_state, 0);"), start)

    def test_a_queued_pr_or_vconn_swap_does_not_undo_the_partner_s(self):
        # The same for the other two kinds.  Only the partner's PR_Swap or
        # VCONN_Swap leads to these states, and the drop comes before the
        # Accept, while nothing else can run.  A queued state of another
        # kind stays.
        machine = function(self.source, "run_state_machine")
        for label, following, queued, line in PARTNER_FIRST_ACCEPT:
            with self.subTest(label):
                accept = case(machine, label, following)
                self.assertRegex(accept, r"\t\tif \(port->upcoming_state == %s\) \{\n"
                                         r"\t\t\ttcpm_log\(port, \"%s\"\);\n"
                                         r"\t\t\tport->upcoming_state = INVALID_STATE;\n"
                                         r"\t\t\}\n"
                                         r"\t\ttcpm_pd_send_control\(port, PD_CTRL_ACCEPT, TCPC_TX_SOP\);\n"
                                         % (queued, line))
                self.assertEqual(accept.count("upcoming_state"), 2)
                self.assertEqual(self.source.count(line), 1)
                self.assertEqual(added(LATER[2][1], TCPM_C).count('tcpm_log(port, "%s");' % line), 1)
                # Entered from the partner's request alone: no state change
                # and no queued state names it otherwise.
                self.assertEqual(self.source.count("tcpm_pd_handle_state(port, %s," % label), 1)
                self.assertNotRegex(self.source, r"tcpm_set_state(?:_cond)?\(port, %s\b|"
                                                 r"upcoming_state = %s;" % (label, label))

    def test_the_vconn_soft_reset_leaves_a_queued_swap_alone(self):
        # A VCONN swap that turns VCONN on ends here, the partner's as well
        # as the port's own.  A swap still queued behind the partner's has
        # not been sent; the port's own VCONN swap is no longer queued here
        # and is completed as before.
        machine = function(self.source, "run_state_machine")
        reset = case(machine, "VCONN_SWAP_SEND_SOFT_RESET", "DR_SWAP_CANCEL")
        self.assertRegex(reset, r"\*/\n"
                                r"\t\tif \(!tcpm_swap_is_queued\(port\)\)\n"
                                r"\t\t\ttcpm_swap_complete\(port, port->swap_status\);\n"
                                r"\t\tif \(tcpm_can_communicate_sop_prime\(port\)\) \{\n")
        self.assertEqual(reset.count("tcpm_swap_complete("), 1)

    def test_states_the_write_up_names_exist(self):
        names = set(re.findall(r"\b[A-Z]+(?:_[A-Z]+)+\b", DOC.read_text()))
        states = set(re.findall(r"^\tS\(([A-Z_]+)\),", self.source, re.M))
        for name in names:
            if re.match(r"(SNK|SRC|DR_SWAP|PR_SWAP|AMS|ERROR|PORT|HARD|SOFT)_", name):
                self.assertIn(name, states)


@unittest.skipUnless(kernel_source(), "no pinned kernel checkout")
class Dwc3GuardAppliedTests(unittest.TestCase):
    """The guard on the pinned dwc3-qcom.c, after what the build applies to
    that file before it."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        tree = Path(cls.tmp.name)
        (tree / DWC3_C).parent.mkdir(parents=True)
        (tree / DWC3_C).write_bytes(subprocess.run(
            ["git", "-C", str(kernel_source()), "show", "HEAD:" + DWC3_C],
            check=True, capture_output=True).stdout)
        cls.unpatched = (tree / DWC3_C).read_text()
        cls.log = {}
        for marker, target, patch in hooks():
            if target != DWC3_C:
                continue
            assert marker not in (tree / DWC3_C).read_text(), marker
            with open(PATCHES / patch, "rb") as stream:
                cls.log[patch] = subprocess.run(
                    ["patch", "-d", str(tree), "-p1", "--no-backup-if-mismatch"],
                    stdin=stream, check=True, capture_output=True, text=True).stdout
        cls.source = (tree / DWC3_C).read_text()

    def test_it_applies_without_fuzz(self):
        # On the SM-X710 line the opps patch above the function moves the
        # hunk by its four added lines; on the SM-X910 line it is exact.
        log = self.log[DWC3_GUARD[1]]
        self.assertNotIn("fuzz", log)
        if DWC3_OPPS[1] in self.log:
            self.assertEqual(re.findall(r"offset -?\d+ lines?", log), ["offset 4 lines"])
        else:
            self.assertNotIn("offset", log)

    def test_the_hcd_is_checked_before_the_root_hub_is_read(self):
        body = function(self.source, "dwc3_qcom_read_usb2_speed")
        guard = re.search(r"\thcd = platform_get_drvdata\(dwc->xhci\);\n\n"
                          r"\t/\*\n(?:\t \*.*\n)+\t \*/\n"
                          r"\tif \(!hcd\)\n"
                          r"\t\treturn USB_SPEED_UNKNOWN;\n\n"
                          r"#ifdef CONFIG_USB\n"
                          r"\tudev = usb_hub_find_child\(hcd->self\.root_hub, port_index \+ 1\);\n",
                          body)
        self.assertIsNotNone(guard, body)
        self.assertIn(DWC3_GUARD[0], body[:guard.end()])
        self.assertEqual(self.source.count(DWC3_GUARD[0]), 1)
        self.assertEqual(body.count("hcd->"), 1)
        # The unpatched function reads the root hub right after the drvdata.
        self.assertNotIn("if (!hcd)", function(self.unpatched, "dwc3_qcom_read_usb2_speed"))


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
        block = block_after(self.doc, "going back to waiting and taking the Rp that follows")
        self.assertEqual(block[0], "state change SNK_TRY_WAIT_DEBOUNCE_CHECK_VBUS -> SNK_TRY_WAIT")
        left = [int(n) for line in block
                for n in re.findall(r"^pending state change SNK_TRY_WAIT -> SRC_TRYWAIT @ (\d+) ms$", line)]
        self.assertEqual(len(left), 1, block)
        self.assertTrue(0 < left[0] < default and left[0] % 4 == 0, left)
        self.assertEqual(block[-1], "state change SNK_TRY_WAIT_DEBOUNCE_CHECK_VBUS -> SNK_ATTACHED")

    def test_it_does_not_claim_more_than_was_measured(self):
        # By hand one hub accepted the tablet's swap, twice.  With the patch
        # both docks did, on every attach whose log was kept; the write-up
        # gives the counts and names what has not been tried on a tablet.
        flat = " ".join(self.doc.split())
        self.assertIn("both times it was tried (the Anker hub)", flat)
        self.assertIn("each of the nine attaches whose log was kept, six times by the Satechi "
                      "dock and three times by the Anker hub", flat)
        self.assertNotIn("Both docks did this on every attach", flat)
        self.assertNotIn("once per attach", flat)
        self.assertNotIn("have not run on a tablet yet", flat)
        self.assertIn("**None of the three fixes and neither switch has been tried on a tablet "
                      "yet.**", flat)
        # True before any kernel with the newest changes is built: no build
        # date, no kernel that is said to wait for a tablet.
        self.assertIn("No kernel with the third fix or the NULL check has run on a tablet, and "
                      "the power domain change has run on the SM-X910 only.", flat)
        self.assertIn("the third fix and the NULL check are in no kernel that has run.", flat)
        for stale in ("the third is what the tree carries now", "The two fixes and the switches",
                      "Neither fix has been tried", "installed on neither",
                      "were built for both tablets", "No dock has been plugged into",
                      "has not been tried there yet", "Everything measured here comes from"):
            self.assertNotIn(stale, flat)
        self.assertIn("This is read from the timing; nothing has watched the CC line itself.", flat)

    def test_the_sm_x710_is_told_as_it_was_recorded(self):
        # 2026-11-21: a baseline with both docks, then a boot with the Anker
        # hub attached on the kit's kernel, whose next xHCI failed to start.
        flat = " ".join(self.doc.split())
        for said in ("On 2026-11-21 it was recorded with both docks on the kernel before the "
                     "kit, and booted with the Anker hub attached on the kit's kernel.",
                     "The SM-X710 needs it as well.",
                     "the probe of the new xHCI failed (`Host halt failed, -110`, "
                     "`can't setup: -110`), so no device came up.",
                     "That is one observation, with the SM-X910's signature.",
                     "The SM-X710's kernel kit now carries the patch, and that kernel has not "
                     "run on the tablet.",
                     "A boot whose bus stays empty does not put the host controller to the test.",
                     "Not done on the SM-X710: `dock` and `swap` runs on a kit kernel, the "
                     "Satechi on a kit kernel, and a boot with the dock attached and devices on "
                     "the bus.",
                     "Idle and suspend power with retention on the SM-X710 are not measured.",
                     "The Anker hub cannot serve as the unpowered hub: without its charger it "
                     "enumerates nothing",
                     "With its charger it enumerated 13 devices on the SM-X910."):
            with self.subTest(said):
                self.assertIn(said, flat)

    def test_the_dwc3_qcom_guard_is_said_to_fix_the_oops_and_not_the_host(self):
        flat = " ".join(self.doc.split())
        self.assertIn("`%s`, for both tablets, treats a port without that data as empty, so the "
                      "suspend completes. It does not bring the host controller back"
                      % DWC3_GUARD[1], flat)
        self.assertIn("It has not run on a tablet.", flat)

    def test_the_fixes_for_a_write_are_described_as_the_patches_have_them(self):
        flat = " ".join(self.doc.split())
        # Each fix by its patch and its log lines.
        for line, patch in LATER:
            self.assertIn("`%s`" % patch, flat)
            self.assertIn("`%s`" % line, flat)
        self.assertIn("`%s`" % PARTNER_FIRST, flat)
        # The refusal comes while the VDM state machine runs, discovery or
        # not.
        self.assertIn("It now turns the swap down for as long as the VDM state machine runs "
                      "(`vdm_sm_running`: from the moment TCPM queues a VDM", flat)
        self.assertIn("can get `EAGAIN`** (`Resource temporarily unavailable`): while a VDM "
                      "exchange is under way, from the moment TCPM queues a VDM", flat)
        self.assertNotIn("discovery is running", flat)
        # The third came from the code, for every port and all three role
        # attributes; the drop is for the data role.
        self.assertIn("A third fix, `tcpm-keep-queued-swap-pending.patch`, comes from reading "
                      "the code after those two; no trace showed the fault.", flat)
        self.assertIn("A write to `data_role`, `power_role` or `vconn_source` puts its swap in "
                      "`upcoming_state`", flat)
        self.assertIn("All of it is for every TCPM port and all three role attributes. None "
                      "of it has been seen on a tablet.", flat)
        for _, _, _, line in PARTNER_FIRST_ACCEPT:
            self.assertIn("`%s`" % line, flat)
        self.assertIn("`VCONN_SWAP_SEND_SOFT_RESET`", flat)
        self.assertNotIn("the second for the data role", flat)
        # The gaps the fixes leave.
        self.assertIn("a VDM exchange that starts in that time is not looked at again.", flat)
        self.assertIn("so none of its checks run for it.", flat)
        self.assertIn("A Soft Reset or a Hard Reset does not complete a pending swap with an "
                      "error", flat)
        # What the power role and VCONN writes still go without.
        self.assertIn("Writes to `power_role` and `vconn_source` (`tcpm_pr_set()`, "
                      "`tcpm_vconn_set()`) have neither of the first two fixes", flat)
        self.assertIn("Of the three fixes only the third covers them", flat)

    def test_the_switches_are_taken_per_attach(self):
        # The latch opens where the port starts to toggle again, which an
        # error recovery does with the dock still plugged in.
        flat = " ".join(self.doc.split())
        self.assertIn("keeps what it read until the port looks for one again, so one attach is "
                      "never handled half with the old value and half with the new. An attach "
                      "is not the same as a plug-in.", flat)
        self.assertIn("A Soft Reset keeps the attach and the values with it, and so does a Hard "
                      "Reset that the port comes through attached.", flat)
        self.assertNotIn("until the port is empty again", flat)
        self.assertNotIn("one connection is never handled", flat)

    def test_links_resolve(self):
        for target in re.findall(r"\]\(([^)#]+)(?:#[^)]*)?\)", self.doc):
            if "://" not in target:
                self.assertTrue((DOC.parent / target).exists(), target)
        readme = (ROOT / "README.md").read_text()
        if "usb-c-dock-roles" in readme:
            self.assertIn("(docs/usb-c-dock-roles.md)", readme)


if __name__ == "__main__":
    unittest.main()
