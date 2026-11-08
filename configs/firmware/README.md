# Per-revision firmware manifests

One TOML file per **(device, bootloader revision, stock build)** describes
every proprietary or pinned file the build stages, where it comes from, and
its SHA-256. The blobs themselves never enter Git — only their identities do,
exactly as `adsp-firmware.sha512` already did in the reference port.
`scripts/stage-firmware.py` turns a manifest plus the owner's stock firmware
package into a verified staging directory (the `FIRMWARE_DIR` the build
consumes), and records what it did in a `PROVENANCE.json` that the build
stamps into the image, the release manifest and the TWRP ZIP.

Naming: `<model>-bl<rev>-<BUILDID>.toml`, where `<model>` is the short
Samsung model (`x910`, `x710`), not the `DEVICE` codename that `[package]
device` holds (`gts9uwifi`, `gts9wifi`): e.g. `x910-bl5-X910XXS5CYG1.toml`,
`x710-bl5-X710XXS5CYG1.toml`.
The build id is Samsung's, and character 8 of it is the bootloader (anti-
rollback) revision: `X910XXS5CYG1` → bl5, `X910XXU1AWHA` → bl1. A device
whose fuses are at revision N boots images signed for revision ≥ N only, so
the TrustZone-authenticated blobs must come from a package matching the
unit's revision. That is the entire reason this directory exists.

Manifests exist for the SM-X910 at revisions 1 and 5 and for the SM-X710 at
revision 5. Read the tablet's build id before staging: `adb shell getprop
ro.bootloader` in One UI or TWRP prints e.g. `X710XXS5CYG1`, whose eighth
character, `5`, is the revision. Stage from the stock package with exactly
that build id. Any other revision needs `stage-firmware.py record` first (see
below).

## File classes

Every entry carries a `class` that states the matching policy — the policy
lives in data, not in prose:

| class | meaning |
|---|---|
| `tz-auth` | Authenticated by the device's TrustZone against its fused rollback state (GPU zap shader, ADSP images, TrustZone apps). Must come from the target revision's own package. |
| `loose` | Loaded by Linux or by chip-side verification, not by the device TZ (Wi-Fi/BT firmware, sensor inputs). Tracked per revision; mixable across revisions if a port ever needs to. |
| `external-pinned` | Public, redistributable-by-reference files pinned to linux-firmware URLs or commits. Revision-independent. |
| `accessory-pinned` | Firmware for an external accessory MCU with no coupling to the tablet's boot chain. Pinned to one specific blob across **all** revisions — the STM32 keyboard image must be V37 regardless of the stock package's own copy, because the mainline driver only speaks to V37. |
| `derived` | Deterministically transformed from other staged entries (BDF payload extraction, board-2 container build, the topology SD1 one-byte patch). Inputs and output are both pinned. |
| `generated` | Produced by a pinned generator from pinned inputs (the HexagonFS sensor registry). The inputs, the generator identity and the output are all recorded. |

`per-unit` data — fingerprint calibration (`calib.dat`, `egoptbds.dat`,
`cell_id`) — is deliberately **not** representable here. It comes from one
physical tablet, not from a firmware package, and is imported separately.

## Source syntax

| source | resolved from |
|---|---|
| `vendor:PATH` | the extracted stock `vendor` image (`--vendor-root` or `--vendor-image`) |
| `vendor@BUILDID:PATH` | another build's vendor tree (`--aux-vendor BUILDID=DIR`) — used by `accessory-pinned` entries |
| `nonhlos:PATH` | the BL tar's `NON-HLOS.bin` vfat image (`--nonhlos`, or streamed from `--odin`) |
| `dspso:PATH` | the BL tar's `dspso.bin` ext4 image (`--dspso`, or streamed from `--odin`) |
| `url:HTTPS-URL` | a pinned download, cached |
| `lfw:COMMIT:PATH` | a sparse checkout of linux-firmware at that commit, cached |
| `derived:TRANSFORM[:ARG]` | a built-in transform over already-staged entries |
| `generated:sensor-hexagonfs` | the sensor-registry pipeline (`--sscregistrygen`) |

## Workflow

The tool runs without root. It needs Python 3.11 or newer (`tomllib`),
`unzip`, `tar`, `lz4`, `file`, `git`, `mtools` (NON-HLOS), `e2fsprogs`
(`debugfs`, for dspso and an ext4 vendor image) and `erofs-utils`
(`fsck.erofs`, for an EROFS vendor image). It fetches the pinned `url:` and
`lfw:` entries over the network (git.kernel.org, raw.githubusercontent.com)
and caches them, with every extraction, in `~/.cache/gts9u-stage-firmware`
(`--cache` moves it); `--offline` reuses the cache and fails on anything not
in it.

`--odin` supplies `NON-HLOS.bin` and `dspso.bin`, streamed out of the BL tar.
The vendor partition is not extracted for you: it is a logical partition
inside the AP tar's `super` image. One way, with `simg2img` from
`android-sdk-libsparse-utils` and AOSP's `lpunpack` (liblp), and room for the
super image three times over (compressed, sparse and raw):

    unzip -p XAR-X710XXS5CYG1-20250728211550.zip 'AP_*.tar.md5' | tar -xf - super.img.lz4
    lz4 -d super.img.lz4 super.img.sparse
    simg2img super.img.sparse super.img    # only if `file` calls it an Android sparse image
    lpunpack -p vendor super.img parts/

Pass `parts/vendor.img` (EROFS on these packages) as `--vendor-image`, or an
already extracted tree as `--vendor-root`.

Stage (verify every hash, refuse anything unexpected, write provenance). The
SM-X710:

    scripts/stage-firmware.py stage \
        --manifest configs/firmware/x710-bl5-X710XXS5CYG1.toml \
        --odin XAR-X710XXS5CYG1-20250728211550.zip \
        --vendor-image parts/vendor.img \
        --sscregistrygen sensh/tools/sscregistrygen \
        --out "$UBUNTU_WORKDIR/firmware/x710-bl5"

The SM-X910. Both X910 manifests take the keyboard image from the bl5
package's vendor tree (`vendor@X910XXS5CYG1:`), so they also need
`--aux-vendor` with an extracted X910XXS5CYG1 vendor tree, for bl5 the same
tree as `--vendor-root`:

    mkdir vendor-x910-bl5 && fsck.erofs --extract=vendor-x910-bl5 parts/vendor.img
    scripts/stage-firmware.py stage \
        --manifest configs/firmware/x910-bl5-X910XXS5CYG1.toml \
        --odin XAR-X910XXS5CYG1-20250728211620.zip \
        --vendor-root vendor-x910-bl5 \
        --aux-vendor X910XXS5CYG1=vendor-x910-bl5 \
        --sscregistrygen sensh/tools/sscregistrygen \
        --out "$UBUNTU_WORKDIR/firmware/x910-bl5"

`--sscregistrygen` is `tools/sscregistrygen` from sensh v2.0.0: `git clone
--branch v2.0.0 https://gitlab.com/flamingradian/sensh`, then `make -C
sensh/tools sscregistrygen` (needs `gcc` and `libjson-c-dev`). It reproduces
the pinned `sensor-hexagonfs.tar.gz` of every manifest here (both X910
revisions and the X710 bl5) byte for byte. Both commands refuse a manifest
with a `generated:` entry when it is missing, rather than staging or recording
it without the sensor registry.
The generator writes registry files without truncating them and walks its
input in directory order, so where two stock configs fill the same entry
(board-revision variants such as `kailua_ak991x_0`/`_5`) the result follows
the order of the temporary directory: generate on a tmpfs `/tmp`, where the
pins reproduce. The pinned sha256 turns any difference into a staging failure.

Record a manifest for a new revision from an existing one's structure
(measures hashes rather than enforcing them — review the diff, then commit):

    scripts/stage-firmware.py record \
        --like configs/firmware/x910-bl5-X910XXS5CYG1.toml \
        --build-id X910XXU1AWHA --odin <rev-1 zip> --vendor-image <vendor.img> \
        --aux-vendor X910XXS5CYG1=<bl5 vendor tree> \
        --sscregistrygen sensh/tools/sscregistrygen \
        --out "$UBUNTU_WORKDIR/firmware/x910-bl1" \
        --write configs/firmware/x910-bl1-X910XXU1AWHA.toml

`record` requires both `--write` (the new manifest) and `--out` (the staging
it measured, with its `PROVENANCE.json`).

Then build with `FIRMWARE_DIR` pointed at the staged output (see
[`docs/ubuntu-userspace.md`](../../docs/ubuntu-userspace.md#building-a-release)).
The release pipeline reads `PROVENANCE.json` from there and names the
artifact `ubuntu-24.04-<sm-x910|sm-x710>-bl<rev>-v<version>.zip`, with
`MANIFEST-<sm-x910|sm-x710>-bl<rev>-v<version>.txt` beside it. The installer reads the
tablet's bootloader build id from `androidboot.bootloader` on the kernel
command line or in bootconfig (`ro.bootloader` only as a fallback), compares
its character 8 with the ZIP's declared revision, and refuses a mismatch
unless the ZIP carries an `ALLOW-FIRMWARE-MISMATCH` member (add one with any
archive manager, the same way `ANDROID-PERCENT` is edited).

Segment-count differences between revisions (`adsp.b*` and friends) are
expected and fine: manifests list each revision's real set, and the overlay
copies whatever the staged set contains.

Not yet covered here, by intention: the NPU CDSP set (still pinned by
`configs/npu/stock-cdsp.sha256`, revision-specific, staged by
`stage-npu-firmware.py`) and the fingerprint per-unit import
(`import-fingerprint-firmware.sh`) — both join the manifest scheme when
their features are next touched. The `fingerprint` group in a manifest
already stages the per-revision TrustZone apps those flows will consume.
The NPU set exists only for the SM-X910; build the SM-X710 with
`GTS9U_INCLUDE_NPU=0`. Also outside the manifests: the CS35L45
speaker-protection files (`cs35l45-dsp1-spk-prot.wmfw`, `.bin`,
`-calib.bin`), which `build-rootfs-overlay.sh` installs only if they are
present in `FIRMWARE_DIR`, so they are copied in by hand when wanted. The
SM-X710 image that was tested had all three copied into its staging this way
(they match `firmware/` in the X710XXS5CYG1 vendor image), and on that unit
the DSP loads them (`DSP1: Firmware: 4fa00 vendor: 0x2 v0.56.0`, tuning named
TabS9). An SM-X710 build without them has not been tested; adding them to the
X710 manifest is a follow-up.

A staging made before a manifest changed is stale: the X710 manifest gained
`regdb.bin` on 2026-11-07, and the overlay requires it. Stage again rather
than adding files to a staging by hand.
