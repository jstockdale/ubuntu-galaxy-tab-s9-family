# Per-revision firmware manifests

One TOML file per **(device, bootloader revision, stock build)** describes
every proprietary or pinned file the build stages, where it comes from, and
its SHA-256. The blobs themselves never enter Git — only their identities do,
exactly as `adsp-firmware.sha512` already did in the reference port.
`scripts/stage-firmware.py` turns a manifest plus the owner's stock firmware
package into a verified staging directory (the `FIRMWARE_DIR` the build
consumes), and records what it did in a `PROVENANCE.json` that the build
stamps into the image, the release manifest and the TWRP ZIP.

Naming: `<device>-bl<rev>-<BUILDID>.toml`, e.g. `x910-bl5-X910XXS5CYG1.toml`.
The build id is Samsung's, and character 8 of it is the bootloader (anti-
rollback) revision: `X910XXS5CYG1` → bl5, `X910XXU1AWHA` → bl1. A device
whose fuses are at revision N boots images signed for revision ≥ N only, so
the TrustZone-authenticated blobs must come from a package matching the
unit's revision. That is the entire reason this directory exists.

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

Stage (verify every hash, refuse anything unexpected, write provenance):

    scripts/stage-firmware.py stage \
        --manifest configs/firmware/x910-bl5-X910XXS5CYG1.toml \
        --odin XAR-X910XXS5CYG1-20250728211620.zip \
        --vendor-image out-x910/parts/vendor.img \
        --sscregistrygen PATH --out "$UBUNTU_WORKDIR/firmware/x910-bl5"

`PATH` is `tools/sscregistrygen` from sensh v2.0.0, the generator that
reproduces the pinned `sensor-hexagonfs.tar.gz` of both X910 revisions byte
for byte. Both commands refuse a manifest with a `generated:` entry when it is
missing, rather than staging or recording it without the sensor registry.
The generator writes registry files without truncating them and walks its
input in directory order, so where two stock configs fill the same entry
(board-revision variants such as `kailua_ak991x_0`/`_5`) the result follows
the order of the temporary directory: generate on a tmpfs `/tmp`, where the
pins reproduce. The pinned sha256 turns any difference into a staging failure.

Record a manifest for a new revision from an existing one's structure
(measures hashes rather than enforcing them — review the diff, then commit):

    scripts/stage-firmware.py record \
        --like configs/firmware/x910-bl5-X910XXS5CYG1.toml \
        --build-id X910XXU1AWHA --odin <rev-1 zip> ... \
        --sscregistrygen PATH --write configs/firmware/x910-bl1-X910XXU1AWHA.toml

Then build with `FIRMWARE_DIR` pointed at the staged output. The release
pipeline reads `PROVENANCE.json` from there and names the artifact
`ubuntu-24.04-sm-x910-bl<rev>-v<version>.zip`; the installer compares the
ZIP's declared revision against the tablet's `ro.bootloader` and refuses a
mismatch unless the ZIP carries an `ALLOW-FIRMWARE-MISMATCH` member (add one
with any archive manager, the same way `ANDROID-PERCENT` is edited).

Segment-count differences between revisions (`adsp.b*` and friends) are
expected and fine: manifests list each revision's real set, and the overlay
copies whatever the staged set contains.

Not yet covered here, by intention: the NPU CDSP set (still pinned by
`configs/npu/stock-cdsp.sha256`, revision-specific, staged by
`stage-npu-firmware.py`) and the fingerprint per-unit import
(`import-fingerprint-firmware.sh`) — both join the manifest scheme when
their features are next touched. The `fingerprint` group in a manifest
already stages the per-revision TrustZone apps those flows will consume.
