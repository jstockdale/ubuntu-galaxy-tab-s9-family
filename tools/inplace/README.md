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
| [`suspendkit/`](suspendkit/README.md) | suspend reliability runs, the ADSP-start sleep lock | [`scripts/diagnostics/suspend/README.md`](../../scripts/diagnostics/suspend/README.md) |
| [`wifi/`](wifi/README.md) | the Wi-Fi address from EFS, firmware at the first probe, the L1 substates | [`docs/gts9wifi-wifi.md`](../../docs/gts9wifi-wifi.md) |

The commands in the kit READMEs use `<user>@10.42.0.2`, where `<user>` is the
desktop user on the tablet.  10.42.0.2 is the tablet's end of the development
USB network, which only an image built with `GTS9U_USB_GADGET=1` has
([`docs/ubuntu-userspace.md`](../../docs/ubuntu-userspace.md)).  Over Wi-Fi,
use the tablet's address instead.
