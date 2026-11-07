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
 * Copyright (C) Samsung Electronics (downstream protocol and sequencing)
 */

#include <linux/bits.h>
#include <linux/delay.h>
#include <linux/i2c.h>
#include <linux/input.h>
#include <linux/input/mt.h>
#include <linux/input/touchscreen.h>
#include <linux/interrupt.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/pm.h>
#include <linux/regulator/consumer.h>
#include <linux/unaligned.h>

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
#define FTS_READ_ONE_EVENT		0x60
#define FTS_READ_ALL_EVENT		0x61
#define FTS_CMD_CLEAR_ALL_EVENT		0x62
#define FTS_CMD_SET_SCAN_MODE		0xa0

#define FTS_SCAN_MODE_OFF		0x00
#define FTS_SCAN_MODE_MS_SS		0x01	/* mutual + self scan */

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

#define FTS_POLL_MS			20
#define FTS_READY_POLLS			150	/* 3 s, downstream ceiling */
#define FTS_ECHO_POLLS			250	/* 5 s, downstream ceiling */
#define FTS_BOOT_ERROR_LIMIT		32

struct fts1ba90a {
	struct i2c_client *client;
	struct input_dev *input;
	struct touchscreen_properties prop;
	struct regulator *vdd;		/* IO rail, stock tsp_io_ldo (1.8 V) */
	struct regulator *avdd;		/* analog rail, stock tsp_avdd_ldo */
	struct mutex io_lock;		/* serialises i2c transactions */
	bool powered;
	u16 max_x;
	u16 max_y;
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
				     int cmd_len)
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

		if (retry++ > FTS_ECHO_POLLS) {
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
 * Post-reset configuration (downstream fts_init()/fts_reinit()): enable the
 * default touch types, optionally force a calibration, clear the event FIFO
 * and start mutual/self scanning ("sense on" for this generation).
 * Calibration is only run on probe, mirroring downstream fts_init vs
 * fts_reinit.  A missing command echo is logged but not fatal: scanning is
 * still enabled by the write itself.
 */
static int fts1ba90a_configure(struct fts1ba90a *ts, bool calibrate)
{
	static const u8 touch_fn[3] = { FTS_CMD_SET_TOUCH_FUNCTION,
					FTS_TOUCHTYPE_DEFAULT_ENABLE, 0x00 };
	static const u8 cal_cmd[1] = { FTS_CMD_FORCE_CALIBRATION };
	static const u8 clear_cmd[1] = { FTS_CMD_CLEAR_ALL_EVENT };
	static const u8 scan_cmd[3] = { FTS_CMD_SET_SCAN_MODE, 0x00,
					FTS_SCAN_MODE_MS_SS };
	int ret;

	ret = fts1ba90a_write(ts, touch_fn, sizeof(touch_fn));
	if (ret < 0)
		return ret;

	usleep_range(10000, 11000);

	if (calibrate) {
		ret = fts1ba90a_write_wait_echo(ts, cal_cmd, sizeof(cal_cmd));
		if (ret < 0 && ret != -ETIMEDOUT)
			return ret;
	}

	ret = fts1ba90a_write_wait_echo(ts, clear_cmd, sizeof(clear_cmd));
	if (ret < 0 && ret != -ETIMEDOUT)
		return ret;

	ret = fts1ba90a_write_wait_echo(ts, scan_cmd, sizeof(scan_cmd));
	if (ret < 0 && ret != -ETIMEDOUT)
		return ret;

	return 0;
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

static void fts1ba90a_release_all(struct fts1ba90a *ts)
{
	int i;

	for (i = 0; i < FTS_FINGER_MAX; i++) {
		input_mt_slot(ts->input, i);
		input_mt_report_slot_inactive(ts->input);
	}
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
		/* hover, proximity, stylus and friends are not reported */
		return;
	}

	input_mt_slot(ts->input, tid);

	switch (action) {
	case FTS_ACTION_PRESS:
	case FTS_ACTION_MOVE:
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
		input_mt_report_slot_inactive(ts->input);
		break;
	}
}

/*
 * Status events.  Errors are named; a "ready" event at runtime means the
 * firmware reset itself (ESD, brown-out) and is sitting idle, so the scan
 * configuration is reapplied the way downstream fts_reinit() would.
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
		if (ret < 0)
			dev_err(dev, "reconfiguration failed: %d\n", ret);
		return;
	}

	if (stype != FTS_STATUS_TYPE_ERROR)
		return;

	switch (ev[1]) {
	case FTS_ERR_EVENT_QUEUE_FULL:
		dev_err(dev, "event queue full, releasing all contacts\n");
		fts1ba90a_release_all(ts);
		break;
	case FTS_ERR_EVENT_ESD:
		dev_err(dev, "ESD error reported\n");
		break;
	default:
		dev_dbg(dev, "error status event: %*ph\n", 8, ev);
		break;
	}
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
	int left, i;

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

	for (i = 0; i <= left; i++) {
		const u8 *ev = &data[i * FTS_EVENT_SIZE];

		switch (ev[0] & 0x3) {
		case FTS_EID_COORDINATE:
			fts1ba90a_handle_coordinate(ts, ev);
			break;
		case FTS_EID_STATUS:
			fts1ba90a_handle_status(ts, ev);
			break;
		default:
			dev_dbg(&ts->client->dev, "event: %*ph\n", 8, ev);
			break;
		}
	}

	input_mt_sync_frame(ts->input);
	input_sync(ts->input);

	return IRQ_HANDLED;
}

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

	ret = devm_mutex_init(dev, &ts->io_lock);
	if (ret)
		return ret;

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

	ts->input = devm_input_allocate_device(dev);
	if (!ts->input)
		return -ENOMEM;

	ts->input->name = "STMicroelectronics FTS1BA90A";
	ts->input->id.bustype = BUS_I2C;

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

	/*
	 * No INPUT_MT_DROP_UNUSED: the controller reports only the contacts
	 * that changed, so an untouched slot must survive the frame.
	 */
	ret = input_mt_init_slots(ts->input, FTS_FINGER_MAX, INPUT_MT_DIRECT);
	if (ret)
		return dev_err_probe(dev, ret, "failed to init MT slots\n");

	ret = input_register_device(ts->input);
	if (ret)
		return dev_err_probe(dev, ret, "failed to register input\n");

	/* trigger type comes from the DT interrupt specifier (level low) */
	ret = devm_request_threaded_irq(dev, client->irq, NULL,
					fts1ba90a_irq_handler, IRQF_ONESHOT,
					client->name, ts);
	if (ret)
		return dev_err_probe(dev, ret, "failed to request irq %d\n",
				     client->irq);

	return 0;
}

static int fts1ba90a_suspend(struct device *dev)
{
	struct fts1ba90a *ts = dev_get_drvdata(dev);

	/* a failed resume left the rails off and the IRQ disabled */
	if (!ts->powered)
		return 0;

	disable_irq(ts->client->irq);
	fts1ba90a_release_all(ts);
	fts1ba90a_power_off(ts);

	return 0;
}

static int fts1ba90a_resume(struct device *dev)
{
	struct fts1ba90a *ts = dev_get_drvdata(dev);
	int ret;

	ret = fts1ba90a_start(ts, false);
	if (ret < 0) {
		dev_err(dev, "resume failed, touch stays off until next resume: %d\n",
			ret);
		return 0;
	}

	enable_irq(ts->client->irq);

	return 0;
}

static DEFINE_SIMPLE_DEV_PM_OPS(fts1ba90a_pm_ops,
				fts1ba90a_suspend, fts1ba90a_resume);

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
		.pm = pm_sleep_ptr(&fts1ba90a_pm_ops),
		.probe_type = PROBE_PREFER_ASYNCHRONOUS,
	},
	.probe = fts1ba90a_probe,
	.id_table = fts1ba90a_id,
};
module_i2c_driver(fts1ba90a_driver);

MODULE_DESCRIPTION("STMicroelectronics FTS1BA90A touchscreen driver");
MODULE_LICENSE("GPL");
