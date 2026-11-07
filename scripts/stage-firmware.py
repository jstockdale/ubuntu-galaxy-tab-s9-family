#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Stage the proprietary firmware for one (device, bootloader revision) target.

A manifest under configs/firmware/ declares every staged file, its source
inside the owner's stock firmware package (or a pinned public location) and
its SHA-256. This tool resolves the sources without root — streamed Odin tar
members, mtools for the NON-HLOS vfat, debugfs for the dspso ext4,
fsck.erofs for the vendor image — verifies every hash, applies the pinned
deterministic transforms, and writes a PROVENANCE.json the build stamps into
the image, the release manifest and the TWRP ZIP.

    stage-firmware.py stage  --manifest M --out DIR [sources...]
    stage-firmware.py record --like M --build-id ID --write M2 [sources...]

`stage` refuses any byte that does not match the manifest. `record` takes an
existing manifest as the structural template, measures the new package's
hashes instead of enforcing them, and writes a new manifest for review.
Entries whose class is `accessory-pinned`, `external-pinned` or `derived`
from pinned inputs keep their pinned hashes even under `record`: those are
policy, not measurements. No blob ever enters Git and nothing is flashed.
"""
import argparse
import hashlib
import json
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

TOOL = "stage-firmware.py/1"
BOARD2_MAGIC = b"QCA-ATH12K-BOARD\0"
X910_BOARD_NAME = ("bus=pci,vendor=17cb,device=1107,subsystem-vendor=17cb,"
                   "subsystem-device=1107,qmi-chip-id=2,qmi-board-id=255")
QRD_BOARD_NAME = ("bus=pci,vendor=17cb,device=1107,subsystem-vendor=17cb,"
                  "subsystem-device=3378,qmi-chip-id=2,qmi-board-id=255")
# Classes whose hashes are policy rather than per-package measurements: they
# survive `record` unchanged so a new revision cannot silently drift them.
PINNED_CLASSES = {"accessory-pinned", "external-pinned"}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def run(argv, **kwargs):
    return subprocess.run(argv, check=True, capture_output=True, **kwargs)


class Sources:
    """Resolves manifest source strings to local files, caching extractions."""

    def __init__(self, args):
        self.cache = args.cache
        self.cache.mkdir(parents=True, exist_ok=True)
        self.odin = args.odin
        self.nonhlos = args.nonhlos
        self.dspso = args.dspso
        self.vendor_root = args.vendor_root
        self.vendor_image = args.vendor_image
        self.aux_vendor = dict(pair.split("=", 1) for pair in args.aux_vendor)
        self.offline = args.offline

    def _odin_bl_member(self, name: str) -> Path:
        """Stream one lz4 member out of the Odin package's BL tar."""
        if self.odin is None:
            flag = name.split('.')[0].lower().replace('-', '')
            raise SystemExit(f"{name} is needed; pass --{flag} or --odin ZIP")
        out = self.cache / f"odin-{self.odin.stem}-{name}"
        if out.is_file():
            return out
        listing = run(["unzip", "-Z1", str(self.odin)]).stdout.decode()
        member = next((l for l in listing.splitlines() if l.startswith("BL_")), None)
        if member is None:
            raise SystemExit(f"no BL_*.tar.md5 member in {self.odin}")
        with tempfile.TemporaryDirectory(dir=self.cache) as tmp:
            unzip = subprocess.Popen(["unzip", "-p", str(self.odin), member],
                                     stdout=subprocess.PIPE)
            run(["tar", "-x", "-C", tmp, f"{name}.lz4"], stdin=unzip.stdout)
            if unzip.wait() not in (0, 141):  # tar closing early causes EPIPE
                raise SystemExit(f"could not stream {member}")
            run(["lz4", "-d", "-f", "-q", f"{tmp}/{name}.lz4", str(out)])
        return out

    def nonhlos_image(self) -> Path:
        return self.nonhlos or self._odin_bl_member("NON-HLOS.bin")

    def dspso_image(self) -> Path:
        return self.dspso or self._odin_bl_member("dspso.bin")

    def vendor_tree(self, build_id=None) -> Path:
        if build_id is not None:
            if build_id not in self.aux_vendor:
                raise SystemExit(f"entry needs --aux-vendor {build_id}=DIR "
                                 "(an extracted vendor tree from that build)")
            return Path(self.aux_vendor[build_id])
        if self.vendor_root:
            return self.vendor_root
        if self.vendor_image is None:
            raise SystemExit("pass --vendor-root DIR or --vendor-image FILE")
        out = self.cache / f"vendor-{sha256_file(self.vendor_image)[:16]}"
        if not (out / "build.prop").exists():
            shutil.rmtree(out, ignore_errors=True)
            out.mkdir(parents=True)
            kind = run(["file", "-b", str(self.vendor_image)]).stdout.decode()
            if "EROFS" in kind:
                run(["fsck.erofs", f"--extract={out}", str(self.vendor_image)])
            else:
                run(["debugfs", "-R", f"rdump / {out}", str(self.vendor_image)])
        return out

    def fetch_url(self, url: str) -> Path:
        out = self.cache / ("url-" + hashlib.sha256(url.encode()).hexdigest()[:16]
                            + "-" + url.rsplit("/", 1)[1])
        if not out.is_file():
            if self.offline:
                raise SystemExit(f"--offline, and {url} is not cached")
            # kernel.org's cgit refuses urllib's default agent with a 403.
            request = urllib.request.Request(url, headers={"User-Agent": TOOL})
            with urllib.request.urlopen(request, timeout=60) as r, out.open("wb") as w:
                shutil.copyfileobj(r, w)
        return out

    def lfw_file(self, commit: str, rel: str) -> Path:
        repo = self.cache / f"lfw-{commit[:16]}"
        target = repo / rel
        if target.is_file():
            return target
        if self.offline:
            raise SystemExit(f"--offline, and linux-firmware {commit} is not cached")
        # One sparse clone per pinned commit; later entries from other
        # directories widen the checkout instead of wiping and re-cloning it.
        if not (repo / ".git").is_dir():
            shutil.rmtree(repo, ignore_errors=True)
            repo.mkdir(parents=True)
            url = "https://gitlab.com/kernel-firmware/linux-firmware.git"
            run(["git", "-C", repo, "init", "-q"])
            run(["git", "-C", repo, "remote", "add", "origin", url])
            run(["git", "-C", repo, "-c", "protocol.version=2", "fetch", "-q",
                 "--depth=1", "--filter=blob:none", "origin", commit])
            run(["git", "-C", repo, "sparse-checkout", "init", "--cone"])
            run(["git", "-C", repo, "sparse-checkout", "set",
                 str(Path(rel).parent)])
        else:
            run(["git", "-C", repo, "sparse-checkout", "add",
                 str(Path(rel).parent)])
        run(["git", "-C", repo, "checkout", "-q", "--detach", "FETCH_HEAD"])
        if not target.is_file():
            raise SystemExit(
                f"linux-firmware {commit} has no {rel} "
                "(check the path exists at that commit, e.g. with git ls-tree)")
        return target

    def extract_nonhlos(self, rel: str, dest: Path):
        run(["mcopy", "-o", "-i", str(self.nonhlos_image()), f"::/{rel}", str(dest)])

    def extract_dspso(self, rel: str, dest: Path):
        run(["debugfs", "-R", f'dump /{rel} {dest}', str(self.dspso_image())])
        if not dest.is_file() or dest.stat().st_size == 0:
            raise SystemExit(f"dspso.bin has no {rel}")


# --- pinned deterministic transforms ---------------------------------------

def transform_elf_payload(data: bytes) -> bytes:
    """The single PT_LOAD payload of a one-segment ELF (historical BDF form)."""
    assert data[:4] == b"\x7fELF" and data[4] == 1
    e_phoff, = struct.unpack_from("<I", data, 0x1c)
    e_phentsize, e_phnum = struct.unpack_from("<HH", data, 0x2a)
    assert e_phnum == 1, e_phnum
    p_type, p_offset, _, _, p_filesz = struct.unpack_from("<IIIII", data, e_phoff)
    assert p_type == 1
    return data[p_offset:p_offset + p_filesz]


def transform_board2(name: str, bdf: bytes, driver: str = "ath12k") -> bytes:
    """A one-entry board-2 container; mirrors make-ath12k-board2.py.

    ath11k and ath12k share the container layout and differ only in the magic;
    both ship their board data ELF-wrapped (every WCN6855 entry in linux-firmware
    is), and both detect that wrapper before telling the firmware the BDF type.
    """
    if driver not in ("ath11k", "ath12k"):
        raise SystemExit(f"board2: unknown driver {driver!r}")
    if not bdf.startswith(b"\x7fELF"):
        raise SystemExit("BDF must retain its ELF wrapper")
    magic = f"QCA-{driver.upper()}-BOARD".encode("ascii") + b"\0"
    def align4(b): return b + bytes((-len(b)) & 3)
    def ie(i, payload): return struct.pack("<II", i, len(payload)) + align4(payload)
    group = ie(0, name.encode("ascii")) + ie(1, bdf)
    return align4(magic) + ie(0, group)


def transform_qrd_extract(blob: bytes) -> bytes:
    """The QRD BDF entry out of the official board-2.bin container."""
    assert blob.startswith(BOARD2_MAGIC[:16])
    off = (len(BOARD2_MAGIC) + 3) & ~3
    while off + 8 <= len(blob):
        ie_id, ie_len = struct.unpack_from("<II", blob, off)
        payload = blob[off + 8:off + 8 + ie_len]
        if ie_id == 0:
            sub, name = 0, None
            while sub + 8 <= len(payload):
                sid, slen = struct.unpack_from("<II", payload, sub)
                sdata = payload[sub + 8:sub + 8 + slen]
                if sid == 0:
                    name = sdata.split(b"\0")[0].decode()
                elif sid == 1 and name == QRD_BOARD_NAME:
                    return sdata
                sub += 8 + ((slen + 3) & ~3)
        off += 8 + ((ie_len + 3) & ~3)
    raise SystemExit("QRD entry not found in official board-2.bin")


def transform_tplg_sd1(data: bytes) -> bytes:
    """Retarget the I2S sink to SD1; exactly one token changes (X910 MI2S)."""
    buf = bytearray(data)
    needle = struct.pack("<I", 0x0700100A)  # MODULE_ID_I2S_SINK
    module = buf.find(needle)
    if module < 0 or buf.find(needle, module + 1) >= 0:
        raise SystemExit("expected exactly one I2S sink module in the topology")
    patched = 0
    for off in range(module, min(module + 200, len(buf) - 8), 4):
        token, value = struct.unpack_from("<II", buf, off)
        if token == 256 and value == 1:  # AR_TKN_U32_MODULE_SD_LINE_IDX -> I2S_SD1
            struct.pack_into("<I", buf, off + 4, 2)
            patched += 1
    if patched != 1:
        raise SystemExit(f"expected one sd_line token, patched {patched}")
    return bytes(buf)


def generate_sensor_hexagonfs(entry, sources: Sources, out_path: Path,
                              sscregistrygen: Path, record: bool):
    """The deterministic HexagonFS tree; mirrors stage-stock-sensor-hexagonfs.sh."""
    vendor = sources.vendor_tree()
    config = vendor / "etc/sensors/config"
    if not config.is_dir() or not (vendor / "etc/sensors/sns_reg_config").is_file():
        raise SystemExit(f"stock sensor configuration missing under {vendor}")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "sensor-hexagonfs"
        for sub in ("dsp/adsp", "sensors/config", "sensors/registry", "socinfo"):
            (root / sub).mkdir(parents=True)
        for f in sorted(config.iterdir()):
            shutil.copy2(f, root / "sensors/config" / f.name)
        run([str(sscregistrygen), "-p", entry.get("platform", "MTP"),
             "-s", str(entry.get("soc_id", 519)),
             str(root / "sensors/config"), str(root / "sensors/registry")])
        (root / "sensors/registry/sensors_registry").touch()
        # The DSP's completion cache; input mtimes are normalized to zero by
        # the deterministic tar below, so the cached values must be zero too.
        entries = {f"/vendor/etc/sensors/config/{p.name}":
                   {"type": "int", "ver": "0", "data": "0"}
                   for p in sorted((root / "sensors/config").glob("*.json"))}
        (root / "sensors/registry/sns_reg_config").write_text(
            json.dumps({"sns_reg_config": {"owner": "NA", **entries}},
                       separators=(",", ":")))
        shutil.copy2(vendor / "etc/sensors/sns_reg_config",
                     root / "sensors/sns_reg.conf")
        (root / "sensors/sns_reg_version").write_bytes(b"version=6\0")
        for name, value in (("hw_platform", entry.get("platform", "MTP")),
                            ("soc_id", str(entry.get("soc_id", 519))),
                            ("platform_subtype", "0"), ("platform_subtype_id", "0"),
                            ("platform_version", "0")):
            (root / "socinfo" / name).write_text(value + "\n")
        skels = dict(entry.get("skels", {}))
        if not skels:
            raise SystemExit("generated:sensor-hexagonfs needs a skels table")
        for name in sorted(skels):
            dest = root / "dsp/adsp" / name
            sources.extract_dspso(f"adsp/{name}", dest)
            actual = sha256_file(dest)
            if record:
                skels[name] = actual
            elif actual != skels[name]:
                raise SystemExit(f"DSP skel mismatch: {name} is {actual}")
        # --mode makes the archive independent of the build host's umask and
        # of whatever modes debugfs/fsck.erofs gave the extracted inputs:
        # data files become 0644 and directories 0755, always.
        run(["tar", "--sort=name", "--mtime=UTC 1970-01-01", "--owner=0",
             "--group=0", "--numeric-owner", "--mode=u=rwX,go=rX",
             "-C", tmp, "-czf", str(out_path), "sensor-hexagonfs"])
    return skels


# --- staging ----------------------------------------------------------------

def stage_entry(name, entry, sources, out, staged, args, record):
    """Materialize one manifest entry into out/name; returns measured extras."""
    dest = out / name
    kind, _, rest = entry["source"].partition(":")
    if kind == "vendor" or kind.startswith("vendor@"):
        build_id = kind.partition("@")[2] or None
        shutil.copy2(sources.vendor_tree(build_id) / rest, dest)
    elif kind == "nonhlos":
        sources.extract_nonhlos(rest, dest)
    elif kind == "dspso":
        sources.extract_dspso(rest, dest)
    elif kind == "url":
        shutil.copy2(sources.fetch_url(entry["source"][4:]), dest)
    elif kind == "lfw":
        commit, _, rel = rest.partition(":")
        shutil.copy2(sources.lfw_file(commit, rel), dest)
    elif kind == "derived":
        transform, _, arg = rest.partition(":")
        def staged_bytes(n):
            if n not in staged:
                raise SystemExit(f"{name}: derived input {n} is not staged yet")
            return (out / n).read_bytes()
        if transform == "elf-payload":
            dest.write_bytes(transform_elf_payload(staged_bytes(arg)))
        elif transform == "board2":
            dest.write_bytes(transform_board2(entry["board_name"], staged_bytes(arg),
                                              entry.get("driver", "ath12k")))
        elif transform == "qrd-extract":
            dest.write_bytes(transform_qrd_extract(staged_bytes(arg)))
        elif transform == "tplg-sd1":
            dest.write_bytes(transform_tplg_sd1(staged_bytes(arg)))
        else:
            raise SystemExit(f"{name}: unknown transform {transform}")
    elif kind == "generated":
        if rest != "sensor-hexagonfs":
            raise SystemExit(f"{name}: unknown generator {rest}")
        if args.sscregistrygen is None:
            raise SystemExit(f"{name} needs --sscregistrygen (or drop the entry)")
        return {"skels": generate_sensor_hexagonfs(
            entry, sources, dest, args.sscregistrygen, record)}
    else:
        raise SystemExit(f"{name}: unknown source kind {kind}")
    dest.chmod(0o644)
    return {}


def order(files):
    """Plain sources first, then derived, then generated: inputs before uses."""
    rank = {"derived": 1, "generated": 2}
    return sorted(files, key=lambda n: (rank.get(files[n]["source"].split(":")[0], 0), n))


def process(manifest, args, record=False):
    files = manifest["files"]
    # A missing generator is not a missing source.  `record` used to drop the
    # sensor registry as "not in this package" when --sscregistrygen was left
    # out, and the first X710 manifest shipped without it; refuse up front.
    generated = [n for n in files
                 if files[n]["source"].startswith("generated:")
                 and (not args.groups or files[n].get("group", "core") in args.groups)]
    if generated and args.sscregistrygen is None:
        raise SystemExit(f"{', '.join(generated)} needs --sscregistrygen")
    sources = Sources(args)
    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    staged, failures = {}, []
    for name in order(files):
        entry = dict(files[name])
        if args.groups and entry.get("group", "core") not in args.groups:
            continue
        try:
            extras = stage_entry(name, entry, sources, out, staged, args, record)
        except (SystemExit, subprocess.CalledProcessError) as error:
            # Segment families differ between revisions (adsp.b26 exists in one
            # package, adsp.b51 in another). Recording a new package drops the
            # template entries its sources cannot satisfy; verification never
            # does.
            if record:
                print(f"dropped {name}: not in this package", file=sys.stderr)
                (out / name).unlink(missing_ok=True)
            else:
                failures.append(f"{name}: {error}")
            continue
        actual = sha256_file(out / name)
        if record and entry.get("class") not in PINNED_CLASSES:
            entry["sha256"] = actual
        if entry.get("sha256") and actual != entry["sha256"]:
            failures.append(f"{name}: SHA-256 {actual}, manifest pins {entry['sha256']}")
            continue
        entry["sha256"] = actual
        entry.update(extras)
        staged[name] = entry
    if failures:
        for line in failures:
            print("FAIL " + line, file=sys.stderr)
        raise SystemExit(f"{len(failures)} entr{'y' if len(failures) == 1 else 'ies'} failed")
    return staged


def write_provenance(manifest, manifest_path, staged, out):
    groups = {}
    for entry in staged.values():
        group = entry.get("group", "core")
        groups[group] = groups.get(group, 0) + 1
    provenance = {
        "tool": TOOL,
        "manifest": {"name": manifest_path.name,
                     "sha256": sha256_file(manifest_path)},
        "package": manifest["package"],
        "staged": len(staged),
        "groups": groups,
        "files_sha256": {n: staged[n]["sha256"] for n in sorted(staged)},
        "date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    (out / "PROVENANCE.json").write_text(json.dumps(provenance, indent=2) + "\n")
    return provenance


def toml_str(value):
    return json.dumps(value)  # JSON string quoting is valid TOML for our values


def write_manifest(path, package, staged):
    lines = ["# Written by scripts/stage-firmware.py record; review before commit.",
             "# Format: configs/firmware/README.md", "", "[package]"]
    for key in ("device", "model", "bl_rev", "build_id", "odin_zip"):
        value = package[key]
        lines.append(f"{key} = {value if isinstance(value, int) else toml_str(value)}")
    lines += ["", "[files]"]
    for name in sorted(staged):
        entry = staged[name]
        parts = [f"source = {toml_str(entry['source'])}",
                 f"sha256 = {toml_str(entry['sha256'])}",
                 f"class = {toml_str(entry.get('class', 'loose'))}"]
        if entry.get("group", "core") != "core":
            parts.append(f"group = {toml_str(entry['group'])}")
        for key in ("board_name", "driver", "platform", "soc_id"):
            if key in entry:
                value = entry[key]
                parts.append(f"{key} = {value if isinstance(value, int) else toml_str(value)}")
        if "skels" in entry:
            skels = ", ".join(f"{toml_str(k)} = {toml_str(v)}"
                              for k, v in sorted(entry["skels"].items()))
            parts.append(f"skels = {{ {skels} }}")
        lines.append(f"{toml_str(name)} = {{ {', '.join(parts)} }}")
    path.write_text("\n".join(lines) + "\n")


def add_source_args(parser):
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--cache", type=Path,
                        default=Path.home() / ".cache/gts9u-stage-firmware")
    parser.add_argument("--odin", type=Path)
    parser.add_argument("--nonhlos", type=Path)
    parser.add_argument("--dspso", type=Path)
    parser.add_argument("--vendor-root", type=Path)
    parser.add_argument("--vendor-image", type=Path)
    parser.add_argument("--aux-vendor", action="append", default=[],
                        metavar="BUILDID=DIR")
    parser.add_argument("--sscregistrygen", type=Path)
    parser.add_argument("--groups", type=lambda s: set(s.split(",")),
                        help="stage only these groups (default: all)")
    parser.add_argument("--offline", action="store_true")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    stage = commands.add_parser("stage", help="stage and verify a manifest")
    stage.add_argument("--manifest", type=Path, required=True)
    add_source_args(stage)
    record = commands.add_parser("record",
                                 help="measure a new package against a template")
    record.add_argument("--like", type=Path, required=True)
    record.add_argument("--build-id", required=True)
    record.add_argument("--odin-zip-name", default="")
    record.add_argument("--write", type=Path, required=True)
    add_source_args(record)
    args = parser.parse_args()

    if args.command == "stage":
        manifest = tomllib.loads(args.manifest.read_text())
        staged = process(manifest, args, record=False)
        provenance = write_provenance(manifest, args.manifest, staged, args.out)
        print(f"staged {provenance['staged']} files for "
              f"{provenance['package']['build_id']} (bl{provenance['package']['bl_rev']}) "
              f"into {args.out}")
    else:
        template = tomllib.loads(args.like.read_text())
        rev = args.build_id[7:8]
        if not rev.isdigit():
            raise SystemExit(f"cannot read a bootloader revision from {args.build_id}")
        package = dict(template["package"])
        package.update(build_id=args.build_id, bl_rev=int(rev),
                       odin_zip=args.odin_zip_name or package.get("odin_zip", ""))
        manifest = {"package": package, "files": template["files"]}
        staged = process(manifest, args, record=True)
        write_manifest(args.write, package, staged)
        write_provenance(manifest, args.write, staged, args.out)
        print(f"recorded {len(staged)} files into {args.write}; "
              "review the diff against the template before committing")


if __name__ == "__main__":
    main()
