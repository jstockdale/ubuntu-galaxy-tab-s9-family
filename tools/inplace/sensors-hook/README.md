# Sensors sleep hook kit (SM-X710 and SM-X910, November 2026)

A record of an in-place install, not an install path (see
[`../README.md`](../README.md)): the device package's system-sleep hook for
the sensors, and the resume unit it restarts, put on a tablet that cannot
take the device package carrying them yet: while the Type-C roles kit is
installed on the SM-X710 test unit, its device package must not be upgraded
(the package's `qcom_spss_irq.ko` is signed for another kernel).

## What it installs

Three files of `ubuntu-gts9u-device`, byte for byte as the package source of
the checkout that packed the kit has them:

| file | what changes |
|---|---|
| `/usr/lib/systemd/system-sleep/ubuntu-gts9u-sensors` | before every system sleep: the resume unit, `iio-sensor-proxy` and `hexagonrpcd-adsp-sensorspd` stopped in that order, each waited for 10 s at most; after it: the resume unit's failed state and start count reset, then a restart with `--no-block`. Only where the resume unit can run (it loads, `ssccli` is installed) |
| `/usr/libexec/ubuntu-gts9u-sensors-resume` | comments and its last message |
| `/usr/lib/systemd/system/ubuntu-gts9u-sensors-resume.service` | a failed recovery is tried again after 30 s, three starts at most in 15 minutes; its stop waits 5 s at most |

Why: with the sensor streams open, the audio DSP left its sleep 30 to 50
times a second through a system sleep, and the DDR its low-power mode with
it; with both clients stopped by hand it woke 0 times in a 30 s sleep
([`docs/gts9wifi-sensors.md`](../../../docs/gts9wifi-sensors.md#across-a-system-suspend)).
The hook before this one (the package's up to now) stopped neither client.

On 2026-11-24 both test units had the same three files: device package 2.69
on the SM-X710 (squirtle), 2.72 on the SM-X910 (blastoise), the hook from
before ac38911 (sha256 `e5546e9f…`), and the same resume script and unit.
`install.sh` knows those, and ac38911's hook; it refuses anything else.

## Make it, copy it

On the host, from the checkout to install from:

```sh
sh tools/inplace/sensors-hook/make-kit.sh
tar -C ~/ubuntu-gts9u/backups/sensors-hook-kit --owner=0 --group=0 -cf - . |
  ssh jstockdale@10.42.0.2 'rm -rf ~/sensors-hook && mkdir ~/sensors-hook && tar -C ~/sensors-hook -xf -'
```

`make-kit.sh` writes the kit to `~/ubuntu-gts9u/backups/sensors-hook-kit`
(another directory as its argument): the scripts, the three files under
`files/` with their modes, `REVISION`, `SOURCE` (the commit) and
`SHA256SUMS`.

## On the tablet

```sh
sudo sh ~/sensors-hook/install.sh           # no reboot
sh ~/sensors-hook/verify.sh                 # no sudo
sudo sh ~/sensors-hook/cycle.sh hand        # the hook's two steps, no sleep
sudo sh ~/sensors-hook/cycle.sh freezer     # around one pm_test=freezer cycle
sudo sh ~/sensors-hook/rollback.sh          # the package's own files back
```

- `install.sh` checks the board (either), the kit's checksums, dpkg, the
  package, and that each file is the package's and holds a known version or
  a revision of this kit, before it changes anything. The first install
  saves the three files in `/var/backups/ubuntu-gts9u-sensors-hook`; a later
  revision installs over an earlier one in place and keeps those. Each file
  is written under a hidden name, root-owned with its mode, and renamed into
  place: the hook, the script, then the unit and a daemon-reload. The log
  is `/var/log/gts9u-sensors-hook.log` (group adm).
- `verify.sh` says whose each file is, how systemd has the unit loaded, what
  the sensors are doing, and for every system sleep in this boot's journal
  how long after the return they were ready again.
- `cycle.sh hand [SECONDS]` runs the installed hook's `pre`, holds the
  clients stopped (10 s by default), runs its `post`, and waits for the
  sensors. `cycle.sh freezer` runs one suspend through `systemd-sleep`
  itself, so every hook runs around it, with `pm_test=freezer`: the tasks
  are frozen for 5 s, no device suspends, the SoC does not sleep. Both end
  with `SENSORS BACK after N s` or `SENSORS NOT BACK ...`.
- `rollback.sh` puts the saved files back (unit first) and retires the
  backup; it stops, with nothing changed, at a file that is neither the
  original nor a revision of the kit.

A device package installed later replaces the three files with its own
(they are not conffiles): the kit is then out of the way, `verify.sh` says
the files are the package's, and `rollback.sh` only retires the backup.
While the kit's files are in place, `dpkg --verify ubuntu-gts9u-device`
lists them as changed.

The Type-C roles kit's `sleep-test.sh sensors=hook` runs this hook's `pre`
before its rtcwake sleeps; it knows the hook by the text `systemctl stop
iio-sensor-proxy.service`, which this hook keeps, so revisions 12 to 14 of
that kit need no change for it.

## Results

On the SM-X710 (squirtle), 2026-11-24 and 25; the logs are in
`~/ubuntu-gts9u/backups/`.

- **Install**, 2026-11-24 16:24 (boot `74dba99a`, device package 2.69, the
  Type-C kit's revision 12 running; `squirtle-sensors-hook-1-install.log`):
  revision 1, from 7a03a68. The package's three files were saved, the kit's
  three put in place, root-owned with their modes, and systemd had the unit
  as `Restart=on-failure TimeoutStopUSec=5s NeedDaemonReload=no
  StartLimitBurst=3`; no reboot. `verify.sh`: every check `ok`, and
  iio-sensor-proxy reported `HasAccelerometer = true` and `HasAmbientLight =
  true`.
- **`cycle.sh hand 10`**, 16:27 (`squirtle-sensors-hook-2-hand.log`): after
  `pre`, iio-sensor-proxy `failed` (it ends by SIGKILL), the file server and
  the resume unit `inactive`; after `post`, `SENSORS BACK after 45 s (SSC
  ready 14.0 s after it; ubuntu-gts9u-sensors-resume.service result success,
  restarts 0; iio-sensor-proxy active, HasAccelerometer true)`. The 45 s
  include the resume unit's 30 s handover watch: SSC was ready 14.0 s after
  `post`.
- **`cycle.sh freezer`**, 16:32, over Wi-Fi (`squirtle-sensors-hook-3-freezer.log`):
  one suspend through `systemd-sleep`, every hook around it, with
  `pm_test=freezer`: `systemd-sleep returned 0`, `SENSORS BACK after 44 s
  (SSC ready 13.1 s after it; ...)`, and `verify.sh` found the sleep in the
  journal: `back after 5.2 s; sensors ready 14.2 s after it (attempt 1)`.
- **Real sleeps with the hook's `pre`**: the Type-C kit's `sleep-test.sh
  sensors=hook`, which runs the `pre` step alone (the restart that ends each
  run brings the clients back, so `post` is not seen there). Two
  suspend-to-idle sleeps on revision 12 (17:03 and 17:43, the USB cable out),
  all nine of revision 13 and the first of revision 14 (2026-11-25 01:30)
  ran with it ([Type-C kit](../typec-roles/README.md), "Revision 12:
  results" to "Revision 14: results"). In each the hook stopped both clients
  with status 0 (`iio-sensor-proxy failed, hexagonrpcd-adsp-sensorspd
  inactive`), and no run stopped at it. In every one of those sleeps that
  came back with no sound stream open, the audio DSP slept through it
  (`adsp +0` with the DSP asleep for the whole sleep: 34.7 s at 17:43, 34.4
  s on revision 14), where it had woken 1,439 times in 49.7 s with the
  clients running (the Type-C kit's revision 10, run 5, 2026-11-23).
- **An open sound stream alone keeps the audio DSP awake.** With the
  clients stopped by the hook, the 17:03 sleep, with a playback and a
  capture stream open, and revision 13's run 7 (22:24), with a playback
  stream held open on purpose, had the audio DSP awake for the whole sleep
  and the DDR at 547 MHz throughout (`ddr +0`). Stopping the sensor clients
  does not quiet the DSP while a stream stays open.

Not done yet:
- rotation and auto-brightness checked by the owner at the tablet after a
  sleep (`~/ubuntu-gts9u/backups/sensors-hook-2026-11-24.md`, section 8,
  step 5);
- a real sleep through `systemctl suspend`, so that the hook's `post` runs
  after a real sleep (it has run only after `cycle.sh hand` and the
  `pm_test=freezer` cycle);
- the plan's optional step 6 (stopping the proxy alone) and the resume unit's
  retries (no recovery has failed by itself);
- the SM-X910 (blastoise): the kit is not installed there.
