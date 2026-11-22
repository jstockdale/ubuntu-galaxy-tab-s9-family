# In-place kits: records of SM-X710 experiments

Each directory here is the record of an in-place experiment on one SM-X710
test unit (squirtle) in November 2026.  A kit copied files, or a rebuilt
`vendor_boot`, onto a tablet that was already installed, so that one change
could be tried without a new release.  They are kept because the SM-X710 docs
cite their procedures and results.  They are not an install or update path.

- **Superseded where they were fixes.**  What the boothygiene, cameras,
  icc, powerthermal, suspendkit and wifi kits installed by hand now ships in
  `ubuntu-gts9u-device` 2.56 and 2.57 or in the image's package list, and the
  Wi-Fi kit's `vendor_boot` change (the hw2.1 firmware in the ramdisk) is in
  the images this branch builds.  The flags and panel kits, and the icc kit's
  weekend experiment, flashed `vendor_boot` variants that no build carries:
  the three `*_ignore_unused` flags are still in
  `configs/vendor_boot/cmdline.txt`, and DISP-5 is open.  2.56 shipped the
  fingerprint-gate generator as `-rw-r--r--`, so on the SM-X710 fprintd still
  timed out after 25 s at the greeter; 2.57 fixes that.
- **They need state that exists only on that unit.**  This includes the
  unpublished fix1 and fix2-s2 images (the `~/display-exp` sets), the `~/fix1`
  kit and device package 2.55.  The images and the fix1 kit are not published;
  device package 2.55 is the version set in commit 0cfa883.  Most
  install scripts check for them and stop otherwise.
- **To install or update**, use the ZIP and the device package as the
  top-level [`README.md`](../../README.md) describes ("Installing on the
  SM-X710", "Updating an existing installation").

| kit | what it tried | results |
|---|---|---|
| [`boothygiene/`](boothygiene/README.md) | the journal's missing first 39 s, rtc-restore order, the fingerprint stack gated to the SM-X910, the pstore archive, the microSD logger | device package 2.56/2.57; [`docs/porting-log.md`](../../docs/porting-log.md) |
| [`cameras/`](cameras/README.md) | a per-board camera table (no phantom webcams), first light for the two HI1337s | [`docs/gts9wifi-cameras.md`](../../docs/gts9wifi-cameras.md) |
| [`flags/`](flags/README.md) | `vendor_boot` without each of the three `*_ignore_unused` flags | [`docs/gts9wifi-bringup-flags.md`](../../docs/gts9wifi-bringup-flags.md) |
| [`icc/`](icc/README.md) | the interconnect late release, and the weekend experiment | [`docs/gts9wifi-interconnect.md`](../../docs/gts9wifi-interconnect.md) |
| [`panel/`](panel/README.md) | the DISP-5 panel cold-boot experiments | [`docs/gts9wifi-display.md`](../../docs/gts9wifi-display.md) |
| [`powerthermal/`](powerthermal/README.md) | rtkit, the greeter's WirePlumber snippet, charging and thermal checks | the kit's README; [`docs/porting-log.md`](../../docs/porting-log.md) |
| [`ufs-g5/`](ufs-g5/README.md) | the UFS link at HS-G5 Rate-A (a kernel and its module tree on top of fix7-ice) | [`docs/porting-log.md`](../../docs/porting-log.md#why-the-link-stops-at-hs-g4-2026-11-07) |
| [`ufs-cx/`](ufs-cx/README.md) | the UFS, USB and PCIe CX votes reaching the rail and dropped for sleep (on top of the G5 kit) | [`docs/porting-log.md`](../../docs/porting-log.md#the-cx-vote-kit-on-top-of-the-g5-kit-2026-11-07) |
| [`cam-lens/`](cam-lens/README.md) | the rear lens rail's 10 ms power-up delay, a DTB-only change (on top of the CX kit) | [`docs/gts9wifi-cameras.md`](../../docs/gts9wifi-cameras.md) |
| [`bt-uart/`](bt-uart/README.md) | the geni UART interrupt served while the port suspends, and the Bluetooth suspend test (on top of the lens kit) | [`docs/porting-log.md`](../../docs/porting-log.md#the-geni-uart-handler-ignored-its-interrupt-while-the-port-suspended-2026-11-08) |
| [`cam-pads/`](cam-pads/README.md) | stock's pad settings on the rear lens's CCI pins, a DTB-only test of the lens's power-up failures (on top of the BT UART kit) | [`docs/gts9wifi-cameras.md`](../../docs/gts9wifi-cameras.md#cci-timeouts-two-problems-not-one-2026-11-08) |
| [`cam-bus/`](cam-bus/README.md) | the rear camera's two buses as stock sets them (400 kHz, 4 mA, weak pull-up), replacing `cam-pads` (on top of the BT UART kit) | [`docs/gts9wifi-cameras.md`](../../docs/gts9wifi-cameras.md#cci-timeouts-two-problems-not-one-2026-11-08) |
| [`hang-log/`](hang-log/README.md) | hung-task, lockup and workqueue detectors, netconsole, and a ramoops region with pmsg, ftrace, ECC 96 and plain panic records, so that a system-wide hang leaves evidence; `cameras/hang-capture.sh` for risky tests (on top of the camera-bus kit) | [`docs/porting-log.md`](../../docs/porting-log.md#the-0019-hang-left-a-console-record-2026-11-10) |
| [`typec-roles/`](typec-roles/README.md) | the kernel changes for powered USB-C docks: the tablet asks for the USB host role itself before discovery, Try.SNK waits for the SM5714 to report a source, three fixes for a swap asked for from userspace, the USB power domains stay in retention across suspend (a boot with the dock attached), and a NULL check in the dwc3-qcom glue; a plug-in recorder with a verdict for each plug-in; since revision 8 the UFS PHY's PCS reset before every calibration on this board too, with a register diagnostic, and a deep-sleep test with every disk filesystem frozen and the UFS disk read-only at the block layer (on top of the hang-log kit) | [`docs/usb-c-dock-roles.md`](../../docs/usb-c-dock-roles.md), [`docs/resume-recovery.md`](../../docs/resume-recovery.md#sm-x710-gts9wifi) |
| [`suspendkit/`](suspendkit/README.md) | suspend reliability runs, the ADSP-start sleep lock | [`scripts/diagnostics/suspend/README.md`](../../scripts/diagnostics/suspend/README.md) |
| [`wifi/`](wifi/README.md) | the Wi-Fi address from EFS, firmware at the first probe, the L1 substates | [`docs/gts9wifi-wifi.md`](../../docs/gts9wifi-wifi.md) |

One kit is for the SM-X910 and is not superseded.  It goes on top of release
v1.2.5 and needs nothing else on the tablet:

| kit | what it tries | results |
|---|---|---|
| [`typec-roles-ultra/`](typec-roles-ultra/README.md) | the kernel changes for powered USB-C docks: the tablet asks for the USB host role itself before discovery, Try.SNK waits for the SM5714 to report a source, three fixes for a swap asked for from userspace, the USB power domains stay in retention across suspend (a boot with the dock attached), and a NULL check in the dwc3-qcom glue; a plug-in recorder with a verdict for each plug-in | [`docs/usb-c-dock-roles.md`](../../docs/usb-c-dock-roles.md) |

The commands in the kit READMEs use `<user>@10.42.0.2`, where `<user>` is the
desktop user on the tablet.  10.42.0.2 is the tablet's end of the development
USB network, which only an image built with `GTS9U_USB_GADGET=1` has
([`docs/ubuntu-userspace.md`](../../docs/ubuntu-userspace.md)).  Over Wi-Fi,
use the tablet's address instead.
