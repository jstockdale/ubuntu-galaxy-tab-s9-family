#!/usr/bin/env python3
"""GNOME Camera's picture path without its window.

Builds the pipeline Snapshot 46.2's aperture builds -- camerabin, with
wrappercamerabinsrc around pipewiresrc ! capsfilter ! decodebin3 ! videoflip
-- with a fakesink viewfinder, takes pictures the way aperture does, and
reports each picture's size, the camera configurations libcamera logged
meanwhile, and any viewfinder stall.  Read-only for the system, no root.

  --mode stock    aperture 46.2's caps (largest height <= 1080 first), no guard
  --mode patched  the gts9u patch: largest raw size first, and the probe that
                  drops RECONFIGURE events which would not change the format
"""
import argparse
import os
import struct
import subprocess
import sys
import tempfile
import time

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

MAX_HEIGHT = 1080


def sizes(caps):
    out = []
    for i in range(caps.get_size()):
        s = caps.get_structure(i)
        ok_w, w = s.get_int("width")
        ok_h, h = s.get_int("height")
        if ok_w and ok_h:
            out.append((w, h))
    return out


def with_size(caps, w, h):
    return caps.intersect_full(Gst.Caps.from_string(f"video/x-raw,width={w},height={h}"),
                               Gst.CapsIntersectMode.FIRST)


def stock_caps(caps):
    heights = [h for _, h in sizes(caps) if h <= MAX_HEIGHT]
    out = with_size_height(caps, max(heights)).copy() if heights else Gst.Caps.new_empty()
    out.append(caps.copy())
    return merge(out)


def with_size_height(caps, h):
    return caps.intersect_full(Gst.Caps.from_string(f"video/x-raw,height={h}"),
                               Gst.CapsIntersectMode.FIRST)


def merge(caps):
    out = Gst.Caps.new_empty()
    for i in range(caps.get_size()):
        one = Gst.Caps.new_empty()
        one.append_structure(caps.get_structure(i).copy())
        out = out.merge(one)
    return out


def patched_caps(caps):
    raw = caps.intersect_full(Gst.Caps.from_string("video/x-raw"), Gst.CapsIntersectMode.FIRST)
    w, h = max(sizes(raw), key=lambda s: s[0] * s[1])
    out = with_size(raw, w, h).copy()
    out.append(stock_caps(caps))
    return merge(out)


def drop_redundant_reconfigure(pad, info):
    event = info.get_event()
    if event is None or event.type != Gst.EventType.RECONFIGURE:
        return Gst.PadProbeReturn.OK
    current = pad.get_current_caps()
    if current is None:
        return Gst.PadProbeReturn.OK
    peer = pad.get_peer()
    source = peer.query_caps(None) if peer else None
    caps = pad.query_caps(source)
    if caps.get_size() and Gst.Caps.from_string(
            caps.get_structure(0).to_string()).can_intersect(current):
        STATS["dropped"] += 1
        return Gst.PadProbeReturn.DROP
    return Gst.PadProbeReturn.OK


def jpeg_size(path):
    with open(path, "rb") as f:
        data = f.read()
    i = 2
    while i < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xC0, 0xC1, 0xC2):
            h, w = struct.unpack(">HH", data[i + 5:i + 9])
            return w, h
        length = struct.unpack(">H", data[i + 2:i + 4])[0]
        i += 2 + length
    return None


def configures_since(since):
    out = subprocess.run(["journalctl", "--user", "-q", "-o", "cat", "--since", since,
                          "-g", "configuring streams"], capture_output=True, text=True).stdout
    return [line.split("configuring streams:")[-1].strip() for line in out.splitlines()]


STATS = {"dropped": 0, "vf": 0}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=("stock", "patched"), required=True)
    ap.add_argument("--pictures", type=int, default=10)
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument("--warmup", type=float, default=4.0)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    Gst.init(None)
    out = a.out or tempfile.mkdtemp(prefix=f"camerabin-{a.mode}-", dir=os.path.expanduser("~"))
    os.makedirs(out, exist_ok=True)

    monitor = Gst.DeviceMonitor.new()
    monitor.add_filter("Video/Source", None)
    monitor.start()
    devices = [d for d in monitor.get_devices()
               if (d.get_properties() or Gst.Structure.new_empty("x")).get_string("api.libcamera.location")]
    monitor.stop()
    if not devices:
        sys.exit("no libcamera camera")
    device = devices[0]
    caps = device.get_caps().intersect_full(
        Gst.Caps.from_string("video/x-raw,framerate=[0/1,30/1]; image/jpeg,framerate=[0/1,30/1]"),
        Gst.CapsIntersectMode.FIRST)
    filt = patched_caps(caps) if a.mode == "patched" else stock_caps(caps)
    print(f"{device.get_display_name()}: preferred {filt.get_structure(0).to_string()}")

    src_bin = Gst.Bin.new()
    pwsrc = device.create_element(None)
    pwsrc.set_property("client-name", "org.gnome.Snapshot")
    capsfilter = Gst.ElementFactory.make("capsfilter")
    capsfilter.set_property("caps", filt)
    if a.mode == "patched":
        capsfilter.get_static_pad("sink").add_probe(Gst.PadProbeType.EVENT_UPSTREAM,
                                                    drop_redundant_reconfigure)
    decode = Gst.ElementFactory.make("decodebin3")
    flip = Gst.ElementFactory.make("videoflip")
    flip.set_property("video-direction", "auto")
    for e in (pwsrc, capsfilter, decode, flip):
        src_bin.add(e)
    pwsrc.link(capsfilter)
    capsfilter.link(decode)

    def pad_added(_, pad):
        stream = pad.get_stream()
        if stream and stream.get_stream_type() & Gst.StreamType.VIDEO:
            pad.link(flip.get_static_pad("sink"))
    decode.connect("pad-added", pad_added)
    src_bin.add_pad(Gst.GhostPad.new("src", flip.get_static_pad("src")))
    wrapper = Gst.ElementFactory.make("wrappercamerabinsrc")
    wrapper.set_property("video-source", src_bin)

    camerabin = Gst.ElementFactory.make("camerabin")
    camerabin.set_property("camera-source", wrapper)
    vf = Gst.ElementFactory.make("fakesink")
    vf.set_property("sync", False)
    camerabin.set_property("viewfinder-sink", vf)
    last_vf = [time.monotonic()]

    def vf_probe(pad, info):
        STATS["vf"] += 1
        last_vf[0] = time.monotonic()
        return Gst.PadProbeReturn.OK
    vf.get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER, vf_probe)

    loop = GLib.MainLoop()
    state = {"n": 0, "pending": False, "t0": 0.0, "results": [], "since": None, "error": None,
             "max_gap": 0.0}

    def on_message(_, msg):
        if msg.type == Gst.MessageType.ERROR:
            state["error"] = msg.parse_error()[0].message
            loop.quit()
        elif msg.type == Gst.MessageType.ELEMENT and msg.get_structure().has_name("image-done"):
            path = msg.get_structure().get_string("filename")
            state["results"].append((path, time.monotonic() - state["t0"]))
            state["pending"] = False
    bus = camerabin.get_bus()
    bus.add_signal_watch()
    bus.connect("message", on_message)

    def take():
        if state["pending"]:
            if time.monotonic() - state["t0"] > 15:
                state["error"] = "picture %d never completed" % state["n"]
                loop.quit()
                return False
            return True
        if state["n"] == 0:
            state["since"] = time.strftime("%Y-%m-%d %H:%M:%S")
            state["vf0"] = STATS["vf"]
        if state["n"] >= a.pictures:
            loop.quit()
            return False
        state["n"] += 1
        camerabin.set_property("mode", 1)
        camerabin.set_property("location", os.path.join(out, "picture-%02d.jpg" % state["n"]))
        state["pending"] = True
        state["t0"] = time.monotonic()
        camerabin.emit("start-capture")
        GLib.timeout_add(int(a.interval * 1000), take)
        return False

    def watch_gap():
        # Stalls count from the first viewfinder frame on.
        if STATS["vf"]:
            key = "max_gap" if state["since"] else "open_gap"
            state[key] = max(state.get(key, 0.0), time.monotonic() - last_vf[0])
        return True
    GLib.timeout_add(100, watch_gap)

    start = time.strftime("%Y-%m-%d %H:%M:%S")
    camerabin.set_state(Gst.State.PLAYING)
    GLib.timeout_add(int(a.warmup * 1000), take)
    t_start = time.monotonic()
    loop.run()
    elapsed = time.monotonic() - t_start
    time.sleep(1.0)
    camerabin.set_state(Gst.State.NULL)

    startup = configures_since(start)
    during = configures_since(state["since"]) if state["since"] else []
    print(f"configurations from open to the first picture: {len(startup) - len(during)}")
    for path, latency in state["results"]:
        size = jpeg_size(path)
        print(f"  {os.path.basename(path)}: {size[0]}x{size[1]}, {latency:.2f} s")
    print(f"pictures: {len(state['results'])}/{a.pictures}; configurations while taking them: "
          f"{len(during)} {sorted(set(during))}")
    print(f"viewfinder frames: {STATS['vf']} in {elapsed:.1f} s; longest stall while opening "
          f"{state.get('open_gap', 0.0):.2f} s, while taking pictures {state['max_gap']:.2f} s;"
          f" reconfigurations dropped by the guard: {STATS['dropped']}")
    if state["error"]:
        print("ERROR:", state["error"])
    print("pictures in", out)
    return 1 if state["error"] or len(state["results"]) != a.pictures else 0


if __name__ == "__main__":
    sys.exit(main())
