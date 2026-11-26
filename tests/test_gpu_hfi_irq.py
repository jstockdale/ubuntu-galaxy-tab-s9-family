"""The GPU controller's (GMU's) HFI interrupt and HFI wait (2026-11-26: a
frequency vote waited 1 s and gave up with its answer in the queue, traced on
the SM-X910, docs/gpu-recovery.md).

Two patches, in every build on both boards, with no variable:
msm-adreno-a6xx-hfi-irq-leave-polled-bits.patch (a6xx_gmu.c: the hfi
interrupt handler no longer clears the MSGQ and OOB acknowledge bits that
other code polls for, and masks again a bit that raised it unmasked) and
msm-adreno-a6xx-hfi-take-queued-response.patch (a6xx_hfi.c: the HFI wait also
ends on a response that is in the queue without its MSGQ bit).  Their lines in
the build script are checked where they stand.  With the pinned kernel
checkout at hand (KERNEL_SRC, or the build script's default place; only read,
with git show) the build script's own lines put them in after the files'
other patches, with and without APPLY_SM8550_SLEEP_FIXES, with no offset and
no fuzz, once.  With a C compiler as well, the driver's own functions, taken
from the files the build makes with neither patch, either one and both, run
on the build host against a simulated GMU, clock and interrupt line: the old
code loses the vote's answer in each way the first captured timeout allows,
the new code takes it in all of them, the table says which patch covers
which case, and no case makes the interrupt storm.
Nothing touches a GPU.
"""
from pathlib import Path
import os
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "scripts/build-mainline-kernel.sh"
STAGE = ROOT / "scripts/stage-gpu-recovery-experiment.sh"
PATCHES = ROOT / "kernel/patches"
IRQ = PATCHES / "msm-adreno-a6xx-hfi-irq-leave-polled-bits.patch"
WAIT = PATCHES / "msm-adreno-a6xx-hfi-take-queued-response.patch"
STALE = PATCHES / "msm-adreno-a6xx-fix-stale-rpmh-votes.patch"
ADRENO = "drivers/gpu/drm/msm/adreno"
GMU = f"{ADRENO}/a6xx_gmu.c"
HFI = f"{ADRENO}/a6xx_hfi.c"
# The lines the build script knows the two patches by.
IRQ_MARKER = "Leave those bits to them"
WAIT_MARKER = "response that is already in the queue ends the wait too"
FIXES_TAG = 'Fixes: df0dff132905 ("drm/msm/a6xx: Poll for HFI responses")'


def kernel_source():
    """The pinned kernel checkout, as tests/test_typec_dock_roles.py finds it."""
    base = os.environ.get("UBUNTU_WORKDIR", str(Path.home() / "ubuntu-gts9u"))
    source = Path(os.environ.get("KERNEL_SRC", base + "/linux-mainline"))
    return source if (source / ".git").exists() and shutil.which("patch") else None


def pinned(path):
    return subprocess.run(["git", "-C", str(kernel_source()), "show", "HEAD:" + path],
                          check=True, capture_output=True, text=True).stdout


def compiler():
    return shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")


def apply_unless():
    return re.search(r"^apply_unless\(\) \{.*?^\}\n", BUILD.read_text(), re.M | re.S).group(0)


def apply_unless_calls():
    """Every top-level apply_unless call of the build script, in order: (the
    call's text, its marker, its file, its patch)."""
    return [(m.group(0), m.group(1), m.group(2), m.group(3)) for m in
            re.finditer(r"^apply_unless '([^']*)' \\\n\t(\S+)(?: \\\n\t| )(\S+)\n", BUILD.read_text(), re.M)]


def fix_lines():
    """The build script's two calls that apply the patches."""
    calls = {patch: text for text, _, _, patch in apply_unless_calls()}
    return calls[IRQ.name] + calls[WAIT.name]


def gmu_sleep_fix():
    """sleep_fix() and its call for the stale-RPMh-vote fix of a6xx_gmu.c."""
    text = BUILD.read_text()
    function = re.search(r"^sleep_fix\(\) \{ # FILE LINE PATCH\n.*?^\}\n", text, re.M | re.S).group(0)
    call = re.search(r"^sleep_fix drivers/gpu/drm/msm/adreno/a6xx_gmu\.c \\\n\t[^\n]*\\\n\t" + re.escape(STALE.name)
                     + "\n", text, re.M).group(0)
    return function + call


def run_bash(script, *args, env=None):
    return subprocess.run(["bash", "-c", "set -euo pipefail\n" + script, "build", *args],
                          capture_output=True, text=True, env=env)


def files(tree):
    return {str(p.relative_to(tree)): p.read_text() for p in tree.rglob("*") if p.is_file()}


def added(patch):
    return [line[1:] for line in patch.read_text().splitlines() if line.startswith("+") and not line.startswith("+++")]


def removed(patch):
    return [line[1:] for line in patch.read_text().splitlines() if line.startswith("-") and not line.startswith("---")]


def function(text, head):
    """The C function of TEXT that starts with the line HEAD, to its closing brace."""
    return re.search("^" + re.escape(head) + r"\n\{\n.*?^\}\n", text, re.M | re.S).group(0)


def step(script, root, fixes):
    """SCRIPT, a part of the build script, on ROOT with APPLY_SM8550_SLEEP_FIXES=FIXES."""
    return run_bash("kernel_tree=$1 pat=$2 apply_sm8550_sleep_fixes=$3\n" + script, str(root), str(PATCHES), fixes)


def strict(root, patch, reverse=False):
    return subprocess.run(["patch", "--batch", "--fuzz=0", "--no-backup-if-mismatch", "-d", str(root), "-p1"]
                          + (["--reverse", "--force"] if reverse else ["--forward"]),
                          input=patch.read_text(), capture_output=True, text=True)


def build_tree(root, fixes, fault_wait="1"):
    """The four adreno files as the build has them when the two calls run:
    pinned, the GPU recovery stage script (GPU_FAULT_WAIT_EXPERIMENTAL=
    FAULT_WAIT), the adreno parameter patches by the build script's own
    calls, and the stale-RPMh-vote fix's sleep_fix call with
    APPLY_SM8550_SLEEP_FIXES=FIXES."""
    for base in ("a6xx_gmu.c", "a6xx_hfi.c", "adreno_device.c", "a6xx_gpu.c"):
        path = root / ADRENO / base
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(pinned(f"{ADRENO}/{base}"))
    env = dict(os.environ, GPU_FAULT_WAIT_EXPERIMENTAL=fault_wait)
    subprocess.run(["bash", str(STAGE), str(root)], check=True, capture_output=True, env=env)
    params = "".join(call for call, _, target, patch in apply_unless_calls()
                     if target.startswith(ADRENO + "/") and patch not in (IRQ.name, WAIT.name))
    if params.count("apply_unless '") != 5:
        raise AssertionError("expected the five adreno parameter patches:\n" + params)
    for script in (apply_unless() + params, gmu_sleep_fix()):
        result = step(script, root, fixes)
        if result.returncode:
            raise AssertionError(result.stdout + result.stderr)
    return root


class BuildScriptTest(unittest.TestCase):
    def test_both_go_into_every_build_after_the_sleep_fixes(self):
        """No variable and no board: the two calls stand at the top level of
        the script (not inside an if), one after the other, right after the
        sleep fixes' last call with nothing but comments between, so that
        the stale-RPMh-vote fix keeps the line numbers it was made for; and
        after the stage script and every other call that patches
        a6xx_gmu.c or a6xx_hfi.c."""
        text = BUILD.read_text()
        lines = fix_lines()
        self.assertEqual(text.count(lines), 1)
        self.assertEqual(lines, f"apply_unless '{IRQ_MARKER}' \\\n\t{GMU} \\\n\t{IRQ.name}\n"
                                f"apply_unless '{WAIT_MARKER}' \\\n\t{HFI} \\\n\t{WAIT.name}\n")
        last_fix = "\tufs-qcom-enable-only-lane-clocks.patch\n"
        end = text.index(last_fix, text.index("sleep_fix() { # FILE LINE PATCH\n")) + len(last_fix)
        between = text[end:text.index(lines)]
        self.assertTrue(between and all(l.startswith("#") for l in between.splitlines()), between)
        self.assertLess(text.index(gmu_sleep_fix().split("}\n", 1)[1]), text.index(lines))
        self.assertLess(text.index('bash "$repo/scripts/stage-gpu-recovery-experiment.sh"'), text.index(lines))
        for call, _, target, patch in apply_unless_calls():
            if target in (GMU, HFI) and patch not in (IRQ.name, WAIT.name):
                self.assertLess(text.index(call), text.index(lines), patch)
        for part in (between, lines):
            self.assertNotIn("DEVICE", part)
            self.assertNotIn("gts9", part)
            self.assertNotIn("$apply_", part)
        # At the top level: no if, case or loop is open where they stand.
        before = text[:text.index(lines)]
        opened = len(re.findall(r"^(?:if|case|for|while) ", before, re.M))
        closed = len(re.findall(r"^(?:fi|esac|done)\b", before, re.M))
        self.assertEqual(opened, closed)

    def test_the_markers_are_lines_the_patches_add_and_no_other_patch_has(self):
        """Each marker is part of one line its patch adds, the same for grep
        -q's basic regular expression as for a fixed string, and in no other
        patch."""
        for marker, patch in ((IRQ_MARKER, IRQ), (WAIT_MARKER, WAIT)):
            with self.subTest(patch.name):
                self.assertIsNone(re.search(r"[.*\[\]^$\\]", marker))
                self.assertEqual(len([l for l in added(patch) if marker in l]), 1)
                self.assertEqual([p.name for p in PATCHES.glob("*.patch") if marker in p.read_text()],
                                 [patch.name])

    def test_each_patch_reads_like_a_mainline_submission_of_one_file(self):
        """The port's own patch format: a subject line in the subsystem's
        style, the reasons and the evidence, the Fixes: tag of the commit
        that made MSGQ a polled bit, then the diff of one file."""
        for patch, target, subject in (
                (IRQ, GMU, "drm/msm/a6xx: Do not clear polled GMU2HOST bits in the HFI interrupt"),
                (WAIT, HFI, "drm/msm/a6xx: Take an HFI response from the queue without its MSGQ bit")):
            with self.subTest(patch.name):
                text = patch.read_text()
                self.assertTrue(text.startswith(f"Subject: [PATCH] {subject}\n\n"), text[:120])
                self.assertLessEqual(len(subject), 75)
                header = text[:text.index("\n--- a/")]
                self.assertEqual(header.count(FIXES_TAG), 1)
                self.assertTrue(header.rstrip("\n").endswith(FIXES_TAG))
                self.assertEqual(re.findall(r"^\+\+\+ b/(\S+)", text, re.M), [target])
                self.assertEqual(re.findall(r"^--- a/(\S+)", text, re.M), [target])
                self.assertTrue(all(len(l) <= 75 for l in header.splitlines()[1:]), header)

    def test_the_handler_leaves_the_polled_bits_and_masks_again(self):
        """The handler no longer writes back what it read; it clears every
        bit but MSGQ and the OOB acknowledge bits of a6xx_gmu_oob_bits[], and
        those too only when the mask it wrote again does not hold; it still
        returns IRQ_HANDLED alone (never IRQ_NONE, which could get the line
        polled by the spurious-interrupt code while the GMU is off)."""
        self.assertIn("\tgmu_write(gmu, REG_A6XX_GMU_GMU2HOST_INTR_CLR, status);", removed(IRQ))
        code = "\n".join(added(IRQ))
        self.assertNotIn("INTR_CLR, status)", code)
        self.assertNotIn("IRQ_NONE", code)
        for line in ("\tpolled = (A6XX_GMU_GMU2HOST_INTR_INFO_MSGQ | a6xx_gmu_oob_ack_bits(gmu)) &",
                     "\tclear = status & ~polled;",
                     "\tunexpected = (status | polled) & ~mask & ~A6XX_HFI_IRQ_MASK;",
                     "\t\tgmu_write(gmu, REG_A6XX_GMU_GMU2HOST_INTR_MASK, ~A6XX_HFI_IRQ_MASK);",
                     "\t\trestored = gmu_read(gmu, REG_A6XX_GMU_GMU2HOST_INTR_MASK);",
                     "\t\tclear |= status & unexpected & ~restored;",
                     "\t\tgmu_write(gmu, REG_A6XX_GMU_GMU2HOST_INTR_CLR, clear);",
                     "\t\tint ack = gmu->legacy ? a6xx_gmu_oob_bits[i].ack :",
                     "static u32 a6xx_gmu_oob_ack_bits(struct a6xx_gmu *gmu);"):
            self.assertIn(line, added(IRQ), line)
        # The mask macro moved above the handler, unchanged.
        self.assertEqual([l for l in removed(IRQ) if "A6XX_HFI_IRQ_MASK" in l or "CM3_FAULT)" in l],
                         ["#define A6XX_HFI_IRQ_MASK \\", "\t(A6XX_GMU_GMU2HOST_INTR_INFO_CM3_FAULT)"])
        self.assertIn("#define A6XX_HFI_IRQ_MASK \\\n+\t(A6XX_GMU_GMU2HOST_INTR_INFO_CM3_FAULT)\n", IRQ.read_text())
        # The one log line is a once-per-boot warning.
        self.assertEqual(code.count("dev_warn_once("), 1)
        self.assertNotRegex(code, r"dev_(err|warn|info)(_ratelimited)?\(")

    def test_the_wait_ends_on_a_queued_response_too(self):
        """The poll's condition is MSGQ or a response queue that is not
        empty; nothing else of the wait changes; the message is read after a
        read barrier that follows the write index."""
        self.assertEqual(removed(WAIT), ["\t\t/* Wait for a response */",
                                         "\t\t\tval & A6XX_GMU_GMU2HOST_INTR_INFO_MSGQ, 100, 1000000);"])
        for line in ("\t\t\t(val & A6XX_GMU_GMU2HOST_INTR_INFO_MSGQ) ||",
                     "\t\t\ta6xx_hfi_queue_has_msg(&gmu->queues[HFI_RESPONSE_QUEUE]),",
                     "\t\t\t100, 1000000);",
                     "\treturn header->read_index != READ_ONCE(header->write_index);",
                     "\tdma_rmb();"):
            self.assertIn(line, added(WAIT), line)
        text = WAIT.read_text()
        self.assertIn("\t\treturn 0;\n \t}\n \n+\t/* Ensure the message is read only after the write index that covers it */\n"
                      "+\tdma_rmb();\n+\n \thdr = queue->data[index];\n", text)


@unittest.skipUnless(kernel_source(), "no pinned kernel checkout")
class PinnedSourceTest(unittest.TestCase):
    """The patches on the pinned adreno files, in the order and the way the
    build applies them."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.tmp = Path(directory.name)

    def tree(self, name, fixes, fault_wait="1"):
        return build_tree(self.tmp / name, fixes, fault_wait)

    def step(self, script, root, fixes):
        return step(script, root, fixes)

    def strict(self, root, patch, reverse=False):
        return strict(root, patch, reverse)

    def test_the_build_s_calls_put_them_in_with_no_offset_once_with_and_without_the_sleep_fixes(self):
        """The two files change and nothing else, with no offset, no fuzz and
        no backup; the same files as the two patches applied strictly; a
        reused tree is left as it is; with the sleep fixes the stale-RPMh
        fix is in as well, and the two builds differ by it alone."""
        built = {}
        for fixes in ("0", "1"):
            with self.subTest(sleep_fixes=fixes):
                root = self.tree(f"build-{fixes}", fixes)
                before = files(root)
                for marker, path in ((IRQ_MARKER, GMU), (WAIT_MARKER, HFI)):
                    self.assertNotIn(marker, before[path])
                self.assertEqual(before[GMU].count("if (!test_and_clear_bit(GMU_STATUS_FW_START, &gmu->status))"),
                                 int(fixes))
                result = self.step(apply_unless() + fix_lines(), root, fixes)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, f"patching file {GMU}\npatching file {HFI}\n")
                after = files(root)
                self.assertEqual(sorted(after), sorted(before))
                self.assertEqual(sorted(p for p in after if after[p] != before[p]), [GMU, HFI])
                self.assertEqual(after[GMU].count(IRQ_MARKER), 1)
                self.assertEqual(after[HFI].count(WAIT_MARKER), 1)
                plain = self.tree(f"strict-{fixes}", fixes)
                for patch in (IRQ, WAIT):
                    result = self.strict(plain, patch)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertNotRegex(result.stdout, "offset|fuzz")
                self.assertEqual(files(plain), after)
                # A reused tree: nothing changes.
                result = self.step(apply_unless() + fix_lines(), root, fixes)
                self.assertEqual((result.returncode, result.stdout), (0, ""), result.stderr)
                self.assertEqual(files(root), after)
                # And both come out whole again.
                for patch in (WAIT, IRQ):
                    result = self.strict(plain, patch, reverse=True)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(files(plain), before)
                built[fixes] = after
        self.assertEqual(built["0"][HFI], built["1"][HFI])
        undo = self.tmp / "undo"
        for path, text in built["1"].items():
            (undo / path).parent.mkdir(parents=True, exist_ok=True)
            (undo / path).write_text(text)
        result = self.strict(undo, STALE, reverse=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(files(undo), built["0"])

    def test_the_fault_wait_opt_out_takes_them_without_fuzz(self):
        """GPU_FAULT_WAIT_EXPERIMENTAL=0, a development opt-out, leaves out
        the bounded fault-capture wait the hunks were made on: they still go
        in, without fuzz, 7 and 1 lines up."""
        root = self.tree("opt-out", "0", fault_wait="0")
        for patch, offset in ((IRQ, "-7"), (WAIT, "-1")):
            with self.subTest(patch.name):
                result = self.strict(root, patch)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertNotIn("fuzz", result.stdout)
                self.assertEqual(re.findall(r"offset (-?\d+) lines?", result.stdout), [offset])


HARNESS = r"""
/* The GMU's HFI path on the build host: the driver's own functions, from
 * the files the build makes, against a simulated GMU, clock and interrupt
 * line.  Times are in microseconds. */
#include <errno.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef uint8_t u8;
typedef uint16_t u16;
typedef uint32_t u32;
typedef int32_t s32;
typedef uint64_t u64;
typedef int spinlock_t;
typedef struct { int counter; } atomic_t;
typedef enum { IRQ_NONE, IRQ_HANDLED } irqreturn_t;
#define __packed __attribute__((packed))
#define BIT(n) (1U << (n))
#define ARRAY_SIZE(a) (sizeof(a) / sizeof((a)[0]))
#define READ_ONCE(x) (*(const volatile __typeof__(x) *)&(x))
#define WRITE_ONCE(x, v) (*(volatile __typeof__(x) *)&(x) = (v))
#define ALIGN(x, a) (((x) + ((__typeof__(x))(a) - 1)) & ~((__typeof__(x))(a) - 1))
#define BUG_ON(c) do { if (c) { printf("BUG\n"); exit(3); } } while (0)
#define WARN_ON_ONCE(c) ((void)(c))
#define min_t(t, a, b) ((t)(a) < (t)(b) ? (t)(a) : (t)(b))
#define CIRC_CNT(head, tail, size) (((head) - (tail)) & ((size) - 1))
#define CIRC_SPACE(head, tail, size) CIRC_CNT((tail), ((head) + 1), (size))
#define container_of(ptr, type, member) ((type *)((char *)(ptr) - __builtin_offsetof(type, member)))
#define spin_lock(l) ((void)(l))
#define spin_unlock(l) ((void)(l))
#define atomic_inc_return(a) (++(a)->counter)
#define mutex_is_locked(m) 1
#define dma_mb() __sync_synchronize()
#define dma_rmb() __sync_synchronize()
#define msecs_to_jiffies(ms) (ms)
#define DRM_DEV_ERROR(dev, fmt, ...) printf("ERROR " fmt, ##__VA_ARGS__)
#define dev_err_ratelimited(dev, fmt, ...) printf("ERROR " fmt, ##__VA_ARGS__)
#define dev_warn_once(dev, fmt, ...) do { static bool done_; \
	if (!done_) { done_ = true; printf("WARN " fmt, ##__VA_ARGS__); } } while (0)

struct device { int unused; };
struct completion { int done; };
static bool completion_done(struct completion *c) { return c->done; }
static unsigned long wait_for_completion_timeout(struct completion *c, unsigned long t)
{ (void)t; return c->done; }
static void wait_for_completion(struct completion *c) { (void)c; }

#include "a6xx_hfi.h"
@ENUM@;

struct a6xx_gmu { struct device *dev; int lock; bool legacy;
	struct a6xx_hfi_queue queues[HFI_MAX_QUEUES]; };
struct adreno_gpu { struct completion fault_coredump_done; };
struct a6xx_gpu { struct adreno_gpu base; struct a6xx_gmu gmu; };
static bool adreno_has_rgmu(struct adreno_gpu *g) { (void)g; return false; }

@REGS@

static u64 sim_now;
static u32 gmu_read(struct a6xx_gmu *gmu, u32 reg);
static void gmu_write(struct a6xx_gmu *gmu, u32 reg, u32 val);
static void sim_advance(u64 us);

/* readl_poll_timeout(), as include/linux/iopoll.h has it, on the clock */
#define gmu_poll_timeout(gmu, addr, val, cond, sleep_us, timeout_us) ({ \
	u64 timeout_ = sim_now + (timeout_us); \
	int ret_; \
	for (;;) { \
		bool expired_ = sim_now > timeout_; \
		(val) = gmu_read(gmu, addr); \
		if (cond) { ret_ = 0; break; } \
		if (expired_) { ret_ = -ETIMEDOUT; break; } \
		sim_advance(sleep_us); \
	} \
	ret_; })

static int faults;
static void a6xx_gmu_fault(struct a6xx_gmu *gmu) { (void)gmu; faults++; }

/* ---- the driver's code ---- */
@GMU_CODE@
@HFI_CODE@
/* ---- the simulation ---- */
static struct a6xx_gpu gpu;
static struct device dev;
static u32 qmem[HFI_MAX_QUEUES][1024];
static struct a6xx_hfi_queue_header qhdr[HFI_MAX_QUEUES];
static u32 info, mask;
static bool mask_sticks = true;
static long answer_at = 150, msgq_lead, irq_latency = 46, oob_latency = 50;
static bool msgq_never, quiet;
static u32 extra_bits;
static long t_queue = -1, t_msgq = -1, t_oob = -1, t_irq = -1, t_forced = -1;
static long forced_irq = -1;
static u32 answered_hdr, oob_ack, gmu_seq = 6;
static int handler_runs;

static void line_check(void)
{
	if ((info & ~mask) && t_irq < 0)
		t_irq = sim_now + irq_latency;
}

static u32 gmu_read(struct a6xx_gmu *g, u32 reg)
{
	(void)g;
	if (reg == REG_A6XX_GMU_GMU2HOST_INTR_INFO)
		return info;
	if (reg == REG_A6XX_GMU_GMU2HOST_INTR_MASK)
		return mask;
	return 0;
}

static void doorbell(u32 val)
{
	struct a6xx_hfi_queue_header *h = &qhdr[HFI_COMMAND_QUEUE];
	unsigned int i;

	if (val & 1) {
		/* The GMU takes the command and answers answer_at later */
		answered_hdr = qmem[HFI_COMMAND_QUEUE][h->read_index];
		h->read_index = h->write_index;
		t_queue = sim_now + answer_at;
		t_msgq = msgq_never ? -1 : t_queue - msgq_lead;
		if (forced_irq >= 0)
			t_forced = sim_now + forced_irq;
	}
	for (i = 0; i < ARRAY_SIZE(a6xx_gmu_oob_bits); i++) {
		int set = gpu.gmu.legacy ? a6xx_gmu_oob_bits[i].set : a6xx_gmu_oob_bits[i].set_new;
		int ack = gpu.gmu.legacy ? a6xx_gmu_oob_bits[i].ack : a6xx_gmu_oob_bits[i].ack_new;

		if (set && (val & BIT(set))) {
			oob_ack = BIT(ack);
			t_oob = sim_now + oob_latency;
			if (forced_irq >= 0)
				t_forced = sim_now + forced_irq;
		}
	}
}

static void gmu_write(struct a6xx_gmu *g, u32 reg, u32 val)
{
	(void)g;
	if (reg == REG_A6XX_GMU_GMU2HOST_INTR_CLR)
		info &= ~val;
	else if (reg == REG_A6XX_GMU_GMU2HOST_INTR_MASK) {
		if (mask_sticks)
			mask = val;
	} else if (reg == REG_A6XX_GMU_HOST2GMU_INTR_SET)
		doorbell(val);
	line_check();
}

static void answer(void)
{
	struct a6xx_hfi_queue_header *h = &qhdr[HFI_RESPONSE_QUEUE];
	u32 i = h->write_index;

	/* As the captured answer: the GMU's own sequence number, an ACK of
	 * three words with the command's id, the command's header, the error */
	qmem[HFI_RESPONSE_QUEUE][i] = (gmu_seq++ << 20) | (HFI_MSG_ACK << 16) | (3 << 8) |
		HFI_HEADER_ID(answered_hdr);
	qmem[HFI_RESPONSE_QUEUE][i + 1] = answered_hdr;
	qmem[HFI_RESPONSE_QUEUE][i + 2] = 0;
	qmem[HFI_RESPONSE_QUEUE][i + 3] = 0xfafafafa;
	h->write_index = (i + 4) % h->size;
}

static long next_event(u64 end)
{
	long t[] = { t_queue, t_msgq, t_oob, t_irq, t_forced }, best = -1;
	unsigned int i;

	for (i = 0; i < ARRAY_SIZE(t); i++)
		if (t[i] >= 0 && (u64)t[i] <= end && (best < 0 || t[i] < best))
			best = t[i];
	return best;
}

static void sim_advance(u64 us)
{
	u64 end = sim_now + us;
	long next;

	while ((next = next_event(end)) >= 0) {
		sim_now = next;
		if (t_queue == next) {
			t_queue = -1;
			answer();
		}
		if (t_msgq == next) {
			t_msgq = -1;
			info |= A6XX_GMU_GMU2HOST_INTR_INFO_MSGQ | extra_bits;
		}
		if (t_oob == next) {
			t_oob = -1;
			info |= oob_ack | extra_bits;
		}
		line_check();
		if (t_irq == next || t_forced == next) {
			bool forced = t_forced == next;

			if (t_irq == next)
				t_irq = -1;
			if (forced)
				t_forced = -1;
			/* A level-triggered line: the handler runs if the line
			 * is still up, or when the scenario says it runs anyway */
			if ((info & ~mask) || forced) {
				if (!quiet)
					printf("IRQ at %llu: status 0x%x mask 0x%x queue %u/%u\n",
					       (unsigned long long)sim_now, info, mask,
					       qhdr[HFI_RESPONSE_QUEUE].read_index,
					       qhdr[HFI_RESPONSE_QUEUE].write_index);
				if (++handler_runs > 1000) {
					printf("STORM\n");
					exit(4);
				}
				a6xx_hfi_irq(0, &gpu.gmu);
			}
			line_check();
		}
	}
	sim_now = end;
}

static int vote(u32 freq, u32 bw)
{
	struct a6xx_hfi_gx_bw_perf_vote_cmd msg = { 0 };

	msg.ack_type = 1;
	msg.freq = freq;
	msg.bw = bw;
	return a6xx_hfi_send_msg(&gpu.gmu, HFI_H2F_MSG_GX_BW_PERF_VOTE, &msg, sizeof(msg), NULL, 0);
}

int main(int argc, char **argv)
{
	const char *s = argc > 1 ? argv[1] : "normal";
	u64 t0;
	int i, ret;

	for (i = 0; i < HFI_MAX_QUEUES; i++) {
		qhdr[i].size = 1024;
		gpu.gmu.queues[i].header = &qhdr[i];
		gpu.gmu.queues[i].data = qmem[i];
	}
	gpu.gmu.dev = &dev;
	gpu.gmu.legacy = strstr(s, "legacy") != NULL;
	gpu.gmu.queues[HFI_COMMAND_QUEUE].seqnum.counter = 3978;
	gpu.base.fault_coredump_done.done = 1;
	/* As a6xx_gmu_resume() leaves it */
	mask = ~A6XX_GMU_GMU2HOST_INTR_INFO_CM3_FAULT;

	if (!strcmp(s, "msgq-unmasked")) {
		/* The mask does not hold MSGQ, and the bit comes before the
		 * queue write shows: the handler finds the queue empty and
		 * the command taken, as in the captured timeout */
		mask &= ~A6XX_GMU_GMU2HOST_INTR_INFO_MSGQ;
		msgq_lead = 60;
		answer_at = 210;
	} else if (!strcmp(s, "msgq-unmasked-together")) {
		mask &= ~A6XX_GMU_GMU2HOST_INTR_INFO_MSGQ;
	} else if (!strcmp(s, "msgq-lag")) {
		/* The queue write shows 60 us before the bit */
		msgq_lead = -60;
	} else if (!strcmp(s, "other-bit")) {
		mask &= ~BIT(1);
		extra_bits = BIT(1);
	} else if (!strcmp(s, "spurious")) {
		forced_irq = 196;
	} else if (!strcmp(s, "latched-status0")) {
		/* MSGQ unmasked; the wait sees and clears the bit at 200 us,
		 * and the interrupt it raised is still delivered at 246 us */
		mask &= ~A6XX_GMU_GMU2HOST_INTR_INFO_MSGQ;
		irq_latency = 96;
		forced_irq = 246;
	} else if (!strcmp(s, "spurious-lead")) {
		forced_irq = 196;
		msgq_lead = 60;
		answer_at = 210;
	} else if (!strcmp(s, "early-irq")) {
		/* The handler runs before the answer, the queue still empty */
		forced_irq = 196;
		answer_at = 300;
	} else if (!strcmp(s, "never-set")) {
		msgq_never = true;
	} else if (!strcmp(s, "mask-stuck")) {
		mask &= ~A6XX_GMU_GMU2HOST_INTR_INFO_MSGQ;
		mask_sticks = false;
	} else if (!strcmp(s, "oob-spurious") || !strcmp(s, "oob-spurious-legacy")) {
		forced_irq = 60;
	} else if (!strcmp(s, "oob-unmasked")) {
		mask &= ~BIT(31);
	} else if (!strcmp(s, "fault")) {
		extra_bits = A6XX_GMU_GMU2HOST_INTR_INFO_CM3_FAULT;
	} else if (strcmp(s, "normal")) {
		printf("unknown scenario %s\n", s);
		return 2;
	}

	if (!strncmp(s, "oob-", 4)) {
		ret = a6xx_gmu_set_oob(&gpu.gmu, GMU_OOB_GPU_SET);
		printf("first: oob ret %d after %llu us\n", ret, (unsigned long long)sim_now);
	} else {
		ret = vote(9, 5);
		printf("first: vote 3979 ret %d after %llu us\n", ret, (unsigned long long)sim_now);
		printf("answer words 0x%x 0x%x 0x%x\n", qmem[HFI_RESPONSE_QUEUE][0],
		       qmem[HFI_RESPONSE_QUEUE][1], qmem[HFI_RESPONSE_QUEUE][2]);
	}
	/* The next vote, with the GMU behaving */
	mask_sticks = true;
	extra_bits = 0;
	msgq_never = false;
	msgq_lead = 0;
	answer_at = 100;
	forced_irq = -1;
	quiet = true;
	sim_advance(1000);
	t0 = sim_now;
	ret = vote(1, 1);
	printf("next: vote ret %d after %llu us\n", ret, (unsigned long long)(sim_now - t0));
	printf("end: handler runs %d, line 0x%x, status 0x%x, mask 0x%x, faults %d\n",
	       handler_runs, info & ~mask, info, mask, faults);
	return 0;
}
"""


# Which variant gets the first vote's answer (or the GPU_SET acknowledge)
# through, per scenario: the code without the patches, with the handler's
# alone, with the wait's alone, with both.
VARIANTS = ("old", "irq-only", "wait-only", "new")
MATRIX = {
    # A GMU that behaves.
    "normal": "TTTT",
    # MSGQ up 60 us after the queue write shows: the new wait takes the
    # answer first and the late bit ends the next wait early, harmlessly.
    "msgq-lag": "TTTT",
    # MSGQ unmasked and up 60 us before the queue write shows: the handler
    # finds the queue empty and the command taken, as captured.
    "msgq-unmasked": "FFTT",
    # MSGQ unmasked, up with the queue write.
    "msgq-unmasked-together": "FTTT",
    # Another bit unmasked, raised with the answer.
    "other-bit": "FTTT",
    # The handler runs with nothing unmasked pending, MSGQ up.
    "spurious": "FTTT",
    # MSGQ unmasked; the wait takes the bit first, and the interrupt it
    # raised reaches the handler with no status bit left (14 of 15 traced
    # hfi interrupts read INTR_INFO 0x0).
    "latched-status0": "TTTT",
    # The same with MSGQ up 60 us before the queue write shows.
    "spurious-lead": "FFTT",
    # The handler runs before the answer comes.
    "early-irq": "TTTT",
    # The answer comes without MSGQ.
    "never-set": "FFTT",
    # MSGQ unmasked and the mask does not take what is written to it.
    "mask-stuck": "FFTT",
    # A firmware fault raised with the answer.
    "fault": "FTTT",
    # GPU_SET acknowledged at 50 us, the handler running at 60 us.
    "oob-spurious": "FTFT",
    # The same on a legacy GMU (acknowledge bit 24).
    "oob-spurious-legacy": "FTFT",
    # The acknowledge bit unmasked: it raises the interrupt itself.
    "oob-unmasked": "FTFT",
}


@unittest.skipUnless(kernel_source() and compiler(), "no pinned kernel checkout or no C compiler")
class HostModelTest(unittest.TestCase):
    """The driver's a6xx_hfi_irq(), a6xx_gmu_set_oob() and HFI send, wait
    and read functions, from the files the build makes with neither patch,
    either one and both, compiled for the build host against a simulated
    GMU, clock and level-triggered interrupt line.  The GMU answers a vote
    (seq 3979, perf 9, bw 5, as in the captured timeout) or acknowledges
    GPU_SET, the hfi interrupt runs in each way the capture leaves open, and
    a second vote follows with a GMU that behaves.  The model exits with
    STORM after 1000 handler runs."""

    REGS = {"GMU_GMU2HOST_INTR_CLR": 0x1f991, "GMU_GMU2HOST_INTR_INFO": 0x1f992,
            "GMU_GMU2HOST_INTR_MASK": 0x1f993, "GMU_HOST2GMU_INTR_SET": 0x1f994}
    SEND = ("static int a6xx_hfi_send_msg(struct a6xx_gmu *gmu, int id,\n"
            "\t\tvoid *data, u32 size, u32 *payload, u32 payload_size)")

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory()
        tmp = Path(cls.dir.name)
        xml = pinned("drivers/gpu/drm/msm/registers/adreno/a6xx_gmu.xml")
        regs = []
        for name, offset in cls.REGS.items():
            # The register file's own offsets and bits.
            if not re.search(rf'<reg32 offset="0x{offset:x}" name="{name}"', xml):
                raise AssertionError(name)
            regs.append(f"#define REG_A6XX_{name} 0x{offset:x}")
        info = re.search(r'name="GMU_GMU2HOST_INTR_INFO">(.*?)</reg32>', xml, re.S).group(1)
        for bit, pos in (("MSGQ", 0), ("CM3_FAULT", 23)):
            if f'<bitfield name="{bit}" pos="{pos}" type="boolean"/>' not in info:
                raise AssertionError(bit)
            regs.append(f"#define A6XX_GMU_GMU2HOST_INTR_INFO_{bit} 0x{1 << pos:08x}")
        (tmp / "a6xx_hfi.h").write_text(pinned(f"{ADRENO}/a6xx_hfi.h"))
        enum = re.search(r"^enum a6xx_gmu_oob_state \{.*?^\}", pinned(f"{ADRENO}/a6xx_gmu.h"),
                         re.M | re.S).group(0)
        cls.bins = {}
        for name, patches in zip(VARIANTS, ((), (IRQ,), (WAIT,), (IRQ, WAIT))):
            root = build_tree(tmp / f"src-{name}", "0")
            for patch in patches:
                result = strict(root, patch)
                if result.returncode:
                    raise AssertionError(result.stdout + result.stderr)
            gmu, hfi = (root / GMU).read_text(), (root / HFI).read_text()
            declaration = "static u32 a6xx_gmu_oob_ack_bits(struct a6xx_gmu *gmu);\n"
            parts = [re.search(r"^#define A6XX_HFI_IRQ_MASK \\\n\t\(A6XX_GMU_GMU2HOST_INTR_INFO_CM3_FAULT\)\n",
                               gmu, re.M).group(0)]
            if IRQ in patches:
                parts.append(declaration)
            parts.append(function(gmu, "static irqreturn_t a6xx_hfi_irq(int irq, void *data)"))
            parts.append(re.search(r"^struct a6xx_gmu_oob_bits \{.*?^\};\n", gmu, re.M | re.S).group(0))
            parts.append(re.search(r"^static const struct a6xx_gmu_oob_bits a6xx_gmu_oob_bits\[\] = \{.*?^\};\n",
                                   gmu, re.M | re.S).group(0))
            if IRQ in patches:
                parts.append(function(gmu, declaration[:-2]))
            parts.append(function(gmu, "int a6xx_gmu_set_oob(struct a6xx_gmu *gmu, enum a6xx_gmu_oob_state state)"))
            send = function(hfi, cls.SEND)
            code = hfi[hfi.index("#define HFI_MSG_ID(val)"):hfi.index(send)] + send
            source = (HARNESS.replace("@ENUM@", enum).replace("@REGS@", "\n".join(regs))
                      .replace("@GMU_CODE@", "\n".join(parts)).replace("@HFI_CODE@", code))
            (tmp / f"{name}.c").write_text(source)
            binary = tmp / name
            result = subprocess.run([compiler(), "-std=gnu11", "-O1", "-Wall", "-Wno-unused-function",
                                     "-Wno-address-of-packed-member", "-I", str(tmp), "-o", str(binary),
                                     str(tmp / f"{name}.c")], capture_output=True, text=True)
            if result.returncode:
                raise AssertionError(result.stderr)
            cls.bins[name] = binary

    @classmethod
    def tearDownClass(cls):
        cls.dir.cleanup()

    def run_model(self, variant, scenario):
        result = subprocess.run([str(self.bins[variant]), scenario], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("STORM", result.stdout)
        self.assertNotIn("BUG", result.stdout)
        return result.stdout

    def first(self, out):
        """The first request's return value and duration."""
        found = re.search(r"^first: (?:vote 3979|oob) ret (-?\d+) after (\d+) us$", out, re.M)
        self.assertIsNotNone(found, out)
        return int(found.group(1)), int(found.group(2))

    def test_each_variant_in_each_scenario(self):
        """The matrix above, and in every run: no storm, no BUG_ON, and the
        next vote goes through.  Where the first request fails it is the
        captured failure: 1 s (10 ms for GPU_SET) and the timeout line, and
        for a vote the next one reports the answer as unexpected."""
        for scenario, expected in MATRIX.items():
            for variant, ok in zip(VARIANTS, expected):
                with self.subTest(scenario=scenario, variant=variant):
                    out = self.run_model(variant, scenario)
                    ret, us = self.first(out)
                    self.assertRegex(out, r"(?m)^next: vote ret 0 after \d+ us$")
                    if ok == "T":
                        self.assertEqual(ret, 0, out)
                        self.assertLessEqual(us, 310, out)
                        self.assertNotIn("timed out", out)
                        self.assertNotIn("Timeout waiting", out)
                        self.assertNotIn("Unexpected message id", out)
                    elif scenario.startswith("oob-"):
                        self.assertEqual(ret, -110, out)
                        self.assertTrue(10000 <= us <= 10100, out)
                        self.assertIn("ERROR Timeout waiting for GMU OOB set GPU_SET: 0x0\n", out)
                    else:
                        self.assertEqual(ret, -110, out)
                        self.assertIn("ERROR Message HFI_H2F_MSG_GX_BW_PERF_VOTE id 3979 timed out waiting "
                                      "for response\n", out)
                        self.assertIn("ERROR Unexpected message id 3979 on the response queue\n", out)
                        # MSGQ seen before the queue write showed: the wait
                        # found the queue empty, cleared the bit, and waited
                        # for it again, for 1 s more.
                        empty = "ERROR The HFI response queue is unexpectedly empty\n" in out
                        self.assertEqual(empty, variant == "irq-only" and scenario.endswith("-lead")
                                         or (variant, scenario) == ("irq-only", "msgq-unmasked"), out)
                        self.assertTrue(1000000 + 200 * empty <= us <= 1000100 + 200 * empty, out)

    def test_the_new_handler_leaves_the_line_down(self):
        """With the handler's patch the line is down at the end of every
        scenario and the handler ran at most once for the first request."""
        for scenario in MATRIX:
            for variant in ("irq-only", "new"):
                with self.subTest(scenario=scenario, variant=variant):
                    out = self.run_model(variant, scenario)
                    end = re.search(r"^end: handler runs (\d+), line 0x0, ", out, re.M)
                    self.assertIsNotNone(end, out)
                    self.assertLessEqual(int(end.group(1)), 1, out)

    def test_the_captured_episode(self):
        """The handler's entry as captured: 196 us into the wait, the queue
        empty, MSGQ up; the old code times out with the answer in the queue,
        the new one masks MSGQ again, logs it once and takes the answer."""
        out = self.run_model("old", "msgq-unmasked")
        self.assertIn("IRQ at 196: status 0x1 mask 0xff7ffffe queue 0/0\n", out)
        out = self.run_model("new", "msgq-unmasked")
        self.assertIn("IRQ at 196: status 0x1 mask 0xff7ffffe queue 0/0\n", out)
        self.assertIn("WARN GMU interrupt mask 0xff7ffffe left 0x1 unmasked (status 0x1), written again: "
                      "0xff7fffff\n", out)
        self.assertEqual(self.first(out), (0, 300))
        self.assertIn(", status 0x0, mask 0xff7fffff, faults 0\n", out)

    def test_the_handler_masks_again_and_clears_only_if_the_mask_does_not_take(self):
        out = self.run_model("new", "other-bit")
        self.assertIn("WARN GMU interrupt mask 0xff7ffffd left 0x2 unmasked (status 0x3), written again: "
                      "0xff7fffff\n", out)
        self.assertEqual(self.first(out), (0, 200))
        out = self.run_model("new", "mask-stuck")
        self.assertIn("WARN GMU interrupt mask 0xff7ffffe left 0x1 unmasked (status 0x1), written again: "
                      "0xff7ffffe\n", out)
        self.assertIn("end: handler runs 1, line 0x0,", out)
        out = self.run_model("new", "oob-unmasked")
        self.assertIn("WARN GMU interrupt mask 0x7f7fffff left 0x80000000 unmasked (status 0x80000000), written "
                      "again: 0xff7fffff\n", out)
        # No status bit left at the handler's entry: the mask is written
        # again all the same, and stays right for the next vote.
        out = self.run_model("new", "latched-status0")
        self.assertEqual(self.first(out), (0, 200))
        self.assertIn("WARN GMU interrupt mask 0xff7ffffe left 0x1 unmasked (status 0x0), written again: "
                      "0xff7fffff\n", out)
        self.assertIn("end: handler runs 1, line 0x0, status 0x0, mask 0xff7fffff,", out)
        self.assertIn("end: handler runs 1, line 0x0, status 0x0, mask 0xff7ffffe,",
                      self.run_model("old", "latched-status0"))
        # Nothing unmasked: no warning, MSGQ left to the wait.
        out = self.run_model("new", "spurious")
        self.assertIn("IRQ at 196: status 0x1 mask 0xff7fffff queue 0/4\n", out)
        self.assertEqual(self.first(out), (0, 200))
        self.assertNotIn("WARN", out)
        out = self.run_model("new", "oob-spurious-legacy")
        self.assertIn("IRQ at 60: status 0x1000000 mask 0xff7fffff", out)
        self.assertEqual(self.first(out), (0, 100))

    def test_a_late_bit_ends_the_next_wait_early_and_harmlessly(self):
        """The new wait takes the answer at the poll after the queue write,
        before MSGQ; the bit stays up and the next vote's first poll ends on
        it, reads an empty queue, waits again and takes its own answer, with
        no error line, at the same time as with a GMU that behaves."""
        out = self.run_model("new", "msgq-lag")
        self.assertEqual(self.first(out), (0, 200))
        self.assertNotIn("ERROR", out)
        self.assertIn("next: vote ret 0 after 100 us\n", out)
        self.assertEqual(self.first(self.run_model("old", "msgq-lag")), (0, 300))

    def test_the_model_s_vote_is_the_captured_one(self):
        """The driver's own send code builds the vote's header, and the
        simulated GMU's answer to it has the words the capture's late answer
        had (record 20261126-105449-vote-A-1016.934: 6357790, 4172284958,
        0): an ACK of three words for id 30, GX_BW_PERF_VOTE, seq 3979,
        four words long, error 0."""
        for variant in VARIANTS:
            with self.subTest(variant):
                self.assertIn(f"answer words 0x{6357790:x} 0x{4172284958:x} 0x0\n",
                              self.run_model(variant, "normal"))

    def test_a_gmu_that_behaves_and_a_firmware_fault_are_served_as_before(self):
        for variant in VARIANTS:
            with self.subTest(variant):
                out = self.run_model(variant, "normal")
                self.assertEqual(self.first(out), (0, 200))
                self.assertNotIn("ERROR", out)
                self.assertNotIn("IRQ at", out)
                self.assertIn("end: handler runs 0, line 0x0, status 0x0,", out)
                out = self.run_model(variant, "fault")
                self.assertEqual(out.count("ERROR GMU firmware fault\n"), 1)
                self.assertIn(", faults 1\n", out)
                self.assertIn("end: handler runs 1, line 0x0,", out)


if __name__ == "__main__":
    unittest.main()
