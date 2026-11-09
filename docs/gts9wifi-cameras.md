# Galaxy Tab S9 (SM-X710, gts9wifi): cameras

The X710 has two cameras, a rear 13 MP and a front 12 MP ultra-wide.  Both are
SK hynix HI1337 modules wired exactly like two of the Ultra's four, so the
Ultra's kernel drivers and userspace cover them; what the X710 needed was its
own device-tree description, a way to keep it off until it is proven, and a
camera stack that stops assuming four cameras on every board.

## What the X710 images got wrong

Device package 2.55 installed the Ultra's camera stack unchanged: a
`modules-load.d` entry and fixed `modprobe.d` options loaded `v4l2loopback` with
four nodes (`/dev/video20`-`23`, `GTS9U-Front-Ultra-Wide`, `GTS9U-Front-Main`,
`GTS9U-Rear-Main`, `GTS9U-Rear-Ultra-Wide`), and four relays served them.  The
X710 kernel has no camera enabled, so every browser and OBS listed four webcams
that only ever delivered the relays' black splash, and the four idle relays
cost CPU for nothing.

## The hardware

From the stock gts9wifi DT overlay (all four `dtbo` entries agree; the unit is
board-id 04, hw rev 6) and the stock CamX blobs of X710XXS5CYG1:

| | rear (`qcom,cam-sensor0`) | front (`qcom,cam-sensor1`, `-sensor12`) |
|---|---|---|
| sensor | HI1337, `com.samsung.sensormodule.0_hynix_hi1337` | HI1337, `1_hynix_hi1337_front` (2032x1524 binned) and `12_hynix_hi1337_front_full` (4000x3000) |
| bus | CCI0 master 1, 8-bit 0x42 = `0x21` | CCI1 master 1 (AON pair GPIO208/209), 8-bit 0x40 = `0x20` |
| CSI | CSIPHY1, 4 lanes | CSIPHY4, 4 lanes |
| MCLK | MCLK2, GPIO102, 19.2 MHz | MCLK4, GPIO104 (`cam_aon_mclk4`), 19.2 MHz |
| reset | GPIO70 (`CAM_RESET0`) | GPIO117 (`CAM_RESET1`) |
| module enable | GPIO15 `RCAM_LDO_EN` | GPIO17 `FCAM_LDO_EN` |
| VIO | L4B 1.8 V (`pm_humu_l4`) | L4B 1.8 V |
| VDIG | L1C 1.1 V (`pm_v6c_l1`) | L11B 1.1 V (`pm_humu_l11`) |
| lens | DW9808 VCM, CCI1 master 0, 8-bit 0x18 = `0x0c`, on `RCAM_LDO_EN` | fixed focus |
| calibration | P24C256F EEPROM, CCI1 master 0, 8-bit 0xb0 | OTP in the sensor |
| placement | roll 90, yaw 180: `rotation = <90>`, back | roll 270, yaw 0: `rotation = <270>`, front |

That is the Ultra's rear-main and front-main wiring to the pin (compare the
X910 overlay's `cam-sensor0` and `cam-sensor1`).  The CSIPHY rails are the
Ultra's too: L1E 0.88 V and L3E 1.2 V.

### Same sensor tables as the Ultra

`hi1337_gts9u_tables.h` was decoded from the X910's blobs.  The X710 blobs
serialise their register tables the same way (Parameter Parser V3: one
ten-word record per write with the address inline, the `registerData` arrays
de-duplicated into a pool after each table).  Comparing address sequences and
value pools (`scripts/check-hi1337-tables.py`):

* the 1476-entry global table: same addresses in all three X710 blobs, and
  their value pools are exactly what the driver's values produce;
* rear `0_hynix_hi1337` mode 0, "4128x3096 1408Mbps 10bit 30fps": identical to
  the X910 rear-main table;
* front `12_hynix_hi1337_front_full` mode 0, "4000x3000": identical to the X910
  front ultra-wide table.  The X710's front module is programmed exactly like
  the Ultra's front ultra-wide, and sits on the Ultra's front-main bus.

So the DT uses `hynix,hi1337-gts9u-rear` for the rear and
`hynix,hi1337-gts9u-front-uw` for the front, and no driver change is needed.
The front's binned 2032x1524 mode (`1_hynix_hi1337_front`, which stock uses for
preview) is not in the driver; it would be a second mode for that variant
(about a hundred lines with the multi-mode plumbing, plus its decoded table),
worth it only for power.

## Mainline status

Everything is in the 7.2-rc3 tree the port builds, already built in on the X710
(`config-gts9wifi.fragment`): CAMSS for SM8550 (`qcom,sm8550-camss`), the CCI
(`qcom,sm8550-cci`), the camera clock controller, and the port's own
`hi1337_gts9u.c` and `dw9808_vcm.c`.  The HI1337 driver is not upstream; it is
this port's (see `kernel/PROVENANCE.md`).  No sensor needs porting.

## Off by default: the device tree and `gts9wifi_camera`

The camera section at the end of `sm8550-samsung-gts9wifi.dts` describes both
modules, the lens, the rear VIO switch (GPIO15 as a fixed regulator shared by
sensor and lens), CCI1's second bus on the AON pins, and the two CAMSS
endpoints.  CAMSS, CCI0, CCI1, the three I2C devices and the VIO switch are all
`status = "disabled"`.  The only things that exist at boot are two idle
regulators, L4B and L1C (registered, never enabled until a camera powers up).
So the boot path -- the SoC hand-off, the boot-time `pm_test` panel cycle, the
interconnect hold -- sees no camera device at all.

`gts9wifi_camera` (`kernel/drivers/gts9wifi_camera.c`, built in on the X710 only,
`CONFIG_VIDEO_GTS9WIFI_CAMERA`) turns them on at runtime:

```sh
echo rear | sudo tee /sys/module/gts9wifi_camera/parameters/enable   # or front, both
cat /sys/module/gts9wifi_camera/parameters/enable                     # none / rear (pending) / rear
```

It applies one OF changeset that sets the chosen nodes to `okay`; the OF
reconfiguration notifiers create the platform and I2C devices and the built-in
drivers bind them.  CAMSS registers `/dev/media0` at probe, but its async
notifier only completes -- sensor links and the `v4l-subdev` nodes libcamera
needs -- once every sensor in its graph has bound, so for `rear` or `front` the
changeset also detaches the other camera's CAMSS endpoint (a sensor that
probes before CAMSS exists defers on that remote endpoint and is retried when
CAMSS binds).  It is one-shot until the next reboot (CAMSS is not built to be
torn down by reverting a changeset), it refuses on any other board, and every
node path and compatible is checked before anything changes.
`gts9wifi_camera.enable=...` on the kernel command line applies it at late
initcall instead, which puts the cameras back on the boot path; not
recommended.

`ubuntu-gts9u-camera-enable.service` applies the choice in
`/etc/ubuntu-gts9u/cameras` (`rear`, `front` or `both`) at every boot, after the
panel's boot cycle and before the loopback nodes are built.  Without that file
it does nothing.

## The camera table

One table now drives the userspace camera stack, from
`ubuntu-gts9u-camera-profile`:

| board | table |
|---|---|
| SM-X910 | the four `GTS9U-*` cameras at `/dev/video20`-`23`, unconditionally: the same numbers, names, sources and module options as before |
| SM-X710 | `GTS9-Front`, then `GTS9-Rear`, numbered from 20, only for HI1337s that are bound *and* have their `v4l-subdev` node (CAMSS creates those only once its async notifier completed, lens included), so a half-probed camera never becomes a webcam |
| other | none |

`ubuntu-gts9u-camera-loopback.service` (replacing the `modules-load.d` and
`modprobe.d` pair) writes it to `/run/ubuntu-gts9u/camera-profile` and loads
`v4l2loopback` with one node per row; an empty table loads nothing, and nodes
this port created that no longer match are removed when nothing holds them.
The relays and the udev refresh read the same file, and the relay unit is
skipped when it is empty (`ConditionFileNotEmpty=`).  udev gives the X710 nodes
`/dev/gts9u-camera/{front,rear}` and `/dev/v4l/by-id/platform-gts9-*` aliases.

The two X710 cameras have the same libcamera node names as the Ultra's
front-main and rear-main (same buses and addresses), and WirePlumber's config
sandbox cannot read the device tree, so the PipeWire labels in
`51-gts9u-camera-backends.lua` are chosen when the package is built:
`build-device-package.sh` rewrites its `gts9u_board` word to `DEVICE`, as it
already does for the sensor registry path.

`tests/test_camera_stack.py` runs the table, the loader, the udev refresh and
the enable helper against a fake root for both boards.

## Open questions and risks

* **Front VDIG voltage.**  Every stock X710 overlay entry sets L11B
  (`pm_humu_l11`) to 1.1 V; this tree holds L11B at 1.2 V for the panel, and
  the HI1337 driver sets no voltage, so the front sensor's core would run at
  1.2 V.  The panel's own L11B note says stock leaves 1.2 V; the overlay says
  otherwise.  Test the rear first; decide on the front with whoever owns the
  panel rail.
* **GPIO17.**  On the Ultra the front module enable is shared with the DMIC
  path, which is why the driver holds its GPIOs only while streaming.  On the
  X710, check the microphones after the first front-camera run.
* **ABL and the DTB.**  The Ultra's ABL rejects any DTB whose structure moves;
  the X710's has accepted every change so far, and this one adds nodes and
  renumbers phandles.  If an X710 ABL ever proves as strict, the same
  description can move into a kernel-applied overlay (the family overlays in
  `stage-performance-overlay.sh` already work that way), with the X710 DTB
  left untouched.
* **Calibration.**  Neither the rear EEPROM nor the front OTP is read; the
  software ISP uses the generic `hi1337-gts9u.yaml` tuning, as on the Ultra.

## Test plan

Phase A needs nothing but the running system; Phase B needs a kernel built
from this tree.  The kit is `tools/inplace/cameras/` (README there), a record
of the tests on the test unit; Phase A applies to a tablet still on device
package 2.55.  `<user>` is the desktop user on the tablet, and 10.42.0.2 is
the development USB gadget's address (an image built with
`GTS9U_USB_GADGET=1`).

Phase A, no phantom cameras:

```sh
# host
sh tools/inplace/cameras/make-kit.sh /tmp/cameras-kit
scp -r /tmp/cameras-kit <user>@10.42.0.2:cameras-kit
# tablet
v4l2-ctl --list-devices                 # before: four GTS9U-* devices
sudo sh ~/cameras-kit/install.sh        # "removed stale camera nodes", "0 camera(s)"
sh ~/cameras-kit/verify.sh              # all ok; no loopback nodes; relays not running
systemctl status ubuntu-gts9u-camera-relays   # inactive, ConditionFileNotEmpty not met
```

Phase B, first light (one camera per boot):

```sh
cat /sys/module/gts9wifi_camera/parameters/enable      # none: nothing probed at boot
sudo sh ~/cameras-kit/camera-test.sh rear              # then read summary.txt
sudo reboot
sudo sh ~/cameras-kit/camera-test.sh front             # then check the microphones
sudo reboot
sudo sh ~/cameras-kit/camera-test.sh both
sudo install -d /etc/ubuntu-gts9u                               # no package creates it
echo both | sudo tee /etc/ubuntu-gts9u/cameras && sudo reboot   # cameras at every boot
```

## First light: rear (2026-11-08)

On squirtle (CX kernel `bfb40d20…`, device 2.58), `camera-test.sh rear` gave
the following. Results are in `/var/log/gts9u-cameras-test/20261108-120850-rear`.
- The switch went from `none` to `rear`; the camera was ready after 1 s.
- The HI1337 identified (`rear-main model=0x1337 vendor=0x2000`) at
  `13-0021` on CCI0, and the DW9808 bound at `14-000c` on CCI1.
- `/dev/media0` (qcom-camss, 49 entities). The sensor joined the graph as
  `v4l-subdev30`; the lens is `v4l-subdev31`.
- libcamera's "Internal back camera" captured 60 frames at 640x480 BGR888
  through the software ISP. The last frame is a real, upright view of the
  room: dark (mean 40/255) and soft, with colour plausible.
- PipeWire has one libcamera node; the table is `20 GTS9-Rear …`. The owner
  saw the picture in the Camera app.
- Exposure (4–3260) and analogue gain (0–240) are live, and libcamera's AGC
  drives them.

Not working:
- Focus. The DW9808's first register write got a CCI timeout at probe
  (`i2c-qcom-cci ac16000.cci: master 0 queue 0 timeout`, then
  `dw9808-vcm 14-000c: register 0x02 write failed: -110`). Opening
  `/dev/v4l-subdev31` then fails with `EINVAL`, and libcamera logs "Lens
  initialisation failed, lens disabled", so the lens sits at rest. The
  causes are in "Focus: two lens faults" below.
- The `/dev/video20` relay (the browser path) served only its black splash
  within the script's 5 s read (mean luma 16, spread 0).

Front: not tried yet. Its open risks are listed above.

## Focus: two lens faults (2026-11-08)

`lens-diag.sh` and the first lens kit (400 kHz) show two faults, both around
the lens's power cycles. Neither is the bus speed.

**1. The first write after a power-up times out.** It is the first write of
`dw9808_init()`, register 0x02: `i2c-qcom-cci ac16000.cci: master 0 queue 0
timeout`, then `dw9808-vcm 14-000c: register 0x02 write failed: -110`.
- It hit the probe-time power-up in both boots, at 1 MHz (`0824d1f3`) and at
  400 kHz (`0a434817`).
- At 400 kHz, after a rebind, the cold open and three more power-ups worked,
  and the fourth failed.
- The failed resume is latched by runtime PM. Every later open returns
  `EINVAL` without touching the bus, until the driver is rebound, and
  libcamera disables the lens ("Lens initialisation failed").

The driver waits 1 ms after enabling `rear_cam_vio`. Samsung's CamX data for
this actuator (`com.samsung.sensormodule.0_hynix_hi1337.bin`, X710XXS5CYG1;
the SM-X910's is the same word for word) waits 10 ms. Its power records are
(type, value, delay) triples:

| sequence | record | meaning |
|---|---|---|
| power-up | `13, 1, 1` | `RCAM_LDO_EN` high, 1 ms |
| power-up | `4, 1800000, 10` | VIO 1.8 V, 10 ms |
| power-down | `4, 0, 1` | VIO off, 1 ms |
| power-down | `13, 0, 1` | `RCAM_LDO_EN` low, 1 ms |

The type numbers come from the sensor's and the EEPROM's records in the same
file: 4 is 1.8 V, 3 is VDIG 1.1 V, 0 is MCLK 19.2 MHz, and 13 is the GPIO. The
delays are CamX's milliseconds.

**2. A park that finds the lens settling leaves it "active" and unpowered.**
`dw9808_suspend()` walks the lens back, then switches the rail off and
returns the walk's error. Its status poll gives up after 10 ms with `-EBUSY`,
and for `-EBUSY` runtime PM puts the device back to `RPM_ACTIVE` without
latching an error. The next open then skips `dw9808_resume()` and talks to a
lens with no power.

The 1 MHz sweep shows the whole chain. Each of its steps was a separate
`v4l2-ctl` open, so a separate power cycle:
- The step to 0 jumped the lens 512 codes and closed at once.
- The step to 256's move found the lens "busy" (`EBUSY`).
- Its park then timed out 36 times on CCI1, the standby write failed, and
  `regulator_disable()` warned "unbalanced disables for rear_cam_vio" from
  `dw9808_suspend`.

The warning means the 256 step's open never ran `dw9808_resume()`. The device
must have stayed active after the step to 0's park, and that park's status
poll is the only source of `-EBUSY`. That is what looked like a stuck bus.

**Ruled out:**
- The lens bus speed. 400 kHz, stock's `i2c-freq-mode 1`, failed the same
  way; the bus is back at the dtsi's 1 MHz.
- The X710's actuator data against the X910's. They differ only in the module
  id and the signature.
- The pad settings. Stock gives CCI1 master 0 4 mA and a weak pull-up, and
  the dtsi gives 2 mA and the strong 2.2 kOhm pull-up. The Ultra focuses with
  the dtsi's pads, on the same lens, rail, 1 MHz bus and driver, so the pads
  stay.

**The fixes:**
- Fault 1, test pending. `rear_cam_vio` gets `startup-delay-us = <10000>`,
  shipped as the [lens power-up kit](../tools/inplace/cam-lens/README.md)
  (DTB `164e1bf8…`). The HI1337 holds the same rail and gains 10 ms at
  power-up.
- Fault 2, built, not yet on a tablet. `dw9808_vcm` now returns 0 from its
  suspend once the rail is off, warning "lens not parked before power-down".
  A failed open also clears the latched error, so the next open powers up
  again. The driver is built in (`CONFIG_VIDEO_DW9808_VCM=y`), so this needs
  the next kernel build. It changes the Ultra's driver too, in error paths
  only.
- `lens-diag.sh` now holds the lens open for its sweep. It then runs 20 power
  cycles that close only once the lens has settled, which tests fault 1 on
  its own, and up to 6 quick cycles, which reproduce fault 2 on the CX
  kernel.

### The power-up kit on squirtle (boot `9c0e3019`)

The running DTB has `startup-delay-us = 10000` on `rear_cam_vio`
(`/proc/device-tree`). `camera-test.sh rear`
(`/var/log/gts9u-cameras-test/20261108-134534-rear`):
- The lens powered up at probe with no error. The kernel log has no `dw9808`
  or CCI line for the whole boot; both earlier boots failed here.
- libcamera kept the lens: there was no "Lens initialisation failed".
  PipeWire had its camera node without a restart.
- The `/dev/video20` relay served a real frame (mean luma 81, spread 169).
  First light's black-splash result did not come back.
- PipeWire and WirePlumber then hold the lens's subdev open, so the lens and
  `rear_cam_vio` stay powered while PipeWire runs, camera in use or not.

The owner opened GNOME Camera (Snapshot 46.2) twice:
- First open: the picture focused for about a second, then glitched and
  froze. The log shows five stream configurations within 1.3 s (640x480,
  then 1280x1080 four times), then nothing. No kernel or PipeWire error, no
  lens or CCI error, and no software-ISP benchmark line, which other sessions
  print about 2.5 s after their last configuration. The stream had stalled.
- Second open: it focused (owner-confirmed). During its own burst of
  configurations, CAMSS logged once "Received wm done for unmapped index".

The stall looks like the capture path under fast reconfiguration, not the
lens; that is a hypothesis. On the Ultra the relay needed a 2 s guard against
the same kind of churn (porting log, session 32). The frame-done interrupt is
`acb7000.isp_msm_csid0`. If its count keeps rising during a freeze, frames
still arrive and userspace stalled. If it stops, CAMSS stalled.

`lens-diag.sh` on the same boot (`20261108-141752-lens`; it paused PipeWire
and the relays, then started them again):
- the cold open: OK;
- the sweep with the lens held open, 0, 256, 512, 768, 1023 and 512: all OK;
- 20 power cycles, each closed 0.2 s after its move: 20 of 20 OK;
- quick cycles, each closed right after its move: cycle 1 was OK. Cycle 2's
  power-up failed the old way, with a CCI timeout, then `register 0x02 write
  failed: -110` and the open timing out. There was no "unbalanced disables",
  so cycle 1's park did not hit fault 2.

So the 10 ms holds for a power-up after a clean power-down. It does not
cover a power-up that follows a power-down taken while the lens was still
moving. Every failed sweep step before the kit had that history too: the
old sweep closed right after each jump. Why the move matters is not known.
In normal use PipeWire keeps the lens open, so this only arises when
PipeWire stops or restarts. The failed resume is latched (`error`), so
PipeWire's libcamera could not reopen the lens after the test: focus is off
until the next boot, or until the driver fix lets the next open power up
again.

### Smooth focus (2026-11-08)

With focus working, the owner heard the lens click, and saw focus move
periodically and jerkily. The cause was the shared libcamera autofocus, not
this board. It is replaced by SoftAf (libcamera-gts9u patch 0009); see
[camera focus](camera-focus-and-enumeration.md#smooth-continuous-autofocus-patch-0009-2026-11-08).

### CCI timeouts: two problems, not one (2026-11-08)

On boot `cc67dab2` the kernel logged two CCI timeouts.

**1. The lens's first write after a power-up**, 18:08:08.840. It came 0.35 s
after the sensor's probe had switched the shared rail on and off. Thousands of
autofocus writes on a powered lens went through today without one timeout.
Only a write right after a power-up fails, and mostly when the power-down
before it was recent or taken mid-move. So this is the chip's state after a
quick power cycle, not the bus.
- Leading hypothesis: the dtsi's strong 2.2 kOhm CCI pull-ups to the SoC's
  1.8 V back-power the unpowered module through its I/O pins (L4B, its VIO,
  goes off with it: use count 0), so the DW9808 never fully resets. Stock's
  weak pull-up would not do that.
- Test: the stock pad settings (4 mA, weak pull-up) on CCI1 master 0 as a
  DTB-only kit, then `lens-diag.sh`'s quick cycles.

**2. The sensor's stream-off write, during a restart burst**, 18:30:38.862.
This was the Snapshot freeze, reproduced by the full-resolution-photos
session's replica of stock GNOME Camera:
- `qcom-camss acb7000.isp: Received wm done for unmapped index`, then 0.6 ms
  later `i2c-qcom-cci ac15000.cci: master 1 queue 0 timeout`, then
  `hi1337-gts9u 13-0021: disabling streaming failed (-110)`;
- frames stopped (csid0 flat from 18:30:47) until a new stream at 18:31:28.

The stale capture interrupt and the I2C timeout were handled within a
millisecond of each other, about 100 ms (`CCI_TIMEOUT`) after the write
started. That fits interrupts on that CPU being held up, rather than a stuck
bus, but it is not proven.

Every device interrupt on squirtle is handled on CPU0, a Cortex-A510:
irqbalance is not installed. That covers the display, the GPU, the BT UART
(about 90/s), RPMh, both CCIs, VFE and CSID.

- Test: log the CCI's interrupt status when a transfer times out (a debug
  patch to `i2c-qcom-cci`). Separately, move the camera interrupts to a big
  core.

Snapshot patched not to reconfigure while shooting avoids the bursts: 30 of
30 full-resolution photos in three runs, with no camera kernel error.
