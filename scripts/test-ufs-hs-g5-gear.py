#!/usr/bin/env python3
"""Apply the HS-G5 patch and execute the real OPP-to-gear mapping with stubs.

Usage: python3 scripts/test-ufs-hs-g5-gear.py PRISTINE_UFS_QCOM_C
This never touches a UFS host. It checks the mapping only; link stability,
resume and power at HS-G5 need the hardware tests in docs/porting-log.md.
"""
from pathlib import Path
import re
import subprocess
import sys
import tempfile

repo = Path(__file__).resolve().parents[1]
original = Path(sys.argv[1]).read_text()
patch = repo / "kernel/patches/ufs-qcom-hs-g5-at-300mhz-sm8550.patch"

script = (repo / "scripts/build-mainline-kernel.sh").read_text()
# Both boards share the SoC and the UFS host; the patch applies to every
# build, unconditionally.
gate = re.search(r"\napply_unless 'it runs HS-G5' \\\n"
                 r"\tdrivers/ufs/host/ufs-qcom.c ufs-qcom-hs-g5-at-300mhz-sm8550.patch\n", script)
assert gate, "build script must apply the patch for every device"

with tempfile.TemporaryDirectory(prefix="gts9u-ufs-g5-test-") as tmp:
    source = Path(tmp) / "drivers/ufs/host/ufs-qcom.c"
    source.parent.mkdir(parents=True)
    source.write_text(original)
    subprocess.run(["patch", "--batch", "--fuzz=0", "-d", tmp, "-p1"],
                   input=patch.read_text(), check=True, text=True)
    patched = source.read_text()
    assert "it runs HS-G5" in patched, "apply_unless marker missing"

    body = re.search(r"^static u32 ufs_qcom_freq_to_gear_speed\(.*?^}\n",
                     patched, re.S | re.M).group(0)
    harness = r"""
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
typedef unsigned int u32;
enum { UFS_HS_DONT_CHANGE, UFS_HS_G1, UFS_HS_G2, UFS_HS_G3, UFS_HS_G4, UFS_HS_G5 };
struct ufs_pa_layer_attr { u32 gear_rx; };
struct ufs_hba {
	bool use_pm_opp;
	void *dev;
	void *priv;
	struct { struct ufs_pa_layer_attr info; } max_pwr_info;
};
struct ufs_qcom_host { struct { u32 major; } hw_ver; };
static void *ufshcd_get_variant(struct ufs_hba *hba) { return hba->priv; }
/* The OPP table maps a core clock rate to the same UniPro rate here. */
static unsigned long ufs_qcom_opp_freq_to_clk_freq(struct ufs_hba *hba,
		unsigned long freq, const char *name)
{ (void)hba; (void)name; return freq; }
#define dev_err(dev, ...) ((void)(dev))
#define min_t(type, a, b) ((type)(a) < (type)(b) ? (type)(a) : (type)(b))
""" + body + r"""
static int failures;
static void check(u32 major, unsigned long freq, u32 max_gear, u32 want)
{
	struct ufs_qcom_host host = { .hw_ver = { major } };
	struct ufs_hba hba = { .use_pm_opp = true, .priv = &host,
			       .max_pwr_info = { .info = { max_gear } } };
	u32 got = ufs_qcom_freq_to_gear_speed(&hba, freq);

	if (got != want) {
		printf("hw %u, %lu Hz, max G%u: got G%u, want G%u\n",
		       major, freq, max_gear, got, want);
		failures++;
	}
}
int main(void)
{
	check(5, 300000000, UFS_HS_G5, UFS_HS_G5);	/* SM8550 + UFS 4.0 */
	check(5, 300000000, UFS_HS_G4, UFS_HS_G4);	/* SM8550 + UFS 3.1 */
	check(6, 300000000, UFS_HS_G5, UFS_HS_G4);	/* SM8650 keeps 403 for G5 */
	check(4, 300000000, UFS_HS_G4, UFS_HS_G4);
	check(6, 403000000, UFS_HS_G5, UFS_HS_G5);
	check(5, 150000000, UFS_HS_G5, UFS_HS_G2);
	check(5, 75000000, UFS_HS_G5, UFS_HS_G1);
	check(5, 12345, UFS_HS_G5, UFS_HS_DONT_CHANGE);
	return failures ? 1 : 0;
}
"""
    test_c = Path(tmp) / "gear.c"
    test_c.write_text(harness)
    exe = Path(tmp) / "gear"
    subprocess.run(["cc", "-std=gnu11", "-Wall", "-Wextra", "-Werror",
                    "-Wno-unused-parameter", str(test_c), "-o", str(exe)],
                   check=True, text=True)
    subprocess.run([str(exe)], check=True, text=True)

print("ufs hs-g5 gear mapping: ok")
