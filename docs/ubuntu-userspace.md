# Building the Ubuntu 24.04 arm64 root filesystem

Last revised: 2026-11-07. It covers both tablets the build supports: the
Galaxy Tab S9 Ultra (SM-X910, `DEVICE=gts9uwifi`, the default) and the
Galaxy Tab S9 (SM-X710, `DEVICE=gts9wifi`).

This document defines **how the Ubuntu userspace is built** and which decisions
belong to Ubuntu. The boot chain is in [`boot-strategy.md`](boot-strategy.md);
what is inherited from the postmarketOS port is in
[`hardware-status.md`](hardware-status.md).

## Guiding principle

The hardware is already solved in the kernel. Ubuntu must reinvent none of it:
it contributes userspace only. The pipeline is therefore split into two halves
with separate responsibilities:

| Half | Source | What it produces |
|---|---|---|
| Kernel and boot | `kernel/` (the Ultra's sources inherited from the pmOS port, the SM-X710's written here), selected by `DEVICE` | `boot`, `init_boot`, `vendor_boot`, `dtbo`, and the signed module set in `modules-root` |
| Userspace | `mmdebstrap` over ports.ubuntu.com | an ext4 root filesystem on the internal UFS |

Neither half may depend on the other having been assembled by hand on a live
installation.

## Build tool: mmdebstrap

**`mmdebstrap`** is used, not `debootstrap` and not a preinstalled Ubuntu
image, for three concrete reasons:

1. it accepts `--architecture=arm64` with `qemu-user-static` and needs no
   native arm64 environment, which does not exist here: the build runs on an
   x86-64 host (native Linux or WSL);
2. it allows `--customize-hook` to inject configuration, local packages and
   overlays **inside** the same invocation, so the root filesystem is never
   modified "by hand" afterwards;
3. it produces the same result from the same package manifest, and allows
   pinning the archive snapshot if strict reproducibility is needed later.

`debootstrap` is rejected as the main tool because it would need a second
configuration phase outside the tool, which is exactly the "unrepeatable
installation" pattern this project forbids.

### Suite and components

- Suite: `noble` (Ubuntu 24.04 LTS).
- Architecture: `arm64`.
- Components: `main`, `restricted`, `universe`, `multiverse`.
- Mirror: `http://ports.ubuntu.com/ubuntu-ports`.
- Pockets: the build bootstraps from `noble`, `noble-updates` and
  `noble-security`; the installed system's apt sources also enable
  `noble-backports`. `UBUNTU_SUITE` and `UBUNTU_MIRROR` override the suite and
  the mirror.

`ports.ubuntu.com` is mandatory: `archive.ubuntu.com` publishes no arm64.

### The first root filesystem's package set

The goal of milestones 1 and 2 is a system that boots and has networking and
SSH. The desktop is installed in the same root filesystem because milestone 3
needs it and rebuilding is cheap, but the validation order remains console →
network → desktop.

The authoritative package set is `base_packages` and `desktop_packages` in
`scripts/build-ubuntu-rootfs.sh`; it is not repeated here, because it keeps
changing. `ubuntu-desktop-minimal`, GDM3, GNOME Shell, Mesa, PipeWire with
WirePlumber, BlueZ, `iio-sensor-proxy` and the bring-up tools (`strace`,
`evtest`, `i2c-tools`, `v4l-utils`, `usbutils`, `pciutils`, `ethtool`,
`libdrm-tests`, `edid-decode`) are all in it. `linux-firmware` is not
installed: every blob comes from the staged firmware overlay.

`v4l-utils` is still the CAMSS diagnostic tool, but the normal path no longer
ends at RAW10. `scripts/build-camera-packages.sh` pins and packages
`libcamera` 0.7.2 (`62d4bfc`) with the `simple` pipeline, the software ISP,
GStreamer, HI1337/HI847 tuning, full field-of-view preservation, and contrast
autofocus for the DW9808. It also rebuilds only PipeWire 1.0.5's libcamera SPA
(`a2287be`) with seven backports: libcamera 0.7 compatibility, the correct RGB
map, safe reuse of requests and buffers, borrowed descriptors, and delivery of
completions on the data loop. The resulting `.deb`s are `libcamera-gts9u` and
`libspa-0.2-libcamera-gts9u`; they replace only the archive's camera packages
and keep Noble's PipeWire and WirePlumber.

The device package also installs the udev rule for `/dev/udmabuf`, needed so
the software ISP can allocate buffers without privileges. The kernel includes a
pinned, signed `v4l2loopback` to create four capture nodes, and
`v4l2-relayd-gts9u` feeds them on demand from the PipeWire sources. The desktop
image includes GNOME Camera and `gstreamer1.0-gl`. A clean build therefore
offers the four named cameras to browsers and V4L2 applications with no scenes
and no per-user setup. That is the SM-X910: the camera table is per board
(`ubuntu-gts9u-camera-profile`). The SM-X710 gets a node only for a sensor
that is actually bound, and its two HI1337 cameras are off by default, opt-in
through `/etc/ubuntu-gts9u/cameras`, and untested on hardware
([`gts9wifi-cameras.md`](gts9wifi-cameras.md)).

**OBS Studio is no longer shipped.** It travelled along as a camera
verification tool, together with `obs-v4l2-gts9u`; with that work closed, both
left in v2.23 of the device package, and with them the 77 MiB of VLC that
`obs-plugins` dragged in through `Recommends`. See "OBS was travelling along"
in the development notes.

The image creates no account. Since `ubuntu-gts9u-device 2.18`,
`ubuntu-gts9u-desktop-user` resolves the OOBE-created account on every boot,
enables its lingering, and generates the relay service's `User=`, `Group=` and
environment under `/run`. Its PipeWire therefore exists before the first login
too, and does not depend on an SSH session staying open. The service waits for
PipeWire's real PID and watches its lifetime; if PipeWire or a relay exits,
systemd rebuilds the four cameras as a single set. Version
`v4l2-relayd-gts9u 0.1.2-gts9u15` adds pre-emptive delivery and correctly keeps
the ISP owner's PID. This stops the nodes from staying enumerated but black
after a boot, an update or a sensor change.

Version `0.1.2-gts9u16` also stops the black-frame producer when the last
reader closes, while keeping the output device open and its CAPTURE identity
available. Black frames resume at 30 fps while a reader waits for the shared
ISP, including during camera handover. Device package 2.48 requires this
version. The ISP, image processing, format and capture rate are unchanged.

`ubuntu-desktop-minimal` rather than `ubuntu-desktop` leaves out office
software and desktop snaps that contribute nothing to bring-up. Snap is
evaluated separately in milestone 5; it is not assumed from the start.

### Ubuntu decisions that must be tried natively first

From the postmarketOS baseline the project inherits hardware, **not** userspace
patches. These pieces must be tried natively before anything is ported:

| Piece | pmOS/Alpine | Ubuntu 24.04 — try first |
|---|---|---|
| Greeter | `gdm-greeter-*` accounts created by hand because Alpine builds systemd without `systemd-userdbd` | Native GDM3: Ubuntu **does** ship `systemd-userdbd`, so the workaround must not be copied |
| Sound server | PulseAudio 17 | PipeWire + WirePlumber, with `pipewire-pulse` as the compatibility layer |
| Split GPU/DPU topology | Patched Xorg + reverse PRIME | Mutter/Wayland handles `card0` (Adreno, render) and `card1` (DPU, KMS) unpatched; pmOS's Xorg stack is **not** ported |
| Rotation | Patched Mutter r6 | Ubuntu's Mutter as it is; the patch is ported only if the specific regression returns (an external mouse disabling auto-rotation) |
| Scaling | XFCE's manual GTK/Xft settings | Mutter's `scale-monitor-framebuffer` and GNOME's 200 % scaling |
| Sensors | Patched `iio-sensor-proxy` 3.9 + `libssc` + `hexagonrpcd` | Ubuntu's `iio-sensor-proxy`; `libssc` and `hexagonrpcd` **do** have to be packaged, because Ubuntu has neither. All three carry their own patches, `libssc` included |
| Network management | NetworkManager | NetworkManager with netplan as the frontend (Ubuntu's default) |

What does **not** translate and has to be repackaged as `.deb`:

- `libssc` and `hexagonrpcd` (the sensors' SSC/FastRPC client). `libssc` carries
  its own patch: its synchronous wait spun the GLib context without blocking,
  which cost a whole core as soon as the SSC left a request unanswered;
- `pd-mapper` (essential: without it the ADSP publishes no `servreg locator`
  and the ALSA card never appears);
- the device package with the equivalent udev rules, UCM, recovery services and
  `deviceinfo`.

## Root filesystem structure

Since v0.18 the root is **a single ext4 filesystem inside a partition** on the
internal UFS, labelled `$DEVICE_LABEL` from `scripts/lib/device.sh`:
`UBTS9U_UFS` on the SM-X910, `UBTS9_UFS` on the SM-X710. `vendor_boot`'s
`root=LABEL=` is set to the same label, so a kernel and a root built for
different tablets refuse to mount instead of half-booting. That partition is
`linuxroot` when the disk has been split (SM-X910 only) and `userdata` when it
has not. The image the ZIP ships has
no partition table: it is the filesystem alone, because it is written inside a
partition that already exists.

`/boot` lives inside the root. The separate microSD partition existed because
the initramfs that fits in `init_boot` (8 MiB) cannot hold the full module tree
and the second stage had to live somewhere mountable; with a single root that
place is `/boot`, and no partition has to be requested that we are not going to
create.

Up to v0.17 the microSD carried **two partitions**:

| Partition | FS | Label | Contents |
|---|---|---|---|
| 1 | ext4 | `UBTS9U_BOOT` | `initramfs-extra`, the reference DTB and build metadata |
| 2 | ext4 | `UBTS9U_ROOT` | the Ubuntu root filesystem |

The labels are deliberately our own. Reusing `pmOS_boot`/`pmOS_root` would make
a postmarketOS initramfs and an Ubuntu one compete for the same medium; and
`UBTS9U_UFS` differs from `UBTS9U_ROOT` so that an old card forgotten in the
slot cannot win the `root=LABEL=` resolution against the internal installation.

The image is generated small (root filesystem plus slack) and a
`ubuntu-gts9u-grow-rootfs.service` unit expands it on the first boot. On the
UFS that means **`resize2fs` only**: the partition is already its full size and
no partitioning tool touches it. On a microSD the partition does have to be
extended first, and the script tells the two cases apart by device and label.

## Two-tier swap

The image carried **no** swap at all: with 14.2 GiB of usable RAM and a real
desktop load (Steam plus a browser), the only backstop against a spike was the
OOM killer. Since v2.22 of the device package there are two tiers, 23 GiB in
total:

| Tier | Size | Priority | Unit |
|---|---|---|---|
| zram (zstd) | 8 GiB | 100 | `ubuntu-gts9u-zram.service` |
| swapfile on the UFS | 16 GiB | 10 | `ubuntu-gts9u-swapfile.service` |

The decisions, and why:

- **zram first, and with `zstd`.** It costs not one write to the UFS. The
  kernel's default algorithm here is `lzo-rle`, but `zstd` is available:
  `CONFIG_CRYPTO_ZSTD` is module-only and this port installs no generic tree,
  but modern zram carries its own zstd backend and announces it in
  `comp_algorithm`. Measured on the tablet: **4.53×** — 0.65 GiB of pages
  occupying 0.16 GiB of RAM.
- **8 GiB of zram, half the RAM.** That is the reasonable ceiling: any larger
  and a run of incompressible pages could claim more memory than it saves.
  `comp_algorithm` only accepts writes while `disksize` is still unset, so that
  order in the script is not interchangeable.
- **The swapfile does not travel in the image.** 16 GiB of zeros would dwarf
  the flashable ZIP; it is created on the device, which is why the unit is
  ordered `After=` the root filesystem expansion: before that the filesystem is
  still the small one that was shipped.
- **`fallocate`, not `dd`.** ext4 with this kernel accepts a swapfile of
  unwritten extents — `mkswap` and `swapon` accept it, verified on the tablet —
  so creation is instant and adds nothing to boot. The `dd` is kept only as a
  fallback should `swapon` ever reject the file.
- **16 GiB, larger than RAM, on purpose.** It is 2 % of the free space and
  leaves hibernation arithmetically possible for anyone who wants to wire up
  `resume=`; it is not promised to work.
- **`vm.swappiness = 100` and `vm.page-cluster = 0`** (`90-gts9u-swap.conf`).
  With a compressed tier in front, evicting an anonymous page is cheap and
  worth preferring over dropping cache. The 180 of a zram-only machine is not
  used, because here there is a swapfile behind. `page-cluster = 0` because
  zram has no seek penalty and reading clusters of pages only wastes
  decompression.

Validated after an unattended reboot: both units active, the swapfile reused
rather than recreated, and a test that reserves 11 GiB without a single OOM
kill.

Both sizes are fixed, the same on the SM-X710. With its 8 or 12 GB of RAM,
8 GiB of zram is more than half of it there; that has not been revisited.

## Ubuntu's own initramfs

**The postmarketOS initramfs is not reused.** Ubuntu generates its own with
`initramfs-tools`, and it has to meet four requirements that do not come by
default:

1. **Find the root without device numbers.** The root is given as
   `root=LABEL=UBTS9U_UFS` (`UBTS9_UFS` on the SM-X710), never `sda34` or
   `mmcblk1p2`. The enumeration order
   of the UFS LUNs and of `sdhc_2` is not guaranteed, and the label is also
   what separates the internal installation from an old microSD.
2. **Wait for the device to appear.** `rootwait` is already in `vendor_boot`'s
   cmdline; on top of that the local hook retries `blkid` instead of dropping
   to the emergency shell on the first failure.
3. **Be packed in legacy LZ4.** This is non-negotiable: the X910's ABL
   concatenates the generic ramdisk from `init_boot` with the fragment from
   `vendor_boot`, and with a gzip generic ramdisk Linux rejects the initrd with
   "invalid magic at start of compressed archive" even though the Android image
   is valid. `initramfs-tools` is configured with `COMPRESS=lz4` and the
   packager checks for the `02 21 4c 18` magic.
4. **Fit in 8 MiB minus the AVB footer.** `MODULES=most`, never `dep`: `dep`
   inspects the build host's root device and fails with "failed to determine
   device for /". `make-initramfs.sh` enforces the 8388608 − 8192 byte
   budget. Since this port's critical providers are built in, the initramfs
   needs no large module tree; if it still does not fit, prune what it
   carries (the device package's `zz-gts9u-prune` hook) rather than cut needed
   drivers. There is no separate boot partition on the UFS install.

The validation script checks all four against the generated image, flashing
nothing.

## Firmware

The repository contains no blobs. `scripts/stage-firmware.py` stages them from
the owner's stock Samsung package against a per-revision manifest in
`configs/firmware/` and writes a verified directory plus `PROVENANCE.json`;
the build reads it through `FIRMWARE_DIR` (see
[`configs/firmware/README.md`](../configs/firmware/README.md)).
`build-rootfs-overlay.sh` installs it into the Debian hierarchy:

| Contents | Path in the Ubuntu root filesystem |
|---|---|
| Adreno 740 GPU (`a740_*`, `gmu_gen70200.bin`) | `/lib/firmware/qcom/` |
| Samsung ADSP (`adsp*.mdt`, `adsp*.bNN`, `*.jsn`) | `/lib/firmware/qcom/sm8550/` |
| AudioReach topology | `/lib/firmware/qcom/sm8550/Samsung-Galaxy-Tab-S9-Ultra-tplg.bin` (SM-X910), `Samsung-Galaxy-Tab-S9-tplg.bin` (SM-X710) |
| SM-X910 Wi-Fi, WCN7850 (official amss + QRD BDF in ELF) | `/lib/firmware/ath12k/WCN7850/hw2.0/` |
| SM-X710 Wi-Fi, WCN6855/QCA6490 (linux-firmware amss and m3 for hw2.0, the IOE 04866.5 build for hw2.1, Samsung's board data by default; `GTS9WIFI_ATH11K_BOARD` and `GTS9WIFI_ATH11K_REGDB` switch it) | `/lib/firmware/ath11k/WCN6855/hw2.0/` and `hw2.1/` |
| Bluetooth: `hmtbtfw20.tlv`, `hmtnv20.b21` (SM-X910); `hpbtfw21.tlv` and the `hpnv21[g].*` set (SM-X710) | `/lib/firmware/qca/` |
| CS35L45 speaker protection (`cs35l45-dsp1-spk-prot.wmfw`, `.bin`, `-calib.bin`): outside the manifests, so only if copied into `FIRMWARE_DIR` by hand. Not loaded yet on the SM-X910; the SM-X710 test image had them and its DSP loads them | `/lib/firmware/` |
| Keyboard cover MCU (`stm32_gts9family.bin`, both tablets) | `/lib/firmware/keyboard_stm/` |
| Sensors' HexagonFS tree | `/usr/share/qcom/sm8550/Samsung/gts9uwifi/` (SM-X910), `.../Samsung/gts9wifi/` (SM-X710) |

In Ubuntu `/lib` is a symlink to `/usr/lib`, as it is in the postmarketOS
initramfs. This is the same trap that broke v0.69 of that port: the overlay
must be written into `/usr/lib/firmware/...`, never creating a `/lib` directory
over the symlink.

Every Bluetooth file of the board (`DEVICE_BT_FW` in `scripts/lib/device.sh`)
must **also** go in `vendor_boot`'s vendor fragment, because `hci_qca` is built
in and probes before the root is mounted. On the SM-X710 the five WCN6855
hw2.1 files go there too, for the same reason: ath11k is built in and probes
at about 11.7 s, before the root is mounted
([`gts9wifi-wifi.md`](gts9wifi-wifi.md)).

## User, locale and input

- **The image carries no account.** Since v0.19 the owner creates it in GNOME's
  first-run wizard (`gnome-initial-setup`), which GDM launches when the machine
  has no ordinary account and `InitialSetupEnable=true` is set in
  `/etc/gdm3/custom.conf`. Name, password, language, keyboard and time zone are
  chosen there.

  This is not only convenience: the account used to be created with a password
  passed in `GTS9U_PW`, which cannot live in the repository, so **nobody who
  did not know it could make a clean build**. A release now needs no secret at
  all.

  `GTS9U_PW` still exists for development images, where working SSH before
  anyone touches the screen matters more.

  One consequence worth knowing when reaching a tablet over SSH: the account's
  name and password are whatever was typed in that wizard, and they change with
  every reinstall. So do the host keys: `build-release.sh` removes the build's
  keys and the tablet generates its own on first boot
  (`ubuntu-gts9u-ssh-host-keys.service`).
- Nothing in the port may name a specific user any more. The flashlight
  extension is enabled through a gschema override, and
  `ubuntu-gts9u-desktop-user` resolves the ordinary account on every boot,
  enables its lingering, applies device groups and generates the relays'
  drop-in under `/run`.
- Locale `en_US.UTF-8`, time zone UTC and keyboard `us` as a neutral starting
  point; the wizard asks for all three.
- A hostname different from postmarketOS's, so two systems on the same LAN
  cannot be confused.
- 200 % scaling by default: 2960×1848 on 14.6" is unusable at 100 %.
- SSH is enabled, with password authentication on and root login off. A key
  is added only when a development build sets `GTS9U_AUTHORIZED_KEYS=<file>`:
  it goes into `/etc/skel/.ssh`, so the wizard's account inherits it (or into
  the `GTS9U_USER` home when `GTS9U_PW` is set). No password lives in the
  repository. `build-release.sh` refuses a root with either, through
  `sanitize-release-rootfs.py`.

## Build pipeline

Any x86-64 Linux host works, native or WSL: the author builds inside
`wsl.exe -d Ubuntu-24.04`, and the SM-X710 was built natively. Everything is
based in `UBUNTU_WORKDIR` (default `/root/ubuntu-gts9u`), which must be a path
without spaces. No script accepts a block device or writes to a partition.

### Prerequisites

- **The repository with its full history:** `git clone
  https://github.com/agcarbajo/ubuntu-galaxy-tab-s9-ultra.git`, not
  `--depth 1` and not a ZIP download. SM-X910 builds read pinned files (the
  ABL-facing DTS, the panel driver) out of commit `5046f92f`.
- **Host tools.** `sudo bash scripts/install-build-deps.sh` installs the
  rootfs, image and packaging tools only. The kernel and the firmware staging
  need more:

  ```sh
  sudo apt install clang lld llvm make bc flex bison libssl-dev libelf-dev \
      dwarves git kmod openssl patch mtools erofs-utils file gcc libjson-c-dev \
      android-sdk-libsparse-utils
  ```

  Linux 7.2 needs Clang 17 or newer; `/usr/lib/llvm-22` is used when it is
  installed. Without `dwarves` (pahole) `olddefconfig` silently drops
  `CONFIG_DEBUG_INFO_BTF`, which the released kernels have.
  `scripts/check-build-deps.sh` checks only what `install-build-deps.sh`
  installs, and exits 1 when any of it is missing (so `install-build-deps.sh`
  fails too).
- **The Android boot tools.** `scripts/stage-android-tools.sh` copies them
  from a postmarketOS chroot (`PMOS_ROOTFS_CHROOT`) and fails without one.
  Otherwise copy `mkbootimg.py` **with its `gki/` directory** from
  https://android.googlesource.com/platform/system/tools/mkbootimg, and
  `avbtool.py` from https://android.googlesource.com/platform/external/avb,
  into `$UBUNTU_WORKDIR/tools`, or point `ANDROID_TOOLS` at them. The bundle
  uses only those two.
- **The staged firmware** for the tablet's model and bootloader revision:
  [`configs/firmware/README.md`](../configs/firmware/README.md).
- **Network access** throughout: kernel.org, GitHub (raw.githubusercontent.com
  too), gitlab.freedesktop.org, git.libcamera.org, git.launchpad.net,
  codeberg.org and the Ubuntu archives.
- **Disk:** the kernel worktree and object tree (`KERNEL_WORKTREE` and
  `KERNEL_BUILD_DIR` can put them on another disk), `$UBUNTU_WORKDIR/buildroot`
  (the arm64 chroot the package builders use), a ~4 GiB rootfs tree, the UFS
  image (4.0 GiB for the SM-X710 v1.2.0, capped by `UFS_IMAGE_MAX_MB`) and a
  ~1.1 GB ZIP.

### Who needs root

`stage-firmware.py`, `fetch-mainline.sh`, `stage-android-tools.sh` and
`build-mainline-kernel.sh` run unprivileged: build the kernel as your own
user. `build-release.sh` needs root for the rest (mmdebstrap and the package
builders' chroots, loop and bind mounts, `chown root:root` in the package
builders). `sudo` resets the environment, and every variable then falls back
to the Ultra and `/root` defaults: `DEVICE` becomes `gts9uwifi`,
`UBUNTU_WORKDIR` becomes `/root/ubuntu-gts9u`, `FIRMWARE_DIR` points at a path
that does not exist, and without `SKIP_KERNEL=1` the kernel is rebuilt as root.
So `export DEVICE=gts9wifi; sudo bash scripts/build-release.sh` builds the
Ultra. Pass every variable on the sudo command line with `sudo env`.

### Building a release

```sh
export W=$HOME/ubuntu-gts9u              # UBUNTU_WORKDIR, no spaces
UBUNTU_WORKDIR=$W bash scripts/fetch-mainline.sh
UBUNTU_WORKDIR=$W bash scripts/stage-android-tools.sh   # or stage them by hand, above
# firmware into $W/firmware/x710-bl5: configs/firmware/README.md

# kernel: unprivileged; KERNEL_CLEAN=1 for anything you publish
UBUNTU_WORKDIR=$W DEVICE=gts9wifi KERNEL_CLEAN=1 bash scripts/build-mainline-kernel.sh

# everything else as root, every variable passed explicitly
sudo env UBUNTU_WORKDIR=$W DEVICE=gts9wifi FIRMWARE_DIR=$W/firmware/x710-bl5 \
    SKIP_KERNEL=1 GTS9U_INCLUDE_NPU=0 bash scripts/build-release.sh
```

For the SM-X910 use `DEVICE=gts9uwifi` (the default) and
`FIRMWARE_DIR=$W/firmware/x910-bl<rev>`. Keep `GTS9U_INCLUDE_NPU=0` unless the
NPU inputs are staged in `$W/npu-release-inputs` and `$W/npu-release-firmware`;
they exist only for the SM-X910. If the kernel was built with
`KERNEL_WORKTREE=` and `KERNEL_BUILD_DIR=` (on another disk, for example), pass
the same two values to `build-release.sh` and keep that disk mounted: the
rootfs stage builds the SPSS IRQ module, the device package and the NPU
package against that object tree, and checks its signing certificate and
`.config`.

The result is `artifacts/ubuntu-24.04-<sm-x910|sm-x710>-bl<rev>-v<version>.zip`
in the repository (`ARTIFACTS_DIR`), with
`MANIFEST-<sm-x910|sm-x710>-bl<rev>-v<version>.txt`
beside it; without a firmware staging both names lose `-bl<rev>`. The
version comes from `VERSION` unless `RELEASE_VERSION` is set. Flashing it is
in [`boot-strategy.md`](boot-strategy.md#exact-installation-procedure).

Things that bite:

- **`KERNEL_CLEAN=1`.** `build-release.sh` does not set it; pass it to the
  kernel run, standalone as above or through `build-release.sh` without
  `SKIP_KERNEL`. It also recreates `KERNEL_WORKTREE` from the pinned HEAD and
  mints a new signing key, so the modules and `boot.img` of that run are one
  set and must ship together.
- **Output paths.** With `build-release.sh`, leave `KERNEL_OUT_DIR`,
  `BUNDLE_OUT_DIR`, `OVERLAY_OUT_DIR` and `INITRAMFS_OVERLAY_OUT_DIR` unset:
  it reads and zips the default `$UBUNTU_WORKDIR/out` paths, so an override
  ships a stale or missing bundle.
- **Both tablets from one `UBUNTU_WORKDIR`.** Only the kernel paths are
  per device. Set `ROOTFS_DIR=$W/rootfs-$DEVICE` (it has to be inside
  `UBUNTU_WORKDIR`). The ZIP and manifest names carry the model, so both
  builds can share `ARTIFACTS_DIR`; `out/bundle` and the overlays are shared,
  so the second build replaces the first's.
  Use `SKIP_ROOTFS=1` only when that rootfs was built for the same `DEVICE`
  and kernel: it is reused as it is and only stamped with the current
  `DEVICE`.
- **Development switches.** `build-release.sh` passes the root through
  `sanitize-release-rootfs.py`, which refuses an account, a password or an
  SSH key in it, so `GTS9U_PW` and `GTS9U_AUTHORIZED_KEYS` cannot produce a
  release.

### Environment

| Variable | Default | Use |
|---|---|---|
| `DEVICE` | `gts9uwifi` | `gts9uwifi` (SM-X910) or `gts9wifi` (SM-X710); unset selects the SM-X910, and the device axis itself adds no SM-X910 difference |
| `UBUNTU_WORKDIR` | `/root/ubuntu-gts9u` | the build base |
| `FIRMWARE_DIR` | the author's old Windows path | the `stage-firmware.py` staging; required |
| `SKIP_KERNEL`, `SKIP_ROOTFS` | `0` | `1` reuses `out/kernel-$DEVICE` or the rootfs |
| `KERNEL_CLEAN` | `0` | `1` builds the kernel from scratch; required for anything published |
| `KERNEL_SRC`, `KERNEL_WORKTREE`, `KERNEL_BUILD_DIR` | `$W/linux-mainline`, `$W/build/linux-src-$DEVICE`, `$W/build/linux-$DEVICE` | pinned checkout, disposable worktree, object tree |
| `GTS9U_INCLUDE_NPU` | `1` | `0` unless the NPU inputs are staged; always `0` on the SM-X710 |
| `ROOTFS_DIR` | `$W/rootfs` | inside `UBUNTU_WORKDIR` |
| `ARTIFACTS_DIR` | `artifacts/` in the repository | where the ZIP and manifest go |
| `RELEASE_VERSION` | `VERSION` | the release version |
| `GTS9U_PROFILE` | `desktop` | or `minimal` |
| `UFS_IMAGE_MAX_MB`, `ROOT_SLACK_MB` | `8192`, `1152` | image budget and slack, below |
| `GTS9U_FINGERPRINT_FIRMWARE_DIR` | unset | SM-X910 only: opt in to the proprietary fingerprint TA |

Development only, never in a release: `GTS9U_PW` and `GTS9U_AUTHORIZED_KEYS`
(see above); `GTS9U_USB_GADGET=1` (an NCM network at 10.42.0.2 and an ACM
console on the Type-C port) and `GTS9U_BRINGUP_SDLOG=1` (kernel logs written
to the microSD card on every boot); `GTS9U_EXTRA_CMDLINE` and
`GTS9U_DROP_CMDLINE` (`vendor_boot` only); `GTS9WIFI_ATH11K_BOARD` and
`GTS9WIFI_ATH11K_REGDB` (SM-X710 Wi-Fi data experiments);
`BUILD_WIFI_MODULES=0` (ships no modules at all); `ENABLE_RUNTIME_DTBO=1`
(does not boot).

### The stages

| Step | Script | Produces |
|---|---|---|
| 0 | `install-build-deps.sh` (root) / `check-build-deps.sh` | the rootfs, image and packaging host tools; the kernel and staging tools are above |
| 0 | `fetch-mainline.sh` | `$W/linux-mainline`, pinned at `a13c140c` (7.2-rc3) |
| 0 | `stage-android-tools.sh` | `mkbootimg.py` with `gki/`, `mkdtboimg.py` and `avbtool.py` in `$W/tools` |
| 0 | `stage-firmware.py stage` | the verified `FIRMWARE_DIR` and its `PROVENANCE.json` |
| 1 | `build-mainline-kernel.sh` (no root) | in `$W/out/kernel-$DEVICE`: `Image.gz`, `sm8550-samsung-$DEVICE.dtb`, `config`, `kernel.release`, `SHA256SUMS`, `fingerprint.layers`, and `modules-root/` (about 60 signed modules: ath12k, qcomtee, snvm, the SPSS stack, netfilter, `v4l2loopback`, and `icc-bwmon` on the SM-X710) |
| 2 | `build-ubuntu-rootfs.sh` (root) | builds or reuses every local `.deb` (companion, device, libfprint, gnome-settings-daemon, fingerprint owner, SPSS IRQ module, NPU, fprintd, sensors, camera, extras, PipeWire AAC) into `$W/out/packages`, then runs `mmdebstrap` into the rootfs |
| 2 | `sanitize-release-rootfs.py` | refuses personal data in the rootfs and removes the build's SSH host keys |
| 3 | `build-rootfs-overlay.sh` | the module and firmware overlay `$W/out/rootfs-overlay`, and `$W/out/initramfs-overlay` for the `vendor_boot` fragment |
| 4 | `build-ufs-image.sh` (root) | Ubuntu's initramfs and the root's ext4 image `$W/out/ubuntu-$DEVICE[-bl<rev>]-v<version>-ufs.img`, with no partition table |
| 5 | `build-android-v4-bundle.sh` | `boot`, `init_boot`, `vendor_boot`, `dtbo`, `vbmeta` in `$W/out/bundle` |
| 6 | `build-update-payload.py` + `make-twrp-zip.py` | the update payload, and a deterministic TWRP ZIP with the root inside |
| 7 | `validate-bundle.sh` + `validate-update-zip.py` | static validation, flashing nothing |
| — | `make-initramfs.sh` | a checked legacy-LZ4 initramfs; both image builders use it |
| — | `build-sd-image.sh` | the two-partition microSD image, how installing worked up to v0.17; legacy, kept for reference: a current bundle boots `root=LABEL=UBTS9U_UFS` or `UBTS9_UFS`, so an SD image built today does not boot without a matching cmdline |
| — | `build-release.sh` (root) | chains 1–7 and writes the manifest |

`import-kernel-sources.sh` is not a build step. It re-imports from the frozen
postmarketOS v1.71 port and overwrites the versioned `kernel/` and `configs/`
sources; a builder never runs it, because it would discard this repository's
own changes, the SM-X710 support included.

To re-check a ZIP outside `build-release.sh`, run `DEVICE=<board>
UBUNTU_WORKDIR=$W bash scripts/validate-bundle.sh <zip>` and `DEVICE=<board>
python3 scripts/validate-update-zip.py <zip>` with the `DEVICE` the ZIP was
built for; without it both judge the ZIP as an SM-X910 one.
`validate-bundle.sh` also reads `$W/out/bundle` and the kernel outputs.

To rebuild a package on its own: `build-device-package.sh` and
`build-companion-package.sh` call `chown root:root`, so run them as root or
under `fakeroot`, with `UBUNTU_WORKDIR` set; the `.deb`s land in
`$UBUNTU_WORKDIR/out/packages` (`DEB_OUT_DIR`). The device package also
needs the board's `DEVICE` (without `DEVICE=gts9wifi` the SM-X710 gets the
Ultra's sensor-registry path and camera labels, and rotation and the light
sensor fail), the outputs of `build-fingerprint-secure-owner.sh` and
`build-spss-irq-module.sh` (one full release run leaves both behind), and the
same `KERNEL_BUILD_DIR` and `KERNEL_OUT_DIR` as the kernel build, or it
refuses on a signing-key or `.config` mismatch.

`make-initramfs.sh` fails the build if the initramfs is not legacy LZ4 or does
not fit in `init_boot`. Those two errors are found here rather than on the
tablet, and they live in one place so the UFS image and the microSD one cannot
diverge on exactly that.

`build-ufs-image.sh` produces the filesystem alone: no GPT, with `/boot`
inside, with the firmware overlay already integrated, labelled
`$DEVICE_LABEL` (`UBTS9U_UFS` or `UBTS9_UFS`), and reserving enough descriptor
blocks (`-E resize=`) for the first boot to grow online to 1 TiB. It refuses
to write to a block device and aborts if the image exceeds its size budget,
because it travels whole inside the ZIP.

Two numbers in that script are deliberate and connected:

- **`-m 1` instead of ext4's default 5 % reserve.** The reserve is a percentage
  held in the superblock, so it survives the resize: 5 % of the partition the
  root grows into would be tens of gigabytes the owner never gets back.
- **The slack, and the budget it needs.** The installer seeds two boot sets of
  216 MiB each into this filesystem before it grows, so it must have room for
  both. The budget (`UFS_IMAGE_MAX_MB`) is now 8192 MiB: first raised from
  4096 for those two boot sets, then again for the NPU runtime. The slack is
  `ROOT_SLACK_MB`, 1152 MiB by default; empty space costs almost nothing in
  the ZIP, where 256 MiB of slack measured 784 KB.

## Validation order

1. `systemd` reaches `multi-user.target` with a persistent journal.
2. Wi-Fi and SSH.
3. GDM and a GNOME Wayland session.
4. Accelerated GPU checked with `eglinfo`/`vulkaninfo`, not assumed.
5. The rest of the hardware parity.

No component is marked working in the matrix merely because its driver bound.
