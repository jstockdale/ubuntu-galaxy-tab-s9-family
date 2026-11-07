// SPDX-License-Identifier: GPL-2.0-only
/*
 * Opt-in switch for the Samsung Galaxy Tab S9 (SM-X710, gts9wifi) cameras.
 *
 * sm8550-samsung-gts9wifi.dts describes both HI1337 modules, the rear
 * module's DW9808 lens and VIO switch, CAMSS and the two CCI controllers, all
 * with status = "disabled": a default boot probes none of it, so the camera
 * stack cannot disturb the boot-time panel cycle or the SoC hand-off.  Writing
 * "rear", "front" or "both" to
 *
 *	/sys/module/gts9wifi_camera/parameters/enable
 *
 * (or booting with gts9wifi_camera.enable=...) applies one OF changeset that
 * sets the chosen nodes to "okay".  The OF reconfiguration notifiers then
 * create the platform and I2C devices and the built-in drivers bind them.
 *
 * CAMSS registers nothing until every sensor its graph names has bound, so a
 * camera that was not chosen must not stay in that graph: the changeset also
 * detaches its CAMSS endpoint.  Nothing here is ever undone (CAMSS and its
 * async notifier are not built to be torn down by reverting a changeset), so
 * the switch is one-shot until the next reboot.
 */
#include <linux/bits.h>
#include <linux/init.h>
#include <linux/kernel.h>
#include <linux/moduleparam.h>
#include <linux/mutex.h>
#include <linux/of.h>
#include <linux/printk.h>
#include <linux/string.h>
#include <linux/sysfs.h>
#include <linux/workqueue.h>

#define GTS9WIFI_CAMERA_REAR	BIT(0)
#define GTS9WIFI_CAMERA_FRONT	BIT(1)
#define GTS9WIFI_CAMERA_BOTH	(GTS9WIFI_CAMERA_REAR | GTS9WIFI_CAMERA_FRONT)

#define GTS9WIFI_CAMERA_CAMSS	"/soc@0/isp@acb7000"

struct gts9wifi_camera_node {
	const char *path;
	const char *compatible;
	unsigned int cameras;	/* enabled when any of these is chosen */
};

/*
 * In the order the devices should appear: supply, buses, I2C clients, CAMSS.
 * Every path and compatible is checked before anything is changed, so a board
 * tree that moved a node makes the switch refuse rather than half-apply.
 */
static const struct gts9wifi_camera_node gts9wifi_camera_nodes[] = {
	{ "/rear-camera-vio-regulator", "regulator-fixed",
	  GTS9WIFI_CAMERA_REAR },
	{ "/soc@0/cci@ac15000", "qcom,sm8550-cci", GTS9WIFI_CAMERA_REAR },
	/* CCI1 carries the rear lens (master 0) and the front sensor (master 1). */
	{ "/soc@0/cci@ac16000", "qcom,sm8550-cci", GTS9WIFI_CAMERA_BOTH },
	{ "/soc@0/cci@ac15000/i2c-bus@1/camera@21", "hynix,hi1337-gts9u-rear",
	  GTS9WIFI_CAMERA_REAR },
	{ "/soc@0/cci@ac16000/i2c-bus@0/lens@c", "dongwoon,dw9808-vcm",
	  GTS9WIFI_CAMERA_REAR },
	{ "/soc@0/cci@ac16000/i2c-bus@1/camera@20", "hynix,hi1337-gts9u-front-uw",
	  GTS9WIFI_CAMERA_FRONT },
	{ GTS9WIFI_CAMERA_CAMSS, "qcom,sm8550-camss", GTS9WIFI_CAMERA_BOTH },
};

/* CAMSS endpoints, detached when their camera is not chosen. */
static const struct gts9wifi_camera_node gts9wifi_camera_endpoints[] = {
	{ GTS9WIFI_CAMERA_CAMSS "/ports/port@1/endpoint", NULL,
	  GTS9WIFI_CAMERA_REAR },
	{ GTS9WIFI_CAMERA_CAMSS "/ports/port@4/endpoint", NULL,
	  GTS9WIFI_CAMERA_FRONT },
};

/*
 * requested: what was asked for and not yet started.
 * claimed:   what the one changeset is for, from the moment it starts until
 *            the next boot (unless building or applying it failed).
 * ready:     the drivers are registered and the board is an SM-X710.
 * The lock is never held across the changeset: applying it probes drivers.
 */
static DEFINE_MUTEX(gts9wifi_camera_lock);
static unsigned int gts9wifi_camera_requested;
static unsigned int gts9wifi_camera_claimed;
static bool gts9wifi_camera_done;
static bool gts9wifi_camera_ready;
/* Never destroyed once applied: the live tree owns its properties. */
static struct of_changeset gts9wifi_camera_changeset;

static const char *gts9wifi_camera_name(unsigned int cameras)
{
	switch (cameras) {
	case GTS9WIFI_CAMERA_REAR:
		return "rear";
	case GTS9WIFI_CAMERA_FRONT:
		return "front";
	case GTS9WIFI_CAMERA_BOTH:
		return "both";
	default:
		return "none";
	}
}

static int gts9wifi_camera_find(const struct gts9wifi_camera_node *node,
				struct device_node **np)
{
	*np = of_find_node_by_path(node->path);
	if (!*np) {
		pr_err("gts9wifi-camera: %s is not in the device tree\n",
		       node->path);
		return -ENODEV;
	}
	if (node->compatible && !of_device_is_compatible(*np, node->compatible)) {
		pr_err("gts9wifi-camera: %pOF is not %s\n", *np, node->compatible);
		of_node_put(*np);
		*np = NULL;
		return -ENODEV;
	}
	return 0;
}

static int gts9wifi_camera_build(struct of_changeset *ocs, unsigned int cameras)
{
	struct device_node *np;
	unsigned int i;
	int ret;

	for (i = 0; i < ARRAY_SIZE(gts9wifi_camera_endpoints); i++) {
		const struct gts9wifi_camera_node *node =
			&gts9wifi_camera_endpoints[i];

		if (node->cameras & cameras)
			continue;
		ret = gts9wifi_camera_find(node, &np);
		if (ret)
			return ret;
		ret = of_changeset_detach_node(ocs, np);
		of_node_put(np);
		if (ret)
			return ret;
	}

	for (i = 0; i < ARRAY_SIZE(gts9wifi_camera_nodes); i++) {
		const struct gts9wifi_camera_node *node = &gts9wifi_camera_nodes[i];

		if (!(node->cameras & cameras))
			continue;
		ret = gts9wifi_camera_find(node, &np);
		if (ret)
			return ret;
		if (of_device_is_available(np)) {
			/* Already enabled by someone else; nothing to flip. */
			of_node_put(np);
			continue;
		}
		ret = of_changeset_update_prop_string(ocs, np, "status", "okay");
		of_node_put(np);
		if (ret)
			return ret;
	}
	return 0;
}

/* Returns true when the live tree now carries the change. */
static bool gts9wifi_camera_apply(unsigned int cameras)
{
	struct device_node *camss;
	bool applied;
	int ret;

	of_changeset_init(&gts9wifi_camera_changeset);
	ret = gts9wifi_camera_build(&gts9wifi_camera_changeset, cameras);
	if (ret) {
		of_changeset_destroy(&gts9wifi_camera_changeset);
		pr_err("gts9wifi-camera: not enabling the %s camera(s): %d\n",
		       gts9wifi_camera_name(cameras), ret);
		return false;
	}

	pr_info("gts9wifi-camera: enabling the %s camera(s)\n",
		gts9wifi_camera_name(cameras));
	ret = of_changeset_apply(&gts9wifi_camera_changeset);

	/*
	 * of_changeset_apply() reverts the tree only when an entry could not be
	 * applied; an error from a notifier (a device that could not be created)
	 * leaves the tree changed.  Tell the two apart by looking at the tree.
	 */
	camss = of_find_node_by_path(GTS9WIFI_CAMERA_CAMSS);
	applied = camss && of_device_is_available(camss);
	of_node_put(camss);
	if (!applied) {
		of_changeset_destroy(&gts9wifi_camera_changeset);
		pr_err("gts9wifi-camera: the device tree change failed: %d\n", ret);
		return false;
	}

	if (ret)
		pr_warn("gts9wifi-camera: %s camera(s) enabled, but a device could not be created: %d\n",
			gts9wifi_camera_name(cameras), ret);
	else
		pr_info("gts9wifi-camera: %s camera(s) enabled; the drivers bind asynchronously\n",
			gts9wifi_camera_name(cameras));
	return true;
}

static void gts9wifi_camera_work_fn(struct work_struct *work)
{
	unsigned int cameras;
	bool applied;

	mutex_lock(&gts9wifi_camera_lock);
	cameras = gts9wifi_camera_requested;
	if (gts9wifi_camera_claimed || !cameras) {
		mutex_unlock(&gts9wifi_camera_lock);
		return;
	}
	gts9wifi_camera_claimed = cameras;
	gts9wifi_camera_requested = 0;
	mutex_unlock(&gts9wifi_camera_lock);

	applied = gts9wifi_camera_apply(cameras);

	mutex_lock(&gts9wifi_camera_lock);
	if (applied)
		gts9wifi_camera_done = true;
	else
		gts9wifi_camera_claimed = 0;	/* nothing changed; may retry */
	mutex_unlock(&gts9wifi_camera_lock);
}
static DECLARE_WORK(gts9wifi_camera_work, gts9wifi_camera_work_fn);

static int gts9wifi_camera_parse(const char *val, unsigned int *cameras)
{
	char buf[8];
	char *word;

	if (strscpy(buf, val, sizeof(buf)) < 0)
		return -EINVAL;
	word = strim(buf);
	if (!strcmp(word, "rear"))
		*cameras = GTS9WIFI_CAMERA_REAR;
	else if (!strcmp(word, "front"))
		*cameras = GTS9WIFI_CAMERA_FRONT;
	else if (!strcmp(word, "both") || !strcmp(word, "1") ||
		 !strcmp(word, "y"))
		*cameras = GTS9WIFI_CAMERA_BOTH;
	else if (!strcmp(word, "none") || !strcmp(word, "0") ||
		 !strcmp(word, "n"))
		*cameras = 0;
	else
		return -EINVAL;
	return 0;
}

static int gts9wifi_camera_set(const char *val, const struct kernel_param *kp)
{
	unsigned int cameras;
	int ret;

	ret = gts9wifi_camera_parse(val, &cameras);
	if (ret)
		return ret;

	mutex_lock(&gts9wifi_camera_lock);
	if (gts9wifi_camera_claimed) {
		/* One-shot: only repeating the same choice is not an error. */
		ret = cameras == gts9wifi_camera_claimed ? 0 : -EBUSY;
	} else if (gts9wifi_camera_ready) {
		gts9wifi_camera_requested = cameras;
		if (cameras)
			queue_work(system_long_wq, &gts9wifi_camera_work);
	} else if (system_state == SYSTEM_BOOTING) {
		/*
		 * A kernel command line value arrives before workqueues exist:
		 * record it, and the late initcall applies it.
		 */
		gts9wifi_camera_requested = cameras;
	} else {
		ret = -ENODEV;	/* booted, and not an SM-X710 */
	}
	mutex_unlock(&gts9wifi_camera_lock);
	return ret;
}

static int gts9wifi_camera_get(char *buffer, const struct kernel_param *kp)
{
	unsigned int claimed, requested;
	bool done;

	mutex_lock(&gts9wifi_camera_lock);
	claimed = gts9wifi_camera_claimed;
	requested = gts9wifi_camera_requested;
	done = gts9wifi_camera_done;
	mutex_unlock(&gts9wifi_camera_lock);

	if (done)
		return sysfs_emit(buffer, "%s\n", gts9wifi_camera_name(claimed));
	if (claimed || requested)
		return sysfs_emit(buffer, "%s (pending)\n",
				  gts9wifi_camera_name(claimed ?: requested));
	return sysfs_emit(buffer, "none\n");
}

static const struct kernel_param_ops gts9wifi_camera_ops = {
	.set = gts9wifi_camera_set,
	.get = gts9wifi_camera_get,
};
module_param_cb(enable, &gts9wifi_camera_ops, NULL, 0644);
MODULE_PARM_DESC(enable,
		 "Enable the SM-X710 cameras once: rear, front or both (a reboot undoes it)");

static int __init gts9wifi_camera_init(void)
{
	if (!of_machine_is_compatible("samsung,gts9wifi")) {
		mutex_lock(&gts9wifi_camera_lock);
		if (gts9wifi_camera_requested)
			pr_warn("gts9wifi-camera: not an SM-X710; ignoring enable=%s\n",
				gts9wifi_camera_name(gts9wifi_camera_requested));
		gts9wifi_camera_requested = 0;
		mutex_unlock(&gts9wifi_camera_lock);
		return 0;
	}

	mutex_lock(&gts9wifi_camera_lock);
	gts9wifi_camera_ready = true;
	mutex_unlock(&gts9wifi_camera_lock);

	/* Every camera driver is built in and registered by now. */
	gts9wifi_camera_work_fn(NULL);
	return 0;
}
late_initcall(gts9wifi_camera_init);
