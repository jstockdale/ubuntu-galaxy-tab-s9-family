// SPDX-License-Identifier: GPL-2.0-only
/*
 * Samsung's bootloader needs the byte-identical, validated base DTB. Add the
 * SM8550-AC OPPs, and on the Ultra the UFS ICE clock, after that handoff,
 * before any of their consumers can probe. On both boards, also take out of
 * the live tree the two properties that let PM8550 L15B leave HPM.
 * The CPU driver still checks its hardware LUT and supplies the real voltage;
 * the GPU entry comes from the X910's stock power-level table, not an OC.
 */
#include <linux/array_size.h>
#include <linux/errno.h>
#include <linux/init.h>
#include <linux/of.h>
#include <linux/printk.h>
#include <linux/string.h>
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

/*
 * PM8550 L15B feeds the eUSB2 repeater's 1.8 V and the WCN PMU's I/O rail.
 * Only the repeater declares a load, so with the USB PHY down a sleep voted
 * the LDO to LPM with Bluetooth on, and once the whole SoC slept the PMIC's
 * over-current protection reset the SM-X710. Both board DTSes hold it in HPM
 * as sm8550-qrd and sm8550-mtp do, without regulator-allow-set-load and
 * regulator-allowed-modes. The Ultra's builds compile the pinned baseline
 * DTS, and an overlay cannot delete a property, so the two come out of the
 * live tree here. They are read when the RPMh regulators probe, on the
 * device rpmh-rsc's probe creates, and rpmh-rsc's own device appears only
 * at arch_initcall_sync. A DTB without them, as the SM-X710's, is left as
 * it is.
 */
#define GTS9_L15B_PATH	"/soc@0/rsc@17a00000/regulators-0/ldo15"

static int __init gts9_hold_l15b_hpm(void)
{
	/*
	 * Set-load first: without it the core never picks a mode by load, so
	 * a failure after it still leaves L15B in HPM.
	 */
	static const char * const props[] __initconst = {
		"regulator-allow-set-load",
		"regulator-allowed-modes",
	};
	struct device_node *np;
	unsigned int i, dropped = 0;
	const char *name;
	int ret = 0;

	np = of_find_node_by_path(GTS9_L15B_PATH);
	if (!np) {
		pr_err("gts9u-performance: L15B not held in HPM: no %s\n",
		       GTS9_L15B_PATH);
		return -ENODEV;
	}
	if (!of_device_is_compatible(np->parent, "qcom,pm8550-rpmh-regulators") ||
	    of_property_read_string(np, "regulator-name", &name) ||
	    strcmp(name, "vreg_l15b_1p8")) {
		pr_err("gts9u-performance: L15B not held in HPM: %pOF is not PM8550's vreg_l15b_1p8, left alone\n",
		       np);
		ret = -ENODEV;
		goto out;
	}

	for (i = 0; i < ARRAY_SIZE(props); i++) {
		struct property *pp = of_find_property(np, props[i], NULL);

		if (!pp)
			continue;
		ret = of_remove_property(np, pp);
		if (ret) {
			pr_err("gts9u-performance: L15B: cannot remove %s from %pOF: %d\n",
			       props[i], np, ret);
			goto out;
		}
		dropped++;
	}
	if (dropped)
		pr_info("gts9u-performance: L15B held in HPM: %u of its DTB properties dropped\n",
			dropped);
	else
		pr_info("gts9u-performance: L15B held in HPM by the DTB\n");
out:
	of_node_put(np);
	return ret;
}

static int __init gts9u_performance_init(void)
{
	bool ultra = of_machine_is_compatible("samsung,gts9uwifi");
	int ret, err;

	if (!ultra && !of_machine_is_compatible("samsung,gts9wifi"))
		return 0;

	/* Not an overlay: it neither needs the overlays nor stops them. */
	ret = gts9_hold_l15b_hpm();

	/*
	 * The overlays are independent and each is applied as one transaction,
	 * so a failing one must not cost the board the others.
	 */
	err = gts9u_apply(gts9_family_overlay, sizeof(gts9_family_overlay),
			  "family");
	ret = ret ?: err;

	/* Both boards' prime-core LUTs end at the same 3.36 GHz boost. */
	err = gts9u_apply(gts9_performance_overlay,
			  sizeof(gts9_performance_overlay), "Galaxy CPU OPP");
	if (!err)
		pr_info("gts9u-performance: registered Galaxy 3.36 GHz CPU OPP\n");
	ret = ret ?: err;

	/*
	 * The GPU level was read from the X910's tables; no other board's yet.
	 * The UFS ICE clock is the SM-X710's board DTS change, made here
	 * because the Ultra's DTB is pinned.
	 */
	if (!ultra)
		return ret;
	err = gts9u_apply(gts9u_performance_overlay,
			  sizeof(gts9u_performance_overlay),
			  "Galaxy GPU OPP and UFS ICE clock");
	if (!err)
		pr_info("gts9u-performance: registered Galaxy 719 MHz GPU OPP and UFS ICE clock scaling\n");
	return ret ?: err;
}
/* qcom-cpufreq-hw registers at postcore; OF platform devices appear later. */
core_initcall(gts9u_performance_init);
