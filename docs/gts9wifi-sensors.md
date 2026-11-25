# Galaxy Tab S9 (SM-X710, gts9wifi): sensors (SSC)

The X710's accelerometer, gyroscope, magnetometer and light sensor sit behind
the ADSP's Sensor Core exactly as on the Ultra, and the userspace chain is the
same: `ubuntu-gts9u-adsp-boot` starts the ADSP, `hexagonrpcd-adsp-sensorspd`
serves the sensor protection domain its HexagonFS tree, `libssc` talks to SSC
over QRTR, and `iio-sensor-proxy` hands rotation and light to GNOME.  Only the
HexagonFS tree, which carries Samsung's sensor registry, is per board.

Status (2026-11-07): SSC comes up on the first attempt, iio-sensor-proxy
reports `HasAccelerometer` and `HasAmbientLight`, and auto-rotation works with
the keyboard cover detached.  The light level follows the room: 0 lux at
night, and 1 to 290 lux over a morning of handling on 2026-11-07 (the cover
on and off, the tablet picked up), read as iio-sensor-proxy's `LightLevel`
once a second by a watcher during the owner's tests.  `ssccli --sensor light
--timeout 8` (below) reads the sensor directly.

For builders: the tree is a generated manifest entry, so staging
`configs/firmware/x710-bl5-X710XXS5CYG1.toml` needs `--sscregistrygen`:
`python3 scripts/stage-firmware.py stage --manifest
configs/firmware/x710-bl5-X710XXS5CYG1.toml ... --sscregistrygen PATH --out
<dir>`, with `PATH` the `tools/sscregistrygen` of sensh v2.0.0 (see
`configs/firmware/README.md`).  Without it `stage` refuses to run.

## What the first X710 images got wrong

Every boot logged `ubuntu-gts9u-sensors-resume: SSC did not become ready;
rotation will not work this boot`, independent of suspend, for three reasons
that compounded:

* the firmware manifest had no `sensor-hexagonfs.tar.gz`: it was recorded
  from the X910 manifest without `--sscregistrygen`, and `record` dropped the
  generated entry as "not in this package";
* the overlay builder only printed a note when the tarball was absent, so the
  image shipped an empty `/usr/share/qcom/sm8550/Samsung/gts9wifi`;
* the device package pointed `hexagonrpcd -R` (and the registry unit and
  tmpfiles rule) at the Ultra's `.../Samsung/gts9uwifi`, which does not exist
  on the X710.  hexagonrpcd then reported `Could not open
  /vendor/etc/sensors/sns_reg_config` and SSC never published a sensor.

Now the manifest pins the X710 tree, the overlay refuses to build without it,
and `build-device-package.sh` rewrites the tree path to `DEVICE_SENSOR_DIR`
from `scripts/lib/device.sh`.  hexagonrpcd's own guess cannot replace `-R`:
it takes the SoC from the first `qcom,` compatible, which is `qcom,kalama-mtp`.

## The tree

`stage-firmware.py` generates it (`generated:sensor-hexagonfs`) from the stock
vendor `etc/sensors` of X710XXS5CYG1, the five sensor-PD skeletons from that
package's `dspso.bin`, and sensh v2.0.0's `sscregistrygen -p MTP -s 519`.  The
same generator reproduces both pinned X910 trees byte for byte, and the X710
tree reproduces itself (sha256 `fc2b9fc5…`, 188 files).

Compared with the Ultra's tree:

| | X710 | X910 |
| --- | --- | --- |
| light sensor | Vishay VEML3328, SSC I2C 3 or 4 at 0x10, `ambient_light` + `rgb` | SensorTek STK31610, SSC I2C 3 or 4 at 0x48 |
| accelerometer/gyro | LSM6DSO, configs identical | LSM6DSO |
| magnetometer | AK0991x, different orientation and correction matrices | AK0991x |
| DSP skeletons | byte-identical to the X910 bl5 set | |

The accelerometer's registry configs are identical on both boards.  The
X910's mount-matrix rule (`61-gts9u-sensor-mount-matrix.rules`) matches
fastrpc-adsp on any board, so it applies to the X710 as well, and the Fedora
X710 port uses the same matrix.  Auto-rotation was confirmed by eye on
2026-11-07 with the keyboard detached.  The light stream is libssc's standard
`ambient_light` on the X710: `use-samsung-auto-brightness.patch` switches to
Samsung's `auto_brightness` only for `samsung,gts9uwifi`, where the
STK31610's physical stream delivers nothing.  The two other X710 ports report
`ambient_light` working; Samsung's X710 HAL implements both streams, so
`auto_brightness` is the fallback if the VEML3328 stays silent.

Two magnetometer entries carry a stray byte.  Samsung ships the AK0991x
calibration twice, `kailua_ak991x_0.json` for board revisions 0-4 and
`kailua_ak991x_5.json` for 5 and later; the DSP on Android applies only the
one matching `ro.revision`, but `sscregistrygen` ignores that key, applies
both, and opens its output without truncating it.  The 0-4 values are
written last (the directory order of the generator's tmpfs input), which is
the set this rev-04 board needs, but the longer rev-5 text leaves one `}`
behind in `ak0991x_0_platform.mag.fac_cal.corr_mat` and
`ak0991x_0_platform.mag.fac_cal_2.corr_mat`.  The Ultra's tree has the same
kind of tail in `qsh_ble_config` and SSC runs there, so this is not expected
to stop SSC; at worst the DSP ignores that calibration matrix and the compass
is less accurate.  Rotation and the light sensor do not use it.  An X710 of
board revision 5 or later gets the 0-4 matrices from this tree.

The sensor rails stay always-on board rails (L1B 1.8 V, L16B 3.0 V): Samsung's
`remoteproc-adsp` node votes them as `sensor_vdd`/`sensor_vddio` for as long as
the ADSP runs, and mainline `qcom_q6v5_pas` has no such supplies.

## Across a system suspend

The sensors run inside the ADSP, and the streams the desktop claims through
`iio-sensor-proxy` (orientation, ambient light) stay open through a system
sleep unless something closes them: the DSP keeps sampling and leaves its own
sleep 30 to 50 times a second.  On the X710 a 30 s suspend counted about
1,500 of the DSP's wakes with them open, and none with `iio-sensor-proxy` and
`hexagonrpcd-adsp-sensorspd` stopped first (Type-C kit revision 10 run 6 and
revision 11 run 5, 2026-11-23).  So the device package's system-sleep hook
`/usr/lib/systemd/system-sleep/ubuntu-gts9u-sensors` stops, before every
system sleep, `ubuntu-gts9u-sensors-resume` (so that it cannot start the proxy
again), then the proxy, then the file server, each before the next, and
after the sleep resets and restarts `ubuntu-gts9u-sensors-resume`, which
attaches the sensor protection domain again, waits for a real accelerometer
sample and starts the proxy with a fresh client (it had to be restarted after
a resume anyway: libssc cannot reconnect a QMI client after the DSP's power
collapse).  Auto-rotation and auto-brightness come back with it.  Both boards.

How it behaves:

* **Bounded.**  systemd-sleep runs the hooks in parallel and suspends only
  when the last one has returned, and `systemd-suspend.service` has no start
  timeout, so each `systemctl` call in the hook waits 10 s at most.  A stop
  still running then is left to systemd (the job goes on) and the sleep goes
  on.  The stops take under a second: the file server and the resume unit in
  milliseconds on the X910 (68 sleeps), the proxy, which is killed with
  SIGKILL, in 0.2 to 0.3 s on the X710.  A failure is logged in the journal
  of `systemd-suspend.service` (`ubuntu-gts9u-sensors: ...`) and never fails
  the sleep.
* **Only where the clients can come back:** the resume unit loads and
  `ssccli`, its condition, is installed (libssc ships it; both test units
  have it).  Elsewhere the hook leaves everything as it is, and says so.
* **On a `systemctl suspend`** the file server's own unit stops it as well
  (`Conflicts=suspend.target`), in the suspend transaction and in no order
  with the hook: on the X910's journal that stop ended just before the hook
  ran in all 68 sleeps, so there the file server goes a fraction of a second
  before the proxy.  rtcwake and the Type-C kit's sleep test go around the
  transaction, and there the hook alone stops it, after the proxy.
* **After the sleep**, also one the kernel refused (the post step runs all
  the same), the resume took 14 to 15 s from "System returned from sleep" to
  "SSC ready after attempt 1" on the X910 (59 resumes, 2026-11-07 to 16; once
  70 s, when something stopped the recovery halfway): 2 s before the unit
  starts, 10 s for the fresh attach to settle, then the first sample.  A
  following sleep stops a recovery still running, and the next resume starts
  it again.
* **A recovery that fails is tried again** after 30 s, three starts at most
  in 15 minutes (`Restart=on-failure`, `StartLimitBurst=3`); the hook resets
  that count before each resume's restart.  No recovery has failed by itself
  in the journals read on 2026-11-24 (118 runs on both tablets, all ready at
  the first attempt), so the retry has not run on hardware.
* With the old hook the proxy ran through the sleep and crashed (SIGSEGV)
  0.3 to 0.9 s after 21 of the X910's 64 returns, before the resume unit
  replaced it; with the proxy stopped first that cannot happen.

The Type-C kit's `sleep-test.sh sensors=hook` runs the hook's pre step for
its rtcwake sleeps, and knows the hook by the text `systemctl stop
iio-sensor-proxy.service`.  [`tools/inplace/sensors-hook`](../tools/inplace/sensors-hook/README.md)
puts the hook and the resume unit on a tablet whose device package cannot be
upgraded yet.

On the X710 (2026-11-24 and 25, through that kit, on device package 2.69)
the new hook has run: its two steps by hand, with SSC ready again 14.0 s
after the post step; one `pm_test=freezer` suspend through `systemd-sleep`
with every hook around it, the sensors ready 14.2 s after the return; and
its pre step before twelve real suspend-to-idle sleeps of the Type-C kit's
sleep test (revisions 12 to 14).  It stopped both clients every time, and
every one of those sleeps that came back with no sound stream open had the
DSP asleep for the whole sleep (`adsp +0`), where it had woken about 1,500
times in 30 s with the clients running.  One more client showed: a sound
stream left open keeps the DSP awake, and the DDR out of its low-power mode,
through the sleep whatever the hook does (two sleeps, one with a playback
and a capture stream open, one with a playback stream held open on
purpose).  Not run yet: rotation and auto-brightness checked at the tablet
after a sleep, a `systemctl suspend` whose post step follows a real sleep,
and the X910 with the new hook.  The kit's
[results](../tools/inplace/sensors-hook/README.md#results) have the logs.

Stock Android does not stop its sensor daemons (`sscrpcd`, the sensors HAL)
for a suspend.  Its kernel tells the DSP that the application processor is
going to sleep, through an SMP2P bit (`smp2p_sleepstate.c`, the `sleepstate`
entry of the ADSP's SMP2P node); mainline has no such driver, so the SSC
always takes the AP for awake.  libssc asks for wakeup delivery for every
stream and no batching.  Android's framework also releases the rotation and
light sensors at screen-off, before the AP sleeps.  Stopping only the proxy
should close every stream by itself (each SSC client is a QRTR socket of its
own, which the kernel closes with the process); keeping the file server
across the sleep would save the fresh attach and its 10 s, but whether the
DSP stays quiet with it attached has not been measured.

## Checking it

```sh
ls /usr/share/qcom/sm8550/Samsung/gts9wifi/sensors/registry | wc -l   # 141
systemctl status hexagonrpcd-adsp-sensorspd ubuntu-gts9u-sensors-resume
ssccli --sensor accelerometer --timeout 4
ssccli --sensor magnetometer --timeout 4  # uT samples; see the corr_mat note
ssccli --sensor light --timeout 8      # cover the sensor: lux must change
monitor-sensor --accel --light          # in the tablet's own session; Ctrl-C to stop
# over SSH polkit refuses monitor-sensor's claim; read the properties instead:
gdbus introspect --system --dest net.hadess.SensorProxy \
	--object-path /net/hadess/SensorProxy --only-properties
journalctl -b -t systemd-sleep          # every sleep, and what the sensors hook said
journalctl -b -u ubuntu-gts9u-sensors-resume   # each recovery, retries included
```

`ubuntu-gts9u-sensors-resume` should log `SSC ready after attempt N`.  GNOME
only rotates in touch mode.  On the X710 the cover's `Samsung Book Cover
Switch` device reports `SW_TABLET_MODE=0` in typing posture and when closed,
so GNOME stays in laptop mode and does not rotate.  Detaching the cover sets
`SW_TABLET_MODE=1` and gives rotation (confirmed); folding it behind the
tablet should do the same, but that posture is not confirmed on the EF-DX720
yet.
