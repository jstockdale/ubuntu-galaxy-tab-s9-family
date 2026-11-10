# Hang-log kit (SM-X710, November 2026)

This kit goes on top of the [camera-bus kit](../cam-bus/README.md), which sits
on the [BT UART kit](../bt-uart/README.md). It makes a system-wide hang leave
evidence. It changes nothing the camera-bus kit set: the 400 kHz CCI pads,
`rear_cam_vio`'s 10 ms, the UFS CX vote and the dw9808 fixes all stay.

**Why.** At 00:19 on 2026-11-10 squirtle hung while an untested 2048x1536
stream ran ([porting log](../../docs/porting-log.md#the-0019-hang-left-a-console-record-2026-11-10)):
- 30+ tasks were in D state;
- the disk had stalled, so the journal kept nothing;
- the console record in ramoops survived the forced restart, but it was
  decayed, and it held err-level lines only.

Its last line is the interconnect BCM voter's `Error sending AMC RPMH
requests (-110)`. The WARN backtrace printed just before it never reached the
console.

**What changes:**
- **Kernel:** the BT UART kernel, rebuilt with these options
  (`kernel/config/config-gts9wifi.fragment`). None of them ever panics.
  - the hung-task detector at 60 s, with its blocker dump (it names the task
    that holds the mutex a hung task waits for);
  - the soft-lockup detector, the buddy hard-lockup detector and the
    workqueue watchdog;
  - netconsole as a module, with configfs targets;
  - pstore ftrace.
- **The ramoops region:** the same address, `sec-pmsg@880900000`, which is
  stock's pmsg carve-out. The DTS comment explains why it stays.
  - six 128 KiB oops/panic records, a 1 MiB console, 128 KiB of pmsg
    (`/dev/pmsg0`) and 128 KiB of ftrace;
  - ECC 64 instead of 16. At the decay measured after the forced restart
    (about 10 % of bytes), ECC 16 recovered 3 blocks of about 96. A Poisson
    model puts ECC 64 at 99.7 %.
  - The first boot on the new layout reads the old contents as noise.
- **`/etc/sysctl.d/60-gts9u-hang-log.conf`:**
  - `kernel.watchdog = 1`. ABL appends `nowatchdog`, which would leave both
    lockup detectors off.
  - `kernel.printk = 5 4 1 7`, so that warnings, and WARN backtraces with
    them, reach the ramoops console. The cost is the 115200 debug UART: the
    busiest minute on 2026-11-09 had 16 warning lines.
- **The module tree** comes from this build and is signed with its own key,
  which is archived in the key registry. The configuration change moves
  `task_struct` (the hung-task fields) and `net_device` (netpoll), and the
  kernel has no `MODVERSIONS`. So a new key is what stops a module from
  another build loading into the wrong layout: the kernel refuses it.

The ftrace zone stays off. Integrity lockdown refuses the debugfs switch, so
the only way to record is `pstore.record_ftrace=1` on the command line, which
traces every kernel function. It is there for a reset that leaves nothing
else.

## Build (host)

```sh
X=/path/to/scratch/build-hang     # on the external drive, no spaces
git worktree add --detach $X/source <commit>
DEVICE=gts9wifi UBUNTU_WORKDIR=$HOME/ubuntu-gts9u TMPDIR=$HOME/ubuntu-gts9u/tmp \
  KERNEL_WORKTREE=$X/linux-src-gts9wifi KERNEL_BUILD_DIR=$X/linux-gts9wifi \
  KERNEL_OUT_DIR=$HOME/ubuntu-gts9u/out/kernel-gts9wifi-hang \
  bash $X/source/scripts/build-mainline-kernel.sh
bash tools/inplace/hang-log/make-kit.sh $X \
  $HOME/ubuntu-gts9u/out/kernel-gts9wifi-hang \
  $HOME/ubuntu-gts9u/out/kernel-gts9wifi-btirq2 \
  $HOME/ubuntu-gts9u/backups/cam-bus-kit \
  $HOME/ubuntu-gts9u/backups/hang-log-kit
```

Start from a fresh build directory, so that kbuild makes a new key; the build
archives it (`scripts/kernel-key-registry.sh add`). `make-kit.sh` first checks
that its recipes reproduce today's images, byte for byte, when the changes are
dropped:
- the BT UART kernel plus the camera-bus DTB gives the camera-bus kit's
  `boot.img` (`5529f6e7…`);
- repacking the camera-bus `vendor_boot` with its own DTB gives itself
  (`51777e20…`);
- the camera-bus commit's DTS (52cb2fc), compiled in this build's tree, gives
  the camera-bus DTB (`4ed9c1d9…`).

It then stops unless every change is one of the intended ones:
- the configuration differs from the BT UART kernel's only in the symbols
  listed in the script (the kit carries the result as `CONFIG.diff`);
- the DTB differs only in the ramoops node (`DTB.diff`);
- the module list differs only by netconsole, and every module carries the
  new key;
- in `boot`, only the kernel and its appended DTB differ; in `vendor_boot`,
  only the DTB.

## Install, check, roll back (tablet)

Copy the kit to `~/hang-log`, then:

```sh
sudo sh ~/hang-log/install.sh       # then: sudo reboot
sh ~/hang-log/verify.sh             # kernel, key, detectors, ramoops, camera DTB, netconsole
sudo sh ~/hang-log/verify.sh hung-test   # the detector reports a blocked task (about 2 min)
sudo sh ~/hang-log/rollback.sh      # back to the camera-bus kit; then: sudo reboot
```

`install.sh` stops before writing anything unless all of these hold:
- the kit matches its checksums;
- the BT UART and camera-bus kits are installed: their markers exist,
  `boot` holds the camera-bus image (`5529f6e7…`) and `vendor_boot` holds
  its image (`51777e20…`);
- the running kernel is the BT UART build (build ID `e862f606…`);
- root is writable;
- the battery is at 30 % or more;
- dpkg is idle and clean;
- there is 600 MB free.

It saves `boot`, `vendor_boot` and the module tree to
`/var/backups/ubuntu-gts9u-hang-log/` (root-only, hash-checked). It then:
1. swaps the module tree;
2. installs the sysctl drop-in;
3. writes `vendor_boot`, then `boot`, synchronously, reading each back;
4. keeps the saved Ubuntu boot set in step.

Reboot right away: the running kernel refuses the new tree's modules.

If the tablet does not come back:
1. Boot TWRP.
2. Write `vendor_boot-cam-bus.img` and `boot-cam-bus.img` from
   `/var/backups/ubuntu-gts9u-hang-log/` back to their partitions
   (`install.sh` has the commands).
3. On the next boot, run `rollback.sh`. Until you do, the old kernel refuses
   the kit's module tree, so v4l2loopback (the camera relays) and the
   netfilter modules are missing. Wi-Fi (ath11k), USB networking and
   Bluetooth are built in.

`dpkg -V ubuntu-gts9u-device` reports `updates/qcom_spss_irq.ko` as changed
while the kit is installed, as under the earlier kernel kits. A later device
package would put its own copy back, signed with the old key. That copy
would not load, but nothing on the X710 loads it anyway: the fingerprint
stack is gated to the SM-X910.

`hung-test` checks the detector end to end, without touching the hardware:
1. It makes a 16 MB ext4 in `/run` and freezes it.
2. It starts a write that blocks in D state.
3. It waits for the `INFO: task sh:<pid> blocked for more than 60 seconds`
   report.
4. It thaws and removes the file system, and gives back the report it used
   (each boot has 10).

## Before a risky test: `hang-capture.sh` (host)

[`../cameras/hang-capture.sh`](../cameras/hang-capture.sh) runs on the host,
in two terminals, started **before** the test:

```sh
sh tools/inplace/cameras/hang-capture.sh log <user>@<tablet> [DIR]
sh tools/inplace/cameras/hang-capture.sh shell <user>@<tablet>
```

- **`log`** streams `dmesg -w` over SSH into `DIR/hang-<tablet>-<time>.log`,
  with a tick every 30 s that shows userspace is still alive. While it runs,
  it raises the console loglevel to 7, so that hung-task stacks and sysrq
  dumps (KERN_INFO) also reach ramoops, and it turns on all-CPU backtraces
  for hung-task reports. Ctrl-C restores both.
- **`shell`** opens a root shell with no profile, in `/`, ready before the
  hang. During the 00:19 hang, ping and an open SSH session kept working, but
  anything that needs the disk (sudo, a new login, `ls`) would have blocked.
  `echo` is built into the shell and `/proc` is not on the disk, so this
  still works:

  ```sh
  echo w > /proc/sysrq-trigger     # blocked tasks with stacks
  echo l > /proc/sysrq-trigger     # every CPU's backtrace
  echo 'what you saw' > /dev/pmsg0 # a note the next boot keeps
  echo c > /proc/sysrq-trigger     # last: panic, pstore gets the log, reset after 10 s
  ```

  Prefer `c` to holding the power key. The forced restart decayed about 10 %
  of the ramoops RAM. The 2026-11-07 checks found warm reboots and watchdog
  resets left the record intact, and a panic resets the same way (`panic=10`).

Each mode asks for the tablet's sudo password once. `HANG_SSH_OPTS` adds ssh
options, for example `-o HostKeyAlias=<USB address>` to check the known key
when you connect over Wi-Fi. The host key is always checked strictly.

**netconsole.** `netconsole` sends the kernel log over UDP from printk
itself, so it keeps working if sshd or the disk does not:

```sh
sh tools/inplace/cameras/hang-capture.sh netconsole <user>@<tablet> [PORT [DIR]]
sh tools/inplace/cameras/hang-capture.sh netconsole-off <user>@<tablet>
```

It works out which interface and next-hop MAC lead back to the host. It then
loads the module, creates a configfs target, and listens on UDP (6666 by
default) into `DIR/netconsole-<tablet>-<time>.log`.
- By default it refuses a Wi-Fi interface. mac80211 transmits under
  BH-disabling locks, while netpoll calls it with interrupts off, and that is
  untested here. Use the USB link, or pass `--allow-wifi`.
- netconsole is a console, so it carries what the console loglevel lets
  through: warnings with the kit's sysctl, everything while `log` runs.

## After a hang

1. On the next boot, before anything else:

   ```sh
   sudo ls -la /var/lib/systemd/pstore
   sudo ls /var/lib/ubuntu-gts9u/pstore
   ```

   The archive keeps the last 10 boots, readable by group adm.
   - `console-ramoops-0` is the hung boot's console.
   - `dmesg-ramoops-*` appears after a panic (sysrq `c`).
   - `pmsg-ramoops-0` holds the notes.
   - The record ends with its ECC line, `ECC: N Corrected bytes, M
     unrecoverable blocks`.
2. Keep the host's `hang-*.log` and `netconsole-*.log` with them.
3. TWRP's `/proc/last_kmsg` (Samsung's `sec_log` ring, another console) is a
   third copy.

## Physical checks still to run (owner)

1. After installing and rebooting: `verify.sh`, with and without sudo. Then
   the BT UART kit's `verify.sh`, `sudo sh ~/cameras-kit/camera-test.sh rear`
   at a known size, audio, and Wi-Fi.
2. `sudo sh ~/hang-log/verify.sh hung-test`.
3. A dry run of `hang-capture.sh log` and `shell` on a healthy tablet: type
   `echo w > /proc/sysrq-trigger`, and the dump must appear in the log file.
4. Optional, once, to prove the whole chain. On a healthy tablet with nothing
   unsaved, run `sync` in the shell, then `echo c > /proc/sysrq-trigger`.
   - Expect a reset within about 10 s.
   - On the next boot, expect a `dmesg-ramoops-0` holding `sysrq: Trigger a
     crash`, and a console record with few or no unrecoverable blocks.
