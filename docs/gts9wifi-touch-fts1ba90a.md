# FTS1BA90A touchscreen on the SM-X710 (gts9wifi)

Status: `kernel/drivers/samsung_fts1ba90a.c` drives the touchscreen on the
SM-X710 since build 7 (firmware 0x0124, config 0x0020, main 0x2618, which is
byte-identical to the X710XXS5CYG1 blob `tsp_stm/fts1ba90a_gts9.bin`).  The
11" unit under test carries an STMicroelectronics FTS1BA90A at I2C 0x49 on
`&i2c4` (TLMM GPIO 25 attention line), confirmed by TWRP's Samsung kernel
binding its `fts_touch` driver to `39-0049` and streaming events.  Mainline
`st,stmfts` probes but times out in `stmfts_command()`: the FingerTipS command
set it speaks (0xA0 system reset, 0x85/0x86 event reads, 8-byte events) is not
what this IC's Samsung firmware implements.

The screen-off power states, double tap to wake, charger mode, lid handling,
pen-hover suppression and the axis resolution described in "Power states and
double tap to wake" below were added afterwards; see that section for what is
verified on hardware and what is not.

The four integration snippets for the other session are in the section
"Integration snippets" below; the search record and provenance follow.

## Power states and double tap to wake

### What the controller does

The driver follows Samsung's input open/close model (`USE_OPEN_CLOSE` in the
X710 `fts_ts.h`; `fts_input_open()` / `fts_input_close()` in `fts_ts.c`):

| Touchscreen | Double tap to wake | Lid | Controller (`power_state`) |
|---|---|---|---|
| has a reader | any | any | `active`: scanning, contacts reported |
| no reader | off (default) | any | `sense-off`: rails on, scan off (`A0 00 00`), IRQ disabled |
| no reader | on | open | `low-power`: rails on, low-power gesture scan, IRQ enabled |
| no reader | on | closed | `sense-off` |

GNOME (mutter 46) closes the built-in touchscreen while the panel is in power
save and reopens it when the panel comes back; that is the screen-off signal,
the same one Android gives Samsung's driver.  The input core's `inhibited`
attribute goes through the same callbacks, so another compositor or a script
can force the screen-off state.  After probe the controller scans for
whoever opens it first, as downstream marks the device enabled at the end of
`fts_probe()`; in practice udev's `input_id` opens and closes every event node
as it appears, so the controller goes to `sense-off` a few seconds into boot
and starts scanning when the greeter opens it.

Downstream powers the controller off at screen off when the gesture is not
wanted (`fts_stop_device(info, 0)`).  This driver stops the scan instead,
which is downstream's own flip-cover state (`fts_set_scanmode(SCAN_OFF)`),
because the IO rail L12B is also the panel's VDDIO: a power-off on close would
cut L12B during boot, through that udev open and close, while the panel's
other rails are still on from the bootloader, and at every blank it would
change the panel's rail sequence.  Both rails therefore behave exactly as
before this change; they only go down for system sleep.  Leaving sense-off
runs the post-reset configuration again, so a self-reset that happened while
the IRQ was off does not matter.

Before this change the controller kept scanning with the panel off and raised
about 6.6 attention IRQs a second with no input events (PMW-4/PMR-8 of the
2026-11-06 assessment); with the touchscreen closed it now does not scan, and
its IRQ is off unless the wake gesture is armed.  Mode changes run from a
freezable work item, off the opener's path (GNOME opens input devices through
logind, and a start from power-off takes about 75 ms, from sense-off about
50 ms).

### System sleep

| Double tap | `power/wakeup` | Lid | During sleep |
|---|---|---|---|
| on | enabled (default) | open | low-power mode, GPIO 25 armed as a wake IRQ |
| otherwise | | | `off`: both rails off, as before this change |

A gesture that woke the system stays in the controller's FIFO.  The I2C
controller is unusable from its late suspend to its early resume (GENI's
runtime PM is disabled and every transfer returns -EACCES, while the level IRQ
fires again at once -- the failure ygecesefa's patch 0006 describes), so the
driver parks its IRQ for exactly that window: its `suspend_late` callback,
which runs before the bus's, first lets the IRQ thread read what is already
queued (entering low-power mode inside `suspend()` can leave echoes of the
mode commands there -- downstream waits for the `0x31` echo when it has no
mode-change delay -- and they would otherwise wake the system at once), then
parks any later thread; its `resume_early` callback, which runs after the
bus's, re-enables the line and the thread then reads the gesture.  An IRQ
parked during suspend entry reports a hard wakeup event, which aborts the
suspend.  Downstream does the same with `fts_handler_wait_resume_work()`.  The
return to the first table happens once tasks are thawed, from the same work
item as above, as downstream restarts its controller at input open rather
than in its PM resume.

`wakeup-source` in the DT makes the i2c core register the device as a wakeup
source and arm its IRQ for every sleep.  The driver takes that arming over and
arms GPIO 25 only while the gesture is really set up: with the controller
powered off for the sleep, the attention line is not driven and must not be
able to wake the system.
`power/wakeup` keeps its usual meaning (may an armed gesture wake the system)
and defaults to enabled, so the one switch a user needs is
`double_tap_to_wake`.

### The wake key

The gesture is reported as `KEY_WAKEUP` (press and release) on a separate
key-only input device, "STMicroelectronics FTS1BA90A Wake Gesture", plus a
hard wakeup event on the touch device (`pm_wakeup_dev_event(..., 1 s, true)`):
a gesture read while a suspend is already under way aborts it, so the key is
handled by a running system instead of waiting out a sleep.  A soft event would
not do that here, because systemd-sleep does not use `/sys/power/wakeup_count`.
It cannot go out on
the touchscreen device: GNOME has that closed while the screen is dark, but it
keeps key devices open.  What GNOME does with it:

- mutter resets the idle time on any key event, so gnome-settings-daemon's
  user-active watch leaves the blank state and turns the panel on
  (`idle_became_active_cb` -> `idle_set_mode(NORMAL)` -> `backlight_enable()`
  in gsd-power 46).  On the lock screen that shows the shield; the key itself
  moves the unlock dialog to its password prompt, as any key does.
- after a system resume gsd-power turns the panel on anyway
  (`handle_resume_actions()`), so the key only matters for the
  screen-blanked-but-awake case.
- logind ignores it (it handles only power, sleep, suspend and reboot keys and
  lid/dock switches), and XKB maps it to `XF86WakeUp`, which nothing binds.

### Turning it on

The setting is off by default, as in the stock kernel, where the framework
applies "Double tap to turn on screen" after boot.  Armed, the controller keeps
scanning slowly for as long as the screen is off instead of stopping, and stays
powered through system sleep instead of being switched off, so it costs
standby power.

`71-gts9u-touchscreen.rules` (device package) gives the desktop user's group
(`input`) write access to the attribute and, where
`/run/ubuntu-gts9u/desktop-user` names the owner, ownership of it.  The I2C
bus number is not stable, so use the driver's directory:

```sh
# now, as the desktop user (no sudo needed once the rule has run)
echo 1 > /sys/bus/i2c/drivers/samsung_fts1ba90a/*-0049/double_tap_to_wake
cat /sys/bus/i2c/drivers/samsung_fts1ba90a/*-0049/power_state   # active / sense-off / low-power (off only during sleep)

# at every boot, once, as root (the driver binds before udevd runs, so the
# boot-time event is coldplug's "add"; "bind" covers a later rebind)
echo 'ACTION=="add|bind", SUBSYSTEM=="i2c", DRIVER=="samsung_fts1ba90a", ATTR{double_tap_to_wake}="1"' \
	| sudo tee /etc/udev/rules.d/72-gts9u-double-tap-to-wake.rules
```

To keep the gesture from waking a suspended system while still using it with
the screen merely blanked, write `disabled` to the device's `power/wakeup`
(root).

### Lid and covers

The driver follows every input device that has an `SW_LID` switch (the
book-cover Hall sensor on GPIO 107, the keyboard cover's Hall payload from
`samsung_stm32_pogo`, and the digitiser's folio detection once the X710 has
one) as a passive observer, and never arms the gesture while any of them
reports closed; downstream turns scanning off under a closed flip cover.  The
switch state is read directly at every decision, so it is right even while Tab
Companion has grabbed the keyboard cover, but the change events of a grabbed
device are not seen: closing the keyboard cover over an already dark screen
leaves the gesture armed until the next screen-off or suspend.  The pogo driver
also reports the keyboard folded behind the tablet as closed (assessment
INPUT-03), which disarms the gesture in that position until that is fixed.  The
lid does not affect the touchscreen while it has a reader.

### Charger mode, pen hover, resolution

- Charger mode (`0x32 0x01`) is set while any external supply is online
  (`power_supply_is_system_supplied()`, re-checked on every non-battery
  power-supply change) and after every start, the mainline equivalent of
  downstream's VBUS notifier.  The X710 sits on USB power most of the time.
- While the S Pen is in range (`samsung_wacom_should_suppress_touch()`, the
  contract of the Ultra's Goodix patch) contacts are not reported and the ones
  already down are released.  It takes effect once the X710 DTS describes its
  digitiser again.
- `touchscreen-x-mm = <148>` / `touchscreen-y-mm = <236>` (the panel's stock r04
  size, in the controller's portrait axes) set the axis resolution to 11
  units/mm after the swap, so libinput and mutter can size-match the
  touchscreen to its output.

### Diagnostics

`power_state` (read-only) names the current state.  The first event of each
kind the driver has no use for is logged once ("unhandled event ..." with the
first eight bytes, or "unreported contact event ..." with the touch type and
no coordinates), so an unexpected event stream can be identified from
`journalctl -k` alone; the image's integrity lockdown refuses dynamic debug.
At probe the driver logs the firmware's gesture library as downstream
`fts_check_custom_library()` reads it ("gesture library: use .., version ..").

Not ported: Samsung's grip/edge zones and per-orientation dead zones (pushed by
the Android framework through `sec_cmd`), temperature compensation (`0x3C`,
polled every 30 s downstream), FOD and the other sponge gestures, and the
pull-down suspend pin state (`fts1ba90a_attn_irq_suspend`; the IRQ is disabled
whenever the controller is off).

## Integration snippets

### 1. DTS node for `kernel/dts/sm8550-samsung-gts9wifi.dts`

Replaces the existing `touchscreen@49` node under `&i2c4` (currently
`compatible = "st,stmfts"`).  The regulator labels `vreg_l14b_3p2` and
`vreg_l12b_1p8` already exist in that file.

```dts
&i2c4 {
	clock-frequency = <400000>;
	status = "okay";

	/*
	 * STMicroelectronics FTS1BA90A (Samsung "FingerTip S").  The stock DTS
	 * node is compatible "stm,fts_touch" with sec,max_coords <0x640 0xa00>
	 * (1600 x 2560), tsp_avdd_ldo = L14B 3.2 V, tsp_io_ldo = L12B 1.8 V,
	 * sec,irq_gpio = TLMM 25, pinctrl input-enable/bias-disable.  The
	 * downstream driver hardcodes IRQF_TRIGGER_LOW | IRQF_ONESHOT.
	 *
	 * Orientation: the controller's native axes are portrait (x 0..1599,
	 * y 0..2559) while the panel is driven as 2560 x 1600 landscape.  The
	 * swap + inverted-x pair is the one validated on an SM-X710 by the
	 * nacht20-de Fedora port and used by the X716B port.
	 */
	touchscreen@49 {
		compatible = "st,fts1ba90a";
		reg = <0x49>;
		interrupts-extended = <&tlmm 25 IRQ_TYPE_LEVEL_LOW>;
		avdd-supply = <&vreg_l14b_3p2>;
		vdd-supply = <&vreg_l12b_1p8>;
		touchscreen-size-x = <1600>;
		touchscreen-size-y = <2560>;
		touchscreen-x-mm = <148>;
		touchscreen-y-mm = <236>;
		touchscreen-inverted-x;
		touchscreen-swapped-x-y;
		wakeup-source;
	};
};
```

`wakeup-source` belongs to the double-tap support only: on its own, with a
driver that powers the controller off in suspend, the i2c core would arm GPIO 25
while nothing drives it.

Optional, mirroring the stock `fts1ba90a_attn_irq` pin state (the three
sibling ports run without it, relying on the bootloader's pin setup):

```dts
&tlmm {
	ts_int_default: ts-int-default-state {
		pins = "gpio25";
		function = "gpio";
		bias-disable;
	};
};
```

and in the node: `pinctrl-names = "default"; pinctrl-0 = <&ts_int_default>;`.

If the Goodix GT9916 node at 0x5d is kept for the other 11" units, both
nodes can coexist on the bus: only the controller that is fitted answers, and
the other driver fails its probe (the FTS driver with "no ready event" then
"controller never became ready", -ETIMEDOUT, after about six seconds of
polling; the rails are released again on that path).

### 2. Install hook for `scripts/build-mainline-kernel.sh`

Modeled on the `samsung_stm32_pogo` hook at lines 504-522.  Place it after
the `samsung_wacom_w90xx` block (lines 524-543) so `touch_dir` is already
defined; otherwise add `touch_dir=$kernel_tree/drivers/input/touchscreen`
first.  The driver includes `<linux/samsung_wacom.h>`, which the same script
installs into the kernel tree before the touchscreen drivers.

```sh
install -m 0644 "$drv/samsung_fts1ba90a.c" \
	"$touch_dir/samsung_fts1ba90a.c"
if ! grep -q 'TOUCHSCREEN_SAMSUNG_FTS1BA90A' "$touch_dir/Kconfig"; then
	cat >> "$touch_dir/Kconfig" <<'EOF'

config TOUCHSCREEN_SAMSUNG_FTS1BA90A
	tristate "Samsung Galaxy Tab S9 STMicroelectronics FTS1BA90A touchscreen"
	depends on I2C
	depends on REGULATOR
	help
	  Driver for the STMicroelectronics FTS1BA90A touch controller in
	  Samsung's Galaxy Tab S9 (SM-X710) and S9+ (SM-X810).  Mainline's
	  stmfts cannot drive it: this firmware uses 16-byte events behind
	  opcodes 0x60/0x61 and an in-band system reset, not the FingerTipS
	  command set.
EOF
fi
grep -q 'samsung_fts1ba90a.o' "$touch_dir/Makefile" || \
	printf 'obj-$(CONFIG_TOUCHSCREEN_SAMSUNG_FTS1BA90A) += samsung_fts1ba90a.o\n' \
		>> "$touch_dir/Makefile"
```

### 3. Config line for `kernel/config/config-gts9wifi.fragment`

```
CONFIG_TOUCHSCREEN_SAMSUNG_FTS1BA90A=y
```

`CONFIG_TOUCHSCREEN_STMFTS=y` can stay or go; the two drivers match different
compatibles and do not conflict.

### 4. Provenance and protocol summary

#### Where the driver came from

`kernel/drivers/samsung_fts1ba90a.c` is a reuse, not a from-scratch port.
The lineage, all GPL-2.0:

| Layer | Source | License |
|---|---|---|
| Base file | `pmaports-overlay/device/testing/linux-postmarketos-qcom-sm8450/fts1ba90a.c` in [aaronsb/sm-x800-linux](https://github.com/aaronsb/sm-x800-linux) (Galaxy Tab S8+, SM-X800, same IC) | GPL-2.0-only (`LICENSE.md` of that repo for the kernel directory; SPDX header in the file) |
| Fixes adopted by that file | [aaronsb/sm-x800-linux PR 14](https://github.com/aaronsb/sm-x800-linux/pull/14): chip-ID check, ready-wait requiring eid STATUS, IRQ-zero guard, `ABS_MT_PRESSURE` from byte 6; taken from `kernel/files/fts1ba90a.c` in [nacht20-de/gts9wifi-fedora](https://github.com/nacht20-de/gts9wifi-fedora) (SM-X710, hardware-validated) | GPL-2.0 (SPDX header; repo is MIT except kernel files) |
| Protocol origin | Samsung's downstream `drivers/input/touchscreen/stm/fts1ba90a/{fts_ts.c,fts_ts.h,fts_sec.c,fts_fwu.c}` and `drivers/input/sec_input/` (SM8450/SM8550 source releases; the low-power, gesture and charger sequences are taken from the SM-X710 release) | GPL-2.0 |

Changes made here relative to aaronsb's file (every one of them):

- Renamed to `samsung_fts1ba90a.c`, driver name `samsung_fts1ba90a`; header
  comment rewritten to record the lineage above.
- Added `samsung,fts1ba90a` to the OF match table next to `st,fts1ba90a`.
- Chip-ID mismatch is now a warning, not `-ENODEV` (downstream behaviour);
  device ID, firmware/config/main versions and panel geometry are logged in
  hex.  A failed panel-info read falls back to the 12-bit range (4095) instead
  of failing the probe; DT `touchscreen-size-x/y` override either value.
- Power-on and the boot "ready" wait follow Samsung's order (`fts_init`,
  `fts_reinit`): wait for the ready event after power-on, system-reset only if
  it does not arrive.  aaronsb's file reset unconditionally after power-on and,
  on resume, did both.
- A missing command echo (`0x43 0x01 <cmd>`) after force-calibration,
  clear-all-event or scan-on is a warning instead of a probe failure; the
  write itself still takes effect.
- Palm-type contacts (ttype 5) are reported with `MT_TOOL_PALM` so libinput
  ignores them; `ABS_MT_TOOL_TYPE` is declared for that.  aaronsb reported
  palms as fingers; nacht20-de dropped them.
- A runtime "ready" status event (firmware self-reset after ESD/brown-out)
  releases all contacts and reapplies the scan configuration from the IRQ
  thread, as downstream `fts_reinit()` does, and re-enters low-power mode if
  that is where the controller was.  Error status events are named (queue
  full releases all contacts; ESD is logged); the first event of every other
  unused kind is logged once.
- Power follows input open/close and system sleep as described in "Power
  states and double tap to wake": active, sense off (scan off, rails on) or
  low power with the wake gesture (Samsung's `fts_set_opmode()`,
  `fts_write_to_sponge()` and gesture decoding), and off for system sleep;
  a separate `KEY_WAKEUP` device, IRQ parking across resume, the
  `double_tap_to_wake` and `power_state` attributes, lid awareness, charger
  mode and pen-hover suppression.  Suspend no longer always cuts the rails,
  and it never writes `SENSE_OFF` (0x11), the older generation's opcode.  nacht20-de's double-tap driver and
  ygecesefa's patch 0006 were the working references; the command sequence and
  event decoding follow Samsung's X710 `fts_ts.c`.
- Power-off moved to a `devm` action registered before the IRQ, with a
  `powered` flag so suspend/remove cannot unbalance the regulators; the
  `remove` callback is gone.  `devm_mutex_init` replaces `mutex_init`.
- `msleep(10)` settles became `usleep_range(10000, 11000)` (checkpatch).

Not carried over from the sibling ports, on purpose: nacht20-de's palm
suppression hook (`wacom_wez01.h`; this driver uses the repository's own
`samsung_wacom_should_suppress_touch()` instead) and ygecesefa's
fingerprint-area sysfs attributes (`fod_rect`/`fod_enable`/`fod_state`, patch
`0017-input-fts1ba90a-fingerprint-area.patch`).  The latter mirrors the Ultra
port's Goodix interface so the EL721 libfprint driver works unchanged; it is
the obvious follow-up.

#### Protocol summary (16-byte-event "sec_input" FTS generation)

Transport: 400 kHz I2C at 0x49.  Every read is a command write followed by a
repeated-start read.  Writes are plain.

Commands used:

| Opcode | Meaning | Notes |
|---|---|---|
| `0xFA 20 00 00 24 81` | system reset (memory-mapped register write) | 10 ms, then read `0xFA 20 00 00 78` (1 byte) for CRC status; bits [1:0] set = firmware CRC error |
| `0x22` | read device ID, 5 bytes | bytes 2,3 = `0x39 0x36` for FTS1BA90A |
| `0x24` | read firmware version, 9 bytes | fw BE16 at 0, config BE16 at 2, main LE16 at 4 |
| `0x23` | read panel info, 11 bytes | max x BE16 at 0, max y BE16 at 2, tx/rx channel counts at 8/9 |
| `0x30 <lo> <hi>` | set touch function mask | `0x61 0x00` = touch, palm, wet |
| `0x13` | force calibration | echoed; probe only |
| `0x62` | clear event FIFO | echoed |
| `0xA0 00 <mode>` | set scan mode | `0x01` = mutual + self scan ("sense on"), `0x00` = off; echoed |
| `0x31 <mode>` | set operating mode | `0x00` normal, `0x01` low power; read back with `0x31` (1 byte) after 5 ms, up to ten tries (stock `stm,lpmode_change_delay = <5>`) |
| `0x39 0x02` | wake gesture command | sent after every operating-mode change while double tap is on, as downstream does |
| `0xAA 00 00 <mode>`, `0xC0 00 00` | write the gesture library ("sponge") mode byte, then commit it | bit 5 = double tap to wake; rewritten after every start and when the setting changes |
| `0x32 <mode>` | charger mode | `0x01` wired charger, `0x00` normal |
| `0xA4 06 91`, then read `0xA6 00 00` (39 bytes) | gesture library information | echoed; use, version LE16, model LE16, name length LE16, name[32]; probe only |
| `0x60` | read one event (16 bytes) | byte 7 bits [5:0] = events still queued |
| `0x61` | read all remaining events | `n * 16` bytes, n from the previous one-event read, FIFO depth 32 |

Event layout, byte 0 bits [1:0] = event id:

- `0` coordinate: bits [5:2] slot (0..9), bits [7:6] action (1 press, 2 move,
  3 release); byte 1 = x[11:4], byte 2 = y[11:4], byte 3 = x[3:0] << 4 |
  y[3:0]; byte 4 major, byte 5 minor; byte 6 = ttype[3:2] << 6 | z[5:0];
  byte 7 = ttype[1:0] << 6 | left_events[5:0].  ttype 0 normal, 1 hover,
  2 flip cover, 3 glove, 4 stylus, 5 palm, 6 wet, 7 proximity, 8 jig; only
  0, 3, 5 and 6 are reported.
- `1` status: bits [5:2] type (0 command-driven, 1 error, 2 info, 3 user
  input, 7 vendor info), byte 1 id.  Info `0x00` is the boot "ready" event,
  `0x01` wet mode, `0x02` noise mode; error `0x01` queue full, `0x02` ESD.
- `2` gesture: bits [5:2] type, bits [7:6] sf, byte 1 gesture id.  Type 1
  (double tap), sf 1 (Samsung feature), id 1 is double tap to wake, i.e. byte
  0 `0x46`, byte 1 `0x01`; types 0 swipe, 3 press (FOD), 4 single tap are
  never armed here.
- `3` vendor: full byte 0 `0x43` with byte 1 `0x01` is a command echo (bytes
  2.. repeat the command); `0xF3` is an error report.

Init order (downstream `fts_init`, as implemented):

1. `vdd` (1.8 V IO) on, 1 ms, `avdd` (3.2 V) on, 10 ms.
2. Poll `0x60` every 20 ms, up to 3 s, for the ready status event.  If it
   never comes: system reset, 10 ms, CRC status, poll again.
3. Read `0x22`, `0x24`, `0x23`; log; warn on an unexpected chip ID.
4. `0x30 61 00`, 10 ms; `0x13` (probe only), `0x62`, `0xA0 00 01`, each
   followed by polling `0x60` for its echo, up to 5 s; then the gesture mode
   byte and, on external power, charger mode.
5. Log the gesture library (probe only).
6. Request the level-low IRQ (trigger from DT), `IRQF_ONESHOT`, then register
   the touchscreen and the wake-gesture input devices.

IRQ: read one event; if byte 7 says more are queued, read them all with
`0x61`; decode; `input_mt_sync_frame` + `input_sync`.  Slots are not
`INPUT_MT_DROP_UNUSED` because the controller reports only changed contacts.

Off (system sleep only): disable IRQ, release all slots, `avdd` off, 4 ms,
`vdd` off.  Active from off: steps 1, 2 and 4 (no calibration), then enable
IRQ.  Sense off from active: disable IRQ, release all slots, `0xA0 00 00`,
50 ms.  Active from sense off: step 4 (no calibration), then enable IRQ.  Low
power from active (downstream `fts_stop_device(lpmode)`): release all slots,
`0x31 01` with read-back, `0x39 02`, gesture mode byte.  Active from low power
(downstream `fts_start_device()`): `0x31 00` with read-back, `0x39 02`,
gesture mode byte, charger mode.  Any failing step powers the controller off
and starts it from scratch.

#### What to expect on boot

```
samsung_fts1ba90a N-0049: device id .. .. 39 36 .., fw 0x...., config 0x...., main 0x....
samsung_fts1ba90a N-0049: panel 1600 x 2560, tx/rx channels NN/NN
samsung_fts1ba90a N-0049: gesture library: use 1, version .., model .. ....
input: STMicroelectronics FTS1BA90A as /devices/.../i2c-N/N-0049/input/inputM
input: STMicroelectronics FTS1BA90A Wake Gesture as /devices/.../i2c-N/N-0049/input/inputM+1
```

`N` is the mainline adapter number of `&i2c4`, which is dynamic without an
alias (9 on this image; the nacht20-de port saw `10-0049`).

Then `evtest` on that input: a finger at the panel's top-left in landscape
should give small `ABS_MT_POSITION_X` and `ABS_MT_POSITION_Y`; if an axis is
mirrored or the two are swapped, adjust `touchscreen-inverted-x/y` and
`touchscreen-swapped-x-y` in the DTS node (no driver change needed).  A
"no echo for command .." warning during probe is harmless unless touch is also
dead; "unexpected chip id" means a different firmware revision and is worth
recording.

## Search record (step 1)

Searched before writing anything, in this order.  Nothing here was executed;
downloaded files sit under the session scratchpad only.

| Where | Result |
|---|---|
| agcarbajo's repos ([postmarketos-galaxy-tab-s9-ultra](https://github.com/agcarbajo/postmarketos-galaxy-tab-s9-ultra), this repo; local copy `~/ubuntu-gts9u/pmos-reference`) | Ultra only; touch is Goodix GT9916 (`goodix_berlin`).  No FTS driver. |
| Azkali: [Azkali/sm8550-mainline](https://github.com/Azkali/sm8550-mainline) branch `gts9wifi-7.0` ("Mainline kernel for Samsung Tab S9 11"", fork of map220v/sm8550-mainline), [Azkali/gts9wifi-firmware](https://github.com/Azkali/gts9wifi-firmware) | The DTS (`arch/arm64/boot/dts/qcom/sm8550-samsung-gts9wifi.dts`, BSD-3-Clause, Azkali Manad 2026) has only a `goodix,gt9916` node at 0x5d; `drivers/input/touchscreen/{Kconfig,Makefile}` on that branch have no fts1ba90a entry.  Azkali's unit is a Goodix one.  The `st,fts1ba90a` compatible seen in a "pmOS DTB" therefore comes from the nacht20-de lineage below, not from Azkali's tree.  gitlab.postmarketos.org / pmaports has no gts9 device package. |
| Fedora effort: [nacht20-de/gts9wifi-fedora](https://github.com/nacht20-de/gts9wifi-fedora) (SM-X710) | `kernel/files/fts1ba90a.c` (GPL-2.0, 18.9 kB): compatible `st,fts1ba90a`, supplies `avdd`/`vddio`, double-tap-to-wake, palm drop, depends on their `wacom_wez01.h`.  DTS node with `interrupts-extended = <&tlmm 25 IRQ_TYPE_LEVEL_LOW>`, size 1600x2560, `touchscreen-inverted-x`, `touchscreen-swapped-x-y`, `wakeup-source`.  Config `CONFIG_TOUCHSCREEN_FTS1BA90A=y`.  Credits Azkali and agcarbajo as prior art. |
| [kquote03/linux-gts9](https://github.com/kquote03/linux-gts9) (SM-X716B 5G) | `kernel/drivers/touchscreen-fts1ba90a-x716.c` (GPL-2.0-only, 15.8 kB): independent port with line-by-line citations into Samsung's `fts_ts.c`; compatible `stm,fts_touch`; hardcodes `IRQF_TRIGGER_LOW`; no suspend.  Also vendors nacht20-de's tree.  Confirms 1600x2560 from `sec,max_coords` of the r04 board and that r00 boards used 1752x2800. |
| [aaronsb/sm-x800-linux](https://github.com/aaronsb/sm-x800-linux) (Tab S8+, SM8450) | `fts1ba90a.c` (GPL-2.0-only, 16.3 kB), the base chosen here (see above).  Its DTS comment documents the stmfts failure mode (-110) on this IC. |
| [ygecesefa/gts9wifi-ubuntu-port](https://github.com/ygecesefa/gts9wifi-ubuntu-port) (SM-X710, Ubuntu 24.04, forked from this repo's Ultra port) | No driver file in-tree (it builds nacht20-de's), but two patches on it: `0006-input-fts1ba90a-read-wake-gesture-after-bus-resume.patch` and `0017-input-fts1ba90a-fingerprint-area.patch` (FOD sysfs interface for the EL721).  MIT repo; patches carry no SPDX. |
| lore.kernel.org / linux-input | No FTS1BA90A or "new-protocol stmfts" submission found; `drivers/input/touchscreen/stmfts.c` upstream is still the 8-byte-event FingerTipS driver. |
| Local: `/run/media/jstockdale/writable/gts9u-scratch/touch-src` | `listing.txt` names `drivers/input/touchscreen/stm/fts1ba90a/{fts_ts.c,fts_ts.h,fts_fwu.c,fts_sec.c}` but only `drivers/input/sec_input/{sec_input.h,sec_cmd.h}` were actually extracted.  The full SM-X710 release is now at `~/gbu/samsung-gts9u/workdir/downloads/kernel-samsung-gts9wifi/`. |
| Local: `~/projects/ubuntu-touch-gts9` | Samsung kernel import under `devices/gts9pwifi/imports/kernel-samsung-gts9wifi` has no `drivers/input/touchscreen`; the X810 stock DTS there (`gts9pwifi_eur_open_w00_r04.dts`) gives the `stm,fts_touch` node, `fts1ba90a_attn_irq` pin states (gpio25, input-enable, bias-disable; bias-pull-down in suspend) and `sec,max_coords <0x6d8 0xaf0>` (1752x2800) for the 12.4" S9+.  No mainline-side touch work. |
| Samsung downstream for cross-checking | `fts_ts.c`/`fts_ts.h` from the GitHub mirror [ianmacd/winnerx](https://github.com/ianmacd/winnerx) (Galaxy Fold, older 8-byte-event firmware generation): same opcodes (0x22/0x23/0x24/0x30/0x13/0x60/0x61/0x62/0xA0, 0xFA reset), same `fts_init` order and delays, same `fts_wait_for_ready` loop.  Event size differs (8 vs 16), so the 16-byte layout above is taken from the three Tab S8/S9 ports, which agree with each other and with `sec_input.h`. |

## Guesses and open points

- IRQ polarity is not a guess: stock pin state is input/bias-disable,
  downstream hardcodes `IRQF_TRIGGER_LOW`, and all three sibling ports use
  `IRQ_TYPE_LEVEL_LOW`.
- Orientation flags are taken from the SM-X710-validated nacht20-de DTS; they
  assume the panel is exposed as 2560x1600 landscape, which this repo's
  `panel-samsung-ana38407.c` gts9wifi mode does (hdisplay 2560, vdisplay
  1600).
- Supply voltage: the stock X710 DTS names L14B for `tsp_avdd_ldo`.  This
  repo's `vreg_l14b_3p2` is pinned to 3.2 V (min = max = 3200000), matching
  the stock vote the X716B port settled on after root-causing a touch crash to
  a wrong AVDD value; nacht20-de's label says 3.3 V but the IC tolerates both.
- L12B is shared with the panel's VDDIO, which is why the screen-off state is
  sense off rather than downstream's power-off: the touch keeps holding L12B
  while the screen is blank, as it always has here.  Downstream can power off
  because its display stack sequences VDDIO itself.
- Not yet verified on hardware when this section was written: the low-power
  IRQ rate, the gesture library being present in firmware 0x0124 (nacht20-de
  validated the same sequence on an SM-X710 without recording its firmware),
  and wake from a real s2idle sleep, which waits on the suspend work.
- The force-calibration step (0x13) on every probe is what Samsung's
  `fts_init` does; nacht20-de's X710 driver skips it and still works, so it
  can be dropped if it ever misbehaves.
