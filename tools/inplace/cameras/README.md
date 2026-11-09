# SM-X710 camera kit

A record of an experiment on one SM-X710 test unit, not an install path (see
[`../README.md`](../README.md)).  Phase A is for a tablet still on device
package 2.55; the device package carries the same camera files from 2.56 on.

Two things, for the Galaxy Tab S9 (SM-X710, `gts9wifi`):

1. **Now, on the running system (no reboot, no new kernel):** stop the SM-X910
   camera stack from publishing four phantom `GTS9U-*` webcams. Device package
   2.55 loads `v4l2loopback` with the Ultra's four nodes on every board and runs
   four relays that serve black frames, because the X710 kernel has no camera
   enabled. `install.sh` puts the board-aware camera files of the next device
   package in place: the loopback nodes and relays now follow a per-board camera
   table, which is empty on an X710 whose cameras are off.
2. **Later, once a kernel with the `gts9wifi_camera` switch is flashed:**
   `camera-test.sh` turns the X710's rear and/or front HI1337 on for one boot
   and collects the evidence (dmesg, `media-ctl -p`, `cam -l`, a libcamera frame,
   a frame from the `/dev/video20` relay).

Background, wiring and the decoded sensor tables: `docs/gts9wifi-cameras.md`.

## Building the kit (host)

```sh
sh tools/inplace/cameras/make-kit.sh /tmp/cameras-kit
scp -r /tmp/cameras-kit <user>@10.42.0.2:cameras-kit
```

`<user>` is the desktop user on the tablet; 10.42.0.2 is the development USB
gadget's address (an image built with `GTS9U_USB_GADGET=1`).

`make-kit.sh` copies the files from `packaging/ubuntu-gts9u-device` exactly as a
`DEVICE=gts9wifi` package build ships them (the WirePlumber labels rewritten
for the board), plus `FILES` (path and mode of each file), `REMOVED` (the two
files the package no longer ships), `BEFORE.sha256` (what 2.55 installed at
those paths) and `SHA256SUMS` over everything.

## Phase A: no phantom cameras (now)

```sh
sudo sh ~/cameras-kit/install.sh       # ~10 s, logs to /var/log/gts9u-cameras.log
sh ~/cameras-kit/verify.sh             # no sudo
```

`install.sh` refuses unless the board is an SM-X710, the kit checksums match,
dpkg is idle and the files it replaces are byte-identical to 2.55's. It backs
them up once to `/var/backups/ubuntu-gts9u-cameras`, stops the relays, installs
the files, removes `/usr/lib/modules-load.d/ubuntu-gts9u-cameras.conf` and
`/usr/lib/modprobe.d/ubuntu-gts9u-cameras.conf`, enables
`ubuntu-gts9u-camera-loopback.service` and `ubuntu-gts9u-camera-enable.service`,
and restarts the loader, which removes the `GTS9U-*` nodes. The relays then skip
themselves (`ConditionFileNotEmpty=/run/ubuntu-gts9u/camera-profile`).

If an application (Chrome, OBS) holds a `/dev/video2x` node, the loader cannot
unload the module and `install.sh` stops and says so: close it and re-run.

Undo: `sudo sh ~/cameras-kit/rollback.sh` (restores 2.55's files, loads the
module with the old options, restarts the four relays). It refuses once a newer
device package is installed; reinstall a package instead.

These are package-owned files changed in place: `dpkg --verify
ubuntu-gts9u-device` lists them until a device package carrying the same
change is installed, which simply replaces them.

## Phase B: first light (after the camera kernel)

The camera kernel's DTB describes both modules but keeps CAMSS, CCI0, CCI1, the
sensors, the lens and the rear VIO switch disabled; nothing camera-related
probes at boot. The switch is one-shot per boot.

```sh
sudo sh ~/cameras-kit/camera-test.sh rear     # results in /var/log/gts9u-cameras-test/
sudo reboot
sudo sh ~/cameras-kit/camera-test.sh front    # front VDIG runs 1.2 V here (stock 1.1 V)
sudo reboot
sudo sh ~/cameras-kit/camera-test.sh both
```

To have the cameras on at every boot once they work (after the panel's boot
cycle, before the relays):

```sh
sudo install -d /etc/ubuntu-gts9u                  # the directory does not exist yet
echo both | sudo tee /etc/ubuntu-gts9u/cameras    # or rear / front
sudo reboot                                        # undo: sudo rm /etc/ubuntu-gts9u/cameras
```

If a test hangs or resets the tablet, that is the result: a reboot comes back
without the cameras (the switch does not persist, the DTB keeps them disabled).

## GNOME Camera's picture path (no root)

Two read-only tools, run as the desktop user while the camera is enabled.
Copy them over yourself (`make-kit.sh` does not include them):

```sh
scp tools/inplace/cameras/{camerabin-photos,measure-stream}.py <user>@<tablet>:
python3 ~/measure-stream.py 1280x1080 4120x3096+gl     # fps, CPU, GPU MHz, csid0 rate
python3 ~/camerabin-photos.py --mode stock --pictures 10
python3 ~/camerabin-photos.py --mode patched --pictures 10
```

- `measure-stream.py` streams one size at a time through pipewiresrc, with a
  headless GL upload for `+gl`. Stick to sizes known to work: on
  libcamera-gts9u10/11, 1400x1050 makes the GPU debayer fail and the
  libcamera node then aborts the whole PipeWire daemon, the user's audio with
  it.
- `camerabin-photos.py` rebuilds Snapshot 46.2's picture pipeline (camerabin,
  wrappercamerabinsrc around `pipewiresrc ! capsfilter ! decodebin3 !
  videoflip`) with a fakesink viewfinder. It takes pictures as aperture
  does, then reports each JPEG's size, the libcamera configurations in the
  user journal, and the longest viewfinder stall. `--mode patched`
  reproduces `packaging/gnome-snapshot`'s caps and its RECONFIGURE probe in
  Python.
- Both fail with "Pipeline handler in use by another process" while
  another libcamera client, such as `cam`, holds the camera.
