#!/usr/bin/env python3
"""Sustained prime-core (3.36 GHz) load test with temperature logging.

Runs as the desktop user; no root, nothing installed, nothing written outside
~/powerthermal-logs.  The load is `sha256sum /dev/zero` pinned with taskset:
hashing with the ARMv8 SHA2 instructions keeps the core fully busy and hot,
and reading /dev/zero touches no storage.

Phases (each logged once a second to CSV):
  wait      until every cpu7 zone is at or below --start-max-c (default 45 C),
            at most --wait-s (default 300 s); the test refuses to start hot
  baseline  --baseline-s idle (default 60)
  prime     --prime-s one worker on cpu7 (default 600)
  big       --big-s one worker on each of cpu3-7 (default 0 = skipped)
  cooldown  --cooldown-s idle (default 180)

Abort (the workers are killed at once, the cool-down is still logged): any CPU
zone >= --abort-cpu-c (default 100 C, 10 C under the 110 C critical trip),
battery >= --abort-battery-c (default 45 C), the AP thermistor (if the new DT
exposes it) >= --abort-ap-c (default 60 C), Ctrl-C, or a hangup (terminal
closed, SSH dropped).

The workers can never outlive this script: they stay in its process group, so
the terminal's Ctrl-C, Ctrl-Z and hangup reach them too, and each one is
SIGKILLed by the kernel the moment this script dies for any reason
(PR_SET_PDEATHSIG), even a kill -9.  An unwatched load on the prime core is
exactly what this test must not leave behind.

Every sample has: cpu7 requested (scaling_cur_freq) and delivered
(cpuinfo_avg_freq) clocks, all thermal zones, the cpufreq/GPU cooling-device
states, the LMh (dcvsh) interrupt counts, the battery, and the xo/ap
thermistors when iio:device0 has them.  A summary is printed and saved.
"""

import argparse
import ctypes
import csv
import datetime as dt
import glob
import os
import signal
import subprocess
import sys
import time

CPU = "/sys/devices/system/cpu/cpufreq"
BAT = "/sys/class/power_supply/sm5714-battery"
IIO = "/sys/bus/iio/devices/iio:device0"
BOOST_KHZ = 3360000


def read(path, default=""):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def read_int(path):
    v = read(path)
    try:
        return int(v)
    except ValueError:
        return None


def zones():
    out = {}
    for z in sorted(glob.glob("/sys/class/thermal/thermal_zone*")):
        out[read(z + "/type")] = z + "/temp"
    return out


def cdevs():
    out = {}
    for c in sorted(glob.glob("/sys/class/thermal/cooling_device*")):
        t = read(c + "/type")
        if t.startswith(("cpufreq-", "devfreq-")):
            out[t] = c + "/cur_state"
    return out


def dcvsh_counts():
    counts = {}
    try:
        with open("/proc/interrupts") as f:
            for line in f:
                if "dcvsh" not in line:
                    continue
                parts = line.split()
                name = parts[-1]
                n = sum(int(p) for p in parts[1:] if p.isdigit())
                counts[name] = n
    except OSError:
        pass
    return counts


def iio_temps():
    out = {}
    for lab in glob.glob(IIO + "/in_temp*_label"):
        label = read(lab)
        val = read_int(lab[: -len("_label")] + "_input")
        if val is not None:
            out[label] = val
    return out


def time_in_state(policy):
    out = {}
    for line in read(f"{CPU}/{policy}/stats/time_in_state").splitlines():
        f, t = line.split()
        out[int(f)] = int(t)
    return out


PR_SET_PDEATHSIG = 1
_libc = ctypes.CDLL(None, use_errno=True)


def _die_with(parent):
    def setup():
        # Runs in the child between fork and exec.  taskset then execs
        # sha256sum in the same process, and the death signal survives that
        # exec, so the worker itself is what the kernel kills when this
        # script goes away.
        if _libc.prctl(PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0) != 0:
            os._exit(127)
        # The parent may already have died between fork and prctl.
        if os.getppid() != parent:
            os._exit(127)
    return setup


def say(*args):
    # A hung-up terminal turns print() into EIO; the log must still be written.
    try:
        print(*args, flush=True)
    except OSError:
        pass


class Load:
    def __init__(self):
        self.procs = []

    def start(self, cpus):
        for c in cpus:
            # Same process group as this script on purpose: Ctrl-C, Ctrl-Z
            # and a terminal hangup stop the load as well as the guard.
            self.procs.append(subprocess.Popen(
                ["taskset", "-c", str(c), "sha256sum", "/dev/zero"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                preexec_fn=_die_with(os.getpid())))

    def stop(self):
        for p in self.procs:
            try:
                p.kill()
            except ProcessLookupError:
                pass
        for p in self.procs:
            p.wait()
        self.procs = []


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start-max-c", type=float, default=45.0)
    ap.add_argument("--wait-s", type=int, default=300)
    ap.add_argument("--baseline-s", type=int, default=60)
    ap.add_argument("--prime-s", type=int, default=600)
    ap.add_argument("--big-s", type=int, default=0)
    ap.add_argument("--cooldown-s", type=int, default=180)
    ap.add_argument("--abort-cpu-c", type=float, default=100.0)
    ap.add_argument("--abort-battery-c", type=float, default=45.0)
    ap.add_argument("--abort-ap-c", type=float, default=60.0)
    ap.add_argument("--outdir", default=os.path.expanduser("~/powerthermal-logs"))
    a = ap.parse_args()

    if os.geteuid() == 0:
        sys.exit("run this as the desktop user, not root")
    if read(f"{CPU}/policy7/scaling_governor") != "schedutil":
        say("note: policy7 governor is", read(f"{CPU}/policy7/scaling_governor"))
    boost = read(f"{CPU}/boost")
    maxf = read_int(f"{CPU}/policy7/scaling_max_freq")
    say(f"boost={boost} policy7 scaling_max_freq={maxf}")
    if maxf != BOOST_KHZ:
        say("WARNING: policy7 cannot reach 3.36 GHz right now (boost off or "
              "capped); the run still measures what it gets")

    os.makedirs(a.outdir, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    csv_path = os.path.join(a.outdir, f"loadtest-{stamp}.csv")
    sum_path = os.path.join(a.outdir, f"loadtest-{stamp}.summary.txt")

    zmap = zones()
    cmap = cdevs()
    cpu_zones = [t for t in zmap if t.startswith("cpu") and "thermal" in t]
    cpu7_zones = [t for t in cpu_zones if t.startswith("cpu7")]
    iio0 = iio_temps()
    fields = (["time", "t_s", "phase", "workers", "cpu7_req_khz", "cpu7_avg_khz",
               "bat_status", "bat_ua", "bat_cap", "bat_temp_dc"]
              + [f"iio:{k}" for k in sorted(iio0)]
              + [f"cdev:{k}" for k in cmap]
              + [f"irq:{k}" for k in sorted(dcvsh_counts())]
              + [f"tz:{k}" for k in zmap])

    load = Load()
    stop = {"why": None}

    hung_up = {"yes": False}

    def on_signal(signum, frame):
        if signum == signal.SIGHUP:
            # Nobody is watching any more: stop, skip the cool-down, write
            # the summary.
            hung_up["yes"] = True
            stop["why"] = "hangup (terminal closed)"
        else:
            stop["why"] = "interrupted"

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGHUP, on_signal)

    peaks = {}
    lmh0 = dcvsh_counts()
    tis0 = time_in_state("policy7")
    cdev_max = {k: 0 for k in cmap}
    t0 = time.monotonic()
    phase_stats = {}

    with open(csv_path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(fields)

        def sample(phase, workers):
            temps = {k: read_int(p) for k, p in zmap.items()}
            iio = iio_temps()
            cd = {k: read_int(p) for k, p in cmap.items()}
            irq = dcvsh_counts()
            req = read_int(f"{CPU}/policy7/scaling_cur_freq")
            avg = read_int(f"{CPU}/policy7/cpuinfo_avg_freq")
            row = [dt.datetime.now().isoformat(timespec="seconds"),
                   round(time.monotonic() - t0, 1), phase, workers, req, avg,
                   read(f"{BAT}/status"), read_int(f"{BAT}/current_now"),
                   read_int(f"{BAT}/capacity"), read_int(f"{BAT}/temp")]
            row += [iio.get(k) for k in sorted(iio0)]
            row += [cd.get(k) for k in cmap]
            row += [irq.get(k) for k in sorted(lmh0)]
            row += [temps.get(k) for k in zmap]
            w.writerow(row)
            fh.flush()
            for k, v in temps.items():
                if v is not None:
                    peaks[k] = max(peaks.get(k, v), v)
            for k, v in iio.items():
                peaks["iio:" + k] = max(peaks.get("iio:" + k, v), v)
            for k, v in cd.items():
                if v is not None:
                    cdev_max[k] = max(cdev_max[k], v)
            st = phase_stats.setdefault(phase, {"n": 0, "avg_sum": 0, "at_boost": 0,
                                                "avg_min": None})
            if avg:
                st["n"] += 1
                st["avg_sum"] += avg
                st["avg_min"] = avg if st["avg_min"] is None else min(st["avg_min"], avg)
                if avg >= BOOST_KHZ * 0.97:
                    st["at_boost"] += 1
            return temps, iio

        def guard(temps, iio):
            for k in cpu_zones:
                v = temps.get(k)
                if v is not None and v >= a.abort_cpu_c * 1000:
                    return f"{k} reached {v / 1000:.1f} C"
            bt = read_int(f"{BAT}/temp")
            if bt is not None and bt >= a.abort_battery_c * 10:
                return f"battery reached {bt / 10:.1f} C"
            apv = iio.get("ap-thermistor")
            if apv is not None and apv >= a.abort_ap_c * 1000:
                return f"ap-thermistor reached {apv / 1000:.1f} C"
            return None

        def run_phase(phase, seconds, cpus):
            if seconds <= 0 or stop["why"]:
                return
            say(f"{phase}: {seconds} s on cpus {cpus or '-'}")
            try:
                if cpus:
                    load.start(cpus)
                end = time.monotonic() + seconds
                while time.monotonic() < end and not stop["why"]:
                    temps, iio = sample(phase, len(cpus))
                    why = guard(temps, iio) if cpus else None
                    if why:
                        stop["why"] = "ABORT: " + why
                        break
                    time.sleep(1)
            finally:
                load.stop()

        # wait for a cool start
        deadline = time.monotonic() + a.wait_s
        while not stop["why"]:
            temps, _ = sample("wait", 0)
            hot = max((temps.get(k) or 0) for k in cpu7_zones) if cpu7_zones else 0
            if hot <= a.start_max_c * 1000:
                break
            if time.monotonic() > deadline:
                stop["why"] = f"cpu7 still {hot / 1000:.1f} C after {a.wait_s} s; not starting"
                break
            time.sleep(2)

        ran_load = not stop["why"]
        run_phase("baseline", a.baseline_s, [])
        run_phase("prime", a.prime_s, [7])
        run_phase("big", a.big_s, [3, 4, 5, 6, 7])
        aborted = stop["why"]
        if ran_load and not hung_up["yes"]:
            if aborted:
                say(f"{aborted}; load stopped, logging the cool-down "
                    "(Ctrl-C again to skip it)")
            stop["why"] = None
            run_phase("cooldown", a.cooldown_s, [])

    lmh1 = dcvsh_counts()
    tis1 = time_in_state("policy7")
    lines = [f"load test {stamp}  csv: {csv_path}",
             f"result: {aborted or 'completed'}",
             f"boost={boost} policy7 scaling_max_freq={maxf}"]
    for ph, st in phase_stats.items():
        if st["n"]:
            lines.append(f"{ph:9s} cpu7 delivered avg {st['avg_sum'] / st['n'] / 1e6:.2f} GHz, "
                         f"min {st['avg_min'] / 1e6:.2f} GHz, "
                         f"at >=97% of 3.36 GHz {100 * st['at_boost'] / st['n']:.0f}% of samples")
    dt_tis = {f: tis1.get(f, 0) - tis0.get(f, 0) for f in tis1}
    tot = sum(dt_tis.values()) or 1
    top = sorted(dt_tis.items(), key=lambda kv: -kv[1])[:4]
    lines.append("policy7 time in state over the run: " + ", ".join(
        f"{f / 1e6:.2f} GHz {100 * t / tot:.0f}%" for f, t in top))
    lines.append("LMh (dcvsh) interrupts during the run: " + ", ".join(
        f"{k} +{lmh1.get(k, 0) - lmh0.get(k, 0)}" for k in sorted(lmh0)))
    lines.append("cooling devices, highest state seen: " + ", ".join(
        f"{k}={v}" for k, v in cdev_max.items()))
    hot = sorted(((k, v) for k, v in peaks.items()
                  if k.startswith(("cpu", "gpuss", "iio:", "sm5714", "pm8550", "cpuss"))),
                 key=lambda kv: -kv[1])
    # Thermal zones and IIO both report millidegrees (the battery's zone too).
    lines.append("peak temperatures (C): " + ", ".join(
        f"{k} {v / 1000:.1f}" for k, v in hot[:16]))
    text = "\n".join(lines) + "\n"
    with open(sum_path, "w") as f:
        f.write(text)
    say(text)
    return 1 if aborted and aborted.startswith("ABORT") else 0


if __name__ == "__main__":
    sys.exit(main())
