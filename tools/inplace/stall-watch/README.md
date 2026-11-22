# Stall watch (SM-X910 and SM-X710, November 2026)

A 1 s system sampler that runs all the time and saves a self-contained record
whenever the tablet stalls, so that the next "the whole desktop froze, the
mouse stopped, then everything came back" explains itself.

It adds one service and its files. It changes nothing else: no kernel, boot
image, package, sysctl, kernel parameter or other unit, and it needs no
reboot. It never opens camera, media or input devices and never runs PipeWire
tools.

## Install, check, read, remove

Copy this directory to the tablet (for example `~/stall-watch`), then:

```sh
sudo sh ~/stall-watch/install.sh     # also upgrades any earlier revision in place
sh ~/stall-watch/verify.sh           # no sudo; 'verify.sh mark' also tests a record end to end
stall-watch status                   # alive? cost so far? what the ring spans
stall-watch list                     # recorded incidents, one line each
stall-watch report                   # the latest one: verdict, then the evidence
stall-watch report 20261122-1357     # one by (part of) its name
stall-watch at 13:48 90              # any moment the ring covers, +-90 s
stall-watch mark                     # "it just froze": save the last 2 minutes now
sudo sh ~/stall-watch/rollback.sh    # remove everything (--keep-data keeps the recordings)
```

Nothing is needed from you when a freeze happens. `status`, `list`, `report`,
`at` and `mark` need no sudo: the recordings are `root:adm`, mode 2750/0640,
and your user is in group `adm`. `mark` is optional, for a freeze the
detectors did not catch.

`install.sh` stops before writing anything unless the board is an SM-X910 or
SM-X710 (`STALL_WATCH_ANY_BOARD=1` overrides), the kit matches its checksums,
python3 is 3.8 or newer, systemd runs, `/var/log` has 300 MB free, and the
sampler can read this system once (`stall_watch.py check`, read-only). It then
installs the files root-owned, enables and restarts the service, and waits
until it has taken two samples.

Files:

| Path | What |
|---|---|
| `/usr/local/lib/gts9u-stall-watch/stall_watch.py` | the program (sampler, detectors, report) |
| `/usr/local/bin/stall-watch` | the command |
| `/etc/systemd/system/gts9u-stall-watch.service` | the service |
| `/etc/gts9u-stall-watch.conf` | thresholds and caps, all commented defaults |
| `/var/log/gts9u-stall-watch/ring/` | the continuous 1 s record, gzip, 10 min per file, 64 MB cap |
| `/var/log/gts9u-stall-watch/incidents/<id>/` | `summary.json`, `samples.jsonl`, `dump.txt` per incident, 64 MB / 200 cap |
| `/run/gts9u-stall-watch/status.json` | live status (tmpfs), every 10 s |
| `/run/gts9u-stall-watch/mark` | the FIFO `stall-watch mark` writes to (group adm) |
| `/var/lib/gts9u-stall-watch/` | install marker |
| `/var/log/gts9u-stall-watch-kit.log` | what install.sh and rollback.sh did |

Recordings contain process names, command lines (in `dump.txt`) and journal
excerpts; they are readable by root and group adm only.

## What it records

Every second, from `/proc` and `/sys` only (every reader is optional, so a
file one tablet lacks is skipped):

- PSI for cpu, memory, io and irq as deltas of the `total` counters (ms of
  stall in that second, `some` and `full`), the same for `user.slice`,
  `system.slice`, the compositor's own cgroup and its `app.slice`;
- the compositor (gnome-shell): its cgroup's memory, I/O and cpu pressure,
  the state and run-queue wait of its main thread and of mutter's
  `KMS thread` (from `schedstat`), and its GPU time (DRM fdinfo);
- meminfo (free, available, file cache, anon, shmem, unevictable, swap),
  vmstat deltas (direct reclaim entries, pages scanned and reclaimed directly
  and by kswapd, swap in/out, file and anon refaults, major faults,
  compaction stalls and failures, migration failures, THP faults and
  fallbacks, OOM kills, unevictable pages added and released (on this port
  mostly GPU buffers), shrinker scans), zram `mm_stat` and per-device swap use;
- per-CPU busy %, iowait and irq time, run queue and blocked counts, context
  switches, load; per-policy frequency and any `scaling_max_freq` cap; LMh
  (`dcvsh`) interrupts; the three hottest thermal zones and any active
  cpufreq/devfreq cooling device (every 5 s); devfreq target frequencies
  (GPU, UFS);
- block devices: I/O, kB, ms, in-flight and busy ms per second;
- the six busiest interrupt lines, display interrupts (msm-kms, DSI) and
  input interrupts (touch, USB, the BT UART), and whether the screen is on;
- every 5 s (every second while escalated): the top 5 processes by CPU and by
  RSS, processes in D state, and big processes that exited;
- kernel log lines at warning or worse, or matching display, GPU, lockup,
  memory, storage, bus or RPMh patterns, read live from `/dev/kmsg`;
- the sampler's own wake-up lateness, work time and run-queue wait.

## Detectors

A sample is flagged when (per second of wall time):

| Flag | Condition | Meaning |
|---|---|---|
| `late` | the sampler woke ≥ 200 ms after its deadline, not across a suspend | the whole machine (or at least the CPU it was on) stalled: the sampler is real-time and memory-locked, so only the kernel, interrupts-off sections or higher RT priorities delay it |
| `slow` | one sample's reads took ≥ 200 ms of the sampler's time | a `/proc` or `/sys` read blocked inside the kernel |
| `mem` | PSI memory `full` ≥ 100 ms | every runnable task was blocked on memory for a tenth of the second |
| `io` | PSI io `full` ≥ 200 ms | the same for storage |
| `cpu` | PSI cpu `some` ≥ 950 ms for 5 s in a row | CPU saturation |
| `desktop` | the compositor's cgroup memory+io `some` ≥ 100 ms, or its main thread in D for 2 samples, or its run-queue wait ≥ 300 ms | the desktop itself was blocked |
| `kernel` | a kernel line of one of the classes above | |
| `oom` | `oom_kill` went up | |
| `display_silent` | input kept coming (≥ 30 irq/s, screen on) while display interrupts stopped (< 2/s) after flowing | soft signal, recorded but never opens an incident alone |

PSI triggers (`poll()` on `/proc/pressure/memory`, `some 150 ms / 1 s`, and
`io`, `full 150 ms / 1 s`) wake the sampler the moment pressure crosses the
line, for an extra sample. A wakeup counts only if the totals show at least a
quarter of the threshold (the trigger alone never makes an incident).

Any flag except `display_silent` opens an incident with the last 120 s of the
ring. While it is open the sampler is escalated for 10 s after each flag:
process scan every second, and a helper thread lists threads in D state with
`wchan` and kernel stack (and the compositor's two threads' stacks) at most
every 2 s. When the flags stop, a state dump is taken at once (meminfo,
vmstat, PSI, buddyinfo, zram, `ps`, cgroups by memory with their pressure and
events, GPU memory per DRM client, the compositor's threads, interrupts). 30 s
later the incident closes; the journal for the window (kernel, warnings, and
the compositor, browsers, oomd, journald lines) is added, and the record is
written if it was significant: memory `full` ≥ 500 ms, io `full` ≥ 1 s,
lateness ≥ 500 ms, compositor stall ≥ 300 ms, a kernel line, an OOM kill or
sustained cpu. Smaller episodes are counted, not saved.

The verdict names the class (whole machine, GPU, memory, I/O, desktop,
display, CPU, OOM) and gives the numbers behind it, then the per-second
evidence table, the blocked threads with their stacks, the largest processes
and the kernel lines.

## Why it keeps running when the tablet is starved

- **SCHED_FIFO priority 1** (from the unit): it preempts every ordinary
  task at once, and only the kernel, interrupts and the interrupt threads
  (FIFO 50) are above it. Its work is about 1 ms per second, so it cannot
  starve anything. Helper threads start as ordinary tasks
  (`CPUSchedulingResetOnFork`) and drop to nice 10.
- **mlockall** after start: no page of the program, Python or its libraries
  can be reclaimed, so it never faults on a page that reclaim took away (the
  freezes this is for are memory stalls). About 25-30 MB stay locked.
- **No forks and no driver calls in the loop**: it reads proc/sysfs counters
  with `pread` on kept-open descriptors; sensor and devfreq files are
  time-guarded (a read slower than 50 ms is never repeated); driver-backed
  cooling devices (Wi-Fi) are not read at all.
- **Disk writes are in another thread**: a stuck write cannot delay a sample.
  The ring goes to disk once a minute (every 10 s, synced, during an
  incident), gzip-compressed (about 15-25 MB a day).
- `OOMScoreAdjust=-900`, `MemoryMax=192M` as a leak guard (it also exits
  and is restarted above 128 MB resident).

What it cannot see: a stall with interrupts off on every CPU, or a kernel
hang, stops the sampler too. That case still shows: the record has a gap
(`late` of seconds on the next sample, the `dt` column), and the ring has
everything up to the last flush. On this kernel the hung-task, soft-lockup and
workqueue detectors are not built (only the RCU stall detector, 21 s), so the
hang-log kit's kernel is the complement for real hangs.

## Cost

Measured on the build host while writing it: an Intel i3-N305, 350
processes, against the real `/proc`, unprivileged, 5 minutes.

- CPU: 0.28 % of one CPU, start-up included (0.83 s of CPU in 300 s).
- Per sample: 1.1 ms without a process scan, 6-8 ms with one (every 5 s).
- Lateness: p99 2.2 ms. Missed intervals: none.
- Memory: 26 MB resident.
- Disk: about 120 bytes per sample once compressed, so about 10 MB a day; the
  64 MB ring holds about 6 days.

On the tablet expect 0.2-0.6 % of one core (the A510 little cores are 2-3
times slower than the host for this; the A715/X3 about the same), and more
while an incident is open. `stall-watch status` shows the real numbers.

Real-time tasks on this kernel default to `uclamp_min 1024`, so each 1-2 ms
sample runs at full frequency, probably on a big core. If power matters more
than starvation-proofing, use `CPUSchedulingPolicy=other` and `Nice=-20` in
the unit; lateness is then a weaker signal.
