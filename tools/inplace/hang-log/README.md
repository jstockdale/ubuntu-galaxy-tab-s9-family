# Hang-log kit (SM-X710, November 2026)

This kit goes on top of the [camera-bus kit](../cam-bus/README.md), which sits
on the [BT UART kit](../bt-uart/README.md). It makes a system-wide hang leave
evidence. It changes nothing the camera-bus kit set: the 400 kHz CCI pads,
`rear_cam_vio`'s 10 ms, the UFS CX vote and the dw9808 fixes all stay.

**Why.** At 00:19 on 2026-11-10 squirtle hung while an untested 2048x1536
stream ran ([porting log](../../docs/porting-log.md#the-0019-hang-left-a-console-record-2026-11-10)):
- 30+ tasks were in D state;
- the disk had stalled, so the journal kept nothing;
- the console record in ramoops survived the forced restart, but it held
  err-level lines only, and about 10 % of its bytes had decayed.

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
  - pstore ftrace;
  - pstore compression off, so that a panic record stays legible when ECC
    cannot repair every block.
- **The ramoops region:** the same address, `sec-pmsg@880900000`, which is
  stock's pmsg carve-out. The DTS comment explains why it stays.
  - three 256 KiB oops/panic records (plain text), a 1 MiB console, 128 KiB
    of pmsg (`/dev/pmsg0`) and 128 KiB of ftrace;
  - ECC 96 instead of 16, because every reset decays this RAM, clean
    reboots too. Aligned against the journal, five boots' records had 7 to
    20 % of their bytes changed, mostly by cleared bits, evenly through each
    record. ECC 16 left 77 to 99 % of blocks unrecoverable. A Poisson model
    keeps 99.9 % of blocks at 13.5 % with ECC 96, and two thirds at 20 %.
    The parity takes 43 % of each zone.
  - The first boot on the new layout reads the old contents as noise.
- **`/etc/sysctl.d/60-gts9u-hang-log.conf`:**
  - `kernel.watchdog = 1`. ABL appends `nowatchdog`, which would leave both
    lockup detectors off.
  - The console loglevel stays at Ubuntu's 4. On this tablet each line the
    consoles print costs 0.1 to 0.25 s with interrupts off: the three err
    lines of a hung-task report came 231, 108 and 243 ms apart. The first
    version of this drop-in also set `kernel.printk = 5 4 1 7`, which put
    every warning, ath11k's `msdu_done` bursts in the Wi-Fi receive path
    among them, through that cost; it is gone. A panic writes the log's tail
    to pstore whatever the loglevel.
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
sh tools/inplace/cameras/hang-capture.sh shell <user>@<tablet> [DIR]
```

- **`log`** streams `dmesg -w` over SSH into `DIR/hang-<tablet>-<time>.log`,
  with a tick every 30 s that shows userspace is still alive. `dmesg -w`
  gets every line whatever the console loglevel. While it runs:
  - it turns on all-CPU backtraces for hung-task reports;
  - it traces RPMh requests and completions (`rpmh_send_msg`,
    `rpmh_tx_done`), interconnect votes (`icc_set_bw`, `icc_set_bw_end`) and
    GMU frequency changes (`msm_gmu_freq_change`) into a 16 KB-per-CPU
    ftrace buffer. The 00:19 hang ended in an interconnect RPMh timeout,
    after a day of GMU bandwidth-vote timeouts. A request with no completion,
    or a vote with no end, is the stuck one. The shell reads the buffer;
    `--no-trace` skips this.
  - `--console-level 7` also raises the console loglevel, so that hung-task
    stacks and sysrq dumps reach the ramoops console. Each extra console line
    then costs 0.1 to 0.25 s with interrupts off, so it is off by default.

  Ctrl-C restores all of it.
- **`shell`** opens a root shell with no profile, in `/`, ready before the
  hang, and logs what it shows into `DIR/shell-<tablet>-<time>.log`. During the 00:19 hang, ping and an open SSH session kept working, but
  anything that needs the disk (sudo, a new login, `ls`) would have blocked.
  `echo` is built into the shell and `/proc` is not on the disk, so this
  still works:

  ```sh
  echo w > /proc/sysrq-trigger     # blocked tasks with stacks
  echo l > /proc/sysrq-trigger     # every CPU's backtrace
  echo 'what you saw' > /dev/pmsg0 # a note the next boot keeps
  # the trace, into the shell's log file:
  while IFS= read -r l; do echo "$l"; done < /sys/kernel/tracing/trace
  echo c > /proc/sysrq-trigger     # last: panic, pstore gets the log, reset after 10 s
  ```

  Avoid sysrq `z` (the trace) and `t` (every task). They print through the
  consoles, which here means minutes with a CPU's interrupts off.

  Prefer `c` to holding the power key, because the panic writes the log's
  tail, sysrq output included, to a plain pstore record. Both resets decay
  the RAM alike, and ECC 96 repairs most of it. If the warm-reset test below
  passed, type `echo warm > /sys/kernel/reboot/mode` before `c`.

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
  through: errors by default, more with `log --console-level`.

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
   - On the next boot, expect a plain `dmesg-ramoops-0` holding `sysrq:
     Trigger a crash`.
   - Expect a console record with few unrecoverable blocks. The ECC line at
     its end says how many.
5. The warm-reset test: does `SYSTEM_RESET2` keep DDR refreshed?
   - Linux resets with PSCI `SYSTEM_RESET`: `/sys/kernel/reboot/mode` reads
     `cold`, and the firmware is PSCI v1.1.
   - Run `echo warm | sudo tee /sys/kernel/reboot/mode`, then `sudo reboot`.
     The setting lasts until that reboot.
   - Compare the archived record's ECC line, and `ramoops: error in header`
     in the boot log, with a cold reboot's.
   - Untested: Samsung's bootloader might take a warm reset for a crash and
     show upload mode. Power + Volume Down restarts from there.
   - If the warm reset keeps the record intact, a persistent `reboot=warm`
     is the next step.
