# Galaxy Tab S9 (SM-X710, gts9wifi): sensors (SSC)

The X710's accelerometer, gyroscope, magnetometer and light sensor sit behind
the ADSP's Sensor Core exactly as on the Ultra, and the userspace chain is the
same: `ubuntu-gts9u-adsp-boot` starts the ADSP, `hexagonrpcd-adsp-sensorspd`
serves the sensor protection domain its HexagonFS tree, `libssc` talks to SSC
over QRTR, and `iio-sensor-proxy` hands rotation and light to GNOME.  Only the
HexagonFS tree, which carries Samsung's sensor registry, is per board.

Status (2026-11-07): SSC comes up on the first attempt, iio-sensor-proxy
reports `HasAccelerometer` and `HasAmbientLight`, and auto-rotation works with
the keyboard cover detached.  The only recorded light level, 0 lux, was read
at night; whether lux follows room light is still to be checked
(`ssccli --sensor light --timeout 8` below).

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
```

`ubuntu-gts9u-sensors-resume` should log `SSC ready after attempt N`.  GNOME
only rotates in touch mode.  On the X710 the cover's `Samsung Book Cover
Switch` device reports `SW_TABLET_MODE=0` in typing posture and when closed,
so GNOME stays in laptop mode and does not rotate.  Detaching the cover sets
`SW_TABLET_MODE=1` and gives rotation (confirmed); folding it behind the
tablet should do the same, but that posture is not confirmed on the EF-DX720
yet.
