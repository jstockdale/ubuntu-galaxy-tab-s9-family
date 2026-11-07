// SPDX-License-Identifier: GPL-2.0-only
/*
 * Samsung's bootloader needs the byte-identical, validated base DTB. Add the
 * SM8550-AC OPPs after that handoff, before either OPP consumer can probe.
 * The CPU driver still checks its hardware LUT and supplies the real voltage;
 * the GPU entry comes from the X910's stock power-level table, not an OC.
 */
#include <linux/init.h>
#include <linux/of.h>
#include <linux/printk.h>
#include <linux/types.h>

#include "gts9u-performance-overlay.h"

static int __init gts9u_apply(const unsigned char *fdt, u32 size,
			      const char *what)
{
	int overlay_id, ret;

	ret = of_overlay_fdt_apply(fdt, size, &overlay_id, NULL);
	if (ret)
		pr_err("gts9u-performance: cannot apply the %s overlay: %d\n",
		       what, ret);
	return ret;
}

static int __init gts9u_performance_init(void)
{
	bool ultra = of_machine_is_compatible("samsung,gts9uwifi");
	int ret;

	if (!ultra && !of_machine_is_compatible("samsung,gts9wifi"))
		return 0;

	ret = gts9u_apply(gts9_family_overlay, sizeof(gts9_family_overlay),
			  "family");
	if (ret)
		return ret;

	/* The OPP values were read from the X910's tables; no other board's yet. */
	if (!ultra)
		return 0;
	ret = gts9u_apply(gts9u_performance_overlay,
			  sizeof(gts9u_performance_overlay), "Galaxy OPP");
	if (ret)
		return ret;

	pr_info("gts9u-performance: registered Galaxy 3.36 GHz CPU and 719 MHz GPU OPPs\n");
	return 0;
}
/* qcom-cpufreq-hw registers at postcore; OF platform devices appear later. */
core_initcall(gts9u_performance_init);
