#!/usr/bin/python3 -IS
"""gts9u stall-watch: a 1 s system sampler that records desktop freezes.

    stall-watch run                  the service (root, started by systemd)
    stall-watch status               is it alive, what it costs, what the ring spans
    stall-watch list                 recorded incidents, one line each
    stall-watch report [ID|latest]   verdict for one incident, then the evidence
    stall-watch at TIME [SECONDS]    the same verdict for any moment the ring covers
    stall-watch mark                 save the last two minutes now (after a freeze
                                     the detectors missed); group adm is enough

Everything it reads is optional: a file that does not exist on this kernel or
board is skipped.  It never writes to /proc or /sys except to arm PSI triggers,
never opens camera or input devices and never runs anything in its sampling
loop.  See README.md for the design and the thresholds.
"""
import collections
import errno
import gzip
import io
import json
import os
import re
import select
import signal
import stat
import sys
import threading
import time
import zlib

VERSION = "1"
FORMAT = 1

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

DEFAULTS = {
    "interval_ms": 1000,        # sampling period
    "proc_scan_every": 5,       # process scan every N samples (every sample while escalated)
    "pre_s": 120,               # seconds of ring kept with an incident, before it
    "post_s": 30,               # quiet seconds that close an incident
    "mark_post_s": 5,           # ... for a manual mark
    "max_incident_s": 600,      # an incident longer than this is split
    "escalate_s": 10,           # detailed sampling after any trigger
    "ring_flush_s": 60,         # disk write cadence (10 s while an incident is open)
    "ring_segment_s": 600,      # one ring file per 10 minutes
    "ring_max_mb": 64,          # disk cap for the ring
    "incidents_max_mb": 64,     # disk cap for incident records
    "incidents_max_count": 200,
    "incidents_per_hour": 20,   # beyond this only a summary is kept
    "min_free_mb": 512,         # stop writing below this much free space
    "late_ms": 200,             # sampler wake-up lateness that counts as a stall
    "late_incident_ms": 500,    # ... and that makes an incident on its own
    "mem_full_ms": 100,         # PSI memory full per second that counts as stalled
    "mem_incident_ms": 500,     # memory full over an episode that makes an incident
    "io_full_ms": 200,
    "io_incident_ms": 1000,
    "cpu_some_ms": 950,         # PSI cpu some per second ...
    "cpu_sustain_s": 5,         # ... for this many seconds in a row
    "desktop_ms": 100,          # compositor cgroup memory+io stall per second
    "desktop_incident_ms": 300,
    "desktop_wait_ms": 300,     # compositor main-thread run-queue wait per second
    "display_input_irqs": 30,   # input interrupts/s that mean someone is using it
    "display_active_irqs": 30,  # display interrupts/s that mean frames were flowing
    "psi_trigger_memory": "some 150000 1000000",
    "psi_trigger_io": "full 150000 1000000",
    "psi_trigger_cpu": "",
    "compositors": "gnome-shell kwin_wayland sway weston mutter",
    "display_irq": "msm-kms dsi_isr mdss dpu",
    "input_irq": "goodix xhci qcom_geni_serial_uart1 spen i2c_hid hid",
    "lmh_irq": "dcvsh",
    # extra cgroups whose memory pressure is sampled, name=path under /sys/fs/cgroup;
    # the BT mouse reaches the kernel through bluetoothd (uhid), so a stalled
    # bluetoothd stops the pointer even when the compositor runs
    "watch_cgroups": "bt=system.slice/bluetooth.service",
    "journal_markers": 1,       # one journal line per saved incident (rate-limited)
    "mlock": 1,
    "max_rss_mb": 128,          # the sampler exits (and is restarted) above this
}

DATA_DIR = "/var/log/gts9u-stall-watch"
RUN_DIR = "/run/gts9u-stall-watch"
CONF_FILE = "/etc/gts9u-stall-watch.conf"
SERVICE = "gts9u-stall-watch.service"

KMSG_CLASSES = [
    ("display", re.compile(r"frame done timeout|flip_done timed out|vblank wait timed out|"
                           r"commit.*timed out|pp done time ?out|underrun|wait for commit done", re.I)),
    ("gpu", re.compile(r"hangcheck|gpu lockup|recover_worker|gpu fault|GMU OOB|HFI_H2F|"
                       r"a6xx_gmu.*(timeout|Timeout)|a6xx_hfi|adreno.*error|\*ERROR\*.*gpu", re.I)),
    ("lockup", re.compile(r"blocked for more than|soft lockup|hard LOCKUP|rcu.*(stall|detected stalls)|"
                          r"workqueue lockup|BUG:|Oops|Unable to handle kernel|Call trace:", re.I)),
    ("memory", re.compile(r"Out of memory|oom-kill|Killed process|page allocation failure|"
                          r"invoked oom-killer|lowmem", re.I)),
    ("storage", re.compile(r"ufshcd|ufs_?qcom|I/O error|blk_update_request|Buffer I/O|"
                           r"EXT4-fs (error|warning)|task abort|DMA timed out|scsi.*(timeout|abort)", re.I)),
    ("bus", re.compile(r"I2C timeout|GPI transfer failed|CH STOP|geni.*timeout|spi.*timeout", re.I)),
    ("rpmh", re.compile(r"rpmh|RPMH|AMC|qcom_rpmh|interconnect|icc_|bwmon", re.I)),
]

VM_KEYS = {
    # short name: vmstat counters summed (deltas, pages)
    "allocstall": ("allocstall_dma", "allocstall_dma32", "allocstall_normal", "allocstall_movable"),
    "scan_d": ("pgscan_direct",),
    "steal_d": ("pgsteal_direct",),
    "scan_k": ("pgscan_kswapd",),
    "steal_k": ("pgsteal_kswapd",),
    "scan_anon": ("pgscan_anon",),
    "steal_anon": ("pgsteal_anon",),
    "scan_file": ("pgscan_file",),
    "steal_file": ("pgsteal_file",),
    "throttle": ("pgscan_direct_throttle",),
    "pswpin": ("pswpin",),
    "pswpout": ("pswpout",),
    "rf_file": ("workingset_refault_file",),
    "rf_anon": ("workingset_refault_anon",),
    "majflt": ("pgmajfault",),
    "cstall": ("compact_stall",),
    "cfail": ("compact_fail",),
    "csucc": ("compact_success",),
    "cscan": ("compact_migrate_scanned",),
    "migfail": ("pgmigrate_fail",),
    "kcompact": ("compact_daemon_wake",),
    "thp_alloc": ("thp_fault_alloc",),
    "thp_fb": ("thp_fault_fallback",),
    "thp_split": ("thp_split_page",),
    "oom": ("oom_kill",),
    "cull": ("unevictable_pgs_culled",),
    "resc": ("unevictable_pgs_rescued",),
    "slab_scan": ("slabs_scanned",),
    "lowhit": ("kswapd_low_wmark_hit_quickly",),
    "drop": ("drop_pagecache", "drop_slab"),
}
VM_NEEDED = sorted({c for v in VM_KEYS.values() for c in v})

MEM_KEYS = {
    "free": "MemFree", "avail": "MemAvailable", "cached": "Cached", "swapcache": "SwapCached",
    "afile": "Active(file)", "ifile": "Inactive(file)", "anon": "AnonPages", "shmem": "Shmem",
    "unevict": "Unevictable", "mlock": "Mlocked", "dirty": "Dirty", "wb": "Writeback",
    "slab_r": "SReclaimable", "slab_u": "SUnreclaim", "thp": "AnonHugePages",
    "swaptotal": "SwapTotal", "swapfree": "SwapFree", "pagetables": "PageTables",
    "committed": "Committed_AS", "total": "MemTotal",
}


def load_config(path=CONF_FILE, overrides=None):
    cfg = dict(DEFAULTS)
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.split("#", 1)[0].strip()
                if "=" not in line:
                    continue
                k, v = (x.strip() for x in line.split("=", 1))
                if k not in DEFAULTS:
                    continue
                if isinstance(DEFAULTS[k], int):
                    try:
                        cfg[k] = int(v)
                    except ValueError:
                        pass
                else:
                    cfg[k] = v.strip('"').strip("'")
    except OSError:
        pass
    if overrides:
        cfg.update(overrides)
    cfg["interval_ms"] = max(100, min(10000, int(cfg["interval_ms"])))
    return cfg


# --------------------------------------------------------------------------
# Reading files: every reader returns None when the file is not there
# --------------------------------------------------------------------------

class Files:
    """Reads files under a root prefix (tests use a fake /proc and /sys).
    Frequently read files are kept open and re-read with pread at offset 0,
    which regenerates proc and sysfs files without a path lookup."""

    def __init__(self, root="/"):
        self.root = root.rstrip("/")
        self.fds = {}
        self.slow = {}           # path -> ms of a read that took too long (skipped from then on)

    def path(self, p):
        return self.root + p if self.root else p

    def read(self, p, keep=False, size=65536):
        fd = self.fds.get(p)
        if fd is None:
            try:
                fd = os.open(self.path(p), os.O_RDONLY | os.O_CLOEXEC)
            except OSError:
                return None
            if keep:
                self.fds[p] = fd
        try:
            chunks = []
            off = 0
            while True:
                b = os.pread(fd, size, off)
                chunks.append(b)
                if len(b) < size:
                    break
                off += len(b)
                if off > 8 * 1024 * 1024:
                    break
            data = b"".join(chunks)
        except OSError:
            data = None
            if keep:
                self.close_one(p)
        finally:
            if not keep:
                try:
                    os.close(fd)
                except OSError:
                    pass
        return data

    def close_one(self, p):
        fd = self.fds.pop(p, None)
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

    def close(self):
        for p in list(self.fds):
            self.close_one(p)

    def read_guarded(self, p, limit_ms=50, keep=True):
        """For driver-backed sysfs files: a read slower than limit_ms is never repeated
        in the sampling loop (a driver lock must not hold the sampler up twice)."""
        if p in self.slow:
            return None
        t = time.monotonic()
        d = self.read(p, keep=keep)
        ms = (time.monotonic() - t) * 1000
        if ms > limit_ms:
            self.slow[p] = round(ms)
            self.close_one(p)
        return d

    def listdir(self, p):
        try:
            return os.listdir(self.path(p))
        except OSError:
            return []

    def exists(self, p):
        return os.path.exists(self.path(p))

    def readlink(self, p):
        try:
            return os.readlink(self.path(p))
        except OSError:
            return None


def to_int(b, default=0):
    try:
        return int(b)
    except (TypeError, ValueError):
        return default


def parse_psi(data):
    """'some avg10=.. total=N' / 'full ...' -> {'some': N, 'full': N} (microseconds)."""
    out = {}
    if not data:
        return out
    for line in data.split(b"\n"):
        if not line:
            continue
        kind, _, rest = line.partition(b" ")
        i = rest.rfind(b"total=")
        if i >= 0:
            out[kind.decode()] = to_int(rest[i + 6:].split()[0])
    return out


def parse_kv(data, wanted=None):
    """'Key:   123 kB' or 'key 123' lines -> {key: int}."""
    out = {}
    if not data:
        return out
    for line in data.split(b"\n"):
        parts = line.split()
        if len(parts) < 2:
            continue
        k = parts[0].rstrip(b":").decode("ascii", "replace")
        if wanted is not None and k not in wanted:
            continue
        out[k] = to_int(parts[1])
    return out


def parse_proc_stat(data):
    out = {"cpus": {}}
    if not data:
        return out
    for line in data.split(b"\n"):
        if line.startswith(b"cpu"):
            parts = line.split()
            vals = [to_int(x) for x in parts[1:]]
            vals += [0] * (10 - len(vals))
            idle = vals[3] + vals[4]
            busy = vals[0] + vals[1] + vals[2] + vals[5] + vals[6] + vals[7]
            name = parts[0].decode()
            out["cpus"][name] = (busy, idle, vals[4], vals[5] + vals[6])
        elif line.startswith(b"ctxt "):
            out["ctxt"] = to_int(line.split()[1])
        elif line.startswith(b"procs_running "):
            out["running"] = to_int(line.split()[1])
        elif line.startswith(b"procs_blocked "):
            out["blocked"] = to_int(line.split()[1])
    return out


def parse_diskstats(data, wanted=None):
    out = {}
    if not data:
        return out
    for line in data.split(b"\n"):
        p = line.split()
        if len(p) < 14:
            continue
        name = p[2].decode()
        if wanted is not None and name not in wanted:
            continue
        v = [to_int(x) for x in p[3:14]]
        # rd_ios rd_sectors rd_ms wr_ios wr_sectors wr_ms inflight io_ticks
        out[name] = (v[0], v[2], v[3], v[4], v[6], v[7], v[8], v[9])
    return out


_TRIGGER_TYPES = (b"Level", b"Edge", b"level", b"edge", b"LEVEL", b"EDGE")


def parse_interrupts(data):
    """-> {label: (total, name)}; name is the device part of the line."""
    out = {}
    if not data:
        return out
    lines = data.split(b"\n")
    if not lines:
        return out
    ncpu = len(lines[0].split())
    for line in lines[1:]:
        label, sep, rest = line.partition(b":")
        if not sep:
            continue
        label = label.strip().decode("ascii", "replace")
        toks = rest.split()
        counts = []
        for t in toks[:ncpu]:
            if t.isdigit():
                counts.append(int(t))
            else:
                break
        tail = toks[len(counts):]
        name = b" ".join(tail)
        for i, t in enumerate(tail):
            if t in _TRIGGER_TYPES:
                name = b" ".join(tail[i + 1:])
                break
        out[label] = (sum(counts), name.decode("ascii", "replace"))
    return out


def parse_pid_stat(data):
    """-> (comm, state, utime+stime ticks, rss pages, nthreads, policy, rtprio) or None."""
    if not data:
        return None
    l = data.find(b"(")
    r = data.rfind(b")")
    if l < 0 or r < 0:
        return None
    comm = data[l + 1:r].decode("utf-8", "replace")
    f = data[r + 2:].split()
    if len(f) < 22:
        return None
    pol = to_int(f[38]) if len(f) > 38 else 0
    rtp = to_int(f[37]) if len(f) > 37 else 0
    return (comm, f[0].decode(), to_int(f[11]) + to_int(f[12]), to_int(f[21]), to_int(f[17]), pol, rtp)


def parse_swaps(data):
    out = {}
    if not data:
        return out
    for line in data.split(b"\n")[1:]:
        p = line.split()
        if len(p) >= 4:
            out[os.path.basename(p[0].decode())] = to_int(p[3]) // 1024  # MB used
    return out


def parse_mm_stat(data):
    if not data:
        return None
    v = [to_int(x) for x in data.split()]
    if len(v) < 3:
        return None
    return (v[0] >> 20, v[1] >> 20, v[2] >> 20)  # orig, compressed, used (MB)


def parse_fdinfo_drm(data):
    out = {}
    if not data:
        return out
    for line in data.split(b"\n"):
        if line.startswith(b"drm-"):
            k, _, v = line.partition(b":")
            out[k.decode()] = v.strip().split()[0].decode() if v.strip() else ""
    return out


def parse_kmsg_record(rec):
    """'prio,seq,usec,flags[,...];text\\n continuation' -> (level, usec, text) or None."""
    try:
        head, _, body = rec.partition(b";")
        h = head.split(b",")
        level = int(h[0]) & 7
        usec = int(h[2])
        text = body.split(b"\n", 1)[0].decode("utf-8", "replace")
        return level, usec, text
    except (ValueError, IndexError):
        return None


def classify_kmsg(text):
    for name, rx in KMSG_CLASSES:
        if rx.search(text):
            return name
    return None


def delta(new, old):
    """Counter delta; a counter that went backwards (reset) counts from zero."""
    if old is None or new is None:
        return 0
    d = new - old
    return d if d >= 0 else new


# --------------------------------------------------------------------------
# The sampler
# --------------------------------------------------------------------------

class Sampler:
    def __init__(self, cfg, files, clock=None):
        self.cfg = cfg
        self.f = files
        self.clock = clock or Clock()
        self.page_kb = os.sysconf("SC_PAGE_SIZE") // 1024 if hasattr(os, "sysconf") else 4
        self.hz = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
        self.prev = {}
        self.prev_proc = {}
        self.prev_proc_t = None
        self.nsample = 0
        self.last_discover = -1e9
        self.discovered = False
        self.pid_self = os.getpid()
        self.comp = None          # compositor info
        self.last_scan = None     # latest process scan result
        self.display_hist = collections.deque(maxlen=6)
        self.cpu_sustain = 0
        self.gs_d_run = 0
        self.desktop_prev = None

    # ----- discovery (every 60 s and at start) ---------------------------
    def discover(self, now):
        f = self.f
        self.last_discover = now
        self.policies = []
        base = "/sys/devices/system/cpu/cpufreq"
        for p in sorted(f.listdir(base)):
            if p.startswith("policy"):
                hw = to_int((f.read(base + "/" + p + "/cpuinfo_max_freq") or b"0").strip())
                self.policies.append((p, base + "/" + p, hw // 1000))
        self.zones = []
        for z in sorted(f.listdir("/sys/class/thermal"), key=lambda s: (len(s), s)):
            if z.startswith("thermal_zone"):
                t = f.read("/sys/class/thermal/" + z + "/type")
                if t is not None:
                    self.zones.append((t.strip().decode("ascii", "replace"), "/sys/class/thermal/" + z + "/temp"))
            if len(self.zones) > 64:
                break
        # Only the kernel's own cpufreq/devfreq cooling devices: their state is a
        # variable.  A driver's cooling device (Wi-Fi, charger) answers under the
        # driver's lock, which a busy driver can hold for seconds.
        self.cooling = []
        for c in sorted(f.listdir("/sys/class/thermal"), key=lambda s: (len(s), s)):
            if c.startswith("cooling_device"):
                t = f.read("/sys/class/thermal/" + c + "/type")
                if t is not None:
                    name = t.strip().decode("ascii", "replace")
                    if name.startswith(("cpufreq-", "devfreq-", "thermal-cpufreq", "thermal-devfreq")):
                        self.cooling.append((name, "/sys/class/thermal/" + c + "/cur_state"))
        # target_freq is devfreq's own record of the last frequency it set;
        # cur_freq may ask the driver.
        self.devfreq = []
        for d in sorted(f.listdir("/sys/class/devfreq")):
            path = "/sys/class/devfreq/" + d + "/target_freq"
            if not f.exists(path):
                path = "/sys/class/devfreq/" + d + "/cur_freq"
            self.devfreq.append((d, path))
        self.disks = set()
        for d in f.listdir("/sys/block"):
            if d.startswith(("loop", "ram", "dm-")):
                continue
            self.disks.add(d)
        self.zrams = sorted(d for d in self.disks if d.startswith("zram"))
        self.connectors = []
        for c in sorted(f.listdir("/sys/class/drm")):
            if "-" in c and c.startswith("card"):
                self.connectors.append("/sys/class/drm/" + c)
        self.cg = {}
        cgroot = "/sys/fs/cgroup"
        if f.exists(cgroot + "/user.slice/memory.pressure"):
            self.cg["user"] = cgroot + "/user.slice"
        if f.exists(cgroot + "/system.slice/memory.pressure"):
            self.cg["system"] = cgroot + "/system.slice"
        for item in self.cfg.get("watch_cgroups", "").split():
            name, _, rel = item.partition("=")
            if name and rel and f.exists(cgroot + "/" + rel.strip("/") + "/memory.pressure"):
                self.cg[name] = cgroot + "/" + rel.strip("/")
        self.discover_compositor()
        self.discovered = True

    def discover_compositor(self):
        """The desktop's compositor: its cgroup, main thread, KMS thread, DRM fd."""
        f = self.f
        names = self.cfg["compositors"].split()
        best = None
        for pid in f.listdir("/proc"):
            if not pid.isdigit():
                continue
            st = parse_pid_stat(f.read("/proc/" + pid + "/stat"))
            if not st or st[0] not in names:
                continue
            try:
                uid = os.stat(f.path("/proc/" + pid)).st_uid
            except OSError:
                continue
            key = (uid >= 1000, st[3])
            if best is None or key > best[0]:
                best = (key, int(pid), st[0], uid)
        if best is None:
            self.comp = None
            return
        pid = best[1]
        comp = {"pid": pid, "comm": best[2], "uid": best[3], "kms": None, "cg": None, "drmfd": None,
                "app": None}
        for tid in f.listdir("/proc/%d/task" % pid):
            c = f.read("/proc/%d/task/%s/comm" % (pid, tid))
            if c and c.strip() == b"KMS thread":
                comp["kms"] = int(tid)
        cgl = f.read("/proc/%d/cgroup" % pid)
        if cgl:
            for line in cgl.split(b"\n"):
                if line.startswith(b"0::"):
                    path = "/sys/fs/cgroup" + line[3:].decode().strip()
                    if f.exists(path + "/memory.pressure"):
                        comp["cg"] = path
                        if "/session.slice/" in path:
                            app = path.split("/session.slice/")[0] + "/app.slice"
                            if f.exists(app + "/memory.pressure"):
                                comp["app"] = app
        for fd in f.listdir("/proc/%d/fd" % pid):
            target = f.readlink("/proc/%d/fd/%s" % (pid, fd)) or ""
            if target.startswith("/dev/dri/renderD"):
                info = parse_fdinfo_drm(f.read("/proc/%d/fdinfo/%s" % (pid, fd)))
                if "drm-engine-gpu" in info:
                    comp["drmfd"] = int(fd)
                    break
        old = self.comp
        if old and (old.get("pid") != pid or old.get("cg") != comp.get("cg")):
            for k in [k for k in self.prev if k.startswith("comp.") or k == "cg.app"]:
                del self.prev[k]
            for p in [p for p in f.fds if (old.get("cg") and p.startswith(old["cg"] + "/")) or
                      (old.get("app") and p.startswith(old["app"] + "/"))]:
                f.close_one(p)
        self.comp = comp

    # ----- one sample ------------------------------------------------------
    def sample(self, mono, boot, wall, late_ms, kind, escalated):
        f = self.f
        cfg = self.cfg
        t0 = self.clock.thread_time()
        if not self.discovered or mono - self.last_discover >= 60:
            self.discover(mono)
        prev = self.prev
        s = {"v": FORMAT, "t": round(wall, 3), "mono": round(mono, 3), "kind": kind}
        pm = prev.get("mono")
        dt = (mono - pm) if pm is not None else cfg["interval_ms"] / 1000.0
        dt = max(dt, 0.001)
        s["dt"] = round(dt * 1000)
        s["late"] = round(late_ms, 1)
        pb = prev.get("boot")
        susp = ((boot - pb) - (mono - pm)) * 1000 if pb is not None and pm is not None else 0
        s["susp"] = round(max(0.0, susp))
        prev["mono"], prev["boot"] = mono, boot

        # PSI, system wide
        psi = {}
        for res in ("cpu", "memory", "io", "irq"):
            v = parse_psi(f.read("/proc/pressure/" + res, keep=True))
            if not v:
                continue
            o = prev.get("psi." + res, {})
            psi[res] = [round(delta(v.get("some"), o.get("some")) / 1000, 1),
                        round(delta(v.get("full"), o.get("full")) / 1000, 1)]
            prev["psi." + res] = v
        s["psi"] = psi

        # cgroup memory PSI: user.slice, system.slice, the compositor and its app.slice
        cg = {}
        targets = dict(self.cg)
        if self.comp and self.comp.get("app"):
            targets["app"] = self.comp["app"]
        for name, path in targets.items():
            v = parse_psi(f.read(path + "/memory.pressure", keep=True))
            if v:
                o = prev.get("cg." + name, {})
                cg[name] = [round(delta(v.get("some"), o.get("some")) / 1000, 1),
                            round(delta(v.get("full"), o.get("full")) / 1000, 1)]
                prev["cg." + name] = v
        s["cg"] = cg

        # compositor: its own cgroup PSI, main and KMS thread state and run-queue wait
        comp = self.comp
        if comp and comp.get("pid"):
            if not f.exists("/proc/%d" % comp["pid"]):
                self.discover_compositor()
                comp = self.comp
        if comp and comp.get("pid"):
            d = {"pid": comp["pid"]}
            if comp.get("cg"):
                for res in ("memory", "io", "cpu"):
                    v = parse_psi(f.read(comp["cg"] + "/" + res + ".pressure", keep=True))
                    if v:
                        o = prev.get("comp." + res, {})
                        d[res[:3]] = [round(delta(v.get("some"), o.get("some")) / 1000, 1),
                                      round(delta(v.get("full"), o.get("full")) / 1000, 1)]
                        prev["comp." + res] = v
            for label, tid in (("main", comp["pid"]), ("kms", comp.get("kms"))):
                if not tid:
                    continue
                base = "/proc/%d/task/%d/" % (comp["pid"], tid)
                st = parse_pid_stat(f.read(base + "stat"))
                ss = f.read(base + "schedstat")
                if st is None:
                    continue
                run = wait = None
                if ss:
                    p = ss.split()
                    if len(p) >= 2:
                        run, wait = to_int(p[0]), to_int(p[1])
                o = prev.get("comp." + label)
                if o and run is not None:
                    d[label] = [st[1], round(delta(run, o[0]) / 1e6, 1), round(delta(wait, o[1]) / 1e6, 1)]
                else:
                    d[label] = [st[1], None, None]
                if run is not None:
                    prev["comp." + label] = (run, wait)
            if comp.get("drmfd") is not None:
                info = parse_fdinfo_drm(f.read("/proc/%d/fdinfo/%d" % (comp["pid"], comp["drmfd"])))
                ns = to_int(info.get("drm-engine-gpu"), None) if info else None
                if ns is not None:
                    o = prev.get("comp.gpu")
                    if o is not None:
                        d["gpu_ms"] = round(delta(ns, o) / 1e6, 1)
                    prev["comp.gpu"] = ns
                    res = to_int(info.get("drm-resident-memory"), None)
                    if res is not None:
                        d["gpu_mb"] = res // 1024
            s["comp"] = d

        # memory
        mi = parse_kv(f.read("/proc/meminfo", keep=True), set(MEM_KEYS.values()))
        if mi:
            s["mem"] = {k: round(mi[v] / 1024, 1) for k, v in MEM_KEYS.items() if v in mi}
            if "swaptotal" in s["mem"] and "swapfree" in s["mem"]:
                s["mem"]["swapused"] = round(s["mem"]["swaptotal"] - s["mem"]["swapfree"], 1)
        vm = parse_kv(f.read("/proc/vmstat", keep=True), set(VM_NEEDED) | {"nr_free_pages", "nr_unevictable"})
        if vm:
            o = prev.get("vm")
            if o is not None:
                d = {k: sum(delta(vm.get(c), o.get(c)) for c in cs) for k, cs in VM_KEYS.items()}
                s["vm"] = {k: v for k, v in d.items() if v}
            prev["vm"] = vm
        sw = parse_swaps(f.read("/proc/swaps", keep=True))
        if sw:
            s["swap"] = sw
        z = {}
        for name in self.zrams:
            mm = parse_mm_stat(f.read("/sys/block/%s/mm_stat" % name, keep=True))
            if mm:
                z[name] = list(mm)
        if z:
            s["zram"] = z

        # cpu: per-cpu busy, iowait, irq time; frequency and caps per policy
        ps = parse_proc_stat(f.read("/proc/stat", keep=True))
        o = prev.get("stat")
        if ps.get("cpus") and o:
            busy = []
            for name in sorted((n for n in ps["cpus"] if n != "cpu"), key=lambda n: int(n[3:] or 0)):
                n, p_ = ps["cpus"][name], o["cpus"].get(name)
                if not p_:
                    continue
                db, di = delta(n[0], p_[0]), delta(n[1], p_[1])
                busy.append(round(100.0 * db / (db + di)) if db + di else 0)
            tot, pt = ps["cpus"].get("cpu"), o["cpus"].get("cpu")
            cpu = {"busy": busy}
            if tot and pt:
                all_ = delta(tot[0], pt[0]) + delta(tot[1], pt[1])
                if all_:
                    cpu["iow"] = round(100.0 * delta(tot[2], pt[2]) / all_, 1)
                    cpu["irq"] = round(100.0 * delta(tot[3], pt[3]) / all_, 1)
            s["cpu"] = cpu
            s["ctxt"] = delta(ps.get("ctxt"), o.get("ctxt"))
        s["run"], s["blk"] = ps.get("running"), ps.get("blocked")
        if ps.get("cpus"):
            prev["stat"] = ps
        freq, cap = [], []
        for name, path, hw in self.policies:
            cur = to_int((f.read(path + "/scaling_cur_freq", keep=True) or b"").strip(), None)
            mx = to_int((f.read(path + "/scaling_max_freq", keep=True) or b"").strip(), None)
            freq.append(cur // 1000 if cur is not None else None)
            cap.append(mx // 1000 if mx is not None and hw and mx // 1000 < hw else None)
        if freq:
            s.setdefault("cpu", {})["freq"] = freq
            if any(c is not None for c in cap):
                s["cpu"]["cap"] = cap
        la = f.read("/proc/loadavg", keep=True)
        if la:
            p = la.split()
            try:
                s["load"] = float(p[0])
            except (ValueError, IndexError):
                pass

        # thermal and cooling: sensor reads go through drivers, so every 5th
        # sample (every sample while escalated), each read time-guarded
        if escalated or self.nsample % 5 == 0 or "therm" not in prev:
            temps = []
            for name, path in self.zones:
                v = to_int((f.read_guarded(path) or b"").strip(), None)
                if v is not None and -40000 < v < 150000:
                    temps.append((v, name))
            temps.sort(reverse=True)
            prev["therm"] = [[n, round(v / 1000.0, 1)] for v, n in temps[:3]]
            cool = {}
            for name, path in self.cooling:
                v = to_int((f.read_guarded(path) or b"").strip(), None)
                if v:
                    cool[name] = v
            prev["cool"] = cool
        if prev.get("therm"):
            s["therm"] = prev["therm"]
        if prev.get("cool"):
            s["cool"] = prev["cool"]
        dv = {}
        for name, path in self.devfreq:
            v = to_int((f.read_guarded(path) or b"").strip(), None)
            if v is not None:
                dv[name] = v // 1000000
        if dv:
            s["devfreq"] = dv

        # block devices
        ds = parse_diskstats(f.read("/proc/diskstats", keep=True), self.disks)
        o = prev.get("disk")
        if ds and o:
            disk = {}
            for name, v in ds.items():
                p_ = o.get(name)
                if not p_:
                    continue
                d = [delta(v[0], p_[0]), delta(v[1], p_[1]) // 2, delta(v[2], p_[2]),
                     delta(v[3], p_[3]), delta(v[4], p_[4]) // 2, delta(v[5], p_[5]),
                     v[6], delta(v[7], p_[7])]
                if any(d[:6]) or d[6] or d[7]:
                    disk[name] = d  # rd_ios rd_kB rd_ms wr_ios wr_kB wr_ms inflight busy_ms
            if disk:
                s["disk"] = disk
        if ds:
            prev["disk"] = ds

        # interrupts: busiest lines, display, input, LMh
        it = parse_interrupts(f.read("/proc/interrupts", keep=True, size=131072))
        o = prev.get("irq")
        if it and o:
            deltas = []
            disp = inp = lmh = 0
            dpat = self.cfg["display_irq"].split()
            ipat = self.cfg["input_irq"].split()
            lpat = self.cfg["lmh_irq"].split()
            for label, (tot, name) in it.items():
                p_ = o.get(label)
                if p_ is None:
                    continue
                d = delta(tot, p_[0])
                if not d:
                    continue
                deltas.append((d, label, name))
                if any(x in name for x in dpat):
                    disp += d
                if any(x in name for x in ipat):
                    inp += d
                if any(x in name for x in lpat):
                    lmh += d
            deltas.sort(reverse=True)
            s["irq"] = [[l if not n else n[:28], d] for d, l, n in deltas[:6]]
            s["disp"], s["inp"] = disp, inp
            if lmh:
                s["lmh"] = lmh
        if it:
            prev["irq"] = it
        on = None
        for c in self.connectors:
            st = f.read(c + "/status")
            if st and st.strip() == b"connected":
                dp = f.read(c + "/dpms")
                if dp is not None:
                    on = bool(on) or dp.strip() == b"On"
        if on is not None:
            s["screen"] = 1 if on else 0
        # suspend attempts (successful or failed) freeze the sampler too
        ok = f.read("/sys/power/suspend_stats/success", keep=True)
        bad = f.read("/sys/power/suspend_stats/fail", keep=True)
        if ok is not None:
            v = to_int(ok.strip()) + to_int((bad or b"0").strip())
            if prev.get("suspends") is not None and v != prev["suspends"]:
                s["resumed"] = v - prev["suspends"]
            prev["suspends"] = v

        # processes
        self.nsample += 1
        if escalated or self.nsample % max(1, self.cfg["proc_scan_every"]) == 0 or self.last_scan is None:
            self.scan_processes(mono, s)

        # the sampler itself
        ss = f.read("/proc/self/schedstat")
        if ss:
            p = ss.split()
            if len(p) >= 2:
                run, wait = to_int(p[0]), to_int(p[1])
                o = prev.get("self")
                if o:
                    s["self"] = [round(delta(run, o[0]) / 1e6, 2), round(delta(wait, o[1]) / 1e6, 2)]
                prev["self"] = (run, wait)
        s["work"] = round((self.clock.thread_time() - t0) * 1000, 2)
        return s

    def scan_processes(self, mono, s):
        f = self.f
        cur = {}
        dstate = []
        for pid in f.listdir("/proc"):
            if not pid.isdigit():
                continue
            st = parse_pid_stat(f.read("/proc/" + pid + "/stat"))
            if not st:
                continue
            ipid = int(pid)
            cur[ipid] = (st[0], st[2], st[3])
            if st[1] == "D":
                dstate.append([ipid, st[0]])
        top_cpu = []
        if self.prev_proc_t is not None:
            span = max(mono - self.prev_proc_t, 0.001)
            for pid, (comm, ticks, rss) in cur.items():
                o = self.prev_proc.get(pid)
                if o and o[0] == comm:
                    d = ticks - o[1]
                    if d > 0:
                        top_cpu.append((d, pid, comm))
            top_cpu.sort(reverse=True)
            # percent of one CPU over the scan span
            s["top_cpu"] = [[pid, comm, round(d * 100.0 / self.hz / span)] for d, pid, comm in top_cpu[:5]]
            s["scan_s"] = round(span, 2)
            exits = []
            for pid, o in self.prev_proc.items():
                if pid not in cur and o[2] * self.page_kb >= 100 * 1024:
                    exits.append([pid, o[0], o[2] * self.page_kb // 1024])
            if exits:
                exits.sort(key=lambda e: -e[2])
                s["exits"] = exits[:5]
        rss = sorted(((v[2], pid, v[0]) for pid, v in cur.items()), reverse=True)[:5]
        s["top_rss"] = [[pid, comm, r * self.page_kb // 1024] for r, pid, comm in rss]
        if dstate:
            s["dproc"] = dstate[:10]
        s["nproc"] = len(cur)
        self.prev_proc = cur
        self.prev_proc_t = mono
        self.last_scan = mono


class Clock:
    def mono(self):
        return time.clock_gettime(time.CLOCK_MONOTONIC)

    def boot(self):
        return time.clock_gettime(getattr(time, "CLOCK_BOOTTIME", time.CLOCK_MONOTONIC))

    def wall(self):
        return time.time()

    def thread_time(self):
        return time.thread_time()


# --------------------------------------------------------------------------
# Detectors and incidents
# --------------------------------------------------------------------------

HARD_FLAGS = ("late", "slow", "mem", "io", "desktop", "kernel", "oom", "mark")


class Detector:
    """Per-sample flags.  Thresholds are per second of wall time."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.cpu_run = 0
        self.gs_d = 0
        self.disp_hist = collections.deque(maxlen=6)

    def flags(self, s, kmsg_classes=()):
        cfg = self.cfg
        fl = []
        # rates per second; a burst sample taken 50 ms after the last one must
        # not turn 20 ms of stall into "400 ms/s"
        dt = max(s.get("dt", 1000), 200) / 1000.0
        suspended = s.get("susp", 0) > 500 or s.get("resumed")
        if s.get("late", 0) >= cfg["late_ms"] and not suspended:
            fl.append("late")
        if s.get("work", 0) >= cfg["late_ms"]:
            fl.append("slow")
        psi = s.get("psi", {})
        if psi.get("memory", [0, 0])[1] / dt >= cfg["mem_full_ms"]:
            fl.append("mem")
        if psi.get("io", [0, 0])[1] / dt >= cfg["io_full_ms"]:
            fl.append("io")
        if psi.get("cpu", [0, 0])[0] / dt >= cfg["cpu_some_ms"]:
            self.cpu_run += 1
        else:
            self.cpu_run = 0
        if self.cpu_run >= cfg["cpu_sustain_s"]:
            fl.append("cpu")
        comp = s.get("comp")
        if comp:
            stall = comp.get("mem", [0, 0])[0] + comp.get("io", [0, 0])[0]
            main = comp.get("main") or [None, None, None]
            self.gs_d = self.gs_d + 1 if main[0] == "D" else 0
            if stall / dt >= cfg["desktop_ms"] or self.gs_d >= 2 or \
                    (main[2] is not None and main[2] / dt >= cfg["desktop_wait_ms"]):
                fl.append("desktop")
        # display went silent while input kept arriving (screen on): soft signal
        if "disp" in s and "inp" in s:
            self.disp_hist.append(s["disp"] / dt)
            was_active = any(x >= cfg["display_active_irqs"] for x in list(self.disp_hist)[:-1])
            if s.get("screen", 1) and s["inp"] / dt >= cfg["display_input_irqs"] and \
                    s["disp"] / dt < 2 and was_active:
                fl.append("display_silent")
        if kmsg_classes:
            fl.append("kernel")
        if s.get("vm", {}).get("oom"):
            fl.append("oom")
        return fl


class Incident:
    def __init__(self, start_mono, start_wall, pre_lines, flags, kind=None):
        self.start_mono = start_mono
        self.start_wall = start_wall
        self.pre = list(pre_lines)
        self.lines = []
        self.flags = collections.Counter(flags)
        self.last_active = start_mono
        self.kind = kind
        self.dump_posted = False
        self.state_text = None

    def add(self, mono, line, flags):
        self.lines.append(line)
        hard = [x for x in flags if x in HARD_FLAGS]
        self.flags.update(flags)
        if hard:
            self.last_active = mono


# --------------------------------------------------------------------------
# Analysis shared by the daemon (at close) and the report command
# --------------------------------------------------------------------------

def mb(pages):
    return pages * 4 / 1024.0


def aggregate(samples, flags_of=None):
    """Totals over a list of sample dicts (the incident window)."""
    a = collections.defaultdict(float)
    a["n"] = len(samples)
    if not samples:
        return a
    a["t0"] = samples[0]["t"] - samples[0].get("dt", 1000) / 1000.0
    a["t1"] = samples[-1]["t"]
    worst = {}
    top_cpu = collections.Counter()
    top_cpu_pid = {}
    dproc = collections.Counter()
    disk = collections.defaultdict(lambda: [0, 0, 0, 0])
    kmsg = []
    exits = []
    probes = []
    temps = {}
    cool = {}
    caps = set()
    a["avail_min"] = a["free_min"] = a["file_min"] = None
    for s in samples:
        dt = max(s.get("dt", 1000), 1)
        psi = s.get("psi", {})
        for res in ("memory", "io", "cpu", "irq"):
            v = psi.get(res)
            if v:
                a[res + "_some"] += v[0]
                a[res + "_full"] += v[1]
                pct = 100.0 * v[1 if res != "cpu" else 0] / dt
                if pct > worst.get(res, (0, 0))[0]:
                    worst[res] = (pct, s["t"])
        for name, v in s.get("cg", {}).items():
            a["cg_" + name + "_some"] += v[0]
            a["cg_" + name + "_full"] += v[1]
        comp = s.get("comp")
        if comp:
            a["comp_pid"] = comp.get("pid", 0)
            for res in ("mem", "io", "cpu"):
                v = comp.get(res)
                if v:
                    a["comp_" + res] += v[0]
            m = comp.get("main")
            if m:
                if m[0] == "D":
                    a["comp_D"] += 1
                if m[2] is not None:
                    a["comp_wait_max"] = max(a["comp_wait_max"], m[2] * 1000.0 / dt)
                if m[1] is not None and m[1] == 0 and m[0] != "R":
                    a["comp_idle_s"] += dt / 1000.0
            k = comp.get("kms")
            if k and k[2] is not None:
                a["kms_wait_max"] = max(a["kms_wait_max"], k[2] * 1000.0 / dt)
            if comp.get("gpu_ms") is not None:
                a["comp_gpu_ms"] += comp["gpu_ms"]
        a["late_max"] = max(a["late_max"], s.get("late", 0))
        a["work_max"] = max(a["work_max"], s.get("work", 0))
        a["dt_max"] = max(a["dt_max"], dt)
        a["susp"] += s.get("susp", 0)
        if s.get("late", 0) >= 200:
            a["late_n"] += 1
        for k, v in s.get("vm", {}).items():
            a["vm_" + k] += v
        mem = s.get("mem", {})
        if "avail" in mem:
            a["avail_min"] = mem["avail"] if a["avail_min"] is None else min(a["avail_min"], mem["avail"])
        if "free" in mem:
            a["free_min"] = mem["free"] if a["free_min"] is None else min(a["free_min"], mem["free"])
        if "afile" in mem and "ifile" in mem:
            fl = mem["afile"] + mem["ifile"]
            a["file_min"] = fl if a["file_min"] is None else min(a["file_min"], fl)
        a["swap_max"] = max(a["swap_max"], mem.get("swapused", 0))
        a["unevict_max"] = max(a["unevict_max"], mem.get("unevict", 0))
        for name, v in s.get("zram", {}).items():
            a["zram_used_max"] = max(a["zram_used_max"], v[2])
            a["zram_orig_max"] = max(a["zram_orig_max"], v[0])
        cpu = s.get("cpu", {})
        if cpu.get("busy"):
            a["cpu_busy_sum"] += sum(cpu["busy"]) / len(cpu["busy"])
            a["cpu_busy_n"] += 1
            a["cpu_max1"] = max(a["cpu_max1"], max(cpu["busy"]))
        a["iow_max"] = max(a["iow_max"], cpu.get("iow", 0))
        for i, c in enumerate(cpu.get("cap") or []):
            if c is not None:
                caps.add((i, c))
        a["load_max"] = max(a["load_max"], s.get("load", 0) or 0)
        a["run_max"] = max(a["run_max"], s.get("run", 0) or 0)
        a["blk_max"] = max(a["blk_max"], s.get("blk", 0) or 0)
        for name, v in s.get("therm", [])[:1]:
            if v > temps.get(name, -999):
                temps[name] = v
        for name, v in s.get("cool", {}).items():
            cool[name] = max(cool.get(name, 0), v)
        a["lmh"] += s.get("lmh", 0)
        for dev, d in s.get("disk", {}).items():
            x = disk[dev]
            x[0] += d[1]
            x[1] += d[4]
            x[2] += d[7]
            x[3] = max(x[3], d[6])
        for pid, comm, pct in s.get("top_cpu", []):
            top_cpu[comm] += pct / 100.0 * s.get("scan_s", dt / 1000.0)  # CPU seconds
            top_cpu_pid.setdefault(comm, set()).add(pid)
        for pid, comm in s.get("dproc", []):
            dproc[comm] += 1
        for e in s.get("exits", []):
            exits.append([s["t"]] + e)
        for k in s.get("kmsg", []):
            kmsg.append(k)
        if "disp" in s:
            a["disp"] += s["disp"]
            a["inp"] += s["inp"]
        for fl in (flags_of(s) if flags_of else s.get("flags", [])):
            a["flag_" + fl] += 1
        for p in s.get("probe", []):
            probes.append(p)
        dv = s.get("devfreq", {})
        for name, v in dv.items():
            if "gpu" in name:
                a["gpu_mhz_max"] = max(a["gpu_mhz_max"], v)
    a["worst"] = worst
    a["top_cpu"] = top_cpu.most_common(6)
    a["top_cpu_pids"] = {k: sorted(v)[:4] for k, v in top_cpu_pid.items()}
    a["dproc"] = dproc.most_common(8)
    a["disk"] = dict(disk)
    a["kmsg"] = kmsg
    a["exits"] = exits
    a["temps"] = temps
    a["cool"] = cool
    a["caps"] = sorted(caps)
    a["probes"] = probes
    return a


def core_window(samples, n_pre=None):
    """-> (the samples that matter, the sample just before them).

    For an incident: from the first to the last sample a detector flagged.  A
    manual mark that nothing flagged covers the whole window (pre-window and
    after): the freeze it marks came before the mark.  n_pre=None means a ring
    window with no incident boundary (the 'at' command)."""
    def hard(s):
        return any(x in HARD_FLAGS and x != "mark" for x in s.get("flags", []))
    start = n_pre or 0
    pure_mark = n_pre is not None and not any(hard(s) for s in samples[start:]) and \
        any("mark" in s.get("flags", []) for s in samples[start:])
    lo_search = 0 if (pure_mark or n_pre is None) else start
    idx = [i for i in range(lo_search, len(samples)) if hard(samples[i])]
    if idx:
        lo, hi = idx[0], idx[-1] + 1
    elif pure_mark or n_pre is None:
        lo, hi = 0, len(samples)
    else:
        lo, hi = start, len(samples)
    return samples[lo:hi], (samples[lo - 1] if lo > 0 else None)


def significant(a, cfg, kinds):
    if "mark" in kinds:
        return True
    if a["memory_full"] >= cfg["mem_incident_ms"]:
        return True
    if a["io_full"] >= cfg["io_incident_ms"]:
        return True
    if a["late_max"] >= cfg["late_incident_ms"]:
        return True
    if a["comp_mem"] + a["comp_io"] >= cfg["desktop_incident_ms"] or a["comp_D"] >= 2:
        return True
    if a["flag_kernel"] or a["vm_oom"]:
        return True
    if a["flag_cpu"] >= 1:
        return True
    if a["flag_desktop"] >= 2:
        return True
    return False


def classify(a, cfg):
    """Ordered list of (class, headline)."""
    out = []
    kcls = collections.Counter(k[1] for k in a["kmsg"] if len(k) > 1 and k[1])
    if a["late_max"] >= cfg["late_incident_ms"] or a["late_n"] >= 3:
        out.append(("machine", "WHOLE-MACHINE stall: the sampler itself (real-time, memory locked) "
                               "could not run for %.1f s" % (a["late_max"] / 1000.0)))
    if kcls.get("gpu"):
        out.append(("gpu", "GPU trouble in the kernel log (%d lines)" % kcls["gpu"]))
    if a["memory_full"] >= cfg["mem_incident_ms"]:
        out.append(("memory", "MEMORY stall: tasks waited %.1f s with every runnable task blocked on memory"
                    % (a["memory_full"] / 1000.0)))
    if a["io_full"] >= cfg["io_incident_ms"]:
        out.append(("io", "I/O stall: %.1f s with every runnable task waiting for storage" % (a["io_full"] / 1000.0)))
    if a["comp_mem"] + a["comp_io"] >= cfg["desktop_incident_ms"] or a["comp_D"] >= 2 or a["flag_desktop"] >= 2:
        out.append(("desktop", "DESKTOP stall: the compositor was blocked (memory %.1f s, I/O %.1f s, "
                               "main thread in D for %d samples)"
                    % (a["comp_mem"] / 1000.0, a["comp_io"] / 1000.0, a["comp_D"])))
    if kcls.get("display"):
        out.append(("display", "display timeouts in the kernel log (%d lines)" % kcls["display"]))
    if a["flag_cpu"]:
        out.append(("cpu", "CPU saturation: cpu pressure stayed above %d ms/s" % cfg["cpu_some_ms"]))
    if a["vm_oom"]:
        out.append(("oom", "OOM killer: %d kills" % a["vm_oom"]))
    for c in ("lockup", "storage", "memory", "bus", "rpmh"):
        if kcls.get(c):
            out.append((c, "%s messages in the kernel log (%d lines)" % (c, kcls[c])))
    if a["flag_mark"] and not out:
        out.append(("mark", "manual mark: no detector fired; read the evidence below"))
    if not out:
        out.append(("minor", "no threshold was crossed"))
    return out


def fmt_mb(v):
    if v is None:
        return "?"
    return "%.1f GB" % (v / 1024.0) if abs(v) >= 1024 else "%d MB" % round(v)


def ts(t, frac=False):
    if t is None:
        return "?"
    s = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))
    return s + (".%03d" % int((t % 1) * 1000) if frac else "")


def verdict(a, before, cfg, title=None):
    """The short human-readable verdict."""
    L = []
    cls = classify(a, cfg)
    dur = a["t1"] - a["t0"] if a["n"] else 0
    if title:
        L.append(title)
    L.append("Window: %s -> %s (%.0f s, %d samples)" % (ts(a["t0"]), ts(a["t1"])[11:], dur, a["n"]))
    L.append("Verdict: " + cls[0][1])
    for c in cls[1:]:
        L.append("    also: " + c[1])
    # memory
    vm = lambda k: a.get("vm_" + k, 0)
    rf = mb(vm("rf_file"))
    m = ["memory: full stall %.1f s (worst second %d %%), some %.1f s"
         % (a["memory_full"] / 1000.0, a["worst"].get("memory", (0, 0))[0], a["memory_some"] / 1000.0)]
    if "cg_user_full" in a:
        m.append("user.slice full %.1f s" % (a["cg_user_full"] / 1000.0))
    cgs = [(n[3:-5], a[n], a.get("cg_%s_full" % n[3:-5], 0)) for n in sorted(a) if n.startswith("cg_") and n.endswith("_some")]
    m.append("direct reclaim %d entries, scanned %s, reclaimed %s" % (vm("allocstall"), fmt_mb(mb(vm("scan_d"))),
                                                                       fmt_mb(mb(vm("steal_d")))))
    m.append("kswapd scanned %s, reclaimed %s" % (fmt_mb(mb(vm("scan_k"))), fmt_mb(mb(vm("steal_k")))))
    m.append("swap-out %s, swap-in %s" % (fmt_mb(mb(vm("pswpout"))), fmt_mb(mb(vm("pswpin")))))
    m.append("refaults: file %s, anon %s%s" % (fmt_mb(rf), fmt_mb(mb(vm("rf_anon"))),
                                                 " (file cache thrashing)" if rf >= 100 else ""))
    if vm("cstall") or vm("migfail"):
        m.append("compaction stalls %d (failed %d), migration failures %d" % (vm("cstall"), vm("cfail"), vm("migfail")))
    if vm("cull") or vm("resc"):
        m.append("unevictable (GPU buffer) pages added %s, released %s" % (fmt_mb(mb(vm("cull"))), fmt_mb(mb(vm("resc")))))
    if vm("thp_fb"):
        m.append("THP fallbacks %d" % vm("thp_fb"))
    m.append("OOM kills %d" % vm("oom"))
    L.append("  " + "; ".join(m))
    if cgs:
        names = {"user": "user.slice", "system": "system.slice", "app": "apps (app.slice)", "bt": "bluetoothd"}
        L.append("  memory stall by cgroup (some/full): " + ", ".join(
            "%s %.1f/%.1f s" % (names.get(n, n), sv / 1000.0, fv / 1000.0) for n, sv, fv in cgs))
    if before:
        mem = before.get("mem", {})
        zr = before.get("zram", {})
        zs = ", ".join("%s %s of %s stored" % (n, fmt_mb(v[2]), fmt_mb(v[0])) for n, v in zr.items())
        L.append("  memory just before: available %s, free %s, file cache %s, anon %s, shmem %s, unevictable %s, "
                 "swap used %s%s"
                 % (fmt_mb(mem.get("avail")), fmt_mb(mem.get("free")),
                    fmt_mb((mem.get("afile") or 0) + (mem.get("ifile") or 0)), fmt_mb(mem.get("anon")),
                    fmt_mb(mem.get("shmem")), fmt_mb(mem.get("unevict")), fmt_mb(mem.get("swapused")),
                    ("; zram: " + zs) if zs else ""))
    L.append("  lowest during: available %s, free %s, file cache %s; swap used up to %s"
             % (fmt_mb(a["avail_min"]), fmt_mb(a["free_min"]), fmt_mb(a["file_min"]), fmt_mb(a["swap_max"])))
    # desktop
    if a.get("comp_pid"):
        L.append("  desktop (compositor pid %d): memory stall %.1f s, I/O stall %.1f s, cpu wait %.1f s; main thread "
                 "in D %d samples, run-queue wait up to %d ms/s (KMS thread up to %d ms/s); GPU time %.1f s; "
                 "display irqs %d, input irqs %d"
                 % (a["comp_pid"], a["comp_mem"] / 1000.0, a["comp_io"] / 1000.0, a["comp_cpu"] / 1000.0,
                    a["comp_D"], a["comp_wait_max"], a["kms_wait_max"], a["comp_gpu_ms"] / 1000.0,
                    a["disp"], a["inp"]))
        if a["flag_display_silent"]:
            L.append("  display: frames stopped while input kept coming for %d samples" % a["flag_display_silent"])
    # sampler
    if a["late_max"] >= cfg["late_ms"]:
        L.append("  sampler: late by up to %.0f ms (%d samples over %d ms): the kernel or a higher-priority task "
                 "kept a real-time, memory-locked process off the CPU" % (a["late_max"], a["late_n"], cfg["late_ms"]))
    else:
        L.append("  sampler: never more than %.0f ms late (longest gap %.0f ms): the kernel and scheduler kept "
                 "running, so this was not a whole-machine stall" % (a["late_max"], a["dt_max"]))
    if a["susp"] > 500:
        L.append("  note: the tablet was suspended for %.0f s inside this window" % (a["susp"] / 1000.0))
    # cpu
    busy = a["cpu_busy_sum"] / a["cpu_busy_n"] if a["cpu_busy_n"] else 0
    top = ", ".join("%s %.1f s" % (c, v) for c, v in a["top_cpu"][:5])
    L.append("  cpu: pressure some %.1f s; average busy %d %%, busiest cpu %d %%; load up to %.1f, running up to %d, "
             "blocked up to %d; iowait up to %.1f %%; top: %s"
             % (a["cpu_some"] / 1000.0, busy, a["cpu_max1"], a["load_max"], a["run_max"], a["blk_max"],
                a["iow_max"], top or "?"))
    caps = ", ".join("policy%d %d MHz" % c for c in a["caps"])
    th = ", ".join("%s %.0f C" % (n, v) for n, v in sorted(a["temps"].items(), key=lambda x: -x[1])[:2])
    L.append("  frequency caps: %s; LMh interrupts %d; hottest: %s; cooling: %s"
             % (caps or "none", a["lmh"], th or "?",
                ", ".join("%s=%d" % kv for kv in a["cool"].items()) or "idle"))
    # io
    dk = ", ".join("%s read %s write %s busy %d ms inflight<=%d" % (d, fmt_mb(v[0] / 1024.0), fmt_mb(v[1] / 1024.0), v[2], v[3])
                   for d, v in sorted(a["disk"].items()) if v[0] or v[1] or v[2])
    L.append("  io: full %.1f s, some %.1f s; %s" % (a["io_full"] / 1000.0, a["io_some"] / 1000.0, dk or "no disk activity"))
    if a["gpu_mhz_max"]:
        L.append("  gpu: frequency up to %d MHz" % a["gpu_mhz_max"])
    if a["dproc"]:
        L.append("  processes in D state (samples): " + ", ".join("%s x%d" % x for x in a["dproc"]))
    if a["exits"]:
        L.append("  big processes that exited: " + ", ".join("%s %d (%s) at %s" % (e[2], e[1], fmt_mb(e[3]), ts(e[0])[11:])
                                                         for e in a["exits"][:5]))
    if a["kmsg"]:
        kc = collections.Counter("%s" % (k[2][:70]) for k in a["kmsg"])
        L.append("  kernel: " + "; ".join("%s x%d" % (k, n) if n > 1 else k for k, n in kc.most_common(6)))
    return L


# --------------------------------------------------------------------------
# Disk: the ring, incident records, caps
# --------------------------------------------------------------------------

def dir_size(path):
    total = 0
    for root, dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def free_mb(path):
    try:
        st = os.statvfs(path)
        return st.f_bavail * st.f_frsize // (1024 * 1024)
    except OSError:
        return 0


class Store:
    def __init__(self, cfg, data_dir, group=None):
        self.cfg = cfg
        self.dir = data_dir
        self.ring = os.path.join(data_dir, "ring")
        self.inc = os.path.join(data_dir, "incidents")
        self.group = group
        self.seg_path = None
        self.seg_start = None
        self.header = None
        for d in (self.dir, self.ring, self.inc):
            self._mkdir(d)

    def _mkdir(self, d):
        try:
            os.makedirs(d, exist_ok=True)
            if self.group is not None:
                os.chown(d, 0, self.group)
                os.chmod(d, 0o2750)
        except OSError:
            pass

    def _own(self, path):
        if self.group is not None:
            try:
                os.chown(path, 0, self.group)
                os.chmod(path, 0o640)
            except OSError:
                pass

    def writable(self):
        return free_mb(self.dir) >= self.cfg["min_free_mb"]

    def append_ring(self, lines, wall, sync=False):
        if not lines or not self.writable():
            return 0
        seg_s = self.cfg["ring_segment_s"]
        if self.seg_path is None or wall - self.seg_start >= seg_s or not os.path.exists(self.seg_path):
            self.seg_start = wall
            name = "seg-%s.jsonl.gz" % time.strftime("%Y%m%d-%H%M%S", time.localtime(wall))
            self.seg_path = os.path.join(self.ring, name)
            if self.header:
                lines = [json.dumps(self.header, separators=(",", ":")).encode()] + list(lines)
        comp = zlib.compressobj(6, zlib.DEFLATED, 31)
        blob = comp.compress(b"\n".join(lines) + b"\n") + comp.flush()
        new = not os.path.exists(self.seg_path)
        fd = os.open(self.seg_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_CLOEXEC, 0o640)
        try:
            os.write(fd, blob)
            if sync:
                os.fdatasync(fd)
            try:
                os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
            except (AttributeError, OSError):
                pass
        finally:
            os.close(fd)
        if new:
            self._own(self.seg_path)
        self.enforce_ring_cap()
        return len(blob)

    def enforce_ring_cap(self):
        cap = self.cfg["ring_max_mb"] * 1024 * 1024
        segs = sorted(x for x in os.listdir(self.ring) if x.startswith("seg-"))
        sizes = []
        for x in segs:
            try:
                sizes.append(os.path.getsize(os.path.join(self.ring, x)))
            except OSError:
                sizes.append(0)
        total = sum(sizes)
        i = 0
        while total > cap and i < len(segs) - 1:
            p = os.path.join(self.ring, segs[i])
            if p != self.seg_path:
                try:
                    os.unlink(p)
                except OSError:
                    pass
                total -= sizes[i]
            i += 1

    def write_incident(self, iid, files):
        """files: {name: bytes}.  Written to a temporary name, then renamed."""
        if not self.writable():
            return None
        final = os.path.join(self.inc, iid)
        tmp = final + ".partial"
        n = 1
        while os.path.exists(final):
            n += 1
            final = os.path.join(self.inc, "%s.%d" % (iid, n))
        os.makedirs(tmp, exist_ok=True)
        for name, data in files.items():
            p = os.path.join(tmp, name)
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC, 0o640)
            try:
                os.write(fd, data)
                os.fdatasync(fd)
                try:
                    os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
                except (AttributeError, OSError):
                    pass
            finally:
                os.close(fd)
            self._own(p)
        os.rename(tmp, final)
        if self.group is not None:
            try:
                os.chown(final, 0, self.group)
                os.chmod(final, 0o2750)
            except OSError:
                pass
        self.enforce_incident_cap()
        return final

    def add_to_incident(self, path, name, data):
        if not path or not os.path.isdir(path):
            return
        p = os.path.join(path, name)
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC, 0o640)
        try:
            os.write(fd, data)
            os.fdatasync(fd)
        finally:
            os.close(fd)
        self._own(p)
        self.enforce_incident_cap()

    def incidents(self):
        try:
            return sorted(x for x in os.listdir(self.inc) if not x.endswith(".partial"))
        except OSError:
            return []

    def enforce_incident_cap(self):
        cap = self.cfg["incidents_max_mb"] * 1024 * 1024
        names = self.incidents()
        for x in os.listdir(self.inc):
            if x.endswith(".partial"):
                _rmtree(os.path.join(self.inc, x))
        sizes = [dir_size(os.path.join(self.inc, x)) for x in names]
        total = sum(sizes)
        i = 0
        while i < len(names) - 1 and (total > cap or len(names) - i > self.cfg["incidents_max_count"]):
            _rmtree(os.path.join(self.inc, names[i]))
            total -= sizes[i]
            i += 1


def _rmtree(path):
    for root, dirs, files in os.walk(path, topdown=False):
        for f in files:
            try:
                os.unlink(os.path.join(root, f))
            except OSError:
                pass
        for d in dirs:
            try:
                os.rmdir(os.path.join(root, d))
            except OSError:
                pass
    try:
        os.rmdir(path)
    except OSError:
        pass


# --------------------------------------------------------------------------
# The daemon
# --------------------------------------------------------------------------

def log(msg):
    try:
        sys.stdout.write("gts9u-stall-watch: %s\n" % msg)
        sys.stdout.flush()
    except (OSError, ValueError):
        pass


def try_mlockall():
    try:
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        MCL_CURRENT, MCL_FUTURE = 1, 2
        if libc.mlockall(MCL_CURRENT | MCL_FUTURE) != 0:
            return "failed: %s" % os.strerror(ctypes.get_errno())
        return "ok"
    except Exception as e:  # noqa: BLE001 - any failure just means no lock
        return "failed: %s" % e


def self_status_kb(files, key):
    st = parse_kv(files.read("/proc/self/status"), {key})
    return st.get(key, 0)


def demote_thread(nice):
    """Helper threads run as ordinary tasks whatever the main thread is."""
    try:
        os.sched_setscheduler(0, os.SCHED_OTHER, os.sched_param(0))
    except (AttributeError, OSError):
        pass
    try:
        os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), nice)
    except (AttributeError, OSError):
        pass


class Daemon:
    def __init__(self, cfg, root="/", data_dir=DATA_DIR, run_dir=RUN_DIR, clock=None, group=None,
                 arm_triggers=True, use_kmsg=True, use_fifo=True):
        self.cfg = cfg
        self.files = Files(root)
        self.clock = clock or Clock()
        self.sampler = Sampler(cfg, self.files, self.clock)
        self.detector = Detector(cfg)
        self.store = Store(cfg, data_dir, group)
        self.run_dir = run_dir
        self.group = group
        self.root = root
        try:
            os.makedirs(run_dir, exist_ok=True)
        except OSError:
            pass
        self.pre_n = max(1, int(cfg["pre_s"] * 1000 / cfg["interval_ms"]))
        self.ring = collections.deque(maxlen=self.pre_n)
        self.pending = []
        self.incident = None
        self.esc_until = -1
        self.last_flush = None
        self.last_status = None
        self.stop = False
        self.q = collections.deque()
        self.qcv = threading.Condition()
        self.started_wall = self.clock.wall()
        self.started_mono = self.clock.mono()
        self.stats = {"samples": 0, "missed": 0, "trig": 0, "late": [], "work": [], "incidents": 0,
                      "minor": 0, "kmsg": 0, "bytes": 0, "dropped": 0, "last_incident": None,
                      "errors": 0, "last_error": None}
        self.saved_times = collections.deque(maxlen=200)
        self.kmsg_fd = None
        self.kmsg_state = "off"
        self.kmsg_buf = []
        self.triggers = {}
        self.trigger_state = {}
        self.fifo_fd = None
        self.fifo_keep = None
        self.lock_state = "off"
        self.last_sample_mono = None
        self.marks = 0
        self.probe_req = threading.Event()
        self.probe_lock = threading.Lock()
        self.probe_results = []
        if arm_triggers:
            self.arm_triggers()
        if use_kmsg:
            self.open_kmsg()
        if use_fifo:
            self.open_fifo()
        self.sampler.discover(self.clock.mono())
        self.store.header = self.header()

    # ----- setup -----------------------------------------------------------
    def header(self):
        u = os.uname()
        boot = (self.files.read("/proc/sys/kernel/random/boot_id") or b"").strip().decode()
        compat = (self.files.read("/proc/device-tree/compatible") or b"").replace(b"\0", b" ").strip()
        return {"hdr": FORMAT, "version": VERSION, "boot": boot, "kernel": u.release, "host": u.nodename,
                "board": compat.decode("ascii", "replace"), "interval_ms": self.cfg["interval_ms"],
                "policies": [[p, hw] for p, _, hw in getattr(self.sampler, "policies", [])]}

    def arm_triggers(self):
        for res in ("memory", "io", "cpu"):
            spec = self.cfg.get("psi_trigger_" + res, "")
            if not spec:
                continue
            path = self.files.path("/proc/pressure/" + res)
            if not os.path.exists(path):
                self.trigger_state[res] = "no PSI"
                continue
            for attempt in (spec, self._unpriv(spec)):
                if attempt is None:
                    continue
                fd = None
                try:
                    fd = os.open(path, os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC)
                    os.write(fd, attempt.encode() + b"\0")
                except OSError as e:
                    if fd is not None:
                        os.close(fd)
                    self.trigger_state[res] = "failed: %s" % e.strerror
                    continue
                self.triggers[fd] = res
                self.trigger_state[res] = "armed: " + attempt
                break

    @staticmethod
    def _unpriv(spec):
        """Unprivileged triggers need a window that is a multiple of 2 s."""
        p = spec.split()
        if len(p) != 3:
            return None
        try:
            stall, win = int(p[1]), int(p[2])
        except ValueError:
            return None
        new = max(2000000, (win + 1999999) // 2000000 * 2000000)
        if new == win:
            return None
        return "%s %d %d" % (p[0], stall * new // win, new)

    def open_kmsg(self):
        try:
            fd = os.open(self.files.path("/dev/kmsg"), os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
            os.lseek(fd, 0, os.SEEK_END)
            self.kmsg_fd = fd
            self.kmsg_state = "reading"
        except OSError as e:
            self.kmsg_state = "unavailable: %s" % e.strerror

    def open_fifo(self):
        path = os.path.join(self.run_dir, "mark")
        try:
            try:
                st = os.lstat(path)
                if not stat.S_ISFIFO(st.st_mode):
                    os.unlink(path)
                    raise FileNotFoundError
            except FileNotFoundError:
                os.mkfifo(path, 0o620)
            if self.group is not None:
                os.chown(path, 0, self.group)
            os.chmod(path, 0o620)
            self.fifo_fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
            # our own writer end keeps poll() from reporting POLLHUP all the time
            self.fifo_keep = os.open(path, os.O_WRONLY | os.O_NONBLOCK | os.O_CLOEXEC)
        except OSError:
            self.fifo_fd = None

    # ----- kmsg --------------------------------------------------------------
    def read_kmsg(self, limit=200):
        if self.kmsg_fd is None:
            return
        n = 0
        while n < limit:
            try:
                rec = os.read(self.kmsg_fd, 8192)
            except BlockingIOError:
                break
            except OSError as e:
                if e.errno == errno.EPIPE:
                    n += 1
                    continue  # overwritten before we read it; go on with the next one
                break
            if not rec:
                break
            n += 1
            r = parse_kmsg_record(rec)
            if not r:
                continue
            level, usec, text = r
            cls = classify_kmsg(text)
            if level <= 4 or cls:
                self.stats["kmsg"] += 1
                if len(self.kmsg_buf) < 50:
                    self.kmsg_buf.append([round(usec / 1e6, 3), cls or "", text[:200], level])

    # ----- main loop ---------------------------------------------------------
    def run(self, max_samples=None, max_seconds=None):
        cfg = self.cfg
        interval = cfg["interval_ms"] / 1000.0
        poller = select.poll()
        for fd in self.triggers:
            poller.register(fd, select.POLLPRI)
        if self.kmsg_fd is not None:
            poller.register(self.kmsg_fd, select.POLLIN)
        if self.fifo_fd is not None:
            poller.register(self.fifo_fd, select.POLLIN)
        try:
            threading.stack_size(1 << 20)  # locked memory: no 8 MB stacks
        except (ValueError, RuntimeError):
            pass
        writer = threading.Thread(target=self.writer_loop, name="writer", daemon=True)
        writer.start()
        prober = threading.Thread(target=self.probe_loop, name="probe", daemon=True)
        prober.start()
        start = self.clock.mono()
        nxt = start + interval
        last_rss_check = start
        while not self.stop:
            now = self.clock.mono()
            if max_seconds is not None and now - start >= max_seconds:
                break
            timeout = max(0.0, nxt - now)
            try:
                events = poller.poll(int(timeout * 1000 + 0.999))
            except InterruptedError:
                events = []
            now = self.clock.mono()
            trig = []
            for fd, ev in events:
                if fd in self.triggers:
                    if ev & (select.POLLERR | select.POLLNVAL):
                        poller.unregister(fd)
                        self.trigger_state[self.triggers[fd]] = "error"
                    else:
                        trig.append(self.triggers[fd])
                elif fd == self.kmsg_fd:
                    self.read_kmsg()
                elif fd == self.fifo_fd:
                    try:
                        data = os.read(self.fifo_fd, 4096)
                    except OSError:
                        data = b""
                    if data:
                        self.marks += 1
                        trig.append("mark")
            if trig:
                self.stats["trig"] += 1
                if self.last_sample_mono is None or now - self.last_sample_mono >= 0.2 or "mark" in trig:
                    s = self.take(now, 0.0, "trig:" + ",".join(sorted(set(trig))), mark="mark" in trig)
                    if "mark" in trig or self.trigger_confirmed(s, trig):
                        self.esc_until = max(self.esc_until, now + cfg["escalate_s"])
                    else:
                        self.stats["trig_unconfirmed"] = self.stats.get("trig_unconfirmed", 0) + 1
            if now >= nxt:
                late = (now - nxt) * 1000.0
                missed = int((now - nxt) / interval)
                if missed:
                    self.stats["missed"] += missed
                self.take(now, late, "tick")
                nxt += (missed + 1) * interval
                if max_samples is not None and self.stats["samples"] >= max_samples:
                    break
            if now - last_rss_check >= 60:
                last_rss_check = now
                rss = self_status_kb(self.files, "VmRSS")
                if rss > cfg["max_rss_mb"] * 1024:
                    log("resident size %d MB is over max_rss_mb; exiting so that systemd restarts it" % (rss // 1024))
                    self.stop = True
        self.shutdown(writer, prober)

    def trigger_confirmed(self, s, resources):
        """A PSI trigger wakeup counts only if the stall it reports shows up in
        the totals: at least a quarter of its threshold since the last sample."""
        if not s:
            return False
        for res in resources:
            spec = (self.cfg.get("psi_trigger_" + res) or "").split()
            v = s.get("psi", {}).get(res)
            if len(spec) != 3 or not v:
                continue
            got = v[0] if spec[0] == "some" else v[1]
            if got * 1000 >= to_int(spec[1]) / 4:
                return True
        return False

    def take(self, now, late, kind, mark=False):
        try:
            escalated = now < self.esc_until
            s = self.sampler.sample(now, self.clock.boot(), self.clock.wall(), late, kind, escalated)
            self.read_kmsg()
            if self.kmsg_buf:
                s["kmsg"], self.kmsg_buf = self.kmsg_buf, []
            kcls = [k[1] for k in s.get("kmsg", []) if k[1]]
            fl = self.detector.flags(s, kcls)
            if mark:
                fl.append("mark")
            if fl:
                s["flags"] = fl
                if any(x in HARD_FLAGS for x in fl):
                    self.esc_until = max(self.esc_until, now + self.cfg["escalate_s"])
                    self.probe_req.set()
            with self.probe_lock:
                if self.probe_results:
                    s["probe"], self.probe_results = self.probe_results[:30], []
            line = json.dumps(s, separators=(",", ":")).encode()
        except Exception as e:  # noqa: BLE001 - a sampling bug must not stop the loop
            self.stats["errors"] += 1
            self.stats["last_error"] = "%s: %s" % (type(e).__name__, e)
            return None
        self.last_sample_mono = now
        st = self.stats
        st["samples"] += 1
        st["late"].append(late)
        st["work"].append(s.get("work", 0))
        if len(st["late"]) > 3600:
            del st["late"][:1800]
            del st["work"][:1800]
        self.pending.append(line)
        self.incident_step(now, s, line, fl)
        self.ring.append(line)
        if self.last_flush is None:
            self.last_flush = now
        if now - self.last_flush >= (10 if self.incident else self.cfg["ring_flush_s"]):
            self.flush(now)
        if self.last_status is None or now - self.last_status >= 10:
            self.last_status = now
            self.post(("status", self.status_dict()))
        return s

    def flush(self, now, sync=None):
        if self.pending:
            self.post(("ring", self.pending, self.clock.wall(), bool(self.incident) if sync is None else sync))
            self.pending = []
        self.last_flush = now

    def incident_step(self, now, s, line, fl):
        hard = [x for x in fl if x in HARD_FLAGS]
        inc = self.incident
        if inc is None:
            if hard:
                self.incident = Incident(now, s["t"], list(self.ring), fl, kind="mark" if "mark" in fl else None)
                self.incident.add(now, line, fl)
                self.flush(now, sync=True)
            return
        inc.add(now, line, fl)
        if not hard and not inc.dump_posted:
            # the stall has just cleared: the state dump now, the journal at close
            inc.dump_posted = True
            self.post(("state", inc))
        only_mark = set(x for x in inc.flags if x in HARD_FLAGS) <= {"mark"}
        post = self.cfg["mark_post_s"] if only_mark else self.cfg["post_s"]
        if now - inc.last_active >= post or now - inc.start_mono >= self.cfg["max_incident_s"]:
            self.incident = None
            if not inc.dump_posted:
                inc.dump_posted = True
                self.post(("state", inc))
            self.post(("incident", inc, dict(self.cfg)))
            self.flush(now, sync=True)

    # ----- probes: D-state threads and the compositor's stacks ---------------
    def probe_loop(self):
        demote_thread(0)
        last = -1e9
        seen = set()
        while not self.stop:
            if not self.probe_req.wait(1.0):
                continue
            self.probe_req.clear()
            now = self.clock.mono()
            if now - last < 2.0:
                continue
            last = now
            try:
                res = self.probe_once(seen)
            except Exception as e:  # noqa: BLE001
                self.stats["last_error"] = "probe: %s: %s" % (type(e).__name__, e)
                res = None
            if res:
                with self.probe_lock:
                    self.probe_results.extend(res)
                    del self.probe_results[60:]
            if len(seen) > 5000:
                seen.clear()

    def probe_once(self, seen):
        f = self.files
        out = []
        t = round(self.clock.wall(), 3)
        dthreads = []
        for pid in f.listdir("/proc"):
            if not pid.isdigit():
                continue
            for tid in f.listdir("/proc/%s/task" % pid):
                st = parse_pid_stat(f.read("/proc/%s/task/%s/stat" % (pid, tid)))
                if st and st[1] == "D":
                    dthreads.append((int(pid), int(tid), st[0]))
            if len(dthreads) > 64:
                break
        comp = self.sampler.comp
        wanted = list(dthreads[:24])
        if comp:
            for tid in (comp.get("pid"), comp.get("kms")):
                if tid and not any(x[1] == tid for x in wanted):
                    st = parse_pid_stat(f.read("/proc/%d/task/%d/stat" % (comp["pid"], tid)))
                    if st:
                        wanted.append((comp["pid"], tid, "%s(%s)" % (st[0], st[1])))
        for pid, tid, comm in wanted:
            base = "/proc/%d/task/%d/" % (pid, tid)
            wchan = (f.read(base + "wchan") or b"").decode("ascii", "replace").strip()
            stack = (f.read(base + "stack") or b"").decode("ascii", "replace")
            frames = [re.sub(r"\+0x[0-9a-f]+/0x[0-9a-f]+", "", l.split("] ", 1)[-1].strip())
                      for l in stack.splitlines() if l.strip()][:16]
            key = (comm.split("(")[0], wchan, tuple(frames))
            entry = [t, pid, tid, comm, wchan]
            if key in seen:
                entry.append("(stack as before)")
            else:
                seen.add(key)
                entry.append(" < ".join(frames) if frames else "")
            out.append(entry)
        if dthreads:
            out.append([t, 0, 0, "D-threads", str(len(dthreads)),
                        ", ".join(sorted({c for _, _, c in dthreads}))[:300]])
        return out

    # ----- the writer thread ------------------------------------------------------
    def post(self, job):
        with self.qcv:
            if len(self.q) > 200:
                self.q.popleft()
                self.stats["dropped"] += 1
            self.q.append(job)
            self.qcv.notify()

    def writer_loop(self):
        demote_thread(10)
        while True:
            with self.qcv:
                while not self.q and not self.stop:
                    self.qcv.wait(1.0)
                if not self.q:
                    return
                job = self.q.popleft()
            self.do_job(job)

    def drain(self):
        """Run every queued job here and now (tests, and shutdown without threads)."""
        while True:
            with self.qcv:
                if not self.q:
                    return
                job = self.q.popleft()
            self.do_job(job)

    def do_job(self, job):
        try:
            kind = job[0]
            if kind == "ring":
                self.stats["bytes"] += self.store.append_ring(job[1], job[2], sync=job[3])
            elif kind == "status":
                self.write_status(job[1])
            elif kind == "state":
                job[1].state_text = state_dump(self.files, self.sampler.comp, self.root)
            elif kind == "incident":
                self.save_incident(job[1], job[2])
        except Exception as e:  # noqa: BLE001 - keep writing whatever happens
            self.stats["errors"] += 1
            self.stats["last_error"] = "writer: %s: %s" % (type(e).__name__, e)

    def save_incident(self, inc, cfg):
        samples = []
        lines = []
        for line in inc.pre + inc.lines:
            try:
                samples.append(json.loads(line))
                lines.append(line)
            except ValueError:
                pass
        n_pre = min(len(inc.pre), len(samples))
        core, before = core_window(samples, n_pre)
        a = aggregate(core)
        if not significant(a, cfg, set(inc.flags)):
            self.stats["minor"] += 1
            return None
        now = self.clock.wall()
        while self.saved_times and now - self.saved_times[0] > 3600:
            self.saved_times.popleft()
        summary_only = len(self.saved_times) >= cfg["incidents_per_hour"]
        self.saved_times.append(now)
        cls = classify(a, cfg)
        t0 = a["t0"] if a["n"] else inc.start_wall
        iid = time.strftime("%Y%m%d-%H%M%S", time.localtime(t0)) + "-" + cls[0][0]
        vtext = verdict(a, before, cfg, title="Incident " + iid)
        summary = {
            "id": iid, "version": VERSION, "format": FORMAT, "boot": self.store.header.get("boot"),
            "kernel": self.store.header.get("kernel"), "board": self.store.header.get("board"),
            "start": a["t0"], "end": a["t1"], "classes": [c[0] for c in cls], "headline": cls[0][1],
            "flags": dict(inc.flags), "pre_samples": n_pre, "samples": len(samples),
            "summary_only": summary_only, "memory_full_ms": a["memory_full"], "io_full_ms": a["io_full"],
            "late_max_ms": a["late_max"], "desktop_mem_ms": a["comp_mem"], "verdict": vtext,
        }
        files = {"summary.json": json.dumps(summary, indent=1).encode()}
        if not summary_only:
            files["samples.jsonl"] = b"\n".join(lines) + b"\n"
            dump = "one-shot state dump, taken when the stall cleared:\n" + (inc.state_text or "(none)\n")
            dump += journal_dump(t0 - cfg["pre_s"], a["t1"] + 2, self.root)
            files["dump.txt"] = dump.encode("utf-8", "replace")
        path = self.store.write_incident(iid, files)
        if path:
            self.stats["incidents"] += 1
            self.stats["last_incident"] = os.path.basename(path)
            if cfg.get("journal_markers"):
                log("incident %s saved: %s" % (os.path.basename(path), cls[0][1]))
        return path

    def status_dict(self):
        st = self.stats
        lat = sorted(st["late"]) or [0]
        wk = sorted(st["work"]) or [0]
        pct = lambda v, p: v[min(len(v) - 1, int(len(v) * p))]
        cpu = os.times()
        up = self.clock.mono() - self.started_mono
        return {
            "version": VERSION, "pid": os.getpid(), "started": self.started_wall, "uptime_s": round(up),
            "updated": self.clock.wall(), "samples": st["samples"], "missed": st["missed"],
            "trigger_wakeups": st["trig"], "trigger_unconfirmed": st.get("trig_unconfirmed", 0),
            "cpu_s": round(cpu.user + cpu.system, 2),
            "cpu_pct": round(100.0 * (cpu.user + cpu.system) / up, 3) if up > 0 else 0,
            "late_ms": {"p50": round(pct(lat, 0.5), 1), "p99": round(pct(lat, 0.99), 1), "max": round(lat[-1], 1)},
            "work_ms": {"p50": round(pct(wk, 0.5), 2), "p99": round(pct(wk, 0.99), 2), "max": round(wk[-1], 2)},
            "incidents": st["incidents"], "minor_episodes": st["minor"], "last_incident": st["last_incident"],
            "incident_open": bool(self.incident), "kmsg_lines": st["kmsg"], "ring_bytes_written": st["bytes"],
            "queue_dropped": st["dropped"], "errors": st["errors"], "last_error": st["last_error"],
            "triggers": self.trigger_state, "kmsg": self.kmsg_state, "mlock": self.lock_state,
            "vmlck_kb": self_status_kb(self.files, "VmLck"), "rss_kb": self_status_kb(self.files, "VmRSS"),
            "compositor": (self.sampler.comp or {}).get("pid"), "interval_ms": self.cfg["interval_ms"],
            "marks": self.marks, "slow_reads": self.files.slow,
        }

    def write_status(self, d):
        path = os.path.join(self.run_dir, "status.json")
        tmp = path + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(d, f)
            os.chmod(tmp, 0o644)
            os.rename(tmp, path)
        except OSError:
            pass

    def shutdown(self, writer=None, prober=None):
        now = self.clock.mono()
        if self.incident:
            inc, self.incident = self.incident, None
            if not inc.dump_posted:
                inc.dump_posted = True
                self.post(("state", inc))
            self.post(("incident", inc, dict(self.cfg)))
        self.flush(now, sync=True)
        self.post(("status", self.status_dict()))
        self.stop = True
        with self.qcv:
            self.qcv.notify_all()
        if writer is not None:
            writer.join(timeout=60)
        else:
            self.drain()
        if prober is not None:
            prober.join(timeout=5)
        self.files.close()


def state_dump(files, comp, root="/"):
    """Taken by the writer thread (nice 10) as soon as the stall clears."""
    out = io.StringIO()

    def section(title):
        out.write("\n===== %s =====\n" % title)

    def cat(p, limit=200000):
        d = files.read(p)
        out.write(d.decode("utf-8", "replace")[:limit] if d is not None else "(not available)\n")

    out.write("taken %s\n" % ts(time.time(), frac=True))
    for p in ("/proc/pressure/memory", "/proc/pressure/io", "/proc/pressure/cpu", "/proc/pressure/irq",
              "/proc/loadavg", "/proc/swaps", "/proc/meminfo", "/proc/vmstat", "/proc/buddyinfo"):
        section(p)
        cat(p)
    for z in sorted(files.listdir("/sys/block")):
        if z.startswith("zram"):
            section("/sys/block/%s: mm_stat, io_stat, comp_algorithm, disksize" % z)
            for n in ("mm_stat", "io_stat", "comp_algorithm", "disksize"):
                cat("/sys/block/%s/%s" % (z, n))
    section("processes by RSS (ps)")
    run_cmd(out, ["ps", "-eo", "pid,ppid,stat,ni,cls,rtprio,rss,pcpu,etimes,wchan:28,args", "--sort=-rss"], root,
            limit=60000)
    section("cgroups by memory.current (>= 32 MB)")
    rows = []
    for top in ("/sys/fs/cgroup/user.slice", "/sys/fs/cgroup/system.slice"):
        base = files.path(top)
        for dirpath, dirs, fns in os.walk(base):
            if dirpath.count("/") - base.count("/") > 5 or "memory.current" not in fns:
                dirs[:] = []
                continue
            rel = top + dirpath[len(base):]
            cur = to_int((files.read(rel + "/memory.current") or b"0").strip())
            if cur < 32 * 1024 * 1024:
                dirs[:] = []
                continue
            sw = to_int((files.read(rel + "/memory.swap.current") or b"0").strip())
            pr = (files.read(rel + "/memory.pressure") or b"").decode().strip().replace("\n", " | ")
            ev = (files.read(rel + "/memory.events") or b"").decode().strip().replace("\n", " ")
            rows.append((cur, rel, sw, pr, ev))
    for cur, rel, sw, pr, ev in sorted(rows, reverse=True)[:40]:
        out.write("%7d MB  swap %6d MB  %s\n        pressure: %s\n        events: %s\n"
                  % (cur >> 20, sw >> 20, rel.replace("/sys/fs/cgroup", ""), pr, ev))
    section("GPU memory per DRM client (fdinfo)")
    seen = set()
    for pid in files.listdir("/proc"):
        if not pid.isdigit():
            continue
        for fd in files.listdir("/proc/%s/fd" % pid):
            target = files.readlink("/proc/%s/fd/%s" % (pid, fd)) or ""
            if not target.startswith("/dev/dri/"):
                continue
            info = parse_fdinfo_drm(files.read("/proc/%s/fdinfo/%s" % (pid, fd)))
            cid = info.get("drm-client-id")
            if not cid or cid in seen or "drm-total-memory" not in info:
                continue
            seen.add(cid)
            comm = (files.read("/proc/%s/comm" % pid) or b"?").decode("utf-8", "replace").strip()
            out.write("pid %-7s %-16s client %-4s total %8s KiB resident %8s KiB purgeable %6s KiB active %8s KiB "
                      "gpu time %s ns\n" % (pid, comm, cid, info.get("drm-total-memory"),
                                            info.get("drm-resident-memory", "?"),
                                            info.get("drm-purgeable-memory", "0"),
                                            info.get("drm-active-memory", "0"), info.get("drm-engine-gpu", "?")))
    if comp:
        section("compositor pid %d threads: tid comm state schedstat(run_ns wait_ns slices)" % comp["pid"])
        for tid in sorted(files.listdir("/proc/%d/task" % comp["pid"]), key=to_int):
            st = parse_pid_stat(files.read("/proc/%d/task/%s/stat" % (comp["pid"], tid)))
            ss = (files.read("/proc/%d/task/%s/schedstat" % (comp["pid"], tid)) or b"").decode().strip()
            if st:
                out.write("%-7s %-18s %s %s\n" % (tid, st[0], st[1], ss))
    section("/proc/interrupts")
    cat("/proc/interrupts")
    return out.getvalue()


def run_cmd(out, cmd, root, limit=300000, timeout=20):
    import subprocess
    if root not in ("/", ""):
        out.write("(not run under a test root: %s)\n" % " ".join(cmd))
        return
    try:
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout,
                           env={"LC_ALL": "C.UTF-8", "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "SYSTEMD_COLORS": "0"})
        out.write(r.stdout.decode("utf-8", "replace")[-limit:])
    except (OSError, subprocess.SubprocessError) as e:
        out.write("(%s failed: %s)\n" % (cmd[0], e))


def journal_dump(t_from, t_to, root="/"):
    """The journal for the incident window; taken at close, when journald has caught up."""
    out = io.StringIO()
    since = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t_from))
    until = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t_to))
    out.write("\n===== kernel log %s .. %s =====\n" % (since, until))
    run_cmd(out, ["journalctl", "-k", "-q", "--no-pager", "-o", "short-iso-precise", "--since", since,
                  "--until", until], root)
    out.write("\n===== journal, priority warning and worse, %s .. %s =====\n" % (since, until))
    run_cmd(out, ["journalctl", "-q", "--no-pager", "-o", "short-iso-precise", "-p", "warning", "--since", since,
                  "--until", until], root)
    out.write("\n===== journal: compositor, browsers, oomd, journald, %s .. %s =====\n" % (since, until))
    run_cmd(out, ["journalctl", "-q", "--no-pager", "-o", "short-iso-precise", "--since", since, "--until", until,
                  "_COMM=gnome-shell", "+", "_COMM=gnome-session-b", "+", "_COMM=chrome", "+", "_COMM=firefox",
                  "+", "SYSLOG_IDENTIFIER=systemd-oomd", "+", "SYSLOG_IDENTIFIER=systemd-journald",
                  "+", "SYSLOG_IDENTIFIER=google-chrome.desktop", "+", "SYSLOG_IDENTIFIER=firefox_firefox.desktop"],
            root)
    return out.getvalue()


# --------------------------------------------------------------------------
# Reading back: status, list, report, at
# --------------------------------------------------------------------------

def read_samples_file(path):
    out = []
    try:
        with open(path, "rb") as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    except OSError:
        pass
    return out


def read_ring(ring_dir, t_from, t_to):
    out = []
    try:
        segs = sorted(x for x in os.listdir(ring_dir) if x.startswith("seg-"))
    except OSError:
        return out
    starts = []
    for x in segs:
        try:
            starts.append(time.mktime(time.strptime(x[4:19], "%Y%m%d-%H%M%S")))
        except ValueError:
            starts.append(0)
    for i, x in enumerate(segs):
        nxt = starts[i + 1] if i + 1 < len(starts) else float("inf")
        if nxt < t_from - 5 or starts[i] > t_to + 5:
            continue
        try:
            with gzip.open(os.path.join(ring_dir, x), "rb") as f:
                for line in f:
                    try:
                        s = json.loads(line)
                    except ValueError:
                        continue
                    if "hdr" in s:
                        continue
                    if t_from <= s.get("t", 0) <= t_to:
                        out.append(s)
        except (OSError, EOFError, zlib.error):
            continue
    out.sort(key=lambda s: s["t"])
    return out


def evidence_table(samples, limit=80):
    L = ["  time          late  memF  memS userF deskM  ioF  cpuS  avail  swpo  rflt alloc scanD scanK  busy run D  top cpu / kernel"]
    flagged = [i for i, s in enumerate(samples) if s.get("flags")]
    if flagged:
        lo = max(0, flagged[0] - 10)
        hi = min(len(samples), flagged[-1] + 6)
    else:
        lo, hi = 0, len(samples)
    rows = samples[lo:hi]
    if len(rows) > limit:
        # keep the flagged samples and their neighbours
        keep = set()
        for i, s in enumerate(rows):
            if s.get("flags"):
                keep.update((i - 1, i, i + 1))
        rows = [s for i, s in enumerate(rows) if i in keep][:limit]
    for s in rows:
        psi = s.get("psi", {})
        vm = s.get("vm", {})
        comp = s.get("comp", {})
        mem = s.get("mem", {})
        busy = s.get("cpu", {}).get("busy") or [0]
        top = s.get("top_cpu", [])
        topt = ("%s %d%%" % (top[0][1], top[0][2])) if top else ""
        k = s.get("kmsg")
        if k:
            topt += "  | " + k[0][2][:60]
        L.append("  %s %5.0f %5.0f %5.0f %5.0f %5.0f %4.0f %5.0f %6s %5.0f %5.0f %5d %5d %5d %4d %3s %1s  %s%s" % (
            time.strftime("%H:%M:%S", time.localtime(s["t"])) + (".%01d" % int((s["t"] % 1) * 10)),
            s.get("late", 0), psi.get("memory", [0, 0])[1], psi.get("memory", [0, 0])[0],
            s.get("cg", {}).get("user", [0, 0])[1], comp.get("mem", [0, 0])[0], psi.get("io", [0, 0])[1],
            psi.get("cpu", [0, 0])[0], fmt_mb(mem.get("avail")), mb(vm.get("pswpout", 0)),
            mb(vm.get("rf_file", 0) + vm.get("rf_anon", 0)), vm.get("allocstall", 0), vm.get("scan_d", 0),
            vm.get("scan_k", 0), round(sum(busy) / len(busy)), s.get("run", ""), len(s.get("dproc", [])),
            topt, ("  <" + ",".join(s["flags"]) + ">") if s.get("flags") else ""))
    L.append("  (late ms; memF/memS/userF = PSI memory full/some, user.slice full, ms per sample; deskM = compositor "
             "cgroup memory some; ioF, cpuS likewise; swpo, rflt = MB swapped out / refaulted; alloc = direct reclaim "
             "entries; scanD/scanK = pages scanned direct/kswapd; busy = average cpu %; D = processes in D)")
    return L


def report_text(samples, cfg, title, n_pre=None, dump_path=None):
    core, before = core_window(samples, n_pre)
    a = aggregate(core)
    L = verdict(a, before, cfg, title=title)
    L.append("")
    L.append("Evidence (samples around the flagged ones):")
    L.extend(evidence_table(samples))
    probes = a["probes"]
    if probes:
        L.append("")
        L.append("Blocked threads and compositor stacks seen during it (time pid tid comm wchan stack):")
        for p in probes[:60]:
            L.append("  %s %s %s %s %s %s" % (ts(p[0])[11:], p[1], p[2], p[3], p[4], p[5][:400] if len(p) > 5 else ""))
    tops = [s for s in core if s.get("top_rss")]
    if tops:
        L.append("")
        L.append("Largest processes at the worst moment: " + ", ".join(
            "%s %d (%s)" % (c, p, fmt_mb(m)) for p, c, m in tops[len(tops) // 2]["top_rss"]))
    if a["kmsg"]:
        L.append("")
        L.append("Kernel messages captured live (seconds since boot, class, text):")
        for k in a["kmsg"][:40]:
            L.append("  [%10.3f] %-8s %s" % (k[0], k[1] or "-", k[2]))
    if dump_path and os.path.exists(dump_path):
        L.append("")
        L.append("One-shot dump taken after it cleared: %s" % dump_path)
    return "\n".join(L)


def cmd_status(data_dir, run_dir):
    path = os.path.join(run_dir, "status.json")
    try:
        with open(path) as f:
            st = json.load(f)
    except (OSError, ValueError):
        print("stall-watch: no status in %s: the service is not running (systemctl status %s)" % (path, SERVICE))
        return 1
    alive = os.path.exists("/proc/%d" % st["pid"])
    age = time.time() - st.get("updated", 0)
    pol = ""
    try:
        with open("/proc/%d/stat" % st["pid"], "rb") as f:
            p = parse_pid_stat(f.read())
        pol = {0: "SCHED_OTHER", 1: "SCHED_FIFO", 2: "SCHED_RR"}.get(p[5], str(p[5])) + (" %d" % p[6] if p[6] else "")
    except (OSError, TypeError, IndexError):
        pass
    print("stall-watch %s: %s (pid %d%s), status written %.0f s ago" % (
        st.get("version"), "running" if alive and age < 30 else "NOT RUNNING or stuck", st["pid"],
        ", " + pol if pol else "", age))
    up = st.get("uptime_s", 0)
    print("  running since %s (%d h %02d min); %d samples every %d ms, %d intervals missed; %d PSI trigger "
          "wakeups (%d not confirmed by the totals)"
          % (ts(st["started"]), up // 3600, up % 3600 // 60, st["samples"], st["interval_ms"], st["missed"],
             st["trigger_wakeups"], st.get("trigger_unconfirmed", 0)))
    print("  cost: %.2f %% of one CPU (%.1f s CPU); work per sample p50 %.2f ms, p99 %.2f ms; memory locked %d MB, "
          "resident %d MB (mlock %s)" % (st["cpu_pct"], st["cpu_s"], st["work_ms"]["p50"], st["work_ms"]["p99"],
                                          st["vmlck_kb"] // 1024, st["rss_kb"] // 1024, st["mlock"]))
    print("  wake-up lateness: p50 %.1f ms, p99 %.1f ms, max %.1f ms" % (
        st["late_ms"]["p50"], st["late_ms"]["p99"], st["late_ms"]["max"]))
    print("  PSI triggers: %s; kernel log: %s; compositor pid: %s" % (
        ", ".join("%s %s" % kv for kv in st["triggers"].items()) or "none", st["kmsg"], st.get("compositor")))
    ring = os.path.join(data_dir, "ring")
    try:
        segs = sorted(x for x in os.listdir(ring) if x.startswith("seg-"))
    except OSError:
        segs = []
    size = sum(os.path.getsize(os.path.join(ring, x)) for x in segs) if segs else 0
    if segs:
        first = time.mktime(time.strptime(segs[0][4:19], "%Y%m%d-%H%M%S"))
        span = st.get("updated", time.time()) - first
        print("  ring: %s -> now (%.1f h), %d files, %.1f MB on disk" % (ts(first), span / 3600.0, len(segs),
                                                                         size / 1048576.0))
    else:
        print("  ring: nothing on disk yet (first write after %d s)" % 60)
    inc = os.path.join(data_dir, "incidents")
    try:
        names = sorted(x for x in os.listdir(inc) if not x.endswith(".partial"))
    except OSError:
        names = []
    print("  incidents: %d saved on disk (%d this run, %d minor episodes not saved)%s; last: %s"
          % (len(names), st["incidents"], st["minor_episodes"], ", one open now" if st["incident_open"] else "",
             names[-1] if names else "none"))
    if st.get("errors"):
        print("  errors: %d, last: %s" % (st["errors"], st.get("last_error")))
    return 0 if alive and age < 30 else 1


def load_incident(data_dir, iid):
    inc = os.path.join(data_dir, "incidents")
    try:
        names = sorted(x for x in os.listdir(inc) if not x.endswith(".partial"))
    except OSError:
        names = []
    if not names:
        return None, None
    if iid in (None, "latest", "last"):
        name = names[-1]
    else:
        m = [x for x in names if x.startswith(iid) or iid in x]
        if not m:
            return None, None
        name = m[-1]
    return name, os.path.join(inc, name)


def cmd_list(data_dir):
    inc = os.path.join(data_dir, "incidents")
    try:
        names = sorted(x for x in os.listdir(inc) if not x.endswith(".partial"))
    except OSError:
        names = []
    if not names:
        print("no incidents recorded in %s" % inc)
        return 0
    for name in names:
        try:
            with open(os.path.join(inc, name, "summary.json")) as f:
                s = json.load(f)
            print("%-28s %4.0f s  %s" % (name, s["end"] - s["start"], s["headline"]))
        except (OSError, ValueError, KeyError):
            print("%-28s (summary unreadable)" % name)
    return 0


def cmd_report(data_dir, iid, cfg, raw=False):
    name, path = load_incident(data_dir, iid)
    if not path:
        print("no incident %s in %s/incidents (stall-watch list)" % (iid or "", data_dir))
        return 1
    try:
        with open(os.path.join(path, "summary.json")) as f:
            summary = json.load(f)
    except (OSError, ValueError):
        summary = {}
    samples = read_samples_file(os.path.join(path, "samples.jsonl"))
    if raw:
        sys.stdout.write(json.dumps(summary, indent=1) + "\n")
        for s in samples:
            sys.stdout.write(json.dumps(s, separators=(",", ":")) + "\n")
        return 0
    if not samples:
        print("\n".join(summary.get("verdict", ["(no samples kept for %s)" % name])))
        return 0
    print(report_text(samples, cfg, "Incident %s (boot %s, kernel %s)" % (name, (summary.get("boot") or "?")[:8],
                                                                          summary.get("kernel", "?")),
                      n_pre=summary.get("pre_samples"), dump_path=os.path.join(path, "dump.txt")))
    return 0


def parse_when(text):
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%H:%M:%S", "%H:%M"):
        try:
            t = time.strptime(text, fmt)
        except ValueError:
            continue
        if fmt.startswith("%H"):
            now = time.localtime()
            return time.mktime((now.tm_year, now.tm_mon, now.tm_mday, t.tm_hour, t.tm_min, t.tm_sec, 0, 0, -1))
        return time.mktime(t)
    try:
        return float(text)
    except ValueError:
        return None


def cmd_at(data_dir, when, window, cfg):
    t = parse_when(when)
    if t is None:
        print("cannot read the time %r; use 'YYYY-mm-dd HH:MM[:SS]' or 'HH:MM[:SS]'" % when)
        return 2
    samples = read_ring(os.path.join(data_dir, "ring"), t - window, t + window)
    if not samples:
        print("the ring has no samples between %s and %s" % (ts(t - window), ts(t + window)))
        return 1
    det = Detector(cfg)
    for s in samples:
        if "flags" not in s:
            fl = det.flags(s, [k[1] for k in s.get("kmsg", []) if k[1]])
            if fl:
                s["flags"] = fl
    print(report_text(samples, cfg, "Ring around %s (+-%d s)" % (ts(t), window)))
    return 0


def cmd_mark(run_dir):
    path = os.path.join(run_dir, "mark")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
        os.write(fd, b"mark\n")
        os.close(fd)
    except OSError as e:
        print("cannot write %s: %s (is the service running, and are you in group adm?)" % (path, e.strerror))
        return 1
    print("marked: the last %d s and the next few are being saved; stall-watch list shows it in a few seconds"
          % load_config()["pre_s"])
    return 0


def main(argv):
    import argparse
    ap = argparse.ArgumentParser(prog="stall-watch", description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", default="/", help=argparse.SUPPRESS)
    ap.add_argument("--data", default=DATA_DIR, help=argparse.SUPPRESS)
    ap.add_argument("--run-dir", default=RUN_DIR, help=argparse.SUPPRESS)
    ap.add_argument("--config", default=CONF_FILE, help=argparse.SUPPRESS)
    sub = ap.add_subparsers(dest="cmd")
    r = sub.add_parser("run", help="the sampler (systemd runs it)")
    r.add_argument("--samples", type=int, default=None, help=argparse.SUPPRESS)
    r.add_argument("--seconds", type=float, default=None, help=argparse.SUPPRESS)
    r.add_argument("--interval-ms", type=int, default=None, help=argparse.SUPPRESS)
    r.add_argument("--no-mlock", action="store_true", help=argparse.SUPPRESS)
    sub.add_parser("status", help="is it alive, cost, ring span")
    sub.add_parser("list", help="recorded incidents")
    rp = sub.add_parser("report", help="verdict and evidence for one incident")
    rp.add_argument("id", nargs="?", default="latest")
    rp.add_argument("--raw", action="store_true", help="summary and samples as JSON")
    at = sub.add_parser("at", help="verdict for any time the ring covers")
    at.add_argument("when")
    at.add_argument("window", nargs="?", type=int, default=60)
    sub.add_parser("mark", help="save the last two minutes now")
    sub.add_parser("check", help="read every source once and print one sample (no writes)")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    if args.cmd == "run":
        if args.interval_ms:
            cfg["interval_ms"] = args.interval_ms
        return run_daemon(cfg, args)
    if args.cmd == "status":
        return cmd_status(args.data, args.run_dir)
    if args.cmd == "list":
        return cmd_list(args.data)
    if args.cmd == "report":
        return cmd_report(args.data, args.id, cfg, raw=args.raw)
    if args.cmd == "at":
        return cmd_at(args.data, args.when, args.window, cfg)
    if args.cmd == "mark":
        return cmd_mark(args.run_dir)
    if args.cmd == "check":
        files = Files(args.root)
        smp = Sampler(cfg, files)
        clk = Clock()
        smp.sample(clk.mono(), clk.boot(), clk.wall(), 0.0, "check", True)
        time.sleep(1.0)
        s = smp.sample(clk.mono(), clk.boot(), clk.wall(), 0.0, "check", True)
        print(json.dumps(s, indent=1))
        return 0
    ap.print_help()
    return 2


def run_daemon(cfg, args):
    import gc
    group = None
    if os.geteuid() == 0:
        try:
            import grp
            group = grp.getgrnam("adm").gr_gid
        except (ImportError, KeyError):
            group = None
    d = Daemon(cfg, root=args.root, data_dir=args.data, run_dir=args.run_dir, group=group)
    sys.setswitchinterval(0.002)
    if cfg.get("mlock") and not args.no_mlock:
        # every import the loop needs is done; from here no page of ours can be reclaimed
        import subprocess  # noqa: F401 - imported before the lock for the dump
        d.lock_state = try_mlockall()
    gc.collect()
    if hasattr(gc, "freeze"):
        gc.freeze()

    def on_term(signum, frame):
        d.stop = True
    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    pol = ""
    try:
        pol = {0: "SCHED_OTHER", 1: "SCHED_FIFO", 2: "SCHED_RR"}.get(os.sched_getscheduler(0), "?")
    except (AttributeError, OSError):
        pass
    log("version %s started: every %d ms, %s, mlock %s, triggers %s, kernel log %s, data in %s"
        % (VERSION, cfg["interval_ms"], pol, d.lock_state,
           ", ".join("%s %s" % kv for kv in d.trigger_state.items()) or "none",
           getattr(d, "kmsg_state", "off"), args.data))
    d.run(max_samples=args.samples, max_seconds=args.seconds)
    log("stopped after %d samples" % d.stats["samples"])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
