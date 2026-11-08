# suspendkit: suspend reliability runs and the ADSP-start sleep lock (SM-X710)

A record of an experiment on one SM-X710 test unit, not an install path (see
[`../README.md`](../README.md)).  Device package 2.56 ships the ADSP-start
sleep lock; the runners are also in `scripts/diagnostics/suspend/`.

For a tablet on the Stage 2 kernel with device package 2.55 (suspend mode off,
`sleep.target` and `suspend.target` masked). Nothing here changes the suspend
policy, the masks, the kernel or a package version.

| In the kit | Install? | What |
|---|---|---|
| `gts9u-suspend-cycles.sh` | no, runs from `~/suspendkit` | RTC-woken s2idle and deep cycles with a checklist, key wakes, RTC alarm test, `status` |
| `x710-suspend-ladder.sh` | no | the step-by-step ladder for bisecting a failed cycle |
| `files/ubuntu-gts9u-adsp-start-inhibit{,.service}` | `install.sh` | the ADSP-start sleep lock, as the next device package will ship it |
| `install.sh`, `rollback.sh`, `verify.sh` | | in the style of the fix1 kit on the test unit (not published) |

Background, the checklist and how to read results:
`scripts/diagnostics/suspend/README.md` in the repository.

## Copy it over (host)

```
sh tools/inplace/suspendkit/build-kit.sh /tmp/suspendkit
tar -C /tmp/suspendkit -cf - . | ssh <user>@10.42.0.2 'mkdir -p ~/suspendkit && tar -C ~/suspendkit -xf -'
ssh <user>@10.42.0.2 'cd ~/suspendkit && sha256sum -c --quiet SHA256SUMS && echo kit ok'
```

`<user>` is the desktop user on the tablet; 10.42.0.2 is the development USB
gadget's address (an image built with `GTS9U_USB_GADGET=1`).

## On the tablet, in this order

Every sleeping step runs detached (`gts9u-suspendkit.service`), so the USB
SSH link may drop while the tablet sleeps; reconnect and read `status`. A run
refuses to start in the first 90 s of a boot, while the coldboot cycle or the
DSP start is still going, or while another run is active.

1. **Preflight** (never sleeps):
   `sudo sh ~/suspendkit/gts9u-suspend-cycles.sh check`
   Expect `pm8xxx_rtc_alarm: IRQ 21`, `pmic_pwrkey: IRQ 22`,
   `pmic_resin: IRQ 23`, rtc0 wakeup enabled, `gem_noc=0 mc_virt=0`, policy off.
2. **RTC alarm while awake** (25 s, the first time IRQ 21 is ever exercised):
   `sudo sh ~/suspendkit/gts9u-suspend-cycles.sh rtc-check`
   Expect `rtc-check: ok, IRQ 21 pm8xxx_rtc_alarm N -> N+1, wakealarm cleared`.
3. **Smoke run**, two s2idle cycles (about 3 min):
   `sudo sh ~/suspendkit/gts9u-suspend-cycles.sh run --s2idle 2 --deep 0`
   then `sh ~/suspendkit/gts9u-suspend-cycles.sh status`.
   Expect two `PASS` lines (`entered=s2idle wake=21(pm8xxx_rtc_alarm)`),
   `RESULT: PASS`.
4. **Full run**, 20 s2idle then 20 deep (about an hour: before each sleep
   it lets the last resume's sensor recovery finish, typically 50 to 70 s
   after the resume):
   `sudo sh ~/suspendkit/gts9u-suspend-cycles.sh run`
   Leave the tablet alone; `status` any time. Deep has resumed once on the
   baseline kernel but never on Stage 2. If the tablet does not come back
   within a minute of a sleep, press Power once first: if that wakes it, the
   RTC alarm cannot wake it from that mode (the cycle fails with
   `wake=22(pmic_pwrkey)`), which is the finding. Only if Power does nothing
   within 30 s, hold Power + Volume Down until it restarts; `status` then
   names the cycle that never returned.
5. **Key wakes**, at the tablet:
   `sudo sh ~/suspendkit/gts9u-suspend-cycles.sh key power` and press Power
   once after the panel goes dark (within 90 s of that; the panel may take up
   to two minutes to go dark if a sensor recovery is still running); then
   `sudo sh ~/suspendkit/gts9u-suspend-cycles.sh key voldown` and press Volume
   Down once. `status` after each: `wake=22(pmic_pwrkey)`, then
   `wake=23(pmic_resin)`, `RESULT: PASS`. The Volume Down press also lowers
   the volume one step.
6. **Optional, sleep depth**, only after 4 passed (irreversible until reboot):
   `sudo sh ~/suspendkit/gts9u-suspend-cycles.sh run --release-icc --s2idle 5 --deep 5`
   The summary then shows aosd/cxsd/ddr counts and the share of time in CX
   collapse; on Stage 2 without the release they stay at +0. A wedge is
   possible (an early interconnect sync hung the boot cycle at Stage 3): a
   forced restart returns to plain Stage 2.
7. **ADSP-start sleep lock** (optional now; it matters once a suspend mode is
   on): `sudo sh ~/suspendkit/install.sh`
   Expect `smoke test: ok`. After the next reboot, whenever that is:
   `sh ~/suspendkit/verify.sh`, expecting `held off until the audio DSP is up`
   and `audio DSP up after N s`, and the lock ready before the DSP start.

To stop a run after the current cycle:
`sudo sh ~/suspendkit/gts9u-suspend-cycles.sh stop` (or `sudo systemctl stop
gts9u-suspendkit`).

## Rollback

- Runs leave nothing behind: the one-cycle drop-in in `/run/systemd/sleep.conf.d`
  and the RTC alarm are removed and `mem_sleep` and `pm_debug_messages` are
  restored when a run ends, whichever way it ends; a reboot clears `/run` too.
  Logs stay in `/var/log/gts9u-suspendkit/` (delete with
  `sudo rm -r /var/log/gts9u-suspendkit` if wanted).
- `--release-icc`: reboot.
- The lock: `sudo sh ~/suspendkit/rollback.sh` (no reboot needed). It leaves a
  file alone once a device package owns it.
