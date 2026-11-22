"""Unit and integration tests for tools/inplace/stall-watch/stall_watch.py against a fake /proc and /sys."""
import contextlib
import gzip
import io
import json
import os
import pathlib
import random
import sys
import tempfile
import threading
import time
import unittest

HERE = pathlib.Path(__file__).resolve().parent
KIT = HERE.parent / "tools" / "inplace" / "stall-watch"
sys.dont_write_bytecode = True      # no __pycache__ inside the kit that goes to the tablet
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(HERE))

import stall_watch as sw  # noqa: E402
from stall_watch_fakeroot import FakeRoot  # noqa: E402


class FakeClock:
    def __init__(self, mono=1000.0, wall=1795383000.0):
        self.m = mono
        self.b = mono
        self.w = wall
        self.cpu = 0.0

    def advance(self, s, suspended=0.0):
        self.m += s
        self.b += s + suspended
        self.w += s + suspended

    def mono(self):
        return self.m

    def boot(self):
        return self.b

    def wall(self):
        return self.w

    def thread_time(self):
        self.cpu += 0.001
        return self.cpu


def cfg(**kw):
    c = dict(sw.DEFAULTS)
    c["journal_markers"] = 0
    c.update(kw)
    return c


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self.tmp.name)
        self.fake = FakeRoot(self.base / "root")
        self.data = self.base / "data"
        self.run = self.base / "run"

    def tearDown(self):
        self.tmp.cleanup()

    def daemon(self, clock=None, **kw):
        c = cfg(**kw)
        d = sw.Daemon(c, root=str(self.fake.root), data_dir=str(self.data), run_dir=str(self.run),
                      clock=clock or FakeClock(), arm_triggers=False, use_kmsg=False, use_fifo=False)
        return d

    def step(self, d, clock, late=0.0, secs=1.0, bump=None, suspended=0.0):
        """Advance one interval, apply counter changes, take one tick sample."""
        clock.advance(secs, suspended)
        if bump:
            bump(self.fake.c)
        self.fake.write()
        return d.take(clock.mono(), late, "tick")


# --------------------------------------------------------------------------
class ParserTests(unittest.TestCase):
    def test_psi(self):
        v = sw.parse_psi(b"some avg10=0.01 avg60=3.86 avg300=4.48 total=56827105\n"
                         b"full avg10=0.01 avg60=3.54 avg300=4.09 total=52028502\n")
        self.assertEqual(v, {"some": 56827105, "full": 52028502})
        self.assertEqual(sw.parse_psi(b"full avg10=1.00 avg60=1.00 avg300=1.00 total=675591199\n"),
                         {"full": 675591199})
        self.assertEqual(sw.parse_psi(None), {})
        self.assertEqual(sw.parse_psi(b"garbage\n"), {})

    def test_meminfo_and_vmstat(self):
        m = sw.parse_kv(b"MemTotal:       14903260 kB\nMemAvailable:    2226712 kB\nActive(file):    1048240 kB\n",
                        {"MemAvailable", "Active(file)"})
        self.assertEqual(m, {"MemAvailable": 2226712, "Active(file)": 1048240})
        v = sw.parse_kv(b"pgscan_direct 806122\nallocstall_normal 1356\nbroken\n")
        self.assertEqual(v["pgscan_direct"], 806122)
        self.assertEqual(v["allocstall_normal"], 1356)

    def test_proc_stat(self):
        s = sw.parse_proc_stat(b"cpu  10 1 2 100 5 3 4 0 0 0\ncpu0 10 1 2 100 5 3 4 0 0 0\n"
                               b"ctxt 501743389\nprocs_running 7\nprocs_blocked 2\n")
        self.assertEqual(s["cpus"]["cpu0"], (10 + 1 + 2 + 3 + 4, 105, 5, 7))
        self.assertEqual((s["ctxt"], s["running"], s["blocked"]), (501743389, 7, 2))

    def test_diskstats(self):
        line = (b"   8       0 sda 105533 33494 18032216 106144 506031 544938 40337424 1578171 0 335444 "
                b"1709704 0 0 0 0 60746 25389\n   8       1 sda1 36 0 288 9 0 0 0 0 0 8 9 0 0 0 0 0 0\n")
        d = sw.parse_diskstats(line, {"sda"})
        self.assertEqual(d, {"sda": (105533, 18032216, 106144, 506031, 40337424, 1578171, 0, 335444)})

    def test_interrupts_tablet_format(self):
        text = ("           CPU0       CPU1       CPU2\n"
                " 208:    3776118          0          0  msm_mdss   0 Edge      msm-kms\n"
                " 166:        377          0          0  msmgpio   14 Level     cs35l45 IRQ1 Controller, cs35l45\n"
                " 302:    2305883          0          0           196608 Edge      glink-smem\n"
                " 294:          0      45250          0  PCI-MSI-0000:01:00.0    8 Edge      DP_EXT_IRQ\n"
                "IPI0:    1591848     412666     387293  Rescheduling interrupts\n"
                "Err:          0\n").encode()
        it = sw.parse_interrupts(text)
        self.assertEqual(it["208"], (3776118, "msm-kms"))
        self.assertEqual(it["166"][1], "cs35l45 IRQ1 Controller, cs35l45")
        self.assertEqual(it["302"], (2305883, "glink-smem"))
        self.assertEqual(it["294"], (45250, "DP_EXT_IRQ"))
        self.assertEqual(it["IPI0"], (1591848 + 412666 + 387293, "Rescheduling interrupts"))
        self.assertEqual(it["Err"], (0, ""))

    def test_pid_stat_odd_names(self):
        tail = " ".join(["0"] * 50)
        st = sw.parse_pid_stat(("12 (a b) c) D 1 " + tail).encode())
        self.assertEqual(st[0], "a b) c")
        self.assertEqual(st[1], "D")
        self.assertIsNone(sw.parse_pid_stat(b"12 (short) S 1 2"))
        self.assertIsNone(sw.parse_pid_stat(None))

    def test_kmsg(self):
        r = sw.parse_kmsg_record(b"3,1234,54123456789,-;[drm:dpu_encoder_frame_done_timeout:2731] "
                                 b"[dpu error]enc35 frame done timeout\n SUBSYSTEM=drm\n")
        self.assertEqual(r[0], 3)
        self.assertAlmostEqual(r[1], 54123456789)
        self.assertTrue(r[2].endswith("frame done timeout"))
        self.assertEqual(sw.classify_kmsg(r[2]), "display")
        self.assertEqual(sw.classify_kmsg("geni_i2c a90000.i2c: geni_i2c_gpi_xfer:I2C timeout"), "bus")
        self.assertEqual(sw.classify_kmsg("adreno 3d00000.gpu: [drm:hangcheck_handler] *ERROR* hangcheck detected gpu lockup rb 0!"), "gpu")
        self.assertEqual(sw.classify_kmsg("INFO: task sh:123 blocked for more than 60 seconds."), "lockup")
        self.assertEqual(sw.classify_kmsg("Out of memory: Killed process 4730 (chrome)"), "memory")
        self.assertIsNone(sw.classify_kmsg("panel-samsung-ana38407 ae94000.dsi.0: ana38407 panel id: 80 00 04"))
        self.assertIsNone(sw.parse_kmsg_record(b"nonsense"))

    def test_misc_parsers(self):
        self.assertEqual(sw.parse_swaps(b"Filename Type Size Used Priority\n/dev/zram0 partition 8388604 399812 100\n"),
                         {"zram0": 390})
        self.assertEqual(sw.parse_mm_stat(b"345313280 74928446 84111360 0 85454848 740 0 189 189"), (329, 71, 80))
        self.assertIsNone(sw.parse_mm_stat(b"1"))
        info = sw.parse_fdinfo_drm(b"drm-driver:\tmsm\ndrm-engine-gpu:\t997240604256 ns\ndrm-total-memory:\t835480 KiB\n")
        self.assertEqual(info["drm-engine-gpu"], "997240604256")
        self.assertEqual(info["drm-total-memory"], "835480")

    def test_delta_handles_reset(self):
        self.assertEqual(sw.delta(150, 100), 50)
        self.assertEqual(sw.delta(30, 100), 30)
        self.assertEqual(sw.delta(5, None), 0)
        self.assertEqual(sw.delta(None, 5), 0)

    def test_config_file(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write("# comment\nlate_ms = 300\npsi_trigger_cpu = some 500000 1000000\nbogus = 1\n"
                    "interval_ms = 5\nmem_full_ms = abc\n")
        try:
            c = sw.load_config(f.name)
        finally:
            os.unlink(f.name)
        self.assertEqual(c["late_ms"], 300)
        self.assertEqual(c["psi_trigger_cpu"], "some 500000 1000000")
        self.assertEqual(c["interval_ms"], 100)   # clamped
        self.assertEqual(c["mem_full_ms"], sw.DEFAULTS["mem_full_ms"])
        self.assertNotIn("bogus", c)

    def test_unprivileged_trigger_spec(self):
        self.assertEqual(sw.Daemon._unpriv("some 150000 1000000"), "some 300000 2000000")
        self.assertIsNone(sw.Daemon._unpriv("full 300000 2000000"))
        self.assertIsNone(sw.Daemon._unpriv("nonsense"))


# --------------------------------------------------------------------------
class SamplerTests(Base):
    def test_deltas_over_one_second(self):
        clock = FakeClock()
        d = self.daemon(clock)
        self.step(d, clock)

        def bump(c):
            c["psi"]["memory"] = [c["psi"]["memory"][0] + 400000, c["psi"]["memory"][1] + 300000]
            c["psi"]["io"] = [c["psi"]["io"][0] + 1000, c["psi"]["io"][1] + 500]
            c["cg"]["comp"] = [c["cg"]["comp"][0] + 250000, c["cg"]["comp"][1] + 200000]
            c["vm"]["allocstall_normal"] += 7
            c["vm"]["allocstall_movable"] += 3
            c["vm"]["pgscan_direct"] += 2560
            c["vm"]["pswpout"] += 25600
            c["vm"]["unevictable_pgs_rescued"] += 258320
            c["cpu"] = [[b + 50, i + 50, w, q] for b, i, w, q in c["cpu"]]
            c["disk"]["sda"] = [v + 10 for v in c["disk"]["sda"]]
            c["irq"]["208"] += 120
            c["irq"]["280"] += 60
            c["irq"]["201"] += 40
            c["irq"]["193"] += 200
            c["irq"]["130"] += 3
            c["comp_sched"]["main"] = [c["comp_sched"]["main"][0] + 45_900_000, c["comp_sched"]["main"][1] + 350_000_000, 0]
            c["gpu_ns"] += 120_000_000
            c["procs"][4730] = ("chrome", "R", 9000 + 48, 200000)
            c["procs"][84] = ("kswapd0", "D", 50 + 12, 0)
        s = self.step(d, clock, bump=bump)
        self.assertEqual(s["psi"]["memory"], [400.0, 300.0])
        self.assertEqual(s["psi"]["io"], [1.0, 0.5])
        self.assertEqual(s["vm"]["allocstall"], 10)
        self.assertEqual(s["vm"]["scan_d"], 2560)
        self.assertEqual(s["vm"]["pswpout"], 25600)
        self.assertEqual(s["vm"]["resc"], 258320)
        self.assertNotIn("cstall", s["vm"])          # zero deltas are left out
        self.assertEqual(s["cpu"]["busy"], [50] * 8)
        self.assertEqual(s["disk"]["sda"][0], 10)
        self.assertEqual(s["disp"], 180)               # msm-kms + dsi_isr
        self.assertEqual(s["inp"], 240)                # goodix + BT UART
        self.assertEqual(s["lmh"], 3)
        self.assertEqual(s["comp"]["mem"], [250.0, 200.0])
        self.assertEqual(set(s["cg"]), {"user", "system", "app", "bt"})
        self.assertEqual(s["comp"]["main"], ["S", 45.9, 350.0])
        self.assertEqual(s["comp"]["gpu_ms"], 120.0)
        self.assertEqual(s["comp"]["gpu_mb"], 545)
        self.assertEqual(s["cpu"]["freq"], [1344, 2054, 3360])
        self.assertNotIn("cap", s["cpu"])
        self.assertEqual(s["therm"][0], ["cpu7-top-thermal", 50.7])
        self.assertEqual(s["devfreq"], {"1d84000.ufshc": 75, "3d00000.gpu": 719})
        self.assertEqual(s["zram"]["zram0"], [329, 71, 80])
        self.assertEqual(s["swap"], {"zram0": 390, "swapfile": 0})
        self.assertEqual(s["screen"], 1)
        # escalated by the flags of the previous sample? not yet: first scan happened at sample 1
        self.assertIn("flags", s)
        self.assertIn("mem", s["flags"])
        self.assertIn("desktop", s["flags"])
        # the next sample is escalated: process scan every second, chrome at 48 %, kswapd in D
        s2 = self.step(d, clock, bump=lambda c: c["procs"].__setitem__(4730, ("chrome", "R", 9048 + 48, 200000)))
        self.assertEqual(s2["top_cpu"][0], [4730, "chrome", 48])
        self.assertIn([84, "kswapd0"], s2["dproc"])
        self.assertEqual(s2["top_rss"][0], [4730, "chrome", 781])

    def test_frequency_cap_and_cooling(self):
        clock = FakeClock()
        d = self.daemon(clock)
        self.step(d, clock)
        s = self.step(d, clock, bump=lambda c: (c.__setitem__("maxfreq", [2016000, 1804800, 2400000]),
                                                c.__setitem__("cool", 7)))
        self.assertEqual(s["cpu"]["cap"], [None, 1804, 2400])
        # cooling is refreshed every 5th sample or while escalated
        for _ in range(5):
            s = self.step(d, clock)
        self.assertEqual(s["cool"], {"cpufreq-cpu7": 7})
        self.assertNotIn("ath12k_thermal", json.dumps(s))   # driver cooling device never read

    def test_missing_files_everywhere(self):
        empty = self.base / "empty"
        (empty / "proc").mkdir(parents=True)
        d = sw.Daemon(cfg(), root=str(empty), data_dir=str(self.data), run_dir=str(self.run), clock=FakeClock(),
                      arm_triggers=True, use_kmsg=True, use_fifo=False)
        for _ in range(3):
            d.clock.advance(1.0)
            s = d.take(d.clock.mono(), 0.0, "tick")
            self.assertIsNotNone(s)
        self.assertEqual(d.stats["errors"], 0, d.stats["last_error"])
        self.assertEqual(s["psi"], {})
        self.assertNotIn("mem", s)
        self.assertEqual(d.trigger_state["memory"], "no PSI")
        self.assertTrue(d.kmsg_state.startswith("unavailable"))
        d.drain()
        self.assertTrue((self.run / "status.json").exists())

    def test_compositor_restart_is_followed(self):
        clock = FakeClock()
        d = self.daemon(clock)
        self.step(d, clock)
        self.assertEqual(d.sampler.comp["pid"], 2924)
        self.assertEqual(d.sampler.comp["kms"], 2959)
        self.assertEqual(d.sampler.comp["drmfd"], 12)
        # gnome-shell goes away: the next sample rediscovers and finds none
        import shutil
        shutil.rmtree(self.fake.root / "proc/2924")
        del self.fake.c["procs"][2924]
        s = d.take(clock.mono() + 1, 0.0, "tick")
        self.assertNotIn("comp", s)

    def test_slow_driver_read_is_not_repeated(self):
        f = sw.Files(str(self.fake.root))
        real = f.read

        def slow(p, keep=False, size=65536):
            if p.endswith("target_freq"):
                time.sleep(0.08)
            return real(p, keep, size)
        f.read = slow
        self.assertEqual(f.read_guarded("/sys/class/devfreq/3d00000.gpu/target_freq").strip(), b"719000000")
        self.assertIn("/sys/class/devfreq/3d00000.gpu/target_freq", f.slow)
        self.assertIsNone(f.read_guarded("/sys/class/devfreq/3d00000.gpu/target_freq"))


# --------------------------------------------------------------------------
class DetectorTests(unittest.TestCase):
    def flags(self, det, **s):
        base = {"dt": 1000, "late": 0, "work": 1, "psi": {}}
        base.update(s)
        return det.flags(base)

    def test_each_detector(self):
        c = cfg()
        det = sw.Detector(c)
        self.assertEqual(self.flags(det), [])
        self.assertEqual(self.flags(det, late=250), ["late"])
        self.assertEqual(self.flags(det, late=2500, susp=3000), [])            # suspended, not stalled
        self.assertEqual(self.flags(det, late=2500, resumed=1), [])
        self.assertEqual(self.flags(det, work=300), ["slow"])
        self.assertEqual(self.flags(det, psi={"memory": [150, 120]}), ["mem"])
        self.assertEqual(self.flags(det, psi={"memory": [150, 90]}), [])
        self.assertEqual(self.flags(det, psi={"io": [400, 250]}), ["io"])
        self.assertEqual(self.flags(det, vm={"oom": 1}), ["oom"])
        self.assertEqual(det.flags({"dt": 1000, "psi": {}}, ["display"]), ["kernel"])

    def test_rates_use_the_sample_length(self):
        det = sw.Detector(cfg())
        # 60 ms of full stall in a 500 ms burst sample is 120 ms/s
        self.assertEqual(self.flags(det, dt=500, psi={"memory": [60, 60]}), ["mem"])
        # a 50 ms sample is treated as 200 ms long: 15 ms is 75 ms/s, below the line
        self.assertEqual(self.flags(det, dt=50, psi={"memory": [15, 15]}), [])

    def test_cpu_needs_to_be_sustained(self):
        det = sw.Detector(cfg(cpu_sustain_s=3))
        out = [self.flags(det, psi={"cpu": [980, 0]}) for _ in range(4)]
        self.assertEqual(out, [[], [], ["cpu"], ["cpu"]])
        self.assertEqual(self.flags(det, psi={"cpu": [100, 0]}), [])

    def test_desktop(self):
        det = sw.Detector(cfg())
        self.assertEqual(self.flags(det, comp={"mem": [120, 100], "main": ["S", 10, 0]}), ["desktop"])
        self.assertEqual(self.flags(det, comp={"main": ["S", 10, 400]}), ["desktop"])
        self.assertEqual(self.flags(det, comp={"main": ["D", 0, 0]}), [])
        self.assertEqual(self.flags(det, comp={"main": ["D", 0, 0]}), ["desktop"])   # second D in a row
        self.assertEqual(self.flags(det, comp={"main": ["S", 5, 3]}), [])

    def test_display_went_silent(self):
        det = sw.Detector(cfg())
        for _ in range(3):
            self.assertEqual(self.flags(det, disp=120, inp=100, screen=1), [])
        self.assertEqual(self.flags(det, disp=0, inp=100, screen=1), ["display_silent"])
        det2 = sw.Detector(cfg())
        self.flags(det2, disp=120, inp=100, screen=1)
        self.assertEqual(self.flags(det2, disp=0, inp=100, screen=0), [])         # screen off: not a stall
        det3 = sw.Detector(cfg())
        self.flags(det3, disp=120, inp=0, screen=1)
        self.assertEqual(self.flags(det3, disp=0, inp=2, screen=1), [])           # nobody using it


# --------------------------------------------------------------------------
class IncidentTests(Base):
    def quiet(self, d, clock, n):
        for _ in range(n):
            self.step(d, clock)

    def incidents(self):
        p = self.data / "incidents"
        return sorted(x for x in os.listdir(p)) if p.exists() else []

    def test_sampler_gap_makes_a_whole_machine_incident(self):
        clock = FakeClock()
        d = self.daemon(clock, post_s=5, pre_s=10)
        self.quiet(d, clock, 12)
        # the sampler could not run for 2.6 s: its next wakeup is that late
        s = self.step(d, clock, late=2600.0, secs=3.6)
        self.assertEqual(s["flags"], ["late"])
        self.assertIsNotNone(d.incident)
        self.assertEqual(len(d.incident.pre), 10)
        self.quiet(d, clock, 6)
        self.assertIsNone(d.incident)
        d.drain()
        names = self.incidents()
        self.assertEqual(len(names), 1, names)
        self.assertTrue(names[0].endswith("-machine"))
        inc = self.data / "incidents" / names[0]
        self.assertEqual(sorted(os.listdir(inc)), ["dump.txt", "samples.jsonl", "summary.json"])
        summ = json.loads((inc / "summary.json").read_text())
        self.assertEqual(summ["classes"][0], "machine")
        self.assertEqual(summ["pre_samples"], 10)
        # 10 before, the late one, and 5 quiet seconds: the fifth closes it
        self.assertEqual(summ["samples"], 10 + 1 + 5)
        self.assertIn("could not run for 2.6 s", summ["headline"])
        dump = (inc / "dump.txt").read_text()
        self.assertIn("===== /proc/meminfo =====", dump)
        self.assertIn("MemAvailable", dump)
        self.assertIn("GPU memory per DRM client", dump)
        self.assertIn("pid 2924", dump)
        self.assertIn("===== kernel log", dump)
        self.assertIn("KMS thread", dump)
        # every line of the record is a sample
        lines = (inc / "samples.jsonl").read_text().splitlines()
        self.assertEqual(len(lines), 16)
        self.assertTrue(all(json.loads(x)["v"] == 1 for x in lines))

    def test_psi_burst_makes_a_memory_incident_with_numbers(self):
        clock = FakeClock()
        d = self.daemon(clock, post_s=5, pre_s=8)
        self.quiet(d, clock, 9)

        def stall(c):
            c["psi"]["memory"] = [c["psi"]["memory"][0] + 900000, c["psi"]["memory"][1] + 800000]
            c["cg"]["user"] = [c["cg"]["user"][0] + 850000, c["cg"]["user"][1] + 780000]
            c["cg"]["comp"] = [c["cg"]["comp"][0] + 700000, c["cg"]["comp"][1] + 700000]
            c["vm"]["allocstall_normal"] += 100
            c["vm"]["pgscan_direct"] += 25600       # 100 MB
            c["vm"]["pgsteal_direct"] += 2560       # 10 MB
            c["vm"]["pswpout"] += 51200             # 200 MB
            c["vm"]["workingset_refault_file"] += 12800  # 50 MB
            c["mem"]["MemAvailable"] -= 100000
        for _ in range(3):
            s = self.step(d, clock, bump=stall)
            self.assertIn("mem", s["flags"])
            self.assertIn("desktop", s["flags"])
        # it clears: a state dump is asked for at once
        self.step(d, clock)
        self.assertTrue(d.incident.dump_posted)
        self.quiet(d, clock, 5)
        d.drain()
        names = self.incidents()
        self.assertEqual(len(names), 1)
        self.assertTrue(names[0].endswith("-memory"), names)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = sw.cmd_report(str(self.data), "latest", cfg())
        self.assertEqual(rc, 0)
        text = out.getvalue()
        self.assertIn("Verdict: MEMORY stall: tasks waited 2.4 s", text)
        self.assertIn("also: DESKTOP stall: the compositor was blocked (memory 2.1 s", text)
        self.assertIn("memory: full stall 2.4 s (worst second 80 %)", text)
        self.assertIn("user.slice full 2.3 s", text)
        self.assertIn("memory stall by cgroup (some/full): apps (app.slice) 0.0/0.0 s, bluetoothd 0.0/0.0 s, "
                      "system.slice 0.0/0.0 s, user.slice 2.5/2.3 s", text)
        self.assertIn("direct reclaim 300 entries, scanned 300 MB, reclaimed 30 MB", text)
        self.assertIn("swap-out 600 MB", text)
        self.assertIn("refaults: file 150 MB", text)
        self.assertIn("(file cache thrashing)", text)
        self.assertIn("this was not a whole-machine stall", text)
        self.assertIn("memory just before: available", text)
        self.assertIn("Evidence (samples around the flagged ones):", text)
        self.assertIn("<mem,desktop>", text)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            sw.cmd_list(str(self.data))
        self.assertIn("MEMORY stall", out.getvalue())

    def test_small_episode_is_counted_not_saved(self):
        clock = FakeClock()
        d = self.daemon(clock, post_s=3, pre_s=5)
        self.quiet(d, clock, 6)
        self.step(d, clock, bump=lambda c: c["psi"].__setitem__("memory", [c["psi"]["memory"][0] + 150000,
                                                                           c["psi"]["memory"][1] + 120000]))
        self.quiet(d, clock, 4)
        d.drain()
        self.assertEqual(self.incidents(), [])
        self.assertEqual(d.stats["minor"], 1)

    def test_kernel_line_makes_an_incident(self):
        clock = FakeClock()
        d = self.daemon(clock, post_s=2, pre_s=3)
        self.quiet(d, clock, 4)
        d.kmsg_buf.append([54123.456, "display", "[drm:dpu_encoder_frame_done_timeout:2731] [dpu error]enc35 frame done timeout", 3])
        s = self.step(d, clock)
        self.assertIn("kernel", s["flags"])
        self.quiet(d, clock, 3)
        d.drain()
        names = self.incidents()
        self.assertEqual(len(names), 1)
        self.assertTrue(names[0].endswith("-display"), names)

    def test_long_incident_is_split(self):
        clock = FakeClock()
        d = self.daemon(clock, post_s=5, pre_s=3, max_incident_s=10)
        self.quiet(d, clock, 4)

        def stall(c):
            c["psi"]["memory"] = [c["psi"]["memory"][0] + 600000, c["psi"]["memory"][1] + 500000]
        for _ in range(15):
            self.step(d, clock, bump=stall)
        self.quiet(d, clock, 6)
        d.drain()
        self.assertEqual(len(self.incidents()), 2)

    def test_hourly_limit_keeps_summaries_only(self):
        clock = FakeClock()
        d = self.daemon(clock, post_s=1, pre_s=2, incidents_per_hour=1)
        self.quiet(d, clock, 3)
        for _ in range(2):
            self.step(d, clock, late=900.0)
            self.quiet(d, clock, 2)
        d.drain()
        names = self.incidents()
        self.assertEqual(len(names), 2)
        self.assertTrue((self.data / "incidents" / names[0] / "samples.jsonl").exists())
        self.assertEqual(os.listdir(self.data / "incidents" / names[1]), ["summary.json"])
        self.assertTrue(all(n.endswith("-machine") for n in names), names)

    def test_mark_saves_the_window_before_it(self):
        clock = FakeClock()
        d = self.daemon(clock, pre_s=5, mark_post_s=2)
        self.quiet(d, clock, 6)
        clock.advance(0.3)
        s = d.take(clock.mono(), 0.0, "trig:mark", mark=True)
        self.assertEqual(s["flags"], ["mark"])
        self.quiet(d, clock, 3)
        d.drain()
        names = self.incidents()
        self.assertEqual(len(names), 1)
        self.assertTrue(names[0].endswith("-mark"), names)
        summ = json.loads((self.data / "incidents" / names[0] / "summary.json").read_text())
        # the verdict covers the pre-window too (the freeze came before the mark)
        self.assertGreaterEqual(summ["end"] - summ["start"], 6)

    def test_core_window(self):
        S = lambda f=None: {"t": 0, "flags": f} if f else {"t": 0}
        samples = [S(), S(), S(["mem"]), S(), S(["mem"]), S()]
        core, before = sw.core_window(samples, 2)
        self.assertEqual(len(core), 3)
        self.assertIs(before, samples[1])
        marked = [S(), S(["mem"]), S(), S(["mark"]), S()]
        core, before = sw.core_window(marked, 3)
        self.assertEqual(len(core), 1)            # the earlier flagged sample is the freeze it marks
        self.assertIs(core[0], marked[1])
        plain = [S(), S(), S(["mark"]), S()]
        core, before = sw.core_window(plain, 2)
        self.assertEqual(len(core), 4)
        self.assertIsNone(before)


# --------------------------------------------------------------------------
class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name) / "data"

    def tearDown(self):
        self.tmp.cleanup()

    def test_ring_rotation_and_cap(self):
        c = cfg(ring_segment_s=60, ring_max_mb=1, min_free_mb=0)
        st = sw.Store(c, str(self.dir))
        st.header = {"hdr": 1, "boot": "x"}
        rnd = random.Random(1)
        wall = 1795383000.0
        total = 0
        for i in range(30):
            lines = [json.dumps({"t": wall + j, "junk": "%x" % rnd.getrandbits(16000)}).encode() for j in range(60)]
            total += st.append_ring(lines, wall)
            wall += 60
        self.assertGreater(total, 2 * 1024 * 1024)
        segs = sorted(os.listdir(self.dir / "ring"))
        size = sum(os.path.getsize(self.dir / "ring" / s) for s in segs)
        self.assertLessEqual(size, 1024 * 1024 + max(os.path.getsize(self.dir / "ring" / s) for s in segs))
        self.assertIn(os.path.basename(st.seg_path), segs)      # the current one is never deleted
        # what is left reads back, header first, samples in order
        with gzip.open(self.dir / "ring" / segs[-1]) as f:
            recs = [json.loads(x) for x in f]
        self.assertEqual(recs[0]["hdr"], 1)
        self.assertEqual(len(recs), 61)
        ring = sw.read_ring(str(self.dir / "ring"), wall - 120, wall)
        self.assertEqual(len(ring), 120)
        self.assertEqual([s["t"] for s in ring], sorted(s["t"] for s in ring))

    def test_gzip_members_append(self):
        c = cfg(ring_segment_s=600, min_free_mb=0)
        st = sw.Store(c, str(self.dir))
        for k in range(3):
            st.append_ring([json.dumps({"t": 1795383000.0 + k}).encode()], 1795383000.0 + k)
        segs = os.listdir(self.dir / "ring")
        self.assertEqual(len(segs), 1)
        with gzip.open(self.dir / "ring" / segs[0]) as f:
            self.assertEqual(len(f.read().splitlines()), 3)

    def test_incident_caps(self):
        c = cfg(incidents_max_count=3, incidents_max_mb=1, min_free_mb=0)
        st = sw.Store(c, str(self.dir))
        for i in range(5):
            st.write_incident("2026112%d-000000-memory" % i, {"summary.json": b"{}"})
        self.assertEqual(st.incidents(), ["20261122-000000-memory", "20261123-000000-memory",
                                          "20261124-000000-memory"])
        big = os.urandom(700 * 1024)
        st.write_incident("20261125-000000-io", {"samples.jsonl": big})
        st.write_incident("20261126-000000-io", {"samples.jsonl": big})
        names = st.incidents()
        self.assertEqual(names[-1], "20261126-000000-io")
        self.assertLessEqual(sum(sw.dir_size(self.dir / "incidents" / n) for n in names), 1024 * 1024)
        # same name twice gets a suffix, never overwrites
        p1 = st.write_incident("20261127-000000-io", {"a": b"1"})
        p2 = st.write_incident("20261127-000000-io", {"a": b"2"})
        self.assertNotEqual(p1, p2)
        self.assertFalse([x for x in os.listdir(self.dir / "incidents") if x.endswith(".partial")])

    def test_no_writes_when_the_disk_is_full(self):
        c = cfg(min_free_mb=10 ** 9)
        st = sw.Store(c, str(self.dir))
        self.assertEqual(st.append_ring([b"{}"], 1795383000.0), 0)
        self.assertIsNone(st.write_incident("x", {"a": b"b"}))


# --------------------------------------------------------------------------
class LoopTests(Base):
    """The real loop with its threads, a real clock, 100 ms samples."""

    def run_daemon(self, d, seconds, during=None):
        t = threading.Thread(target=d.run, kwargs={"max_seconds": seconds})
        t.start()
        if during:
            during()
        t.join(seconds + 30)
        self.assertFalse(t.is_alive())

    def test_end_to_end_burst_mark_status(self):
        c = cfg(interval_ms=100, post_s=1, pre_s=2, mark_post_s=1, ring_flush_s=1, min_free_mb=0)
        d = sw.Daemon(c, root=str(self.fake.root), data_dir=str(self.data), run_dir=str(self.run),
                      arm_triggers=False, use_kmsg=False, use_fifo=True)
        self.assertIsNotNone(d.fifo_fd)

        def during():
            time.sleep(1.0)
            for _ in range(4):
                self.fake.c["psi"]["memory"][1] += 200000
                self.fake.c["psi"]["memory"][0] += 200000
                self.fake.write()
                time.sleep(0.1)
            time.sleep(2.5)
            fd = os.open(self.run / "mark", os.O_WRONLY | os.O_NONBLOCK)
            os.write(fd, b"mark\n")
            os.close(fd)
            time.sleep(1.0)
        self.run_daemon(d, 6.5, during)
        names = sorted(os.listdir(self.data / "incidents"))
        self.assertEqual(len(names), 2, names)
        self.assertTrue(names[0].endswith("-memory"))
        self.assertTrue(names[1].endswith("-mark"))
        st = json.loads((self.run / "status.json").read_text())
        self.assertGreaterEqual(st["samples"], 50)
        self.assertEqual(st["errors"], 0, st["last_error"])
        self.assertEqual(st["marks"], 1)
        self.assertLess(st["late_ms"]["p99"], 50)
        segs = os.listdir(self.data / "ring")
        self.assertTrue(segs)
        ring = sw.read_ring(str(self.data / "ring"), 0, time.time() + 1)
        self.assertGreaterEqual(len(ring), 50)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = sw.cmd_status(str(self.data), str(self.run))
        self.assertIn("incidents: 2 saved on disk", out.getvalue())
        self.assertIn("samples every 100 ms", out.getvalue())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            sw.cmd_at(str(self.data), str(ring[len(ring) // 2]["t"]), 10, c)
        self.assertIn("Ring around", out.getvalue())


if __name__ == "__main__":
    unittest.main()
