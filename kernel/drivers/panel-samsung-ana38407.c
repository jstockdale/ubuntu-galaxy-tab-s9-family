// SPDX-License-Identifier: GPL-2.0-only
/*
 * DRM panel driver for the Samsung AMSA46AS02 (Anapass ANA38407 DDIC) as fitted
 * to the Galaxy Tab S9 Ultra Wi-Fi (SM-X910, "gts9uwifi").
 *
 * 2960x1848 command-mode DSI panel, 4 lanes, DSC 1.1 (2 slices 1480x77, 8bpp).
 * The DCS init/exit sequences and timings were recovered from the Samsung
 * open-source drop (opensource.samsung.com, SM-X910_EUR_16) panel data file and
 * from the stock DTBO; see docs/panel-ana38407-bringup.md.  Samsung's
 * proprietary gamma/VRR/ACL/mdnie machinery is intentionally NOT ported: the
 * DPU switches refresh rate by mode-set, and brightness goes through the
 * standard DCS 0x51 path.  The one exception is the 11" glass, whose DDIC
 * sets its own refresh in command mode: there the mode being enabled selects
 * Samsung's VRR_SETTING and GLUT_SETTING branches (see
 * ana38407_amsa10fa01_on()).
 */

#include <linux/backlight.h>
#include <linux/delay.h>
#include <linux/gpio/consumer.h>
#include <linux/iopoll.h>
#include <linux/ktime.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/of.h>
#include <linux/of_device.h>
#include <linux/regulator/consumer.h>
#include <linux/workqueue.h>

#include <drm/display/drm_dsc.h>
#include <drm/display/drm_dsc_helper.h>
#include <drm/drm_connector.h>
#include <drm/drm_crtc.h>
#include <drm/drm_mipi_dsi.h>
#include <drm/drm_modes.h>
#include <drm/drm_panel.h>

#include "panel-samsung-ana38407-glut.h"

/*
 * Boot-time switches for the display experiments, so a change of DSI timing
 * or clock mode is a vendor_boot repack rather than a kernel rebuild.  The
 * driver is built in, so they are set on the kernel command line as
 * panel_samsung_ana38407.<name>=.  -1 keeps the per-glass default.
 */
static int param_continuous_clock = -1;
module_param_named(continuous_clock, param_continuous_clock, int, 0444);
MODULE_PARM_DESC(continuous_clock,
		 "keep the DSI clock lane in HS between frames (-1 per-glass default, 0 no, 1 yes)");

/*
 * Only the preferred mode: the other modes take Samsung's own porches, which
 * already land on the same lane rate (see ana38407_amsa10fa01_modes).
 */
static int param_hback_porch = -1;
module_param_named(hback_porch, param_hback_porch, int, 0444);
MODULE_PARM_DESC(hback_porch,
		 "override the horizontal back porch of the preferred mode, recomputing its clock (-1 keeps the table)");

static bool param_mode_30hz;
module_param_named(mode_30hz, param_mode_30hz, bool, 0444);
MODULE_PARM_DESC(mode_30hz,
		 "also offer the 30 Hz mode of glasses that have one (experimental)");

/*
 * Cold-boot experiments (see the comment above ANA38407_TCON_RDY_TIMEOUT_US).
 * The first power-on resets a DDIC the bootloader left running; every later
 * one starts from a DDIC whose own supplies were off.  cold_start_ms=N makes
 * the first power-on start the same way: the DDIC is taken down as
 * unprepare() does and held there for N ms first.  trace_power_on logs, for
 * every power-on, what the first one already reports: the supplies as the
 * regulator framework sees them, TCON_RDY and the id the DDIC answered.
 */
static unsigned int param_cold_start_ms;
module_param_named(cold_start_ms, param_cold_start_ms, uint, 0444);
MODULE_PARM_DESC(cold_start_ms,
		 "power the bootloader's DDIC down for this many ms before the first power-on (0: reset it while powered, the default; at most 2000)");

static bool param_trace_power_on;
module_param_named(trace_power_on, param_trace_power_on, bool, 0444);
MODULE_PARM_DESC(trace_power_on,
		 "log supplies, TCON_RDY and the panel id of every power-on (bring-up)");

#define ANA38407_COLD_START_MAX_MS	2000

/*
 * DCS 0x51 carries 11 significant bits on this DDIC, not 12: Samsung's own
 * power-on sequence programs 0x07ff as "full brightness", and anything with bit
 * 11 set wraps back to the bottom of the range.  Declaring 4095 made the
 * desktop's slider sweep the panel from dark to bright twice.
 */
#define ANA38407_MAX_BRIGHTNESS		0x07ff
/* One UI enrollment reports HBM platform level 385. Samsung's HBM table
 * maps that to WRDISBV 1623 (634 cd/m2), not 2047 (900 cd/m2). Normal mode
 * also uses 2047, but with a different luminance table (420 cd/m2).
 */
#define ANA38407_FOD_BRIGHTNESS		1623
#define ANA38407_FOD_WATCHDOG_MS	15000
#define ANA38407_FOD_SETTLE_MS		35

/*
 * Revision D, as read back by the bootloader (lcd_id=0x800004).  The 11"
 * AMSA10FA01 answers the same id: the SM-X710 cmdline carries
 * msm_drm.lcd_id=800004 and every working init on that board read 80 00 04.
 * Samsung's own X710 driver also knows revision C (0x800003) glass, which
 * needs the SSCG_OFF_SETTING block this driver does not send.
 */
static const u8 ana38407_expected_id[3] = { 0x80, 0x00, 0x04 };

/* Refresh-rate families of Samsung's VRR_SETTING ("HS" = normal speed). */
enum ana38407_vrr {
	ANA38407_VRR_120HS,
	ANA38407_VRR_60HS,
	ANA38407_VRR_30HS,
};

/*
 * One driver serves the ANA38407 across the Tab S9 family; the glass differs.
 * A variant carries its modes, physical size, DSC geometry and, once read on
 * hardware, the expected panel id (NULL skips the comparison and the prepare
 * path just logs what the DDIC answered).
 */
struct ana38407;

struct ana38407_desc {
	/* The DDIC's vendor power-on sequence differs per glass. */
	int (*on)(struct ana38407 *ctx);
	/* Lowest 0x51 level the glass renders cleanly; 0 passes GNOME's fade through. */
	u16 min_brightness;
	const struct drm_display_mode *modes;
	unsigned int num_modes;
	u16 width_mm;
	u16 height_mm;
	u16 pic_width;
	u16 pic_height;
	u16 slice_width;
	u16 slice_height;
	const u8 *expected_id;
	/*
	 * Keep the DSI clock lane in HS between command-mode frames.  The
	 * 11" glass shows intermittent speckle on frames written after an idle
	 * gap (never during continuous updates), the signature of a marginal
	 * LP->HS re-entry at 1.524 Gbps with mainline's generic PHY timings.
	 */
	bool continuous_clock;
	/*
	 * The DDIC sets its own refresh in command mode: the mode being enabled
	 * selects Samsung's VRR_SETTING and GLUT_SETTING branches, and below
	 * 120 Hz a brightness change re-sends the matching GLUT_OFFSET_60HS row.
	 */
	bool vrr_by_mode;
};

/*
 * On a cold boot the DDIC answers 00:00:00 and emits black, even though the
 * link is up and DRM reports the connector enabled; a suspend/resume then
 * recovers it and the id reads 80:00:04.  So the id is a reliable signal, and
 * the fault is not in this driver: replaying the init sequence, toggling reset
 * and even dropping the panel supplies the way unprepare/prepare does were all
 * measured to leave it at 00:00:00.  What differs on resume is that the DSI
 * host and PHY are re-initialised from scratch, rather than inherited from the
 * state the bootloader left behind after painting its logo.
 *
 * Log the mismatch so the cause is visible, but do not burn boot time cycling
 * the panel for a fix that does not work at this level.  Where the board
 * describes Samsung's TCON_RDY line, prepare also waits for it after reset,
 * as Samsung's Anapass power sequence does, and the first prepare reports how
 * the DDIC came out of reset: a DDIC that never raises TCON_RDY is dead on
 * the panel side, one that does but still reads 00:00:00 points at the host.
 * On the SM-X710 it does (high before the reset, high again 114-115 ms after
 * it, every boot of 2026-11-07), so the host is the main suspect.  Samsung
 * itself never resets the DDIC over the bootloader's display: it inherits the
 * splash, and its first power-on follows a full power-off.  cold_start_ms
 * reproduces that power-off on the panel side alone; the msm switches
 * mdss_handoff_gdsc_cycle and boot_display_cycle do it on the host side.
 */
#define ANA38407_TCON_RDY_TIMEOUT_US	200000

struct ana38407 {
	struct drm_panel panel;
	struct mipi_dsi_device *dsi;
	struct drm_dsc_config dsc;
	struct regulator_bulk_data *supplies;
	struct gpio_desc *reset_gpio;
	struct gpio_desc *tcon_rdy_gpio;
	/* Set by get_modes(); prepare() reads the mode being enabled from it. */
	struct drm_connector *connector;
	/* Serializes panel lifetime, normal brightness and optical FOD state. */
	struct mutex lock;
	struct delayed_work fod_watchdog;
	u16 user_brightness;
	bool prepared;
	bool enabled;
	bool fod_mode;
	bool fod_circle;
	bool prepared_once;
	bool id_read;
	/* Power-ons since probe, for trace_power_on. */
	unsigned int power_ons;
	enum ana38407_vrr vrr;
	/* GLUT_OFFSET_60HS row the DDIC holds, -1 for none (120 Hz zeroes). */
	int glut_row;
	u8 id[3];
	char cell_id[23];
	const struct ana38407_desc *desc;
};

/*
 * gts9u panel rails (from the stock DTS): vddio 1.8 V (l12b), vdd 1.2 V,
 * vci 3.0 V (l13b) and the AMOLED ELVDD "avdd" ~5.5 V behind a GPIO load switch.
 * All four must be up before the DDIC will light.
 */
static const struct regulator_bulk_data ana38407_supplies[] = {
	{ .supply = "vddio" },
	{ .supply = "vdd" },
	{ .supply = "vci" },
	{ .supply = "avdd" },
};

static inline struct ana38407 *to_ana38407(struct drm_panel *panel)
{
	return container_of(panel, struct ana38407, panel);
}

/*
 * Samsung sequences these rails rather than raising them together: its
 * dsi_panel_pwr_supply brings up vddio first and then waits
 * qcom,supply-post-on-sleep = 0x14 (20 ms) before vdd and vci, with avdd - the
 * AMOLED ELVDD behind a load switch - after them.
 *
 * Enabling all four at once and sleeping afterwards, which is what a plain
 * regulator_bulk_enable() does, left the DDIC unreliable at every enable: a
 * cold boot came up black, a resume often needed two or three attempts, and
 * the first frames sometimes showed artefacts.
 */
static int ana38407_power_on(struct ana38407 *ctx)
{
	int ret;

	ret = regulator_enable(ctx->supplies[0].consumer);	/* vddio */
	if (ret)
		return ret;

	msleep(20);

	ret = regulator_bulk_enable(ARRAY_SIZE(ana38407_supplies) - 1,
				    &ctx->supplies[1]);		/* vdd, vci, avdd */
	if (ret)
		regulator_disable(ctx->supplies[0].consumer);

	return ret;
}

/* Pack a signed DSC range BPG offset into the 6-bit field. */
#define DSC_BPG_OFFSET(x)	((u8)((x) & DSC_RANGE_BPG_OFFSET_MASK))

static void ana38407_reset(struct ana38407 *ctx)
{
	/* Samsung reset-sequence <0 10 1 1>: assert low, release high. */
	gpiod_set_value_cansleep(ctx->reset_gpio, 1);
	usleep_range(5000, 6000);
	gpiod_set_value_cansleep(ctx->reset_gpio, 0);
	usleep_range(10000, 11000);
	gpiod_set_value_cansleep(ctx->reset_gpio, 1);
	usleep_range(10000, 11000);
}

/*
 * Samsung's Anapass power sequence (samsung,anapass-power-seq): VDD on, LP11,
 * reset, then no command until the DDIC raises TCON_RDY.  Samsung polls every
 * 2 ms for up to a second and carries on either way; the DDIC normally takes a
 * few tens of milliseconds.  Returns the wait in milliseconds or -ETIMEDOUT.
 */
static int ana38407_wait_tcon_ready(struct ana38407 *ctx)
{
	ktime_t start = ktime_get();
	int ret, val;

	ret = read_poll_timeout(gpiod_get_value_cansleep, val, val > 0,
				2000, ANA38407_TCON_RDY_TIMEOUT_US, false,
				ctx->tcon_rdy_gpio);
	if (ret)
		return ret;

	return ktime_ms_delta(ktime_get(), start);
}

static const char *ana38407_level(int value)
{
	return value < 0 ? "unknown" : value ? "high" : "low";
}

/*
 * The supplies as the regulator framework sees them, for the power-on traces:
 * 1 enabled (by anyone), 0 off, negative an error.  A rail the bootloader left
 * on but nobody has claimed yet may read 0 although it is up.
 */
static void ana38407_supply_states(struct ana38407 *ctx, char *buf, size_t len)
{
	size_t pos = 0;
	unsigned int i;

	buf[0] = '\0';
	for (i = 0; i < ARRAY_SIZE(ana38407_supplies) && pos < len; i++)
		pos += scnprintf(buf + pos, len - pos, "%s%s %d", i ? ", " : "",
				 ana38407_supplies[i].supply,
				 regulator_is_enabled(ctx->supplies[i].consumer));
}

/*
 * cold_start_ms: before the first power-on, take the DDIC the bootloader left
 * running down the way unprepare() does - reset asserted, every supply this
 * driver holds released - and keep it there, so that it starts like every
 * later power-on.  The supplies are claimed first so that the release is
 * balanced.  Rails another consumer also holds stay up, as they do at every
 * blank (on the SM-X710 vddio with the touchscreen, vci with the digitizer).
 */
static void ana38407_cold_start(struct ana38407 *ctx)
{
	struct device *dev = &ctx->dsi->dev;
	unsigned int ms = min_t(unsigned int, param_cold_start_ms,
				ANA38407_COLD_START_MAX_MS);
	int tcon_before = -1, tcon_after = -1;
	int ret;

	if (ctx->tcon_rdy_gpio)
		tcon_before = gpiod_get_value_cansleep(ctx->tcon_rdy_gpio);

	ret = ana38407_power_on(ctx);
	if (ret) {
		dev_warn(dev, "cold start: could not claim the supplies: %d\n", ret);
		return;
	}
	gpiod_set_value_cansleep(ctx->reset_gpio, 0);
	regulator_bulk_disable(ARRAY_SIZE(ana38407_supplies), ctx->supplies);
	msleep(ms);

	if (ctx->tcon_rdy_gpio)
		tcon_after = gpiod_get_value_cansleep(ctx->tcon_rdy_gpio);
	dev_info(dev, "first power-on: DDIC held off %u ms before it (TCON_RDY %s, then %s)\n",
		 ms, ana38407_level(tcon_before), ana38407_level(tcon_after));
}

/*
 * The 11" glass at 60 and 30 Hz ("60HS"/"30HS"): Samsung's GLUT_SETTING loads
 * a gamma LUT offset that depends on the platform brightness level, and its
 * BRIGHTNESS_SETTING re-sends it with every level change.  The level of a raw
 * DCS 0x51 value is the highest NORMAL_CANDELA_MAP entry that does not exceed
 * it.  HBM is only ever entered for the fingerprint sensor, for at most
 * ANA38407_FOD_WATCHDOG_MS, and keeps the user's row.
 */
static unsigned int ana38407_amsa10fa01_level(u16 brightness)
{
	unsigned int lo = 0, hi = ARRAY_SIZE(ana38407_amsa10fa01_wrdisbv) - 1;

	while (lo < hi) {
		unsigned int mid = (lo + hi + 1) / 2;

		if (ana38407_amsa10fa01_wrdisbv[mid] <= brightness)
			lo = mid;
		else
			hi = mid - 1;
	}

	return lo;
}

/* GLUT_OFFSET_60HS row for the current mode and brightness, -1 at 120 Hz. */
static int ana38407_glut_row(struct ana38407 *ctx)
{
	if (!ctx->desc->vrr_by_mode || ctx->vrr == ANA38407_VRR_120HS)
		return -1;

	return ana38407_amsa10fa01_glut60_row[ana38407_amsa10fa01_level(ctx->user_brightness)];
}

/*
 * GLUT_SETTING: the offsets go to 0xEB from gpara 0x91 (110 bytes) and on into
 * 0xEC (82 bytes); the 120 Hz branch writes the same pages with zeroes.  The
 * trailing EB 02 / 93 20 writes select the table and are common to both.
 */
static void ana38407_glut_multi(struct mipi_dsi_multi_context *dsi_ctx, int row)
{
	u8 eb[1 + ANA38407_GLUT_EB_LEN] = { 0xeb };
	u8 ec[1 + ANA38407_GLUT_EC_LEN] = { 0xec };

	if (row >= 0) {
		memcpy(&eb[1], ana38407_amsa10fa01_glut60[row], ANA38407_GLUT_EB_LEN);
		memcpy(&ec[1], &ana38407_amsa10fa01_glut60[row][ANA38407_GLUT_EB_LEN],
		       ANA38407_GLUT_EC_LEN);
	}

	mipi_dsi_dcs_write_seq_multi(dsi_ctx, 0xf0, 0x5a, 0x5a);
	mipi_dsi_dcs_write_seq_multi(dsi_ctx, 0xb0, 0x91, 0xeb);
	mipi_dsi_dcs_write_buffer_multi(dsi_ctx, eb, sizeof(eb));
	mipi_dsi_dcs_write_buffer_multi(dsi_ctx, ec, sizeof(ec));
	mipi_dsi_dcs_write_seq_multi(dsi_ctx, 0xeb, 0x02);
	mipi_dsi_dcs_write_seq_multi(dsi_ctx, 0xb0, 0x26, 0x93);
	mipi_dsi_dcs_write_seq_multi(dsi_ctx, 0x93, 0x20);
	mipi_dsi_dcs_write_seq_multi(dsi_ctx, 0xf0, 0xa5, 0xa5);
}

/* Re-send the GLUT row when a brightness change below 120 Hz moved it. */
static int ana38407_sync_glut_locked(struct ana38407 *ctx)
{
	struct mipi_dsi_multi_context dsi_ctx = { .dsi = ctx->dsi };
	unsigned long mode_flags = ctx->dsi->mode_flags;
	int row = ana38407_glut_row(ctx);

	if (row < 0 || row == ctx->glut_row)
		return 0;

	/* High speed, like the 0x51 write that follows it. */
	ctx->dsi->mode_flags &= ~MIPI_DSI_MODE_LPM;
	ana38407_glut_multi(&dsi_ctx, row);
	ctx->dsi->mode_flags = mode_flags;
	if (!dsi_ctx.accum_err)
		ctx->glut_row = row;

	return dsi_ctx.accum_err;
}

/*
 * The optical sensor needs Samsung's short-lived fingerprint HBM sequence.
 * Keep every brightness and FOD transaction under the same lock: GNOME may
 * update the normal backlight while fprintd is sampling, but that new value
 * must only be remembered and restored after the sample has finished.
 */
static int ana38407_write_brightness_locked(struct ana38407 *ctx, u16 brightness)
{
	unsigned long mode_flags = ctx->dsi->mode_flags;
	int ret;

	ctx->dsi->mode_flags &= ~MIPI_DSI_MODE_LPM;
	ret = mipi_dsi_dcs_set_display_brightness_large(ctx->dsi, brightness);
	ctx->dsi->mode_flags = mode_flags;

	return ret;
}

static int ana38407_write_fod_locked(struct ana38407 *ctx, bool enable)
{
	struct mipi_dsi_multi_context dsi_ctx = { .dsi = ctx->dsi };
	unsigned long mode_flags = ctx->dsi->mode_flags;
	u8 brightness_hi = ctx->user_brightness >> 8;
	u8 brightness_lo = ctx->user_brightness & 0xff;

	ctx->dsi->mode_flags |= MIPI_DSI_MODE_LPM;
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf0, 0x5a, 0x5a);
	if (enable) {
		/* Revision-D optical FOD + FlatZ sequence from Samsung's panel data. */
		mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0x53, 0xe0);
		mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0x51,
					     ANA38407_FOD_BRIGHTNESS >> 8,
					     ANA38407_FOD_BRIGHTNESS & 0xff);
		mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xb0, 0x0a, 0xe0);
		mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xe0, 0x3c, 0xfd, 0xff,
					     0x15, 0x00, 0x00, 0x66, 0xcc,
					     0x00, 0xff, 0x12);
	} else {
		mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0x53, 0x28);
		mipi_dsi_dcs_write_var_seq_multi(&dsi_ctx, 0x51,
						 brightness_hi, brightness_lo);
	}
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf0, 0xa5, 0xa5);
	ctx->dsi->mode_flags = mode_flags;

	return dsi_ctx.accum_err;
}

static int ana38407_generic_write(struct ana38407 *ctx, const u8 *data,
				  size_t len)
{
	ssize_t ret;

	ret = mipi_dsi_generic_write(ctx->dsi, data, len);
	if (ret < 0)
		return ret;

	return 0;
}

static int ana38407_write_fod_circle_locked(struct ana38407 *ctx, bool enable)
{
	static const u8 level2_unlock[] = { 0xf1, 0x5a, 0x5a };
	static const u8 level2_lock[] = { 0xf1, 0xa5, 0xa5 };
	u8 circle[] = { 0x7a, 0x05, 0x00, 0x00, enable ? 0x00 : 0x02 };
	unsigned long mode_flags = ctx->dsi->mode_flags;
	int ret, lock_ret;

	ctx->dsi->mode_flags |= MIPI_DSI_MODE_LPM;
	ret = ana38407_generic_write(ctx, level2_unlock,
				     sizeof(level2_unlock));
	if (!ret)
		ret = ana38407_generic_write(ctx, circle, sizeof(circle));
	lock_ret = ana38407_generic_write(ctx, level2_lock,
					  sizeof(level2_lock));
	ctx->dsi->mode_flags = mode_flags;
	if (!ret)
		ret = lock_ret;

	/* No TE-wait primitive is available here; cover two frames at 60 Hz. */
	if (!ret)
		msleep(ANA38407_FOD_SETTLE_MS);

	return ret;
}

static int ana38407_fod_cleanup_locked(struct ana38407 *ctx)
{
	int ret = 0, tmp;

	if (ctx->fod_circle) {
		tmp = ana38407_write_fod_circle_locked(ctx, false);
		if (!ret)
			ret = tmp;
		ctx->fod_circle = false;
	}

	if (ctx->fod_mode) {
		/*
		 * Brightness changes made while sampling were only remembered;
		 * below 120 Hz their GLUT row goes out ahead of the 0x51 restore.
		 */
		tmp = ana38407_sync_glut_locked(ctx);
		if (!ret)
			ret = tmp;
		tmp = ana38407_write_fod_locked(ctx, false);
		if (!ret)
			ret = tmp;
		ctx->fod_mode = false;
	}

	return ret;
}

static void ana38407_fod_watchdog_work(struct work_struct *work)
{
	struct ana38407 *ctx = container_of(to_delayed_work(work),
						  struct ana38407,
						  fod_watchdog);
	int ret = 0;

	mutex_lock(&ctx->lock);
	if (ctx->prepared && ctx->enabled) {
		ret = ana38407_fod_cleanup_locked(ctx);
	} else {
		ctx->fod_circle = false;
		ctx->fod_mode = false;
	}
	mutex_unlock(&ctx->lock);

	if (ret)
		dev_warn(&ctx->dsi->dev,
			 "failed to leave fingerprint display mode: %d\n", ret);
}

/*
 * Power-on DCS sequence, transcribed from the panel PDF (macros expanded).
 * Level keys 0xF0/0xF1 0x5A 0x5A unlock; 0xA5 0xA5 relock.  The 0xC0/0xB0/0xC1
 * triples are indirect DDIC register accesses (Samsung "gpara").
 */
static int ana38407_on(struct ana38407 *ctx)
{
	struct mipi_dsi_multi_context dsi_ctx = { .dsi = ctx->dsi };
	struct drm_dsc_picture_parameter_set pps;
	u8 module_info[11] = {};
	u8 id[3] = {};
	ssize_t module_info_len;

	ctx->dsi->mode_flags |= MIPI_DSI_MODE_LPM;

	/*
	 * Rev C/D order (POWER_ON_PRE_SETTING): VBP and the display-on-delay
	 * register writes come BEFORE sleep-out.  The panel id (0x80 0x00 0x04,
	 * read by the bootloader as lcd_id=0x800004) is revision D.
	 */

	/* VBP_SETTING_FOR_SDC_IP */
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf0, 0x5a, 0x5a);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf1, 0x5a, 0x5a);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xc1, 0x0a);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xb0, 0x03);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xc0, 0x0f, 0x00, 0x00, 0x00, 0x09, 0xfd, 0x81);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf0, 0xa5, 0xa5);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf1, 0xa5, 0xa5);

	/* DISPLAY_ON_DELAY_SETTING */
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf0, 0x5a, 0x5a);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf1, 0x5a, 0x5a);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xc1, 0x23);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xb0, 0x03);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xc0, 0x0f, 0x00, 0x00, 0x00, 0x01, 0x04, 0x81);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf0, 0xa5, 0xa5);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf1, 0xa5, 0xa5);

	/* sleep out */
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0x11);
	mipi_dsi_msleep(&dsi_ctx, 120);

	/*
	 * Confirm the DDIC answers on the DSI link.  Kept here, right after
	 * sleep-out, because that is where it reliably responds; prepare()
	 * checks the result and retries the whole sequence if it is wrong.
	 */
	mipi_dsi_dcs_read(ctx->dsi, 0xda, &id[0], 1);
	mipi_dsi_dcs_read(ctx->dsi, 0xdb, &id[1], 1);
	mipi_dsi_dcs_read(ctx->dsi, 0xdc, &id[2], 1);
	memcpy(ctx->id, id, sizeof(ctx->id));
	dev_info(&ctx->dsi->dev, "ana38407 panel id: %02x %02x %02x\n",
		 id[0], id[1], id[2]);

	/*
	 * Samsung's fingerprint TA binds optical calibration to the panel cell ID.
	 * RX_MODULE_INFO is DCS A1, 11 bytes under the level-0 key; Android exposes
	 * bytes 4..10 followed by 0..3 as 22 lowercase hexadecimal characters.
	 */
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf0, 0x5a, 0x5a);
	module_info_len = mipi_dsi_dcs_read(ctx->dsi, 0xa1, module_info,
					    sizeof(module_info));
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf0, 0xa5, 0xa5);
	if (module_info_len == (ssize_t)sizeof(module_info)) {
		snprintf(ctx->cell_id, sizeof(ctx->cell_id),
			 "%02x%02x%02x%02x%02x%02x%02x%02x%02x%02x%02x",
			 module_info[4], module_info[5], module_info[6],
			 module_info[7], module_info[8], module_info[9],
			 module_info[10], module_info[0], module_info[1],
			 module_info[2], module_info[3]);
		dev_info(&ctx->dsi->dev, "ana38407 cell id: %s\n", ctx->cell_id);
	} else {
		ctx->cell_id[0] = '\0';
		dev_warn(&ctx->dsi->dev,
			 "failed to read panel cell id: %zd\n", module_info_len);
	}

	/* MX_IP_ENABLE */
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf0, 0x5a, 0x5a);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf1, 0x5a, 0x5a);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xc1, 0x23);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xb0, 0x03);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xc0, 0x0f, 0x00, 0x00, 0x00, 0x09, 0xb2, 0x81);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf0, 0xa5, 0xa5);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf1, 0xa5, 0xa5);

	/* TCON_INTR_SETTING (TE active low) */
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf0, 0x5a, 0x5a);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf1, 0x5a, 0x5a);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xc1, 0x02);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xb0, 0x03);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xc0, 0x0f, 0x00, 0x00, 0x00, 0x14, 0x46, 0x81);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xc1, 0x13);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xb0, 0x03);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xc0, 0x0f, 0x00, 0x00, 0x00, 0x08, 0xcf, 0x81);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xc1, 0x05);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xb0, 0x03);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xc0, 0x0f, 0x00, 0x00, 0x00, 0x09, 0xcd, 0x81);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf0, 0xa5, 0xa5);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf1, 0xa5, 0xa5);

	/* TE_ON */
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf0, 0x5a, 0x5a);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0x35, 0x00);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf0, 0xa5, 0xa5);

	/* TSP_SYNC_SETTING (rev C+) */
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf0, 0x5a, 0x5a);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xb0, 0x0b, 0xb9);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xb9, 0xcc);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xb0, 0x0e, 0xb9);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xb9, 0x15);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf0, 0xa5, 0xa5);

	/*
	 * DSC: enable compression and send the Picture Parameter Set as a proper
	 * MIPI PPS packet generated from drm_dsc_config (this is how mainline
	 * command-mode DSC panels do it; a hand-rolled DCS 0x0A long write is the
	 * wrong packet type).
	 */
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf0, 0x5a, 0x5a);
	mipi_dsi_compression_mode_multi(&dsi_ctx, true);
	drm_dsc_pps_payload_pack(&pps, &ctx->dsc);
	mipi_dsi_picture_parameter_set_multi(&dsi_ctx, &pps);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf0, 0xa5, 0xa5);

	/* DIA_SETTING (digital image adjust on) */
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0x91, 0x02);

	/*
	 * BRIGHTNESS: dimming control (normal) + an explicit non-zero brightness
	 * level (0x51, 12-bit).  Without a real 0x51 write the DDIC emits black
	 * even with the display on.
	 */
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf0, 0x5a, 0x5a);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0x53, 0x28);
	mipi_dsi_dcs_write_var_seq_multi(&dsi_ctx, 0x51,
					 ctx->user_brightness >> 8,
					 ctx->user_brightness & 0xff);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf0, 0xa5, 0xa5);

	/* SP_SETTING */
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf0, 0x5a, 0x5a);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xc3, 0x02);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf0, 0xa5, 0xa5);

	/*
	 * The stock panel declares samsung,delayed-display-on: complete the
	 * initialisation here, but keep the OLED dark until the bridge's enable
	 * phase, when the DPU/DSI command-mode stream is ready.  Sending 0x29
	 * from prepare exposed unsynchronised DSC data after resume: the shell's
	 * freshly damaged top bar was valid while the rest of the panel GRAM
	 * contained coloured noise.
	 */
	mipi_dsi_msleep(&dsi_ctx, 100);

	return dsi_ctx.accum_err;
}

/*
 * AMSA10FA01 (SM-X710), panel revision D (id 0x80 0x00 0x04): Samsung's
 * POWER_ON_PRE_SETTING for GTS9_ANA38407_AMSA10FA01, transcribed macro by
 * macro from the panel data file in the X710 open-source kernel.  It shares
 * MX_IP_ENABLE, TCON_INTR_SETTING, TE_ON, DSC, DIA, brightness and SP_SETTING
 * with the Ultra glass, but brackets them with slew boosting and uses its own
 * display-on-delay and TSP sync registers; the Ultra's VBP_SETTING_FOR_SDC_IP
 * writes a vertical porch for the 1848-line panel and must not be sent here.
 *
 * In command mode the DDIC runs its own refresh, so a mode below 120 Hz only
 * takes effect through VRR_SETTING, and needs GLUT_SETTING's 60 Hz gamma
 * offsets on top.  Both follow the mode prepare() found in the commit: the
 * normal-speed "60HS" and "30HS" branches, which One UI uses for its
 * 60 Hz setting; the frame-skipping "PHS" modes are not offered.
 */
struct ana38407_vrr_cmds {
	u8 scan;	/* 0x60: 0x00 120 Hz scan, 0x10 60 Hz scan */
	u8 skip;	/* 0xDD at gpara 0x13: frames skipped per refresh */
	u8 b9[4];	/* 0xB9 at gpara 0x10, revision C onwards */
};

static const struct ana38407_vrr_cmds ana38407_amsa10fa01_vrr[] = {
	[ANA38407_VRR_120HS] = { 0x00, 0x00, { 0x80, 0x00, 0x00, 0x00 } },
	[ANA38407_VRR_60HS]  = { 0x10, 0x00, { 0xaa, 0xaa, 0xaa, 0xaa } },
	[ANA38407_VRR_30HS]  = { 0x10, 0x01, { 0xaa, 0xaa, 0xaa, 0xaa } },
};

static int ana38407_amsa10fa01_on(struct ana38407 *ctx)
{
	struct mipi_dsi_multi_context dsi_ctx = { .dsi = ctx->dsi };
	struct drm_dsc_picture_parameter_set pps;
	static const u8 slew_regs[] = { 0x4f, 0x62, 0x75, 0x88 };
	const struct ana38407_vrr_cmds *vrr = &ana38407_amsa10fa01_vrr[ctx->vrr];
	u8 module_info[11] = {};
	u8 id[3] = {};
	ssize_t module_info_len;
	unsigned int i;

	ctx->dsi->mode_flags |= MIPI_DSI_MODE_LPM;

	/* SLEW_BOOSTING_OFF */
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf0, 0x5a, 0x5a);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf1, 0x5a, 0x5a);
	for (i = 0; i < ARRAY_SIZE(slew_regs); i++) {
		mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xc1, 0x2a);
		mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xb0, 0x03);
		mipi_dsi_dcs_write_var_seq_multi(&dsi_ctx, 0xc0, 0x0f, 0x00, 0x00,
						 0x00, 0x13, slew_regs[i], 0x81);
	}
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf0, 0xa5, 0xa5);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf1, 0xa5, 0xa5);

	/* PM_EN_DISP_ON_DELAY */
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf0, 0x5a, 0x5a);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf1, 0x5a, 0x5a);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xc1, 0x00);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xb0, 0x03);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xc0, 0x0f, 0x00, 0x00, 0x00, 0x14, 0x35, 0x81);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xc1, 0x23);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xb0, 0x03);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xc0, 0x0f, 0x00, 0x00, 0x00, 0x01, 0x04, 0x81);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf0, 0xa5, 0xa5);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf1, 0xa5, 0xa5);

	/* Sleep out.  Samsung waits 50 ms; the DCS minimum of 120 ms costs nothing. */
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0x11);
	mipi_dsi_msleep(&dsi_ctx, 120);

	mipi_dsi_dcs_read(ctx->dsi, 0xda, &id[0], 1);
	mipi_dsi_dcs_read(ctx->dsi, 0xdb, &id[1], 1);
	mipi_dsi_dcs_read(ctx->dsi, 0xdc, &id[2], 1);
	memcpy(ctx->id, id, sizeof(ctx->id));
	dev_info(&ctx->dsi->dev, "ana38407 panel id: %02x %02x %02x\n",
		 id[0], id[1], id[2]);

	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf0, 0x5a, 0x5a);
	module_info_len = mipi_dsi_dcs_read(ctx->dsi, 0xa1, module_info,
					    sizeof(module_info));
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf0, 0xa5, 0xa5);
	if (module_info_len == (ssize_t)sizeof(module_info)) {
		snprintf(ctx->cell_id, sizeof(ctx->cell_id),
			 "%02x%02x%02x%02x%02x%02x%02x%02x%02x%02x%02x",
			 module_info[4], module_info[5], module_info[6],
			 module_info[7], module_info[8], module_info[9],
			 module_info[10], module_info[0], module_info[1],
			 module_info[2], module_info[3]);
		dev_info(&ctx->dsi->dev, "ana38407 cell id: %s\n", ctx->cell_id);
	} else {
		ctx->cell_id[0] = '\0';
		dev_warn(&ctx->dsi->dev,
			 "failed to read panel cell id: %zd\n", module_info_len);
	}

	/* MX_IP_ENABLE */
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf0, 0x5a, 0x5a);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf1, 0x5a, 0x5a);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xc1, 0x23);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xb0, 0x03);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xc0, 0x0f, 0x00, 0x00, 0x00, 0x09, 0xb2, 0x81);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf0, 0xa5, 0xa5);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf1, 0xa5, 0xa5);

	/* TCON_INTR_SETTING */
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf0, 0x5a, 0x5a);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf1, 0x5a, 0x5a);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xc1, 0x02);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xb0, 0x03);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xc0, 0x0f, 0x00, 0x00, 0x00, 0x14, 0x46, 0x81);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xc1, 0x13);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xb0, 0x03);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xc0, 0x0f, 0x00, 0x00, 0x00, 0x08, 0xcf, 0x81);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xc1, 0x05);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xb0, 0x03);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xc0, 0x0f, 0x00, 0x00, 0x00, 0x09, 0xcd, 0x81);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf0, 0xa5, 0xa5);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf1, 0xa5, 0xa5);

	/* Revision D: neither SSCG block applies (B and C only). */

	/* TE_ON */
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf0, 0x5a, 0x5a);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0x35, 0x00);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf0, 0xa5, 0xa5);

	/* TSP_SYNC_SETTING (revision C+): no 0x0e/0xb9 pair on this glass. */
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf0, 0x5a, 0x5a);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xb0, 0x0b, 0xb9);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xb9, 0xcc);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf0, 0xa5, 0xa5);

	/* DSC_SETTING: compression on, PPS from drm_dsc_config (2 x 1280 x 100). */
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf0, 0x5a, 0x5a);
	mipi_dsi_compression_mode_multi(&dsi_ctx, true);
	drm_dsc_pps_payload_pack(&pps, &ctx->dsc);
	mipi_dsi_picture_parameter_set_multi(&dsi_ctx, &pps);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf0, 0xa5, 0xa5);

	/* DIA_SETTING */
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0x91, 0x02);

	/*
	 * GLUT_SETTING: both gamma LUT pages cleared at 120 Hz, the
	 * GLUT_OFFSET_60HS row of the current brightness at 60 and 30 Hz.
	 */
	ctx->glut_row = ana38407_glut_row(ctx);
	ana38407_glut_multi(&dsi_ctx, ctx->glut_row);

	/* BRIGHTNESS_DIMMING_SETTING: normal mode plus a real level. */
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf0, 0x5a, 0x5a);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0x53, 0x28);
	mipi_dsi_dcs_write_var_seq_multi(&dsi_ctx, 0x51,
					 ctx->user_brightness >> 8,
					 ctx->user_brightness & 0xff);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf0, 0xa5, 0xa5);

	/* SP_SETTING */
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf0, 0x5a, 0x5a);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xc3, 0x02);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf0, 0xa5, 0xa5);

	mipi_dsi_msleep(&dsi_ctx, 20);

	/* SLEW_BOOSTING_ON */
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf0, 0x5a, 0x5a);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf1, 0x5a, 0x5a);
	for (i = 0; i < ARRAY_SIZE(slew_regs); i++) {
		mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xc1, 0x2e);
		mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xb0, 0x03);
		mipi_dsi_dcs_write_var_seq_multi(&dsi_ctx, 0xc0, 0x0f, 0x00, 0x00,
						 0x00, 0x13, slew_regs[i], 0x81);
	}
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf0, 0xa5, 0xa5);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf1, 0xa5, 0xa5);

	/* VRR_SETTING, revision C+ variant, for the mode being enabled. */
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf0, 0x5a, 0x5a);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf1, 0x5a, 0x5a);
	mipi_dsi_dcs_write_var_seq_multi(&dsi_ctx, 0x60, vrr->scan);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xb0, 0x13, 0xdd);
	mipi_dsi_dcs_write_var_seq_multi(&dsi_ctx, 0xdd, vrr->skip);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xb0, 0x10, 0xb9);
	mipi_dsi_dcs_write_var_seq_multi(&dsi_ctx, 0xb9, vrr->b9[0], vrr->b9[1],
					 vrr->b9[2], vrr->b9[3]);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf0, 0xa5, 0xa5);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf1, 0xa5, 0xa5);

	/* Display-on (0x29) stays in enable(), once the DPU streams. */
	mipi_dsi_msleep(&dsi_ctx, 100);

	return dsi_ctx.accum_err;
}

static int ana38407_enable(struct drm_panel *panel)
{
	struct ana38407 *ctx = to_ana38407(panel);
	struct mipi_dsi_multi_context dsi_ctx = { .dsi = ctx->dsi };
	int ret = 0;

	mutex_lock(&ctx->lock);
	if (!ctx->prepared) {
		ret = -EPIPE;
		goto out_unlock;
	}
	if (ctx->enabled)
		goto out_unlock;

	ctx->dsi->mode_flags |= MIPI_DSI_MODE_LPM;
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf0, 0x5a, 0x5a);
	mipi_dsi_dcs_set_display_on_multi(&dsi_ctx);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf0, 0xa5, 0xa5);
	mipi_dsi_msleep(&dsi_ctx, 20);
	ret = dsi_ctx.accum_err;
	if (!ret)
		ctx->enabled = true;

out_unlock:
	mutex_unlock(&ctx->lock);
	return ret;
}

static int ana38407_disable(struct drm_panel *panel)
{
	struct ana38407 *ctx = to_ana38407(panel);
	struct mipi_dsi_multi_context dsi_ctx = { .dsi = ctx->dsi };
	int cleanup_ret = 0, ret = 0;

	mutex_lock(&ctx->lock);
	if (!ctx->enabled)
		goto out_unlock;

	cleanup_ret = ana38407_fod_cleanup_locked(ctx);
	ctx->dsi->mode_flags |= MIPI_DSI_MODE_LPM;
	mipi_dsi_dcs_set_display_off_multi(&dsi_ctx);
	mipi_dsi_msleep(&dsi_ctx, 20);
	ret = dsi_ctx.accum_err;
	ctx->enabled = false;

out_unlock:
	mutex_unlock(&ctx->lock);
	cancel_delayed_work_sync(&ctx->fod_watchdog);

	return ret ?: cleanup_ret;
}

static int ana38407_sleep_in(struct ana38407 *ctx)
{
	struct mipi_dsi_multi_context dsi_ctx = { .dsi = ctx->dsi };

	ctx->dsi->mode_flags |= MIPI_DSI_MODE_LPM;
	mipi_dsi_dcs_enter_sleep_mode_multi(&dsi_ctx);
	mipi_dsi_msleep(&dsi_ctx, 120);

	return dsi_ctx.accum_err;
}

/*
 * prepare() runs inside the atomic commit that enables a mode, after the new
 * state has been swapped in, so the connector's CRTC state holds the mode the
 * DDIC must be set up for.  Glasses without per-mode DDIC settings, and any
 * prepare outside a commit, keep the 120 Hz settings.
 */
static enum ana38407_vrr ana38407_target_vrr(struct ana38407 *ctx)
{
	struct drm_connector *connector = ctx->connector;
	int refresh;

	if (!ctx->desc->vrr_by_mode || !connector || !connector->state ||
	    !connector->state->crtc)
		return ANA38407_VRR_120HS;

	refresh = drm_mode_vrefresh(&connector->state->crtc->state->mode);
	if (refresh <= 40)
		return ANA38407_VRR_30HS;
	if (refresh <= 90)
		return ANA38407_VRR_60HS;

	return ANA38407_VRR_120HS;
}

static const char *const ana38407_vrr_names[] = {
	[ANA38407_VRR_120HS] = "120HS",
	[ANA38407_VRR_60HS] = "60HS",
	[ANA38407_VRR_30HS] = "30HS",
};

static int ana38407_prepare(struct drm_panel *panel)
{
	struct ana38407 *ctx = to_ana38407(panel);
	struct device *dev = &ctx->dsi->dev;
	const u8 *expected = ctx->desc->expected_id;
	int tcon_before = 0, tcon_ms = 0;
	char supplies[64] = "";
	int ret;

	mutex_lock(&ctx->lock);
	if (ctx->prepared) {
		ret = 0;
		goto out_unlock;
	}

	ctx->vrr = ana38407_target_vrr(ctx);

	if (!ctx->prepared_once && param_cold_start_ms)
		ana38407_cold_start(ctx);

	ctx->power_ons++;
	if (param_trace_power_on)
		ana38407_supply_states(ctx, supplies, sizeof(supplies));

	ret = ana38407_power_on(ctx);
	if (ret)
		goto out_unlock;

	if (ctx->tcon_rdy_gpio)
		tcon_before = gpiod_get_value_cansleep(ctx->tcon_rdy_gpio);
	ana38407_reset(ctx);
	if (ctx->tcon_rdy_gpio) {
		tcon_ms = ana38407_wait_tcon_ready(ctx);
		if (param_trace_power_on)
			dev_info(dev, "power-on %u: supplies before it: %s; TCON_RDY %s before reset, %s %d ms after it\n",
				 ctx->power_ons, supplies,
				 ana38407_level(tcon_before),
				 tcon_ms < 0 ? "still low" : "high",
				 tcon_ms < 0 ? ANA38407_TCON_RDY_TIMEOUT_US / 1000 : tcon_ms);
		/*
		 * The first power-on takes over the bootloader's splash: record
		 * how the DDIC came out of reset, for the cold-boot fault above.
		 */
		if (!ctx->prepared_once && tcon_ms < 0)
			dev_info(dev, "first power-on: TCON_RDY %s before reset, still low %d ms after it\n",
				 tcon_before > 0 ? "high" : "low",
				 ANA38407_TCON_RDY_TIMEOUT_US / 1000);
		else if (!ctx->prepared_once)
			dev_info(dev, "first power-on: TCON_RDY %s before reset, high %d ms after it\n",
				 tcon_before > 0 ? "high" : "low", tcon_ms);
		else if (tcon_ms < 0)
			dev_warn(dev, "TCON_RDY still low %d ms after reset\n",
				 ANA38407_TCON_RDY_TIMEOUT_US / 1000);
	} else if (param_trace_power_on) {
		dev_info(dev, "power-on %u: supplies before it: %s; no TCON_RDY line\n",
			 ctx->power_ons, supplies);
	}
	ctx->prepared_once = true;

	ret = ctx->desc->on(ctx);
	/* on() reads the id whether or not its writes went through. */
	ctx->id_read = true;
	if (param_trace_power_on)
		dev_info(dev, "power-on %u: panel id %02x %02x %02x, on sequence %d\n",
			 ctx->power_ons, ctx->id[0], ctx->id[1], ctx->id[2], ret);
	if (ret) {
		ctx->glut_row = -1;
		gpiod_set_value_cansleep(ctx->reset_gpio, 0);
		regulator_bulk_disable(ARRAY_SIZE(ana38407_supplies), ctx->supplies);
		goto out_unlock;
	}
	ctx->prepared = true;

	if (ctx->vrr != ANA38407_VRR_120HS)
		dev_info(dev, "VRR_SETTING %s, GLUT_OFFSET_60HS row %d\n",
			 ana38407_vrr_names[ctx->vrr], ctx->glut_row);

	/*
	 * 00 00 00 is not a glass revision but a DDIC that never answered the
	 * read (the cold-boot fault described above); anything else that is
	 * not the pinned id is glass this power-on sequence was not written for.
	 */
	if (!memchr_inv(ctx->id, 0, sizeof(ctx->id)))
		dev_warn(dev, "panel id 00 00 00: the DDIC did not answer; the panel will stay dark until a suspend/resume re-initialises the DSI host\n");
	else if (!expected)
		dev_info(dev, "panel id %02x %02x %02x (no expected id pinned for this glass yet)\n",
			 ctx->id[0], ctx->id[1], ctx->id[2]);
	else if (memcmp(ctx->id, expected, sizeof(ctx->id)))
		dev_warn(dev, "panel id %02x %02x %02x, expected %02x %02x %02x: the power-on sequence is written for that revision\n",
			 ctx->id[0], ctx->id[1], ctx->id[2],
			 expected[0], expected[1], expected[2]);

out_unlock:
	mutex_unlock(&ctx->lock);
	return ret;
}

static int ana38407_unprepare(struct drm_panel *panel)
{
	struct ana38407 *ctx = to_ana38407(panel);
	int cleanup_ret = 0, ret = 0;

	mutex_lock(&ctx->lock);
	if (!ctx->prepared)
		goto out_unlock;

	cleanup_ret = ana38407_fod_cleanup_locked(ctx);
	ret = ana38407_sleep_in(ctx);
	ctx->enabled = false;
	ctx->prepared = false;
	/* The DDIC loses its gamma LUT with its supplies. */
	ctx->glut_row = -1;
	gpiod_set_value_cansleep(ctx->reset_gpio, 0);
	regulator_bulk_disable(ARRAY_SIZE(ana38407_supplies), ctx->supplies);

out_unlock:
	mutex_unlock(&ctx->lock);
	cancel_delayed_work_sync(&ctx->fod_watchdog);

	return ret ?: cleanup_ret;
}

/* All five timing modes from the stock DTBO, hactive/vactive 2960x1848. */
static const struct drm_display_mode ana38407_modes[] = {
	{	/* 120 Hz */
		.clock = (2960 + 16 + 36 + 30) * (1848 + 16 + 32 + 32) * 120 / 1000,
		.hdisplay = 2960, .hsync_start = 2960 + 16, .hsync_end = 2960 + 16 + 36,
		.htotal = 2960 + 16 + 36 + 30,
		.vdisplay = 1848, .vsync_start = 1848 + 16, .vsync_end = 1848 + 16 + 32,
		.vtotal = 1848 + 16 + 32 + 32,
	},
	{	/* 60 Hz */
		.clock = (2960 + 256 + 256 + 255) * (1848 + 127 + 256 + 137) * 60 / 1000,
		.hdisplay = 2960, .hsync_start = 2960 + 256, .hsync_end = 2960 + 256 + 256,
		.htotal = 2960 + 256 + 256 + 255,
		.vdisplay = 1848, .vsync_start = 1848 + 127, .vsync_end = 1848 + 127 + 256,
		.vtotal = 1848 + 127 + 256 + 137,
	},
};

/*
 * Tab S9 11" (gts9wifi): AMSA10FA01 glass, 2560x1600, command mode.  The
 * blanking is not transmitted in command mode; it only sets the DSI byte
 * clock: msm's dsi_adjust_pclk_for_compression() shrinks hdisplay to
 * DIV_ROUND_UP(2560 * 8 / 24) = 854 columns, keeps the blanking, and the lane
 * rate is six times that pixel clock.  Stock Android runs every mode at
 * 1.524 Gbps (qcom,mdss-dsi-panel-clockrate), with the D-PHY timing table the
 * board DTS hands the PHY.
 *
 * 120 Hz (wqxga120hs): the stock porches 34/64/34 would give 1.234 Gbps; a
 * 266-pixel back porch reproduces the stock rate ((854 + 364) * 1738 * 120 =
 * 254 MHz).  That rate needs Samsung's PHY table: with the timings mainline
 * computes it showed frame-done timeouts and more speckle, with the table it
 * ran smoothly.  Build 10's black panel was the continuous clock, which stays
 * off (stock declares no qcom,mdss-dsi-force-clock-lane-hs).
 *
 * 60 and 30 Hz (wqxga60hs, wqxga30hs): Samsung's own porches, which land on the
 * same lane rate (1.525 Gbps), so the PHY table applies unchanged and a mode
 * switch does not move the link.  30 Hz is offered only with
 * panel_samsung_ana38407.mode_30hz=1.
 */
static const struct drm_display_mode ana38407_amsa10fa01_modes[] = {
	{	/* 120 Hz, 1.524 Gbps per lane */
		.clock = (2560 + 34 + 64 + 266) * (1600 + 42 + 64 + 32) * 120 / 1000,
		.hdisplay = 2560, .hsync_start = 2560 + 34, .hsync_end = 2560 + 34 + 64,
		.htotal = 2560 + 34 + 64 + 266,
		.vdisplay = 1600, .vsync_start = 1600 + 42, .vsync_end = 1600 + 42 + 64,
		.vtotal = 1600 + 42 + 64 + 32,
	},
	{	/* 60 Hz, 1.525 Gbps per lane */
		.clock = (2560 + 128 + 512 + 203) * (1600 + 127 + 512 + 257) * 60 / 1000,
		.hdisplay = 2560, .hsync_start = 2560 + 128, .hsync_end = 2560 + 128 + 512,
		.htotal = 2560 + 128 + 512 + 203,
		.vdisplay = 1600, .vsync_start = 1600 + 127, .vsync_end = 1600 + 127 + 512,
		.vtotal = 1600 + 127 + 512 + 257,
	},
	{	/* 30 Hz, 1.525 Gbps per lane */
		.clock = (2560 + 600 + 735 + 512) * (1600 + 512 + 512 + 512) * 30 / 1000,
		.hdisplay = 2560, .hsync_start = 2560 + 600, .hsync_end = 2560 + 600 + 735,
		.htotal = 2560 + 600 + 735 + 512,
		.vdisplay = 1600, .vsync_start = 1600 + 512, .vsync_end = 1600 + 512 + 512,
		.vtotal = 1600 + 512 + 512 + 512,
	},
};

static const struct ana38407_desc ana38407_amsa46as02_desc = {
	.on = ana38407_on,
	.modes = ana38407_modes,
	.num_modes = ARRAY_SIZE(ana38407_modes),
	.width_mm = 313,
	.height_mm = 196,
	.pic_width = 2960,
	.pic_height = 1848,
	.slice_width = 1480,
	.slice_height = 77,
	.expected_id = ana38407_expected_id,
};

static const struct ana38407_desc ana38407_amsa10fa01_desc = {
	.on = ana38407_amsa10fa01_on,
	/* Samsung's platform scale never goes below 10/2047 (about 2 nits). */
	.min_brightness = 10,
	.modes = ana38407_amsa10fa01_modes,
	.num_modes = ARRAY_SIZE(ana38407_amsa10fa01_modes),
	/* Stock r04 physical dimensions: 236 x 148 mm. */
	.width_mm = 236,
	.height_mm = 148,
	.pic_width = 2560,
	.pic_height = 1600,
	/* Stock r04 DSC geometry: two 1280 x 100 slices, 8 bpc / 8 bpp. */
	.slice_width = 1280,
	.slice_height = 100,
	/* Off: stock has no forced HS clock lane, and on it the glass stays black. */
	.continuous_clock = false,
	.expected_id = ana38407_expected_id,
	.vrr_by_mode = true,
};

static int ana38407_get_modes(struct drm_panel *panel,
			      struct drm_connector *connector)
{
	struct ana38407 *ctx = to_ana38407(panel);
	const struct ana38407_desc *desc = ctx->desc;
	struct drm_display_mode *mode;
	int i, count = 0;

	ctx->connector = connector;

	for (i = 0; i < desc->num_modes; i++) {
		if (drm_mode_vrefresh(&desc->modes[i]) == 30 && !param_mode_30hz)
			continue;
		mode = drm_mode_duplicate(connector->dev, &desc->modes[i]);
		if (!mode)
			continue;
		if (i == 0 && param_hback_porch >= 0) {
			int vrefresh = drm_mode_vrefresh(&desc->modes[i]);

			mode->htotal = mode->hsync_end + param_hback_porch;
			mode->clock = DIV_ROUND_CLOSEST(mode->htotal * mode->vtotal *
							vrefresh, 1000);
			dev_info(panel->dev, "mode %d: back porch %d, clock %d kHz (boot parameter)\n",
				 i, param_hback_porch, mode->clock);
		}
		mode->type = DRM_MODE_TYPE_DRIVER;
		if (i == 0)
			mode->type |= DRM_MODE_TYPE_PREFERRED;
		mode->width_mm = desc->width_mm;
		mode->height_mm = desc->height_mm;
		drm_mode_set_name(mode);
		drm_mode_probed_add(connector, mode);
		count++;
	}

	connector->display_info.width_mm = desc->width_mm;
	connector->display_info.height_mm = desc->height_mm;

	return count;
}

static const struct drm_panel_funcs ana38407_panel_funcs = {
	.prepare = ana38407_prepare,
	.enable = ana38407_enable,
	.disable = ana38407_disable,
	.unprepare = ana38407_unprepare,
	.get_modes = ana38407_get_modes,
};

static int ana38407_bl_update(struct backlight_device *bl)
{
	struct ana38407 *ctx = bl_get_data(bl);
	u16 brightness = min_t(u16, backlight_get_brightness(bl),
			       ANA38407_MAX_BRIGHTNESS);
	int ret = 0;

	if (brightness && brightness < ctx->desc->min_brightness)
		brightness = ctx->desc->min_brightness;
	mutex_lock(&ctx->lock);
	ctx->user_brightness = brightness;
	if (ctx->prepared && !ctx->fod_mode) {
		/* Samsung's BRIGHTNESS_SETTING: GLUT first, then 0x51. */
		int glut_ret = ana38407_sync_glut_locked(ctx);

		ret = ana38407_write_brightness_locked(ctx, brightness);
		if (!ret)
			ret = glut_ret;
	}
	mutex_unlock(&ctx->lock);

	return ret;
}

static const struct backlight_ops ana38407_bl_ops = {
	.update_status = ana38407_bl_update,
};

static struct ana38407 *ana38407_from_bl_dev(struct device *dev)
{
	return bl_get_data(to_backlight_device(dev));
}

static void ana38407_update_fod_watchdog_locked(struct ana38407 *ctx)
{
	if (ctx->fod_mode || ctx->fod_circle)
		mod_delayed_work(system_wq, &ctx->fod_watchdog,
				 msecs_to_jiffies(ANA38407_FOD_WATCHDOG_MS));
	else
		cancel_delayed_work(&ctx->fod_watchdog);
}

static ssize_t fod_mode_show(struct device *dev,
			     struct device_attribute *attr, char *buf)
{
	struct ana38407 *ctx = ana38407_from_bl_dev(dev);
	bool enabled;

	mutex_lock(&ctx->lock);
	enabled = ctx->fod_mode;
	mutex_unlock(&ctx->lock);

	return sysfs_emit(buf, "%u\n", enabled);
}

static ssize_t fod_mode_store(struct device *dev,
			      struct device_attribute *attr,
			      const char *buf, size_t count)
{
	struct ana38407 *ctx = ana38407_from_bl_dev(dev);
	bool enable;
	int ret;

	ret = kstrtobool(buf, &enable);
	if (ret)
		return ret;

	mutex_lock(&ctx->lock);
	if (!ctx->prepared || !ctx->enabled) {
		ret = -EPIPE;
		goto out_unlock;
	}

	if (ctx->fod_mode == enable) {
		ana38407_update_fod_watchdog_locked(ctx);
		ret = 0;
		goto out_unlock;
	}

	if (enable) {
		ret = ana38407_write_fod_locked(ctx, true);
		if (ret) {
			/* A failed sequence may have raised HBM; restore normal mode. */
			ana38407_write_fod_locked(ctx, false);
			goto out_unlock;
		}
		ctx->fod_mode = true;
	} else {
		ret = ana38407_fod_cleanup_locked(ctx);
	}
	ana38407_update_fod_watchdog_locked(ctx);

out_unlock:
	mutex_unlock(&ctx->lock);
	return ret ? ret : count;
}

static ssize_t fod_circle_show(struct device *dev,
			       struct device_attribute *attr, char *buf)
{
	struct ana38407 *ctx = ana38407_from_bl_dev(dev);
	bool enabled;

	mutex_lock(&ctx->lock);
	enabled = ctx->fod_circle;
	mutex_unlock(&ctx->lock);

	return sysfs_emit(buf, "%u\n", enabled);
}

static ssize_t fod_circle_store(struct device *dev,
				struct device_attribute *attr,
				const char *buf, size_t count)
{
	struct ana38407 *ctx = ana38407_from_bl_dev(dev);
	bool enable;
	int ret;

	ret = kstrtobool(buf, &enable);
	if (ret)
		return ret;

	mutex_lock(&ctx->lock);
	if (!ctx->prepared || !ctx->enabled) {
		ret = -EPIPE;
		goto out_unlock;
	}
	if (enable && !ctx->fod_mode) {
		ret = -EINVAL;
		goto out_unlock;
	}
	if (ctx->fod_circle == enable) {
		ana38407_update_fod_watchdog_locked(ctx);
		ret = 0;
		goto out_unlock;
	}

	ret = ana38407_write_fod_circle_locked(ctx, enable);
	if (!ret)
		ctx->fod_circle = enable;
	else if (enable)
		ana38407_write_fod_circle_locked(ctx, false);
	ana38407_update_fod_watchdog_locked(ctx);

out_unlock:
	mutex_unlock(&ctx->lock);
	return ret ? ret : count;
}

static ssize_t fod_ready_show(struct device *dev,
			      struct device_attribute *attr, char *buf)
{
	struct ana38407 *ctx = ana38407_from_bl_dev(dev);
	bool ready;

	mutex_lock(&ctx->lock);
	ready = ctx->prepared && ctx->enabled;
	mutex_unlock(&ctx->lock);

	return sysfs_emit(buf, "%u\n", ready);
}

static ssize_t fod_brightness_show(struct device *dev,
				   struct device_attribute *attr, char *buf)
{
	/* Raw WRDISBV programmed by fod_mode, not the desktop's saved value. */
	return sysfs_emit(buf, "%u\n", ANA38407_FOD_BRIGHTNESS);
}

static ssize_t cell_id_show(struct device *dev,
			    struct device_attribute *attr, char *buf)
{
	struct ana38407 *ctx = ana38407_from_bl_dev(dev);
	ssize_t ret;

	mutex_lock(&ctx->lock);
	ret = ctx->cell_id[0] ? sysfs_emit(buf, "%s\n", ctx->cell_id) : -ENODATA;
	mutex_unlock(&ctx->lock);

	return ret;
}

/*
 * What the DDIC answered to DA/DB/DC at the last power-on: "00 00 00" after a
 * cold boot that the DSI host has not recovered from yet, ENODATA before the
 * first power-on.  Lets the boot-time recovery skip work the kernel already
 * did.
 */
static ssize_t panel_id_show(struct device *dev,
			     struct device_attribute *attr, char *buf)
{
	struct ana38407 *ctx = ana38407_from_bl_dev(dev);
	ssize_t ret;

	mutex_lock(&ctx->lock);
	ret = ctx->id_read ? sysfs_emit(buf, "%02x %02x %02x\n", ctx->id[0],
					ctx->id[1], ctx->id[2]) : -ENODATA;
	mutex_unlock(&ctx->lock);

	return ret;
}

static DEVICE_ATTR_RW(fod_mode);
static DEVICE_ATTR_RW(fod_circle);
static DEVICE_ATTR_RO(fod_ready);
static DEVICE_ATTR_RO(fod_brightness);
static DEVICE_ATTR_RO(cell_id);
static DEVICE_ATTR_RO(panel_id);

static struct attribute *ana38407_bl_attrs[] = {
	&dev_attr_fod_mode.attr,
	&dev_attr_fod_circle.attr,
	&dev_attr_fod_ready.attr,
	&dev_attr_fod_brightness.attr,
	&dev_attr_cell_id.attr,
	&dev_attr_panel_id.attr,
	NULL,
};

static const struct attribute_group ana38407_bl_attr_group = {
	.attrs = ana38407_bl_attrs,
};

static struct backlight_device *ana38407_create_backlight(struct ana38407 *ctx)
{
	struct device *dev = &ctx->dsi->dev;
	const struct backlight_properties props = {
		.type = BACKLIGHT_RAW,
		.brightness = ANA38407_MAX_BRIGHTNESS,
		.max_brightness = ANA38407_MAX_BRIGHTNESS,
	};

	return devm_backlight_device_register(dev, dev_name(dev), dev, ctx,
					      &ana38407_bl_ops, &props);
}

/*
 * DSC config decoded from the panel's PPS (WT 0x0A ...): DSC 1.1, 2960x1848,
 * two 1480x77 slices, 8 bpc, 8.0 bpp.  The rc_buf_thresh / rc_range_params are
 * the DSC 8 bpp spec-standard tables (identical across 8 bpp panels).  The msm
 * DSI host fills convert_rgb/line_buf_depth and calls
 * drm_dsc_compute_rc_parameters() for the derived fields, so those are left out.
 */
static const struct drm_dsc_config ana38407_dsc_template = {
	.dsc_version_major = 1,
	.dsc_version_minor = 1,
	.slice_height = 77,
	.slice_width = 1480,
	.slice_count = 2,
	.bits_per_component = 8,
	.bits_per_pixel = 8 << 4,
	.block_pred_enable = true,
	.pic_width = 2960,
	.pic_height = 1848,
	.rc_buf_thresh = {
		14, 28, 42, 56, 70, 84, 98, 105, 112, 119, 121, 123, 125, 126
	},
	.rc_model_size = DSC_RC_MODEL_SIZE_CONST,
	.rc_edge_factor = DSC_RC_EDGE_FACTOR_CONST,
	.rc_tgt_offset_high = DSC_RC_TGT_OFFSET_HI_CONST,
	.rc_tgt_offset_low = DSC_RC_TGT_OFFSET_LO_CONST,
	.mux_word_size = DSC_MUX_WORD_SIZE_8_10_BPC,
	.line_buf_depth = 9,
	.first_line_bpg_offset = 12,
	.initial_xmit_delay = 512,
	.initial_offset = 6144,
	.rc_quant_incr_limit0 = 11,
	.rc_quant_incr_limit1 = 11,
	.rc_range_params = {
		{ 0,  4, DSC_BPG_OFFSET(2)},
		{ 0,  4, DSC_BPG_OFFSET(0)},
		{ 1,  5, DSC_BPG_OFFSET(0)},
		{ 1,  6, DSC_BPG_OFFSET(-2)},
		{ 3,  7, DSC_BPG_OFFSET(-4)},
		{ 3,  7, DSC_BPG_OFFSET(-6)},
		{ 3,  7, DSC_BPG_OFFSET(-8)},
		{ 3,  8, DSC_BPG_OFFSET(-8)},
		{ 3,  9, DSC_BPG_OFFSET(-8)},
		{ 3, 10, DSC_BPG_OFFSET(-10)},
		{ 5, 10, DSC_BPG_OFFSET(-10)},
		{ 5, 11, DSC_BPG_OFFSET(-12)},
		{ 5, 11, DSC_BPG_OFFSET(-12)},
		{ 9, 12, DSC_BPG_OFFSET(-12)},
		{12, 13, DSC_BPG_OFFSET(-12)},
	},
	.slice_chunk_size = 1480,
};

static void ana38407_dsc_config(struct ana38407 *ctx)
{
	ctx->dsc = ana38407_dsc_template;
	ctx->dsc.pic_width = ctx->desc->pic_width;
	ctx->dsc.pic_height = ctx->desc->pic_height;
	ctx->dsc.slice_width = ctx->desc->slice_width;
	ctx->dsc.slice_height = ctx->desc->slice_height;
}

static int ana38407_probe(struct mipi_dsi_device *dsi)
{
	struct device *dev = &dsi->dev;
	struct ana38407 *ctx;
	bool continuous_clock;
	int ret;

	ctx = devm_drm_panel_alloc(dev, struct ana38407, panel,
				   &ana38407_panel_funcs,
				   DRM_MODE_CONNECTOR_DSI);
	if (IS_ERR(ctx))
		return PTR_ERR(ctx);

	ctx->desc = of_device_get_match_data(dev);
	if (!ctx->desc)
		ctx->desc = &ana38407_amsa46as02_desc;

	ret = devm_regulator_bulk_get_const(dev, ARRAY_SIZE(ana38407_supplies),
					    ana38407_supplies, &ctx->supplies);
	if (ret < 0)
		return dev_err_probe(dev, ret, "failed to get panel regulators\n");

	ctx->reset_gpio = devm_gpiod_get(dev, "reset", GPIOD_OUT_HIGH);
	if (IS_ERR(ctx->reset_gpio))
		return dev_err_probe(dev, PTR_ERR(ctx->reset_gpio),
				     "failed to get reset gpio\n");

	/* Samsung's samsung,tcon-rdy-gpio; optional, boards that lack it skip the wait. */
	ctx->tcon_rdy_gpio = devm_gpiod_get_optional(dev, "tcon-rdy", GPIOD_IN);
	if (IS_ERR(ctx->tcon_rdy_gpio))
		return dev_err_probe(dev, PTR_ERR(ctx->tcon_rdy_gpio),
				     "failed to get TCON_RDY gpio\n");

	ctx->dsi = dsi;
	mipi_dsi_set_drvdata(dsi, ctx);
	mutex_init(&ctx->lock);
	INIT_DELAYED_WORK(&ctx->fod_watchdog, ana38407_fod_watchdog_work);
	ctx->user_brightness = ANA38407_MAX_BRIGHTNESS;
	ctx->vrr = ANA38407_VRR_120HS;
	ctx->glut_row = -1;

	dsi->lanes = 4;
	dsi->format = MIPI_DSI_FMT_RGB888;
	dsi->mode_flags = MIPI_DSI_MODE_LPM;
	continuous_clock = ctx->desc->continuous_clock;
	if (param_continuous_clock >= 0)
		continuous_clock = param_continuous_clock;
	if (!continuous_clock)
		dsi->mode_flags |= MIPI_DSI_CLOCK_NON_CONTINUOUS;
	dev_info(dev, "DSI clock %s between frames%s\n",
		 continuous_clock ? "continuous" : "non-continuous",
		 param_continuous_clock >= 0 ? " (boot parameter)" : "");

	ctx->panel.prepare_prev_first = true;

	ctx->panel.backlight = ana38407_create_backlight(ctx);
	if (IS_ERR(ctx->panel.backlight))
		return dev_err_probe(dev, PTR_ERR(ctx->panel.backlight),
				     "failed to create backlight\n");

	ret = sysfs_create_group(&ctx->panel.backlight->dev.kobj,
				 &ana38407_bl_attr_group);
	if (ret)
		return dev_err_probe(dev, ret,
				     "failed to create fingerprint controls\n");

	drm_panel_add(&ctx->panel);

	ana38407_dsc_config(ctx);
	dsi->dsc = &ctx->dsc;

	ret = mipi_dsi_attach(dsi);
	if (ret < 0) {
		drm_panel_remove(&ctx->panel);
		sysfs_remove_group(&ctx->panel.backlight->dev.kobj,
				   &ana38407_bl_attr_group);
		return dev_err_probe(dev, ret, "failed to attach to DSI host\n");
	}

	return 0;
}

static void ana38407_remove(struct mipi_dsi_device *dsi)
{
	struct ana38407 *ctx = mipi_dsi_get_drvdata(dsi);
	int ret;

	sysfs_remove_group(&ctx->panel.backlight->dev.kobj,
			   &ana38407_bl_attr_group);
	cancel_delayed_work_sync(&ctx->fod_watchdog);
	mutex_lock(&ctx->lock);
	if (ctx->prepared)
		ana38407_fod_cleanup_locked(ctx);
	mutex_unlock(&ctx->lock);

	ret = mipi_dsi_detach(dsi);
	if (ret < 0)
		dev_err(&dsi->dev, "failed to detach from DSI host: %d\n", ret);

	drm_panel_remove(&ctx->panel);
}

static const struct of_device_id ana38407_of_match[] = {
	{ .compatible = "samsung,ana38407-amsa46as02",
	  .data = &ana38407_amsa46as02_desc },
	{ .compatible = "samsung,ana38407-amsa10fa01",
	  .data = &ana38407_amsa10fa01_desc },
	{ }
};
MODULE_DEVICE_TABLE(of, ana38407_of_match);

static struct mipi_dsi_driver ana38407_driver = {
	.probe = ana38407_probe,
	.remove = ana38407_remove,
	.driver = {
		.name = "panel-samsung-ana38407",
		.of_match_table = ana38407_of_match,
	},
};
module_mipi_dsi_driver(ana38407_driver);

MODULE_DESCRIPTION("Samsung ANA38407 AMSA46AS02 (gts9u) DSI panel driver");
MODULE_LICENSE("GPL");
