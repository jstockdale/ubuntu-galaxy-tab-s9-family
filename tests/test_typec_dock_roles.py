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

And the swap back to sink (request_sink_as_source): as power source to a PD
partner, ask for its source capabilities now and then and for the sink role
when they say Unconstrained Power, bounded per attach and stopped by a swap
that fails; with the driver's fix that applies the tablet's own accepted
PR_Swap in place, as the partner's has always been.

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
# The swap back to sink: a tcpc_dev field again, hooked after all of the
# above, whose hunks it follows.
SWAP_BACK = ("request_sink_as_source", "tcpm-swap-back-to-sink.patch")
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


def board_hooks(board):
    """hooks() of a build with DEVICE=board: with the hooks of that board's
    `if [ "$DEVICE" = ... ]; then` blocks (indented one tab), in order."""
    text = BUILD.read_text().replace("\\\n", " ")
    found, gate = [], None
    for line in text.splitlines():
        opened = re.match(r'if \[ "\$DEVICE" = (\w+) \]; then$', line)
        if opened:
            gate = opened.group(1)
        elif line == "fi":
            gate = None
        elif gate is not None and re.match(r"(else|elif)\b", line):
            gate = "?"
        hook = re.match(r"(\t?)apply_unless '([^']+)'\s+(\S+)\s+(\S+)\s*$", line)
        if not hook or (hook.group(1) == "") != (gate is None):
            continue
        if gate == "?":
            raise AssertionError("a hook in the else branch of a board gate: " + line)
        if gate in (None, board):
            found.append(hook.groups()[1:])
    return found


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
                         [(line, TCPM_C, patch) for line, patch in LATER] +
                         [(SWAP_BACK[0], TCPM_H, SWAP_BACK[1])])
        # For both tablets: no board gate around it.
        for board in ("gts9uwifi", "gts9wifi"):
            self.assertIn((SWAP_BACK[0], TCPM_H, SWAP_BACK[1]), board_hooks(board), board)

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
        field, patch = SWAP_BACK
        self.assertTrue((PATCHES / patch).is_file(), patch)
        self.assertIn(field, added(patch, TCPM_H))
        for _, earlier in FIELDS + LATER:
            self.assertNotIn(field, (PATCHES / earlier).read_text(), earlier)

    def test_tcpm_patches_touch_only_tcpm(self):
        for _, patch in FIELDS:
            files = re.findall(r"^\+\+\+ b/(\S+)", (PATCHES / patch).read_text(), re.M)
            self.assertEqual(sorted(files), [TCPM_C, TCPM_H], patch)
        for _, patch in LATER:
            files = re.findall(r"^\+\+\+ b/(\S+)", (PATCHES / patch).read_text(), re.M)
            self.assertEqual(files, [TCPM_C], patch)
        files = re.findall(r"^\+\+\+ b/(\S+)", (PATCHES / SWAP_BACK[1]).read_text(), re.M)
        self.assertEqual(files, [TCPM_C, TCPM_H])

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
        # The guard is for both tablets and follows the power domain patch.
        # The opps patch changes the file first, on both tablets since
        # 2026-11-25 (the SM-X910's gcc is under CX through
        # gts9u-sync-state.dtso); it was gated to the SM-X710 before.
        self.assertTrue((PATCHES / DWC3_OPPS[1]).is_file())
        self.assertIn((DWC3_OPPS[0], DWC3_C, DWC3_OPPS[1]), hooks())
        for board in ("gts9wifi", "gts9uwifi"):
            with self.subTest(board):
                order = board_hooks(board)
                guard = order.index(mine[0])
                on_file = [name for _, target, name in order if target == DWC3_C]
                self.assertEqual(on_file, [DWC3_OPPS[1], DWC3_GUARD[1]])
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
        for field, _ in FIELDS + [SWAP_BACK]:
            self.assertRegex(self.source, r"tcpc\.%s\s*=" % field)

    def test_defaults_and_runtime_switches(self):
        self.assertIn("static bool sink_host_swap = true;", self.source)
        self.assertIn("module_param(sink_host_swap, bool, 0644);", self.source)
        self.assertIn("static unsigned int try_snk_extra_wait_ms = 600;", self.source)
        self.assertIn("module_param(try_snk_extra_wait_ms, uint, 0644);", self.source)
        self.assertIn("static bool swap_back_to_sink = true;", self.source)
        self.assertIn("module_param(swap_back_to_sink, bool, 0644);", self.source)
        self.assertIn("static bool own_pr_swap_in_place = true;", self.source)
        self.assertIn("module_param(own_pr_swap_in_place, bool, 0644);", self.source)

    def test_policy_is_bounded(self):
        policy = function(self.source, "sm5714_usbpd_update_policy")
        self.assertIn("sm->tcpc.request_dfp_as_sink = sink_host_swap;", policy)
        # The two switches of the swap back, latched with the others.
        self.assertIn("sm->tcpc.request_sink_as_source = swap_back_to_sink;", policy)
        self.assertIn("sm->own_swap_in_place = own_pr_swap_in_place;", policy)
        self.assertEqual(self.source.count("own_pr_swap_in_place;"), 1)
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
    """The eight TCPM patches on the pinned kernel, applied as the build does."""

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
        cls.upstream = (tree / TCPM_C).read_text()
        cls.log = {}
        for marker, patch in FIELDS + LATER + [SWAP_BACK]:
            marked = TCPM_H if (marker, patch) in FIELDS + [SWAP_BACK] else TCPM_C
            assert marker not in (tree / marked).read_text(), marker
            with open(PATCHES / patch, "rb") as stream:
                cls.log[patch] = subprocess.run(
                    ["patch", "-d", str(tree), "-p1", "--no-backup-if-mismatch"],
                    stdin=stream, check=True, capture_output=True, text=True).stdout
        cls.source = (tree / TCPM_C).read_text()
        cls.header = (tree / TCPM_H).read_text()

    def test_new_patches_apply_exactly(self):
        # Fuzz or an offset in a state machine full of look-alike case blocks
        # is how a hunk lands in the wrong state.  All eight: the two imported
        # ones as well, since the second lost its Hard Reset hunk (and its
        # offset of -9 lines with it).
        for _, patch in FIELDS + LATER + [SWAP_BACK]:
            self.assertNotRegex(self.log[patch], r"fuzz|offset", patch)

    def test_header_declares_every_field_once(self):
        for field, _ in FIELDS + [SWAP_BACK]:
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
                     "port->sink_dfp_not_before = 0;"):
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

    def test_a_hard_reset_returns_a_sink_to_ufp_as_upstream_does(self):
        # A Hard Reset puts both ends back to their default data roles,
        # Source/DFP and Sink/UFP, sent or received (both run
        # HARD_RESET_START).  The series kept a sink in the host role across
        # it with adopt_retained_source_ufp, unless the role came from a
        # DR_Swap: a sink that had become one through a PR_Swap from
        # Source/DFP was still host, the partner's first message after the
        # reset was a data role mismatch and the port went to error recovery
        # (SM-X910, Anker hub, 2026-11-26, swap_protocol_reset=N).  Both
        # Hard Reset states are upstream's again, byte for byte.
        machine = function(self.source, "run_state_machine")
        upstream = function(self.upstream, "run_state_machine")
        for label, following in (("SNK_HARD_RESET_SINK_OFF", "SNK_HARD_RESET_WAIT_VBUS"),
                                 ("SRC_HARD_RESET_VBUS_OFF", "SRC_HARD_RESET_VBUS_ON")):
            with self.subTest(label):
                self.assertEqual(case(machine, label, following), case(upstream, label, following))
        sink_off = case(machine, "SNK_HARD_RESET_SINK_OFF", "SNK_HARD_RESET_WAIT_VBUS")
        self.assertRegex(sink_off, r"tcpm_set_roles\(port, port->self_powered, TYPEC_STATE_USB, TYPEC_SINK,\n"
                                   r"\t+ +tcpm_data_role_for_sink\(port\)\);")
        self.assertNotIn("TYPEC_HOST", sink_off)
        # Nothing is left of the exception that decided which host role to
        # keep.
        self.assertNotIn("data_role_swapped", self.source)
        for _, patch in FIELDS + LATER + [SWAP_BACK]:
            self.assertNotIn("data_role_swapped", (PATCHES / patch).read_text(), patch)

    def test_a_partner_that_kept_ufp_is_taken_back_as_host_on_its_first_message(self):
        # The reboot case the retained-role patches exist for: a dock that
        # kept Source/UFP across a Hard Reset finds the sink UFP after it; its
        # first message then has the port adopt DFP in place, without an
        # error recovery.  Only as Sink/UFP, towards a Source/UFP message,
        # and only with the opt-in.
        rx = function(self.source, "tcpm_pd_rx_handler")
        self.assertRegex(rx, r"if \(port->tcpc->adopt_retained_source_ufp &&\n"
                             r"\t+ +port->pwr_role == TYPEC_SINK &&\n"
                             r"\t+ +port->data_role == TYPEC_DEVICE &&\n"
                             r"\t+ +\(header & PD_HEADER_PWR_ROLE\) &&\n"
                             r"\t+ +!\(header & PD_HEADER_DATA_ROLE\)\) \{\n")
        self.assertRegex(rx, r"ret = tcpm_set_roles\(port, true, TYPEC_STATE_USB,\n"
                             r"\t+ +TYPEC_SINK, TYPEC_HOST\);\n\t+if \(!ret\)\n\t+goto process_message;")
        # And the attach-time restore of a retained Sink/DFP role stays.
        attach = function(self.source, "tcpm_snk_attach")
        self.assertIn("port->tcpc->consume_retained_sink_dfp(port->tcpc);", attach)
        self.assertIn("Restoring DFP for retained Source/UFP attachment", attach)

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

    # The swap back to sink, on the applied tree.

    def test_swap_back_states_follow_get_sink_cap(self):
        self.assertIn("\tS(GET_SINK_CAP_TIMEOUT),\t\t\\\n"
                      "\tS(GET_SOURCE_CAP),\t\t\t\\\n"
                      "\tS(GET_SOURCE_CAP_TIMEOUT),\t\t\\\n", self.source)
        # The AMS was there already; nothing else starts it as source.
        self.assertEqual(self.source.count("tcpm_ams_start(port, GET_SOURCE_CAPABILITIES)"), 1)
        self.assertIn("tcpm_ams_start(port, GET_SOURCE_CAPABILITIES)",
                      function(self.source, "tcpm_swap_back_probe"))
        self.assertEqual(self.source.count("port->upcoming_state = GET_SOURCE_CAP;"), 1)

    def test_swap_back_question_is_source_only_and_has_a_timeout(self):
        machine = function(self.source, "run_state_machine")
        state = case(machine, "GET_SOURCE_CAP", "GET_SOURCE_CAP_TIMEOUT")
        # Only the swap back's own question, in its own AMS; probe_sent only
        # once it went out.
        self.assertRegex(state, r"case GET_SOURCE_CAP:\n"
                                r"(?:\t\t ?/?\*.*\n)+"
                                r"\t\tif \(port->pwr_role != TYPEC_SOURCE \|\| !port->swap_back.probing \|\|\n"
                                r"\t\t    port->ams != GET_SOURCE_CAPABILITIES\) \{\n"
                                r"\t\t\ttcpm_set_state\(port, ready_state\(port\), 0\);\n"
                                r"\t\t\tbreak;\n"
                                r"\t\t\}\n"
                                r"\t\ttcpm_swap_back_sent\(port, false\);\n"
                                r"\t\tret = tcpm_pd_send_control\(port, PD_CTRL_GET_SOURCE_CAP, TCPC_TX_SOP\);\n"
                                r"\t\tif \(ret\) \{\n"
                                r"\t\t\ttcpm_swap_back_probe_failed\(port, ret\);\n"
                                r"\t\t\ttcpm_set_state\(port, ready_state\(port\), 0\);\n"
                                r"\t\t\} else \{\n"
                                r"\t\t\tport->swap_back.probe_sent = true;\n"
                                r"\t\t\ttcpm_set_state_cond\(port, GET_SOURCE_CAP_TIMEOUT,\n"
                                r"\t+ +PD_T_SENDER_RESPONSE\);\n"
                                r"\t\t\}\n\t\tbreak;\n")
        self.assertEqual(self.source.count("probe_sent = true;"), 1)
        timeout = case(machine, "GET_SOURCE_CAP_TIMEOUT", "ERROR_RECOVERY")
        self.assertRegex(timeout, r"case GET_SOURCE_CAP_TIMEOUT:\n"
                                  r"\t\ttcpm_swap_back_probe_failed\(port, 0\);\n"
                                  r"\t\ttcpm_set_state\(port, ready_state\(port\), 0\);\n"
                                  r"\t\tbreak;\n")

    def test_swap_back_answer_finishes_the_ams(self):
        # Upstream took Source_Capabilities as source only through
        # tcpm_pd_handle_state(..., NONE_AMS), which from a waiting state is a
        # Soft Reset and from SRC_READY leaves SinkTxNG.  The answer to the
        # port's own question goes to SRC_READY with the AMS still set, whose
        # entry finishes it; every other case is upstream's.
        data = function(self.source, "tcpm_pd_data_request")
        self.assertRegex(data, r"if \(port->pwr_role == TYPEC_SOURCE\) \{\n"
                               r"\t+if \(port->ams == GET_SOURCE_CAPABILITIES &&\n"
                               r"\t+ +port->state == GET_SOURCE_CAP\) \{\n"
                               r"(?:\t+ ?/?\*.*\n)+"
                               r"\t+tcpm_swap_back_source_caps\(port\);\n"
                               r"\t+tcpm_set_state\(port, SRC_READY, 0\);\n"
                               r"\t+\} else if \(port->ams == GET_SOURCE_CAPABILITIES\) \{\n"
                               r"\t+tcpm_pd_handle_state\(port, SRC_READY, NONE_AMS, 0\);\n"
                               r"\t+\} else \{\n")
        ready = case(function(self.source, "run_state_machine"), "SRC_READY", "SRC_WAIT_NEW_CAPABILITIES")
        self.assertRegex(ready, r"if \(port->ams != NONE_AMS\)\n\t+tcpm_ams_finish\(port\);")

    def test_swap_back_refusals_do_not_soft_reset(self):
        # Reject, Wait and Not_Supported to Get_Source_Cap end the AMS in the
        # ready state; without their own case they reached the default one,
        # a Soft Reset.
        control = function(self.source, "tcpm_pd_ctrl_request")
        refused = control[control.index("case PD_CTRL_REJECT:"):control.index("case PD_CTRL_ACCEPT:")]
        self.assertRegex(refused, r"case GET_SOURCE_CAP:\n"
                                  r"\t+tcpm_swap_back_probe_refused\(port, type\);\n"
                                  r"\t+tcpm_set_state\(port, ready_state\(port\), 0\);\n"
                                  r"\t+break;\n")
        self.assertLess(refused.index("case GET_SOURCE_CAP:"), refused.index("default:"))

    def test_swap_back_ready_states_hook_after_queued_and_before_discovery(self):
        machine = function(self.source, "run_state_machine")
        for label, following in (("SRC_READY", "SRC_WAIT_NEW_CAPABILITIES"),
                                 ("SNK_READY", "SNK_HARD_RESET_SINK_OFF")):
            with self.subTest(label):
                ready = case(machine, label, following)
                self.assertEqual(ready.count("tcpm_swap_back_ready(port);"), 1)
                hook = ready.index("tcpm_swap_back_ready(port);")
                self.assertLess(ready.index("tcpm_set_state(port, upcoming_state, 0);"), hook)
                self.assertLess(hook, ready.index("6.4.4.3.1 Discover Identity"))
        self.assertEqual(self.source.count("tcpm_swap_back_ready(port);"), 2)

    def test_swap_back_reset_registration_and_teardown(self):
        reset = function(self.source, "tcpm_reset_port")
        self.assertEqual(reset.count("tcpm_swap_back_reset(port);"), 1)
        self.assertLess(reset.index("port->upcoming_state = INVALID_STATE;"),
                        reset.index("tcpm_swap_back_reset(port);"))
        register = function(self.source, "tcpm_register_port")
        self.assertLess(register.index("INIT_DEFERRABLE_WORK(&port->swap_back.work, tcpm_swap_back_work);"),
                        register.index("tcpm_init(port);"))
        unregister = function(self.source, "tcpm_unregister_port")
        self.assertRegex(unregister, r"port->registered = false;\n"
                                     r"(?:\t ?/?\*.*\n)+"
                                     r"\tcancel_delayed_work_sync\(&port->swap_back.work\);\n"
                                     r"\tkthread_destroy_worker\(port->wq\);\n"
                                     r"\tcancel_delayed_work_sync\(&port->swap_back.work\);\n")

    def test_swap_back_knows_whose_pr_swap_it_is(self):
        machine = function(self.source, "run_state_machine")
        send = case(machine, "PR_SWAP_SEND", "PR_SWAP_SEND_TIMEOUT")
        self.assertRegex(send, r"case PR_SWAP_SEND:\n(?:\t\t ?/?\*.*\n)*"
                               r"\t\ttcpm_swap_back_pr_send\(port\);\n"
                               r"\t\ttcpm_pd_send_control\(port, PD_CTRL_PR_SWAP, TCPC_TX_SOP\);\n")
        self.assertRegex(function(self.source, "tcpm_swap_back_pr_send"),
                         r"\tsb->pr_sent = sb->pr_queued;\n"
                         r"\tsb->pr_queued = false;\n"
                         r"\tif \(sb->pr_sent\)\n"
                         r"\t\ttcpm_swap_back_sent\(port, true\);")
        timeout = case(machine, "PR_SWAP_SEND_TIMEOUT", "PR_SWAP_START")
        self.assertLess(timeout.index("tcpm_swap_back_pr_answer(port, 0);"),
                        timeout.index("tcpm_swap_complete(port, -ETIMEDOUT);"))
        control = function(self.source, "tcpm_pd_ctrl_request")
        refused = control[control.index("case PD_CTRL_REJECT:"):control.index("case PD_CTRL_ACCEPT:")]
        self.assertRegex(case(refused, "PR_SWAP_SEND", "VCONN_SWAP_SEND"),
                         r"case PR_SWAP_SEND:\n\t+tcpm_swap_back_pr_answer\(port, type\);\n")
        accepted = control[control.index("case PD_CTRL_ACCEPT:"):control.index("case PD_CTRL_SOFT_RESET:")]
        self.assertRegex(case(accepted, "PR_SWAP_SEND", "VCONN_SWAP_SEND"),
                         r"case PR_SWAP_SEND:\n\t+tcpm_swap_back_pr_answer\(port, PD_CTRL_ACCEPT\);\n"
                         r"\t+tcpm_set_state\(port, PR_SWAP_START, 0\);\n")
        self.assertEqual(self.source.count("tcpm_swap_back_pr_answer(port, "), 3)
        # The partner's own PR_Swap: before upstream's drop of a queued one.
        accept = case(machine, "PR_SWAP_ACCEPT", "PR_SWAP_SEND")
        self.assertLess(accept.index("tcpm_swap_back_partner_swap(port);"),
                        accept.index("if (port->upcoming_state == PR_SWAP_SEND) {"))

    def test_swap_back_gives_way_to_a_write_of_source(self):
        # Before the state check, so that a write during a question still
        # counts; and before the early return for a role the port has.
        pr_set = function(self.source, "tcpm_pr_set")
        hook = pr_set.index("if (port->tcpc->request_sink_as_source && port->attached) {")
        self.assertLess(pr_set.index("if (port->port_type != TYPEC_PORT_DRP) {"), hook)
        self.assertLess(hook, pr_set.index("if (port->state != SRC_READY && port->state != SNK_READY) {"))
        self.assertLess(hook, pr_set.index("if (role == port->pwr_role) {"))
        block = pr_set[hook:pr_set.index("if (port->state != SRC_READY", hook)]
        self.assertRegex(block, r"if \(role == TYPEC_SOURCE\) \{\n"
                                r"\t+if \(port->swap_back.pr_queued &&\n"
                                r"\t+ +port->upcoming_state == PR_SWAP_SEND\)\n"
                                r"\t+port->upcoming_state = INVALID_STATE;\n"
                                r"\t+tcpm_swap_back_stop\(port,\n")
        self.assertIn("port->swap_back.pr_queued = false;", block)
        self.assertIn("port->swap_back.pr_due = false;", block)
        # The other writes are left as they were, but a preferred_role that
        # took resumes the questions where they left off.
        for name in ("tcpm_dr_set", "tcpm_vconn_set"):
            self.assertNotIn("swap_back", function(self.source, name), name)
        try_role = function(self.source, "tcpm_try_role")
        self.assertEqual(try_role.count("swap_back"), 1)
        self.assertRegex(try_role, r"\tport->try_snk_count = 0;\n(?:\t ?/?\*.*\n)+"
                                   r"\tif \(!ret\)\n\t\ttcpm_swap_back_kick\(port\);\n"
                                   r"\tmutex_unlock\(&port->lock\);\n")

    def test_toggling_starts_only_unattached_after_port_reset_set_cc(self):
        # sm5714_usbpd_start_toggling() drops the error recovery's detach:
        # that is right only if TCPM toggles when unattached, after
        # PORT_RESET's set_cc(), the detach's one user.
        machine = function(self.source, "run_state_machine")
        handler = function(self.source, "tcpm_pd_event_handler")
        self.assertEqual(self.source.count("tcpm_start_toggling(port, "), 3)
        self.assertIn("if (port->state == CHECK_CONTAMINANT) {\n"
                      "\t\t\t\tif (tcpm_start_toggling(port, tcpm_rp_cc(port)))", handler)
        self.assertEqual(machine.count("tcpm_start_toggling(port, "), 2)
        for label, following in (("SRC_UNATTACHED", "SRC_ATTACH_WAIT"), ("SNK_UNATTACHED", "SNK_ATTACH_WAIT")):
            self.assertIn("tcpm_start_toggling(port, ", case(machine, label, following), label)
        reset = case(machine, "PORT_RESET", "PORT_RESET_WAIT_OFF")
        self.assertRegex(reset, r"tcpm_reset_port\(port\);\n(?:.*\n)*?\t+tcpm_set_cc\(port, ")
        self.assertIn("tcpm_set_state(port, PORT_RESET_WAIT_OFF,", reset)
        self.assertNotIn("tcpm_start_toggling", reset)

    def test_swap_back_states_are_source_states_for_cc_and_vbus(self):
        for handler, first in (("_tcpm_cc_change", "SRC_ATTACHED"),
                               ("_tcpm_pd_vbus_off", "SRC_STARTUP"),
                               ("_tcpm_pd_vbus_vsafe0v", "SRC_STARTUP")):
            with self.subTest(handler):
                # The labels of the one case block that SRC_READY is in.
                body = function(self.source, handler)
                start = body.index("\tcase %s:\n" % first)
                run = re.match(r"(?:\tcase [A-Z_]+:\n)+", body[start:]).group(0)
                labels = re.findall(r"case ([A-Z_]+):", run)
                self.assertIn("GET_SOURCE_CAP", labels)
                self.assertIn("GET_SOURCE_CAP_TIMEOUT", labels)
                self.assertIn("SRC_READY", labels)


@unittest.skipUnless(kernel_source(), "no pinned kernel checkout")
class Dwc3GuardAppliedTests(unittest.TestCase):
    """The guard on the pinned dwc3-qcom.c, after what each tablet's build
    applies to that file before it."""

    BOARDS = ("gts9wifi", "gts9uwifi")

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        pristine = subprocess.run(
            ["git", "-C", str(kernel_source()), "show", "HEAD:" + DWC3_C],
            check=True, capture_output=True).stdout
        cls.unpatched = pristine.decode()
        cls.log, cls.source = {}, {}
        for board in cls.BOARDS:
            tree = Path(cls.tmp.name) / board
            (tree / DWC3_C).parent.mkdir(parents=True)
            (tree / DWC3_C).write_bytes(pristine)
            cls.log[board] = {}
            for marker, target, patch in board_hooks(board):
                if target != DWC3_C:
                    continue
                assert marker not in (tree / DWC3_C).read_text(), marker
                with open(PATCHES / patch, "rb") as stream:
                    cls.log[board][patch] = subprocess.run(
                        ["patch", "-d", str(tree), "-p1", "--no-backup-if-mismatch"],
                        stdin=stream, check=True, capture_output=True, text=True).stdout
            cls.source[board] = (tree / DWC3_C).read_text()

    def test_it_applies_without_fuzz(self):
        # The opps patch above the function moves the hunk by its four added
        # lines, on both tablets since 2026-11-25.
        for board in self.BOARDS:
            with self.subTest(board):
                self.assertIn(DWC3_OPPS[1], self.log[board])
                log = self.log[board][DWC3_GUARD[1]]
                self.assertNotIn("fuzz", log)
                self.assertEqual(re.findall(r"offset -?\d+ lines?", log), ["offset 4 lines"])

    def test_the_hcd_is_checked_before_the_root_hub_is_read(self):
        for board in self.BOARDS:
            with self.subTest(board):
                source = self.source[board]
                body = function(source, "dwc3_qcom_read_usb2_speed")
                guard = re.search(r"\thcd = platform_get_drvdata\(dwc->xhci\);\n\n"
                                  r"\t/\*\n(?:\t \*.*\n)+\t \*/\n"
                                  r"\tif \(!hcd\)\n"
                                  r"\t\treturn USB_SPEED_UNKNOWN;\n\n"
                                  r"#ifdef CONFIG_USB\n"
                                  r"\tudev = usb_hub_find_child\(hcd->self\.root_hub, port_index \+ 1\);\n",
                                  body)
                self.assertIsNotNone(guard, body)
                self.assertIn(DWC3_GUARD[0], body[:guard.end()])
                self.assertEqual(source.count(DWC3_GUARD[0]), 1)
                self.assertEqual(body.count("hcd->"), 1)
        # The unpatched function reads the root hub right after the drvdata.
        self.assertNotIn("if (!hcd)", function(self.unpatched, "dwc3_qcom_read_usb2_speed"))


def swap_back_constants():
    """The SWAP_BACK_* #defines of the swap back patch, as numbers."""
    code = added(SWAP_BACK[1], TCPM_C)
    found = {}
    for name, value in re.findall(r"^#define (SWAP_BACK_[A-Z_]+)\s+(.+)$", code, re.M):
        # A number, or a product of numbers in parentheses.
        number = 1
        for factor in value.strip("()").split("*"):
            number *= int(re.fullmatch(r"\s*(\d+)U?\s*", factor).group(1))
        found[name] = number
    return found


class SwapBackPatchTests(unittest.TestCase):
    """tcpm-swap-back-to-sink.patch, read as text: who it asks, when, how
    often, and what stops it."""

    def setUp(self):
        self.code = added(SWAP_BACK[1], TCPM_C) + "\n"
        self.const = swap_back_constants()

    def test_opt_in_first_and_every_condition_under_the_lock(self):
        wanted = function(self.code, "tcpm_swap_back_wanted")
        self.assertTrue(wanted.lstrip().startswith(
            "struct swap_back_data *sb = &port->swap_back;\n\n"
            "\treturn port->tcpc->request_sink_as_source && port->registered &&"), wanted)
        for condition in ("port->attached", "port->pwr_role == TYPEC_SOURCE",
                          "port->port_type == TYPEC_PORT_DRP",
                          "port->try_role == TYPEC_SINK", "port->pd_capable",
                          "port->explicit_contract", "sb->armed", "!sb->stopped",
                          "sb->strikes < SWAP_BACK_MAX_STRIKES"):
            self.assertIn(condition, wanted)
        work = function(self.code, "tcpm_swap_back_work")
        self.assertLess(work.index("mutex_lock(&port->lock);"),
                        work.index("if (!tcpm_swap_back_wanted(port))"))
        # The hooks the state machine calls do nothing without the flag.
        for name in ("tcpm_swap_back_ready", "tcpm_swap_back_halt"):
            self.assertIn("if (!port->tcpc->request_sink_as_source)\n\t\treturn", function(self.code, name))

    def test_nothing_goes_out_into_a_busy_port(self):
        work = function(self.code, "tcpm_swap_back_work")
        self.assertRegex(work, r"if \(port->state != SRC_READY\)\n\t\tgoto unlock;\n\n"
                               r"\tif \(port->vdm_sm_running \|\| port->send_discover \|\|\n"
                               r"\t    port->send_discover_prime \|\| port->ams != NONE_AMS \|\|\n"
                               r"\t    port->upcoming_state != INVALID_STATE \|\|\n"
                               r"\t    port->delayed_state != INVALID_STATE\) \{\n"
                               r"\t\ttcpm_swap_back_retry\(port\);\n")
        self.assertLess(work.index("port->delayed_state != INVALID_STATE"),
                        work.index("tcpm_swap_back_request(port);"))
        # A busy port is asked every second for a minute, then on the ladder.
        retry = function(self.code, "tcpm_swap_back_retry")
        self.assertRegex(retry, r"if \(ktime_ms_delta\(now, sb->busy_since\) < SWAP_BACK_BUSY_MAX_MS\) \{\n"
                                r"\t\ttcpm_swap_back_schedule\(port, SWAP_BACK_RETRY_MS\);\n"
                                r"\t\treturn;\n\t\}\n"
                                r"\tsb->busy_since = 0;\n"
                                r"\ts = tcpm_swap_back_backoff\(port\);\n")
        self.assertIn("sb->busy_since = 0;", function(self.code, "tcpm_swap_back_sent"))
        # Both starts go through tcpm_ams_start() (PD 3 collision avoidance),
        # and one that cannot start leaves nothing queued.
        for name, state, ams in (("tcpm_swap_back_probe", "GET_SOURCE_CAP", "GET_SOURCE_CAPABILITIES"),
                                 ("tcpm_swap_back_request", "PR_SWAP_SEND", "POWER_ROLE_SWAP")):
            body = function(self.code, name)
            self.assertRegex(body, r"port->upcoming_state = %s;\n"
                                   r"\tif \(tcpm_ams_start\(port, %s\) == -EAGAIN\) \{\n"
                                   r"\t\tport->upcoming_state = INVALID_STATE;\n" % (state, ams), name)

    def test_the_questions_back_off_and_are_bounded(self):
        c = self.const
        self.assertGreaterEqual(c["SWAP_BACK_FIRST_MS"], 1000)
        self.assertGreaterEqual(c["SWAP_BACK_POLL_MIN_MS"], 10000)
        self.assertLessEqual(c["SWAP_BACK_POLL_MAX_MS"], 30 * 60 * 1000)
        self.assertLess(c["SWAP_BACK_POLL_MIN_MS"], c["SWAP_BACK_POLL_MAX_MS"])
        self.assertLessEqual(c["SWAP_BACK_MAX_REQUESTS"], 10)
        self.assertLessEqual(c["SWAP_BACK_MAX_ACCEPTED"], c["SWAP_BACK_MAX_REQUESTS"])
        self.assertTrue(1 <= c["SWAP_BACK_MAX_STRIKES"] <= 3)
        # A reconnect (error recovery, Try.SNK with its longest extra wait,
        # the attach debounces) is well inside the session's gap.
        self.assertGreaterEqual(c["SWAP_BACK_SESSION_GAP_MS"], 4 * (100 + 2000 + 200 + 200 + 480))
        self.assertGreater(c["SWAP_BACK_SINK_STABLE_MS"], c["SWAP_BACK_SINK_SHORT_MS"])
        self.assertGreaterEqual(c["SWAP_BACK_SINK_STABLE_MS"], 10 * 60 * 1000)
        # The refund needs a sink role that held; the strikes a longer one.
        self.assertTrue(c["SWAP_BACK_SINK_SHORT_MS"] < c["SWAP_BACK_SINK_REFUND_MS"]
                        < c["SWAP_BACK_SINK_STABLE_MS"])
        # A reset this soon after an unanswered question is laid to it; the
        # partner's own answer time (tSenderResponse) is well inside.
        self.assertTrue(30 < c["SWAP_BACK_CUT_MS"] < c["SWAP_BACK_SESSION_GAP_MS"])
        self.assertTrue(c["SWAP_BACK_RETRY_MS"] < c["SWAP_BACK_BUSY_MAX_MS"] <= c["SWAP_BACK_POLL_MAX_MS"])
        backoff = function(self.code, "tcpm_swap_back_backoff")
        self.assertIn("sb->interval_ms = min(2 * ms, SWAP_BACK_POLL_MAX_MS);", backoff)
        self.assertEqual(self.code.count("sb->interval_ms = SWAP_BACK_POLL_MIN_MS;"), 2)

    def test_it_never_wakes_the_tablet_and_sleeps_with_it(self):
        # A deferrable timer and a freezable workqueue, wherever it is queued.
        queued = re.findall(r"mod_delayed_work\((\w+), &sb->work,", self.code)
        self.assertEqual(set(queued), {"system_freezable_power_efficient_wq"})
        self.assertEqual(len(queued), 3)
        self.assertIn("INIT_DEFERRABLE_WORK(&port->swap_back.work, tcpm_swap_back_work);", self.code)
        self.assertNotIn("hrtimer", self.code)
        self.assertNotIn("queue_delayed_work(", self.code.replace("mod_delayed_work(", ""))

    def test_the_answers(self):
        caps = function(self.code, "tcpm_swap_back_source_caps")
        # The first PDO is only read once there is one, and must be fixed.
        self.assertLess(caps.index("!port->nr_source_caps"), caps.index("pdo = port->source_caps[0];"))
        self.assertRegex(caps, r"if \(pdo & PDO_FIXED_EXTPOWER\) \{\n(?:\t\t.*\n)+?"
                               r"\t\tsb->pr_due = true;\n\t\treturn;\n")
        self.assertNotIn("PDO_FIXED_DUAL_ROLE) ==", caps)
        self.assertIn("sb->partner_drp = pdo & PDO_FIXED_DUAL_ROLE;", caps)
        # Only Unconstrained Power leads to a PR_Swap: pr_due is set there,
        # and again only by a request that could not start, which runs only
        # when it was due.
        self.assertEqual(self.code.count("sb->pr_due = true;"), 2)
        request = function(self.code, "tcpm_swap_back_request")
        self.assertTrue(request.lstrip().split("\n\n")[1].startswith("\tsb->pr_due = false;"), request)
        self.assertIn("sb->pr_due = true;", request[request.index("== -EAGAIN) {"):])
        self.assertIn("\tif (sb->pr_due)\n\t\ttcpm_swap_back_request(port);",
                      function(self.code, "tcpm_swap_back_work"))
        # Reject, Not_Supported and no answer to Get_Source_Cap: a sink now,
        # asked again at the slow end of the ladder, never stopped.
        refused = function(self.code, "tcpm_swap_back_probe_refused")
        self.assertRegex(refused, r"if \(type != PD_CTRL_WAIT\) \{\n"
                                  r"\t\ttcpm_swap_back_no_source\(port, type == PD_CTRL_REJECT \?\n"
                                  r"\t+ +\"Reject\" : \"Not_Supported\"\);\n\t\treturn;\n\t\}\n")
        self.assertTrue(refused.rstrip().endswith('"Swap back: Get_Source_Cap answered Wait, asking again in %u s", s);'),
                        refused)
        failed = function(self.code, "tcpm_swap_back_probe_failed")
        self.assertRegex(failed, r"if \(ret == -EAGAIN\) \{\n\t\tsb->sent_at = 0;\n\t\treturn;\n\t\}\n")
        self.assertTrue(failed.rstrip().endswith('tcpm_swap_back_no_source(port, "nothing");'), failed)
        none = function(self.code, "tcpm_swap_back_no_source")
        self.assertNotIn("stop", none)
        self.assertRegex(none, r"\tsb->interval_ms = SWAP_BACK_POLL_MAX_MS;\n\ts = tcpm_swap_back_backoff\(port\);\n"
                               r"\tif \(sb->told_refused\) \{\n\t\ttcpm_log\(")
        answer = function(self.code, "tcpm_swap_back_pr_answer")
        self.assertTrue(answer.lstrip().split("\n\n")[1].startswith("\tif (!sb->pr_sent)\n\t\treturn;"), answer)
        self.assertRegex(answer, r"case PD_CTRL_ACCEPT:\n\t\tsb->accepted\+\+;\n\t\tsb->pending = true;\n")
        self.assertRegex(answer, r"case PD_CTRL_NOT_SUPP:\n\t\ttcpm_swap_back_stop\(")
        self.assertRegex(answer, r"case PD_CTRL_REJECT:\n\t\tif \(!sb->partner_drp\) \{\n\t\t\ttcpm_swap_back_stop\(")
        # Wait, Reject of a dual-role partner, and no answer: the ladder,
        # bounded by the requests.
        self.assertRegex(answer, r"\tif \(!tcpm_swap_back_wanted\(port\)\)\n\t\treturn;\n"
                                 r"\ts = tcpm_swap_back_backoff\(port\);\n"
                                 r"\ttcpm_swap_back_info\(port, \"PR_Swap answered %s, asking again in %u s\",\n")

    def test_a_failed_swap_stops_the_questions_and_counts(self):
        ready = function(self.code, "tcpm_swap_back_ready")
        source = ready[ready.index("if (port->pwr_role == TYPEC_SINK) {"):]
        source = source[source.index("\t\treturn;\n\t}\n"):]
        self.assertRegex(source, r"if \(sb->pending\) \{\n(?:\t\t/\*.*\n)?"
                                 r"\t\tsb->pending = false;\n\t\tsb->strikes\+\+;\n"
                                 r"\t\ttcpm_swap_back_stop\(")
        sink = ready[ready.index("if (port->pwr_role == TYPEC_SINK) {"):]
        sink = sink[:sink.index("\t\treturn;\n\t}\n")]
        self.assertIn("if (!port->explicit_contract)\n\t\t\treturn;", sink)
        self.assertRegex(sink, r"if \(sb->pending\) \{\n\t\t\tsb->pending = false;\n"
                               r"\t\t\tsb->sink_by_swap = true;\n")
        # The end of a sink stint: a lasting one clears the strikes (and at
        # a reset the stop); one from a swap gives the budget back after the
        # refund time, and is judged later when it was short.
        ended = function(self.code, "tcpm_swap_back_sink_ended")
        self.assertRegex(ended, r"if \(ms >= SWAP_BACK_SINK_STABLE_MS\) \{\n"
                                r"\t\tsb->strikes = 0;\n\t\tsb->requests = 0;\n"
                                r"\t\tsb->accepted = 0;\n\t\tif \(by_reset\) \{\n"
                                r"\t\t\tsb->stopped = false;\n")
        self.assertRegex(ended, r"\} else if \(sb->sink_by_swap && ms >= SWAP_BACK_SINK_REFUND_MS\) \{\n"
                                r"\t\tsb->requests = 0;\n\t\tsb->accepted = 0;\n"
                                r"\t\} else if \(sb->sink_by_swap && ms < SWAP_BACK_SINK_SHORT_MS\) \{\n"
                                r"\t\tsb->lost_early = true;\n\t\}\n")
        self.assertNotIn("strikes++", ended)
        self.assertIn("if (sb->sink_at)\n\t\ttcpm_swap_back_sink_ended(port, now, true);",
                      function(self.code, "tcpm_swap_back_reset"))
        self.assertIn("if (sb->sink_at)\n\t\ttcpm_swap_back_sink_ended(port, now, false);\n"
                      "\tif (sb->lost_early) {\n\t\tsb->lost_early = false;\n\t\tsb->strikes++;\n"
                      "\t\ttcpm_swap_back_stop(", source)
        # A new session (an unplug) drops a sink role lost early.
        self.assertEqual(self.code.count("sb->lost_early = false;"), 3)
        # Strikes are cleared nowhere else, and only by a lasting sink.
        self.assertEqual(self.code.count("sb->strikes = 0;"), 1)
        # Counted where a failed swap shows: source again after it, a sink
        # role lost early with the port source again, and a swap or a cut
        # short question still unsettled when its partner is gone.
        self.assertEqual(self.code.count("sb->strikes++;"), 3)

    def test_a_session_outlives_the_reconnect_of_a_failed_swap(self):
        ready = function(self.code, "tcpm_swap_back_ready")
        gap = re.search(r"if \(!sb->seen_ready\) \{\n\t\tsb->seen_ready = true;\n"
                        r"(?:\t\t ?/?\*.*\n)*"
                        r"\t\tif \(ktime_ms_delta\(now, sb->reset_at\) > SWAP_BACK_SESSION_GAP_MS\) \{\n"
                        r"((?:\t\t\t.*\n)+)\t\t\}\n"
                        r"(?:\t\t/\*.*\*/\n)?\t\tsb->unsettled = false;\n\t\}\n", ready)
        self.assertIsNotNone(gap, ready)
        self.assertTrue(gap.group(1).endswith("\t\t\tsb->pending = false;\n\t\t\tsb->lost_early = false;\n"
                                              "\t\t\tsb->requests = 0;\n\t\t\tsb->accepted = 0;\n"
                                              "\t\t\tsb->stopped = false;\n"
                                              "\t\t\tsb->told_no_power = false;\n"
                                              "\t\t\tsb->told_refused = false;\n"), gap.group(1))
        # The old partner's swap that never ended, or its question that a
        # reset cut short, counts against the strikes, so that the loop
        # breaker does not depend on how long a reconnect takes.
        self.assertRegex(gap.group(1), r"^\t\t\tif \(sb->pending \|\| sb->unsettled\) \{\n"
                                       r"\t\t\t\tsb->strikes\+\+;\n")
        # The reset keeps the session's counts, the strikes and a pending
        # verdict; it clears what belongs to one attach.
        reset = function(self.code, "tcpm_swap_back_reset")
        tail = reset[reset.index("sb->reset_at = now;"):]
        for kept in ("requests", "accepted", "stopped", "strikes", "pending ="):
            self.assertNotIn("sb->%s" % kept, tail)
        for cleared in ("armed", "seen_ready", "probing", "probe_sent", "pr_due", "pr_queued",
                        "pr_sent", "busy_since", "sent_at", "sink_at", "sink_by_swap"):
            self.assertIn("sb->%s = " % cleared, tail)
        self.assertIn("if (sb->pending)\n\t\tsb->pending_reset = true;", reset)
        # A question or a request on the wire, or sent just before and not
        # answered, stops the session at a reset and is remembered for the
        # next ready state.
        self.assertRegex(reset, r"\tif \(sb->probe_sent \|\| sb->pr_sent \|\|\n"
                                r"\t    \(sb->sent_at &&\n"
                                r"\t     ktime_ms_delta\(now, sb->sent_at\) < SWAP_BACK_CUT_MS\)\) \{\n"
                                r"\t\ttcpm_swap_back_stop\(port, \"%s cut short by a port reset\",\n"
                                r"(?:\t\t\t.*\n)+"
                                r"\t\tsb->unsettled = true;\n\t\}\n")
        self.assertLess(reset.index("sb->unsettled = true;"), reset.index("sb->reset_at = now;"))
        # Without a reset, in the same session: one that went out stops, one
        # that never did is asked again.
        self.assertRegex(ready, r"\tif \(sb->probe_sent \|\| sb->pr_sent\) \{\n(?:\t\t/\*.*\n)?"
                                r"\t\ttcpm_swap_back_stop\(port, \"%s cut short\",\n(?:\t+.*\n)+?"
                                r"\t\} else if \(sb->probing\) \{\n(?:\t\t/\*.*\n)?"
                                r"\t\tsb->probing = false;\n"
                                r"\t\tif \(tcpm_swap_back_wanted\(port\)\)\n"
                                r"\t\t\ttcpm_swap_back_retry\(port\);\n"
                                r"\t\} else if \(sb->pr_queued\) \{\n")
        # Boot time, so that a system sleep counts as time without a partner.
        self.assertNotIn("ktime_get()", self.code)

    def test_every_kernel_log_line_is_bounded(self):
        # dev_info() only in the two helpers, which format once (no macro
        # that could evaluate an argument twice), and a stop says so once.
        self.assertEqual(self.code.count("dev_info("), 2)
        self.assertNotIn("#define tcpm_swap_back", self.code)
        for name in ("tcpm_swap_back_info", "tcpm_swap_back_stop"):
            body = function(self.code, name)
            self.assertIn("__printf(2, 3)\nstatic void %s(struct tcpm_port *port, const char *fmt, ...)" % name,
                          self.code)
            self.assertEqual(body.count("vsnprintf(buf, sizeof(buf), fmt, args);"), 1, name)
            self.assertEqual(body.count("dev_info("), 1, name)
            self.assertEqual(body.count("buf);"), 2, name)
        stop = function(self.code, "tcpm_swap_back_stop")
        self.assertLess(stop.index("if (!tcpm_swap_back_halt(port))\n\t\treturn;"), stop.index("vsnprintf"))
        halt = function(self.code, "tcpm_swap_back_halt")
        self.assertRegex(halt, r"if \(sb->stopped\)\n\t\treturn false;\n\tsb->stopped = true;\n\treturn true;")
        # The per-probe line goes to the TCPM log only.
        caps = function(self.code, "tcpm_swap_back_source_caps")
        self.assertRegex(caps, r"if \(sb->told_no_power\) \{\n\t\ttcpm_log\(")


class SwapBackDriverTests(unittest.TestCase):
    """The SM5714 side: in-place PR_Swaps, the role bit, the Protocol Layer
    and the knobs that go with them."""

    def setUp(self):
        self.source = DRIVER.read_text()

    def param(self, name, kind, default):
        self.assertIn("static %s %s%s;" % (kind, name, default), self.source)
        self.assertIn("module_param(%s, %s, 0644);"
                      % (name, {"unsigned int": "uint"}.get(kind, kind)), self.source)

    def test_a_pr_swap_sent_is_remembered_for_its_answer(self):
        transmit = function(self.source, "sm5714_usbpd_transmit")
        self.assertRegex(transmit, r"case TCPC_TX_SOP:\n\t\trequest = 0x07;\n"
                                   r"\t\tctrl = !pd_header_cnt_le\(msg->header\);\n"
                                   r"\t\tmsg_type = pd_header_type_le\(msg->header\);\n")
        self.assertIn("\t\tsm->pr_swap_sent = ctrl && msg_type == PD_CTRL_PR_SWAP;\n", transmit)
        hard = transmit[transmit.index("if (type == TCPC_TX_HARD_RESET) {"):]
        self.assertTrue(hard.startswith("if (type == TCPC_TX_HARD_RESET) {\n"
                                        "\t\tsm5714_usbpd_forget_swap(sm, \"Hard Reset sent\");\n"
                                        "\t\tsm->recovery_detach_armed = true;\n"), hard)
        self.assertIn("sm->pr_swap_sent = false;", function(self.source, "sm5714_usbpd_clear_swap"))

    def test_an_accept_while_source_arms_the_in_place_swap(self):
        receive = function(self.source, "sm5714_usbpd_receive")
        arm = receive.index("if (sop == TCPC_TX_SOP && sm->pr_swap_sent) {")
        self.assertRegex(receive[arm:], r"^if \(sop == TCPC_TX_SOP && sm->pr_swap_sent\) \{\n"
                                        r"\t\tsm->pr_swap_sent = false;\n"
                                        r"\t\tif \(!count && type == PD_CTRL_ACCEPT && sm->own_swap_in_place &&\n"
                                        r"\t\t    !regmap_read\(sm->regmap, SM5714_REG_CC_STATUS, &status\) &&\n"
                                        r"\t\t    \(status & SM5714_CC_ATTACH_MASK\) == SM5714_CC_ATTACH_SINK\) \{\n"
                                        r"\t\t\tsm->pr_swap_src_to_snk = true;\n")
        # Before TCPM sees the Accept, and before the partner's own PR_Swap
        # and PS_RDY are looked at.
        self.assertLess(arm, receive.index("tcpm_pd_receive(sm->port, &msg, sop);"))
        self.assertLess(arm, receive.index("type == PD_CTRL_PR_SWAP"))
        # The tablet's own swap to source is left as it was.
        self.assertEqual(self.source.count("sm->pr_swap_snk_to_src = true;"), 1)

    def test_only_a_swap_that_goes_ahead_stays_armed(self):
        # Anything but Accept and PS_RDY from TCPM, and a Hard Reset either
        # way, drops an armed swap and releases its hold; set_cc(Rp) does not.
        transmit = function(self.source, "sm5714_usbpd_transmit")
        self.assertRegex(transmit, r"\t\tif \(!ctrl \|\| \(msg_type != PD_CTRL_ACCEPT &&\n"
                                   r"\t+ +msg_type != PD_CTRL_PS_RDY\)\)\n"
                                   r"\t\t\tsm5714_usbpd_forget_swap\(sm, \"TCPM sent another message\"\);\n")
        self.assertLess(transmit.index("sm5714_usbpd_forget_swap(sm, \"TCPM sent another message\");"),
                        transmit.index("sm->pr_swap_sent = ctrl && msg_type == PD_CTRL_PR_SWAP;"))
        irq = function(self.source, "sm5714_usbpd_irq")
        self.assertRegex(irq, r"if \(intr\[3\] & SM5714_INT4_HRST_RX\) \{\n"
                              r"\t\tsm5714_usbpd_forget_swap\(sm, \"Hard Reset received\"\);\n"
                              r"\t\tsm->recovery_detach_armed = true;\n"
                              r"\t\ttcpm_pd_hard_reset\(sm->port\);\n")
        forget = function(self.source, "sm5714_usbpd_forget_swap")
        self.assertRegex(forget, r"\tsm5714_usbpd_clear_swap\(sm\);\n\tif \(held\)\n"
                                 r"\t\tsm5714_usbpd_set_cc_hold\(sm, 0\);\n")
        self.assertEqual(self.source.count("sm5714_usbpd_forget_swap(sm, "), 3)
        set_cc = function(self.source, "sm5714_usbpd_set_cc")
        self.assertNotIn("forget_swap", set_cc)

    def test_the_role_bit_is_toggled_for_a_swap_and_cleared_everywhere_else(self):
        flip = function(self.source, "sm5714_usbpd_flip_cc_role")
        self.assertIn("role ^ SM5714_CC_ROLE_SWAPPED", flip)
        self.assertIn("#define  SM5714_CC_ROLE_SWAPPED\t\tBIT(0)", self.source)
        clear = function(self.source, "sm5714_usbpd_clear_cc_role")
        self.assertIn("regmap_update_bits(sm->regmap, SM5714_REG_CC_CNTL7,\n"
                      "\t\t\t\t  SM5714_CC_ROLE_SWAPPED, 0);", clear)
        # No parity any more: the bit is cleared, never toggled back.
        self.assertNotIn("cc_role_flipped", self.source)
        self.assertNotIn("restore_cc_role", self.source)
        # Toggled only by the two in-place swaps.
        self.assertEqual(self.source.count("sm5714_usbpd_flip_cc_role(sm);"), 2)
        # Cleared where the driver forgets a swap or forces a mode: CC open
        # (set_cc(OPEN) and the error recovery's detach, both through
        # sm5714_usbpd_open_cc()), toggling, both forced modes, the self-heal
        # and probe.
        set_cc = function(self.source, "sm5714_usbpd_set_cc")
        self.assertEqual(set_cc.count("sm5714_usbpd_clear_cc_role(sm);"), 2)
        self.assertEqual(set_cc.count("sm5714_usbpd_open_cc(sm);"), 2)
        self.assertIn("return sm5714_usbpd_clear_cc_role(sm);", function(self.source, "sm5714_usbpd_open_cc"))
        self.assertIn("sm5714_usbpd_clear_cc_role(sm);", function(self.source, "sm5714_usbpd_restart_cc"))
        for forced in ("SM5714_REG_CC_CNTL1, 0x45);", "SM5714_REG_CC_CNTL1,\n\t\t\t\t   0x49 |"):
            at = set_cc.index(forced)
            self.assertIn("sm5714_usbpd_clear_cc_role(sm);", set_cc[set_cc.rindex("break;", 0, at):at])
        toggling = function(self.source, "sm5714_usbpd_start_toggling")
        self.assertRegex(toggling, r"mutex_lock\(&sm->lock\);\n\tsm5714_usbpd_clear_swap\(sm\);\n"
                                   r"\tsm5714_usbpd_set_cc_hold\(sm, 0\);\n"
                                   r"\tsm5714_usbpd_clear_cc_role\(sm\);\n")
        probe = function(self.source, "sm5714_usbpd_probe")
        self.assertLess(probe.index("ret = sm5714_usbpd_clear_cc_role(sm);"),
                        probe.index("sm->retained_sink_dfp ="))
        self.assertIn("CC role bit left set by a swap before this boot", probe)
        self.assertLess(probe.index("if (role & SM5714_CC_ROLE_SWAPPED) {"),
                        probe.index("ret = sm5714_usbpd_clear_cc_role(sm);"))

    def test_the_hold_is_applied_once_and_released_by_the_ps_rdy(self):
        set_cc = function(self.source, "sm5714_usbpd_set_cc")
        self.assertRegex(set_cc, r"ret = sm5714_usbpd_set_cc_hold\(sm, 0x10\);\n"
                                 r"\t+if \(!ret\)\n\t+ret = sm5714_usbpd_flip_cc_role\(sm\);\n"
                                 r"\t+if \(!ret\) \{\n"
                                 r"\t+sm->pr_swap_src_to_snk = false;\n"
                                 r"\t+sm->pr_swap_hold = true;\n"
                                 r"\t+sm->recovery_detach_armed = true;\n")
        receive = function(self.source, "sm5714_usbpd_receive")
        self.assertRegex(receive, r"if \(sop == TCPC_TX_SOP && !count && type == PD_CTRL_PS_RDY &&\n"
                                  r"\t    sm->pr_swap_hold\) \{\n"
                                  r"\t\tsm->pr_swap_hold = false;\n\t\tswapped = true;\n"
                                  r"\t\tret = sm5714_usbpd_set_cc_hold\(sm, 0\);\n")
        # The freeze needs the swap armed, which it no longer is after the
        # pull change: a PORT_RESET's set_vbus(false) does not freeze again.
        self.assertIn("if (!on && sm->pr_swap_src_to_snk) {", function(self.source, "sm5714_usbpd_set_vbus"))

    def test_the_protocol_layer_is_reset_at_the_end_of_an_in_place_swap(self):
        self.param("swap_protocol_reset", "bool", " = true")
        reset = function(self.source, "sm5714_usbpd_reset_protocol")
        self.assertRegex(reset, r"ret = regmap_write\(sm->regmap, SM5714_REG_RX_BUF_ST, 0x10\);\n"
                                r"\tif \(!ret\)\n"
                                r"\t\tret = regmap_write\(sm->regmap, SM5714_REG_PD_CNTL4,\n"
                                r"\t+ +SM5714_PD_PROTOCOL_RESET\);\n")
        self.assertIn("#define  SM5714_PD_PROTOCOL_RESET\tBIT(3)", self.source)
        # Source-to-Sink: after the partner's PS_RDY is with TCPM and the RX
        # buffer is released.
        receive = function(self.source, "sm5714_usbpd_receive")
        tail = receive[receive.index("acknowledge:"):]
        self.assertRegex(tail, r"^acknowledge:\n\tregmap_write\(sm->regmap, SM5714_REG_RX_BUF, 0x80\);\n"
                               r"(?:\t/\*.*\n)?"
                               r"\tif \(swapped && READ_ONCE\(swap_protocol_reset\)\)\n"
                               r"\t\tsm5714_usbpd_reset_protocol\(sm, \"Source-to-Sink\"\);")
        # Sink-to-Source: when our PS_RDY of that swap got its GoodCRC,
        # before TCPM hears of it.
        set_cc = function(self.source, "sm5714_usbpd_set_cc")
        self.assertIn("sm->pr_swap_ps_rdy_due = true;", set_cc)
        transmit = function(self.source, "sm5714_usbpd_transmit")
        self.assertRegex(transmit, r"if \(ctrl && msg_type == PD_CTRL_PS_RDY && sm->pr_swap_ps_rdy_due\) \{\n"
                                   r"\t\t\tsm->pr_swap_ps_rdy_due = false;\n"
                                   r"\t\t\tsm->prl_reset_on_tx_done = true;\n")
        irq = function(self.source, "sm5714_usbpd_irq")
        done = irq[irq.index("if (intr[3] & SM5714_INT4_TX_DONE) {"):]
        self.assertLess(done.index("sm5714_usbpd_reset_protocol(sm, \"Sink-to-Source\");"),
                        done.index("tcpm_pd_transmit_complete(sm->port, TCPC_TX_SUCCESS);"))
        self.assertEqual(irq.count("sm->prl_reset_on_tx_done = false;"), 3)

    def test_a_hard_reset_is_reported_done_to_the_protocol_layer(self):
        rx = function(self.source, "sm5714_usbpd_set_pd_rx")
        self.assertRegex(rx, r"if \(on && !regmap_read\(sm->regmap, SM5714_REG_PD_STATE3, &state3\) &&\n"
                             r"\t    \(state3 & SM5714_PD_STATE3_RESET_WAIT\)\) \{\n"
                             r"\t\tret = regmap_write\(sm->regmap, SM5714_REG_PD_CNTL4,\n"
                             r"\t+ +SM5714_PD_RESET_DONE\);\n")
        self.assertLess(rx.index("SM5714_PD_RESET_DONE"), rx.index("SM5714_REG_PD_CNTL1"))
        self.assertIn("#define  SM5714_PD_STATE3_RESET_WAIT\tGENMASK(2, 1)", self.source)
        self.assertIn("#define  SM5714_PD_RESET_DONE\t\tBIT(0)", self.source)
        # The same test as at probe (0x06), in the register init that
        # tcpc->init() and the self-heal share.
        self.assertIn("if (state3 & 0x06) {", function(self.source, "sm5714_usbpd_init_regs"))

    def test_an_error_recovery_after_a_swap_or_hard_reset_is_a_detach(self):
        self.param("recovery_detach", "bool", " = true")
        set_cc = function(self.source, "sm5714_usbpd_set_cc")
        rd = set_cc[set_cc.index("case TYPEC_CC_RD:"):set_cc.index("case TYPEC_CC_RP_DEF:")]
        self.assertRegex(rd, r"SM5714_CC_ATTACH_SOURCE\) \{\n(?:\t+ ?/?\*.*\n)+"
                             r"\t+if \(!sm->recovery_detach_armed \|\| !recovery_detach\)\n\t+break;\n"
                             r"\t+ret = sm5714_usbpd_open_cc\(sm\);\n")
        opened = function(self.source, "sm5714_usbpd_open_cc")
        self.assertRegex(opened, r"^\tint ret;\n\n\tsm5714_usbpd_clear_swap\(sm\);\n"
                                 r"\tsm->recovery_detach_armed = false;\n"
                                 r"\tret = regmap_write\(sm->regmap, SM5714_REG_CC_CNTL3, 0x88\);\n")
        # Armed by both pull changes and by a Hard Reset either way; cleared
        # by a detach the chip saw, by a CC open (its own use included) and
        # whenever the port toggles again.
        self.assertEqual(self.source.count("sm->recovery_detach_armed = true;"), 4)
        self.assertEqual(self.source.count("sm->recovery_detach_armed = false;"), 3)
        irq = function(self.source, "sm5714_usbpd_irq")
        self.assertRegex(irq, r"if \(intr\[0\] & SM5714_INT1_DETACH\)\n\t\tsm->recovery_detach_armed = false;\n"
                              r"\n\tmutex_unlock\(&sm->lock\);")
        self.assertIn("ret = sm5714_usbpd_open_cc(sm);",
                      set_cc[set_cc.index("case TYPEC_CC_OPEN:"):set_cc.index("case TYPEC_CC_RD:")])

    def test_toggling_again_disarms_the_detach_for_every_port_type(self):
        # A Hard Reset sent after the IRQ handled the detach, or a detach the
        # chip never announced, must not carry the detach into the next
        # attach.  Under the lock, before the source-only and sink-only
        # branches (the latter goes through set_cc(RD)).
        toggling = function(self.source, "sm5714_usbpd_start_toggling")
        self.assertRegex(toggling, r"\tsm->policy_open = true;\n\n(?:\t ?/?\*.*\n)+"
                                   r"\tmutex_lock\(&sm->lock\);\n"
                                   r"\tsm->recovery_detach_armed = false;\n"
                                   r"\tmutex_unlock\(&sm->lock\);\n")
        clear = toggling.index("sm->recovery_detach_armed = false;")
        self.assertLess(clear, toggling.index("if (port_type == TYPEC_PORT_SRC)"))
        self.assertLess(clear, toggling.index("if (port_type == TYPEC_PORT_SNK)"))
        self.assertEqual(toggling.count("recovery_detach_armed"), 1)
        # Nothing else re-initialises it: tcpc->init runs once per probe, on a
        # zeroed struct, and tcpm_tcpc_reset() is not called by this driver.
        self.assertNotIn("recovery_detach_armed", function(self.source, "sm5714_usbpd_init"))
        self.assertNotIn("tcpm_tcpc_reset", self.source)
        self.assertIn("sm = devm_kzalloc(dev, sizeof(*sm), GFP_KERNEL);", self.source)

    def test_the_rd_delay_is_bounded_and_waits_outside_the_lock(self):
        self.param("pr_swap_rd_delay_ms", "unsigned int", "")
        self.assertIn("#define SM5714_PR_SWAP_RD_DELAY_MAX_MS\t500", self.source)
        set_cc = function(self.source, "sm5714_usbpd_set_cc")
        self.assertRegex(set_cc, r"delay = min\(READ_ONCE\(pr_swap_rd_delay_ms\),\n"
                                 r"\t+ +SM5714_PR_SWAP_RD_DELAY_MAX_MS\);\n"
                                 r"\tif \(cc == TYPEC_CC_RD && delay && READ_ONCE\(sm->pr_swap_src_to_snk\)\)\n"
                                 r"\t\tmsleep\(delay\);\n")
        self.assertLess(set_cc.index("msleep(delay);"), set_cc.index("mutex_lock(&sm->lock);"))

    def test_sink_tx_ok_is_rp_3_0_only_for_sink_tx_ok(self):
        # On by default since kit r9 (the user's decision, 2026-11-26).
        self.param("sink_tx_ok_rp", "bool", " = true")
        self.assertIn('"as PD 3 source, present Rp 3.0 A for SinkTxOK (default on)"', self.source)
        comment = self.source[:self.source.index("static bool sink_tx_ok_rp")]
        comment = comment[comment.rindex("/*"):]
        self.assertNotIn("Off by default", comment)
        self.assertIn("Clear this to present otg_rp for every Rp request", " ".join(
            line.lstrip(" *") for line in comment.splitlines()))
        self.assertIn("\tsm->sink_tx_ok = sink_tx_ok_rp;", function(self.source, "sm5714_usbpd_update_policy"))
        set_cc = function(self.source, "sm5714_usbpd_set_cc")
        natural = set_cc[set_cc.index("SM5714_CC_ATTACH_SINK) {", set_cc.index("case TYPEC_CC_RP_DEF:")):]
        natural = natural[:natural.index("break;")]
        self.assertRegex(natural, r"rp = sm->sink_tx_ok && cc == TYPEC_CC_RP_3_0 \?\n"
                                  r"\t+ +2 : otg_rp & 3;\n"
                                  r"\t+ret = regmap_update_bits\(sm->regmap,\n"
                                  r"\t+ +SM5714_REG_CC_CNTL1,\n"
                                  r"\t+ +GENMASK\(5, 4\), rp << 4\);\n")
        # Nowhere else.
        self.assertEqual(self.source.count("sm->sink_tx_ok"), 2)


# A small C program around the swap back's own code: the tcpm_swap_back_*
# functions, their struct and constants and the state machine cases they
# hook into, taken from a patched tcpm.c, with the kernel calls they make
# stubbed.  The scenarios play the partner and the rest of TCPM; time is in
# milliseconds (one jiffy each).  Built with the host compiler in a temporary
# directory.
HARNESS_HEAD = r"""
#include <stdarg.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

typedef uint32_t u32;
typedef int64_t s64;
typedef int64_t ktime_t;

#define __printf(a, b) __attribute__((format(printf, a, b)))
#define min(a, b) ((a) < (b) ? (a) : (b))
#define EAGAIN 11
#define ETIMEDOUT 110
#define PD_T_SENDER_RESPONSE 60
#define TCPC_TX_SOP 0

enum typec_role { TYPEC_SINK, TYPEC_SOURCE };
enum typec_port_type { TYPEC_PORT_SRC, TYPEC_PORT_SNK, TYPEC_PORT_DRP };
enum tcpm_state { INVALID_STATE, SRC_READY, SNK_READY, AMS_START, GET_SOURCE_CAP,
		  GET_SOURCE_CAP_TIMEOUT, PR_SWAP_ACCEPT, PR_SWAP_SEND,
		  PR_SWAP_SEND_TIMEOUT, PR_SWAP_START, ERROR_RECOVERY };
enum tcpm_ams { NONE_AMS, GET_SOURCE_CAPABILITIES, POWER_ROLE_SWAP };
enum pd_ctrl_msg_type { PD_CTRL_ACCEPT = 3, PD_CTRL_REJECT = 4, PD_CTRL_GET_SOURCE_CAP = 7,
			PD_CTRL_PR_SWAP = 10, PD_CTRL_WAIT = 12, PD_CTRL_NOT_SUPP = 16 };
#define PDO_TYPE_FIXED 0
#define pdo_type(p) ((p) >> 30)
#define PDO_FIXED_DUAL_ROLE (1u << 29)
#define PDO_FIXED_EXTPOWER (1u << 27)

static unsigned long jiffies = 1000000;
static ktime_t now_ns = 1000000LL * 1000000;
#define msecs_to_jiffies(m) ((unsigned long)(m))
#define time_before(a, b) ((long)((a) - (b)) < 0)
#define time_after(a, b) time_before(b, a)
static ktime_t ktime_get_boottime(void) { return now_ns; }
static s64 ktime_ms_delta(ktime_t a, ktime_t b) { return (a - b) / 1000000; }
static const char *str_yes_no(bool v) { return v ? "yes" : "no"; }

struct work_struct { int unused; };
struct delayed_work { struct work_struct work; };
#define system_freezable_power_efficient_wq 0
static long work_at = -1;
static bool mod_delayed_work(int wq, struct delayed_work *w, unsigned long delay)
{
	(void)wq; (void)w;
	work_at = (long)(jiffies + delay);
	printf("W %ld %lu\n", (long)jiffies, delay);
	return true;
}
static bool cancel_delayed_work(struct delayed_work *w) { (void)w; work_at = -1; return true; }
static bool delayed_work_pending(struct delayed_work *w) { (void)w; return work_at >= 0; }
#define to_delayed_work(w) ((struct delayed_work *)(w))
#define container_of(ptr, type, member) ((type *)((char *)(ptr) - offsetof(type, member)))
struct mutex { int unused; };
static void mutex_lock(struct mutex *m) { (void)m; }
static void mutex_unlock(struct mutex *m) { (void)m; }
struct device { int unused; };
struct tcpc_dev { bool request_sink_as_source; };
"""

HARNESS_PORT = r"""
struct tcpm_port {
	struct device *dev;
	struct tcpc_dev *tcpc;
	struct mutex lock;
	bool registered, attached, pd_capable, explicit_contract;
	bool vdm_sm_running, send_discover, send_discover_prime;
	enum typec_role pwr_role;
	enum typec_port_type port_type;
	int try_role;
	enum tcpm_state state, upcoming_state, delayed_state, next_state;
	enum tcpm_ams ams;
	u32 source_caps[7];
	unsigned int nr_source_caps;
	u32 supply_voltage, current_limit;
	struct swap_back_data swap_back;
};

__printf(2, 3) static void tcpm_log(struct tcpm_port *port, const char *fmt, ...)
{
	va_list args;

	(void)port;
	printf("T %ld ", (long)jiffies);
	va_start(args, fmt);
	vprintf(fmt, args);
	va_end(args);
	printf("\n");
}
__printf(2, 3) static void dev_info(struct device *dev, const char *fmt, ...)
{
	va_list args;

	(void)dev;
	printf("K %ld ", (long)jiffies);
	va_start(args, fmt);
	vprintf(fmt, args);
	va_end(args);
}
static int send_result, sends;
static enum pd_ctrl_msg_type last_sent;
static int tcpm_pd_send_control(struct tcpm_port *port, enum pd_ctrl_msg_type type, int sop)
{
	(void)port; (void)sop;
	if (type == PD_CTRL_GET_SOURCE_CAP || type == PD_CTRL_PR_SWAP) {
		sends++;
		last_sent = type;
		printf("S %ld %s\n", (long)jiffies, type == PD_CTRL_PR_SWAP ? "PRS" : "GSC");
	}
	return send_result;
}
static void tcpm_set_state(struct tcpm_port *port, enum tcpm_state s, unsigned int delay)
{
	if (delay)
		port->delayed_state = s;
	else
		port->next_state = s;
}
#define tcpm_set_state_cond tcpm_set_state
static void tcpm_swap_complete(struct tcpm_port *port, int result) { (void)port; (void)result; }
static int tcpm_ams_start(struct tcpm_port *port, enum tcpm_ams ams)
{
	port->ams = ams;
	port->delayed_state = AMS_START;
	return 0;
}
static enum tcpm_state ready_state(struct tcpm_port *port)
{
	return port->pwr_role == TYPEC_SOURCE ? SRC_READY : SNK_READY;
}
"""

HARNESS_CASES = r"""
static void run_case(struct tcpm_port *port)
{
	int ret;

	(void)ret;
	switch (port->state) {
@CASES@
	default:
		break;
	}
}
"""

HARNESS_MAIN = r"""
static struct tcpc_dev the_tcpc = { .request_sink_as_source = true };
static struct device the_dev;
static struct tcpm_port P;
#define POWERED (PDO_FIXED_DUAL_ROLE | PDO_FIXED_EXTPOWER | 0x0001912cu)
#define UNPOWERED (PDO_FIXED_DUAL_ROLE | 0x0001912cu)
#define MINUTE (60L * 1000)

static void advance(long ms) { jiffies += ms; now_ns += (ktime_t)ms * 1000000; }

static void goto_state(enum tcpm_state st)
{
	int guard;

	for (guard = 0; guard < 50; guard++) {
		P.state = st;
		P.next_state = INVALID_STATE;
		P.delayed_state = INVALID_STATE;
		if (st == SRC_READY || st == SNK_READY) {
			P.ams = NONE_AMS;
			if (P.upcoming_state != INVALID_STATE) {
				st = P.upcoming_state;
				P.upcoming_state = INVALID_STATE;
				continue;
			}
			tcpm_swap_back_ready(&P);
			return;
		}
		if (st == AMS_START) {
			st = P.upcoming_state != INVALID_STATE ? P.upcoming_state : ready_state(&P);
			P.upcoming_state = INVALID_STATE;
			continue;
		}
		run_case(&P);
		if (P.next_state == INVALID_STATE)
			return;
		st = P.next_state;
	}
	printf("X state loop\n");
}

/* The work at its time, then the AMS it started (tSinkTx: 16 ms) */
static bool run_work(long limit)
{
	if (work_at < 0 || work_at - (long)jiffies > limit)
		return false;
	advance(work_at - (long)jiffies);
	work_at = -1;
	tcpm_swap_back_work(&P.swap_back.work.work);
	if (P.delayed_state == AMS_START) {
		advance(16);
		goto_state(AMS_START);
	}
	return true;
}

/* What the work sends next within the limit, or 0 */
static int next_send(long limit)
{
	int before = sends;

	while (sends == before)
		if (!run_work(limit))
			return 0;
	return last_sent;
}

static void attach(enum typec_role role)
{
	printf("A %ld %s\n", (long)jiffies, role == TYPEC_SOURCE ? "source" : "sink");
	P.attached = true;
	P.pwr_role = role;
	P.pd_capable = true;
	P.explicit_contract = true;
	P.ams = NONE_AMS;
	goto_state(role == TYPEC_SOURCE ? SRC_READY : SNK_READY);
}

/* tcpm_reset_port(), as far as it matters */
static void reset_port(void)
{
	tcpm_swap_back_reset(&P);
	P.attached = false;
	P.pd_capable = false;
	P.explicit_contract = false;
	P.upcoming_state = INVALID_STATE;
	P.delayed_state = INVALID_STATE;
	P.state = INVALID_STATE;
	P.ams = NONE_AMS;
}

/* A reset in the middle of things: detach, VBUS lost, error recovery */
static void detach(void)
{
	printf("R %ld\n", (long)jiffies);
	reset_port();
}

/* The user pulls the cable */
static void unplug(void)
{
	printf("UNPLUG\n");
	reset_port();
}

static void answer_caps(u32 pdo)
{
	if (P.state != GET_SOURCE_CAP || P.ams != GET_SOURCE_CAPABILITIES) {
		printf("X caps out of turn\n");
		return;
	}
	P.nr_source_caps = 1;
	P.source_caps[0] = pdo;
	tcpm_swap_back_source_caps(&P);
	goto_state(SRC_READY);
}

static void refuse_probe(enum pd_ctrl_msg_type type)
{
	tcpm_swap_back_probe_refused(&P, type);
	goto_state(SRC_READY);
}

static void probe_timeout(void)
{
	advance(60);
	goto_state(GET_SOURCE_CAP_TIMEOUT);
}

static void answer_pr(enum pd_ctrl_msg_type type)
{
	tcpm_swap_back_pr_answer(&P, type);
	if (type == PD_CTRL_ACCEPT) {
		P.state = PR_SWAP_START;
		return;
	}
	goto_state(SRC_READY);
}

static void swap_done(void)
{
	advance(800);
	P.pwr_role = TYPEC_SINK;
	P.explicit_contract = true;
	P.supply_voltage = 9000;
	P.current_limit = 1660;
	goto_state(SNK_READY);
}

/* A dock with its supply back: answers with E, takes the swap, delivers */
static void good_swap(void)
{
	int sent;

	while ((sent = next_send(3600L * 1000)) == PD_CTRL_GET_SOURCE_CAP)
		answer_caps(POWERED);
	if (sent != PD_CTRL_PR_SWAP) {
		printf("X no PR_Swap\n");
		return;
	}
	answer_pr(PD_CTRL_ACCEPT);
	swap_done();
}

static void end(void)
{
	struct swap_back_data *sb = &P.swap_back;

	printf("END strikes=%u stopped=%d requests=%u accepted=%u\n",
	       sb->strikes, sb->stopped, sb->requests, sb->accepted);
}

int main(int argc, char **argv)
{
	const char *s = argc > 1 ? argv[1] : "";
	int i, sent;

	P.dev = &the_dev;
	P.tcpc = &the_tcpc;
	P.registered = true;
	P.port_type = TYPEC_PORT_DRP;
	P.try_role = TYPEC_SINK;
	P.upcoming_state = INVALID_STATE;
	reset_port();
	advance(10 * MINUTE);

	if (!strcmp(s, "probe_detach") || !strcmp(s, "probe_detach_slow")) {
		/* A partner that leaves the bus on every Get_Source_Cap */
		attach(TYPEC_SOURCE);
		for (i = 0; i < 8; i++) {
			if (!next_send(3600L * 1000))
				break;
			detach();
			advance(!strcmp(s, "probe_detach") ? 3000 : 70000);
			attach(TYPEC_SOURCE);
		}
	} else if (!strcmp(s, "prswap_detach")) {
		/* One that says it has a supply, and leaves the bus on every PR_Swap */
		attach(TYPEC_SOURCE);
		for (i = 0; i < 20; i++) {
			sent = next_send(3600L * 1000);
			if (!sent)
				break;
			if (sent == PD_CTRL_GET_SOURCE_CAP) {
				answer_caps(POWERED);
				continue;
			}
			detach();
			advance(3000);
			attach(TYPEC_SOURCE);
		}
	} else if (!strcmp(s, "ladder")) {
		attach(TYPEC_SOURCE);
		for (i = 0; i < 9 && next_send(3600L * 1000) == PD_CTRL_GET_SOURCE_CAP; i++)
			answer_caps(UNPOWERED);
	} else if (!strcmp(s, "prswap_wait")) {
		/* Powered, dual-role, and answers Wait to every PR_Swap */
		attach(TYPEC_SOURCE);
		for (i = 0; i < 40; i++) {
			sent = next_send(3600L * 1000);
			if (!sent)
				break;
			if (sent == PD_CTRL_GET_SOURCE_CAP)
				answer_caps(POWERED);
			else
				answer_pr(PD_CTRL_WAIT);
		}
	} else if (!strcmp(s, "accept_fail")) {
		/* Takes the swap but cannot supply: three plug-ins, then a long sink */
		for (i = 0; i < 3; i++) {
			attach(TYPEC_SOURCE);
			while ((sent = next_send(3600L * 1000)) == PD_CTRL_GET_SOURCE_CAP)
				answer_caps(POWERED);
			if (sent == PD_CTRL_PR_SWAP) {
				answer_pr(PD_CTRL_ACCEPT);
				advance(500);
				detach();
				advance(1300);
				attach(TYPEC_SOURCE);
				next_send(3600L * 1000);
			}
			unplug();
			advance(2 * MINUTE);
		}
		attach(TYPEC_SINK);
		advance(31 * MINUTE);
		unplug();
		advance(2 * MINUTE);
		attach(TYPEC_SOURCE);
		next_send(3600L * 1000);
	} else if (!strcmp(s, "unplug_after_swap")) {
		attach(TYPEC_SOURCE);
		good_swap();
		advance(30000);
		unplug();
		advance(2 * MINUTE);
		attach(TYPEC_SOURCE);
		if (next_send(3600L * 1000) == PD_CTRL_GET_SOURCE_CAP)
			answer_caps(UNPOWERED);
	} else if (!strcmp(s, "lost_early")) {
		/* The supply collapses under the tablet's load: source again at once */
		attach(TYPEC_SOURCE);
		good_swap();
		advance(30000);
		detach();
		advance(3000);
		attach(TYPEC_SOURCE);
		next_send(3600L * 1000);
	} else if (!strcmp(s, "refused_reject") || !strcmp(s, "refused_notsupp") ||
		   !strcmp(s, "refused_none")) {
		attach(TYPEC_SOURCE);
		while (next_send(3600L * 1000) == PD_CTRL_GET_SOURCE_CAP &&
		       (long)jiffies < 1000000 + 10 * MINUTE + 125 * MINUTE) {
			if (!strcmp(s, "refused_reject"))
				refuse_probe(PD_CTRL_REJECT);
			else if (!strcmp(s, "refused_notsupp"))
				refuse_probe(PD_CTRL_NOT_SUPP);
			else
				probe_timeout();
		}
	} else if (!strcmp(s, "refund")) {
		/* A supply that drops every 11 minutes */
		attach(TYPEC_SOURCE);
		for (i = 0; i < 6; i++) {
			good_swap();
			advance(11 * MINUTE);
			detach();
			advance(3000);
			attach(TYPEC_SOURCE);
		}
	} else {
		printf("X unknown scenario\n");
		return 2;
	}
	end();
	return 0;
}
"""


def swap_back_harness_source(tcpm_c):
    """The C harness around the swap back's code of a patched tcpm.c."""
    defines = "\n".join(re.findall(r"^#define SWAP_BACK_\w+\s+.+$", tcpm_c, re.M))
    start = tcpm_c.index("struct swap_back_data {")
    struct = tcpm_c[start:tcpm_c.index("\n};\n", start) + 4]
    start = tcpm_c.index("/*\n * Opt-in (tcpc->request_sink_as_source): the swap back to sink.")
    block = tcpm_c[start:tcpm_c.index("/*\n * VDM/VDO handling functions", start)]
    machine = function(tcpm_c, "run_state_machine")
    cases = "".join(case(machine, label, following) for label, following in (
        ("PR_SWAP_ACCEPT", "PR_SWAP_SEND"), ("PR_SWAP_SEND", "PR_SWAP_SEND_TIMEOUT"),
        ("PR_SWAP_SEND_TIMEOUT", "PR_SWAP_START"), ("GET_SOURCE_CAP", "GET_SOURCE_CAP_TIMEOUT"),
        ("GET_SOURCE_CAP_TIMEOUT", "ERROR_RECOVERY")))
    return (HARNESS_HEAD + defines + "\n" + struct + HARNESS_PORT + block +
            HARNESS_CASES.replace("@CASES@", cases) + HARNESS_MAIN)


def applied_tcpm(destination):
    """tcpm.c of the pinned kernel with the TCPM patches applied in build
    order, in a scratch tree under destination."""
    tree = Path(destination)
    for path in (TCPM_C, TCPM_H):
        (tree / path).parent.mkdir(parents=True, exist_ok=True)
        (tree / path).write_bytes(subprocess.run(
            ["git", "-C", str(kernel_source()), "show", "HEAD:" + path],
            check=True, capture_output=True).stdout)
    for _, patch in FIELDS + LATER + [SWAP_BACK]:
        with open(PATCHES / patch, "rb") as stream:
            subprocess.run(["patch", "-d", str(tree), "-p1", "--no-backup-if-mismatch"],
                           stdin=stream, check=True, capture_output=True)
    return (tree / TCPM_C).read_text()


def host_compiler():
    return shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")


def build_swap_back_harness(tcpm_c, directory):
    """Compile the harness for this tcpm.c; returns the binary's path."""
    source = Path(directory) / "swap_back_harness.c"
    binary = Path(directory) / "swap_back_harness"
    source.write_text(swap_back_harness_source(tcpm_c))
    subprocess.run([host_compiler(), "-std=gnu11", "-O0", "-w", "-o", str(binary), str(source)],
                   check=True, capture_output=True, text=True)
    return binary


def run_swap_back_harness(binary, scenario):
    """[(kind, ms, rest)] of one scenario's trace."""
    out = subprocess.run([str(binary), scenario], check=True, capture_output=True,
                         text=True, timeout=60).stdout
    trace = []
    for line in out.splitlines():
        kind, _, rest = line.partition(" ")
        if kind in ("S", "T", "K", "W", "R", "A"):
            ms, _, rest = rest.partition(" ")
            trace.append((kind, int(ms), rest))
        else:
            trace.append((kind, None, rest))
    return trace


@unittest.skipUnless(kernel_source() and host_compiler(), "no pinned kernel checkout or no C compiler")
class SwapBackBehaviourTests(unittest.TestCase):
    """What the swap back's own code does, run: the bounds and the ladder."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        source = applied_tcpm(Path(cls.tmp.name) / "tree")
        cls.binary = build_swap_back_harness(source, cls.tmp.name)
        cls.const = swap_back_constants()

    def run_scenario(self, name):
        trace = run_swap_back_harness(self.binary, name)
        self.assertNotIn("X", [kind for kind, _, _ in trace], trace)
        self.assertEqual(trace[-1][0], "END", trace[-5:])
        return trace

    @staticmethod
    def sends(trace, what=None):
        return [ms for kind, ms, rest in trace if kind == "S" and what in (None, rest)]

    @staticmethod
    def ending(trace):
        return dict(item.split("=") for item in trace[-1][2].split())

    def test_a_partner_that_leaves_on_every_question_is_asked_once_per_session(self):
        # Reconnecting 3 s later: the same session, one question in all.
        trace = self.run_scenario("probe_detach")
        self.assertEqual(len(self.sends(trace, "GSC")), 1)
        self.assertEqual(len([1 for kind, _, _ in trace if kind == "R"]), 1)
        self.assertIn("Get_Source_Cap cut short by a port reset",
                      " ".join(rest for kind, _, rest in trace if kind == "K"))
        # Away 70 s each time: a session each, the cut-short question is a
        # failed swap, and after two nothing is asked.
        trace = self.run_scenario("probe_detach_slow")
        self.assertEqual(len(self.sends(trace, "GSC")), self.const["SWAP_BACK_MAX_STRIKES"])
        self.assertIn("paused after 2 failed swaps", " ".join(rest for kind, _, rest in trace))

    def test_a_partner_that_leaves_on_every_pr_swap_gets_one(self):
        trace = self.run_scenario("prswap_detach")
        self.assertEqual(len(self.sends(trace, "PRS")), 1)
        self.assertIn("PR_Swap cut short by a port reset",
                      " ".join(rest for kind, _, rest in trace if kind == "K"))

    def test_the_ladder_is_the_one_both_logs_print(self):
        trace = self.run_scenario("ladder")
        probes = self.sends(trace, "GSC")
        attached = next(ms for kind, ms, _ in trace if kind == "A")
        # The first one after SWAP_BACK_FIRST_MS and the harness's tSinkTx (16 ms).
        self.assertEqual(probes[0] - attached, self.const["SWAP_BACK_FIRST_MS"] + 16)
        waits = [(b - a) // 1000 for a, b in zip(probes, probes[1:])]
        self.assertEqual(waits, [30, 60, 120, 240, 480, 600, 600, 600])
        # Each answer says the wait that follows it, in each log the same.
        said = [(kind, ms, int(re.search(r"asking again in (\d+) s", rest).group(1)))
                for kind, ms, rest in trace if kind in ("T", "K") and "asking again in" in rest]
        first = [entry for entry in said if entry[1] == said[0][1]]
        self.assertEqual(sorted(kind for kind, _, _ in first), ["K", "T"])
        self.assertEqual({seconds for _, _, seconds in first}, {30})
        for kind, ms, seconds in said:
            if ms < probes[-1]:
                following = next(p for p in probes if p > ms)
                self.assertEqual((following - ms) // 1000, seconds, (kind, ms))
        kernel = [rest for kind, _, rest in trace if kind == "K"]
        self.assertEqual(kernel, ["swap back to sink: partner has no external power now, asking again "
                                  "in 30 s, then less often down to every 600 s"])
        # Refusals of a dual-role partner climb the same ladder, one step each.
        trace = self.run_scenario("prswap_wait")
        swaps = self.sends(trace, "PRS")
        self.assertEqual(len(swaps), self.const["SWAP_BACK_MAX_REQUESTS"])
        said = [int(re.search(r"asking again in (\d+) s", rest).group(1))
                for kind, ms, rest in trace if kind == "K" and "answered Wait" in rest]
        self.assertEqual(said, [30, 60, 120, 240, 480, 600, 600, 600])

    def test_a_swap_that_fails_costs_one_reconnect_and_two_pause_until_a_long_sink(self):
        trace = self.run_scenario("accept_fail")
        plugs = [[]]
        for entry in trace:
            if entry[0] == "UNPLUG":
                plugs.append([])
            else:
                plugs[-1].append(entry)
        # Two plug-ins with a failed swap each: one reconnect (the reset of
        # the failure) and one PR_Swap per plug-in.
        for number in (0, 1):
            plug = plugs[number]
            self.assertEqual(len([1 for kind, _, _ in plug if kind == "R"]), 1, number)
            self.assertEqual(len(self.sends(plug, "PRS")), 1, number)
            self.assertIn("failed swap %d of 2" % (number + 1),
                          " ".join(rest for kind, _, rest in plug if kind == "K"))
        # The third asks nothing, nor does the long sink; after the sink the
        # questions start again.
        self.assertEqual(self.sends(plugs[2]), [])
        self.assertIn("paused after 2 failed swaps", " ".join(rest for _, _, rest in plugs[2]))
        self.assertEqual(self.sends(plugs[3]), [])
        self.assertEqual(len(self.sends(plugs[4], "GSC")), 1)

    def test_an_unplug_soon_after_a_good_swap_is_no_failure(self):
        trace = self.run_scenario("unplug_after_swap")
        self.assertEqual(self.ending(trace)["strikes"], "0")
        self.assertNotIn("failed swap", " ".join(rest for _, _, rest in trace))
        self.assertEqual(len(self.sends(trace, "GSC")), 2)
        # A supply that collapses under the load, with the tablet source again
        # within the session, is one.
        trace = self.run_scenario("lost_early")
        self.assertEqual(self.ending(trace)["strikes"], "1")
        self.assertIn("sink role lost within a minute of the swap, failed swap 1 of 2",
                      " ".join(rest for kind, _, rest in trace if kind == "K"))
        self.assertEqual(len(self.sends(trace)), 2)

    def test_a_sink_now_is_asked_every_ten_minutes_and_never_for_the_swap(self):
        for scenario, answer in (("refused_reject", "Reject"), ("refused_notsupp", "Not_Supported"),
                                 ("refused_none", "nothing")):
            with self.subTest(scenario):
                trace = self.run_scenario(scenario)
                probes = self.sends(trace, "GSC")
                self.assertEqual(self.sends(trace, "PRS"), [])
                self.assertGreaterEqual(len(probes), 10)
                waits = {(b - a) // 1000 for a, b in zip(probes, probes[1:])}
                self.assertEqual(waits, {600})
                kernel = [rest for kind, _, rest in trace if kind == "K"]
                self.assertEqual(kernel, ["swap back to sink: Get_Source_Cap answered %s, not a source "
                                          "now, asking again every 600 s" % answer])
                self.assertEqual(self.ending(trace)["stopped"], "0")

    def test_a_swap_that_held_ten_minutes_gives_the_budget_back(self):
        trace = self.run_scenario("refund")
        self.assertEqual(len(self.sends(trace, "PRS")), 6)
        accepted = [rest for kind, _, rest in trace if kind == "K" and "PR_Swap accepted" in rest]
        self.assertEqual(accepted, ["swap back to sink: PR_Swap accepted, swap 1 of 3"] * 6)
        self.assertEqual(self.ending(trace)["stopped"], "0")


class CcRecoveryDriverTests(unittest.TestCase):
    """The SM5714 after a CC open of its own (the recovery detach that left
    the SM-X910's port dead on 2026-11-26), the self-heal, and the attach
    watchdog that compares the chip with what TCPM was told."""

    def setUp(self):
        self.source = DRIVER.read_text()

    def param(self, name):
        self.assertIn("static bool %s = true;" % name, self.source)
        self.assertIn("module_param(%s, bool, 0644);" % name, self.source)

    def test_a_cc_open_is_left_through_unattached_snk(self):
        # Samsung turns CC on again with 0x82, never with 0x80; the probe path
        # and every docked boot go 0x88, 0x45/0x82, 0x40/0x80.  The runtime
        # path went 0x88, 0x40/0x80, and the chip detected nothing for 100
        # minutes.
        self.param("cc_reopen_via_snk")
        reopen = function(self.source, "sm5714_usbpd_reopen_cc")
        self.assertRegex(reopen, r"\tif \(!sm->cc_open \|\| !READ_ONCE\(cc_reopen_via_snk\)\)\n\t\treturn 0;\n\n"
                                 r"\tret = regmap_write\(sm->regmap, SM5714_REG_CC_CNTL1, 0x45\);\n"
                                 r"\tif \(!ret\)\n\t\tret = regmap_write\(sm->regmap, SM5714_REG_CC_CNTL3, 0x82\);\n")
        toggling = function(self.source, "sm5714_usbpd_start_toggling")
        order = [toggling.index(s) for s in ("sm5714_usbpd_clear_cc_role(sm);",
                                             "ret = sm5714_usbpd_reopen_cc(sm);",
                                             "SM5714_REG_CC_CNTL1, val);",
                                             "SM5714_REG_CC_CNTL3, 0x80);",
                                             "sm->cc_open = false;")]
        self.assertEqual(order, sorted(order))
        self.assertEqual(self.source.count("sm5714_usbpd_reopen_cc(sm)"), 1)
        # The chip is left in Unattached.SNK for no less than the 0.5 ms the
        # re-probe of 2026-11-26 had between its 0x82 and its 0x80, here and in
        # the self-heal, and nowhere else.
        self.assertIn("#define SM5714_CC_REOPEN_US\t\t1000", self.source)
        dwell = "\tusleep_range(SM5714_CC_REOPEN_US, 2 * SM5714_CC_REOPEN_US);\n"
        self.assertEqual(self.source.count("usleep_range("), 2)
        self.assertIn("SM5714_REG_CC_CNTL3, 0x82);\n\tif (ret)\n\t\treturn ret;\n" + dwell, reopen)
        self.assertIn("SM5714_REG_CC_CNTL3, 0x82);\n\tif (ret)\n\t\treturn ret;\n" + dwell +
                      "\tret = regmap_write(sm->regmap, SM5714_REG_CC_CNTL1, 0x40);\n",
                      function(self.source, "sm5714_usbpd_restart_cc"))
        # The open: CC off first, the role bit after it (not an in-place swap
        # back of its own while still attached), and the self-heal armed.
        opened = function(self.source, "sm5714_usbpd_open_cc")
        order = [opened.index(s) for s in ("SM5714_REG_CC_CNTL3, 0x88);", "sm->cc_open = true;",
                                           "sm->heal_armed = true;",
                                           "return sm5714_usbpd_clear_cc_role(sm);")]
        self.assertEqual(order, sorted(order))
        # Every 0x88 the driver writes marks the open: the helper's and the
        # probe's retained-dock reset.
        self.assertEqual(self.source.count("SM5714_REG_CC_CNTL3, 0x88);"), 2)
        probe = function(self.source, "sm5714_usbpd_probe")
        at = probe.index("SM5714_REG_CC_CNTL3, 0x88);")
        self.assertLess(probe.index("sm->cc_open = true;", at), probe.index("msleep(50);", at))
        # And every CC_CNTL3 write without bit 3 clears it: both forced modes,
        # toggling and the self-heal.
        set_cc = function(self.source, "sm5714_usbpd_set_cc")
        for forced in ("0x82);", "0x81);"):
            at = set_cc.index("SM5714_REG_CC_CNTL3,\n\t\t\t\t\t   " + forced)
            self.assertTrue(set_cc[at:].split("break;")[0].rstrip().endswith("sm->cc_open = false;"), forced)
        self.assertTrue(function(self.source, "sm5714_usbpd_restart_cc").rstrip().endswith(
            "if (!ret)\n\t\tsm->cc_open = false;\n\treturn ret;"))

    def test_the_register_init_is_one_and_the_same(self):
        init = function(self.source, "sm5714_usbpd_init")
        self.assertRegex(init, r"\tmutex_lock\(&sm->lock\);\n\tret = sm5714_usbpd_init_regs\(sm\);\n"
                               r"\tmutex_unlock\(&sm->lock\);\n\treturn ret;")
        regs = function(self.source, "sm5714_usbpd_init_regs")
        order = [regs.index(s) for s in ("SM5714_REG_CORR_CNTL5, 0x00);", "SM5714_REG_CORR_CNTL4, 0x00);",
                                         "SM5714_REG_PD_STATE3, &state3);", "SM5714_REG_RX_BUF_ST, 0x10);",
                                         "if (state3 & 0x06) {", "SM5714_REG_PD_CNTL4, 0x01);",
                                         "regmap_bulk_read(sm->regmap, SM5714_REG_INT1,",
                                         "regmap_bulk_write(sm->regmap, SM5714_REG_MASK1,")]
        self.assertEqual(order, sorted(order))
        self.assertRegex(self.source, r"static const u8 sm5714_usbpd_masks\[5\] = \{\n\t0xe6,[^\n]*\n"
                                      r"\t0xcf,[^\n]*\n\t0xff,\n\t0x08,[^\n]*\n\t0xff,\n\};")
        # The self-heal is what a re-probe writes on an empty port, in order.
        restart = function(self.source, "sm5714_usbpd_restart_cc")
        order = [restart.index(s) for s in ("ret = sm5714_usbpd_init_regs(sm);",
                                            "SM5714_REG_CC_CNTL1, 0x45);", "SM5714_REG_CC_CNTL3, 0x82);",
                                            "SM5714_REG_CC_CNTL1, 0x40);", "SM5714_REG_CC_CNTL3, 0x80);")]
        self.assertEqual(order, sorted(order))
        self.assertEqual(self.source.count("sm5714_usbpd_restart_cc(sm)"), 1)
        self.assertIn("heal = !sm5714_usbpd_restart_cc(sm);", function(self.source, "sm5714_usbpd_cc_watch_work"))

    def test_the_watchdog_compares_with_what_tcpm_was_told(self):
        # No edge memories any more: the stale ones hid the attach after a
        # forced detach shorter than a sample period.
        for gone in ("irq_saw_attach", "watch_saw_attach"):
            self.assertNotIn(gone, self.source)
        # TCPM toggles from a successful start_toggling() until it calls
        # set_cc() or set_roles(); a get_cc() that reports a partner is TCPM
        # being told.
        self.assertEqual(self.source.count("sm->toggling = true;"), 1)
        self.assertIn("sm->toggling = true;", function(self.source, "sm5714_usbpd_start_toggling"))
        self.assertEqual(self.source.count("sm->toggling = false;"), 2)
        for name in ("sm5714_usbpd_set_cc", "sm5714_usbpd_set_roles"):
            self.assertIn("sm->toggling = false;", function(self.source, name))
        self.assertEqual(self.source.count("sm->partner_seen = true;"), 1)
        self.assertIn("sm->partner_seen = true;", function(self.source, "sm5714_usbpd_note_cc"))
        get_cc = function(self.source, "sm5714_usbpd_get_cc")
        self.assertRegex(get_cc, r"ret = regmap_read\(sm->regmap, SM5714_REG_CC_STATUS, &cc\);\n"
                                 r"\tif \(ret\)\n\t\treturn ret;\n\tsm5714_usbpd_note_cc\(sm, cc\);\n")
        # A partner is what get_cc() reports as one, in both places.
        attached = function(self.source, "sm5714_usbpd_cc_attached")
        for kind in ("SOURCE", "SINK", "AUDIO"):
            self.assertIn("case SM5714_CC_ATTACH_%s:" % kind, attached)
            self.assertIn("case SM5714_CC_ATTACH_%s:" % kind, get_cc)
        # Nothing is read before the watchdog knows that TCPM toggles and has
        # not been told of a partner, and everything under the lock.
        watch = function(self.source, "sm5714_usbpd_cc_watch_work")
        guard = watch.index("if (!sm->toggling || sm->partner_seen) {")
        self.assertLess(watch.index("mutex_lock(&sm->lock);"), guard)
        self.assertLess(guard, watch.index("regmap_read("))
        self.assertLess(watch.rindex("regmap_read("), watch.index("unlock:\n\tmutex_unlock(&sm->lock);"))
        self.assertLess(watch.index("unlock:\n\tmutex_unlock(&sm->lock);"), watch.index("tcpm_cc_change(sm->port);"))
        # Two samples SM5714_CC_CONFIRM_MS apart, and the waits between
        # resyncs of an attachment TCPM did not take only grow, to 10 min.
        self.assertIn("#define SM5714_CC_CONFIRM_MS\t\t500", self.source)
        waits = [int(n) for n in re.search(r"sm5714_resync_wait_ms\[\] = \{ ([0-9, ]+) \};",
                                           self.source).group(1).split(", ")]
        self.assertEqual(waits, sorted(waits))
        self.assertEqual((waits[0], waits[-1]), (0, 600000))
        # The resyncs come back on a change the chip or TCPM shows.
        self.assertIn("sm->resyncs = 0;", function(self.source, "sm5714_usbpd_irq"))
        self.assertIn("if (attached)\n\t\tsm->resyncs = 0;", function(self.source, "sm5714_usbpd_set_roles"))

    def test_the_self_heal_is_bounded(self):
        self.param("cc_self_heal")
        waits = [int(n) for n in re.search(r"sm5714_heal_wait_ms\[\] = \{ ([0-9, ]+) \};",
                                           self.source).group(1).split(", ")]
        self.assertEqual(len(waits), 3)
        self.assertEqual(waits[0], 5000)
        watch = function(self.source, "sm5714_usbpd_cc_watch_work")
        self.assertRegex(watch, r"if \(!why \|\| !READ_ONCE\(cc_self_heal\) \|\|\n"
                                r"\t+ +sm->heal_tries >= ARRAY_SIZE\(sm5714_heal_wait_ms\) \|\|\n"
                                r"\t+ +time_before\(jiffies, sm->heal_after\)\)\n\t+goto unlock;")
        # Reset only where the chip detects again: a partner in get_cc() and
        # a fresh CC open of the driver's.
        self.assertEqual(self.source.count("sm->heal_tries = 0;"), 2)
        self.assertIn("sm->heal_tries = 0;", function(self.source, "sm5714_usbpd_note_cc"))
        self.assertIn("sm->heal_tries = 0;", function(self.source, "sm5714_usbpd_open_cc"))


# The driver itself on the host, around a model of the chip's CC logic, of the
# partner and of the few TCPM steps that matter here (fake_kernel.h stands in
# for the kernel headers).  The model has 0x88 turn the CC logic off and only
# a forced state (0x82, 0x81) turn it on again, the reading of 2026-11-26;
# the scenario short_detach_missed_irq has 0x80 do it too.  Time in ms.
FAKE_KERNEL_H = r"""/* Just enough of the kernel API for sm5714_usbpd.c on the host. */
#ifndef FAKE_KERNEL_H
#define FAKE_KERNEL_H
#include <stdarg.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef uint8_t u8;
typedef uint16_t u16;
typedef uint32_t u32;
typedef uint16_t __le16;
typedef uint32_t __le32;

#define BIT(n) (1U << (n))
#define GENMASK(h, l) (((~0U) << (l)) & (~0U >> (31 - (h))))
#define ARRAY_SIZE(a) (sizeof(a) / sizeof((a)[0]))
#define min(a, b) ((a) < (b) ? (a) : (b))
#define min_t(t, a, b) ((t)(a) < (t)(b) ? (t)(a) : (t)(b))
#define READ_ONCE(x) (x)
#define WRITE_ONCE(x, v) ((x) = (v))
#define container_of(ptr, type, member) ((type *)((char *)(ptr) - offsetof(type, member)))
#define EIO 5
#define ENOMEM 12
#define EINVAL 22
#define EOPNOTSUPP 95
#define GFP_KERNEL 0
#define IS_ERR(p) ((unsigned long)(p) >= (unsigned long)-4095)
#define IS_ERR_OR_NULL(p) (!(p) || IS_ERR(p))
#define PTR_ERR(p) ((long)(p))

#define module_param(n, t, p)
#define MODULE_PARM_DESC(n, d)
#define MODULE_DEVICE_TABLE(a, b)
#define MODULE_DESCRIPTION(d)
#define MODULE_LICENSE(l)
#define module_i2c_driver(d)

extern unsigned long jiffies;
#define msecs_to_jiffies(m) ((unsigned long)(m))
#define time_before(a, b) ((long)((a) - (b)) < 0)
void msleep(unsigned int ms);
/* Under a jiffy (1 ms here): the clock stays where it is, the call is counted */
extern int usleeps;
static inline void usleep_range(unsigned long min, unsigned long max)
{ (void)min; (void)max; usleeps++; }

struct mutex { int held; };
void mutex_init(struct mutex *m);
void mutex_lock(struct mutex *m);
void mutex_unlock(struct mutex *m);

struct work_struct { int unused; };
struct delayed_work {
	struct work_struct work;
	long due;
	void (*fn)(struct work_struct *);
};
#define to_delayed_work(w) container_of(w, struct delayed_work, work)
#define INIT_DELAYED_WORK(d, f) ((d)->fn = (f), (d)->due = -1)
#define INIT_DEFERRABLE_WORK(d, f) INIT_DELAYED_WORK(d, f)
extern int system_dfl_wq;
bool queue_delayed_work(int wq, struct delayed_work *d, unsigned long delay);
bool mod_delayed_work(int wq, struct delayed_work *d, unsigned long delay);
bool cancel_delayed_work(struct delayed_work *d);
bool cancel_delayed_work_sync(struct delayed_work *d);

struct device { void *drvdata; };
struct fwnode_handle { int unused; };
struct i2c_client { struct device dev; int irq; };
struct gpio_desc;
#define GPIOD_OUT_LOW 0
void fake_log(const char *kind, const char *fmt, ...) __attribute__((format(printf, 2, 3)));
#define dev_info(dev, ...) fake_log("I", __VA_ARGS__)
#define dev_warn(dev, ...) fake_log("W", __VA_ARGS__)
#define dev_err_probe(dev, err, ...) (fake_log("E", __VA_ARGS__), (int)(err))
#define dev_name(dev) "8-0033"
static inline void *dev_get_drvdata(struct device *d) { return d->drvdata; }
static inline void i2c_set_clientdata(struct i2c_client *c, void *p) { c->dev.drvdata = p; }
static inline void *i2c_get_clientdata(struct i2c_client *c) { return c->dev.drvdata; }
static inline void *devm_kzalloc(struct device *d, size_t n, int gfp) { (void)d; (void)gfp; return calloc(1, n); }
static inline struct gpio_desc *devm_gpiod_get_optional(struct device *d, const char *n, int f)
{ (void)d; (void)n; (void)f; return NULL; }
static inline void gpiod_set_value_cansleep(struct gpio_desc *g, int v) { (void)g; (void)v; }
extern struct fwnode_handle fake_fwnode;
static inline struct fwnode_handle *device_get_named_child_node(struct device *d, const char *n)
{ (void)d; (void)n; return &fake_fwnode; }
static inline void fwnode_handle_put(struct fwnode_handle *f) { (void)f; }
static inline bool device_property_read_bool(struct device *d, const char *n) { (void)d; (void)n; return false; }
static inline void device_init_wakeup(struct device *d, bool w) { (void)d; (void)w; }
static inline bool device_may_wakeup(struct device *d) { (void)d; return false; }
static inline int enable_irq_wake(int irq) { (void)irq; return 0; }
static inline int disable_irq_wake(int irq) { (void)irq; return 0; }

typedef int irqreturn_t;
#define IRQ_NONE 0
#define IRQ_HANDLED 1
#define IRQF_ONESHOT 1
#define IRQF_TRIGGER_LOW 2
int devm_request_threaded_irq(struct device *d, int irq, void *h,
			      irqreturn_t (*thread)(int, void *), int flags,
			      const char *name, void *data);

struct of_device_id { const char *compatible; };
struct dev_pm_ops { int (*suspend)(struct device *); int (*resume)(struct device *); };
#define DEFINE_SIMPLE_DEV_PM_OPS(name, s, r) \
	const struct dev_pm_ops name = { .suspend = (s), .resume = (r) }
#define pm_sleep_ptr(p) (p)
struct i2c_driver {
	struct {
		const char *name;
		const struct of_device_id *of_match_table;
		const struct dev_pm_ops *pm;
	} driver;
	int (*probe)(struct i2c_client *);
	void (*remove)(struct i2c_client *);
};

struct regmap { int unused; };
#define REGCACHE_NONE 0
struct regmap_config { int reg_bits, val_bits, max_register, cache_type; };
struct regmap *devm_regmap_init_i2c(struct i2c_client *c, const struct regmap_config *cfg);
int regmap_read(struct regmap *m, unsigned int reg, unsigned int *val);
int regmap_write(struct regmap *m, unsigned int reg, unsigned int val);
int regmap_update_bits(struct regmap *m, unsigned int reg, unsigned int mask, unsigned int val);
int regmap_bulk_read(struct regmap *m, unsigned int reg, void *buf, size_t n);
int regmap_bulk_write(struct regmap *m, unsigned int reg, const void *buf, size_t n);

#define PD_MAX_PAYLOAD 7
struct pd_message { __le16 header; __le32 payload[PD_MAX_PAYLOAD]; };
#define pd_header_cnt_le(h) (((h) >> 12) & 7)
#define pd_header_type_le(h) ((h) & 0x1f)
#define PD_CTRL_ACCEPT 3
#define PD_CTRL_PS_RDY 6
#define PD_CTRL_PR_SWAP 10

enum typec_cc_status { TYPEC_CC_OPEN, TYPEC_CC_RA, TYPEC_CC_RD, TYPEC_CC_RP_DEF,
		       TYPEC_CC_RP_1_5, TYPEC_CC_RP_3_0 };
enum typec_cc_polarity { TYPEC_POLARITY_CC1, TYPEC_POLARITY_CC2 };
enum typec_role { TYPEC_SINK, TYPEC_SOURCE };
enum typec_data_role { TYPEC_DEVICE, TYPEC_HOST };
enum typec_port_type { TYPEC_PORT_SRC, TYPEC_PORT_SNK, TYPEC_PORT_DRP };
enum tcpm_transmit_type { TCPC_TX_SOP, TCPC_TX_SOP_PRIME, TCPC_TX_SOP_PRIME_PRIME,
			  TCPC_TX_SOP_DEBUG_PRIME, TCPC_TX_SOP_DEBUG_PRIME_PRIME,
			  TCPC_TX_HARD_RESET, TCPC_TX_CABLE_RESET, TCPC_TX_BIST_MODE_2 };
enum tcpm_transmit_status { TCPC_TX_SUCCESS, TCPC_TX_DISCARDED, TCPC_TX_FAILED };
struct tcpm_port;
struct tcpc_dev {
	struct fwnode_handle *fwnode;
	bool adopt_retained_source_ufp;
	bool (*consume_retained_sink_dfp)(struct tcpc_dev *dev);
	bool request_dfp_as_sink;
	unsigned int try_snk_extra_wait_ms;
	bool request_sink_as_source;
	int (*init)(struct tcpc_dev *dev);
	int (*get_vbus)(struct tcpc_dev *dev);
	int (*get_current_limit)(struct tcpc_dev *dev);
	int (*set_cc)(struct tcpc_dev *dev, enum typec_cc_status cc);
	int (*get_cc)(struct tcpc_dev *dev, enum typec_cc_status *cc1,
		      enum typec_cc_status *cc2);
	int (*set_polarity)(struct tcpc_dev *dev, enum typec_cc_polarity polarity);
	int (*set_vconn)(struct tcpc_dev *dev, bool on);
	int (*set_vbus)(struct tcpc_dev *dev, bool on, bool charge);
	int (*set_current_limit)(struct tcpc_dev *dev, u32 max_ma, u32 mv);
	int (*set_pd_rx)(struct tcpc_dev *dev, bool on);
	int (*set_roles)(struct tcpc_dev *dev, bool attached, enum typec_role role,
			 enum typec_data_role data);
	int (*start_toggling)(struct tcpc_dev *dev, enum typec_port_type port_type,
			      enum typec_cc_status cc);
	int (*pd_transmit)(struct tcpc_dev *dev, enum tcpm_transmit_type type,
			   const struct pd_message *msg, unsigned int negotiated_rev);
};
struct tcpm_port *tcpm_register_port(struct device *dev, struct tcpc_dev *tcpc);
void tcpm_unregister_port(struct tcpm_port *port);
void tcpm_cc_change(struct tcpm_port *port);
void tcpm_vbus_change(struct tcpm_port *port);
void tcpm_pd_hard_reset(struct tcpm_port *port);
void tcpm_pd_receive(struct tcpm_port *port, const struct pd_message *msg,
		     enum tcpm_transmit_type sop);
void tcpm_pd_transmit_complete(struct tcpm_port *port, enum tcpm_transmit_status status);
#endif
"""

DRIVER_HARNESS = r"""/*
 * sm5714_usbpd.c on the host: a model of the chip's CC logic, of the
 * partner, and of the few TCPM steps that matter here.  Time is in ms (one
 * jiffy each).  Trace lines: "<kind> <ms> <text>".
 */
#include "fake_kernel.h"

unsigned long jiffies = 100000;
int usleeps;
int system_dfl_wq;
struct fwnode_handle fake_fwnode;
static struct regmap the_regmap;

/* ---- the chip ---- */
enum partner { P_NONE, P_HUB, P_LEGACY, P_AUDIO };
static u8 regs[256];
static enum partner partner;
static bool engine_off;		/* CC logic disabled (CC_CNTL3 bit 3 or stuck) */
static bool model_80_revives;	/* does 0x80 alone leave a CC open? */
static bool model_dead;		/* nothing revives the CC logic */
static bool die_on_open;	/* a CC open kills the CC logic for good */
static bool irq_dead;		/* the chip raises no attach/detach interrupt */
static bool was_attached;
static int reads_watch, reads_other, in_watch, cc_changes, heals;

static u8 cc_status(void)
{
	if (engine_off || model_dead)
		return 0;
	switch (partner) {
	case P_HUB: case P_LEGACY: return 0x09;	/* source, Rp 1.5 A */
	case P_AUDIO: return 0x03;
	default: return 0;
	}
}

static bool vbus(void)
{
	if (partner == P_LEGACY)
		return true;
	return partner == P_HUB && cc_status();	/* the hub powers a seen Rd */
}

static void chip_update(void)
{
	bool now = cc_status() != 0;

	if (now != was_attached && !irq_dead)
		regs[0x01] |= now ? 0x08 : 0x10;
	was_attached = now;
}

int regmap_read(struct regmap *m, unsigned int reg, unsigned int *val)
{
	(void)m;
	if (in_watch)
		reads_watch++;
	else
		reads_other++;
	switch (reg) {
	case 0x28: *val = cc_status(); break;
	case 0x0b: *val = (vbus() ? 0x01 : 0) | (cc_status() ? 0x08 : 0x10); break;
	case 0xd5: *val = (engine_off || model_dead) ? 0x01 : 0x05; break;
	default: *val = regs[reg]; break;
	}
	return 0;
}

int regmap_write(struct regmap *m, unsigned int reg, unsigned int val)
{
	(void)m;
	regs[reg] = val;
	if (reg == 0x2b) {
		if (val & 0x08) {
			engine_off = true;
			model_dead |= die_on_open;
		}
		else if ((val & 3) || model_80_revives)
			engine_off = false;
		printf("C %lu CC_CNTL3=0x%02x usleeps=%d\n", jiffies, val, usleeps);
	}
	chip_update();
	return 0;
}

int regmap_update_bits(struct regmap *m, unsigned int reg, unsigned int mask, unsigned int val)
{
	unsigned int old;

	regmap_read(m, reg, &old);
	if (((old & ~mask) | (val & mask)) == old)
		return 0;
	return regmap_write(m, reg, (old & ~mask) | (val & mask));
}

int regmap_bulk_read(struct regmap *m, unsigned int reg, void *buf, size_t n)
{
	u8 *b = buf;
	size_t i;

	(void)m;
	for (i = 0; i < n; i++) {
		b[i] = regs[reg + i];
		if (reg + i >= 0x01 && reg + i <= 0x05)
			regs[reg + i] = 0;	/* read to clear */
	}
	return 0;
}

int regmap_bulk_write(struct regmap *m, unsigned int reg, const void *buf, size_t n)
{
	(void)m;
	memcpy(&regs[reg], buf, n);
	return 0;
}

struct regmap *devm_regmap_init_i2c(struct i2c_client *c, const struct regmap_config *cfg)
{
	(void)c; (void)cfg;
	return &the_regmap;
}

/* ---- kernel bits ---- */
void fake_log(const char *kind, const char *fmt, ...)
{
	va_list args;

	printf("%s %lu ", kind, jiffies);
	va_start(args, fmt);
	vprintf(fmt, args);
	va_end(args);
}

void msleep(unsigned int ms) { jiffies += ms; }

void mutex_init(struct mutex *m) { m->held = 0; }
void mutex_lock(struct mutex *m)
{
	if (m->held) {
		printf("X %lu recursive mutex_lock\n", jiffies);
		exit(3);
	}
	m->held = 1;
}
void mutex_unlock(struct mutex *m)
{
	if (!m->held) {
		printf("X %lu unlock of a free mutex\n", jiffies);
		exit(3);
	}
	m->held = 0;
}

bool queue_delayed_work(int wq, struct delayed_work *d, unsigned long delay)
{
	(void)wq;
	if (d->due >= 0)
		return false;
	d->due = jiffies + delay;
	return true;
}
bool mod_delayed_work(int wq, struct delayed_work *d, unsigned long delay)
{
	(void)wq;
	d->due = jiffies + delay;
	return true;
}
bool cancel_delayed_work(struct delayed_work *d) { d->due = -1; return true; }
bool cancel_delayed_work_sync(struct delayed_work *d) { d->due = -1; return true; }

static irqreturn_t (*irq_thread)(int, void *);
static void *irq_data;
int devm_request_threaded_irq(struct device *d, int irq, void *h,
			      irqreturn_t (*thread)(int, void *), int flags,
			      const char *name, void *data)
{
	(void)d; (void)irq; (void)h; (void)flags; (void)name;
	irq_thread = thread;
	irq_data = data;
	return 0;
}

int sm5714_battery_set_pd_contract(unsigned int mv, unsigned int ma) { (void)mv; (void)ma; return 0; }
int sm5714_battery_set_otg(bool active) { (void)active; return 0; }
bool sm5714_battery_is_otg_active(void) { return false; }

/* ---- TCPM, the few steps that matter ---- */
enum tstate { T_IDLE, T_RESET, T_TOGGLING, T_ATTACH_WAIT, T_ATTACHED };
static struct {
	struct tcpc_dev *tcpc;
	enum tstate st;
	unsigned long timer;
	bool cc_event, refuse;
	int attaches;
} tc;
static int fake_port;

struct tcpm_port *tcpm_register_port(struct device *dev, struct tcpc_dev *tcpc)
{
	enum typec_cc_status a, b;

	(void)dev;
	tc.tcpc = tcpc;
	tcpc->init(tcpc);
	tcpc->get_vbus(tcpc);
	tcpc->get_cc(tcpc, &a, &b);
	tc.st = T_RESET;		/* tcpm_init(): PORT_RESET */
	tcpc->set_cc(tcpc, TYPEC_CC_RD);
	tc.timer = jiffies;		/* no VBUS: no wait */
	return (struct tcpm_port *)&fake_port;
}
void tcpm_unregister_port(struct tcpm_port *port) { (void)port; }
void tcpm_cc_change(struct tcpm_port *port) { (void)port; tc.cc_event = true; cc_changes++; }
void tcpm_vbus_change(struct tcpm_port *port) { (void)port; }
void tcpm_pd_hard_reset(struct tcpm_port *port) { (void)port; }
void tcpm_pd_receive(struct tcpm_port *port, const struct pd_message *msg,
		     enum tcpm_transmit_type sop) { (void)port; (void)msg; (void)sop; }
void tcpm_pd_transmit_complete(struct tcpm_port *port, enum tcpm_transmit_status status)
{ (void)port; (void)status; }

static void tcpm_toggle(void)
{
	tc.st = T_TOGGLING;
	tc.tcpc->start_toggling(tc.tcpc, TYPEC_PORT_DRP, TYPEC_CC_RD);
}

/* An error recovery of an attached port: PORT_RESET, 100 ms, toggling */
static void tcpm_error_recovery(void)
{
	tc.st = T_RESET;
	tc.tcpc->set_cc(tc.tcpc, TYPEC_CC_RD);
	tc.timer = jiffies + 100;
}

static void tcpm_step(void)
{
	enum typec_cc_status a, b;
	bool seen;

	if (tc.st == T_RESET && jiffies >= tc.timer)
		tcpm_toggle();
	if (tc.cc_event) {
		tc.cc_event = false;
		tc.tcpc->get_cc(tc.tcpc, &a, &b);
		seen = a != TYPEC_CC_OPEN || b != TYPEC_CC_OPEN;
		if (tc.st == T_TOGGLING && seen) {
			tc.st = T_ATTACH_WAIT;
			tc.timer = jiffies + 200;
		} else if ((tc.st == T_ATTACH_WAIT || tc.st == T_ATTACHED) && !seen) {
			printf("D %lu\n", jiffies);
			tcpm_toggle();
		}
	}
	if (tc.st == T_ATTACH_WAIT && jiffies >= tc.timer) {
		if (tc.refuse) {
			printf("F %lu\n", jiffies);
			tcpm_toggle();
		} else {
			tc.tcpc->set_roles(tc.tcpc, true, TYPEC_SINK, TYPEC_DEVICE);
			tc.st = T_ATTACHED;
			tc.attaches++;
			printf("A %lu\n", jiffies);
		}
	}
}

/* ---- the loop ---- */
#include "sm5714_usbpd.c"

static struct i2c_client client;
static struct sm5714_usbpd *sm;

static void run_work(struct delayed_work *d)
{
	if (d->due >= 0 && (long)jiffies >= d->due) {
		d->due = -1;
		if (d == &sm->cc_watch_work)
			in_watch = 1;
		d->fn(&d->work);
		in_watch = 0;
	}
}

static void run(unsigned long ms)
{
	unsigned long end = jiffies + ms;

	while (jiffies < end) {
		if ((regs[0x01] | regs[0x02] | regs[0x04]) && irq_thread)
			irq_thread(0, irq_data);
		tcpm_step();
		run_work(&sm->cc_resync_work);
		run_work(&sm->cc_watch_work);
		jiffies += 10;
	}
}

static void plug(enum partner p) { partner = p; chip_update(); printf("P %lu %d\n", jiffies, p); }

static void attach_hub(void)
{
	plug(P_HUB);
	run(1000);
	if (tc.st != T_ATTACHED) {
		printf("X %lu hub did not attach\n", jiffies);
		exit(4);
	}
}

static void end(void)
{
	printf("END attaches=%d state=%d cc_changes=%d reads_watch=%d\n",
	       tc.attaches, tc.st, cc_changes, reads_watch);
}

int main(int argc, char **argv)
{
	const char *s = argc > 1 ? argv[1] : "";

	model_80_revives = !strcmp(s, "short_detach_missed_irq");
	if (sm5714_usbpd_driver.probe(&client)) {
		printf("X probe failed\n");
		return 2;
	}
	sm = i2c_get_clientdata(&client);
	run(10000);			/* boot: toggling, empty */

	if (!strcmp(s, "idle")) {
		reads_watch = 0;
		run(3600L * 1000);
	} else if (!strcmp(s, "healthy_plug")) {
		attach_hub();
		run(60000);
	} else if (!strcmp(s, "missed_attach")) {
		irq_dead = true;
		plug(P_HUB);
		run(60000);
	} else if (!strcmp(s, "short_detach_missed_irq")) {
		attach_hub();
		irq_dead = true;
		sm->recovery_detach_armed = true;	/* as a Hard Reset leaves it */
		tcpm_error_recovery();
		run(60000);
	} else if (!strcmp(s, "recovery_detach")) {
		attach_hub();
		sm->recovery_detach_armed = true;
		tcpm_error_recovery();
		run(60000);
	} else if (!strcmp(s, "recovery_detach_r9")) {
		cc_reopen_via_snk = false;
		attach_hub();
		sm->recovery_detach_armed = true;
		tcpm_error_recovery();
		run(60000);
	} else if (!strcmp(s, "recovery_detach_dead")) {
		attach_hub();
		die_on_open = true;
		sm->recovery_detach_armed = true;
		tcpm_error_recovery();
		run(3600L * 1000);
	} else if (!strcmp(s, "recovery_detach_unplugged")) {
		attach_hub();
		sm->recovery_detach_armed = true;
		tcpm_error_recovery();
		plug(P_NONE);
		run(3600L * 1000);
	} else if (!strcmp(s, "self_heal_off")) {
		cc_reopen_via_snk = false;
		cc_self_heal = false;
		attach_hub();
		sm->recovery_detach_armed = true;
		tcpm_error_recovery();
		run(300000);
		printf("M %lu self-heal on\n", jiffies);
		cc_self_heal = true;
		run(30000);
	} else if (!strcmp(s, "refused_partner")) {
		tc.refuse = true;
		plug(P_AUDIO);
		run(3600L * 1000);
	} else if (!strcmp(s, "vbus_no_cc")) {
		engine_off = true;		/* stuck, with no open of the driver's */
		plug(P_LEGACY);
		run(4000);
		sm->tcpc.get_vbus(&sm->tcpc);	/* TCPM's VBUS event */
		run(60000);
	} else if (!strcmp(s, "suspend_dead")) {
		cc_reopen_via_snk = false;
		attach_hub();
		sm->recovery_detach_armed = true;
		tcpm_error_recovery();
		run(200);
		sm5714_usbpd_pm_ops.suspend(&client.dev);
		printf("M %lu suspended\n", jiffies);
		run(600000);
		printf("M %lu resumed\n", jiffies);
		sm5714_usbpd_pm_ops.resume(&client.dev);
		run(30000);
	} else {
		printf("X unknown scenario\n");
		return 2;
	}
	end();
	return 0;
}
"""

FAKE_HEADERS = ("bitops", "delay", "gpio/consumer", "i2c", "interrupt", "minmax", "module", "mutex",
                "of", "property", "regmap", "usb/pd", "usb/tcpm", "workqueue")


def build_driver_harness(directory, driver=DRIVER):
    """Compile the driver in its harness; returns the binary's path."""
    directory = Path(directory)
    for name in FAKE_HEADERS:
        header = directory / "inc/linux" / (name + ".h")
        header.parent.mkdir(parents=True, exist_ok=True)
        header.write_text('#include "fake_kernel.h"\n')
    (directory / "inc/fake_kernel.h").write_text(FAKE_KERNEL_H)
    (directory / "sm5714_usbpd.c").write_text(Path(driver).read_text())
    (directory / "harness.c").write_text(DRIVER_HARNESS)
    binary = directory / "harness"
    subprocess.run([host_compiler(), "-std=gnu11", "-O0", "-w", "-Werror=implicit-function-declaration",
                    "-I", str(directory / "inc"), "-I", str(directory), "-o", str(binary),
                    str(directory / "harness.c")], check=True, capture_output=True, text=True)
    return binary


@unittest.skipUnless(host_compiler(), "no C compiler")
class CcRecoveryBehaviourTests(unittest.TestCase):
    """What the driver does, run: the recovery detach, the self-heal and the
    attach watchdog."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.binary = build_driver_harness(cls.tmp.name)

    def run_scenario(self, name):
        out = subprocess.run([str(self.binary), name], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stdout[-2000:])
        trace = []
        for line in out.stdout.splitlines():
            kind, _, rest = line.partition(" ")
            if kind == "END":
                trace.append((kind, None, rest))
                continue
            ms, _, text = rest.partition(" ")
            trace.append((kind, int(ms), text))
        self.assertNotIn("X", [kind for kind, _, _ in trace], trace[-5:])
        self.assertEqual(trace[-1][0], "END")
        return trace

    @staticmethod
    def times(trace, kind, text=""):
        return [ms for k, ms, rest in trace if k == kind and text in rest]

    @staticmethod
    def ending(trace):
        return dict(item.split("=") for item in trace[-1][2].split())

    def test_an_idle_empty_port_costs_one_read_per_period_and_says_nothing(self):
        trace = self.run_scenario("idle")
        self.assertEqual(self.times(trace, "W"), [])
        self.assertEqual([rest for kind, _, rest in trace if kind == "I"],
                         ["SM5714 USB Type-C/PD controller registered",
                          "resynchronizing TCPM, CC status 0x00"])
        end = self.ending(trace)
        self.assertEqual(int(end["reads_watch"]), 3600 // 4)
        self.assertEqual(end["cc_changes"], "1")

    def test_a_healthy_attach_is_left_alone(self):
        trace = self.run_scenario("healthy_plug")
        self.assertEqual(len(self.times(trace, "A")), 1)
        self.assertEqual(self.times(trace, "I", "resyncing TCPM"), [])
        self.assertEqual(self.times(trace, "W"), [])

    def test_an_attach_the_chip_did_not_announce_is_reported_within_a_few_seconds(self):
        trace = self.run_scenario("missed_attach")
        plugged = self.times(trace, "P")[0]
        resyncs = self.times(trace, "I", "shows a partner TCPM was not told of; resyncing TCPM")
        self.assertEqual(len(resyncs), 1)
        self.assertLessEqual(resyncs[0] - plugged, 4600)
        self.assertEqual(len(self.times(trace, "A")), 1)

    def test_an_attach_after_a_forced_detach_shorter_than_a_period_is_reported(self):
        # The old watchdog's blind spot: its memory of the attach before the
        # forced detach kept it quiet for the attach after it.
        trace = self.run_scenario("short_detach_missed_irq")
        opened = self.times(trace, "I", "CC opened for TCPM's error recovery")[0]
        attaches = self.times(trace, "A")
        self.assertEqual(len(attaches), 2)
        self.assertLessEqual(attaches[1] - opened, 5000)

    def test_the_recovery_detach_comes_back_through_unattached_snk(self):
        trace = self.run_scenario("recovery_detach")
        opened = self.times(trace, "I", "CC opened for TCPM's error recovery after a swap or Hard Reset")[0]
        self.assertEqual(len(self.times(trace, "I", "CC on again through Unattached.SNK after the driver opened it")), 1)
        writes = [rest.split(" usleeps=") for kind, ms, rest in trace if kind == "C" and ms >= opened]
        self.assertEqual([reg for reg, _ in writes[:3]], ["CC_CNTL3=0x88", "CC_CNTL3=0x82", "CC_CNTL3=0x80"])
        # One dwell in Unattached.SNK, between the 0x82 and the 0x80.
        dwells = [int(n) for _, n in writes[:3]]
        self.assertEqual((dwells[1] - dwells[0], dwells[2] - dwells[1]), (0, 1))
        attaches = self.times(trace, "A")
        self.assertEqual(len(attaches), 2)
        self.assertLess(attaches[1] - opened, 1000)
        self.assertEqual(self.times(trace, "W"), [])

    def test_the_r9_sequence_is_healed(self):
        # cc_reopen_via_snk=N: 0x88 then 0x80, the port of 2026-11-26.  The
        # self-heal re-initialises it 5 to 9 s into the toggling, and says
        # what the chip's CC state was.
        trace = self.run_scenario("recovery_detach_r9")
        opened = self.times(trace, "I", "CC opened for TCPM's error recovery")[0]
        heals = [(ms, rest) for kind, ms, rest in trace if kind == "W"]
        self.assertEqual(len(heals), 1)
        self.assertTrue(5000 <= heals[0][0] - opened <= 10000, heals)
        self.assertIn("CC finds no partner since the driver opened it (CC status 0x00, status1 0x10, "
                      "CC state 0x01): CC logic re-initialised, try 1 of 3", heals[0][1])
        attaches = self.times(trace, "A")
        self.assertEqual(len(attaches), 2)
        self.assertLess(attaches[1] - heals[0][0], 1000)

    def test_a_chip_that_stays_dead_is_tried_three_times_and_left(self):
        trace = self.run_scenario("recovery_detach_dead")
        opened = self.times(trace, "I", "CC opened for TCPM's error recovery")[0]
        heals = [ms - opened for ms in self.times(trace, "W")]
        self.assertEqual(len(heals), 3)
        self.assertTrue(5000 <= heals[0] <= 10000 and 29000 <= heals[1] <= 40000
                        and 119000 <= heals[2] <= 135000, heals)
        self.assertEqual(len(self.times(trace, "A")), 1)
        self.assertEqual(self.times(trace, "I", "resyncing TCPM"), [])

    def test_an_empty_port_after_a_recovery_detach_is_healed_at_most_three_times(self):
        trace = self.run_scenario("recovery_detach_unplugged")
        self.assertLessEqual(len(self.times(trace, "W")), 3)
        self.assertEqual(len(self.times(trace, "A")), 1)

    def test_the_self_heal_can_be_held_off_and_then_acts(self):
        trace = self.run_scenario("self_heal_off")
        on = self.times(trace, "M", "self-heal on")[0]
        heals = self.times(trace, "W")
        self.assertEqual(len(heals), 1)
        self.assertTrue(0 <= heals[0] - on <= 4100, heals)
        self.assertEqual(len(self.times(trace, "A")), 2)

    def test_an_attachment_tcpm_does_not_take_is_resynced_with_back_off(self):
        trace = self.run_scenario("refused_partner")
        resyncs = self.times(trace, "I", "resyncing TCPM")
        self.assertTrue(4 <= len(resyncs) <= 12, resyncs)
        gaps = [b - a for a, b in zip(resyncs, resyncs[1:])]
        self.assertEqual(gaps[1:], sorted(gaps[1:]))
        self.assertTrue(600000 <= max(gaps) < 610000, gaps)

    def test_vbus_from_a_partner_with_nothing_detected_is_healed(self):
        trace = self.run_scenario("vbus_no_cc")
        heals = [rest for kind, _, rest in trace if kind == "W"]
        self.assertEqual(len(heals), 1)
        self.assertIn("CC finds no partner though VBUS comes from one", heals[0])
        self.assertEqual(len(self.times(trace, "A")), 1)

    def test_nothing_runs_in_a_system_sleep(self):
        trace = self.run_scenario("suspend_dead")
        asleep = self.times(trace, "M", "suspended")[0]
        awake = self.times(trace, "M", "resumed")[0]
        heals = self.times(trace, "W")
        self.assertEqual([ms for ms in heals if asleep <= ms < awake], [])
        self.assertEqual(len(heals), 1)
        self.assertTrue(0 < heals[0] - awake <= 5000, heals)
        self.assertEqual(len(self.times(trace, "A")), 2)


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


class SwapBackWriteUpTests(unittest.TestCase):
    """The write-up's section on the swap back, held to the code."""

    def setUp(self):
        self.doc = DOC.read_text()
        self.flat = " ".join(self.doc.split())
        start = self.doc.index("## The power role, later")
        self.section = self.doc[start:self.doc.index("\n## ", start + 1)]
        self.code = added(SWAP_BACK[1], TCPM_C)

    def test_it_says_what_has_run_and_what_has_not(self):
        flat = " ".join(self.section.split())
        self.assertIn("**Written and compiled; the swap back has not run on hardware** (2026-11-26).", flat)
        self.assertIn("The partner's in-place PR_Swap ran once on the SM-X910, with the r8 kernel,", flat)
        self.assertIn("The driver fixes for that, and the tablet's own in-place PR_Swap, have not run.", flat)
        for claim in ("was run on", "has been tried on", "passed on", "confirmed on", "works on"):
            self.assertNotIn(claim, flat)

    def test_numbers_are_the_code_s(self):
        c = swap_back_constants()
        flat = " ".join(self.section.split())
        self.assertIn("first question goes out %d s after the first `SRC_READY`"
                      % (c["SWAP_BACK_FIRST_MS"] // 1000), flat)
        ladder, ms = [], c["SWAP_BACK_POLL_MIN_MS"]
        while ms < c["SWAP_BACK_POLL_MAX_MS"]:
            ladder.append(ms // 1000)
            ms *= 2
        self.assertIn("asked again after %d s, then %s and %d s, then every %d s"
                      % (ladder[0], ", ".join(str(n) for n in ladder[1:-1]), ladder[-1],
                         c["SWAP_BACK_POLL_MAX_MS"] // 1000), flat)
        # Reject, Not_Supported or no answer: the slow end of the ladder.
        self.assertIn("so it is asked again every %d s" % (c["SWAP_BACK_POLL_MAX_MS"] // 1000), flat)
        self.assertIn("within %d s of a port reset" % (c["SWAP_BACK_SESSION_GAP_MS"] // 1000), flat)
        self.assertIn("at most %d PR_Swap requests and %d accepted swaps"
                      % (c["SWAP_BACK_MAX_REQUESTS"], c["SWAP_BACK_MAX_ACCEPTED"]), flat)
        self.assertIn("After %d failed swaps" % c["SWAP_BACK_MAX_STRIKES"], flat)
        self.assertIn("a power sink for %d minutes" % (c["SWAP_BACK_SINK_STABLE_MS"] // 60000), flat)
        self.assertIn("within a minute of the swap", flat)
        self.assertEqual(c["SWAP_BACK_SINK_SHORT_MS"], 60000)
        self.assertIn("a swap whose sink role lasted %d minutes gives the %d requests and %d swaps back"
                      % (c["SWAP_BACK_SINK_REFUND_MS"] // 60000, c["SWAP_BACK_MAX_REQUESTS"],
                         c["SWAP_BACK_MAX_ACCEPTED"]), flat)
        self.assertIn("sent less than %d s before a port reset" % (c["SWAP_BACK_CUT_MS"] // 1000), flat)
        self.assertEqual(c["SWAP_BACK_BUSY_MAX_MS"], 60000)
        self.assertIn("waits a second at a time, for at most a minute", flat)
        self.assertEqual(c["SWAP_BACK_RETRY_MS"], 1000)

    # The switches of this section and the defaults kit r9 ships with.
    SWITCHES = {"swap_back_to_sink": "Y", "own_pr_swap_in_place": "Y", "sink_tx_ok_rp": "Y",
                "swap_protocol_reset": "Y", "recovery_detach": "Y", "pr_swap_rd_delay_ms": "0",
                "cc_reopen_via_snk": "Y", "cc_self_heal": "Y"}

    @staticmethod
    def driver_default(driver, name):
        """The parameter's default as sysfs shows it, read from its initialiser."""
        found = re.search(r"^static (bool|unsigned int) %s( = (\w+))?;$" % name, driver, re.M)
        if found.group(1) == "bool":
            return "Y" if found.group(3) == "true" else "N"
        return found.group(3) or "0"

    def test_switches_are_the_driver_s(self):
        driver = DRIVER.read_text()
        for name, shown in self.SWITCHES.items():
            with self.subTest(name):
                self.assertEqual(self.driver_default(driver, name), shown)
                rows = [line for line in self.section.splitlines()
                        if line.startswith("| `/sys/module/sm5714_usbpd/parameters/%s`" % name)]
                self.assertEqual(len(rows), 1)
                self.assertTrue(rows[0].startswith("| `/sys/module/sm5714_usbpd/parameters/%s` | `%s` |"
                                                   % (name, self.driver_default(driver, name))), rows[0])
        # Every bool parameter whose description names its default names the
        # one its initialiser gives.
        for name, desc in re.findall(r"MODULE_PARM_DESC\((\w+),\n\t+ *\"([^\"]*)\"\);", driver):
            said = re.search(r"\(default (on|off)\)", desc)
            if said and re.search(r"^static bool %s\b" % name, driver, re.M):
                with self.subTest(name):
                    self.assertEqual(said.group(1), {"Y": "on", "N": "off"}[self.driver_default(driver, name)])
        self.assertIn("at most 500", " ".join(self.section.split()))
        self.assertIn("#define SM5714_PR_SWAP_RD_DELAY_MAX_MS\t500", driver)

    def test_steps_follow_the_defaults(self):
        flat = " ".join(self.section.split())
        steps = self.section[self.section.index("### Trying it on a tablet"):]
        one = steps[steps.index("1. **The hub asks itself**"):steps.index("2. **The tablet's own PR_Swap**")]
        two = steps[steps.index("2. **The tablet's own PR_Swap**"):steps.index("3. **The swap back")]
        three = steps[steps.index("3. **The swap back"):steps.index("4. **A partner")]
        six = steps[steps.index("6. **Nothing changes**"):]
        # Step 1 runs with the defaults; the steps that need it off say so,
        # with the command.
        self.assertNotIn("echo Y | sudo tee $P/sink_tx_ok_rp", " ".join(one.split()))
        self.assertIn("with the defaults: `sink_tx_ok_rp` on, `otg_rp` at 0", " ".join(one.split()))
        self.assertIn("`echo N | sudo tee $P/swap_back_to_sink $P/sink_tx_ok_rp`", " ".join(two.split()))
        self.assertIn("`echo N | sudo tee $P/sink_tx_ok_rp`; with it on, the hub may ask first",
                      " ".join(three.split()))
        self.assertNotIn("sink_tx_ok_rp", six)
        # The status and the paragraph say it is on, and what N gives.
        self.assertIn("The SinkTxOK signal below (`sink_tx_ok_rp`) is on by default, the user's decision "
                      "of 2026-11-26", flat)
        self.assertIn("`sink_tx_ok_rp` itself has not run either.", flat)
        self.assertIn("`sink_tx_ok_rp` (below, on by default)", flat)
        self.assertNotIn("off by default", flat)
        self.assertIn("Which one comes first on this hub is not known until it runs.", flat)
        # Kernel time stamps, the TCPM log's, in both places.
        self.assertEqual(self.section.count("journalctl -k -b -o short-monotonic"), 2)
        self.assertNotIn("short-precise", self.section)

    def test_log_lines_are_the_code_s(self):
        driver = DRIVER.read_text()
        flat = " ".join(self.section.split())
        code = " ".join(self.code.split())
        # The driver's lines the steps name, in the driver and in the doc.
        for line in ("own request accepted, applying it in place", "CC state frozen", "CC hold applied",
                     "CC hold released", "PR_SWAP: protocol layer reset",
                     "PD protocol layer released after a Hard Reset (PD_STATE3 0x",
                     "CC opened for TCPM's error recovery after a swap or Hard Reset",
                     "PR_SWAP: in-place swap dropped (", "CC role bit left set by a swap before this boot (CC_CNTL7 0x"):
            with self.subTest(line):
                self.assertIn(line, driver)
                self.assertIn(line, flat)
        self.assertIn('"%s PR_SWAP: protocol layer reset\\n", swap', driver)
        self.assertIn('sm5714_usbpd_reset_protocol(sm, "Source-to-Sink");', driver)
        # The kernel log lines of the swap back, from the patch's format
        # strings, as the doc shows them filled in.
        stop = "; not asking again until the partner has been away for a minute"
        self.assertIn('"swap back to sink: %s' + stop + '\\n"', code)
        filled = (("partner has no external power now, asking again in %u s, then less often down to every %u s",
                   "partner has no external power now, asking again in 30 s, then less often down to every 600 s"),
                  ("Get_Source_Cap answered %s, not a source now, asking again every %u s",
                   "Get_Source_Cap answered Reject, not a source now, asking again every 600 s"),
                  ("Get_Source_Cap answered %s, not a source now, asking again every %u s",
                   "Get_Source_Cap answered nothing, not a source now, asking again every 600 s"),
                  ("partner has external power (vSafe5V PDO %#010x), requesting the sink role, PR_Swap %u of %u",
                   "partner has external power (vSafe5V PDO 0x2e0190c8), requesting the sink role, PR_Swap 1 of 8"),
                  ("PR_Swap accepted, swap %u of %u", "PR_Swap accepted, swap 1 of 3"),
                  ("power sink at %u mV %u mA%s", "power sink at 9000 mV 1660 mA"),
                  ("PR_Swap answered %s, asking again in %u s", "PR_Swap answered Wait, asking again in N s"),
                  ("swap accepted but the partner did not supply power, failed swap %u of %u",
                   "swap accepted but the partner did not supply power, failed swap 1 of 2" + stop),
                  ("paused after %u failed swaps until the port has been a power sink for %u minutes",
                   "paused after 2 failed swaps until the port has been a power sink for 30 minutes"),
                  ("%s cut short by a port reset", "Get_Source_Cap cut short by a port reset" + stop),
                  ("the source role was asked for through power_role",
                   "the source role was asked for through power_role" + stop),
                  ("PR_Swap answered Not_Supported", "PR_Swap answered Not_Supported"),
                  ("PR_Swap answered Reject by a partner that does not claim dual-role power",
                   "PR_Swap answered Reject by a partner that does not claim dual-role power"),
                  ("the partner's source capabilities do not start with a fixed supply",
                   "the partner's source capabilities do not start with a fixed supply"),
                  ("%u PR_Swap requests made", "8 PR_Swap requests made"),
                  ("%u swaps accepted", "3 swaps accepted"),
                  ("the partner asked for the sink role", "the partner asked for the sink role"))
        for fmt, shown in filled:
            with self.subTest(fmt):
                self.assertIn('"%s"' % fmt, code)
                self.assertIn(shown, flat)
        # The TCPM log lines the steps name.
        for fmt, shown in (("Swap back: no external power, asking again in %u s",
                            "Swap back: no external power, asking again in N s"),
                           ("Swap back: Get_Source_Cap answered %s, asking again in %u s",
                            "Swap back: Get_Source_Cap answered ..., asking again in 600 s")):
            with self.subTest(fmt):
                self.assertIn('"%s"' % fmt, code)
                self.assertIn(shown, flat)
        # The PDO in the example is the 5 V 2 A [RUDE] one.
        self.assertEqual((1 << 29) | (1 << 27) | (1 << 26) | (1 << 25) | (100 << 10) | 200, 0x2e0190c8)

    def test_run_3_is_told_with_the_code_s_lines(self):
        # 2026-11-26, kit r9, swap_protocol_reset=N: the host role kept
        # through a Hard Reset, and a port that died after the error
        # recovery's CC open.  Both fixes are said not to have run.
        driver = DRIVER.read_text()
        flat = " ".join(self.section.split())
        self.assertIn("### Run 3: a Hard Reset after a deaf swap, and a port that died", self.section)
        self.assertIn("**Two fixes, written and compiled, not run on a tablet** (2026-11-27).", flat)
        for line in ("CC on again through Unattached.SNK after the driver opened it",
                     "shows a partner TCPM was not told of; resyncing TCPM"):
            with self.subTest(line):
                self.assertIn(line, driver)
                self.assertIn(line, flat)
        self.assertIn('"CC finds no partner %s (CC status 0x%02x, status1 0x%02x, CC state 0x%02x): '
                      '%s, try %u of %zu\\n"', driver)
        for why in ("since the driver opened it", "though VBUS comes from one"):
            self.assertIn('why = "%s";' % why, driver)
        self.assertIn("`CC finds no partner since the driver opened it (CC status 0x00, status1 0x10, "
                      "CC state 0x01): CC logic re-initialised, try 1 of 3`", flat)
        self.assertIn("CC logic re-initialised", driver)
        # The numbers are the driver's.
        heal = [int(n) // 1000 for n in re.search(r"sm5714_heal_wait_ms\[\] = \{ ([0-9, ]+) \};",
                                                  driver).group(1).split(", ")]
        self.assertEqual(heal, [5, 25, 90])
        self.assertIn("about 5 s, 30 s and 2 min into the toggling", flat)
        self.assertIn("repeated at the next sample, then after 30 s and 2 min, and then every 10 min "
                      "at the most", flat)
        # The Anker hub's answer to Get_Source_Cap never asks for the swap.
        self.assertIn("**The Anker hub never gives the first answer below.**", flat)
        self.assertNotIn("the r8 deafness up to the hub's Hard Reset is expected again", flat)

    def test_states_it_names_exist(self):
        states = set(re.findall(r"^\tS\(([A-Z_]+)\),", added(SWAP_BACK[1], TCPM_C), re.M))
        self.assertEqual(states, {"GET_SOURCE_CAP", "GET_SOURCE_CAP_TIMEOUT"})
        for name in states:
            self.assertIn("`%s`" % name, self.section)


if __name__ == "__main__":
    unittest.main()
