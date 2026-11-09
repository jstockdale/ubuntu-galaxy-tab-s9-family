#!/usr/bin/env python3
"""Stream the libcamera camera through pipewiresrc at given sizes, one at a
time, and report the time to the first frame, fps, CPU per process, GPU
frequency and the CAMSS csid0 frame-interrupt rate.  Read-only, no root.

  measure-stream.py [--seconds N] SIZE...    SIZE = WxH, or WxH+gl to add a
                                             headless GL upload, as GNOME
                                             Camera's GL viewfinder does
"""
import argparse
import glob
import os
import subprocess
import time

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

HZ = os.sysconf("SC_CLK_TCK")


def ticks(pid):
    try:
        with open(f"/proc/{pid}/stat") as f:
            fields = f.read().rsplit(")", 1)[1].split()
        return int(fields[11]) + int(fields[12])
    except (OSError, ValueError, TypeError):
        return 0


def csid0():
    with open("/proc/interrupts") as f:
        for line in f:
            if "isp_msm_csid0" in line:
                return int(line.split()[1])
    return 0


def pid_of(name):
    out = subprocess.run(["pgrep", "-x", name], capture_output=True, text=True).stdout.split()
    return int(out[0]) if out else None


def libcamera_node():
    out = subprocess.run(["pw-cli", "ls", "Node"], capture_output=True, text=True).stdout
    for line in out.splitlines():
        if 'node.name = "libcamera_input' in line:
            return line.split('"')[1]
    return None


def run(spec, seconds, node, pids, gpu):
    size, gl = (spec[:-3], True) if spec.endswith("+gl") else (spec, False)
    w, h = (int(v) for v in size.split("x"))
    sink = "glupload ! glcolorconvert ! fakesink name=sink sync=false" if gl else \
        "fakesink name=sink sync=false"
    pipe = Gst.parse_launch(f'pipewiresrc target-object="{node}" ! '
                            f"video/x-raw,format=RGBA,width={w},height={h} ! {sink}")
    frames = []
    pipe.get_by_name("sink").get_static_pad("sink").add_probe(
        Gst.PadProbeType.BUFFER, lambda p, i: (frames.append(time.monotonic()),
                                               Gst.PadProbeReturn.OK)[1])
    loop = GLib.MainLoop()
    err = []

    def on_msg(_, msg):
        if msg.type == Gst.MessageType.ERROR:
            err.append(msg.parse_error()[0].message)
            loop.quit()
    bus = pipe.get_bus()
    bus.add_signal_watch()
    bus.connect("message", on_msg)
    t0 = time.monotonic()
    pipe.set_state(Gst.State.PLAYING)
    sample = {}
    freqs = []

    def tick():
        now = time.monotonic()
        if "first" not in sample:
            if frames:
                sample["first"] = frames[0] - t0
                sample["start"] = now
                sample["n0"] = len(frames)
                sample["t"] = {k: ticks(p) for k, p in pids.items()}
                sample["c"] = csid0()
            elif now - t0 > 10:
                err.append("no frame in 10 s")
                loop.quit()
                return False
            return True
        if gpu and now - sample.get("fq", 0) >= 1:
            sample["fq"] = now
            with open(os.path.join(gpu, "cur_freq")) as f:
                freqs.append(int(f.read()) // 1000000)
        if now - sample["start"] >= seconds:
            sample["end"] = now
            sample["n1"] = len(frames)
            sample["t1"] = {k: ticks(p) for k, p in pids.items()}
            sample["c1"] = csid0()
            loop.quit()
            return False
        return True
    GLib.timeout_add(100, tick)
    loop.run()
    caps = pipe.get_by_name("sink").get_static_pad("sink").get_current_caps()
    pipe.set_state(Gst.State.NULL)
    if err:
        print(f"== {spec}: ERROR {err[0]}")
        return
    span = sample["end"] - sample["start"]
    gaps = [b - a for a, b in zip(frames[sample["n0"]:], frames[sample["n0"] + 1:sample["n1"]])]
    cpu = ", ".join(f"{k} {100 * (sample['t1'][k] - sample['t'][k]) / HZ / span:.0f}"
                    for k in pids)
    print(f"== {spec}: first frame {sample['first']:.2f} s; "
          f"{(sample['n1'] - sample['n0']) / span:.1f} fps; longest gap {max(gaps) * 1000:.0f} ms")
    print(f"   CPU % of one core: {cpu}")
    print(f"   csid0 {(sample['c1'] - sample['c']) / span:.1f}/s; GPU MHz {sorted(set(freqs))}")
    print(f"   caps: {caps.to_string() if caps else None}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=15)
    ap.add_argument("--pause", type=float, default=5)
    ap.add_argument("sizes", nargs="+")
    a = ap.parse_args()
    os.environ.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    os.environ.setdefault("GST_GL_WINDOW", "surfaceless")
    Gst.init(None)
    node = libcamera_node()
    if not node:
        raise SystemExit("no libcamera node")
    pids = {"self": os.getpid(), "wireplumber": pid_of("wireplumber"),
            "pipewire": pid_of("pipewire")}
    gpu = next(iter(glob.glob("/sys/class/devfreq/*gpu*")), None)
    print(f"node {node}; pids {pids}; gpu {gpu}")
    for i, spec in enumerate(a.sizes):
        if i:
            time.sleep(a.pause)
        run(spec, a.seconds, node, pids, gpu)


if __name__ == "__main__":
    main()
