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
drivers bind them.  CAMSS registers nothing until every sensor in its graph has
bound, so for `rear` or `front` the changeset also detaches the other camera's
CAMSS endpoint.  It is one-shot until the next reboot (CAMSS is not built to be
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
| SM-X710 | `GTS9-Front`, then `GTS9-Rear`, numbered from 20, only for HI1337s that are bound |
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
from this tree.  The kit is `tools/inplace/cameras/` (README there).

Phase A, no phantom cameras:

```sh
# host
sh tools/inplace/cameras/make-kit.sh /tmp/cameras-kit
scp -r /tmp/cameras-kit jstockdale@10.42.0.2:cameras-kit
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
echo both | sudo tee /etc/ubuntu-gts9u/cameras && sudo reboot   # cameras at every boot
```
