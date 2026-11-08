# Boot hygiene kit (SM-X710, over ubuntu-gts9u-device 2.55)

A record of an experiment on one SM-X710 test unit, not an install path (see
[`../README.md`](../README.md)).

In-place install of the boot-hygiene changes on this branch over device
package 2.55. Device package 2.56 carries them, except that it shipped the
fingerprint-gate generator as `-rw-r--r--`, so fprintd still timed out after
25 s at the greeter; 2.57 fixes that. No kernel, boot image, module or
package change; one reboot to see the effect. Files only:
units, helpers, a journald drop-in, a generator, the overlay extension, and
the bring-up microSD logger where it is already installed.

## What it fixes and why

1. **The first ~39 s of every boot was missing from the journal.** Not the
   256M cap by itself: the clock. Until `ubuntu-gts9u-rtc-restore` ran (17.8 s)
   the clock read systemd's build date (2026-10-12 07:36:16 UTC; PID 1's
   "System time before build time, advancing clock"), and journald (15.4 s)
   flushed the boot's entries at 16.6 s into a new persistent file whose head
   time and creation time were that date. At the boot's first rotation (the
   user journal's "different sequence number ID, rotating" at login, ~39 s, or
   an NTP "Time jumped backwards") journald archived that file, and its vacuum,
   which sorts by those times and deletes the oldest first while the directory
   is over SystemMaxUse (it sat at 255.9M of 256M), deleted it at once.
   Fixes: rtc-restore now runs **before journald** (it only reads the RTC and
   the saved offset from the root fs the initramfs mounted); rtc-offset `save`
   keeps the smaller of the two whole-second readings (a restored clock that is
   never ahead, so NTP never steps it back) and refreshes `/usr/lib/clock-epoch`
   (PID 1's epoch, for boots where the restore cannot run); journald gets
   `SystemMaxUse=1G` from `/usr/lib/systemd/journald.conf.d/60-ubuntu-gts9u.conf`.
   A udev rule (`70-gts9u-rtc-restore.rules`) restores the clock when rtc0
   appears later, for kernels where rtc-pm8xxx is a module (the Ultra's); on
   the X710 it is built in and the rule is a no-op.
2. **rtc-restore ran up to 3 times per boot** (16.56, 17.47, 17.76 s in the
   boot-loop logs): a finished oneshot without `RemainAfterExit` is started
   again by every later transaction that pulls in sysinit.target. Now
   `RemainAfterExit=yes`.
3. **Fingerprint stack failed on every X710 boot; GDM waited 25 s.**
   `egis_el721` registers a fallback `egis-el721` device on the X710 too, so
   the path conditions passed. fingerprint-secure, -ui and qcomtee now carry
   `ConditionFirmware=device-tree-compatible(samsung,gts9uwifi)`. fprintd is
   not ours and is D-Bus activated: a condition on it would leave dbus-daemon
   waiting out its 25 s activation timeout, so the generator
   `/usr/lib/systemd/system-generators/ubuntu-gts9u-fingerprint-gate` masks it
   for the boot on every board but the Ultra; activation then fails at once
   and GDM, the lock screen and gdm-fingerprint fall back to the password. The
   overlay extension's fingerprint part stays inactive off the Ultra (no 50 ms
   poll, no pulses, no target, no keyboard fallback); its two GNOME 46 guards
   (the closed GDM connection in `cancel()`, which fired on this X710 on
   2026-11-06, and targetless keyboard events) stay on as before.
   QTEE and SNVM are no longer loaded on the X710.
4. **pstore.** systemd-pstore moves the records to `/var/lib/systemd/pstore`,
   where the next boot replaces `console-ramoops-0` and, because the copy
   keeps the pstore file's pre-restore time stamp, `systemd-tmpfiles-clean`
   deletes it 15 minutes after boot (14-day rule). That is why the directory
   was always empty. New `ubuntu-gts9u-pstore-archive.service` (sysinit, after
   systemd-pstore, synced to disk) keeps each new record in
   `/var/lib/ubuntu-gts9u/pstore/<seq>-<UTC>-boot-<previous boot>/` with INFO
   and SHA256SUMS, the newest 10, readable by group adm. The ramoops region
   (0x880900000) itself is fine: the console record survived every warm reboot
   and watchdog reset; zone 0 (first dmesg record) was uncorrectable only on
   the two boots after a TWRP session (Samsung's kernel there writes its
   sec_pmsg ring from offset 0); nothing points at ABL overrunning
   sec-debug-bl. No move or shrink.
5. **Bring-up microSD logger** (opt-in, `GTS9U_BRINGUP_SDLOG=1` images; this
   tablet has it): it dumped every 60 s for the whole uptime (4.6 GB, 54 boot
   directories). Now four snapshots per boot (multi-user.target, 45, 90 and
   180 s), boot directories numbered `<seq>-<bootid>` with the newest 20 kept
   (the old `<bootid>` directories are left for you to delete), and it copies
   `/var/lib/systemd/pstore` (where the records are by then) as well as
   `/sys/fs/pstore`.
6. **Panel delayed ~15 s after a TWRP session.** Both reflashes of the
   keyboard MCU on 2026-11-07 (02:13 and 02:26) were the first boots after a
   TWRP session (the controller was found off V37; the boot-loop resets in
   between never needed one), and the 15 s flash ran before the cold-boot panel
   cycle could start. The two must not overlap, but the order can be the other
   way round: `ubuntu-gts9u-pogo-firmware` now runs after the panel cycle; on a
   normal boot (already V37) it takes 0.1 s either way.

## Use

From the host (no sudo), in this repository's top directory, copy the kit
as committed at HEAD (`<user>@10.42.0.2` needs the development USB gadget):

    git archive HEAD tools/inplace/boothygiene |
      ssh <user>@10.42.0.2 'rm -rf ~/boothygiene && mkdir ~/boothygiene && tar -x --strip-components=3 -C ~/boothygiene'

On the tablet:

    sudo sh ~/boothygiene/install.sh     # checks, backs up, installs; log /var/log/gts9u-boothygiene.log
    sudo reboot
    sh ~/boothygiene/verify.sh           # no sudo; expect "0 CHECK"

`install.sh` stops without changing anything unless: the board is the SM-X710,
ubuntu-gts9u-device is 2.55, dpkg is idle, the kit checksums match, and every
file it replaces is byte-identical to 2.55's (or already the kit's). Rerunning
it is harmless. It does not restart journald or anything the session uses; it
reloads systemd (the generator masks fprintd immediately), clears the failed
fingerprint units and stops this boot's every-minute logger timer. The
fingerprint broker keeps running until the reboot on purpose: the Shell
running now still has the old overlay, which pulses it twice a second.

Rollback (restores the 2.55 files, removes the added ones and
`/usr/lib/clock-epoch` if the kit created it; the pstore archives stay):

    sudo sh ~/boothygiene/rollback.sh && sudo reboot

## Notes

- `dpkg --verify ubuntu-gts9u-device` lists the replaced files as changed
  until the next device package, which carries the same content (the added
  files are not owned by 2.55 and are taken over by that package silently).
- To run fprintd on a board other than the Ultra anyway (development only),
  make `/etc/systemd/system/fprintd.service` a symlink to
  `/usr/lib/systemd/system/fprintd.service`; /etc wins over the generator.
- The archives of the boots before this kit (Nov 5-7) carry the 2026-10-12
  time stamps and will be the first files the vacuum deletes once the journal
  reaches 1G; export them first if they matter
  (`journalctl --file=<file> -o export`).
- `make-kit.sh` (host) rebuilds `files/` and `SHA256SUMS` from the package
  sources; `tests/test_boothygiene_kit.py` checks the kit against them and
  runs install/rollback in a temporary root.
