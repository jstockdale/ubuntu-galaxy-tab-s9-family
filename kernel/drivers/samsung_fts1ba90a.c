// SPDX-License-Identifier: GPL-2.0-only
/*
 * STMicroelectronics FTS1BA90A touchscreen controller as fitted to the
 * Samsung Galaxy Tab S9 family (Samsung "FingerTip S", sec_input generation).
 *
 * Reused from fts1ba90a.c in aaronsb's Galaxy Tab S8+ (SM-X800) mainline port,
 * https://github.com/aaronsb/sm-x800-linux (GPL-2.0-only), which is a
 * mainline-style port of Samsung's GPL-2.0 downstream fts_ts driver
 * (drivers/input/touchscreen/stm/fts1ba90a/{fts_ts.c,fts_ts.h} in Samsung's
 * SM8450/SM8550 kernel releases) with the sec_input factory/command layer
 * removed, and which adopted the ready-wait, chip-ID and pressure fixes from
 * nacht20-de's SM-X710 Fedora port (https://github.com/nacht20-de/gts9wifi-fedora).
 * The changes made for this repository are listed in
 * docs/gts9wifi-touch-fts1ba90a.md.
 *
 * This part is NOT compatible with mainline stmfts.c: it uses 16-byte events
 * read with opcodes 0x60 (one) / 0x61 (all remaining), scan control via 0xA0
 * and an in-band 6-byte system reset, where stmfts speaks an 8-byte-event
 * protocol with a different opcode map.  Binding stmfts to it times out in
 * stmfts_command() waiting for a controller-ready event that never comes.
 *
 * Only firmware already resident in the IC is used; there is no firmware
 * download path here.  Samsung's boot chain leaves the IC flashed and this
 * driver only resets and configures it.
 *
 * Power follows downstream's input open/close model (USE_OPEN_CLOSE in
 * fts_ts.h): the controller scans while the touchscreen has a reader, and
 * when the last reader goes -- GNOME closes the built-in touchscreen while
 * the panel is blanked -- it stops scanning, or, with double tap to wake
 * enabled, enters its low-power gesture mode.  The gesture is reported as
 * KEY_WAKEUP on a separate input device, which the compositor keeps open
 * while the screen is dark, and can wake the system from sleep; without it
 * the rails are cut for system sleep.
 *
 * Copyright (C) Samsung Electronics (downstream protocol and sequencing)
 */

#include <linux/bitmap.h>
#include <linux/bits.h>
#include <linux/delay.h>
#include <linux/i2c.h>
#include <linux/input.h>
#include <linux/input/mt.h>
#include <linux/input/touchscreen.h>
#include <linux/interrupt.h>
#include <linux/math.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/pm.h>
#include <linux/pm_wakeirq.h>
#include <linux/pm_wakeup.h>
#include <linux/power_supply.h>
#include <linux/property.h>
#include <linux/regulator/consumer.h>
#include <linux/samsung_wacom.h>
#include <linux/slab.h>
#include <linux/spinlock.h>
#include <linux/string.h>
#include <linux/sysfs.h>
#include <linux/unaligned.h>
#include <linux/workqueue.h>

#define FTS_EVENT_SIZE			16
#define FTS_FIFO_MAX			32
#define FTS_FINGER_MAX			10
#define FTS_I2C_RETRY_CNT		3

/* 12-bit coordinates: bytes 1/2 carry bits [11:4], byte 3 both low nibbles */
#define FTS_COORD_MAX			4095
/* contact pressure z: byte 6 bits [5:0] (bits [7:6] are the ttype high bits) */
#define FTS_PRESSURE_MAX		63

/* command opcodes (downstream fts_ts.h) */
#define FTS_CMD_FORCE_CALIBRATION	0x13
#define FTS_READ_DEVICE_ID		0x22
#define FTS_READ_PANEL_INFO		0x23
#define FTS_READ_FW_VERSION		0x24
#define FTS_CMD_SET_TOUCH_FUNCTION	0x30
#define FTS_CMD_SET_GET_OPMODE		0x31
#define FTS_CMD_SET_CHARGER_MODE	0x32
#define FTS_CMD_WRITE_WAKEUP_GESTURE	0x39
#define FTS_READ_ONE_EVENT		0x60
#define FTS_READ_ALL_EVENT		0x61
#define FTS_CMD_CLEAR_ALL_EVENT		0x62
#define FTS_CMD_SET_SCAN_MODE		0xa0
#define FTS_CMD_SYSTEM_REQUEST		0xa4
#define FTS_READ_HOST_DATA		0xa6
#define FTS_CMD_SPONGE_READ_WRITE	0xaa
#define FTS_CMD_SPONGE_NOTIFY		0xc0

#define FTS_SCAN_MODE_OFF		0x00
#define FTS_SCAN_MODE_MS_SS		0x01	/* mutual + self scan */

/*
 * FTS_CMD_SET_GET_OPMODE.  The stock node sets stm,lpmode_change_delay = <5>,
 * which makes downstream fts_set_opmode() write the mode, wait 5 ms and read
 * it back, up to ten times.
 */
#define FTS_OPMODE_NORMAL		0x00
#define FTS_OPMODE_LOWPOWER		0x01
#define FTS_OPMODE_SETTLE_US		5000
#define FTS_OPMODE_TRIES		10

/* FTS_CMD_WRITE_WAKEUP_GESTURE argument downstream sends with double tap on */
#define FTS_WAKEUP_GESTURE_DOUBLETAP	0x02

/*
 * The firmware's gesture library ("sponge").  Its mode byte at offset 0
 * selects the low-power gestures; bit 5 is FTS_MODE_DOUBLETAP_WAKEUP.
 * struct fts_sponge_information, read after FTS_CMD_SYSTEM_REQUEST 06 91,
 * is 39 packed bytes: use, version, model number, name length, name[32].
 */
#define FTS_SPONGE_OFFSET_MODE		0x0000
#define FTS_SPONGE_MODE_DOUBLETAP_WAKEUP BIT(5)
#define FTS_SPONGE_INFO_SIZE		39
#define FTS_SPONGE_NAME_MAX		32

/* FTS_CMD_SET_CHARGER_MODE values (downstream fts_vbus_notification()) */
#define FTS_CHARGER_MODE_NORMAL		0x00
#define FTS_CHARGER_MODE_WIRED		0x01

/* chip id bytes 2 and 3 returned by FTS_READ_DEVICE_ID */
#define FTS_ID0				0x39
#define FTS_ID1				0x36

/* FTS_CMD_SET_TOUCH_FUNCTION bits: touch | palm | wet */
#define FTS_TOUCHTYPE_TOUCH		BIT(0)
#define FTS_TOUCHTYPE_PALM		BIT(5)
#define FTS_TOUCHTYPE_WET		BIT(6)
#define FTS_TOUCHTYPE_DEFAULT_ENABLE	(FTS_TOUCHTYPE_TOUCH | \
					 FTS_TOUCHTYPE_PALM | \
					 FTS_TOUCHTYPE_WET)

/* event id, byte 0 bits [1:0] */
#define FTS_EID_COORDINATE		0
#define FTS_EID_STATUS			1
#define FTS_EID_GESTURE			2
#define FTS_EID_VENDOR			3

/* status events (eid == FTS_EID_STATUS): stype byte 0 bits [5:2], id byte 1 */
#define FTS_STATUS_TYPE_ERROR		1
#define FTS_STATUS_TYPE_INFO		2
#define FTS_ERR_EVENT_QUEUE_FULL	0x01
#define FTS_ERR_EVENT_ESD		0x02
#define FTS_INFO_READY_STATUS		0x00

/*
 * gesture events (eid == FTS_EID_GESTURE, struct fts_gesture_status): type
 * byte 0 bits [5:2], sf bits [7:6], gesture id byte 1
 */
#define FTS_GESTURE_SF_SAMSUNG		1
#define FTS_GESTURE_TYPE_DOUBLETAP	1
#define FTS_GESTURE_ID_DOUBLETAP_WAKEUP	1

/* full byte-0 values */
#define FTS_EVENT_STATUS_REPORT		0x43	/* command echo carrier */
#define FTS_EVENT_ERROR_REPORT		0xf3

/* coordinate actions, byte 0 bits [7:6] */
#define FTS_ACTION_NONE			0
#define FTS_ACTION_PRESS		1
#define FTS_ACTION_MOVE			2
#define FTS_ACTION_RELEASE		3

/* touch type: byte 6 bits [7:6] are ttype[3:2], byte 7 bits [7:6] ttype[1:0] */
#define FTS_TTYPE_NORMAL		0
#define FTS_TTYPE_GLOVE			3
#define FTS_TTYPE_PALM			5
#define FTS_TTYPE_WET			6

/* slots in seen_contacts beyond the 16 touch types */
#define FTS_SEEN_NO_ACTION		16
#define FTS_SEEN_LOWPOWER		17
#define FTS_SEEN_CONTACT_BITS		18

#define FTS_POLL_MS			20
#define FTS_READY_POLLS			150	/* 3 s, downstream ceiling */
#define FTS_ECHO_POLLS			250	/* 5 s, downstream ceiling */
#define FTS_INFO_ECHO_POLLS		50	/* 1 s, informational query only */
#define FTS_SCAN_SETTLE_MS		50	/* downstream fts_set_scanmode() */
#define FTS_BOOT_ERROR_LIMIT		32
#define FTS_WAKE_EVENT_MS		1000

enum fts1ba90a_state {
	FTS_STATE_OFF,		/* rails off, IRQ disabled */
	FTS_STATE_SENSE_OFF,	/* rails on, not scanning, IRQ disabled */
	FTS_STATE_ACTIVE,	/* scanning, contacts reported */
	FTS_STATE_LOWPOWER,	/* rails on, low-power scan for the wake gesture */
};

struct fts1ba90a {
	struct i2c_client *client;
	struct input_dev *input;
	struct input_dev *wake_input;	/* KEY_WAKEUP only, see the file header */
	struct touchscreen_properties prop;
	struct regulator *vdd;		/* IO rail, stock tsp_io_ldo (1.8 V) */
	struct regulator *avdd;		/* analog rail, stock tsp_avdd_ldo */
	struct mutex io_lock;		/* serialises i2c transactions */
	/*
	 * Serialises power-state changes and the settings they are computed
	 * from.  Never taken by the IRQ thread: a state change disables the
	 * IRQ, which waits for a running thread, so the thread always sees a
	 * stable state without the lock.
	 */
	struct mutex lock;
	enum fts1ba90a_state state;
	bool powered;
	bool opened;			/* the touchscreen has a reader; lockless */
	bool double_tap;		/* user setting, off by default */
	bool charger;			/* external power present */
	bool irq_wake;			/* enable_irq_wake() done for this sleep */
	/* late suspend to early resume with the gesture armed, see park_irq() */
	spinlock_t irq_lock;
	bool suspended;
	bool irq_parked;
	struct work_struct mode_work;
	struct work_struct charger_work;
	struct notifier_block psy_nb;
	struct input_handler lid_handler;
	bool lid_watch;
	DECLARE_BITMAP(seen_events, 256);
	DECLARE_BITMAP(seen_contacts, FTS_SEEN_CONTACT_BITS);
	u16 max_x;
	u16 max_y;
};

static const char *const fts1ba90a_state_names[] = {
	[FTS_STATE_OFF] = "off",
	[FTS_STATE_SENSE_OFF] = "sense-off",
	[FTS_STATE_ACTIVE] = "active",
	[FTS_STATE_LOWPOWER] = "low-power",
};

static int fts1ba90a_write(struct fts1ba90a *ts, const u8 *buf, int len)
{
	struct i2c_msg msg = {
		.addr = ts->client->addr,
		.len = len,
		.buf = (u8 *)buf,
	};
	int retry = FTS_I2C_RETRY_CNT;
	int ret;

	mutex_lock(&ts->io_lock);
	do {
		ret = i2c_transfer(ts->client->adapter, &msg, 1);
		if (ret == 1)
			break;
		usleep_range(10000, 11000);
	} while (--retry > 0);
	mutex_unlock(&ts->io_lock);

	if (ret != 1) {
		dev_err(&ts->client->dev, "write [%02x..] failed: %d\n",
			buf[0], ret);
		return ret < 0 ? ret : -EIO;
	}
	return 0;
}

/*
 * Every read is a command write followed by a repeated-start read of the
 * reply (downstream fts_read_reg()).
 */
static int fts1ba90a_read(struct fts1ba90a *ts, const u8 *cmd, int cmd_len,
			  u8 *buf, int len)
{
	struct i2c_msg msgs[2] = {
		{
			.addr = ts->client->addr,
			.len = cmd_len,
			.buf = (u8 *)cmd,
		}, {
			.addr = ts->client->addr,
			.flags = I2C_M_RD,
			.len = len,
			.buf = buf,
		},
	};
	int retry = FTS_I2C_RETRY_CNT;
	int ret;

	mutex_lock(&ts->io_lock);
	do {
		ret = i2c_transfer(ts->client->adapter, msgs, 2);
		if (ret == 2)
			break;
		usleep_range(10000, 11000);
	} while (--retry > 0);
	mutex_unlock(&ts->io_lock);

	if (ret != 2) {
		dev_err(&ts->client->dev, "read [%02x..] failed: %d\n",
			cmd[0], ret);
		return ret < 0 ? ret : -EIO;
	}
	return 0;
}

static int fts1ba90a_read_one_event(struct fts1ba90a *ts, u8 *ev)
{
	static const u8 cmd = FTS_READ_ONE_EVENT;

	return fts1ba90a_read(ts, &cmd, 1, ev, FTS_EVENT_SIZE);
}

static bool fts1ba90a_is_ready_event(const u8 *ev)
{
	return (ev[0] & 0x3) == FTS_EID_STATUS &&
	       ((ev[0] >> 2) & 0xf) == FTS_STATUS_TYPE_INFO &&
	       ev[1] == FTS_INFO_READY_STATUS;
}

/*
 * Poll the one-event FIFO until the firmware posts its "ready" status
 * event (stype INFO, id READY), which it does after power-on and after a
 * system reset.  The downstream driver allows 150 polls at 20 ms.
 */
static int fts1ba90a_wait_for_ready(struct fts1ba90a *ts)
{
	u8 ev[FTS_EVENT_SIZE];
	int err_cnt = 0;
	int retry = 0;
	int ret;

	for (;;) {
		ret = fts1ba90a_read_one_event(ts, ev);
		if (ret < 0)
			return ret;

		if (fts1ba90a_is_ready_event(ev))
			return 0;

		if (ev[0] == FTS_EVENT_ERROR_REPORT) {
			/*
			 * 0x20-0x21 / 0xA0-0xA8 flag config/cx corruption,
			 * 0x24 etc. a broken OSC trim.  Without the factory
			 * tooling there is no recovery path here, so just
			 * name the failure.
			 */
			dev_err(&ts->client->dev, "boot error event: %*ph\n",
				8, ev);
			if (err_cnt++ > FTS_BOOT_ERROR_LIMIT)
				return -EIO;
			continue;
		}

		if (retry++ > FTS_READY_POLLS) {
			dev_err(&ts->client->dev,
				"timed out waiting for ready: %*ph\n", 8, ev);
			return -ETIMEDOUT;
		}
		msleep(FTS_POLL_MS);
	}
}

/*
 * Write a command and wait for the firmware to echo it back as a vendor
 * event (0x43 0x01 <cmd bytes>).  Commands with side effects (calibration,
 * FIFO clear, scan-mode change) complete asynchronously and signal via this
 * echo (downstream fts_command(..., true) / fts_fw_wait_for_echo_event()).
 */
static int fts1ba90a_write_wait_echo(struct fts1ba90a *ts, const u8 *cmd,
				     int cmd_len, int polls)
{
	u8 ev[FTS_EVENT_SIZE];
	int cmp = min(cmd_len, 4);
	int retry = 0;
	int ret;

	ret = fts1ba90a_write(ts, cmd, cmd_len);
	if (ret < 0)
		return ret;

	for (;;) {
		ret = fts1ba90a_read_one_event(ts, ev);
		if (ret < 0)
			return ret;

		if (ev[0] == FTS_EVENT_STATUS_REPORT && ev[1] == 0x01 &&
		    !memcmp(&ev[2], cmd, cmp))
			return 0;

		if (retry++ > polls) {
			dev_warn(&ts->client->dev,
				 "no echo for command %02x: %*ph\n",
				 cmd[0], 8, ev);
			return -ETIMEDOUT;
		}
		msleep(FTS_POLL_MS);
	}
}

/*
 * In-band system reset.  There is no reset GPIO on these boards (the stock
 * DTS node has none); reset is this 6-byte memory-mapped register write
 * (downstream fts_systemreset()), followed by the firmware CRC status word.
 */
static int fts1ba90a_system_reset(struct fts1ba90a *ts)
{
	static const u8 reset_cmd[6] = { 0xfa, 0x20, 0x00, 0x00, 0x24, 0x81 };
	static const u8 crc_cmd[5] = { 0xfa, 0x20, 0x00, 0x00, 0x78 };
	u8 crc = 0;
	int ret;

	ret = fts1ba90a_write(ts, reset_cmd, sizeof(reset_cmd));
	if (ret < 0)
		return ret;

	usleep_range(10000, 11000);

	/* firmware CRC status; non-fatal, but worth a line if it is bad */
	ret = fts1ba90a_read(ts, crc_cmd, sizeof(crc_cmd), &crc, 1);
	if (ret == 0 && (crc & 0x03))
		dev_warn(&ts->client->dev,
			 "firmware CRC error reported: %02x\n", crc);

	return fts1ba90a_wait_for_ready(ts);
}

/*
 * Read and log the device ID, firmware versions and panel geometry.  The
 * downstream driver logs an unexpected chip ID and carries on; so does this
 * driver, because the IC at this address was already confirmed to be the
 * STM part by Samsung's own kernel binding to it, and a hard failure here
 * would only turn an unknown ID revision into a dead touchscreen.
 */
static int fts1ba90a_read_ids(struct fts1ba90a *ts)
{
	struct device *dev = &ts->client->dev;
	u8 cmd = FTS_READ_DEVICE_ID;
	u8 id[5] = { 0 };
	u8 ver[9] = { 0 };
	u8 panel[11] = { 0 };
	int ret;

	ret = fts1ba90a_read(ts, &cmd, 1, id, sizeof(id));
	if (ret < 0)
		return ret;

	if (id[2] != FTS_ID0 || id[3] != FTS_ID1)
		dev_warn(dev, "unexpected chip id %*ph (expected .. %02x %02x ..)\n",
			 (int)sizeof(id), id, FTS_ID0, FTS_ID1);

	cmd = FTS_READ_FW_VERSION;
	ret = fts1ba90a_read(ts, &cmd, 1, ver, sizeof(ver));
	if (ret < 0)
		return ret;

	dev_info(dev, "device id %*ph, fw 0x%04x, config 0x%04x, main 0x%04x\n",
		 (int)sizeof(id), id, get_unaligned_be16(&ver[0]),
		 get_unaligned_be16(&ver[2]), get_unaligned_le16(&ver[4]));

	cmd = FTS_READ_PANEL_INFO;
	ret = fts1ba90a_read(ts, &cmd, 1, panel, sizeof(panel));
	if (ret < 0) {
		dev_warn(dev, "panel info unavailable (%d), using %u x %u\n",
			 ret, FTS_COORD_MAX + 1, FTS_COORD_MAX + 1);
		ts->max_x = FTS_COORD_MAX;
		ts->max_y = FTS_COORD_MAX;
		return 0;
	}

	ts->max_x = get_unaligned_be16(&panel[0]);
	ts->max_y = get_unaligned_be16(&panel[2]);
	if (!ts->max_x || !ts->max_y) {
		ts->max_x = FTS_COORD_MAX;
		ts->max_y = FTS_COORD_MAX;
	}

	dev_info(dev, "panel %u x %u, tx/rx channels %u/%u\n",
		 ts->max_x + 1, ts->max_y + 1, panel[8], panel[9]);

	return 0;
}

/*
 * Name the firmware's gesture library, as downstream
 * fts_check_custom_library() does at probe: request the information block
 * (echoed) and read it from the host data window.  Informational only: a
 * firmware without the library simply never reports the wake gesture.
 */
static void fts1ba90a_log_gesture_library(struct fts1ba90a *ts)
{
	static const u8 request[3] = { FTS_CMD_SYSTEM_REQUEST, 0x06, 0x91 };
	static const u8 read_cmd[3] = { FTS_READ_HOST_DATA, 0x00, 0x00 };
	struct device *dev = &ts->client->dev;
	u8 info[FTS_SPONGE_INFO_SIZE] = { 0 };
	const char *name = (const char *)&info[7];
	int len, ret;

	ret = fts1ba90a_write_wait_echo(ts, request, sizeof(request),
					FTS_INFO_ECHO_POLLS);
	if (ret < 0) {
		dev_info(dev, "gesture library query not answered: %d\n", ret);
		return;
	}

	ret = fts1ba90a_read(ts, read_cmd, sizeof(read_cmd), info, sizeof(info));
	if (ret < 0)
		return;

	len = min_t(int, get_unaligned_le16(&info[5]), FTS_SPONGE_NAME_MAX);
	len = strnlen(name, len);
	dev_info(dev, "gesture library: use %u, version %u, model %u %*pE\n",
		 info[0], get_unaligned_le16(&info[1]),
		 get_unaligned_le16(&info[3]), len, name);
}

/*
 * The gesture library takes its mode byte at offset 0: write it, then commit
 * it with the notify command (downstream fts_write_to_sponge()).  Downstream
 * keeps it equal to its low-power flags at all times and rewrites it after
 * every start and whenever the setting changes, not only when the gesture is
 * armed; so does this driver.
 */
static int fts1ba90a_write_sponge_mode(struct fts1ba90a *ts)
{
	u8 cmd[4] = {
		FTS_CMD_SPONGE_READ_WRITE,
		FTS_SPONGE_OFFSET_MODE >> 8,
		FTS_SPONGE_OFFSET_MODE & 0xff,
		READ_ONCE(ts->double_tap) ? FTS_SPONGE_MODE_DOUBLETAP_WAKEUP : 0,
	};
	int ret;

	ret = fts1ba90a_write(ts, cmd, sizeof(cmd));
	if (ret < 0)
		return ret;

	cmd[0] = FTS_CMD_SPONGE_NOTIFY;
	return fts1ba90a_write(ts, cmd, 3);
}

/*
 * Charger mode hardens the scan against charger noise.  Downstream sets it
 * from the VBUS notifier while the controller runs and again after every
 * start; the X710 sits on USB power most of the time.
 */
static int fts1ba90a_write_charger_mode(struct fts1ba90a *ts)
{
	u8 cmd[2] = {
		FTS_CMD_SET_CHARGER_MODE,
		READ_ONCE(ts->charger) ? FTS_CHARGER_MODE_WIRED :
					 FTS_CHARGER_MODE_NORMAL,
	};

	return fts1ba90a_write(ts, cmd, sizeof(cmd));
}

/*
 * Downstream fts_set_opmode() with the stock 5 ms mode-change delay: write
 * the mode, let it latch, read it back, up to ten times.  With double tap on,
 * downstream follows every mode change with the wake gesture command, in
 * normal mode too, and so does this.  A read-back that never matches is
 * returned as -EAGAIN; downstream logs it and carries on.
 */
static int fts1ba90a_set_opmode(struct fts1ba90a *ts, u8 mode)
{
	static const u8 gesture[2] = { FTS_CMD_WRITE_WAKEUP_GESTURE,
				       FTS_WAKEUP_GESTURE_DOUBLETAP };
	u8 cmd[2] = { FTS_CMD_SET_GET_OPMODE, mode };
	u8 readback = 0;
	int tries, ret = 0;

	for (tries = 0; tries < FTS_OPMODE_TRIES; tries++) {
		ret = fts1ba90a_write(ts, cmd, sizeof(cmd));
		if (ret < 0)
			return ret;

		usleep_range(FTS_OPMODE_SETTLE_US, FTS_OPMODE_SETTLE_US + 500);

		ret = fts1ba90a_read(ts, cmd, 1, &readback, 1);
		if (ret == 0 && readback == mode)
			break;
	}

	if (tries == FTS_OPMODE_TRIES) {
		if (ret < 0)
			return ret;
		dev_warn(&ts->client->dev,
			 "operating mode %u not confirmed, read back %u\n",
			 mode, readback);
		ret = -EAGAIN;
	}

	if (READ_ONCE(ts->double_tap)) {
		int err = fts1ba90a_write(ts, gesture, sizeof(gesture));

		if (err < 0)
			return err;
	}

	return ret;
}

/*
 * Post-reset configuration (downstream fts_init()/fts_reinit()): enable the
 * default touch types, optionally force a calibration, clear the event FIFO
 * and start mutual/self scanning ("sense on" for this generation).
 * Calibration is only run on probe, mirroring downstream fts_init vs
 * fts_reinit.  A missing command echo is logged but not fatal: scanning is
 * still enabled by the write itself.  A reset also returns the gesture block
 * and charger mode to their defaults, so both are reapplied the way
 * downstream fts_start_device() does after its reinit; failing those only
 * costs the feature.
 */
static int fts1ba90a_configure(struct fts1ba90a *ts, bool calibrate)
{
	static const u8 touch_fn[3] = { FTS_CMD_SET_TOUCH_FUNCTION,
					FTS_TOUCHTYPE_DEFAULT_ENABLE, 0x00 };
	static const u8 cal_cmd[1] = { FTS_CMD_FORCE_CALIBRATION };
	static const u8 clear_cmd[1] = { FTS_CMD_CLEAR_ALL_EVENT };
	static const u8 scan_cmd[3] = { FTS_CMD_SET_SCAN_MODE, 0x00,
					FTS_SCAN_MODE_MS_SS };
	struct device *dev = &ts->client->dev;
	int ret;

	ret = fts1ba90a_write(ts, touch_fn, sizeof(touch_fn));
	if (ret < 0)
		return ret;

	usleep_range(10000, 11000);

	if (calibrate) {
		ret = fts1ba90a_write_wait_echo(ts, cal_cmd, sizeof(cal_cmd),
						FTS_ECHO_POLLS);
		if (ret < 0 && ret != -ETIMEDOUT)
			return ret;
	}

	ret = fts1ba90a_write_wait_echo(ts, clear_cmd, sizeof(clear_cmd),
					FTS_ECHO_POLLS);
	if (ret < 0 && ret != -ETIMEDOUT)
		return ret;

	ret = fts1ba90a_write_wait_echo(ts, scan_cmd, sizeof(scan_cmd),
					FTS_ECHO_POLLS);
	if (ret < 0 && ret != -ETIMEDOUT)
		return ret;

	ret = fts1ba90a_write_sponge_mode(ts);
	if (ret < 0)
		dev_warn(dev, "failed to set the gesture mode: %d\n", ret);

	if (READ_ONCE(ts->charger)) {
		ret = fts1ba90a_write_charger_mode(ts);
		if (ret < 0)
			dev_warn(dev, "failed to set charger mode: %d\n", ret);
	}

	return 0;
}

/*
 * Sense off: the controller stays powered and configured but stops scanning
 * (downstream fts_set_scanmode(FTS_SCAN_MODE_SCAN_OFF), its flip-cover
 * state).  This is the screen-off state without the wake gesture, used where
 * downstream's fts_stop_device() powers off, because the IO rail (L12B) is
 * also the panel's VDDIO here: udev's input_id opens and closes every event
 * node as it appears, so a power-off on close would cut L12B a few seconds
 * into boot while the panel's other rails are still on from the bootloader,
 * and at every blank it would change the panel's rail sequence.  The rails
 * therefore only go down for system sleep, as before.  Like downstream, the
 * write is not waited on but given 50 ms to settle; its echo stays in the
 * FIFO, which is cleared when scanning resumes.
 */
static int fts1ba90a_scan_off(struct fts1ba90a *ts)
{
	static const u8 cmd[3] = { FTS_CMD_SET_SCAN_MODE, 0x00,
				   FTS_SCAN_MODE_OFF };
	int ret;

	ret = fts1ba90a_write(ts, cmd, sizeof(cmd));
	if (ret < 0)
		return ret;

	msleep(FTS_SCAN_SETTLE_MS);
	return 0;
}

/*
 * Downstream fts_stop_device() with low power: operating mode low power
 * (plus the wake gesture command), then the gesture block.  The controller
 * keeps both rails, scans slowly and raises its attention line for the armed
 * gesture.  Its flip-cover branch (scan off under a closed cover) is the
 * sense-off state here, which is used whenever a lid is closed.
 */
static int fts1ba90a_enter_lowpower(struct fts1ba90a *ts)
{
	int ret;

	ret = fts1ba90a_set_opmode(ts, FTS_OPMODE_LOWPOWER);
	if (ret < 0 && ret != -EAGAIN)
		return ret;

	return fts1ba90a_write_sponge_mode(ts);
}

/*
 * Downstream fts_start_device() from low power: normal operating mode, then
 * the gesture block and charger mode again.  Downstream reinitialises only
 * on an i2c error; here an unconfirmed mode change fails too, so that the
 * caller restarts the controller instead of leaving the screen without touch.
 */
static int fts1ba90a_exit_lowpower(struct fts1ba90a *ts)
{
	int ret;

	ret = fts1ba90a_set_opmode(ts, FTS_OPMODE_NORMAL);
	if (ret < 0)
		return ret;

	ret = fts1ba90a_write_sponge_mode(ts);
	if (ret < 0)
		return ret;

	return fts1ba90a_write_charger_mode(ts);
}

/*
 * Rail order and delays follow Samsung's sec_input_power(): the IO rail
 * comes up first, the analog rail about 1 ms later; power-off is the
 * reverse with about 4 ms between the rails.
 */
static int fts1ba90a_power_on(struct fts1ba90a *ts)
{
	int ret;

	if (ts->powered)
		return 0;

	ret = regulator_enable(ts->vdd);
	if (ret)
		return ret;
	usleep_range(1000, 1500);
	ret = regulator_enable(ts->avdd);
	if (ret) {
		regulator_disable(ts->vdd);
		return ret;
	}
	usleep_range(10000, 11000);

	ts->powered = true;
	return 0;
}

static void fts1ba90a_power_off(struct fts1ba90a *ts)
{
	if (!ts->powered)
		return;

	ts->powered = false;
	regulator_disable(ts->avdd);
	usleep_range(4000, 5000);
	regulator_disable(ts->vdd);
}

static void fts1ba90a_power_off_action(void *data)
{
	fts1ba90a_power_off(data);
}

static void fts1ba90a_release_slots(struct fts1ba90a *ts)
{
	int i;

	for (i = 0; i < FTS_FINGER_MAX; i++) {
		input_mt_slot(ts->input, i);
		input_mt_report_slot_inactive(ts->input);
	}
}

static void fts1ba90a_release_all(struct fts1ba90a *ts)
{
	fts1ba90a_release_slots(ts);
	input_mt_sync_frame(ts->input);
	input_sync(ts->input);
}

/*
 * Bring the controller from power-off to scanning (downstream fts_init() on
 * probe, fts_reinit() on resume): power on, wait for the boot "ready" event
 * and fall back to an in-band system reset if it does not show up, then
 * configure.  On failure the rails are left off.
 */
static int fts1ba90a_start(struct fts1ba90a *ts, bool first)
{
	struct device *dev = &ts->client->dev;
	int ret;

	ret = fts1ba90a_power_on(ts);
	if (ret) {
		dev_err(dev, "failed to power on: %d\n", ret);
		return ret;
	}

	ret = fts1ba90a_wait_for_ready(ts);
	if (ret < 0) {
		dev_warn(dev, "no ready event after power-on (%d), resetting\n",
			 ret);
		ret = fts1ba90a_system_reset(ts);
		if (ret < 0) {
			dev_err(dev, "controller never became ready: %d\n", ret);
			goto err_power_off;
		}
	}

	if (first) {
		ret = fts1ba90a_read_ids(ts);
		if (ret < 0)
			goto err_power_off;
	}

	ret = fts1ba90a_configure(ts, first);
	if (ret < 0) {
		dev_err(dev, "failed to start scanning: %d\n", ret);
		goto err_power_off;
	}

	return 0;

err_power_off:
	fts1ba90a_power_off(ts);
	return ret;
}

/* the attention IRQ is enabled exactly in these states */
static bool fts1ba90a_irq_wanted(enum fts1ba90a_state state)
{
	return state == FTS_STATE_ACTIVE || state == FTS_STATE_LOWPOWER;
}

/*
 * Every powered target is reached through a scanning controller: back to
 * normal scanning first (or a full start from off), then on to low power or
 * sense off.  A step that fails powers the controller off, so a later
 * open, close or resume starts it from scratch.
 */
static int fts1ba90a_change_state(struct fts1ba90a *ts,
				  enum fts1ba90a_state target)
{
	struct device *dev = &ts->client->dev;
	int ret = 0;

	if (target == FTS_STATE_OFF) {
		fts1ba90a_release_all(ts);
		fts1ba90a_power_off(ts);
		WRITE_ONCE(ts->state, FTS_STATE_OFF);
		return 0;
	}

	if (ts->state == FTS_STATE_LOWPOWER)
		ret = fts1ba90a_exit_lowpower(ts);
	else if (ts->state == FTS_STATE_SENSE_OFF)
		ret = fts1ba90a_configure(ts, false);

	if (ret < 0) {
		dev_warn(dev, "resuming the scan failed (%d), restarting\n", ret);
		fts1ba90a_power_off(ts);
		WRITE_ONCE(ts->state, FTS_STATE_OFF);
	} else if (ts->state != FTS_STATE_OFF) {
		WRITE_ONCE(ts->state, FTS_STATE_ACTIVE);
	}

	if (ts->state == FTS_STATE_OFF) {
		ret = fts1ba90a_start(ts, false);
		if (ret < 0) {
			dev_err(dev, "failed to start: %d\n", ret);
			return ret;
		}
		WRITE_ONCE(ts->state, FTS_STATE_ACTIVE);
	}

	if (target == FTS_STATE_ACTIVE)
		return 0;

	fts1ba90a_release_all(ts);

	if (target == FTS_STATE_LOWPOWER)
		ret = fts1ba90a_enter_lowpower(ts);
	else
		ret = fts1ba90a_scan_off(ts);

	if (ret < 0) {
		dev_warn(dev, "failed to enter %s: %d\n",
			 fts1ba90a_state_names[target], ret);
		fts1ba90a_power_off(ts);
		WRITE_ONCE(ts->state, FTS_STATE_OFF);
		return ret;
	}

	WRITE_ONCE(ts->state, target);
	return 0;
}

/*
 * Move the controller to @target.  The IRQ is disabled for the duration of
 * a change, which also waits for a running IRQ thread, and is enabled again
 * if the state reached needs it.
 */
static int fts1ba90a_set_state(struct fts1ba90a *ts,
			       enum fts1ba90a_state target)
{
	int ret;

	lockdep_assert_held(&ts->lock);

	if (ts->state == target)
		return 0;

	if (fts1ba90a_irq_wanted(ts->state))
		disable_irq(ts->client->irq);

	ret = fts1ba90a_change_state(ts, target);

	if (fts1ba90a_irq_wanted(ts->state))
		enable_irq(ts->client->irq);

	return ret;
}

static int fts1ba90a_lid_is_closed(struct input_handle *handle, void *data)
{
	return test_bit(SW_LID, handle->dev->sw);
}

/*
 * Any lid switch reporting closed counts, as for logind.  The switch state is
 * read directly, so it is right even for a device that another reader has
 * grabbed (Tab Companion grabs the keyboard cover); only the change events of
 * such a device are not seen.
 */
static bool fts1ba90a_lid_closed(struct fts1ba90a *ts)
{
	return READ_ONCE(ts->lid_watch) &&
	       input_handler_for_each_handle(&ts->lid_handler, NULL,
					     fts1ba90a_lid_is_closed);
}

/*
 * Scanning while someone reads the touchscreen; with no reader, the wake
 * gesture if it is enabled and no cover is closed over the glass (downstream
 * turns scanning off under a closed flip cover), otherwise sense off.
 */
static enum fts1ba90a_state fts1ba90a_target_state(struct fts1ba90a *ts)
{
	if (READ_ONCE(ts->opened))
		return FTS_STATE_ACTIVE;

	if (ts->double_tap && !fts1ba90a_lid_closed(ts))
		return FTS_STATE_LOWPOWER;

	return FTS_STATE_SENSE_OFF;
}

static void fts1ba90a_mode_work(struct work_struct *work)
{
	struct fts1ba90a *ts = container_of(work, struct fts1ba90a, mode_work);

	mutex_lock(&ts->lock);
	fts1ba90a_set_state(ts, fts1ba90a_target_state(ts));
	mutex_unlock(&ts->lock);
}

/*
 * Mode changes run from a freezable workqueue: off the caller's path (the
 * opener of a touchscreen is logind; a start takes 50-125 ms, seconds if the
 * controller has to be reset), and never between the system-sleep
 * callbacks.  Suspend makes its own change; after resume the work restores
 * the state once tasks are thawed.
 */
static void fts1ba90a_queue_mode(struct fts1ba90a *ts)
{
	queue_work(system_freezable_wq, &ts->mode_work);
}

/*
 * Events this driver has no use for are logged the first time each kind
 * (byte 0: event id, type and flags) shows up, so that an unexpected event
 * stream can be told apart from the kernel log alone; the image's lockdown
 * mode refuses dynamic debug.
 */
static void fts1ba90a_note_event(struct fts1ba90a *ts, const u8 *ev)
{
	if (!test_and_set_bit(ev[0], ts->seen_events))
		dev_info(&ts->client->dev,
			 "unhandled event %*ph (logged once per kind)\n", 8, ev);
}

/* Contacts are named by kind only: their coordinates are never logged. */
static void fts1ba90a_note_contact(struct fts1ba90a *ts, const u8 *ev,
				   unsigned int kind, u8 ttype)
{
	if (!test_and_set_bit(kind, ts->seen_contacts))
		dev_info(&ts->client->dev,
			 "unreported contact event %02x, touch type %u, %s (logged once per kind)\n",
			 ev[0], ttype,
			 fts1ba90a_state_names[READ_ONCE(ts->state)]);
}

/*
 * struct fts_event_coordinate (downstream fts_ts.h), 16 bytes, unpacked by
 * hand rather than as a bitfield struct:
 *
 *   byte0: eid[1:0] tid[5:2] tchsta[7:6]
 *   byte1: x[11:4]
 *   byte2: y[11:4]
 *   byte3: y[3:0] in bits [3:0], x[3:0] in bits [7:4]
 *   byte4: major
 *   byte5: minor
 *   byte6: z[5:0] ttype[3:2] in bits [7:6]
 *   byte7: left_event[5:0] ttype[1:0] in bits [7:6]
 */
static void fts1ba90a_handle_coordinate(struct fts1ba90a *ts, const u8 *ev)
{
	u8 tid = (ev[0] >> 2) & 0xf;
	u8 action = ev[0] >> 6;
	u8 ttype = ((ev[6] >> 6) << 2) | (ev[7] >> 6);
	u8 z = ev[6] & 0x3f;
	u16 x = (ev[1] << 4) | (ev[3] >> 4);
	u16 y = (ev[2] << 4) | (ev[3] & 0xf);
	unsigned int tool;

	if (tid >= FTS_FINGER_MAX)
		return;

	switch (ttype) {
	case FTS_TTYPE_NORMAL:
	case FTS_TTYPE_GLOVE:
	case FTS_TTYPE_WET:
		tool = MT_TOOL_FINGER;
		break;
	case FTS_TTYPE_PALM:
		/*
		 * The firmware classifies large contacts as palms.  Report
		 * them with the palm tool type so libinput drops them instead
		 * of moving the pointer under a resting hand.
		 */
		tool = MT_TOOL_PALM;
		break;
	default:
		/* hover, flip cover, stylus, proximity and jig: not reported */
		fts1ba90a_note_contact(ts, ev, ttype, ttype);
		return;
	}

	switch (action) {
	case FTS_ACTION_PRESS:
	case FTS_ACTION_MOVE:
		input_mt_slot(ts->input, tid);
		input_mt_report_slot_state(ts->input, tool, true);
		touchscreen_report_pos(ts->input, &ts->prop, x, y, true);
		input_report_abs(ts->input, ABS_MT_TOUCH_MAJOR, ev[4]);
		input_report_abs(ts->input, ABS_MT_TOUCH_MINOR, ev[5]);
		/*
		 * A held contact can report z == 0; keep it non-zero so
		 * userspace that treats pressure 0 as a lift does not drop
		 * the finger (same clamp as downstream and the S9 port).
		 */
		input_report_abs(ts->input, ABS_MT_PRESSURE, z ? z : 1);
		break;
	case FTS_ACTION_RELEASE:
		input_mt_slot(ts->input, tid);
		input_mt_report_slot_inactive(ts->input);
		break;
	default:
		/* an empty event reads back as all zeroes */
		fts1ba90a_note_contact(ts, ev, FTS_SEEN_NO_ACTION, ttype);
		break;
	}
}

/*
 * Status events.  Errors are named; a "ready" event at runtime means the
 * firmware reset itself (ESD, brown-out) and is sitting idle, so the scan
 * configuration -- and low-power mode, if that is where the controller was
 * -- is reapplied the way downstream fts_reinit()/fts_reset_work() would.
 */
static void fts1ba90a_handle_status(struct fts1ba90a *ts, const u8 *ev)
{
	struct device *dev = &ts->client->dev;
	u8 stype = (ev[0] >> 2) & 0xf;
	int ret;

	if (stype == FTS_STATUS_TYPE_INFO && ev[1] == FTS_INFO_READY_STATUS) {
		dev_warn(dev, "controller reset itself, reconfiguring\n");
		fts1ba90a_release_all(ts);
		ret = fts1ba90a_configure(ts, false);
		if (!ret && READ_ONCE(ts->state) == FTS_STATE_LOWPOWER)
			ret = fts1ba90a_enter_lowpower(ts);
		if (ret < 0)
			dev_err(dev, "reconfiguration failed: %d\n", ret);
		return;
	}

	if (stype == FTS_STATUS_TYPE_ERROR) {
		switch (ev[1]) {
		case FTS_ERR_EVENT_QUEUE_FULL:
			dev_err(dev, "event queue full, releasing all contacts\n");
			fts1ba90a_release_all(ts);
			return;
		case FTS_ERR_EVENT_ESD:
			dev_err(dev, "ESD error reported\n");
			return;
		}
	}

	fts1ba90a_note_event(ts, ev);
}

static void fts1ba90a_report_wake(struct fts1ba90a *ts)
{
	/*
	 * Account the wakeup and abort a suspend that is already under way,
	 * so that the key is read by a running system rather than left queued
	 * across a sleep.  It has to be a hard event: a soft one only stops a
	 * suspend when userspace uses /sys/power/wakeup_count, and
	 * systemd-sleep does not.
	 */
	pm_wakeup_dev_event(&ts->client->dev, FTS_WAKE_EVENT_MS, true);

	input_report_key(ts->wake_input, KEY_WAKEUP, 1);
	input_sync(ts->wake_input);
	input_report_key(ts->wake_input, KEY_WAKEUP, 0);
	input_sync(ts->wake_input);

	dev_info(&ts->client->dev, "double tap to wake\n");
}

/*
 * struct fts_gesture_status (downstream fts_ts.h).  Only the Samsung-feature
 * double tap with the wake id is "double tap to turn on screen"
 * (downstream fts_event_handler_type_b()); the library's other gestures
 * (swipe, single tap, AOD and fingerprint presses) are never armed here.
 */
static void fts1ba90a_handle_gesture(struct fts1ba90a *ts, const u8 *ev)
{
	if (ev[0] >> 6 == FTS_GESTURE_SF_SAMSUNG &&
	    ((ev[0] >> 2) & 0xf) == FTS_GESTURE_TYPE_DOUBLETAP &&
	    ev[1] == FTS_GESTURE_ID_DOUBLETAP_WAKEUP) {
		if (READ_ONCE(ts->double_tap))
			fts1ba90a_report_wake(ts);
		return;
	}

	fts1ba90a_note_event(ts, ev);
}

static void fts1ba90a_handle_vendor(struct fts1ba90a *ts, const u8 *ev)
{
	/* the echo of a command nothing is waiting for any more */
	if (ev[0] == FTS_EVENT_STATUS_REPORT && ev[1] == 0x01) {
		dev_dbg(&ts->client->dev, "late echo: %*ph\n", 8, ev);
		return;
	}

	fts1ba90a_note_event(ts, ev);
}

/*
 * With the gesture armed, the I2C controller is unusable from its late
 * suspend until its early resume: GENI's runtime PM is disabled for that
 * window and every transfer fails with -EACCES, while the level IRQ fires
 * again at once (the failure ygecesefa's gts9wifi patch 0006 describes, about
 * 130 errors per wake).  An IRQ thread that runs in the window parks the line
 * instead and reports a hard wakeup event, which aborts a suspend that is
 * still in progress; resume_early re-enables the line once the bus works.
 * The event stays in the controller's FIFO and keeps the line low.
 * Downstream's fts_handler_wait_resume_work() does the same with a work
 * item.
 */
static bool fts1ba90a_park_irq(struct fts1ba90a *ts, int irq)
{
	bool parked;

	spin_lock(&ts->irq_lock);
	parked = ts->suspended;
	if (parked && !ts->irq_parked) {
		ts->irq_parked = true;
		disable_irq_nosync(irq);
	}
	spin_unlock(&ts->irq_lock);

	if (parked)
		pm_wakeup_hard_event(&ts->client->dev);

	return parked;
}

static void fts1ba90a_unpark_irq(struct fts1ba90a *ts)
{
	bool parked;

	spin_lock(&ts->irq_lock);
	ts->suspended = false;
	parked = ts->irq_parked;
	ts->irq_parked = false;
	spin_unlock(&ts->irq_lock);

	if (parked)
		enable_irq(ts->client->irq);
}

/*
 * One FTS_READ_ONE_EVENT read always happens first (downstream
 * fts_event_handler_type_b()); byte 7 bits [5:0] of every event say how many
 * MORE events are queued, which are then burst-read in a single
 * FTS_READ_ALL_EVENT transaction.
 */
static irqreturn_t fts1ba90a_irq_handler(int irq, void *dev_id)
{
	struct fts1ba90a *ts = dev_id;
	u8 data[FTS_FIFO_MAX * FTS_EVENT_SIZE];
	u8 cmd = FTS_READ_ALL_EVENT;
	bool active, suppress;
	int left, i;

	if (fts1ba90a_park_irq(ts, irq))
		return IRQ_HANDLED;

	if (fts1ba90a_read_one_event(ts, data) < 0)
		return IRQ_HANDLED;

	left = data[7] & 0x3f;
	if (left >= FTS_FIFO_MAX)
		left = FTS_FIFO_MAX - 1;

	if (left > 0) {
		if (fts1ba90a_read(ts, &cmd, 1, &data[FTS_EVENT_SIZE],
				   left * FTS_EVENT_SIZE) < 0)
			left = 0;
	}

	active = READ_ONCE(ts->state) == FTS_STATE_ACTIVE;
	/*
	 * While the S Pen is in range, a hand resting on the glass next to it
	 * is not input: report no fingers, and release the ones that were down
	 * when the pen arrived (the contract of the Ultra's Goodix path).
	 */
	suppress = active && samsung_wacom_should_suppress_touch();

	for (i = 0; i <= left; i++) {
		const u8 *ev = &data[i * FTS_EVENT_SIZE];

		switch (ev[0] & 0x3) {
		case FTS_EID_COORDINATE:
			if (!active)
				fts1ba90a_note_contact(ts, ev, FTS_SEEN_LOWPOWER,
						       ((ev[6] >> 6) << 2) |
						       (ev[7] >> 6));
			else if (!suppress)
				fts1ba90a_handle_coordinate(ts, ev);
			break;
		case FTS_EID_STATUS:
			fts1ba90a_handle_status(ts, ev);
			break;
		case FTS_EID_GESTURE:
			fts1ba90a_handle_gesture(ts, ev);
			break;
		default:
			fts1ba90a_handle_vendor(ts, ev);
			break;
		}
	}

	if (active) {
		if (suppress)
			fts1ba90a_release_slots(ts);
		input_mt_sync_frame(ts->input);
		input_sync(ts->input);
	}

	return IRQ_HANDLED;
}

/*
 * The input core calls these when the first reader arrives and the last one
 * leaves, and for the "inhibited" attribute.  GNOME closes the built-in
 * touchscreen while the panel is in power save and reopens it when the panel
 * comes back, which is downstream's screen-off/screen-on signal
 * (fts_input_open()/fts_input_close()).  The opener holds the input device's
 * mutex, so neither waits for a mode change in progress: they only record
 * the new state for the work, which reads it under the lock.
 */
static int fts1ba90a_input_open(struct input_dev *input)
{
	struct fts1ba90a *ts = input_get_drvdata(input);

	WRITE_ONCE(ts->opened, true);
	fts1ba90a_queue_mode(ts);

	return 0;
}

static void fts1ba90a_input_close(struct input_dev *input)
{
	struct fts1ba90a *ts = input_get_drvdata(input);

	WRITE_ONCE(ts->opened, false);
	fts1ba90a_queue_mode(ts);
}

static void fts1ba90a_lid_event(struct input_handle *handle,
				unsigned int type, unsigned int code, int value)
{
	struct fts1ba90a *ts = handle->private;

	if (type == EV_SW && code == SW_LID)
		fts1ba90a_queue_mode(ts);
}

static int fts1ba90a_lid_connect(struct input_handler *handler,
				 struct input_dev *dev,
				 const struct input_device_id *id)
{
	struct fts1ba90a *ts = container_of(handler, struct fts1ba90a,
					    lid_handler);
	struct input_handle *handle;
	int error;

	handle = kzalloc_obj(*handle);
	if (!handle)
		return -ENOMEM;

	handle->dev = dev;
	handle->handler = handler;
	handle->name = "fts1ba90a-lid";
	handle->private = ts;

	error = input_register_handle(handle);
	if (error)
		goto err_free;

	/* passive: does not count as a reader of the lid device */
	error = input_open_device(handle);
	if (error)
		goto err_unregister;

	fts1ba90a_queue_mode(ts);
	return 0;

err_unregister:
	input_unregister_handle(handle);
err_free:
	kfree(handle);
	return error;
}

static void fts1ba90a_lid_disconnect(struct input_handle *handle)
{
	struct fts1ba90a *ts = handle->private;

	input_close_device(handle);
	input_unregister_handle(handle);
	kfree(handle);

	fts1ba90a_queue_mode(ts);
}

static const struct input_device_id fts1ba90a_lid_ids[] = {
	{
		.flags = INPUT_DEVICE_ID_MATCH_EVBIT |
			 INPUT_DEVICE_ID_MATCH_SWBIT,
		.evbit = { BIT_MASK(EV_SW) },
		.swbit = { [BIT_WORD(SW_LID)] = BIT_MASK(SW_LID) },
	},
	{ }
};

static void fts1ba90a_unwatch_lid(void *data)
{
	struct fts1ba90a *ts = data;

	WRITE_ONCE(ts->lid_watch, false);
	input_unregister_handler(&ts->lid_handler);
}

/*
 * Follow every SW_LID switch (the book-cover Hall sensor, the keyboard
 * cover's Hall payload, the digitiser's folio detection) so that the wake
 * gesture is never armed under a closed cover.  Without the watch the
 * gesture simply ignores covers, so a failure is not fatal.
 */
static void fts1ba90a_watch_lid(struct fts1ba90a *ts)
{
	struct device *dev = &ts->client->dev;
	int ret;

	ts->lid_handler.event = fts1ba90a_lid_event;
	ts->lid_handler.connect = fts1ba90a_lid_connect;
	ts->lid_handler.disconnect = fts1ba90a_lid_disconnect;
	ts->lid_handler.passive_observer = true;
	ts->lid_handler.name = "fts1ba90a-lid";
	ts->lid_handler.id_table = fts1ba90a_lid_ids;

	ret = input_register_handler(&ts->lid_handler);
	if (ret) {
		dev_warn(dev, "cannot follow lid switches: %d\n", ret);
		return;
	}

	ret = devm_add_action_or_reset(dev, fts1ba90a_unwatch_lid, ts);
	if (ret) {
		dev_warn(dev, "cannot follow lid switches: %d\n", ret);
		return;
	}

	WRITE_ONCE(ts->lid_watch, true);
}

static void fts1ba90a_charger_work(struct work_struct *work)
{
	struct fts1ba90a *ts = container_of(work, struct fts1ba90a,
					    charger_work);
	bool charger = power_supply_is_system_supplied() > 0;
	int ret;

	mutex_lock(&ts->lock);
	if (charger != ts->charger) {
		WRITE_ONCE(ts->charger, charger);
		dev_dbg(&ts->client->dev, "external power %s\n",
			charger ? "present" : "gone");
		if (ts->state != FTS_STATE_OFF) {
			ret = fts1ba90a_write_charger_mode(ts);
			if (ret < 0)
				dev_warn(&ts->client->dev,
					 "failed to set charger mode: %d\n", ret);
		}
	}
	mutex_unlock(&ts->lock);
}

static int fts1ba90a_psy_notify(struct notifier_block *nb,
				unsigned long event, void *data)
{
	struct fts1ba90a *ts = container_of(nb, struct fts1ba90a, psy_nb);
	struct power_supply *psy = data;

	/* a battery's own updates (capacity, voltage) do not change this */
	if (event == PSY_EVENT_PROP_CHANGED &&
	    psy->desc->type != POWER_SUPPLY_TYPE_BATTERY)
		queue_work(system_freezable_wq, &ts->charger_work);

	return NOTIFY_OK;
}

static void fts1ba90a_unwatch_power(void *data)
{
	struct fts1ba90a *ts = data;

	power_supply_unreg_notifier(&ts->psy_nb);
}

/*
 * The VBUS notifier downstream uses is Samsung-only; any external supply
 * coming online is the mainline equivalent.  Without the notifier the
 * controller just stays in normal mode.
 */
static void fts1ba90a_watch_power(struct fts1ba90a *ts)
{
	struct device *dev = &ts->client->dev;
	int ret;

	if (!IS_ENABLED(CONFIG_POWER_SUPPLY))
		return;

	ts->psy_nb.notifier_call = fts1ba90a_psy_notify;
	ret = power_supply_reg_notifier(&ts->psy_nb);
	if (ret) {
		dev_warn(dev, "cannot follow external power: %d\n", ret);
		return;
	}

	ret = devm_add_action_or_reset(dev, fts1ba90a_unwatch_power, ts);
	if (ret) {
		dev_warn(dev, "cannot follow external power: %d\n", ret);
		return;
	}

	queue_work(system_freezable_wq, &ts->charger_work);
}

static void fts1ba90a_cancel_work(void *data)
{
	struct fts1ba90a *ts = data;

	cancel_work_sync(&ts->mode_work);
	cancel_work_sync(&ts->charger_work);
}

/*
 * touchscreen-x-mm/-y-mm describe the controller's own axes, like
 * touchscreen-size-x/-y, so they follow the axis swap (as in wacom_w9000).
 * libinput and mutter use the resolution to match the touchscreen's size to
 * its output.
 */
static void fts1ba90a_set_resolution(struct fts1ba90a *ts)
{
	struct device *dev = &ts->client->dev;
	bool swap = ts->prop.swap_x_y;
	u32 mm;

	if (!device_property_read_u32(dev, "touchscreen-x-mm", &mm) && mm)
		input_abs_set_res(ts->input,
				  swap ? ABS_MT_POSITION_Y : ABS_MT_POSITION_X,
				  DIV_ROUND_CLOSEST(ts->prop.max_x + 1, mm));

	if (!device_property_read_u32(dev, "touchscreen-y-mm", &mm) && mm)
		input_abs_set_res(ts->input,
				  swap ? ABS_MT_POSITION_X : ABS_MT_POSITION_Y,
				  DIV_ROUND_CLOSEST(ts->prop.max_y + 1, mm));
}

static ssize_t double_tap_to_wake_show(struct device *dev,
				       struct device_attribute *attr, char *buf)
{
	struct fts1ba90a *ts = dev_get_drvdata(dev);

	return sysfs_emit(buf, "%d\n", READ_ONCE(ts->double_tap));
}

/*
 * The user setting (downstream sec_cmd aot_enable).  It is off by default,
 * as in the stock kernel, where the framework applies the "Double tap to
 * turn on screen" setting after boot: armed, the controller keeps both rails
 * and scans in low-power mode for as long as the screen is off.
 */
static ssize_t double_tap_to_wake_store(struct device *dev,
					struct device_attribute *attr,
					const char *buf, size_t count)
{
	struct fts1ba90a *ts = dev_get_drvdata(dev);
	bool enable;
	int ret;

	ret = kstrtobool(buf, &enable);
	if (ret)
		return ret;

	mutex_lock(&ts->lock);
	if (enable != ts->double_tap) {
		WRITE_ONCE(ts->double_tap, enable);
		if (ts->state != FTS_STATE_OFF) {
			ret = fts1ba90a_write_sponge_mode(ts);
			if (ret < 0)
				dev_warn(dev, "failed to set the gesture mode: %d\n",
					 ret);
		}
		dev_info(dev, "double tap to wake %s\n",
			 enable ? "enabled" : "disabled");
	}
	mutex_unlock(&ts->lock);

	fts1ba90a_queue_mode(ts);
	return count;
}
static DEVICE_ATTR_RW(double_tap_to_wake);

static ssize_t power_state_show(struct device *dev,
				struct device_attribute *attr, char *buf)
{
	struct fts1ba90a *ts = dev_get_drvdata(dev);

	return sysfs_emit(buf, "%s\n",
			  fts1ba90a_state_names[READ_ONCE(ts->state)]);
}
static DEVICE_ATTR_RO(power_state);

static struct attribute *fts1ba90a_attrs[] = {
	&dev_attr_double_tap_to_wake.attr,
	&dev_attr_power_state.attr,
	NULL
};
ATTRIBUTE_GROUPS(fts1ba90a);

static int fts1ba90a_probe(struct i2c_client *client)
{
	struct device *dev = &client->dev;
	struct fts1ba90a *ts;
	int ret;

	if (!i2c_check_functionality(client->adapter, I2C_FUNC_I2C))
		return -ENXIO;

	if (!client->irq)
		return dev_err_probe(dev, -EINVAL, "no irq specified\n");

	ts = devm_kzalloc(dev, sizeof(*ts), GFP_KERNEL);
	if (!ts)
		return -ENOMEM;

	ts->client = client;
	i2c_set_clientdata(client, ts);
	spin_lock_init(&ts->irq_lock);
	INIT_WORK(&ts->mode_work, fts1ba90a_mode_work);
	INIT_WORK(&ts->charger_work, fts1ba90a_charger_work);

	ret = devm_mutex_init(dev, &ts->io_lock);
	if (ret)
		return ret;

	ret = devm_mutex_init(dev, &ts->lock);
	if (ret)
		return ret;

	/*
	 * wakeup-source in the DT makes the i2c core register the device as a
	 * wakeup source and arm client->irq for every system sleep.  The IRQ is
	 * armed here instead, and only while the wake gesture is set up: with
	 * the controller powered off for the sleep, its attention line is not
	 * driven and must not wake the system.  power/wakeup keeps its
	 * meaning, whether an armed gesture may wake the system from sleep.
	 */
	if (device_can_wakeup(dev))
		dev_pm_clear_wake_irq(dev);

	ts->vdd = devm_regulator_get(dev, "vdd");
	if (IS_ERR(ts->vdd))
		return dev_err_probe(dev, PTR_ERR(ts->vdd),
				     "failed to get vdd\n");

	ts->avdd = devm_regulator_get(dev, "avdd");
	if (IS_ERR(ts->avdd))
		return dev_err_probe(dev, PTR_ERR(ts->avdd),
				     "failed to get avdd\n");

	/*
	 * Registered before the IRQ so that, on teardown, the IRQ is freed
	 * first and the rails are cut last.
	 */
	ret = devm_add_action_or_reset(dev, fts1ba90a_power_off_action, ts);
	if (ret)
		return ret;

	ret = fts1ba90a_start(ts, true);
	if (ret < 0)
		return dev_err_probe(dev, ret, "failed to initialise\n");

	/*
	 * Started for whoever opens the touchscreen first, like downstream,
	 * which marks the device enabled at the end of probe; the first close
	 * applies the screen-off state.
	 */
	ts->state = FTS_STATE_ACTIVE;
	ts->opened = true;

	fts1ba90a_log_gesture_library(ts);

	ts->input = devm_input_allocate_device(dev);
	if (!ts->input)
		return -ENOMEM;

	ts->input->name = "STMicroelectronics FTS1BA90A";
	ts->input->id.bustype = BUS_I2C;
	ts->input->open = fts1ba90a_input_open;
	ts->input->close = fts1ba90a_input_close;
	input_set_drvdata(ts->input, ts);

	/* touchscreen-size-x/y from DT override the panel's own geometry */
	input_set_abs_params(ts->input, ABS_MT_POSITION_X, 0, ts->max_x, 0, 0);
	input_set_abs_params(ts->input, ABS_MT_POSITION_Y, 0, ts->max_y, 0, 0);
	input_set_abs_params(ts->input, ABS_MT_TOUCH_MAJOR, 0, 255, 0, 0);
	input_set_abs_params(ts->input, ABS_MT_TOUCH_MINOR, 0, 255, 0, 0);
	input_set_abs_params(ts->input, ABS_MT_PRESSURE, 0, FTS_PRESSURE_MAX,
			     0, 0);
	input_set_abs_params(ts->input, ABS_MT_TOOL_TYPE, MT_TOOL_FINGER,
			     MT_TOOL_PALM, 0, 0);
	touchscreen_parse_properties(ts->input, true, &ts->prop);
	fts1ba90a_set_resolution(ts);

	/*
	 * No INPUT_MT_DROP_UNUSED: the controller reports only the contacts
	 * that changed, so an untouched slot must survive the frame.
	 */
	ret = input_mt_init_slots(ts->input, FTS_FINGER_MAX, INPUT_MT_DIRECT);
	if (ret)
		return dev_err_probe(dev, ret, "failed to init MT slots\n");

	/*
	 * The wake gesture gets its own key-only device: GNOME closes the
	 * touchscreen while the panel is off but keeps key devices open, and
	 * treats any key as activity that turns the screen back on.
	 */
	ts->wake_input = devm_input_allocate_device(dev);
	if (!ts->wake_input)
		return -ENOMEM;

	ts->wake_input->name = "STMicroelectronics FTS1BA90A Wake Gesture";
	ts->wake_input->id.bustype = BUS_I2C;
	input_set_capability(ts->wake_input, EV_KEY, KEY_WAKEUP);

	/*
	 * Requested before the input devices are registered, so that open and
	 * close, which can follow registration at once, find it in place; the
	 * trigger type comes from the DT interrupt specifier (level low).
	 */
	ret = devm_request_threaded_irq(dev, client->irq, NULL,
					fts1ba90a_irq_handler, IRQF_ONESHOT,
					client->name, ts);
	if (ret)
		return dev_err_probe(dev, ret, "failed to request irq %d\n",
				     client->irq);

	/* runs after the input devices are gone, before the IRQ is freed */
	ret = devm_add_action_or_reset(dev, fts1ba90a_cancel_work, ts);
	if (ret)
		return ret;

	ret = input_register_device(ts->input);
	if (ret)
		return dev_err_probe(dev, ret, "failed to register input\n");

	ret = input_register_device(ts->wake_input);
	if (ret)
		return dev_err_probe(dev, ret,
				     "failed to register wake input\n");

	fts1ba90a_watch_power(ts);
	fts1ba90a_watch_lid(ts);

	return 0;
}

/*
 * System sleep.  With double tap on, wakeups allowed (power/wakeup) and no
 * cover closed, the controller stays in (or enters) low-power mode and its
 * IRQ is armed to wake the system, as in downstream fts_stop_device();
 * otherwise it is powered off for the sleep.
 */
static int fts1ba90a_suspend(struct device *dev)
{
	struct fts1ba90a *ts = dev_get_drvdata(dev);
	int irq = ts->client->irq;

	mutex_lock(&ts->lock);

	if (ts->double_tap && device_may_wakeup(dev) &&
	    !fts1ba90a_lid_closed(ts) &&
	    !fts1ba90a_set_state(ts, FTS_STATE_LOWPOWER)) {
		if (!enable_irq_wake(irq)) {
			ts->irq_wake = true;
			mutex_unlock(&ts->lock);
			return 0;
		}
		dev_warn(dev, "irq %d cannot wake the system\n", irq);
	}

	fts1ba90a_set_state(ts, FTS_STATE_OFF);
	mutex_unlock(&ts->lock);

	return 0;
}

/*
 * The I2C controller, an ancestor, stops working in its own late suspend,
 * which runs after this.  Up to here the IRQ thread reads the controller
 * normally.  When suspend() has just entered low-power mode, echoes of the
 * mode commands can still be queued (downstream waits for the 0x31 echo when
 * it has no mode-change delay, so the firmware posts one) and hold the level
 * line low; they have to be read now, or they would wake the system as soon
 * as it sleeps.  So wait for a thread already woken, then park any later one
 * (see park_irq()), and wait once more for a thread that read the flag just
 * before it was set and may still be on the bus.
 */
static int fts1ba90a_suspend_late(struct device *dev)
{
	struct fts1ba90a *ts = dev_get_drvdata(dev);
	int irq = ts->client->irq;

	if (!ts->irq_wake)
		return 0;

	synchronize_irq(irq);

	spin_lock(&ts->irq_lock);
	ts->suspended = true;
	spin_unlock(&ts->irq_lock);

	synchronize_irq(irq);

	return 0;
}

/*
 * The I2C controller's early resume, which re-enables its runtime PM, has
 * run by now.  A gesture that woke the system is still in the controller's
 * FIFO: the unparked IRQ reads it and reports KEY_WAKEUP.
 */
static int fts1ba90a_resume_early(struct device *dev)
{
	fts1ba90a_unpark_irq(dev_get_drvdata(dev));

	return 0;
}

/*
 * Returning to whatever the touchscreen's readers and the setting ask for is
 * left to the mode work, which runs once tasks are thawed: downstream's PM
 * resume does nothing either and restarts the controller at input open, and
 * a start would add 75-125 ms of i2c traffic to the resume path.
 */
static int fts1ba90a_resume(struct device *dev)
{
	struct fts1ba90a *ts = dev_get_drvdata(dev);

	if (ts->irq_wake) {
		disable_irq_wake(ts->client->irq);
		ts->irq_wake = false;
	}

	fts1ba90a_queue_mode(ts);

	return 0;
}

static const struct dev_pm_ops fts1ba90a_pm_ops = {
	SYSTEM_SLEEP_PM_OPS(fts1ba90a_suspend, fts1ba90a_resume)
	LATE_SYSTEM_SLEEP_PM_OPS(fts1ba90a_suspend_late,
				 fts1ba90a_resume_early)
};

static const struct of_device_id fts1ba90a_of_match[] = {
	{ .compatible = "st,fts1ba90a" },
	{ .compatible = "samsung,fts1ba90a" },
	{ }
};
MODULE_DEVICE_TABLE(of, fts1ba90a_of_match);

static const struct i2c_device_id fts1ba90a_id[] = {
	{ "fts1ba90a" },
	{ }
};
MODULE_DEVICE_TABLE(i2c, fts1ba90a_id);

static struct i2c_driver fts1ba90a_driver = {
	.driver = {
		.name = "samsung_fts1ba90a",
		.of_match_table = fts1ba90a_of_match,
		.dev_groups = fts1ba90a_groups,
		.pm = pm_sleep_ptr(&fts1ba90a_pm_ops),
		.probe_type = PROBE_PREFER_ASYNCHRONOUS,
	},
	.probe = fts1ba90a_probe,
	.id_table = fts1ba90a_id,
};
module_i2c_driver(fts1ba90a_driver);

MODULE_DESCRIPTION("STMicroelectronics FTS1BA90A touchscreen driver");
MODULE_LICENSE("GPL");
