// SPDX-License-Identifier: GPL-2.0-only
/*
 * Silicon Mitus SM5714 USB Type-C / USB-PD controller.
 *
 * This is a clean TCPM transport driver based on the register-level behaviour
 * documented by Samsung's GPL downstream driver.  The policy engine remains
 * Linux TCPM; none of Samsung's private notifier, battery or altmode framework
 * is copied here.
 */

#include <linux/bitops.h>
#include <linux/delay.h>
#include <linux/gpio/consumer.h>
#include <linux/i2c.h>
#include <linux/interrupt.h>
#include <linux/minmax.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/of.h>
#include <linux/property.h>
#include <linux/regmap.h>
#include <linux/usb/pd.h>
#include <linux/usb/tcpm.h>
#include <linux/workqueue.h>

#define SM5714_REG_INT1			0x01
#define SM5714_REG_MASK1		0x06
#define SM5714_REG_STATUS1		0x0b
#define  SM5714_INT1_VBUS_POK		BIT(0)
#define  SM5714_INT1_ATTACH		BIT(3)
#define  SM5714_INT1_DETACH		BIT(4)
#define  SM5714_INT2_SRC_ADV		BIT(4)
#define  SM5714_INT2_VBUS_0V		BIT(5)
#define  SM5714_INT4_RX_DONE		BIT(0)
#define  SM5714_INT4_TX_DONE		BIT(1)
#define  SM5714_INT4_TX_SOP_ERR	BIT(2)
#define  SM5714_INT4_PRL_RST_DONE	BIT(4)
#define  SM5714_INT4_HRST_RX		BIT(5)
#define  SM5714_INT4_HCRST_DONE	BIT(6)
#define  SM5714_INT4_TX_DISCARD	BIT(7)

#define SM5714_REG_CORR_CNTL4		0x23
#define SM5714_REG_CORR_CNTL5		0x24
#define SM5714_REG_CC_STATUS		0x28
#define  SM5714_CC_ATTACH_MASK		GENMASK(2, 0)
#define  SM5714_CC_ATTACH_SOURCE	1
#define  SM5714_CC_ATTACH_SINK		2
#define  SM5714_CC_ATTACH_AUDIO	3
#define  SM5714_CC_RP_MASK		GENMASK(4, 3)
#define  SM5714_CC_FLIPPED		BIT(5)
#define  SM5714_CC_POWERED_CABLE	BIT(6)
#define SM5714_REG_CC_CNTL1		0x29
#define SM5714_REG_CC_CNTL3		0x2b
#define SM5714_REG_CC_CNTL5		0x2d
#define SM5714_REG_CC_CNTL7		0x2f
/* Toggled by an in-place PR_Swap; 0 is the role the attachment was made in */
#define  SM5714_CC_ROLE_SWAPPED		BIT(0)
#define SM5714_REG_PD_CNTL1		0x38
#define SM5714_REG_PD_CNTL2		0x39
#define SM5714_REG_PD_CNTL4		0x3b
#define  SM5714_PD_RESET_DONE		BIT(0)
#define  SM5714_PD_HARD_RESET		BIT(2)
#define  SM5714_PD_PROTOCOL_RESET	BIT(3)
#define SM5714_REG_RX_SRC		0x41
#define SM5714_REG_RX_HEADER		0x42
#define SM5714_REG_RX_PAYLOAD		0x44
#define SM5714_REG_RX_BUF		0x5e
#define SM5714_REG_RX_BUF_ST		0x5f
#define SM5714_REG_TX_HEADER		0x60
#define SM5714_REG_TX_PAYLOAD		0x62
#define SM5714_REG_TX_REQ		0x7e
/* Samsung reads the CC state machine here; low nibble 0x1 is CC_DISABLE */
#define SM5714_REG_PD_STATE0		0xd5
#define SM5714_REG_PD_STATE3		0xd8
/* Samsung: "Reset Wait Policy Engine", the Protocol Layer after a Hard Reset */
#define  SM5714_PD_STATE3_RESET_WAIT	GENMASK(2, 1)

/* How often the attach watchdog samples CC while the port is idle. */
#define SM5714_CC_WATCH_MS		4000
/* A sample that disagrees with TCPM is taken again this much later. */
#define SM5714_CC_CONFIRM_MS		500
/*
 * How long the chip is left in Unattached.SNK (CC_CNTL3 0x82) after a CC open
 * before the toggling mode is written.  The re-probe that brought the
 * SM-X910's dead port back had 0.5 ms between the two writes (TCPM log,
 * 2026-11-26); this is no less, and nobody has measured what the chip needs.
 */
#define SM5714_CC_REOPEN_US		1000

/*
 * The watchdog's resyncs of one attachment that TCPM did not take: the wait
 * after the first, second, third and every later one.  Back to the first
 * when the chip shows nothing, reports an attach or detach, or TCPM attaches.
 */
static const unsigned int sm5714_resync_wait_ms[] = { 0, 30000, 120000, 600000 };

/*
 * The CC self-heal: how long the port toggles without a partner before the
 * first re-initialisation of the CC logic, and the waits before the second
 * and the third.  There is no fourth until a partner has been seen.
 */
static const unsigned int sm5714_heal_wait_ms[] = { 5000, 25000, 90000 };

/*
 * Rp advertised to a device we are powering, as the SM5714's CC_CNTL1 bits
 * 5:4.  0 keeps the stock advertisement, which is what every accessory
 * validated on this board expects; raising it is an experiment, not a default.
 *
 * It exists because a bus-powered USB-C hub that a PC drives happily never
 * brings its controller up here, and the advertised current is the most
 * plausible difference left.  Raising it blindly is not safe: the port
 * genuinely sources only 900 mA, and a device that believes otherwise can
 * brown the rail out.  Hence a knob, defaulting to today's behaviour, so the
 * theory can be tested on hardware without shipping it to anyone.
 *
 * Under a PD 3 contract sink_tx_ok_rp (on by default) presents Rp 3.0 A
 * where TCPM asks for SinkTxOK; otg_rp is what every other Rp request gets.
 */
static unsigned int otg_rp;
module_param(otg_rp, uint, 0644);
MODULE_PARM_DESC(otg_rp,
		 "Rp advertised while sourcing: 0 stock (default), 1..3 raise it");

/*
 * The SM5714 interrupt is level-low and this board can leave it asserted
 * while a charger is attached.  Making it a wake IRQ in that state causes
 * AOSS to return immediately without Linux observing a wake IRQ.  Keep USB-C
 * connection wake opt-in; the power key remains the normal wake source.
 */
static bool connection_wakeup;
module_param(connection_wakeup, bool, 0644);
MODULE_PARM_DESC(connection_wakeup,
		 "allow USB-C connection events to wake the system");

/*
 * As a PD sink, ask the source for the USB host role as soon as the contract
 * is in place (tcpc_dev.request_dfp_as_sink).
 *
 * A powered dock or hub is the power source and wants to be the USB device.
 * It attaches as Source/DFP and, left alone, asks for the data role swap
 * itself about 270 ms after discovery.  On this controller that swap does not
 * hold: TCPM accepts it and becomes DFP, the dock's next message still claims
 * DFP, and TCPM goes to error recovery over the data role mismatch, attach
 * after attach.  A swap that the tablet asked for first, before any
 * discovery, was accepted and held both times it was tried (one hub), and
 * discovery then ran once, as host.  TCPM repeats the request after Wait or
 * when it got no answer, with at most three requests per attach.  A Hard
 * Reset returns the role to the default; it is then asked for again.
 *
 * Source capabilities cannot tell a dock from a laptop or a phone: the
 * request goes to every PD source that advertises data role swap, and one
 * that accepts becomes the tablet's USB device.  Clear this to get TCPM's
 * standard behaviour back; a change applies from the next attach (see
 * sm5714_usbpd_latch_policy()).
 */
static bool sink_host_swap = true;
module_param(sink_host_swap, bool, 0644);
MODULE_PARM_DESC(sink_host_swap,
		 "as PD sink, request the USB host role before discovery (default on)");

/*
 * How much longer than the specification's tDRPTry TCPM keeps presenting Rd
 * in Try.SNK (tcpc_dev.try_snk_extra_wait_ms).
 *
 * The connector asks for the sink role (try-power-role), but that never took
 * effect: TCPM gives the partner 100 ms to answer Rd with Rp, and the SM5714
 * does not expose its CC comparators, only an attachment it has debounced
 * itself.  With the port forced to sink, the report for a dual-role dock
 * came 433 and 453 ms after Rd went up (two samples), about 30 ms after the
 * dock's VBUS.  All 13 Try.SNK attempts on record timed out, and a dual-role
 * dock that was first seen as a sink ended up powered by the tablet instead
 * of charging it.
 *
 * The cost is that an accessory which can only be a sink gets VBUS this much
 * later.  0 gives the standard timing back; a change applies from the next
 * attach (see sm5714_usbpd_latch_policy()).
 */
#define SM5714_TRY_SNK_EXTRA_WAIT_MAX_MS	2000

static unsigned int try_snk_extra_wait_ms = 600;
module_param(try_snk_extra_wait_ms, uint, 0644);
MODULE_PARM_DESC(try_snk_extra_wait_ms,
		 "extra ms to wait for a source in Try.SNK: 0 standard timing, default 600, at most 2000");

/*
 * As power source to a PD partner, ask it now and then for its source
 * capabilities, and ask for the sink role with a PR_Swap when they say that
 * it has its own supply (tcpc_dev.request_sink_as_source).
 *
 * A charge-through dock whose supply goes away for a moment falls back to an
 * unpowered sink, Try.SNK finds no source, and the tablet powers the dock.
 * The Anker hub (291a:8383) then said nothing for 26 minutes, also when its
 * charger was pulled and plugged in again: no PD message, no CC change, and
 * the tablet powered it from its battery until the hub was re-plugged.  TCPM
 * asks 5 s after the contract, then backs off to once every 10 minutes while
 * the partner has no supply of its own; a swap that fails stops it while
 * this partner stays attached, or comes back within a minute, and the swaps
 * are bounded.  Only while the port prefers the sink role.  sink_tx_ok_rp
 * below, on by default, lets a PD 3 partner ask for the swap itself, which
 * the same hub did at once; this is the fallback for a partner that does not.
 *
 * Clear this to get TCPM's standard behaviour back; a change applies from
 * the next attach (see sm5714_usbpd_latch_policy()).
 */
static bool swap_back_to_sink = true;
module_param(swap_back_to_sink, bool, 0644);
MODULE_PARM_DESC(swap_back_to_sink,
		 "as power source, ask a partner with its own supply for the sink role (default on)");

/*
 * Apply a Source-to-Sink PR_Swap that the tablet asked for in place, once the
 * partner has accepted it, as one the partner asks for (see
 * sm5714_usbpd_set_cc()).
 *
 * Without it the tablet's own request ends the connection: set_cc(RD) in
 * PR_SWAP_SRC_SNK_SOURCE_OFF forces the sink/UFP mode, the SM5714 drops its
 * attachment (CC reads open 0.8 ms later), the PS_RDY that follows is never
 * sent, and TCPM goes to error recovery, the partner sending a Hard Reset
 * (SM-X910, Anker hub, 2026-11-26).  Samsung's driver runs the same in-place
 * sequence for its own request and for the partner's.  The partner's swap
 * ran in place on the SM-X910 (2026-11-26, with the hub's own PR_Swap) up to
 * the hub's PS_RDY; the tablet's own has not run yet.  Clear this for the old
 * behaviour; a change applies from the next attach.
 */
static bool own_pr_swap_in_place = true;
module_param(own_pr_swap_in_place, bool, 0644);
MODULE_PARM_DESC(own_pr_swap_in_place,
		 "apply the tablet's own accepted PR_Swap to sink in place (default on)");

/*
 * Reset the chip's PD Protocol Layer at the end of an in-place PR_Swap, as
 * Samsung's PE_SNK_Startup and PE_SRC_Startup do after every swap.
 *
 * The SM5714 keeps GoodCRC, retries and the MessageIDs in hardware, and TCPM
 * resets only its own counters in SNK_STARTUP and SRC_STARTUP.  Without the
 * reset the hub's PR_Swap on the SM-X910 (2026-11-26) ended deaf: the swap
 * itself completed, then no message was received, a Hard Reset followed, and
 * PD came back only with a detach 12 s later.  The hub's PS_RDY carried
 * MessageID 0, as a new source's first Source_Capabilities do.  Clear this to
 * see the old behaviour again (the Hard Reset path below then runs); read at
 * each swap.
 */
static bool swap_protocol_reset = true;
module_param(swap_protocol_reset, bool, 0644);
MODULE_PARM_DESC(swap_protocol_reset,
		 "reset the PD protocol layer at the end of an in-place PR_Swap (default on)");

/*
 * Wait this long in the in-place Source-to-Sink swap, after VBUS is off and
 * before the pull is changed to Rd.  Samsung changes the pull about 375 ms
 * after the Accept, this driver about 65 ms after it, and sends PS_RDY 200 ms
 * after that; a partner gives up on the PS_RDY after tPSSourceOff, at least
 * 750 ms after the Accept.  Accept to PS_RDY is about 265 ms plus this wait,
 * so keep it at 450 ms or less; 310 matches Samsung's pull change.  0 (the
 * default) changes nothing; read at each swap; at most 500.
 */
#define SM5714_PR_SWAP_RD_DELAY_MAX_MS	500

static unsigned int pr_swap_rd_delay_ms;
module_param(pr_swap_rd_delay_ms, uint, 0644);
MODULE_PARM_DESC(pr_swap_rd_delay_ms,
		 "ms to wait before Rd in an in-place PR_Swap to sink: 0 none (default), at most 500");

/*
 * As source under a PD 3 contract, present Rp 3.0 A when TCPM asks for
 * SinkTxOK, so that the partner may start a message sequence itself, and
 * otg_rp otherwise.
 *
 * Without it a PD 3 sink never sees SinkTxOK and never asks for anything:
 * the hub above said nothing for 26 minutes as the tablet's sink.  With Rp
 * 3.0 A on the SM-X910 (otg_rp=2, 2026-11-26) it asked for the source role
 * itself 8 s after its charger came back.  TCPM asks for TYPEC_CC_RP_3_0 only
 * as SinkTxOK, after a PD 3 contract (tcpm_ams_finish()): the 900 mA source
 * PDO gives TYPEC_CC_RP_DEF otherwise, a PD 2 contract SinkTxNG, and a
 * partner without PD never gets a contract.  Under a contract the sink
 * follows the PDO, not the Rp; a sink that does not runs into the OTG
 * current limit.  Samsung's driver presents Rp 3.0 A to PD 3 sinks too, with
 * its Rp auto control.  Clear this to present otg_rp for every Rp request
 * again, as before; a change applies from the next attach (see
 * sm5714_usbpd_latch_policy()).
 */
static bool sink_tx_ok_rp = true;
module_param(sink_tx_ok_rp, bool, 0644);
MODULE_PARM_DESC(sink_tx_ok_rp,
		 "as PD 3 source, present Rp 3.0 A for SinkTxOK (default on)");

/*
 * Make TCPM's error recovery a real detach after an in-place PR_Swap or a
 * Hard Reset, by opening CC as set_cc(OPEN) does.
 *
 * TCPM keeps Rd up in PORT_RESET on a port that is not self-powered, and
 * while the chip has a source attached this driver then writes nothing: the
 * partner never sees a detach.  After the deaf swap above that gave three
 * more attaches without a message, until the hub opened its own CC.  Only
 * when an in-place swap or a Hard Reset has happened since the port last
 * went unattached or the chip last reported a detach.  A boot with a
 * charger keeps its Rd unless a Hard Reset came first; after one, the
 * charger's VBUS is away for about a second, as it already was for the Hard
 * Reset itself.  Clear this for the old behaviour; read at each error
 * recovery.
 */
static bool recovery_detach = true;
module_param(recovery_detach, bool, 0644);
MODULE_PARM_DESC(recovery_detach,
		 "open CC for TCPM's error recovery after an in-place PR_Swap or a Hard Reset (default on)");

/*
 * Leave a CC open of the driver's own (CC_CNTL3 0x88, Samsung's "CC off")
 * through Unattached.SNK, CC_CNTL1 0x45 and CC_CNTL3 0x82, before the
 * toggling mode (0x40, 0x80) is written.
 *
 * 0x80 alone did not bring the CC logic back.  On the SM-X910 (2026-11-26,
 * kit r9) the recovery detach above wrote 0x88 with the Anker hub attached
 * and start_toggling() wrote 0x40/0x80 100 ms later: for 100 minutes the chip
 * reported nothing (CC_STATUS 0), raised no interrupt, and the hub, which
 * powers VBUS only for an Rd it sees, stayed dark, also across re-plugs.  A
 * re-probe brought it back: there PORT_RESET's set_cc(RD) found nothing
 * attached and wrote 0x45/0x82, and the same 0x40/0x80 followed; the hub was
 * reported 112 ms later.  A boot that resets a retained dock goes 0x88,
 * 0x45/0x82, 0x40/0x80 as well.  Samsung's driver turns CC back on with
 * 0x82, never with 0x80, and writes 0x82 whenever its CC state machine reads
 * CC_DISABLE.  Clear this for the r9 sequence, for comparison; read each
 * time.
 */
static bool cc_reopen_via_snk = true;
module_param(cc_reopen_via_snk, bool, 0644);
MODULE_PARM_DESC(cc_reopen_via_snk,
		 "leave a CC open of the driver's through Unattached.SNK before toggling (default on)");

/*
 * Re-initialise the CC logic, as a re-probe does, when TCPM toggles and the
 * chip has reported nothing for 5 s after the driver opened CC, or reports
 * nothing on two samples while a partner powers VBUS.  At most three times,
 * 5 s, 30 s and 2 min into the toggling, until a partner is seen; each time
 * one warning with the chip's CC state.  See sm5714_usbpd_cc_watch_work().
 * Clear this to keep a dead port as it is (to read its registers); setting
 * it again lets the next sample act.
 */
static bool cc_self_heal = true;
module_param(cc_self_heal, bool, 0644);
MODULE_PARM_DESC(cc_self_heal,
		 "re-initialise the CC logic when the port finds no partner after a CC open (default on)");

/* Implemented by the companion charger/fuel-gauge driver on this board. */
int sm5714_battery_set_pd_contract(unsigned int mv, unsigned int ma);
int sm5714_battery_set_otg(bool active);
bool sm5714_battery_is_otg_active(void);

struct sm5714_usbpd {
	struct device *dev;
	struct regmap *regmap;
	struct mutex lock;
	struct tcpc_dev tcpc;
	struct tcpm_port *port;
	struct fwnode_handle *connector;
	struct delayed_work cc_resync_work;
	struct delayed_work cc_watch_work;
	struct delayed_work otg_det_work;
	struct gpio_desc *otg_det_gpio;
	enum typec_cc_polarity polarity;
	/*
	 * An in-place swap is armed (sm5714_usbpd_receive()) until its pull is
	 * changed; sm5714_usbpd_forget_swap() disarms it.
	 */
	bool pr_swap_src_to_snk;
	bool pr_swap_snk_to_src;
	/* Source-to-Sink: the CC hold is applied, until the partner's PS_RDY */
	bool pr_swap_hold;
	/* Sink-to-Source: the pull is changed, our PS_RDY is still to go out */
	bool pr_swap_ps_rdy_due;
	/* Our PS_RDY of that swap is on the wire: reset the Protocol Layer */
	bool prl_reset_on_tx_done;
	/* The last SOP message sent was a PR_Swap, not answered yet */
	bool pr_swap_sent;
	/* own_pr_swap_in_place and sink_tx_ok_rp, as latched for this attach */
	bool own_swap_in_place;
	bool sink_tx_ok;
	/* An in-place swap or a Hard Reset since the chip last saw a detach */
	bool recovery_detach_armed;
	/* CC_CNTL3 bit 3 (CC off) was written by the driver, not cleared since */
	bool cc_open;
	/*
	 * What TCPM knows, for the watchdog: it has been toggling since its
	 * last start_toggling() and called nothing else, and a get_cc() since
	 * then reported a partner.  VBUS from a partner at the last get_vbus().
	 */
	bool toggling;
	bool partner_seen;
	bool vbus_ext;
	/* Watchdog: suspect samples in a row, resyncs since the last change */
	unsigned int watch_suspect;
	unsigned int vbus_suspect;
	unsigned int resyncs;
	unsigned long resync_after;
	/* Self-heal: armed by a CC open, tries since a partner was last seen */
	bool heal_armed;
	unsigned int heal_tries;
	unsigned long heal_after;
	bool retained_sink_dfp;
	bool retained_dock_reset;
	bool irq_wake_enabled;
	bool suspended;
	bool policy_open;
	unsigned int negotiated_mv;
	unsigned int negotiated_ma;
};

static const struct regmap_config sm5714_usbpd_regmap_config = {
	.reg_bits = 8,
	.val_bits = 8,
	.max_register = 0xff,
	.cache_type = REGCACHE_NONE,
};

static inline struct sm5714_usbpd *tcpc_to_sm5714(struct tcpc_dev *tcpc)
{
	return container_of(tcpc, struct sm5714_usbpd, tcpc);
}

static int sm5714_usbpd_set_cc_hold(struct sm5714_usbpd *sm, u8 hold)
{
	return regmap_update_bits(sm->regmap, SM5714_REG_CC_CNTL3,
				  GENMASK(5, 4), hold);
}

/*
 * An in-place PR_Swap changes the pull by toggling bit 0 of CC_CNTL7, in
 * either direction, as Samsung's sm5714_assert_rd() and sm5714_assert_rp()
 * do.  Samsung clears bits 1:0 at every detach, so bit 0 says "swapped" from
 * the role the attachment was made in, whichever that was.  This driver
 * clears bit 0 wherever it forgets a swap or forces a mode of its own, which
 * also heals a bit left by a swap before a reboot; bit 1 is never written by
 * either driver and is left alone.  regmap_update_bits() writes nothing when
 * the bit is already clear.  Called with sm->lock (or before the IRQ).
 */
static int sm5714_usbpd_flip_cc_role(struct sm5714_usbpd *sm)
{
	unsigned int role;
	int ret;

	ret = regmap_read(sm->regmap, SM5714_REG_CC_CNTL7, &role);
	if (!ret)
		ret = regmap_write(sm->regmap, SM5714_REG_CC_CNTL7,
				   role ^ SM5714_CC_ROLE_SWAPPED);
	return ret;
}

static int sm5714_usbpd_clear_cc_role(struct sm5714_usbpd *sm)
{
	return regmap_update_bits(sm->regmap, SM5714_REG_CC_CNTL7,
				  SM5714_CC_ROLE_SWAPPED, 0);
}

/*
 * Reset the Protocol Layer (MessageIDs, retries) and flush the RX buffer, as
 * Samsung's sm5714_protocol_layer_reset() does on entry to its PE_SNK_Startup
 * and PE_SRC_Startup.  Called with sm->lock, at a moment when the partner
 * sends nothing: right after the PS_RDY that ends an in-place swap, which
 * leaves it tSwapSourceStart (20 ms) before its first message.
 */
static void sm5714_usbpd_reset_protocol(struct sm5714_usbpd *sm,
					const char *swap)
{
	int ret;

	ret = regmap_write(sm->regmap, SM5714_REG_RX_BUF_ST, 0x10);
	if (!ret)
		ret = regmap_write(sm->regmap, SM5714_REG_PD_CNTL4,
				   SM5714_PD_PROTOCOL_RESET);
	if (ret)
		dev_warn(sm->dev, "%s PR_SWAP: protocol layer reset failed: %d\n",
			 swap, ret);
	else
		dev_info(sm->dev, "%s PR_SWAP: protocol layer reset\n", swap);
}

/* No swap of any kind under way any more; the caller sees to the CC hold */
static void sm5714_usbpd_clear_swap(struct sm5714_usbpd *sm)
{
	sm->pr_swap_src_to_snk = false;
	sm->pr_swap_snk_to_src = false;
	sm->pr_swap_hold = false;
	sm->pr_swap_ps_rdy_due = false;
	sm->prl_reset_on_tx_done = false;
	sm->pr_swap_sent = false;
}

/*
 * Forget an in-place swap that does not go ahead, or whose end is not coming:
 * disarm it, and release a CC hold it applied, as Samsung's transition to
 * default does at a Hard Reset.  The pull stays as it is: a Hard Reset keeps
 * the power roles.  Called with sm->lock.
 */
static void sm5714_usbpd_forget_swap(struct sm5714_usbpd *sm,
				     const char *why)
{
	bool armed = sm->pr_swap_src_to_snk || sm->pr_swap_snk_to_src ||
		     sm->pr_swap_hold || sm->pr_swap_ps_rdy_due;
	bool held = sm->pr_swap_hold;

	sm5714_usbpd_clear_swap(sm);
	if (held)
		sm5714_usbpd_set_cc_hold(sm, 0);
	if (armed)
		dev_info(sm->dev, "PR_SWAP: in-place swap dropped (%s)\n", why);
}

/* A partner as get_cc() reports one: a source, a sink, an audio accessory */
static bool sm5714_usbpd_cc_attached(unsigned int cc)
{
	switch (cc & SM5714_CC_ATTACH_MASK) {
	case SM5714_CC_ATTACH_SOURCE:
	case SM5714_CC_ATTACH_SINK:
	case SM5714_CC_ATTACH_AUDIO:
		return true;
	default:
		return false;
	}
}

/*
 * Open both CC pins (CC_CNTL3 0x88, Samsung's "CC off") for set_cc(OPEN) and
 * for the recovery detach, and arm the self-heal for the toggling that
 * follows.  The role bit is cleared after the CC logic is off, not before:
 * with the chip attached in the swapped role, clearing it first would be an
 * in-place swap back of its own (Samsung clears it only after a detach).
 * start_toggling() turns CC on again through sm5714_usbpd_reopen_cc().
 * Called with sm->lock.
 */
static int sm5714_usbpd_open_cc(struct sm5714_usbpd *sm)
{
	int ret;

	sm5714_usbpd_clear_swap(sm);
	sm->recovery_detach_armed = false;
	ret = regmap_write(sm->regmap, SM5714_REG_CC_CNTL3, 0x88);
	if (ret)
		return ret;
	sm->cc_open = true;
	sm->heal_armed = true;
	sm->heal_tries = 0;
	return sm5714_usbpd_clear_cc_role(sm);
}

/*
 * After a CC open of the driver's, go through Unattached.SNK (CC_CNTL1 0x45,
 * CC_CNTL3 0x82) before the caller writes the toggling mode: what PORT_RESET's
 * set_cc(RD) writes on an empty port, the sequence of a boot that resets a
 * retained dock and of the re-probe that brought the SM-X910's dead port
 * back (see cc_reopen_via_snk), with the chip left there for
 * SM5714_CC_REOPEN_US.  The caller writes CC_CNTL3 next and clears cc_open.
 * Called with sm->lock.
 */
static int sm5714_usbpd_reopen_cc(struct sm5714_usbpd *sm)
{
	int ret;

	if (!sm->cc_open || !READ_ONCE(cc_reopen_via_snk))
		return 0;

	ret = regmap_write(sm->regmap, SM5714_REG_CC_CNTL1, 0x45);
	if (!ret)
		ret = regmap_write(sm->regmap, SM5714_REG_CC_CNTL3, 0x82);
	if (ret)
		return ret;
	usleep_range(SM5714_CC_REOPEN_US, 2 * SM5714_CC_REOPEN_US);
	dev_info(sm->dev, "CC on again through Unattached.SNK after the driver opened it\n");
	return 0;
}

/* Interrupt masks: 1 masks.  See sm5714_usbpd_init_regs(). */
static const u8 sm5714_usbpd_masks[5] = {
	0xe6, /* VBUS valid, attach and detach */
	0xcf, /* advertised current and VBUS 0 V */
	0xff,
	0x08, /* all PD transport events; bit 3 is unused */
	0xff,
};

/*
 * The register part of tcpc->init(), also run by the self-heal.  Called with
 * sm->lock.
 */
static int sm5714_usbpd_init_regs(struct sm5714_usbpd *sm)
{
	unsigned int state3;
	u8 pending[5];
	int ret;

	/* Match the non-water-detection part of Samsung's register init. */
	ret = regmap_write(sm->regmap, SM5714_REG_CORR_CNTL5, 0x00);
	if (ret)
		return ret;
	ret = regmap_write(sm->regmap, SM5714_REG_CORR_CNTL4, 0x00);
	if (ret)
		return ret;
	ret = regmap_read(sm->regmap, SM5714_REG_PD_STATE3, &state3);
	if (ret)
		return ret;
	/*
	 * The controller and a powered partner can both retain PD state across
	 * an AP reboot.  Flush any partial message and protocol event left in
	 * the RX window before resetting the protocol layer.  Without this, a
	 * retained-dock CC reset can deliver Source_Capabilities but then expose
	 * a stale hard reset before TCPM has transmitted its Request.
	 *
	 * This is the same RX-buffer flush used by Samsung's
	 * sm5714_protocol_layer_reset().
	 */
	ret = regmap_write(sm->regmap, SM5714_REG_RX_BUF_ST, 0x10);
	if (ret)
		return ret;
	if (state3 & 0x06) {
		ret = regmap_write(sm->regmap, SM5714_REG_PD_CNTL4, 0x01);
		if (ret)
			return ret;
	}

	/* Reading the interrupt block clears stale edge latches. */
	ret = regmap_bulk_read(sm->regmap, SM5714_REG_INT1,
			       pending, sizeof(pending));
	if (ret)
		return ret;
	return regmap_bulk_write(sm->regmap, SM5714_REG_MASK1,
				 sm5714_usbpd_masks, sizeof(sm5714_usbpd_masks));
}

/*
 * The self-heal: what an unbind and bind of the driver does to the chip on
 * an empty port, without touching TCPM, which goes on toggling: the register
 * init, PORT_RESET's set_cc(RD) (0x45/0x82), start_toggling() (0x40/0x80).
 * Called with sm->lock, only while TCPM toggles and no partner is reported.
 */
static int sm5714_usbpd_restart_cc(struct sm5714_usbpd *sm)
{
	int ret;

	ret = sm5714_usbpd_init_regs(sm);
	if (ret)
		return ret;
	sm5714_usbpd_clear_swap(sm);
	ret = sm5714_usbpd_clear_cc_role(sm);
	if (!ret)
		ret = regmap_write(sm->regmap, SM5714_REG_CC_CNTL1, 0x45);
	if (!ret)
		ret = regmap_write(sm->regmap, SM5714_REG_CC_CNTL3, 0x82);
	if (ret)
		return ret;
	usleep_range(SM5714_CC_REOPEN_US, 2 * SM5714_CC_REOPEN_US);
	ret = regmap_write(sm->regmap, SM5714_REG_CC_CNTL1, 0x40);
	if (!ret)
		ret = regmap_write(sm->regmap, SM5714_REG_CC_CNTL3, 0x80);
	if (!ret)
		sm->cc_open = false;
	return ret;
}

static void sm5714_usbpd_cc_resync_work(struct work_struct *work)
{
	struct sm5714_usbpd *sm =
		container_of(to_delayed_work(work), struct sm5714_usbpd,
			     cc_resync_work);
	unsigned int cc;

	if (READ_ONCE(sm->suspended) || IS_ERR_OR_NULL(sm->port))
		return;

	if (!regmap_read(sm->regmap, SM5714_REG_CC_STATUS, &cc))
		dev_info(sm->dev, "resynchronizing TCPM, CC status 0x%02x\n",
			 cc);
	tcpm_cc_change(sm->port);
	tcpm_vbus_change(sm->port);
}

/*
 * The attach watchdog: what the chip reports, compared with what TCPM was
 * told, while TCPM toggles.
 *
 * Measured on this board: after a detach, the SM5714 can update CC_STATUS for
 * the next accessory and raise no ATTACH interrupt at all.  The interrupt
 * count stays frozen, INT1..INT5 read back empty, the masks are correct, and
 * TCPM sits unattached forever while the connector plainly has something in
 * it.  Re-probing the driver clears it instantly, which is what pointed here.
 * When TCPM toggles and no get_cc() since its start_toggling() has reported a
 * partner, while CC_STATUS shows one on two samples 0.5 s apart, TCPM was not
 * told: it is told now (tcpm_cc_change()).  No edge has to have been seen
 * for that, a detach shorter than a sample period included; a healthy port
 * reports a partner within milliseconds, so two samples never disagree.
 *
 * And the chip can stop detecting altogether.  On the SM-X910 (2026-11-26)
 * CC_STATUS read 0 for 100 minutes with the Anker hub plugged in and
 * powered, after the recovery detach and a toggling restart; see
 * cc_reopen_via_snk.  With cc_self_heal the port is re-initialised as a
 * re-probe does it (sm5714_usbpd_restart_cc()): when TCPM has toggled for 5 s
 * after a CC open of the driver's with nothing reported, or when the chip
 * reports nothing on two samples while VBUS comes from a partner.  At most
 * three times until a partner is seen.
 *
 * The recovery has to be narrow.  An earlier attempt to re-arm the resync
 * unconditionally made TCPM oscillate between host and unattached, because
 * poking it while genuinely idle feeds back through start_toggling().  So
 * nothing here runs while TCPM is anywhere but toggling with no partner seen
 * (not in Try.SNK, a debounce, PORT_RESET or an attached state).  A resync
 * only makes TCPM read the CC state; for an attachment TCPM looked at and did
 * not take it is repeated at the next sample, after 30 s, after 2 min, then
 * every 10 min at the most.  Nothing here is armed by start_toggling().  On
 * an idle, empty port it costs one i2c read per period, and the work is
 * deferrable, so an idle tablet is never woken for it.
 */
static void sm5714_usbpd_cc_watch_work(struct work_struct *work)
{
	struct sm5714_usbpd *sm =
		container_of(to_delayed_work(work), struct sm5714_usbpd,
			     cc_watch_work);
	unsigned long delay = msecs_to_jiffies(SM5714_CC_WATCH_MS);
	unsigned int status1 = 0, state0 = 0, cc = 0;
	const char *why = NULL;
	unsigned int tries = 0;
	bool resync = false;
	bool heal = false;
	unsigned int wait;

	if (READ_ONCE(sm->suspended))
		return;

	if (IS_ERR_OR_NULL(sm->port))
		goto again;

	mutex_lock(&sm->lock);
	if (!sm->toggling || sm->partner_seen) {
		/* TCPM deals with a partner or with the port itself */
		sm->watch_suspect = 0;
		sm->vbus_suspect = 0;
		goto unlock;
	}
	if (regmap_read(sm->regmap, SM5714_REG_CC_STATUS, &cc))
		goto unlock;

	if (sm5714_usbpd_cc_attached(cc)) {
		/* A partner TCPM has not been told of; the chip detects again */
		sm->vbus_suspect = 0;
		sm->heal_armed = false;
		if (time_before(jiffies, sm->resync_after)) {
			sm->watch_suspect = 0;
			goto unlock;
		}
		if (!sm->watch_suspect++) {
			delay = msecs_to_jiffies(SM5714_CC_CONFIRM_MS);
			goto unlock;
		}
		sm->watch_suspect = 0;
		wait = sm5714_resync_wait_ms[min_t(unsigned int, sm->resyncs,
						    ARRAY_SIZE(sm5714_resync_wait_ms) - 1)];
		sm->resyncs++;
		sm->resync_after = jiffies + msecs_to_jiffies(wait);
		resync = true;
		goto unlock;
	}

	/* Nothing attached: the next attachment starts afresh */
	sm->watch_suspect = 0;
	sm->resyncs = 0;
	sm->resync_after = jiffies;
	if (cc & SM5714_CC_ATTACH_MASK) {
		/* A debug accessory: the chip detects, get_cc() reports none */
		sm->heal_armed = false;
		sm->vbus_suspect = 0;
		goto unlock;
	}
	if (READ_ONCE(sm->vbus_ext) &&
	    !regmap_read(sm->regmap, SM5714_REG_STATUS1, &status1)) {
		WRITE_ONCE(sm->vbus_ext, status1 & SM5714_INT1_VBUS_POK);
		if (status1 & SM5714_INT1_VBUS_POK)
			sm->vbus_suspect++;
		else
			sm->vbus_suspect = 0;
	} else {
		sm->vbus_suspect = 0;
	}

	if (sm->heal_armed)
		why = "since the driver opened it";
	else if (sm->vbus_suspect >= 2)
		why = "though VBUS comes from one";
	if (!why || !READ_ONCE(cc_self_heal) ||
	    sm->heal_tries >= ARRAY_SIZE(sm5714_heal_wait_ms) ||
	    time_before(jiffies, sm->heal_after))
		goto unlock;

	/* Read for the log only: Samsung's CC state, 0x1 is CC_DISABLE */
	if (regmap_read(sm->regmap, SM5714_REG_PD_STATE0, &state0))
		state0 = 0xff;
	if (!status1 && regmap_read(sm->regmap, SM5714_REG_STATUS1, &status1))
		status1 = 0xff;
	heal = !sm5714_usbpd_restart_cc(sm);
	tries = ++sm->heal_tries;
	sm->vbus_suspect = 0;
	if (sm->heal_tries < ARRAY_SIZE(sm5714_heal_wait_ms))
		sm->heal_after = jiffies +
			msecs_to_jiffies(sm5714_heal_wait_ms[sm->heal_tries]);
	else
		sm->heal_armed = false;
	delay = msecs_to_jiffies(1000);

unlock:
	mutex_unlock(&sm->lock);

	if (resync)
		dev_info(sm->dev,
			 "CC status 0x%02x shows a partner TCPM was not told of; resyncing TCPM\n",
			 cc);
	if (tries)
		dev_warn(sm->dev,
			 "CC finds no partner %s (CC status 0x%02x, status1 0x%02x, CC state 0x%02x): %s, try %u of %zu\n",
			 why, cc, status1, state0,
			 heal ? "CC logic re-initialised" : "re-initialisation failed",
			 tries, ARRAY_SIZE(sm5714_heal_wait_ms));
	if (resync || heal) {
		tcpm_cc_change(sm->port);
		tcpm_vbus_change(sm->port);
	}

again:
	if (!READ_ONCE(sm->suspended))
		queue_delayed_work(system_dfl_wq, &sm->cc_watch_work, delay);
}

static void sm5714_usbpd_otg_det_work(struct work_struct *work)
{
	struct sm5714_usbpd *sm =
		container_of(to_delayed_work(work), struct sm5714_usbpd,
			     otg_det_work);

	gpiod_set_value_cansleep(sm->otg_det_gpio, 0);
}

static int sm5714_usbpd_init(struct tcpc_dev *tcpc)
{
	struct sm5714_usbpd *sm = tcpc_to_sm5714(tcpc);
	int ret;

	mutex_lock(&sm->lock);
	ret = sm5714_usbpd_init_regs(sm);
	mutex_unlock(&sm->lock);
	return ret;
}

static int sm5714_usbpd_get_vbus(struct tcpc_dev *tcpc)
{
	struct sm5714_usbpd *sm = tcpc_to_sm5714(tcpc);
	unsigned int status;
	int ret;

	ret = regmap_read(sm->regmap, SM5714_REG_STATUS1, &status);
	if (ret)
		return ret;

	/* For the watchdog: VBUS from a partner, the one thing it reads it for */
	WRITE_ONCE(sm->vbus_ext, status & SM5714_INT1_VBUS_POK);

	/*
	 * STATUS1.VBUS_POK reports an external source but stays low while the
	 * companion charger supplies VBUS from its OTG boost.  Returning false
	 * in that state makes TCPM tear a valid source attachment down after
	 * tSrcTurnOn (480 ms), before a bus-powered hub can enumerate.
	 */
	return !!(status & SM5714_INT1_VBUS_POK) ||
	       sm5714_battery_is_otg_active();
}

static enum typec_cc_status sm5714_usbpd_rp_status(unsigned int cc)
{
	switch (cc & SM5714_CC_RP_MASK) {
	case 0x08:
		return TYPEC_CC_RP_1_5;
	case 0x10:
	case 0x18:
		return TYPEC_CC_RP_3_0;
	default:
		return TYPEC_CC_RP_DEF;
	}
}

static int sm5714_usbpd_get_current_limit(struct tcpc_dev *tcpc)
{
	struct sm5714_usbpd *sm = tcpc_to_sm5714(tcpc);
	unsigned int cc;
	int ret;

	ret = regmap_read(sm->regmap, SM5714_REG_CC_STATUS, &cc);
	if (ret)
		return ret;

	switch (sm5714_usbpd_rp_status(cc)) {
	case TYPEC_CC_RP_3_0:
		return 3000;
	case TYPEC_CC_RP_1_5:
		return 1500;
	default:
		return 500;
	}
}

/*
 * The policy parameters are writable at runtime.  TCPM reads the copies in
 * tcpc_dev under its port lock, which it also holds around start_toggling()
 * and get_cc().
 */
static void sm5714_usbpd_update_policy(struct sm5714_usbpd *sm)
{
	sm->tcpc.request_dfp_as_sink = sink_host_swap;
	sm->tcpc.try_snk_extra_wait_ms =
		min(try_snk_extra_wait_ms, SM5714_TRY_SNK_EXTRA_WAIT_MAX_MS);
	sm->tcpc.request_sink_as_source = swap_back_to_sink;
	sm->own_swap_in_place = own_pr_swap_in_place;
	sm->sink_tx_ok = sink_tx_ok_rp;
}

/*
 * A changed parameter applies from the next attach.  start_toggling() alone
 * cannot give that: it runs when the port goes back to unattached, so a
 * value written while the port sits empty would be picked up one attach
 * late.  The copies are therefore refreshed by every get_cc() from there up
 * to the one that reports a partner.  That one is the first thing TCPM asks
 * for in an attach, ahead of every use of the policy, and from it the copies
 * stay as they are until the port toggles again.
 */
static void sm5714_usbpd_latch_policy(struct sm5714_usbpd *sm, bool attached)
{
	if (!sm->policy_open)
		return;

	sm5714_usbpd_update_policy(sm);
	if (attached)
		sm->policy_open = false;
}

/*
 * get_cc() is what TCPM knows of CC: a partner it reports ends the
 * watchdog's suspicion and the self-heal's tries (the chip detects).
 */
static void sm5714_usbpd_note_cc(struct sm5714_usbpd *sm, unsigned int cc)
{
	if (!sm5714_usbpd_cc_attached(cc))
		return;

	mutex_lock(&sm->lock);
	sm->partner_seen = true;
	sm->heal_armed = false;
	sm->heal_tries = 0;
	mutex_unlock(&sm->lock);
}

static int sm5714_usbpd_get_cc(struct tcpc_dev *tcpc,
			       enum typec_cc_status *cc1,
			       enum typec_cc_status *cc2)
{
	struct sm5714_usbpd *sm = tcpc_to_sm5714(tcpc);
	enum typec_cc_status active = TYPEC_CC_OPEN;
	enum typec_cc_status inactive = TYPEC_CC_OPEN;
	unsigned int cc;
	bool flipped;
	int ret;

	ret = regmap_read(sm->regmap, SM5714_REG_CC_STATUS, &cc);
	if (ret)
		return ret;
	sm5714_usbpd_note_cc(sm, cc);

	switch (cc & SM5714_CC_ATTACH_MASK) {
	case SM5714_CC_ATTACH_SOURCE:
		active = sm5714_usbpd_rp_status(cc);
		break;
	case SM5714_CC_ATTACH_SINK:
		active = TYPEC_CC_RD;
		/*
		 * Samsung calls bit 6 CABLE_TYPE.  In source/host mode it
		 * denotes Ra on the unused CC pin and the downstream driver
		 * immediately enables VCONN.  Report Ra to TCPM so its normal
		 * policy requests VCONN through sm5714_usbpd_set_vconn().
		 */
		if (cc & SM5714_CC_POWERED_CABLE)
			inactive = TYPEC_CC_RA;
		break;
	case SM5714_CC_ATTACH_AUDIO:
		*cc1 = TYPEC_CC_RA;
		*cc2 = TYPEC_CC_RA;
		sm5714_usbpd_latch_policy(sm, true);
		return 0;
	default:
		break;
	}

	flipped = cc & SM5714_CC_FLIPPED;
	*cc1 = flipped ? inactive : active;
	*cc2 = flipped ? active : inactive;
	sm5714_usbpd_latch_policy(sm, active != TYPEC_CC_OPEN);
	return 0;
}

static int sm5714_usbpd_set_cc(struct tcpc_dev *tcpc,
			       enum typec_cc_status cc)
{
	struct sm5714_usbpd *sm = tcpc_to_sm5714(tcpc);
	unsigned int status;
	unsigned int delay;
	unsigned int rp;
	int ret;

	/*
	 * pr_swap_rd_delay_ms, before the lock so that the interrupt is
	 * served meanwhile; the branch below checks the swap again.
	 */
	delay = min(READ_ONCE(pr_swap_rd_delay_ms),
		    SM5714_PR_SWAP_RD_DELAY_MAX_MS);
	if (cc == TYPEC_CC_RD && delay && READ_ONCE(sm->pr_swap_src_to_snk))
		msleep(delay);

	mutex_lock(&sm->lock);
	/* TCPM is no longer just toggling (see start_toggling()) */
	sm->toggling = false;
	switch (cc) {
	case TYPEC_CC_OPEN:
		ret = sm5714_usbpd_open_cc(sm);
		break;
	case TYPEC_CC_RD:
		ret = regmap_read(sm->regmap, SM5714_REG_CC_STATUS,
				  &status);
		if (ret)
			break;
		if ((status & SM5714_CC_ATTACH_MASK) ==
		    SM5714_CC_ATTACH_SOURCE) {
			/*
			 * A source is attached and TCPM wants Rd: keep the
			 * attachment, except in TCPM's error recovery
			 * (PORT_RESET) after an in-place swap or a Hard Reset,
			 * where the partner has to see a detach: open CC as
			 * set_cc(OPEN) does (Samsung's "CC off").
			 * start_toggling() turns it on again.
			 */
			if (!sm->recovery_detach_armed || !recovery_detach)
				break;
			ret = sm5714_usbpd_open_cc(sm);
			if (!ret)
				dev_info(sm->dev,
					 "CC opened for TCPM's error recovery after a swap or Hard Reset\n");
			break;
		}
		if (sm->pr_swap_src_to_snk) {
			/*
			 * Apply an in-place Source -> Sink PR_SWAP.  Forcing
			 * CC_CNTL1/3 here briefly opens both CC pins and the
			 * powered dock disappears before TCPM can complete the
			 * swap.  Samsung's driver toggles bit 0 of CC_CNTL7 for
			 * both PR_SWAP directions; the SM5714 then changes the
			 * pull while preserving the existing attachment.  The
			 * swap is no longer armed once applied, so that a
			 * second set_cc(RD) in the same swap (PORT_RESET after
			 * a lost PS_RDY) takes the paths above or below and
			 * does not toggle again.
			 */
			ret = sm5714_usbpd_set_cc_hold(sm, 0x10);
			if (!ret)
				ret = sm5714_usbpd_flip_cc_role(sm);
			if (!ret) {
				sm->pr_swap_src_to_snk = false;
				sm->pr_swap_hold = true;
				sm->recovery_detach_armed = true;
				dev_info(sm->dev,
					 "Source-to-Sink PR_SWAP: CC hold applied\n");
			}
			break;
		}

		/* No natural DRP attachment exists: force sink/UFP. */
		sm5714_usbpd_clear_cc_role(sm);
		ret = regmap_write(sm->regmap, SM5714_REG_CC_CNTL1, 0x45);
		if (!ret)
			ret = regmap_write(sm->regmap, SM5714_REG_CC_CNTL3,
					   0x82);
		if (!ret)
			sm->cc_open = false;
		break;
	case TYPEC_CC_RP_DEF:
	case TYPEC_CC_RP_1_5:
	case TYPEC_CC_RP_3_0:
		/*
		 * Force source/DFP mode with the board's stock Rp advertisement.
		 *
		 * TCPM derives its initial Rp from the source PDO and asks for
		 * RP_1_5 for the 900 mA OTG supply.  On this SM5714, however,
		 * CC_CNTL1=0x59 repeatedly drops an attached passive OTG dongle.
		 * Samsung's X910 driver explicitly changes PLUG_CTRL_RP180 back
		 * to PLUG_CTRL_RP80 (CC_CNTL1=0x49) on source attachment, before
		 * enabling VBUS.  Keep that hardware-specific behaviour here.
		 * PD-capable partners still learn the real 900 mA limit from the
		 * source PDO; non-PD accessories see the safe default USB current.
		 */
		ret = regmap_read(sm->regmap, SM5714_REG_CC_STATUS,
				  &status);
		if (ret)
			break;
		if ((status & SM5714_CC_ATTACH_MASK) ==
		    SM5714_CC_ATTACH_SINK) {
			/*
			 * The autonomous DRP engine already owns this natural
			 * attachment.  Samsung leaves mode 0x40/0x80 intact
			 * and only changes the Rp selection in bits 5:4.
			 * Forcing 0x49/0x81 here generates an immediate DETACH.
			 * With sink_tx_ok_rp, SinkTxOK is Rp 3.0 A (Samsung's
			 * PLUG_CTRL_RP330); see there.
			 */
			rp = sm->sink_tx_ok && cc == TYPEC_CC_RP_3_0 ?
			     2 : otg_rp & 3;
			ret = regmap_update_bits(sm->regmap,
						 SM5714_REG_CC_CNTL1,
						 GENMASK(5, 4), rp << 4);
			break;
		}
		if (sm->pr_swap_snk_to_src) {
			/* Apply the inverse Sink -> Source PR_SWAP in place. */
			ret = sm5714_usbpd_flip_cc_role(sm);
			if (!ret) {
				sm->pr_swap_ps_rdy_due = true;
				sm->recovery_detach_armed = true;
				dev_info(sm->dev,
					 "Sink-to-Source PR_SWAP: Rp applied\n");
			}
			break;
		}

		/* No natural DRP attachment exists: force source/DFP. */
		sm5714_usbpd_clear_cc_role(sm);
		ret = regmap_write(sm->regmap, SM5714_REG_CC_CNTL1,
				   0x49 | ((otg_rp & 3) << 4));
		if (!ret)
			ret = regmap_write(sm->regmap, SM5714_REG_CC_CNTL3,
					   0x81);
		if (!ret)
			sm->cc_open = false;
		break;
	default:
		ret = -EOPNOTSUPP;
		break;
	}
	mutex_unlock(&sm->lock);

	return ret;
}

static int sm5714_usbpd_start_toggling(struct tcpc_dev *tcpc,
				       enum typec_port_type port_type,
				       enum typec_cc_status cc)
{
	struct sm5714_usbpd *sm = tcpc_to_sm5714(tcpc);
	unsigned int val = 0x40;
	int ret;

	sm5714_usbpd_update_policy(sm);
	sm->policy_open = true;

	/*
	 * TCPM toggles only unattached (SRC_UNATTACHED, SNK_UNATTACHED, a clean
	 * port after CHECK_CONTAMINANT), and its PORT_RESET, the one user of
	 * the error recovery's detach, calls set_cc() before that.  An in-place
	 * swap or a Hard Reset of the attach that ended no longer calls for a
	 * detach, also when the chip never announced the detach or TCPM sent a
	 * Hard Reset after it.  For every port type: the sink-only one goes
	 * through set_cc(RD) below.
	 */
	mutex_lock(&sm->lock);
	sm->recovery_detach_armed = false;
	mutex_unlock(&sm->lock);

	/*
	 * The SM5714 has an autonomous DRP state machine.  Samsung's NORMAL_DRP
	 * configuration selects it with CC_CNTL1=0x40; bits 5:4 retain the Rp
	 * advertisement requested by TCPM.  Using the hardware state machine is
	 * not merely an optimisation: software toggling would keep issuing I2C
	 * transfers after the QUP controller has suspended.
	 */
	if (port_type == TYPEC_PORT_SRC) {
		ret = sm5714_usbpd_set_cc(tcpc, cc);
		goto toggled;
	}
	if (port_type == TYPEC_PORT_SNK) {
		ret = sm5714_usbpd_set_cc(tcpc, TYPEC_CC_RD);
		goto toggled;
	}

	/*
	 * Use the same default Rp while the autonomous DRP engine toggles.
	 * See sm5714_usbpd_set_cc(): the stock X910 policy does not retain
	 * Rp 1.5 A after detecting a source/host attachment.
	 */

	mutex_lock(&sm->lock);
	sm5714_usbpd_clear_swap(sm);
	sm5714_usbpd_set_cc_hold(sm, 0);
	sm5714_usbpd_clear_cc_role(sm);
	/* A CC open of the driver's is left through Unattached.SNK first */
	ret = sm5714_usbpd_reopen_cc(sm);
	if (!ret)
		ret = regmap_write(sm->regmap, SM5714_REG_CC_CNTL1, val);
	/*
	 * set_cc(OPEN) and the recovery detach disable both CC comparators
	 * with CC_CNTL3=0x88.  Merely selecting NORMAL_DRP in CC_CNTL1 does not
	 * clear that latch, leaving TCPM permanently unattached.  0x80 is
	 * Samsung's CC_OP_EN (sm5714_check_cc_state()); after a CC open it
	 * follows the 0x82 above, as in Samsung's driver.
	 */
	if (!ret)
		ret = regmap_write(sm->regmap, SM5714_REG_CC_CNTL3, 0x80);
	if (!ret)
		sm->cc_open = false;
	mutex_unlock(&sm->lock);

toggled:
	if (ret)
		return ret;

	/*
	 * TCPM toggles from here, and holds its port lock until this returns,
	 * so no get_cc() of the new toggling can come before this.  The
	 * watchdog compares from here; the self-heal after a CC open counts
	 * from here.
	 */
	mutex_lock(&sm->lock);
	sm->toggling = true;
	sm->partner_seen = false;
	sm->watch_suspect = 0;
	sm->vbus_suspect = 0;
	if (sm->heal_armed && sm->heal_tries < ARRAY_SIZE(sm5714_heal_wait_ms))
		sm->heal_after = jiffies +
			msecs_to_jiffies(sm5714_heal_wait_ms[sm->heal_tries]);
	mutex_unlock(&sm->lock);
	return 0;
}

static int sm5714_usbpd_set_polarity(struct tcpc_dev *tcpc,
				     enum typec_cc_polarity polarity)
{
	struct sm5714_usbpd *sm = tcpc_to_sm5714(tcpc);

	sm->polarity = polarity;
	return 0;
}

static int sm5714_usbpd_set_vconn(struct tcpc_dev *tcpc, bool on)
{
	struct sm5714_usbpd *sm = tcpc_to_sm5714(tcpc);
	u8 val;

	if (!on)
		val = 0x18;
	else
		val = sm->polarity == TYPEC_POLARITY_CC1 ? 0x19 : 0x1a;

	return regmap_write(sm->regmap, SM5714_REG_CC_CNTL5, val);
}

static int sm5714_usbpd_set_vbus(struct tcpc_dev *tcpc, bool on, bool charge)
{
	struct sm5714_usbpd *sm = tcpc_to_sm5714(tcpc);
	int ret;

	/*
	 * Samsung freezes the detected CC state before removing VBUS in a
	 * Source-to-Sink PR_SWAP.  TCPM already supplies the stock 25 ms and
	 * 350 ms timing windows; without this SM5714-specific latch the powered
	 * dock electrically detaches while it is taking over as the new source.
	 */
	if (!on && sm->pr_swap_src_to_snk) {
		mutex_lock(&sm->lock);
		ret = sm5714_usbpd_set_cc_hold(sm, 0x20);
		mutex_unlock(&sm->lock);
		if (ret)
			return ret;
		dev_info(sm->dev,
			 "Source-to-Sink PR_SWAP: CC state frozen\n");
	}

	/*
	 * The X910 stock driver pulses usbpd,otg_det high for 130 ms on every
	 * source attachment.  This board signal is distinct from the SM5714
	 * boost controls: without it the PTN3222 reaches host Connect Detect,
	 * but never observes the downstream USB2 pull-up.
	 */
	if (on && sm->otg_det_gpio) {
		gpiod_set_value_cansleep(sm->otg_det_gpio, 1);
		mod_delayed_work(system_dfl_wq, &sm->otg_det_work,
				 msecs_to_jiffies(130));
	}

	ret = sm5714_battery_set_otg(on);
	if (!ret && on && sm->pr_swap_snk_to_src) {
		sm->pr_swap_snk_to_src = false;
		dev_info(sm->dev,
			 "Sink-to-Source PR_SWAP: VBUS source enabled\n");
	}
	if (ret || !on) {
		cancel_delayed_work(&sm->otg_det_work);
		if (sm->otg_det_gpio)
			gpiod_set_value_cansleep(sm->otg_det_gpio, 0);
	}

	return ret;
}

static int sm5714_usbpd_set_current_limit(struct tcpc_dev *tcpc,
					  u32 max_ma, u32 mv)
{
	struct sm5714_usbpd *sm = tcpc_to_sm5714(tcpc);
	int ret;

	ret = sm5714_battery_set_pd_contract(mv, max_ma);
	if (ret)
		return ret;

	sm->negotiated_mv = mv;
	sm->negotiated_ma = max_ma;
	dev_info(sm->dev, "USB-PD contract: %u mV, %u mA\n", mv, max_ma);
	return 0;
}

/*
 * TCPM turns PD reception off at every Hard Reset (HARD_RESET_START) and back
 * on where its transition to default ends: SNK_WAIT_CAPABILITIES as sink,
 * SRC_HARD_RESET_VBUS_ON as source.  That is where Samsung tells the chip's
 * Protocol Layer that the Policy Engine is done (PD_CNTL4 bit 0 in
 * sm5714_snk_transition_to_default() and sm5714_src_transition_to_pwr_on()).
 * Until it is told, the Protocol Layer waits (PD_STATE3, "Reset Wait Policy
 * Engine") and neither receives messages nor gets a transmission through;
 * a Hard Reset received still is.  Without this the SM-X910 stayed deaf from
 * the hub's Hard Reset until the chip saw a detach 11 s later.  Done only when
 * the chip says it waits, as at probe.
 */
static int sm5714_usbpd_set_pd_rx(struct tcpc_dev *tcpc, bool on)
{
	struct sm5714_usbpd *sm = tcpc_to_sm5714(tcpc);
	unsigned int state3;
	int ret;

	mutex_lock(&sm->lock);
	if (on && !regmap_read(sm->regmap, SM5714_REG_PD_STATE3, &state3) &&
	    (state3 & SM5714_PD_STATE3_RESET_WAIT)) {
		ret = regmap_write(sm->regmap, SM5714_REG_PD_CNTL4,
				   SM5714_PD_RESET_DONE);
		if (!ret)
			dev_info(sm->dev,
				 "PD protocol layer released after a Hard Reset (PD_STATE3 0x%02x)\n",
				 state3);
	}
	ret = regmap_write(sm->regmap, SM5714_REG_PD_CNTL1, on ? 0x08 : 0x00);
	mutex_unlock(&sm->lock);

	return ret;
}

static bool sm5714_usbpd_consume_retained_sink_dfp(struct tcpc_dev *tcpc)
{
	struct sm5714_usbpd *sm = tcpc_to_sm5714(tcpc);
	bool retained = sm->retained_sink_dfp;

	sm->retained_sink_dfp = false;
	return retained;
}

static int sm5714_usbpd_set_roles(struct tcpc_dev *tcpc, bool attached,
				  enum typec_role role,
				  enum typec_data_role data)
{
	struct sm5714_usbpd *sm = tcpc_to_sm5714(tcpc);
	unsigned int val;
	int ret;

	/*
	 * TCPM sets the roles at every attach, swap and reset: it is not just
	 * toggling.  An attach (snk/src/acc attach, data role swap) gives the
	 * watchdog's resyncs back.
	 */
	mutex_lock(&sm->lock);
	sm->toggling = false;
	if (attached)
		sm->resyncs = 0;
	mutex_unlock(&sm->lock);

	ret = regmap_read(sm->regmap, SM5714_REG_PD_CNTL2, &val);
	if (ret)
		return ret;

	if (role == TYPEC_SOURCE)
		val |= BIT(1);
	else
		val &= ~BIT(1);
	if (data == TYPEC_HOST)
		val |= BIT(0);
	else
		val &= ~BIT(0);

	return regmap_write(sm->regmap, SM5714_REG_PD_CNTL2, val);
}

static int sm5714_usbpd_transmit(struct tcpc_dev *tcpc,
				 enum tcpm_transmit_type type,
				 const struct pd_message *msg,
				 unsigned int negotiated_rev)
{
	struct sm5714_usbpd *sm = tcpc_to_sm5714(tcpc);
	unsigned int count;
	u8 request;
	bool ctrl;
	u8 msg_type;
	int ret;

	mutex_lock(&sm->lock);

	if (type == TCPC_TX_HARD_RESET) {
		sm5714_usbpd_forget_swap(sm, "Hard Reset sent");
		sm->recovery_detach_armed = true;
		ret = regmap_update_bits(sm->regmap, SM5714_REG_PD_CNTL4,
					 SM5714_PD_HARD_RESET,
					 SM5714_PD_HARD_RESET);
		goto out;
	}

	switch (type) {
	case TCPC_TX_SOP:
		request = 0x07;
		ctrl = !pd_header_cnt_le(msg->header);
		msg_type = pd_header_type_le(msg->header);
		/*
		 * An in-place swap goes ahead only with an Accept from TCPM
		 * (the partner's PR_Swap) and then our PS_RDY.  Anything else
		 * TCPM sends instead (Wait, Reject, Not_Supported, a Soft
		 * Reset after an Accept it no longer takes) means it does not.
		 */
		if (!ctrl || (msg_type != PD_CTRL_ACCEPT &&
			      msg_type != PD_CTRL_PS_RDY))
			sm5714_usbpd_forget_swap(sm, "TCPM sent another message");
		/* Our PS_RDY of an in-place Sink-to-Source swap */
		if (ctrl && msg_type == PD_CTRL_PS_RDY && sm->pr_swap_ps_rdy_due) {
			sm->pr_swap_ps_rdy_due = false;
			sm->prl_reset_on_tx_done = true;
		}
		/* For the answer, in sm5714_usbpd_receive() */
		sm->pr_swap_sent = ctrl && msg_type == PD_CTRL_PR_SWAP;
		break;
	case TCPC_TX_SOP_PRIME:
		request = 0x17;
		break;
	case TCPC_TX_SOP_PRIME_PRIME:
		request = 0x27;
		break;
	default:
		ret = -EOPNOTSUPP;
		goto out;
	}

	count = pd_header_cnt_le(msg->header);
	ret = regmap_bulk_write(sm->regmap, SM5714_REG_TX_HEADER,
				&msg->header, sizeof(msg->header));
	if (ret)
		goto out;
	if (count) {
		ret = regmap_bulk_write(sm->regmap, SM5714_REG_TX_PAYLOAD,
					msg->payload,
					count * sizeof(msg->payload[0]));
		if (ret)
			goto out;
	}
	ret = regmap_write(sm->regmap, SM5714_REG_TX_REQ, request);
out:
	mutex_unlock(&sm->lock);
	return ret;
}

static void sm5714_usbpd_receive(struct sm5714_usbpd *sm)
{
	struct pd_message msg = {};
	enum tcpm_transmit_type sop;
	unsigned int origin;
	unsigned int status;
	unsigned int count;
	bool swapped = false;
	u8 type;
	int ret;

	ret = regmap_bulk_read(sm->regmap, SM5714_REG_RX_HEADER,
			       &msg.header, sizeof(msg.header));
	if (ret)
		return;
	count = pd_header_cnt_le(msg.header);
	if (count > PD_MAX_PAYLOAD)
		goto acknowledge;
	if (count) {
		ret = regmap_bulk_read(sm->regmap, SM5714_REG_RX_PAYLOAD,
				       msg.payload,
				       count * sizeof(msg.payload[0]));
		if (ret)
			goto acknowledge;
	}
	ret = regmap_read(sm->regmap, SM5714_REG_RX_SRC, &origin);
	if (ret)
		goto acknowledge;

	switch (origin & 0x0f) {
	case 0:
		sop = TCPC_TX_SOP;
		break;
	case 1:
		sop = TCPC_TX_SOP_PRIME;
		break;
	case 2:
		sop = TCPC_TX_SOP_PRIME_PRIME;
		break;
	default:
		goto acknowledge;
	}

	type = pd_header_type_le(msg.header);
	/*
	 * The partner's answer to a PR_Swap the tablet sent.  An Accept while
	 * the chip has a sink attached (the tablet is the source) starts the
	 * same in-place Source-to-Sink swap as a PR_Swap of the partner's.
	 * The tablet's own Sink-to-Source request is left as it was.
	 */
	if (sop == TCPC_TX_SOP && sm->pr_swap_sent) {
		sm->pr_swap_sent = false;
		if (!count && type == PD_CTRL_ACCEPT && sm->own_swap_in_place &&
		    !regmap_read(sm->regmap, SM5714_REG_CC_STATUS, &status) &&
		    (status & SM5714_CC_ATTACH_MASK) == SM5714_CC_ATTACH_SINK) {
			sm->pr_swap_src_to_snk = true;
			dev_info(sm->dev,
				 "Source-to-Sink PR_SWAP: own request accepted, applying it in place\n");
		}
	}
	if (sop == TCPC_TX_SOP && !count && type == PD_CTRL_PR_SWAP) {
		ret = regmap_read(sm->regmap, SM5714_REG_CC_STATUS, &status);
		if (!ret) {
			if ((status & SM5714_CC_ATTACH_MASK) ==
			    SM5714_CC_ATTACH_SINK)
				sm->pr_swap_src_to_snk = true;
			else if ((status & SM5714_CC_ATTACH_MASK) ==
				 SM5714_CC_ATTACH_SOURCE)
				sm->pr_swap_snk_to_src = true;
		}
	}
	/* The partner's PS_RDY that ends an in-place Source-to-Sink swap */
	if (sop == TCPC_TX_SOP && !count && type == PD_CTRL_PS_RDY &&
	    sm->pr_swap_hold) {
		sm->pr_swap_hold = false;
		swapped = true;
		ret = sm5714_usbpd_set_cc_hold(sm, 0);
		if (!ret)
			dev_info(sm->dev,
				 "Source-to-Sink PR_SWAP: CC hold released\n");
	}
	tcpm_pd_receive(sm->port, &msg, sop);

acknowledge:
	regmap_write(sm->regmap, SM5714_REG_RX_BUF, 0x80);
	/* The partner's first message as source comes tSwapSourceStart later */
	if (swapped && READ_ONCE(swap_protocol_reset))
		sm5714_usbpd_reset_protocol(sm, "Source-to-Sink");
}

static irqreturn_t sm5714_usbpd_irq(int irq, void *data)
{
	struct sm5714_usbpd *sm = data;
	u8 intr[5];
	int ret;

	mutex_lock(&sm->lock);
	ret = regmap_bulk_read(sm->regmap, SM5714_REG_INT1,
			       intr, sizeof(intr));
	if (ret) {
		mutex_unlock(&sm->lock);
		return IRQ_NONE;
	}

	if (intr[3] & SM5714_INT4_RX_DONE)
		sm5714_usbpd_receive(sm);
	if (intr[3] & SM5714_INT4_TX_DONE) {
		/* Our PS_RDY ended an in-place Sink-to-Source swap */
		if (sm->prl_reset_on_tx_done && READ_ONCE(swap_protocol_reset))
			sm5714_usbpd_reset_protocol(sm, "Sink-to-Source");
		sm->prl_reset_on_tx_done = false;
		tcpm_pd_transmit_complete(sm->port, TCPC_TX_SUCCESS);
	} else if (intr[3] & SM5714_INT4_TX_DISCARD) {
		sm->prl_reset_on_tx_done = false;
		tcpm_pd_transmit_complete(sm->port, TCPC_TX_DISCARDED);
	} else if (intr[3] & SM5714_INT4_TX_SOP_ERR) {
		sm->prl_reset_on_tx_done = false;
		tcpm_pd_transmit_complete(sm->port, TCPC_TX_FAILED);
	}
	if (intr[3] & SM5714_INT4_HRST_RX) {
		sm5714_usbpd_forget_swap(sm, "Hard Reset received");
		sm->recovery_detach_armed = true;
		tcpm_pd_hard_reset(sm->port);
	}
	/* An attach or a detach the chip reported gives the resyncs back */
	if (intr[0] & (SM5714_INT1_ATTACH | SM5714_INT1_DETACH))
		sm->resyncs = 0;
	/* A detach the chip saw itself starts the next attach afresh */
	if (intr[0] & SM5714_INT1_DETACH)
		sm->recovery_detach_armed = false;

	mutex_unlock(&sm->lock);

	if (intr[0] & (SM5714_INT1_ATTACH | SM5714_INT1_DETACH) ||
	    intr[1] & SM5714_INT2_SRC_ADV)
		tcpm_cc_change(sm->port);
	if (intr[0] & SM5714_INT1_VBUS_POK ||
	    intr[1] & SM5714_INT2_VBUS_0V)
		tcpm_vbus_change(sm->port);

	return IRQ_HANDLED;
}

static int sm5714_usbpd_probe(struct i2c_client *client)
{
	struct device *dev = &client->dev;
	struct sm5714_usbpd *sm;
	unsigned int cc;
	unsigned int retained_roles;
	unsigned int role;
	int ret;

	sm = devm_kzalloc(dev, sizeof(*sm), GFP_KERNEL);
	if (!sm)
		return -ENOMEM;

	sm->dev = dev;
	sm->regmap = devm_regmap_init_i2c(client,
					  &sm5714_usbpd_regmap_config);
	if (IS_ERR(sm->regmap))
		return PTR_ERR(sm->regmap);
	mutex_init(&sm->lock);
	INIT_DELAYED_WORK(&sm->cc_resync_work, sm5714_usbpd_cc_resync_work);
	INIT_DEFERRABLE_WORK(&sm->cc_watch_work, sm5714_usbpd_cc_watch_work);
	INIT_DELAYED_WORK(&sm->otg_det_work, sm5714_usbpd_otg_det_work);
	i2c_set_clientdata(client, sm);

	sm->otg_det_gpio = devm_gpiod_get_optional(dev, "otg-det",
						   GPIOD_OUT_LOW);
	if (IS_ERR(sm->otg_det_gpio))
		return dev_err_probe(dev, PTR_ERR(sm->otg_det_gpio),
				     "failed to acquire OTG-detect GPIO\n");

	sm->connector = device_get_named_child_node(dev, "connector");
	if (!sm->connector)
		return dev_err_probe(dev, -EINVAL,
				     "missing usb-c-connector child\n");

	/*
	 * The SM5714 keeps its registers across a reboot or power-off taken
	 * while sink after an in-place swap, CC_CNTL7 bit 0 with them.  Clear it
	 * before anything below forces a mode or reads the attachment (see
	 * sm5714_usbpd_clear_cc_role()).
	 */
	ret = regmap_read(sm->regmap, SM5714_REG_CC_CNTL7, &role);
	if (ret)
		goto put_fwnode;
	if (role & SM5714_CC_ROLE_SWAPPED) {
		ret = sm5714_usbpd_clear_cc_role(sm);
		if (ret)
			goto put_fwnode;
		dev_info(dev, "CC role bit left set by a swap before this boot (CC_CNTL7 0x%02x), cleared\n",
			 role);
	}

	/*
	 * A powered dock and the SM5714 both survive a warm tablet reboot.  Save
	 * the controller's old local data role before tcpm_register_port()
	 * clears it while constructing the new unattached state.  CC must still
	 * describe a source attachment, so a previous PC/device connection can
	 * never seed DFP here.
	 */
	ret = regmap_read(sm->regmap, SM5714_REG_PD_CNTL2, &retained_roles);
	if (ret)
		goto put_fwnode;
	ret = regmap_read(sm->regmap, SM5714_REG_CC_STATUS, &cc);
	if (ret)
		goto put_fwnode;
	sm->retained_sink_dfp =
		(retained_roles & BIT(0)) &&
		((cc & SM5714_CC_ATTACH_MASK) == SM5714_CC_ATTACH_SOURCE);
	if (sm->retained_sink_dfp) {
		dev_info(dev, "retained Sink/DFP role detected\n");
		/*
		 * Keeping the data role is sufficient for USB host, but some
		 * powered docks retain their old PD/DisplayPort state and never
		 * send Source_Capabilities after the tablet reboots.  Present Rp
		 * briefly to create a real CC detach at the still-powered Source,
		 * then leave both pins open.  TCPM subsequently starts normal DRP
		 * toggling and the dock negotiates power, USB and altmodes from
		 * scratch exactly as it does after a physical reconnect.
		 */
		ret = regmap_write(sm->regmap, SM5714_REG_CC_CNTL1, 0x49);
		if (ret)
			goto put_fwnode;
		ret = regmap_write(sm->regmap, SM5714_REG_CC_CNTL3, 0x81);
		if (ret)
			goto put_fwnode;
		msleep(200);
		ret = regmap_write(sm->regmap, SM5714_REG_CC_CNTL3, 0x88);
		if (ret)
			goto put_fwnode;
		/* PORT_RESET's set_cc(RD) turns CC on again (0x45/0x82) */
		sm->cc_open = true;
		msleep(50);
		sm->retained_sink_dfp = false;
		sm->retained_dock_reset = true;
		dev_info(dev,
			 "reset retained dock with a CC detach before TCPM\n");
	}

	sm->tcpc.fwnode = sm->connector;
	/*
	 * A powered USB-C dock stays Source/UFP across an X910 reboot.  It is
	 * the power source but the USB data peripheral, so recover as
	 * Sink/DFP when its retained role appears in the first PD reply.
	 */
	sm->tcpc.adopt_retained_source_ufp = true;
	sm->tcpc.consume_retained_sink_dfp =
		sm5714_usbpd_consume_retained_sink_dfp;
	sm5714_usbpd_update_policy(sm);
	sm->tcpc.init = sm5714_usbpd_init;
	sm->tcpc.get_vbus = sm5714_usbpd_get_vbus;
	sm->tcpc.get_current_limit = sm5714_usbpd_get_current_limit;
	sm->tcpc.set_cc = sm5714_usbpd_set_cc;
	sm->tcpc.get_cc = sm5714_usbpd_get_cc;
	sm->tcpc.set_polarity = sm5714_usbpd_set_polarity;
	sm->tcpc.set_vconn = sm5714_usbpd_set_vconn;
	sm->tcpc.set_vbus = sm5714_usbpd_set_vbus;
	sm->tcpc.set_current_limit = sm5714_usbpd_set_current_limit;
	sm->tcpc.set_pd_rx = sm5714_usbpd_set_pd_rx;
	sm->tcpc.set_roles = sm5714_usbpd_set_roles;
	sm->tcpc.start_toggling = sm5714_usbpd_start_toggling;
	sm->tcpc.pd_transmit = sm5714_usbpd_transmit;

	sm->port = tcpm_register_port(dev, &sm->tcpc);
	if (IS_ERR(sm->port)) {
		ret = PTR_ERR(sm->port);
		goto put_fwnode;
	}

	ret = devm_request_threaded_irq(dev, client->irq, NULL,
					sm5714_usbpd_irq,
					IRQF_ONESHOT | IRQF_TRIGGER_LOW,
					dev_name(dev), sm);
	if (ret)
		goto unregister_port;

	device_init_wakeup(dev, connection_wakeup &&
			   device_property_read_bool(dev, "wakeup-source"));
	if (device_may_wakeup(dev)) {
		ret = enable_irq_wake(client->irq);
		if (ret)
			dev_warn(dev, "failed to enable wake IRQ: %d\n", ret);
		else
			sm->irq_wake_enabled = true;
	}
	/*
	 * CC comparators settle after the autonomous DRP machine is enabled.
	 * tcpm_register_port() can see Rp before the charger has raised VBUS:
	 * TCPM then spends about 1.02 seconds in PORT_RESET/PORT_RESET_WAIT_OFF.
	 * An earlier 250 ms resync was consumed inside that reset and left TCPM
	 * in TOGGLING forever when the physical attach edge predated the IRQ
	 * registration.  Wait until both that reset and VBUS ramp have finished.
	 *
	 * This remains deliberately scheduled only once: re-arming it from
	 * start_toggling() feeds back through tcpm_cc_change() and oscillates
	 * between host and unattached.
	 */
	/*
	 * A retained-dock reset creates a fresh physical CC/VBUS edge after the
	 * IRQ is registered.  Its first Source_Capabilities can still be in
	 * flight at 1.5 s, so do not let the fallback resync interrupt that
	 * negotiation.  Keep the fallback, but move it beyond the normal PD and
	 * altmode discovery window.
	 */
	mod_delayed_work(system_dfl_wq, &sm->cc_resync_work,
			 msecs_to_jiffies(sm->retained_dock_reset ? 3000 :
					  1500));
	/* Start the attach watchdog after that first resync has settled, so the
	 * two never race over the same edge. */
	queue_delayed_work(system_dfl_wq, &sm->cc_watch_work,
			   msecs_to_jiffies(5000));
	dev_info(dev, "SM5714 USB Type-C/PD controller registered\n");
	return 0;

unregister_port:
	tcpm_unregister_port(sm->port);
put_fwnode:
	fwnode_handle_put(sm->connector);
	return ret;
}

static int sm5714_usbpd_suspend(struct device *dev)
{
	struct sm5714_usbpd *sm = dev_get_drvdata(dev);

	/* No deferred I2C transaction may outlive the parent QUP suspend. */
	WRITE_ONCE(sm->suspended, true);
	cancel_delayed_work_sync(&sm->cc_watch_work);
	cancel_delayed_work_sync(&sm->cc_resync_work);
	return 0;
}

static int sm5714_usbpd_resume(struct device *dev)
{
	struct sm5714_usbpd *sm = dev_get_drvdata(dev);

	/* Samples from before the sleep are not samples in a row */
	mutex_lock(&sm->lock);
	sm->watch_suspect = 0;
	sm->vbus_suspect = 0;
	mutex_unlock(&sm->lock);
	WRITE_ONCE(sm->suspended, false);
	queue_delayed_work(system_dfl_wq, &sm->cc_watch_work,
			   msecs_to_jiffies(SM5714_CC_WATCH_MS));
	return 0;
}

static DEFINE_SIMPLE_DEV_PM_OPS(sm5714_usbpd_pm_ops,
				sm5714_usbpd_suspend,
				sm5714_usbpd_resume);

static void sm5714_usbpd_remove(struct i2c_client *client)
{
	struct sm5714_usbpd *sm = i2c_get_clientdata(client);

	cancel_delayed_work_sync(&sm->cc_watch_work);
	cancel_delayed_work_sync(&sm->cc_resync_work);
	cancel_delayed_work_sync(&sm->otg_det_work);
	if (sm->otg_det_gpio)
		gpiod_set_value_cansleep(sm->otg_det_gpio, 0);
	if (sm->irq_wake_enabled)
		disable_irq_wake(client->irq);
	tcpm_unregister_port(sm->port);
	fwnode_handle_put(sm->connector);
}

static const struct of_device_id sm5714_usbpd_of_match[] = {
	{ .compatible = "siliconmitus,sm5714-usbpd" },
	{ }
};
MODULE_DEVICE_TABLE(of, sm5714_usbpd_of_match);

static struct i2c_driver sm5714_usbpd_driver = {
	.driver = {
		.name = "sm5714-usbpd",
		.of_match_table = sm5714_usbpd_of_match,
		.pm = pm_sleep_ptr(&sm5714_usbpd_pm_ops),
	},
	.probe = sm5714_usbpd_probe,
	.remove = sm5714_usbpd_remove,
};
module_i2c_driver(sm5714_usbpd_driver);

MODULE_DESCRIPTION("Silicon Mitus SM5714 USB Type-C and PD controller");
MODULE_LICENSE("GPL");
