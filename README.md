# Ubuntu 24.04 LTS for Samsung Galaxy Tab S9 Ultra Wi-Fi

<img width="4000" height="2252" alt="20260911_124542" src="https://github.com/user-attachments/assets/80a44675-d588-43b5-b86d-514cd7875800" />

Ubuntu 24.04 LTS arm64 for the Samsung Galaxy Tab S9 Ultra Wi-Fi
(`SM-X910`, `gts9uwifi`), running GNOME 46 on Wayland and upstream Linux
7.2-rc3.

The same source tree also builds for the Galaxy Tab S9 Wi-Fi (`SM-X710`,
`gts9wifi`) with `DEVICE=gts9wifi`. That port is experimental, installs on the
whole tablet only, and has no published ZIP: you build it yourself. See
[Galaxy Tab S9 (SM-X710)](#galaxy-tab-s9-sm-x710).

## Hardware compatibility (SM-X910)

| Component | Status | Summary |
|---|---:|---|
| Display | ✅ | Native 2960×1848 at 120 Hz |
| Desktop | ✅ | GNOME 46, Wayland and GDM |
| GPU | ✅ | Adreno 740 hardware acceleration, up to 719 MHz |
| Touchscreen | ✅ | Goodix multitouch |
| S Pen writing | ✅ | Hover, pressure, tilt, side button and palm rejection |
| S Pen dock | ✅ | Insertion, orientation and charging |
| S Pen BLE | ✅ | Pairing, battery, air gestures and pointer mode |
| Tab Companion | ✅ | Device settings, fingerprint, dual boot and system updates |
| Keyboard cover | ✅ | EF-DX920 tested |
| Cover switch | ✅ | Suspend on close and wake on open |
| Power and volume buttons | ✅ | Power, wake and volume controls |
| Wi-Fi | ✅ | WCN7850 / ath12k |
| Bluetooth | ✅ | Controller, audio and S Pen BLE |
| Speakers and microphones | ✅ | Four speakers and digital microphones |
| Vibration / haptics | ✅ | On-screen keyboard feedback |
| Motion sensors | ✅ | Rotation, accelerometer, gyroscope and compass |
| Battery telemetry | ✅ | Charge, voltage, current and temperature |
| USB-PD/PPS charging | ✅ | Up to 25 W measured into the battery |
| Suspend / resume | ✅ | Deep sleep and reliable storage recovery |
| microSD | ✅ | Read and write storage works normally |
| USB host | ✅ | HID and storage, powered or unpowered |
| USB-C DisplayPort | ✅ | External video output |
| Cameras and flash | ✅ | Four cameras, full field of view, autofocus and flash |
| Fingerprint reader | ✅ | Login, enrolment and recognition |
| Ambient light sensor | ✅ | Automatic screen brightness |
| NPU | ✅ | Hardware acceleration support |
| Virtual machines | 🟡 | Gunyah host support; guest VMs still in development |
| Waydroid | ✅ | Android with Google Play, GPU acceleration and Internet |

✅ working on the physical tablet · 🟡 experimental or partially validated ·
❌ unavailable · ❓ not tested

The evidence and limitations are documented in
[hardware-status.md](docs/hardware-status.md) for most components, and in
[npu-status.md](docs/npu-status.md), [gunyah.md](docs/gunyah.md) and
[waydroid.md](docs/waydroid.md) for the NPU, virtual machines and Waydroid.

This table describes the current source tree; previously published installation
ZIPs may not include the latest changes. The shared changes made with the
SM-X710 port (the S Pen decode, tilt included, the kernel configuration and
patches, the panel, pogo, charger and battery drivers, and the device package
2.56 units) have not yet been built and booted on an SM-X910.

## Tab Companion
<img width="2164" height="1764" alt="Screenshot from 2026-08-20 04-02-55" src="https://github.com/user-attachments/assets/df7cddca-46e8-438a-bcb8-be713cc93bed" />

Tab Companion provides:

- full S Pen settings (pairing, gestures and air-pointer mouse mode);
- remapping for compatible Samsung Book Cover Keyboard keys;
- adjustable vibration feedback for GNOME's on-screen keyboard and optional notification vibration;
- fingerprint enrolment with progress, deletion and recognition tests, using the same fingerprints as Ubuntu Settings and login;
- dual boot to Android, with an optional toggle in quick settings.

The air-pointer concept is inspired by
[PenMouse S](https://github.com/jojczak/PenMouseS) by Jakub J (`@jojczak`).
Tab Companion uses an independent native Linux implementation.

## Installation

Both ways use the same installation ZIP. The only difference is whether you
split the UFS first. On the SM-X710 only "Ubuntu on the whole tablet" is
available: `gts9u-split.zip` refuses any tablet that is not an SM-X910, so dual
boot, the Dualboot app and switching systems do not apply to it.

> [!WARNING]
> Either way, **everything Android keeps in `userdata` is erased**: its apps,
> its settings and its files. Android's system itself is not touched. Back up
> anything you want to keep first. On another note, the project seems really stable so far, but as always: I'm not responsible for bricked devices, missing recovery partitions, nuclear wars or you getting fired because you forgot to boot back into android for the alarm.

### Before you start

1. **An unlocked bootloader.** In Android, enable Settings → Developer options →
   OEM unlocking. Then enter Download mode (with the tablet off, hold Volume Up +
   Volume Down while connecting the USB cable; from a running One UI,
   `adb reboot download` also works) and long-press Volume Up on the warning
   screen to unlock. Unlocking wipes Android.
2. **Root with Magisk** (only if you want dual boot, so SM-X910 only), done
   *before* you flash the Ubuntu ZIP: the installer saves Android's current boot
   images, Magisk's patched `init_boot` included, as the set Ubuntu switches
   back to.
3. Get the installation ZIP for your model:
   - SM-X910: `ubuntu-24.04-sm-x910-vX.X.X.zip` from the [latest release](https://github.com/agcarbajo/ubuntu-galaxy-tab-s9-ultra/releases/latest). `gts9u-split.zip` and `Dualboot-vX.X.X.apk` only if you want dual boot.
   - SM-X710: there is no release download. Build `ubuntu-24.04-sm-x710-bl5-vX.X.X.zip` yourself (see [Galaxy Tab S9 (SM-X710)](#galaxy-tab-s9-sm-x710)).

   A ZIP built from a staged firmware set carries `-bl<rev>` in its name. That
   revision must match your tablet's bootloader revision, the 8th character of
   its bootloader version (`adb shell getprop ro.bootloader`; `X710XXS5CYG1` is
   revision 5).
4. **TWRP** and **a `vbmeta` with AVB verification disabled** (AVB flags 2).
   Flash both with Odin, each `.tar` in the **AP** slot, with Auto Reboot
   unticked; re-enter Download mode between the two flashes and flash TWRP
   last. TWRP cannot write `vbmeta` (it is read-only from recovery), and the
   Ubuntu ZIP stops if `vbmeta` does not already have flags 2.
   - SM-X910: [TWRP](https://github.com/rainbowdashh/android_device_samsung_gts9u/releases/tag/V2) and [the `vbmeta` published alongside it](https://xdaforums.com/attachments/vbmeta-1-tar.6077784/).
   - SM-X710: a TWRP built for the Galaxy Tab S9 (`gts9wifi`), not the Ultra's
     `gts9u` image; this project does not link one yet. Any empty `vbmeta` made
     with `avbtool make_vbmeta_image --flags 2` has the flags the installer
     checks; the build leaves one at `$UBUNTU_WORKDIR/out/bundle/vbmeta.img`
     (for Odin: `tar -H ustar -cf vbmeta.tar vbmeta.img`). Flashing that file
     with Odin has not been tried yet: the test SM-X710 already had a
     flags-2 `vbmeta`.

   Heimdall is an alternative to Odin. It has no AP slot and writes by
   partition name, both files in one call:
   `heimdall flash --RECOVERY twrp.img --VBMETA vbmeta.img --no-reboot` (the
   names as `heimdall print-pit` lists them; any heimdall command after a
   `--no-reboot` one must carry `--resume`, see
   [development notes](docs/development-notes.md#flashing-without-graphical-odin-and-how-to-know-what-the-device-is-booting)).
   A web-based flashing tool is in preparation.
5. After flashing, go from Download mode straight into TWRP: hold Volume Down +
   Power until the screen goes off, then immediately hold Volume Up + Power
   until TWRP appears. **Don't let One UI boot** or TWRP will be overwritten
   with the stock recovery and you will need to flash it again.

### Ubuntu on the whole tablet

1. From TWRP flash the installation ZIP (`ubuntu-24.04-sm-x910-v1.2.0.zip`, or
   the SM-X710 ZIP you built) from a microSD (`/external_sd` in TWRP),
   external USB storage or `adb sideload`. **Don't flash it from internal
   storage** (`/sdcard`) as it will get wiped. With ADB you can also `adb push` the ZIP to `/tmp` and
   install it from there. Before writing anything the installer checks that the
   ZIP was built for this model and, for `-bl<rev>` builds, this bootloader
   revision, and stops with an error otherwise; every partition it writes is
   read back and verified.
2. Reboot by hand (the ZIP never reboots). The first boot grows the root
   filesystem to fill the partition and then opens the setup wizard, where you
   create your account: the image has no default user or password.

### Ubuntu beside Android

**SM-X910 only.** `gts9u-split.zip` refuses the SM-X710.

> [!IMPORTANT]
> The default split is 50% of the storage for Android and 50% for Linux. If you want to modify it just change the value of `ANDROID-PERCENT` inside `gts9u-split.zip` to the percentage you want Android to keep (between 5% and 95%).

1. Flash `gts9u-split.zip` from a microSD or USB drive. It shortens
   `userdata`, creates `linuxroot` beside it, and recreates Android's data so
   Android can make fresh encryption keys on its next boot. It also saves a
   backup of the partition table, `gpt-sda-before-split.bin`, next to the ZIP;
   keep that file. (From `/tmp` or internal storage the backup goes to `/tmp`,
   which is lost at the next reboot; the ZIP prints the `adb pull` command.)
2. Reboot to TWRP (Reboot > Recovery). Do not skip this: until recovery
   re-reads the table it may not show `linuxroot`, and the installer would then
   write Ubuntu over Android's `userdata`.
3. Wipe > Format Data.
4. Flash the installation ZIP (`ubuntu-24.04-sm-x910-v1.2.0.zip`).
5. Reboot and enjoy! (see below for how to reboot to Android).

### Switching systems

From **Ubuntu**, just use the toggle that you should see on quick settings or from the Tab Companion app under the Dualboot tab.

From **Android**, install the `Dualboot-vX.X.X.apk` app and give it root access, then reboot from the app or add the toggle to quick settings.

Both One UI and LineageOS have been tested in dual boot with Ubuntu and they work fine.

### Going back to Android

After a whole-tablet install, restore stock Android with Odin and the full
firmware package for your model; on a split tablet, Odin's Re-Partition with
that package's PIT also returns `userdata` to the whole disk. See
[Recovery](docs/boot-strategy.md#recovery).

Liked the project? you might want to buy me a coffee, I'll be very grateful!

<a href='https://ko-fi.com/V7W426ZJIP' target='_blank'><img height='36' style='border:0px;height:36px;' src='https://storage.ko-fi.com/cdn/kofi5.png?v=6' border='0' alt='Buy Me a Coffee at ko-fi.com' /></a>

## Updating an existing installation

Starting with v1.1.0, open **Tab Companion → Updates** to update the complete
port. For v1.0.0 installations without the updater, run:

```sh
d=$(mktemp -d) && curl -fL https://raw.githubusercontent.com/agcarbajo/ubuntu-galaxy-tab-s9-ultra/main/scripts/update-to-latest.py -o "$d/update.py" && sudo python3 "$d/update.py"
```

Updates preserve your data and settings. See [system updates](docs/system-updates.md)
for local ZIP installation, repair and troubleshooting.

SM-X710: updating from GitHub, the hidden repair action and
`update-to-latest.py` only know SM-X910 releases. Update with a ZIP you built
for the same model and bootloader revision, through Tab Companion → Updates →
local ZIP, or with
`sudo gts9u-update --zip ubuntu-24.04-sm-x710-bl5-vX.X.X.zip` (not yet tried
on an SM-X710).

## Known issues

- Regarding the official cover keyboard, as I said, only EF-DX920 cover keyboard has been tested. EF-DX900, EF-DX910, EF-DX915 and
EF-DX925 are untested as I don't have them, so they might not work.
- Guest virtual machines remain under development; initial Gunyah host support is included.
- Sometimes returning from deep suspend might need a hard reset.

## Galaxy Tab S9 (SM-X710)

`DEVICE=gts9wifi` builds Ubuntu for the 11" Galaxy Tab S9 Wi-Fi (`SM-X710`).
The port is experimental and has been brought up on one tablet with
bootloader revision 5. Compared with the SM-X910:

- **No published ZIP.** Build it as below.
- **Whole-tablet install only.** No split, no Dualboot app, no switching.
- **No GitHub updates.** Update with a ZIP you built (see
  [Updating](#updating-an-existing-installation)); a local-ZIP update has not
  yet been tried on an SM-X710.

### Status (2026-11-07)

| Component | Status | Summary |
|---|---:|---|
| Display | ✅ | 2560×1600, 120 Hz or 60 Hz (Settings → Displays) |
| GPU | ✅ | Adreno 740; the earlier lost pixels are fixed (`videocc` is built in, so `rpmhpd` reaches `sync_state` at boot). No 719 MHz step |
| CPU | ✅ | Prime core up to 3.36 GHz |
| Touchscreen | ✅ | STM FTS1BA90A; double tap to wake works, off by default (Tab Companion → Touch) |
| S Pen | ✅ | Pressure, tilt, hover and the side button, in landscape and portrait; dock detection and charging; the BLE pairing service starts (pairing a pen not yet recorded). Optionally a tap with the side button held near the screen is a right click. Tab Companion → S Pen → S Pen test checks a pen ([notes](docs/tab-companion.md#the-pen-test)) |
| Vibration / haptics | ✅ | On-screen keyboard feedback |
| Keyboard cover | ✅ | EF-DX720 tested, with the posture switch: typing posture keeps laptop mode; detaching or folding it back gives tablet mode and the on-screen keyboard |
| Wi-Fi and Bluetooth | ✅ | WCN6855 / ath11k; the Bluetooth controller comes up, Bluetooth audio (AAC included) not yet tested ([notes](docs/gts9wifi-wifi.md)) |
| Rotation and light sensor | ✅ | Through the ADSP's sensor core, as on the SM-X910; the light reading follows the room ([notes](docs/gts9wifi-sensors.md)) |
| RTC and sleep statistics | ✅ | PMIC RTC and `qcom_stats` |
| Suspend / resume | 🟡 | Off by default until repeated RTC-woken runs pass; the power key locks and blanks the screen instead, and closing the cover locks it |
| Speakers and microphones | ✅ | Four CS35L45 amplifiers and the digital microphones: recording and playback confirmed, with Samsung's speaker-protection files copied in by hand ([building](#building-the-sm-x710-zip)) |
| Cameras | 🟡 | Two HI1337 (rear and front), off unless enabled; not yet tested on hardware ([notes](docs/gts9wifi-cameras.md)) |
| USB-C charging, host and DisplayPort | ❓ | Not yet assessed |
| Fingerprint reader | ❌ | Not supported yet; its services are skipped on this model |
| NPU | ❌ | Not built for this model (`GTS9U_INCLUDE_NPU=0`) |

The evidence is in [hardware-status.md](docs/hardware-status.md#sm-x710-galaxy-tab-s9).
The tested tablet was first installed from an earlier gts9wifi ZIP and then
updated in place to this tree's kernel and device package 2.56; a ZIP built
from the current tree has not yet been flashed end to end.

Known limits:

- **Suspend is off by default.** To try s2idle for one boot:
  `echo s2idle | sudo tee /run/ubuntu-gts9u/suspend-mode` and then
  `sudo systemctl restart ubuntu-gts9u-suspend-mode.service`. The same word in
  `/etc/ubuntu-gts9u/suspend-mode` (create the directory first) keeps it
  across boots. The reliability tools are in [scripts/diagnostics/suspend](scripts/diagnostics/suspend/README.md).
- **The panel still needs the boot-time `pm_test` cycle** (as on the
  SM-X910), so the screen stays dark for roughly the first half minute of a
  cold boot. Replacing that cycle is open ([display notes](docs/gts9wifi-display.md)).
- **The interconnect is released only after that cycle**, by
  `ubuntu-gts9u-icc-late-release`: releasing it earlier hangs the cycle. The
  DDR bandwidth monitor (`icc_bwmon`) is a module and blacklisted
  ([interconnect notes](docs/gts9wifi-interconnect.md)).

### Building the SM-X710 ZIP

Only bootloader revision 5 has a firmware manifest,
`configs/firmware/x710-bl5-X710XXS5CYG1.toml`, staged from Samsung's
`XAR-X710XXS5CYG1-20250728211550.zip`. Check your revision first (step 3 of
[Before you start](#before-you-start)): the installer refuses a ZIP built for
another one.

Prepare the build host as in
[Build pipeline](docs/ubuntu-userspace.md#build-pipeline) (host packages, and
`mkbootimg.py` and `avbtool.py` in `/root/ubuntu-gts9u/tools`), then, as root
from the repository (the work directory defaults to `/root/ubuntu-gts9u`):

```sh
bash scripts/fetch-mainline.sh
python3 scripts/stage-firmware.py stage \
    --manifest configs/firmware/x710-bl5-X710XXS5CYG1.toml \
    --odin XAR-X710XXS5CYG1-20250728211550.zip \
    --vendor-image <the package's vendor image, extracted from super> \
    --sscregistrygen <tools/sscregistrygen from sensh v2.0.0> \
    --out /root/ubuntu-gts9u/firmware/x710-bl5
DEVICE=gts9wifi FIRMWARE_DIR=/root/ubuntu-gts9u/firmware/x710-bl5 \
    GTS9U_INCLUDE_NPU=0 bash scripts/build-release.sh
```

The manifest does not stage the CS35L45 speaker-protection files yet. The
tested image had `cs35l45-dsp1-spk-prot.wmfw`, `cs35l45-dsp1-spk-prot.bin` and
`cs35l45-dsp1-spk-prot-calib.bin` copied into `FIRMWARE_DIR` by hand, from the
same vendor image's `firmware/` directory, and the build installs them when
they are there. Audio on a build without them has not been tested.

[Firmware manifests](configs/firmware/README.md) explains the staging
options. With `sudo` instead of a root shell, pass the variables through:
`sudo env DEVICE=gts9wifi FIRMWARE_DIR=… GTS9U_INCLUDE_NPU=0 bash scripts/build-release.sh`.
The result is `artifacts/ubuntu-24.04-sm-x710-bl5-v<VERSION>.zip`, with
`artifacts/MANIFEST-sm-x710-bl5-v<VERSION>.txt` beside it. Its root filesystem is
labelled `UBTS9_UFS` (`UBTS9U_UFS` on the SM-X910).

### Installing on the SM-X710

The image drives only the STM FTS1BA90A touchscreen; the stock device tree
also lists a Goodix GT9916, which this image does not describe, so check which
controller your unit has first
([touch notes](docs/gts9wifi-touch-fts1ba90a.md)).

Follow [Before you start](#before-you-start) and
[Ubuntu on the whole tablet](#ubuntu-on-the-whole-tablet) with the SM-X710
items. If TWRP's microSD mount fails, push the ZIP to `/tmp` instead:

```sh
adb push ubuntu-24.04-sm-x710-bl5-vX.X.X.zip /tmp/
adb shell twrp install /tmp/ubuntu-24.04-sm-x710-bl5-vX.X.X.zip
```

`twrp install` returns before the flash has finished. Check the result later,
in TWRP, by comparing
`sha256sum /dev/block/by-name/boot /dev/block/by-name/init_boot /dev/block/by-name/vendor_boot /dev/block/by-name/dtbo`
with the `SHA256SUMS` inside the ZIP, not by the command's output.

## Documentation

| Document | Contents |
|---|---|
| [Boot strategy](docs/boot-strategy.md) | Requirements, partitions, the boot chain and recovery |
| [Dual boot](docs/dual-boot.md) | How switching systems works, and what it never touches |
| [Hardware status](docs/hardware-status.md) | Evidence, limitations and pending hardware tests |
| [Tab Companion](docs/tab-companion.md) | S Pen, keyboard and fingerprint settings, architecture and diagnostics |
| [System updates](docs/system-updates.md) | GitHub and local ZIP updates, current-build repair and the updater for older installations |
| [Ubuntu userspace](docs/ubuntu-userspace.md) | Root filesystem, desktop integration and the build pipeline |
| [Release 1.2.0 build inputs](docs/release-1.2.0-build.md), [firmware manifests](configs/firmware/README.md) | The SM-X910 release's NPU and fingerprint inputs, and staging the firmware of one bootloader revision for `scripts/build-release.sh` |
| [DisplayPort](docs/gts9u-displayport.md) | USB-C DisplayPort output on the SM-X910 and its remaining issues |
| SM-X710: [display](docs/gts9wifi-display.md), [touch](docs/gts9wifi-touch-fts1ba90a.md), [sensors](docs/gts9wifi-sensors.md), [Wi-Fi](docs/gts9wifi-wifi.md), [cameras](docs/gts9wifi-cameras.md), [interconnect](docs/gts9wifi-interconnect.md), [bring-up flags](docs/gts9wifi-bringup-flags.md) | Galaxy Tab S9 bring-up notes |
| [Fingerprint reader](docs/fingerprint-reader.md) | Working EL721/UDFPS integration, validation evidence and maintenance constraints |
| [Gunyah virtual machines](docs/gunyah.md) | Host-driver architecture, current validation and tablet test procedure |
| [Development notes](docs/development-notes.md) | Durable technical conclusions and rejected approaches |
| [Porting log](docs/porting-log.md) | Chronological engineering history |

## Repository layout

```text
kernel/       Device tree, drivers, patches and kernel configuration
packaging/    Debian packages installed in the Ubuntu image
android/      The Dualboot app, which switches systems from the Android side
configs/      System configuration, service overlays, the TWRP installers and
              per-revision firmware manifests (configs/firmware/README.md)
scripts/      Reproducible build and validation tools
tools/        In-place experiment kits for the SM-X710 test unit (records, not
              an install path)
docs/         Detailed documentation and engineering history
artifacts/    Generated release files; not versioned
work/         Scratch space for builds; not versioned
```

## Firmware and licensing

Keep in mind project has been made with the help of agentic AIs (codex and claude code), so some things might not be done in the best possible way.

Proprietary Samsung and Qualcomm firmware is not stored in Git. Build helpers
stage it from the owner's tablet or official Samsung firmware and verify pinned
checksums.

The project default license is MIT; per-file SPDX headers take precedence.
Kernel code and patches remain GPL-2.0-only, and the device tree remains
BSD-3-Clause. Imported sources are listed in
[kernel/PROVENANCE.md](kernel/PROVENANCE.md).

Contributions must be reproducible in the repository and validated on physical
hardware before a component is marked as working.
