# powerthermal kit (SM-X710 squirtle, also safe on the SM-X910)

Overnight 2026-11-07 work on power, charging, thermal and audio userspace.
This directory is the in-place half; the kernel half rides on the next kernel
build from the `overnight/powerthermal` commits.

Copy it to the tablet and check it first (from the build host):

    rsync -a tools/inplace/powerthermal/ jstockdale@10.42.0.2:powerthermal/
    ssh jstockdale@10.42.0.2 'cd ~/powerthermal && sha256sum -c SHA256SUMS'

| file | runs as | what it does |
| --- | --- | --- |
| `install.sh` | sudo | installs rtkit (apt, needs the network) and the greeter WirePlumber snippets; logs to `/var/log/gts9u-powerthermal.log`; backups in `/var/backups/ubuntu-gts9u-powerthermal` |
| `rollback.sh` | sudo | undoes exactly what `install.sh` did (rtkit only if the kit installed it) |
| `verify.sh` | user | read-only checks for both halves; `ok` / `CHECK` / `info` lines |
| `charge-watch.sh [min] [label]` | user | logs charging every 5 s to `~/powerthermal-logs`, then counts the kernel lines of the window |
| `thermal-loadtest.py` | user | sustained 3.36 GHz prime-core load with temperature, clock and LMh logging; its workers die with it (Ctrl-C, hangup, even kill -9) |
| `files/` | - | copies of what `ubuntu-gts9u-device` will ship for the greeter |
| `files/optional/60-ubuntu-gts9u-rtkit-linger.rules` | sudo, by hand | polkit rule, only if PipeWire stays SCHED_OTHER after a reboot (see below); not installed by `install.sh` |

Nothing in the kit restarts a service.  The greeter snippet applies the next
time the login screen starts (next boot).  For rtkit, see the next section.

## rtkit, linger and when PipeWire actually gets realtime

The desktop user lingers (`loginctl show-user jstockdale -p Linger` is `yes`),
so `pipewire`, `filter-chain` (the speaker DSP chain), `pipewire-pulse` and
`wireplumber` run in the user manager from boot until shutdown:

- logging out and back in does **not** restart them.  After `install.sh`,
  restart them by hand, as the desktop user, while logged in (a short audio
  gap; `filter-chain` is listed because it is BindsTo pipewire and a plain
  `restart pipewire` would leave the speakers' DSP chain stopped):

      systemctl --user restart pipewire.service filter-chain.service pipewire-pulse.service wireplumber.service

- at boot they start before anyone logs in.  rtkit asks polkit, polkit finds
  no session for a process outside one (`sd_uid_get_display` has nothing yet),
  and the stock rtkit policy says `allow_any=no`, so realtime is most likely
  refused for that whole boot.  `verify.sh` says when PipeWire started before
  the graphical session.  If the data loop is still `TS` after a reboot, the
  optional rule lets members of the `audio` group (only `jstockdale` here)
  ask rtkit from outside a session too; rtkit still caps the priority at 20,
  requires RLIMIT_RTTIME and demotes everything if its canary starves:

      sudo install -m 0644 ~/powerthermal/files/optional/60-ubuntu-gts9u-rtkit-linger.rules /etc/polkit-1/rules.d/
      sudo rm /etc/polkit-1/rules.d/60-ubuntu-gts9u-rtkit-linger.rules   # undo (rollback.sh removes it too)

  polkitd rereads its rules by itself; the effect shows at the next boot.

rtkit grants SCHED_RR, so `verify.sh` expects `RR/20`.  Mutter's KMS thread
may stay `TS` (Mutter drops it back around mode sets, and a GNOME 49 desktop
with rtkit was seen running it as TS); that line is informational.

## What was found

**The charging line every 30 s.**  `kern.log` has had `sm5714-battery 3-0049:
enabled charging for USB type 1 ...` every 30.72 s since the first X710 boot
(3,355 lines).  The SM5714's own 1 s poll is not what reopens charging.  The
SM5440 direct-charge driver retries every 30 s, and HZ=250's timer wheel
rounds that to 15 x 2.048 s = 30.72 s, which matches the measured period to
the millisecond.  Each retry finds the TCPM port "online" (the PC's USB port,
Type-C 5 V / 500 mA), opens the SM5714's Q4 (`set_direct_charge(true)`),
soft-resets the SM5440, and asks TCPM for PPS.  TCPM refuses with
`-EOPNOTSUPP` (-95) because the source has no PPS APDO.  The driver then
restores the switching charger, closing Q4 again, and that restore prints
`enabled charging`.  The `-95` warning appears only once per boot because the
driver logs once per failure streak.  The boot log shows the restore's line
immediately before the warning, 20 us apart.

Kernel fix (`sm5440_direct.c`):
- start only when TCPM reports a PPS source (`usb_type` PD_PPS or
  PD_PPS_SPR_AVS), checked from TCPM state with no I2C;
- for a PD charger without PPS, log one info line per plug-in;
- back off on repeated start failures (30 s, 1, 2, 4, 8 min), and end the
  streak on detach;
- re-evaluate about 1 s after any TCPM change (a power-supply notifier), so a
  PPS charger no longer waits up to 30 s;
- in suspend, stop the work first and then hand back.

What this does not change: a direct-charge *session* that starts and then
dies.  On this X710 with a PPS charger (kern.log, 2026-11-06 13:11-13:17)
three sessions ended the same way: TCPM dropped back to the fixed 9 V PDO
("USB-PD contract: 9000 mV, 1660 mA", once 48 ms after the PPS contract,
then after 2 min and after 47 s), so the next PPS refresh got -95 because
`pps_data.active` was false, the driver handed back ("enabled charging") and
started again 30.7 s later; the fourth ran 49 s until the cable was pulled.  Mainline
TCPM renegotiates a fixed PDO whenever the source sends Source_Capabilities
in SNK_READY, and that is the likely trigger; TCPM's own log
(`/sys/kernel/debug/usb/tcpm-4-0033/log`, root) during a PPS charge would
settle it.  The 30 s pause after a stop is unchanged, so with that charger
expect a restart every 1-2.5 minutes, not one continuous session.

Kernel fix (`sm5714_battery.c`):
- `configure_charging()` now re-checks `direct_charging` under its lock.  The
  poll, and the thermal-band change, could otherwise close Q4 behind a
  running pump; that race was real but narrow.
- `enabled charging` is printed only when the limits change or Q4 was really
  open.  TCPM's repeated identical contracts at boot become debug lines.

**Charging while current_now is negative.**  At 5 V / 500 mA (PC port) the
input is 2.5 W.  With the panel on, the tablet uses more than that, so the
pack drains while `STATUS2.CHG_ON` still says the charger runs.  UPower then
shows "charging" with a time to full while the percentage falls.  Worse, it
never raises the low/critical warnings or takes the critical action, so the
tablet would brown out.

Decision: report `Discharging` when the charger is on but the fuel gauge's
average current stays at or below -100 mA for two 10 s samples.  It goes back
after two samples at or above 0 mA, and plugging in always shows `Charging`
first.  This is what the mainline bq27xxx and sbs-battery gauges do.  The
runtime switch is `/sys/module/sm5714_battery/parameters/report_net_drain`
(root, `0` = old behaviour).

**Thermal.**
- `sm8550.dtsi` puts cdsp0-3 on tsens2 4-7, the GPU's sensors.  The stock
  kalama base DTB (extracted from the stock vendor_boot) has nspss-0..3 on
  tsens1 4-7 and gpuss-0..7 on tsens2 1-8.  The CDSP zones therefore shadowed
  gpuss-3..6, and with them the 85 C GPU cooling maps.  Fixed in the X710 DTS.
- The CPU zones have passive trips at 90 and 95 C and no maps.  CPU throttling
  is LMh's job (the dcvsh interrupts, 9 and 12 events so far this boot), as on
  stock.  The 95 C trips are now bound to the cluster cpufreq cooling devices
  as a backstop below the 110 C critical shutdown, with 100 ms passive
  polling.  The 90 C trips stay unbound.
- The XO and AP thermistors (stock `pmk8550_xo_therm` 0x44 and `ap_therm`
  0x148) are added as plain IIO channels.  You can read them without root;
  nothing acts on them yet.

**Audio userspace.**
- rtkit was not installed: PipeWire logged `RTKit error: ServiceUnknown`, and
  `pw-data-loop` and Mutter's `KMS thread` ran as SCHED_OTHER.  rtkit is now
  in the rootfs package list, and this kit installs it.  Because the desktop
  user lingers, see "rtkit, linger and when PipeWire actually gets realtime"
  above: without the optional polkit rule, PipeWire is likely to get realtime
  only after a manual restart inside the session, not at boot.
- The GDM greeter's WirePlumber took the Bluetooth profiles at boot.  The
  device package now writes a WirePlumber snippet into the gdm user's
  `~/.config/wireplumber` that turns off the greeter's bluez (and bluez-midi)
  monitor.  It is the PipeWire counterpart of gdm3's own
  `pulse/default.pa`.  ALSA stays on for login sounds and the screen reader.

## Rollback

- userspace: `sudo sh ~/powerthermal/rollback.sh` (also removes the optional
  polkit rule if it was installed), then restart the audio services as above
- the net-drain status alone, at runtime:
  `echo 0 | sudo tee /sys/module/sm5714_battery/parameters/report_net_drain`
- the kernel half: boot the previous boot/vendor_boot images (fix2-s2), as
  for every kernel test.
