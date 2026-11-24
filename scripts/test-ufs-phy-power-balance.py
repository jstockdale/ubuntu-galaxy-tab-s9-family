#!/usr/bin/env python3
"""Off-target model check of the UFS PHY power reference (ufs-qcom on SM8550).

It models, step by step, the code paths of the as-built kit r10 kernel that
change the UFS PHY's power_count and init_count, and explores every
interleaving of the clock-gating work with one SCSI command submission:

  drivers/phy/phy-core.c          phy_init/phy_exit/phy_power_on/phy_power_off
                                  (225-377): callbacks only on 0->1 and 1->0
  drivers/phy/qualcomm/phy-qcom-qmp-ufs.c
                                  qmp_ufs_power_on (1985-2011): PWRDN=1
                                  qmp_ufs_power_off (2114-2129): PWRDN=0
                                  qmp_ufs_set_mode (2131-2148): marks the
                                  power-on the r10 diagnostic logs
                                  qmp_ufs_phy_calibrate (2059-2112)
  drivers/ufs/host/ufs-qcom.c     ufs_qcom_power_up_sequence (519-580)
                                  ufs_qcom_setup_clocks (1460-1523)
                                  ufs_qcom_exit (1692-1699)
  drivers/ufs/core/ufshcd.c       ufshcd_ungate_work (1918-1950)
                                  ufshcd_hold (1957-2025)
                                  ufshcd_gate_work (2028-2094)
                                  __ufshcd_release (2096-2121)
                                  ufshcd_setup_clocks (9764-9830): the vops
                                  hook runs whatever the clocks' state
                                  ufshcd_is_ufs_dev_busy (286-290)
                                  ufshcd_suspend/resume (10629-10699)
                                  __ufshcd_wl_suspend/resume (10239-10521)
  drivers/scsi/scsi_lib.c         scsi_queue_rq (1853-1935): the command is
                                  counted by scsi_host_busy() from
                                  blk_mq_start_request(), before
                                  ->queuecommand() takes ufshcd_hold()

Two variants of ufs-qcom are modelled: "old" (as built in r10, mainline
v7.2-rc3 to v7.4-rc4) and "fixed" (ufs-qcom-track-phy-power-state-gts9u.patch).

Run:  python3 -I scripts/test-ufs-phy-power-balance.py      (exit 0 = all checks met;
      tests/test_ufs_qcom_phy_power.py runs it)
"""

import copy
import sys

CLKS_OFF, CLKS_ON, REQ_CLKS_OFF, REQ_CLKS_ON = "CLKS_OFF", "CLKS_ON", "REQ_CLKS_OFF", "REQ_CLKS_ON"


class Phy:
    """phy-core counters plus the QMP UFS PHY's registers and callbacks."""

    def __init__(self):
        self.power_count = 0
        self.init_count = 0
        self.powered = False          # callback state: regulators/clocks on
        self.pwrdn = 0                # POWER_DOWN_CONTROL.SW_PWRDN (reset 0)
        self.dbg_pending = False      # qmp->dbg_pwrdn_pending (r10 diagnostic)
        self.log = []

    # phy-core.c
    def init(self):
        self.init_count += 1          # .init is a no-op for SM8550 (2150-2177)

    def exit(self):
        self.init_count -= 1          # no .exit op in qcom_qmp_ufs_phy_ops

    def power_on(self):
        if self.power_count == 0:     # phy-core.c:322
            self.powered = True       # qmp_ufs_power_on
            self.pwrdn = 1
            if self.dbg_pending:
                self.dbg_pending = False
                self.log.append("power-up write")
        self.power_count += 1

    def power_off(self):
        if self.power_count == 1:     # phy-core.c:360
            self.pwrdn = 0            # qmp_ufs_power_off
            self.powered = False
        self.power_count -= 1         # no floor: 0 -> -1 is possible

    def set_mode(self):
        self.dbg_pending = True

    def calibrate(self):
        self.log.append("calibration entry PWRDN %d" % self.pwrdn)
        ok = self.powered and self.pwrdn == 1
        if not ok:
            self.log.append("phy initialization timed-out")
        return ok

    def gdsc_collapse(self):
        """ufs_mem_phy_gdsc off in the sleep: PHY registers back to reset."""
        self.pwrdn = 0


class Host:
    def __init__(self, variant):
        self.variant = variant
        self.phy = Phy()
        self.phy_powered_on = False   # the fix's flag
        self.clk_on = False           # ufshcd clki->enabled (all clocks)
        self.link = "active"          # active / hibern8 / off
        self.state = CLKS_ON
        self.active_reqs = 0
        self.is_suspended = False
        self.operational = True
        self.inflight = 0             # started SCSI commands (scsi_host_busy)
        self.gate = "none"            # none / pending / running
        self.gate_pc = 0
        self.ungate = "none"          # none / queued / running
        self.calibrations = 0
        self.failures = 0

    # ---- ufs-qcom ----
    def q_phy_on(self):
        if self.variant == "fixed":
            if self.phy_powered_on:
                return
            self.phy.power_on()
            self.phy_powered_on = True
        else:
            self.phy.power_on()

    def q_phy_off(self):
        if self.variant == "fixed":
            if not self.phy_powered_on:
                return
            self.phy.power_off()
            self.phy_powered_on = False
        else:
            self.phy.power_off()

    def q_setup_clocks(self, on, pre):
        if pre and not on:
            self.q_phy_off()          # ufs-qcom.c:1494
        if not pre and on:
            self.q_phy_on()           # ufs-qcom.c:1503

    def q_power_up_sequence(self):
        # ufs_qcom_host_reset(): no effect on the counts
        if self.variant == "fixed":
            self.q_phy_off()
        elif self.phy.power_count:    # ufs-qcom.c:542
            self.phy.power_off()
        self.phy.init()
        self.phy.set_mode()
        self.q_phy_on()
        self.calibrations += 1
        if self.phy.calibrate():
            return True
        self.failures += 1
        self.phy.exit()               # out_disable_phy: ufs-qcom.c:577
        return False

    def q_exit(self):
        self.q_phy_off()
        self.phy.exit()

    # ---- ufshcd ----
    def setup_clocks(self, on):
        self.q_setup_clocks(on, pre=True)
        self.clk_on = on              # idempotent per clki->enabled
        self.q_setup_clocks(on, pre=False)
        if on:
            self.state = CLKS_ON      # ufshcd.c:9818-9823

    def release(self):                # __ufshcd_release
        self.active_reqs -= 1
        if self.active_reqs or self.is_suspended or self.state == CLKS_OFF:
            return
        if not self.operational:
            return
        self.state = REQ_CLKS_OFF
        self.gate = "pending"         # queue_delayed_work(gate_work)

    def hold_switch(self):
        """One pass of ufshcd_hold()'s switch; returns 'done' or 'wait'."""
        if self.state == CLKS_ON:
            if self.link == "hibern8":
                return "wait"         # flush ungate, then re-check
            return "done"
        if self.state == REQ_CLKS_OFF:
            if self.gate == "pending":    # cancel_delayed_work() succeeds
                self.gate = "none"
                self.state = CLKS_ON
                return "done"
            # gate work done or running: fall through to CLKS_OFF
        if self.state in (REQ_CLKS_OFF, CLKS_OFF):
            self.state = REQ_CLKS_ON
            self.ungate = "queued"
        return "wait"

    def gate_step(self):
        """One locked section of ufshcd_gate_work(); returns True when done."""
        if self.gate_pc == 0:
            if self.is_suspended or self.state != REQ_CLKS_OFF:
                self.state = CLKS_ON
                return True
            if self.active_reqs:
                return True
            self.gate_pc = 1
            return False
        if self.gate_pc == 1:
            if self.inflight or not self.operational:   # dev busy / state
                return True           # leaves REQ_CLKS_OFF, clocks on
            self.link = "hibern8"
            self.setup_clocks(False)
            self.gate_pc = 2
            return False
        if self.state == REQ_CLKS_OFF:
            self.state = CLKS_OFF
        return True

    def ungate_run(self):             # ufshcd_ungate_work, whole
        if self.state == CLKS_ON:
            return
        self.setup_clocks(True)
        if self.link == "hibern8":
            self.link = "active"

    def drain_wq(self):
        """Let the ordered clock-gating workqueue run until it is idle."""
        while True:
            if self.gate == "running":
                while not self.gate_step():
                    pass
                self.gate, self.gate_pc = "none", 0
            elif self.ungate == "queued":
                self.ungate = "none"
                self.ungate_run()
            elif self.gate == "pending":
                self.gate, self.gate_pc = "running", 0
            else:
                return

    def hold_blocking(self):
        self.active_reqs += 1
        while self.hold_switch() == "wait":
            self.drain_wq()
            if self.state == CLKS_ON and self.link == "hibern8":
                self.link = "active"

    # ---- one system suspend/resume at spm_lvl 5 (link off) ----
    def system_sleep(self):
        self.hold_blocking()              # __ufshcd_wl_suspend: hold
        self.is_suspended = True
        self.link = "off"                 # SSU + link off
        self.setup_clocks(False)          # ufshcd_suspend
        self.state = CLKS_OFF
        self.phy.gdsc_collapse()          # genpd noirq: ufs_mem_phy_gdsc off
        self.setup_clocks(True)           # ufshcd_resume
        ok = False
        for _ in range(5 * 3):            # MAX_HOST_RESET_RETRIES x 3 HCE
            if self.q_power_up_sequence():
                ok = True
                break
        self.link = "active" if ok else "off"
        self.is_suspended = False
        self.release()                    # __ufshcd_wl_resume: release
        self.drain_wq()
        return ok

    def balanced(self):
        want = 1 if self.clk_on else 0
        return self.phy.power_count == want and self.phy.powered == self.clk_on


def booted(variant):
    """Probe done and idle: clocks on, one PHY reference, CLKS_ON."""
    h = Host(variant)
    h.clk_on = True                       # ufshcd_hba_init: setup_clocks(on)
    h.q_setup_clocks(True, pre=False)     # ufs_qcom_init(): POST_CHANGE on
    assert h.q_power_up_sequence()        # ufshcd_hba_enable() at probe
    h.phy.log.clear()
    return h


# The SCSI command: started (counted by scsi_host_busy), then queuecommand
# takes ufshcd_hold() (blocking), then it completes (release), then the SCSI
# core clears SCMD_STATE_INFLIGHT.
CMD_STEPS = ("start", "hold", "hold_wait", "complete", "unbusy", "end")


def explore(variant):
    """All interleavings of the gate work (timer may fire at any point) with
    one command submitted 150 ms after the previous one completed."""
    h = booted(variant)
    h.active_reqs = 1
    h.release()                           # previous command done: gate pending
    results = []

    def run(h, pc, trace):
        moves = []
        # the gate timer fires / the gate work runs one section
        if h.gate == "pending":
            moves.append("fire")
        if h.gate == "running":
            moves.append("gate")
        if h.gate != "running" and h.ungate == "queued":
            moves.append("ungate")
        step = CMD_STEPS[pc]
        if step == "start":
            moves.append("cmd")
        elif step == "hold":
            moves.append("cmd")
        elif step == "hold_wait":
            if h.gate != "running" and h.ungate == "none":
                moves.append("cmd")
        elif step in ("complete", "unbusy"):
            moves.append("cmd")
        if not moves:
            h.drain_wq()
            results.append((h, trace))
            return
        for m in moves:
            g = copy.deepcopy(h)
            npc = pc
            if m == "fire":
                g.gate, g.gate_pc = "running", 0
            elif m == "gate":
                if g.gate_step():
                    g.gate, g.gate_pc = "none", 0
            elif m == "ungate":
                g.ungate = "none"
                g.ungate_run()
            else:
                if step == "start":
                    g.inflight += 1
                    npc = 1
                elif step == "hold":
                    g.active_reqs += 1
                    npc = 3 if g.hold_switch() == "done" else 2
                elif step == "hold_wait":
                    if g.state == CLKS_ON and g.link == "hibern8":
                        g.link = "active"
                    npc = 3 if g.hold_switch() == "done" else 2
                elif step == "complete":
                    g.release()
                    npc = 4
                elif step == "unbusy":
                    g.inflight -= 1
                    npc = 5
            run(g, npc, trace + [m])

    run(h, 0, [])
    return results


def check(name, cond, detail=""):
    print("%-4s %s%s" % ("ok" if cond else "FAIL", name, (": " + detail) if detail else ""))
    return cond


def main():
    good = True
    for variant in ("old", "fixed"):
        res = explore(variant)
        bad = [(h, t) for h, t in res if not h.balanced()]
        print("== %s: %d interleavings, %d leave the PHY reference unbalanced" %
              (variant, len(res), len(bad)))
        if variant == "old":
            good &= check("old: the gate/submission race leaves an extra PHY reference",
                          len(bad) > 0,
                          "e.g. %s -> power_count %d with clocks %s" %
                          (" ".join(bad[0][1]), bad[0][0].phy.power_count,
                           "on" if bad[0][0].clk_on else "off") if bad else "")
            if bad:
                h = bad[0][0]
                ok = h.system_sleep()
                good &= check("old: the next sleep's resume never runs the PHY power-on "
                              "before its calibrations, and all 15 fail",
                              not ok and h.failures == 15 and
                              "power-up write" not in h.phy.log and
                              all(l == "calibration entry PWRDN 0"
                                  for l in h.phy.log if l.startswith("calibration")),
                              "calibrations %d, failures %d, power_count after %d" %
                              (h.calibrations - 1, h.failures, h.phy.power_count))
                good &= check("old: the failed retries leave power_count where it was "
                              "(it does not climb)", h.phy.power_count == 2)
            # Balanced, but the gate work gave up after the command's release
            # (it was still counted by scsi_host_busy()): clocks on in
            # REQ_CLKS_OFF, so the next ufshcd_hold() ungates them again.
            latent = [(h, t) for h, t in res if h.balanced() and h.clk_on and
                      h.state == REQ_CLKS_OFF]
            clean = [(h, t) for h, t in res if h.balanced() and not h.clk_on and
                     h.state == CLKS_OFF]
            print("     old: %d clean, %d latent (clocks on in REQ_CLKS_OFF)" %
                  (len(clean), len(latent)))
            h = copy.deepcopy(clean[0][0])
            good &= check("old: without the race the sleep passes on the first try",
                          h.system_sleep() and h.failures == 0 and
                          h.phy.log[:2] == ["power-up write", "calibration entry PWRDN 1"])
            if latent:
                h = copy.deepcopy(latent[0][0])
                good &= check("old: a latent state fails the next sleep too (its hold "
                              "repeats the power-on)", not h.system_sleep(),
                              " ".join(latent[0][1]))
        else:
            good &= check("fixed: no interleaving unbalances the PHY reference", not bad)
            allpass = True
            for h, t in res:
                g = copy.deepcopy(h)
                if not (g.system_sleep() and g.failures == 0 and
                        g.phy.log[:2] == ["power-up write", "calibration entry PWRDN 1"]
                        and g.balanced()):
                    allpass = False
            good &= check("fixed: after every interleaving the sleep's first calibration "
                          "follows a PHY power-on and passes", allpass)

    # Repeated races: old accumulates, fixed does not.
    for variant in ("old", "fixed"):
        h = booted(variant)
        for _ in range(3):
            h.hold_blocking()             # command A: ungates if gated
            h.release()                   # A done: gate work pending
            h.inflight += 1               # command B started
            assert h.gate == "pending"
            h.gate, h.gate_pc = "running", 0
            while not h.gate_step():      # gate work sees scsi_host_busy()
                pass
            h.gate, h.gate_pc = "none", 0
            h.hold_blocking()             # B's queuecommand -> ungate -> setup_clocks(on)
            h.release()
            h.inflight -= 1
            h.drain_wq()                  # clocks gated again
            assert h.state == CLKS_OFF and not h.clk_on
        want = 3 if variant == "old" else 0
        good &= check("%s: three races leave power_count %d with the clocks gated" %
                      (variant, want),
                      h.phy.power_count == want and not h.clk_on,
                      "power_count %d, PHY %s" % (h.phy.power_count,
                                                  "powered" if h.phy.powered else "off"))
        if variant == "old":
            good &= check("old: the PHY stays powered while the host is clock-gated "
                          "(the r10-5 regulator/clock evidence)", h.phy.powered)

    # Calibration failure that is not the race: the retry runs the power-on again.
    for variant in ("old", "fixed"):
        h = booted(variant)
        h.phy.log.clear()
        real = h.phy.calibrate
        n = {"c": 0}

        def flaky():
            n["c"] += 1
            return real() if n["c"] > 1 else (h.phy.log.append("injected failure") or False)

        h.phy.calibrate = flaky
        first = h.q_power_up_sequence()
        second = h.q_power_up_sequence()
        good &= check("%s: a retry after an unrelated calibration failure powers the PHY "
                      "up again and passes" % variant,
                      not first and second and h.phy.log.count("power-up write") == 2)

    # Driver removal: ufs_qcom_exit() then ufshcd_setup_clocks(false).
    for variant in ("old", "fixed"):
        h = booted(variant)
        h.q_exit()
        h.setup_clocks(False)
        want = -1 if variant == "old" else 0
        good &= check("%s: removal leaves power_count %d" % (variant, want),
                      h.phy.power_count == want)

    print("ALL CHECKS MET" if good else "SOME CHECKS FAILED")
    return 0 if good else 1


if __name__ == "__main__":
    sys.exit(main())
