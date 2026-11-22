"""A fake /proc and /sys tree shaped like blastoise's (SM-X910), for the tests.

Counters are kept in a dict and written out by FakeRoot.write(); tests bump
them between samples to simulate stalls."""
import os
import pathlib

INTERRUPTS_HEAD = "           CPU0       CPU1       CPU2       CPU3       CPU4       CPU5       CPU6       CPU7       \n"
# Lines copied in shape from blastoise's /proc/interrupts (2026-11-22).
INTERRUPT_LINES = [
    ("11", "GICv3                      27 Level     arch_timer"),
    ("193", "GICv3                     493 Level     qcom_geni_serial_uart1"),
    ("201", "msmgpio                    25 Level     goodix-berlin"),
    ("204", "GICv3                     332 Level     gpu-irq"),
    ("208", "msm_mdss                    0 Edge      msm-kms"),
    ("279", "GICv3                     297 Level     ufshcd"),
    ("280", "msm_mdss                    4 Edge      dsi_isr"),
    ("283", "GICv3                     165 Level     xhci-hcd:usb1"),
    ("130", "GICv3                     177 Level     dcvsh-irq-0"),
    ("302", "                       196608 Edge      glink-smem"),
    ("166", "msmgpio                    14 Level     cs35l45 IRQ1 Controller, cs35l45 IRQ1 Controller"),
    ("IPI0", "Rescheduling interrupts"),
    ("IPI1", "Function call interrupts"),
]

STAT_FIELDS = 52


def pid_stat(pid, comm, state="S", ticks=0, rss_pages=1000, threads=1, policy=0, rtprio=0):
    f = ["0"] * STAT_FIELDS
    f[0] = state
    f[1] = "1"
    f[11] = str(ticks)       # utime
    f[12] = "0"              # stime
    f[17] = str(threads)     # num_threads
    f[21] = str(rss_pages)   # rss
    f[37] = str(rtprio)      # rt_priority
    f[38] = str(policy)      # policy
    return "%d (%s) %s\n" % (pid, comm, " ".join(f))


class FakeRoot:
    def __init__(self, base, board=b"qcom,kalama-mtp\0qcom,kalama\0samsung,gts9uwifi\0qcom,sm8550\0"):
        self.root = pathlib.Path(base)
        self.c = {
            "psi": {"cpu": [1000, 0], "memory": [5000, 4000], "io": [3000, 2000], "irq": [0, 9000]},
            "cg": {"user": [1000, 900], "system": [500, 400], "comp": [100, 90], "app": [800, 700], "bt": [10, 10]},
            "comp_io": [0, 0], "comp_cpu": [0, 0],
            "vm": {"allocstall_normal": 10, "allocstall_movable": 5, "pgscan_direct": 100, "pgsteal_direct": 50,
                   "pgscan_kswapd": 1000, "pgsteal_kswapd": 800, "pswpin": 1, "pswpout": 2,
                   "workingset_refault_file": 10, "workingset_refault_anon": 1, "pgmajfault": 3,
                   "compact_stall": 0, "compact_fail": 0, "compact_success": 0, "pgmigrate_fail": 0,
                   "oom_kill": 0, "unevictable_pgs_culled": 0, "unevictable_pgs_rescued": 0,
                   "thp_fault_alloc": 0, "thp_fault_fallback": 0, "slabs_scanned": 0, "nr_free_pages": 1000},
            "mem": {"MemTotal": 14903260, "MemFree": 578604, "MemAvailable": 2226712, "Cached": 3644180,
                    "SwapCached": 124, "Active(file)": 1048240, "Inactive(file)": 704300, "AnonPages": 9742444,
                    "Shmem": 1917900, "Unevictable": 902332, "Mlocked": 16, "Dirty": 5116, "Writeback": 0,
                    "SReclaimable": 145268, "SUnreclaim": 206712, "AnonHugePages": 1294336,
                    "SwapTotal": 25165816, "SwapFree": 24766004, "PageTables": 134268, "Committed_AS": 47524580},
            "cpu": [[100, 900, 0, 0]] * 8,   # busy, idle, iowait, irq per cpu (jiffies)
            "procs_running": 2, "procs_blocked": 0, "ctxt": 1000,
            "disk": {"sda": [100, 2000, 50, 200, 4000, 300, 0, 400], "zram0": [10, 80, 1, 20, 160, 2, 0, 5]},
            "irq": {k: 1000 for k, _ in INTERRUPT_LINES},
            "procs": {1: ("systemd", "S", 100, 3000), 84: ("kswapd0", "S", 50, 0),
                      2924: ("gnome-shell", "S", 5000, 90000), 4730: ("chrome", "S", 9000, 200000)},
            "comp_sched": {"main": [10**9, 10**8, 100], "kms": [10**8, 10**7, 50]},
            "comp_state": {"main": "S", "kms": "S"},
            "gpu_ns": 10**9,
            "freq": [1344000, 2054400, 3360000], "maxfreq": [2016000, 2803200, 3360000],
            "temp": 49900, "cool": 0, "dpms": "On",
            "suspends": 1, "swaps_used_kb": 399812,
            "loadavg": "3.06 2.75 2.39 1/1786 96081",
        }
        self.write()

    def p(self, rel):
        path = self.root / rel.lstrip("/")
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def put(self, rel, text):
        # in place, like a proc/sysfs file: the sampler keeps descriptors open
        # and re-reads them with pread at offset 0
        data = text.encode() if isinstance(text, str) else text
        path = self.p(rel)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o644)
        try:
            os.pwrite(fd, data, 0)
            os.ftruncate(fd, len(data))
        finally:
            os.close(fd)

    def write(self):
        c = self.c
        for res, (some, full) in c["psi"].items():
            if res == "irq":
                self.put("/proc/pressure/irq", "full avg10=0.00 avg60=0.00 avg300=0.00 total=%d\n" % full)
            else:
                self.put("/proc/pressure/" + res,
                         "some avg10=0.00 avg60=0.00 avg300=0.00 total=%d\n"
                         "full avg10=0.00 avg60=0.00 avg300=0.00 total=%d\n" % (some, full))
        cg = "/sys/fs/cgroup"
        comp_cg = cg + "/user.slice/user-1000.slice/user@1000.service/session.slice/org.gnome.Shell@wayland.service"
        app_cg = cg + "/user.slice/user-1000.slice/user@1000.service/app.slice"
        psi_txt = "some avg10=0.00 avg60=0.00 avg300=0.00 total=%d\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=%d\n"
        self.put(cg + "/user.slice/memory.pressure", psi_txt % tuple(c["cg"]["user"]))
        self.put(cg + "/system.slice/memory.pressure", psi_txt % tuple(c["cg"]["system"]))
        self.put(comp_cg + "/memory.pressure", psi_txt % tuple(c["cg"]["comp"]))
        self.put(comp_cg + "/io.pressure", psi_txt % tuple(c["comp_io"]))
        self.put(comp_cg + "/cpu.pressure", psi_txt % tuple(c["comp_cpu"]))
        self.put(comp_cg + "/memory.current", "596697088\n")
        self.put(app_cg + "/memory.pressure", psi_txt % tuple(c["cg"]["app"]))
        self.put(cg + "/system.slice/bluetooth.service/memory.pressure", psi_txt % tuple(c["cg"]["bt"]))
        self.put(app_cg + "/memory.current", "12640669696\n")
        self.put("/proc/meminfo", "".join("%-16s%8d kB\n" % (k + ":", v) for k, v in c["mem"].items()))
        self.put("/proc/vmstat", "".join("%s %d\n" % kv for kv in c["vm"].items()))
        tot = [sum(x[i] for x in c["cpu"]) for i in range(4)]
        lines = ["cpu  %d 0 0 %d %d %d 0 0 0 0" % (tot[0], tot[1], tot[2], tot[3])]
        for i, (b, idle, iow, irq) in enumerate(c["cpu"]):
            lines.append("cpu%d %d 0 0 %d %d %d 0 0 0 0" % (i, b, idle, iow, irq))
        lines += ["intr 1 2 3", "ctxt %d" % c["ctxt"], "btime 1795329592", "processes 96081",
                  "procs_running %d" % c["procs_running"], "procs_blocked %d" % c["procs_blocked"],
                  "softirq 1 2 3"]
        self.put("/proc/stat", "\n".join(lines) + "\n")
        ds = []
        for i, (name, v) in enumerate(sorted(c["disk"].items())):
            ds.append("%4d %7d %s %d 0 %d %d %d 0 %d %d %d %d 0 0 0 0 0 0" % (
                8 if name.startswith("sd") else 253, 0, name, v[0], v[1] * 2, v[2], v[3], v[4] * 2, v[5], v[6], v[7]))
        ds.append("   8       1 sda1 36 0 288 9 0 0 0 0 0 8 9 0 0 0 0 0 0")
        self.put("/proc/diskstats", "\n".join(ds) + "\n")
        irq = [INTERRUPTS_HEAD]
        for label, name in INTERRUPT_LINES:
            n = c["irq"][label]
            irq.append("%4s: %s  %s\n" % (label, " ".join("%10d" % (n if i == 0 else 0) for i in range(8)), name))
        self.put("/proc/interrupts", "".join(irq))
        self.put("/proc/loadavg", c["loadavg"] + "\n")
        self.put("/proc/swaps", "Filename\tType\tSize\tUsed\tPriority\n"
                                "/dev/zram0                              partition\t8388604\t%d\t100\n"
                                "/swapfile                               file\t16777212\t0\t10\n" % c["swaps_used_kb"])
        self.put("/proc/sys/kernel/random/boot_id", "66a30813-cddb-4441-94c6-a7723deab5f8\n")
        self.put("/proc/device-tree/compatible", b"qcom,kalama-mtp\0qcom,kalama\0samsung,gts9uwifi\0qcom,sm8550\0")
        self.put("/proc/self/schedstat", "123456789 1234 10\n")
        self.put("/proc/self/status", "Name:\tpython3\nVmLck:\t  25000 kB\nVmRSS:\t  27000 kB\n")
        for pid, (comm, state, ticks, rss) in c["procs"].items():
            self.put("/proc/%d/stat" % pid, pid_stat(pid, comm, state, ticks, rss))
            self.put("/proc/%d/comm" % pid, comm + "\n")
            self.put("/proc/%d/task/%d/stat" % (pid, pid), pid_stat(pid, comm, state, ticks, rss))
        gs = 2924
        self.put("/proc/%d/cgroup" % gs, "0::" + comp_cg[len(cg):] + "\n")
        main, kms = c["comp_sched"]["main"], c["comp_sched"]["kms"]
        self.put("/proc/%d/task/%d/stat" % (gs, gs), pid_stat(gs, "gnome-shell", c["comp_state"]["main"], 5000, 90000))
        self.put("/proc/%d/task/%d/schedstat" % (gs, gs), "%d %d %d\n" % tuple(main))
        self.put("/proc/%d/task/2959/stat" % gs, pid_stat(2959, "KMS thread", c["comp_state"]["kms"]))
        self.put("/proc/%d/task/2959/comm" % gs, "KMS thread\n")
        self.put("/proc/%d/task/2959/schedstat" % gs, "%d %d %d\n" % tuple(kms))
        self.put("/proc/%d/task/%d/comm" % (gs, gs), "gnome-shell\n")
        fd = self.p("/proc/%d/fd/12" % gs)
        if not fd.is_symlink():
            fd.symlink_to("/dev/dri/renderD128")
        self.put("/proc/%d/fdinfo/12" % gs,
                 "pos:\t0\nflags:\t02100002\ndrm-driver:\tmsm\ndrm-client-id:\t16\ndrm-engine-gpu:\t%d ns\n"
                 "drm-total-memory:\t835480 KiB\ndrm-resident-memory:\t558116 KiB\n" % c["gpu_ns"])
        cpufreq = "/sys/devices/system/cpu/cpufreq"
        for i, (cur, mx) in enumerate(zip(c["freq"], c["maxfreq"])):
            pol = "%s/policy%d" % (cpufreq, [0, 3, 7][i])
            self.put(pol + "/scaling_cur_freq", "%d\n" % cur)
            self.put(pol + "/scaling_max_freq", "%d\n" % mx)
            self.put(pol + "/cpuinfo_max_freq", "%d\n" % [2016000, 2803200, 3360000][i])
        self.put("/sys/class/thermal/thermal_zone1/type", "cpuss0-thermal\n")
        self.put("/sys/class/thermal/thermal_zone1/temp", "%d\n" % c["temp"])
        self.put("/sys/class/thermal/thermal_zone13/type", "cpu7-top-thermal\n")
        self.put("/sys/class/thermal/thermal_zone13/temp", "%d\n" % (c["temp"] + 800))
        self.put("/sys/class/thermal/cooling_device2/type", "cpufreq-cpu7\n")
        self.put("/sys/class/thermal/cooling_device2/cur_state", "%d\n" % c["cool"])
        self.put("/sys/class/thermal/cooling_device4/type", "ath12k_thermal\n")
        self.put("/sys/class/thermal/cooling_device4/cur_state", "0\n")
        self.put("/sys/class/devfreq/3d00000.gpu/target_freq", "719000000\n")
        self.put("/sys/class/devfreq/1d84000.ufshc/cur_freq", "75000000\n")
        for d in ("sda", "zram0", "loop0"):
            self.p("/sys/block/%s/.keep" % d)
        self.put("/sys/block/zram0/mm_stat", "345313280 74928446 84111360 0 85454848 740 0 189 189\n")
        self.put("/sys/class/drm/card1-DSI-1/status", "connected\n")
        self.put("/sys/class/drm/card1-DSI-1/dpms", c["dpms"] + "\n")
        self.put("/sys/class/drm/card1-DP-1/status", "disconnected\n")
        self.put("/sys/power/suspend_stats/success", "%d\n" % c["suspends"])
        self.put("/sys/power/suspend_stats/fail", "0\n")
