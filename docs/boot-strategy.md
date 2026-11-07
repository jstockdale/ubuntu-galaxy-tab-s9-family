# Boot chain, installation and recovery

Last revised: 2026-11-07. It inherits the chain physically proven by
postmarketOS v1.71 on the SM-X910; only the root filesystem and its initramfs
differ. The Galaxy Tab S9 (SM-X710, `DEVICE=gts9wifi`) boots through the same
chain; where the two tablets differ, this page says so.

Since v0.18 the root filesystem is installed on the **internal UFS** and a
release is a single flashable ZIP. Since v1.0.0 it can share that UFS with
Android on the SM-X910 — see [Two ways to install](#two-ways-to-install). On
the SM-X710 only the whole-tablet install exists, and no SM-X710 ZIP is
published: build it as [`ubuntu-userspace.md`](ubuntu-userspace.md#building-a-release)
describes.

## The proven chain

- Neither tablet has A/B slots, and Samsung offers Download Mode and Odin, not
  a usable `fastboot boot`.
- The Android chain uses **boot header v4**.
- Samsung's ABL loads the kernel from `boot`, the generic initramfs from
  `init_boot`, and the DTB, cmdline and bootconfig from `vendor_boot`, all from
  the internal UFS.
- The Linux root is an **ext4 filesystem inside a partition** on that same UFS.
  ABL reads no filesystems: the kernel comes out of `boot`, and only the
  initramfs looks for the root, by label.
- The stock DTBO carries downstream interfaces and does not apply on top of a
  mainline DTB. The bundle writes a `dtbo` that deliberately is **not** an
  Android DT table, which keeps ABL off its overlay path. The same DTB goes
  into `vendor_boot` and, appended to the kernel, into `boot`; on the SM-X910
  ABL was measured to use the `vendor_boot` copy
  ([`performance.md`](performance.md#bootloader-compatible-implementation)).
- TWRP carries its own recovery DTB/DTBO and stays recoverable however the
  mainline images change.

Two invariants that cannot be touched:

- `build-android-v4-bundle.sh` always appends the DTB to `Image.gz`, and
  `ENABLE_RUNTIME_DTBO` must stay `0` (the default), which writes a non-table
  `dtbo` (4096 zero bytes before its AVB footer). With `ENABLE_RUNTIME_DTBO=1`, ABL returns to its `ufdt`
  fork, rejects the base DTB and drops into Odin before ever reaching Linux.
  (postmarketOS called these `APPEND_DTB_TO_KERNEL=1` and
  `DISABLE_RUNTIME_DTBO=1`.)
- **Changing the DTS means rewriting `vendor_boot`.** The X910's ABL applies the
  DTB from `vendor_boot`, not the one appended to `boot.img`. Rewriting only
  `boot` leaves the old DTS in use. Write `boot` as well, so the two copies stay
  identical. Which copy the X710's ABL uses has not been isolated: write both.

## Requirements

Three things have to be in place first. The second and third are flashed with
Odin from Download mode, each into the `AP` slot, one Odin session per file:

1. **An unlocked bootloader.** Once it is, `ro.boot.verifiedbootstate` reads
   `orange`.
2. **TWRP** for the tablet: the gts9u build linked in the README on the
   SM-X910, a Galaxy Tab S9 family (gts9wifi) build on the SM-X710. The
   installer accepts any Tab S9 family recovery and identifies the tablet from
   the bootloader's command line, not from the recovery's properties.
3. **A `vbmeta` with AVB verification disabled** (AVB flags 2) — the one
   published alongside TWRP on the SM-X910. On the SM-X710 any empty flags-2
   image has what the installer checks, such as the build's
   `$UBUNTU_WORKDIR/out/bundle/vbmeta.img` packed for Odin with
   `tar -H ustar -cf vbmeta.tar vbmeta.img` (not yet tried on hardware).

Heimdall can replace Odin: `heimdall flash --RECOVERY <recovery.img> --VBMETA
<vbmeta.img>`, with the images taken out of the two Odin tars and the partition
names as the tablet's PIT lists them, in a single invocation (see
[`development-notes.md`](development-notes.md#flashing-without-graphical-odin-and-how-to-know-what-the-device-is-booting)
for its `--no-reboot`/`--resume` trap). A web-based flashing tool is being
prepared and is not available yet.

The third is the one that catches people out, because it is invisible until it
bites. Flashing stock firmware puts back a `vbmeta` with verification enabled,
and TWRP cannot write `vbmeta` on either tablet: on the SM-X910 it lives on a
read-only LUN — `sde15`, not the `sda` everything else here is on — that only
the bootloader can write. The installer therefore cannot patch it from TWRP.
It checks the flags instead and stops with:

```
ERROR: vbmeta is read-only and does not have AVB flags 2
```

That is the installer refusing to write a kernel the tablet would then decline
to boot. Flash the disabled `vbmeta` with Odin and run it again. Changing
`vbmeta` makes Android erase its own data on the next boot. (Should a recovery
ever show `vbmeta` writable, `blockdev --getro` = 0, the installer writes the
ZIP's own flags-2 `vbmeta.img` instead, with the same consequence for
Android's data.)

After flashing TWRP, reboot **straight into recovery**. Letting Android boot
once puts the stock recovery back over it. With Odin's Auto Reboot unticked,
leave Download mode with the buttons: Volume Down + Power until the screen goes
off, then at once Volume Up + Power. No Odin or Heimdall command leaves
Download mode for recovery.

## Partitions used

Both tablets have these five boot partitions with the same exact sizes; the
installer checks every size before it writes anything.

| Partition | Exact size | Contents |
|---|---:|---|
| `boot` | 100663296 | `Image.gz` with appended DTB, header v4, no ramdisk |
| `init_boot` | 8388608 | Ubuntu initramfs, legacy LZ4 |
| `vendor_boot` | 100663296 | the board's DTB (`sm8550-samsung-gts9uwifi.dtb` or `sm8550-samsung-gts9wifi.dtb`); the cmdline of `configs/vendor_boot/cmdline.txt` with `root=LABEL=` set to the board's label (plus `GTS9U_EXTRA_CMDLINE` when a build sets it); the bootconfig; and a vendor fragment with the board's early firmware: Bluetooth (`hmtbtfw20`/`hmtnv20` on the SM-X910, `hpbtfw21`/`hpnv21*` on the SM-X710) and, on the SM-X710, the WCN6855 hw2.1 Wi-Fi set |
| `dtbo` | 16777216 | a non-table image, see above |
| `vbmeta` | 131072 | AVB with verification and verity disabled (`flags=2`) |
| `userdata` | the rest of the UFS (up to 1007985586176 on a 1 TB SM-X910) | Android's data, or Ubuntu's root when the disk is not split |
| `linuxroot` | the remainder | Ubuntu's ext4 root, labelled `UBTS9U_UFS`, when an SM-X910 is split |

The installer finds every partition by name. On a 1 TB SM-X910 `userdata` is
GPT entry 34 and `linuxroot` entry 35; nothing depends on those numbers, and
`userdata` is smaller on smaller units and on the SM-X710.

The installation never touches `super`, the bootloader, the PIT, EFS, persist,
modem/modemst or the calibration partitions.

## Two ways to install

Both use the same ZIP. The difference is whether the UFS is split first, and
the installer's rule is one line: **`linuxroot` if it exists, otherwise
`userdata`**.

### Ubuntu on the whole tablet

Nothing is repartitioned. The ext4 image is written with `dd` into a partition
that already exists, and the filesystem grows to fill it on the first boot.
This is the only way on the SM-X710.

- **The GPT stays Samsung's, byte for byte.** Neither the build nor the
  installer runs `sgdisk`, `parted`, `sfdisk`, `mkfs` or `wipefs` against the
  device; `scripts/validate-bundle.sh` fails if any of those appears in the
  packaged installer.
- **`super` is untouched**, so Android's system image is still there.
- **Android's user data is lost**: it is exactly what occupies the partition
  being reused.

### Ubuntu beside Android

SM-X910 only for now: the split ZIP refuses any other tablet, so an SM-X710
has no `linuxroot`, no seeded boot sets and no system switcher.

`gts9u-split.zip` shortens `userdata` and creates `linuxroot` next to it, then
recreates Android's data so it can make fresh encryption keys on its next boot.
Afterwards the installer finds `linuxroot`, installs there, and leaves
Android's `userdata` alone. Builders make it with
`python3 scripts/make-repartition-zip.py artifacts/gts9u-split.zip
--android-percent 50`.

The split ships set to halve the disk. The share is one file inside the ZIP,
`ANDROID-PERCENT`, holding the percentage `userdata` keeps. Changing the split
means changing that number — open the ZIP in any archive manager and edit it:

```bash
printf '30\n' > ANDROID-PERCENT && zip gts9u-split.zip ANDROID-PERCENT
```

Nothing else needs touching: `SHA256SUMS` inside the ZIP covers the installer
script alone, so editing the number invalidates nothing. Anything from 5 to 95
is accepted, and the tablet checks the same bounds before it writes the table.

Flashed on a tablet that is already split it exits saying so and changes
nothing, so there is no harm in running it twice.

### Where the ZIP has to sit

When Ubuntu is going onto `userdata`, that partition *is* the internal storage:
reading the ZIP from there would destroy it half way through the write, so the
installer aborts if the ZIP's path is under `/data`, `/sdcard`, `/mnt/sdcard`
or `/storage/emulated`. In order of convenience:

1. **`adb sideload`.** The ZIP is served from the PC and takes no room on the
   tablet. This is the proven route on the SM-X910.
2. **USB-OTG**, if a stick is at hand.
3. **microSD**, with a warning: TWRP mounts the card's **first** partition at
   `/external_sd`, and on a card left over from the microSD releases that first
   partition is `UBTS9U_BOOT`, 256 MiB. A ~1 GiB ZIP does not fit there. Use a
   plain data card.

Sideload and the `/tmp` route below need `adb` (Android platform-tools) on
the PC.

On the SM-X710 the tested route is TWRP's RAM-backed `/tmp`: `adb push <zip>
/tmp/`, then Install from `/tmp` in TWRP, or `adb shell twrp install
/tmp/<zip>`. `/tmp` does not survive a reboot, and `/sdcard` is the internal
storage the install overwrites. `adb shell twrp install` returns after about a
second while TWRP goes on flashing (on the test unit it then rebooted by
itself), so its output is not seen there; at the next TWRP visit, check that
`sha256sum /dev/block/by-name/{boot,init_boot,vendor_boot,dtbo}` matches the
ZIP's `SHA256SUMS` (each image is exactly its partition's size). `adb
sideload` has not been tried on the SM-X710.

When Ubuntu goes onto `linuxroot`, the ZIPs may sit on internal storage,
because the partition being written is not the one they are on.

### Labels

| Label | Where | What it is |
|---|---|---|
| `UBTS9U_UFS` | `linuxroot`, or `userdata` | the installed root on the SM-X910, since v0.18 |
| `UBTS9_UFS` | `userdata` | the installed root on the SM-X710 |
| `UBTS9U_ROOT` | microSD | the root of releases up to v0.17 |

They differ on purpose. `root=LABEL=` resolves to the first match, and with the
same label in both places an old card forgotten in the slot would boot instead
of the new installation. The two UFS labels are scoped per board, so a kernel
and a root built for different tablets fail at mount instead of half-booting.

## The single installation step

Flash the ZIP from TWRP. It writes the root filesystem, verifies it by reading
it back and hashing it, and only then seeds the dual-boot sets (split tablets
only) and writes `boot`, `init_boot`, `vendor_boot` and `dtbo`, reading each
back against the ZIP's `SHA256SUMS`. It writes `vbmeta` too only if the
recovery exposes it read-write, which neither tablet's TWRP does. That order is
deliberate: if the long part fails, the device keeps the boot images it already
had and is still one retry from where it was.

Firmware for the GPU, ADSP, Wi-Fi and audio lives **inside** the root
filesystem image, not in an overlay applied afterwards. The early Bluetooth
set, and on the SM-X710 the Wi-Fi hw2.1 set, also travel in `vendor_boot`.

The build tools **never** write to a partition. The owner flashes the ZIP.

### Update ZIPs

A ZIP with no `rootfs.img` but with an overlay updates an existing installation
in place: it mounts the root after checking it with `e2fsck -p`, replaces
firmware, modules and configuration, and leaves the data alone. This is how a
new kernel is tested without reinstalling. The two contents are mutually
exclusive and `make-twrp-zip.py` refuses to build a ZIP with both.

### Seeding the dual-boot sets

While the ZIP runs is the only moment both systems' boot images exist at once:
Android's are still on the partitions, and Ubuntu's are in the ZIP. So that is
where the sets the switchers read get saved, into
`/var/lib/gts9u-boot-sets/{android,ubuntu}` on the new root.

A set is the four boot images, 216 MiB, so both together need 432 MiB inside a
filesystem that has not grown yet. The installer refuses below 480 MiB rather
than seed half of them — Android's set is the one that cannot be rebuilt
afterwards. The image is therefore built with enough slack to clear that bar;
see `scripts/build-ufs-image.sh`.

This runs only when installing into `linuxroot`. On a whole-tablet install
there is no second system to switch to, and no copy of Android's boot images
is kept: back them up first (see [Before starting](#before-starting)).

Because the set is copied off the live partitions, **install Android first and
Ubuntu second**, and root Android before that step if it is meant to be rooted:
switching systems from the Android side goes through `su`.

## Iterating on a running system

With Ubuntu up, kernel or DTS changes can be tested by writing only the image
strictly needed, always with explicit authorisation:

- `boot` for the kernel;
- `vendor_boot` for DTS, cmdline or bootconfig (and `boot` as well after a DTS
  change, see above);
- both when the kernel and its signed module set change.

Before every write: a temporary backup, a size check, `dd conv=fsync` and a
SHA-256 comparison between source and destination. `sdaN` numbers are never
hardcoded; the stable links under `/dev/disk/by-partlabel/` are used instead.

Kernel lockdown requires that `boot` and the modules installed on the root —
everything in the kernel build's `modules-root` — come from the **same**
build. Recreating the `O=` tree generates a new signing key and the old modules
start being rejected with "Operation not permitted": always ship the modules
alongside their kernel.

On a split SM-X910, after a validated `boot` write, also refresh the saved
Ubuntu set, or the next Android → Ubuntu switch restores the old kernel:
`pkexec scripts/refresh-saved-ubuntu-boot.sh <boot.img> <sha256> --apply`.
That helper refreshes `boot` only; `vendor_boot`, `init_boot` and `dtbo` in
`/var/lib/gts9u-boot-sets/ubuntu` are updated by hand.

An experiment that only needs command-line tokens is a `vendor_boot` repack,
not a kernel build. `GTS9U_EXTRA_CMDLINE` appends tokens and
`GTS9U_DROP_CMDLINE` leaves tokens of `cmdline.txt` out; write only the
resulting `vendor_boot.img`:

```sh
UBUNTU_WORKDIR=$W DEVICE=<gts9uwifi|gts9wifi> \
INITRAMFS=$W/rootfs/boot/initrd.img-<release> \
INITRAMFS_OVERLAY_DIR=$W/out/initramfs-overlay \
BUNDLE_OUT_DIR=$W/out/bundle-experiment \
GTS9U_EXTRA_CMDLINE='…' bash scripts/build-android-v4-bundle.sh
```

## Boot quirks Ubuntu has to keep

- The ANA38407 panel is not reachable after Samsung's cold hand-off, on either
  tablet. Before the display manager starts, a single platform suspend/resume
  (`pm_test=platform`, `ubuntu-gts9u-panel-coldboot-recover.service`) is
  needed to recover the DDIC, the DPU and the graphics session. In Ubuntu this
  is ordered `Before=display-manager.service`. On the SM-X710 the panel stays dark until
  that cycle has run, about 24 s into boot; retiring it is open (DISP-5,
  [`gts9wifi-display.md`](gts9wifi-display.md)).
- On the SM-X710 the interconnect has to keep its boot vote until that cycle
  has returned: an interconnect that reaches `sync_state()` earlier hangs the
  cycle (watchdog reset, boot loop). The DDR bandwidth monitor, its last
  consumer, is therefore a module and blacklisted, and
  `ubuntu-gts9u-icc-late-release.service` releases the vote after the cycle
  ([`gts9wifi-interconnect.md`](gts9wifi-interconnect.md)).
- If a DisplayPort dock is already connected, its HPD must stay **deferred**
  until that recovery finishes. An early HPD also blocked the internal DSI in
  the baseline tests.
- An early visible console is not a requirement: ABL may add `console=null`.
  The persistent journal and TWRP are the reliable sources of diagnosis.
- Critical kernel providers are **built-in**. This port neither installs nor
  auto-loads a general module tree; it ships only the isolated modules in the
  kernel build's `modules-root` (ath12k, QTEE and SNVM, the SPSS stack, the
  netfilter set for containers, `v4l2loopback`, and on the SM-X710 the
  blacklisted `icc-bwmon`), all signed by that build's key. The SM-X710's
  ath11k Wi-Fi is built in, and so is its video clock controller: left at
  `=m` it never loaded, rpmhpd never reached `sync_state()`, and the GPU lost
  writes. A critical symbol left at `=m` usually makes the subsystem fail to
  appear at all.

## Exact installation procedure

The steps the owner performs. No tool in the project does them for them.

### Before starting

1. Check the ZIP's SHA-256: against the release notes for a published
   release, or against its line in `MANIFEST-<sm-x910|sm-x710>-bl<rev>-v<version>.txt`
   (without `-bl<rev>` for a build without a firmware staging) for a local
   build. The ZIP's name carries the model (`sm-x910` or `sm-x710`) and the
   bootloader revision (`bl<rev>`); both must be the tablet's.
2. Have the way back at hand. Before the first install, back up `boot`,
   `init_boot`, `vendor_boot` and `dtbo` from TWRP, or keep the stock Odin
   package of exactly the tablet's build, which contains them; a whole-tablet
   install saves no Android boot set. Back up `efs`, `sec_efs` and `persist`
   too, with `dd if=/dev/block/by-name/<partition>`, onto external media.
3. Accept that **whatever Android keeps in the partition being written is
   lost**. `super`, the bootloader, EFS and the calibrations are untouched.
4. For a whole-tablet install, keep the ZIP off internal storage — see the
   media list above.

### Flashing

1. Boot TWRP. For sideload: `Advanced` → `ADB Sideload`, then `adb sideload
   <zip>` from the PC. From external media or `/tmp`: `Install` → pick the ZIP.
2. Wait. Writing the root filesystem takes several minutes, shows no progress,
   and deliberately runs before the boot images.
3. Read the output. The installer aborts if the tablet is not the model the
   ZIP was built for (SM-X910 or SM-X710, from the ZIP's `DEVICE-IDENTITY`), if
   the recovery is not a Galaxy Tab S9 family recovery, if the ZIP's firmware
   revision (`FIRMWARE-IDENTITY`) differs from the tablet's bootloader revision
   (an empty `ALLOW-FIRMWARE-MISMATCH` member overrides that), if a partition
   or an image is missing, if a partition size does not match, if `vbmeta`
   lacks AVB flags 2, if the target is smaller than the image, if the ZIP sits
   on the destination, if the target is still mounted, or if what it reads back
   does not match the image's SHA-256.
4. The ZIP **does not reboot**. Reboot by hand when it finishes.
5. On the first boot the filesystem grows to fill its partition. Only the
   filesystem is resized: the partition was already that size. On the SM-X710
   the screen stays dark for the first ~25 s of every boot (see
   [Boot quirks](#boot-quirks-ubuntu-has-to-keep)).

### If something goes wrong

The installer does not format, does not repartition and does not reboot, so a
failure half way leaves the device in TWRP, still reachable. From there:

- retry the flash, or
- enter Download Mode and restore the official firmware with Odin.

A failure while writing the root filesystem leaves that partition half done but
the boot images intact: there is no state in which the device has a new kernel
and no system to boot.

## Recovery

In order:

1. **TWRP and `adb`**, to mount the root, pull the journal and restore images.
   Pull the logs with `adb shell mount -o ro -t ext4
   /dev/block/by-name/<linuxroot|userdata> /mnt` (unmount TWRP's `/data`
   first if it holds the same partition) and `adb pull /mnt/var/log/journal`.
   Restore the boot images with `adb push boot.img init_boot.img
   vendor_boot.img dtbo.img /tmp/`, then, from the PC's shell, `for p in boot
   init_boot vendor_boot dtbo; do adb shell "dd if=/tmp/$p.img
   of=/dev/block/by-name/$p bs=4M conv=fsync && sha256sum
   /dev/block/by-name/$p"; done`. Every image is exactly its partition's size, so the
   partition's hash must equal the image's; reboot only when all four match.
   This is how the SM-X710's boot-loop experiments were undone. `vbmeta` is
   only ever written with Odin.
2. **Download Mode and Odin** with official firmware for the same model, at
   the tablet's bootloader revision or newer, never older (anti-rollback). On a
   tablet that was never split this is one step, because the GPT was never
   modified; after a whole-tablet install flash the full `CSC` rather than
   `HOME_CSC`, or Format Data afterwards, because `userdata` now holds
   Ubuntu's ext4. On a split one, Odin's Re-Partition with the PIT from the
   firmware's CSC restores the factory table and returns `userdata` to the
   whole disk.

Backups are restored through `/dev/block/by-name/<partition>`, never with
hardcoded LUN numbers. EFS is only ever mounted `ro,noload`, when the Bluetooth
or Wi-Fi address has to be read; it is never written.
