#!/usr/bin/env python3
"""Test staging and execute the real calibration function with mocked MMIO.

Usage: python3 scripts/test-ufs-resume-experiment.py PRISTINE_QMP_UFS_C
This never accesses a PHY or suspends the host. Passing tests is not physical
resume validation, nor a replacement for building the complete driver.

The PCS reset (stage-ufs-resume-experiment.sh) and, on top, the diagnostic
of qmp-ufs-pcs-state-diagnostic.patch, which a build applies only with
APPLY_QMP_UFS_PCS_STATE_DIAGNOSTIC=1: what it reads, what it logs, that it
writes nothing and changes no result, and that its longer poll runs only
after a timeout.
"""
import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

repo = Path(__file__).resolve().parents[1]
original = Path(sys.argv[1]).read_text()


def run(*args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


def function(source, name):
    match = re.search(rf"^static \w+ {name}\([^)]*\)\n\{{.*?\n\}}\n", source, re.S | re.M)
    assert match, f"{name} not found"
    return match.group(0)


with tempfile.TemporaryDirectory(prefix="gts9u-ufs-test-") as tmp:
    tree = Path(tmp)
    source = tree / "drivers/phy/qualcomm/phy-qcom-qmp-ufs.c"
    source.parent.mkdir(parents=True)
    source.write_text(original)
    baseline = repo / "kernel/patches/qmp-ufs-reset-serdes-before-power-down-gts9u.patch"
    run("patch", "--batch", "--fuzz=0", "-d", tmp, "-p1", input=baseline.read_text())
    staged = source.read_bytes()
    orig = source.with_suffix(".c.orig")
    orig.write_bytes(b"Pre-existing developer backup; preserve it.\n")
    stage = str(repo / "scripts/stage-ufs-resume-experiment.sh")
    env = dict(os.environ, UFS_PCS_RESET_EXPERIMENTAL="0")
    run("bash", stage, tmp, env=env)
    assert source.read_bytes() == staged, "explicit opt-out changed PHY"
    env.pop("UFS_PCS_RESET_EXPERIMENTAL", None)  # Release default must apply the validated fix.
    run("bash", stage, tmp, env=env)
    candidate = source.read_bytes()
    assert orig.read_bytes() == b"Pre-existing developer backup; preserve it.\n"
    run("bash", stage, tmp, env=env)
    assert source.read_bytes() == candidate, "repeat staging is not idempotent"
    for value in ("0", "invalid"):
        env["UFS_PCS_RESET_EXPERIMENTAL"] = value
        result = subprocess.run(["bash", stage, tmp], env=env, capture_output=True)
        assert result.returncode != 0, f"accepted unsafe option/tree {value}"
        assert source.read_bytes() == candidate

    # A tree prepared before 2026-11-22 carries the SM-X910-only form.  The
    # stage script carries that hunk to take it out; pin it to the patch file
    # as it was (sha256 of the diff part of 109c079's copy).
    script = Path(stage).read_text()
    legacy = re.search(r"<<'LEGACY_PCS_RESET'\n(.*?\n)LEGACY_PCS_RESET\n",
                       script, re.S).group(1)
    assert hashlib.sha256(legacy.encode()).hexdigest() == \
        "59aff98d860aec148f4f88c509e6fdd99f87897190e3536316bd1da44a67b5ec", \
        "the stage script's legacy hunk is not the old patch"
    assert b"of_machine_is_compatible" not in candidate, "board check is back"
    source.write_bytes(staged)
    run("patch", "--batch", "--fuzz=0", "--no-backup-if-mismatch", "-d", tmp, "-p1",
        input=legacy)
    legacy_tree = source.read_bytes()
    assert b"SM-X910 experiment" in legacy_tree
    env["UFS_PCS_RESET_EXPERIMENTAL"] = "0"
    result = subprocess.run(["bash", stage, tmp], env=env, capture_output=True)
    assert result.returncode != 0, "opt-out accepted a tree with the old form"
    assert source.read_bytes() == legacy_tree
    env.pop("UFS_PCS_RESET_EXPERIMENTAL")
    run("bash", stage, tmp, env=env)
    assert source.read_bytes() == candidate, "old form not replaced by the new one"
    run("bash", stage, tmp, env=env)
    assert source.read_bytes() == candidate
    assert orig.read_bytes() == b"Pre-existing developer backup; preserve it.\n"

    # A comment alone must not make a damaged experiment look installed.
    damaged = candidate.replace(
        b'\t\tqphy_setbits(pcs, cfg->regs[QPHY_SW_RESET], SW_RESET);',
        b'\t\tqphy_setbits(pcs, cfg->regs[QPHY_SW_RESET], 0);')
    assert damaged != candidate
    source.write_bytes(damaged)
    env["UFS_PCS_RESET_EXPERIMENTAL"] = "1"
    result = subprocess.run(["bash", stage, tmp], env=env, capture_output=True)
    assert result.returncode != 0, "accepted a damaged reset sequence"
    assert source.read_bytes() == damaged
    # Nor a marker somewhere else in a tree without the reset, of either
    # form: patch --reverse --batch would take such a tree for one with it.
    for marker in (b"SM8550: assert the PCS reset before the calibration tables",
                   b"SM-X910 experiment: assert PCS reset before calibration tables"):
        elsewhere = staged + b"/* " + marker + b" */\n"
        source.write_bytes(elsewhere)
        result = subprocess.run(["bash", stage, tmp], env=env, capture_output=True)
        assert result.returncode != 0, f"accepted a stray marker: {marker}"
        assert source.read_bytes() == elsewhere
    source.write_bytes(candidate)

    match = re.search(r"static int qmp_ufs_phy_calibrate\(struct phy \*phy\)\n\{.*?\n\}",
                      candidate.decode(), re.S)
    assert match, "calibration function not found"
    harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>
#define __iomem
#define SW_RESET 1
#define SERDES_START 1
#define PCS_READY 1
#define PHY_INIT_COMPLETE_TIMEOUT 10000
enum { QPHY_SW_RESET, QPHY_START_CTRL, QPHY_PCS_READY_STATUS };
struct qmp_phy_cfg { bool no_pcs_sw_reset; unsigned regs[3]; };
static const struct qmp_phy_cfg sm8550_ufsphy_cfg = {false, {0, 4, 8}};
static const struct qmp_phy_cfg other_cfg = {false, {0, 4, 8}};
static const struct qmp_phy_cfg no_pcs_cfg = {true, {0, 4, 8}};
struct qmp_ufs { const struct qmp_phy_cfg *cfg; void *pcs, *ufs_reset, *dev; };
struct phy { struct qmp_ufs *qmp; };
static int assert_error, deassert_error, poll_error;
static char trace[64];
static void event(char c) { size_t n = strlen(trace); trace[n] = c; trace[n+1] = 0; }
static struct qmp_ufs *phy_get_drvdata(struct phy *p) { return p->qmp; }
/* Not static: unused is the expected case.  A board check fails the test. */
int of_machine_is_compatible(const char *s) {
    (void)s; assert(!"the PCS reset is for every SM8550 PHY, no board check");
    return 0;
}
static int reset_control_assert(void *p) { (void)p; event('A'); return assert_error; }
static int reset_control_deassert(void *p) { (void)p; event('D'); return deassert_error; }
static void qphy_setbits(void *p, unsigned r, unsigned v) {
    (void)p; (void)v; event(r == 0 ? 'R' : 'S');
}
static void qphy_clrbits(void *p, unsigned r, unsigned v) {
    (void)p; (void)r; (void)v; event('C');
}
static void qmp_ufs_init_registers(struct qmp_ufs *q, const struct qmp_phy_cfg *c) {
    (void)q; (void)c; event('T');
}
#define readl_poll_timeout(addr, val, cond, delay, timeout) \
    ((void)(addr), (val) = 1, (void)(cond), event('P'), poll_error)
#define dev_err(...) event('E')
'''
    harness += match.group(0)
    harness += r'''
static void check(const struct qmp_phy_cfg *cfg,
                  int ae, int de, int pe, int expected, const char *events) {
    char pcs[16];
    struct qmp_ufs q = { .cfg = cfg, .pcs = pcs };
    struct phy phy = { &q };
    trace[0] = 0;
    assert_error = ae; deassert_error = de; poll_error = pe;
    assert(qmp_ufs_phy_calibrate(&phy) == expected);
    assert(!strcmp(trace, events));
}
int main(void) {
    for (int i = 0; i < 3; i++)
        check(&sm8550_ufsphy_cfg, 0, 0, 0, 0, "ARTDCSP");
    check(&other_cfg, 0, 0, 0, 0, "ATDCSP");
    check(&no_pcs_cfg, 0, 0, 0, 0, "ATDSP");
    check(&sm8550_ufsphy_cfg, -5, 0, 0, -5, "A");
    check(&sm8550_ufsphy_cfg, 0, -5, 0, -5, "ARTD");
    check(&sm8550_ufsphy_cfg, 0, 0, -110, -110, "ARTDCSPE");
    puts("PASS: calibration ordering, repeated cycles, SM8550-only guard, error propagation");
}
'''
    test_c = tree / "calibrate-test.c"
    test_c.write_text(harness)
    exe = tree / "calibrate-test"
    run("cc", "-std=gnu11", "-Wall", "-Wextra", "-Werror", str(test_c), "-o", str(exe))
    run(str(exe))

    # The diagnostic goes on top of the PCS reset, strictly, as
    # build-mainline-kernel.sh applies it, and leaves the PCS reset as it is:
    # the stage script still finds it whole in a tree that has both.
    diagnostic = repo / "kernel/patches/qmp-ufs-pcs-state-diagnostic.patch"
    applied = run("patch", "--forward", "--batch", "--fuzz=0", "--no-backup-if-mismatch", "-d", tmp,
                  "-p1", input=diagnostic.read_text(), capture_output=True).stdout
    assert "offset" not in applied and "fuzz" not in applied, applied
    with_diagnostic = source.read_text()
    run("bash", stage, tmp, env=dict(os.environ))
    assert source.read_text() == with_diagnostic, "the stage script changed a tree with the diagnostic"
    env["UFS_PCS_RESET_EXPERIMENTAL"] = "0"
    result = subprocess.run(["bash", stage, tmp], env=env, capture_output=True)
    assert result.returncode != 0, "opt-out accepted a tree with the PCS reset and the diagnostic"
    # The two functions it adds only read: no register write, no reset, no
    # clock or supply call.
    state = function(with_diagnostic, "qmp_ufs_dbg_state")
    late = function(with_diagnostic, "qmp_ufs_dbg_timeout")
    for body in (state, late):
        for forbidden in ("writel", "qphy_setbits", "qphy_clrbits", "reset_control", "clk_",
                          "regulator_", "udelay", "msleep"):
            assert forbidden not in body, forbidden
    calibrate = function(with_diagnostic, "qmp_ufs_phy_calibrate")
    diag = r'''
#include <assert.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>
#define __iomem
#define SW_RESET 1
#define SERDES_START 1
#define PCS_READY 1
#define PHY_INIT_COMPLETE_TIMEOUT 10000
#define USEC_PER_SEC 1000000L
#define KERN_DEBUG "<7>"
#define KERN_WARNING "<4>"
#define QSERDES_V6_COM_CMN_STATUS 0x1d0
#define QSERDES_V6_COM_C_READY_STATUS 0x1f8
typedef long long ktime_t;
enum { QPHY_SW_RESET, QPHY_START_CTRL, QPHY_PCS_READY_STATUS, QPHY_PCS_POWER_DOWN_CONTROL };
struct qmp_phy_cfg { bool no_pcs_sw_reset; unsigned regs[4]; };
/* The v6 layout: SW_RESET 0x8, START 0x0, READY_STATUS 0x1a8, POWER_DOWN 0x4. */
static const struct qmp_phy_cfg sm8550_ufsphy_cfg = {false, {0x8, 0x0, 0x1a8, 0x4}};
static const struct qmp_phy_cfg other_cfg = {false, {0x8, 0x0, 0x1a8, 0x4}};
struct qmp_ufs { const struct qmp_phy_cfg *cfg; char *serdes, *pcs; void *ufs_reset, *dev; };
struct phy { struct qmp_ufs *qmp; };
static char pcsmem[0x400], serdesmem[0x400];
static int assert_error, deassert_error, poll_error, late_error;
static long late_timeout;
static char trace[256], reads[256], logbuf[4096];
static void event(const char *s) { strcat(trace, s); }
/* A read: its block and offset go to reads, an r to the trace. */
static unsigned readl(const void *a) {
    const char *p = a;
    char b[32];
    if (p >= pcsmem && p < pcsmem + sizeof(pcsmem))
        snprintf(b, sizeof(b), " p%lx", (long)(p - pcsmem));
    else if (p >= serdesmem && p < serdesmem + sizeof(serdesmem))
        snprintf(b, sizeof(b), " s%lx", (long)(p - serdesmem));
    else
        snprintf(b, sizeof(b), " ?");
    strcat(reads, b);
    event("r");
    return 0x5a;
}
static void log_line(const char *level, const char *fmt, ...) {
    va_list ap;
    strcat(logbuf, level);
    va_start(ap, fmt);
    vsnprintf(logbuf + strlen(logbuf), sizeof(logbuf) - strlen(logbuf), fmt, ap);
    va_end(ap);
}
#define dev_printk(level, dev, fmt, ...) ((void)(dev), log_line(level, fmt, __VA_ARGS__))
#define dev_warn(dev, fmt, ...) ((void)(dev), log_line(KERN_WARNING, fmt, ##__VA_ARGS__))
#define dev_err(dev, fmt, ...) ((void)(dev), event("E"), log_line("<3>", fmt, ##__VA_ARGS__))
static ktime_t ktime_get(void) { event("K"); return 0; }
static long long ktime_us_delta(ktime_t a, ktime_t b) { (void)a; (void)b; return 1234; }
/* The calibration's own poll (10 ms) and the diagnostic's (the rest of 1 s). */
static int poll(const void *addr, long delay, long timeout) {
    assert(addr == pcsmem + 0x1a8);
    assert(delay == 200);
    if (timeout == PHY_INIT_COMPLETE_TIMEOUT) { event("P"); return poll_error; }
    late_timeout = timeout;
    event("L");
    return late_error;
}
#define readl_poll_timeout(addr, val, cond, delay, timeout) \
    ((val) = 0, (void)(cond), poll((addr), (delay), (timeout)))
static struct qmp_ufs *phy_get_drvdata(struct phy *p) { return p->qmp; }
static int reset_control_assert(void *p) { (void)p; event("A"); return assert_error; }
static int reset_control_deassert(void *p) { (void)p; event("D"); return deassert_error; }
static void qphy_setbits(void *p, unsigned r, unsigned v) {
    (void)v; assert(p == pcsmem); event(r == 0x8 ? "R" : r == 0x0 ? "S" : "?");
}
static void qphy_clrbits(void *p, unsigned r, unsigned v) {
    (void)v; assert(p == pcsmem && r == 0x8); event("C");
}
static void qmp_ufs_init_registers(struct qmp_ufs *q, const struct qmp_phy_cfg *c) {
    (void)q; (void)c; event("T");
}
'''
    diag += state + late + calibrate
    diag += r'''
/* The six registers a state line reads, in any order. */
#define STATE_READS " p0 p1a8 p4 p8 s1d0 s1f8"
static int sorted_equal(const char *got, const char *want) {
    char a[256], b[256], *wa[16], *wb[16];
    int na = 0, nb = 0;
    strcpy(a, got); strcpy(b, want);
    for (char *t = strtok(a, " "); t; t = strtok(NULL, " ")) wa[na++] = t;
    for (char *t = strtok(b, " "); t; t = strtok(NULL, " ")) wb[nb++] = t;
    if (na != nb) return 0;
    for (int i = 0; i < na; i++) {
        int found = 0;
        for (int j = 0; j < nb; j++)
            if (wb[j] && !strcmp(wa[i], wb[j])) { wb[j] = NULL; found = 1; break; }
        if (!found) return 0;
    }
    return 1;
}
static int run(const struct qmp_phy_cfg *cfg, int ae, int de, int pe, int le) {
    struct qmp_ufs q = { .cfg = cfg, .serdes = serdesmem, .pcs = pcsmem };
    struct phy phy = { &q };
    trace[0] = reads[0] = logbuf[0] = 0;
    late_timeout = 0;
    assert_error = ae; deassert_error = de; poll_error = pe; late_error = le;
    return qmp_ufs_phy_calibrate(&phy);
}
int main(void) {
    /* A calibration that succeeds: the entry state, read before anything
       is written, one debug line, the same writes as without it. */
    for (int i = 0; i < 2; i++) {
        assert(run(&sm8550_ufsphy_cfg, 0, 0, 0, 0) == 0);
        assert(!strcmp(trace, "rrrrrrARTDCSP"));
        assert(sorted_equal(reads, STATE_READS));
        assert(!strcmp(logbuf, "<7>calibration entry: SW_RESET 0x5a START 0x5a PWRDN 0x5a READY 0x5a "
                               "C_READY 0x5a CMN 0x5a\n"));
        assert(late_timeout == 0);
    }
    /* A timeout, then PCS_READY within the second: the state, the time,
       and the calibration fails all the same. */
    assert(run(&sm8550_ufsphy_cfg, 0, 0, -110, 0) == -110);
    assert(!strcmp(trace, "rrrrrrARTDCSPEKrrrrrrLK"));
    assert(late_timeout == 990000);
    assert(strstr(logbuf, "<3>phy initialization timed-out\n<4>after the PCS_READY timeout: SW_RESET 0x5a "));
    assert(strstr(logbuf, "<4>PCS_READY 1234 us after the timeout, within 1 s of SERDES_START\n"));
    /* A timeout and no PCS_READY within the second: the state again. */
    assert(run(&sm8550_ufsphy_cfg, 0, 0, -110, -110) == -110);
    assert(!strcmp(trace, "rrrrrrARTDCSPEKrrrrrrLrrrrrr"));
    assert(strstr(logbuf, "<4>no PCS_READY within 1 s of SERDES_START\n<4>1 s after SERDES_START: SW_RESET "));
    /* Another PHY: nothing of the diagnostic, as before. */
    assert(run(&other_cfg, 0, 0, 0, 0) == 0);
    assert(!strcmp(trace, "ATDCSP") && !reads[0] && !logbuf[0]);
    assert(run(&other_cfg, 0, 0, -110, 0) == -110);
    assert(!strcmp(trace, "ATDCSPE") && !reads[0] && late_timeout == 0);
    /* Errors before the poll: as without it, after the entry line. */
    assert(run(&sm8550_ufsphy_cfg, -5, 0, 0, 0) == -5);
    assert(!strcmp(trace, "rrrrrrA"));
    assert(run(&sm8550_ufsphy_cfg, 0, -5, 0, 0) == -5);
    assert(!strcmp(trace, "rrrrrrARTD") && late_timeout == 0);
    puts("PASS: the diagnostic reads the six registers before the PCS reset, writes nothing, "
         "changes no result, and polls on only after a timeout");
}
'''
    diag_c = tree / "diagnostic-test.c"
    diag_c.write_text(diag)
    diag_exe = tree / "diagnostic-test"
    run("cc", "-std=gnu11", "-Wall", "-Wextra", "-Werror", str(diag_c), "-o", str(diag_exe))
    run(str(diag_exe))
    print("PASS: explicit opt-out unchanged, strict patch application, repeat staging, stale-tree refusal, "
          "old-form upgrade, the diagnostic on top")
