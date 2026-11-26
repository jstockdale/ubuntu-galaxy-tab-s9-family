# GPU freeze and combined GPU/UFS candidate, 2026-09-09

## Physical failure

The owner reported an illuminated, unresponsive screen. Wi-Fi and SSH still
worked. The tablet ran kernel `7.2.0-rc3-dirty` build 8 with Mesa
`25.2.8-0ubuntu0.24.04.2`. Root remained `rw,noatime`, about 7.8 GiB of memory
was available, and the local ordinary-suspend safeguard was still installed.
This capture is a GPU recovery failure, not another demonstrated UFS failure.

The first GMU bandwidth-vote response timeout appeared at monotonic time
4704.977, followed by hangcheck recovery. Later failures culminated at
8872–8879 seconds in a fault from the ChatGPT GPU process, a CP translation
read at IOVA zero, and a preemption timeout. This identifies a failing submit,
not the cause of the earlier GMU failures or proof of an application bug.

The blocked kernel stacks establish the recovery dependency:

| Task | Blocked path |
| --- | --- |
| GPU recovery worker | `recover_worker -> a6xx_recover -> runtime_suspend -> a6xx_gmu_stop -> a6xx_gmu_set_oob`, waiting for `fault_coredump_done` while recovery holds `gpu->lock` |
| SMMU fault IRQ thread | `adreno_fault_handler -> msm_gpu_fault_crashstate_capture`, waiting for `gpu->lock` before completing the fault capture |
| GNOME Shell | `adreno_get_param -> rpm_resume`, waiting for GPU runtime PM |
| GPU submission workers | Waiting for the same GPU lock |

GPU runtime status remained `suspending`. The
[upstream review of the completion wait](https://www.mail-archive.com/freedreno@lists.freedesktop.org/msg39108.html)
also explicitly raised this lock dependency. The separate memory-reclaim
deadlock fix is already present in this source and does not address these
captured stacks.

Logs and stacks were saved on the tablet under
`/var/lib/gts9u-diagnostics/gpu-freeze-20260909T134554Z/`, with a copy in the
PC's ignored `work/gpu-freeze-diagnostic.tar.gz` and
`work/gpu-blocked-stacks.txt`. Do not publish raw process command lines or
GPU memory dumps. The owner authorized reboot; normal reboot was requested
after syncing, but SSH did not return during preparation. Physical recovery
was requested. No experimental kernel has been flashed.

## Candidate change

`msm-adreno-bound-fault-coredump-wait-gts9u.patch` changes both the GMU OOB
and HFI response loops. Successful transactions retain their existing path.
After a transaction times out while a fault capture is pending, it waits
at most one second for that capture and permits at most one transaction
retry. If capture cannot finish, the existing timeout error propagates to
the recovery code. Repeated faults cannot restart the wait indefinitely.

This bounds the demonstrated deadlock path; **hardware recovery and the
initial GPU fault remain unvalidated**. It does not disable acceleration,
change Mesa, alter clock tables, or claim to eliminate all GPU hangs.
Kernel #11's hardware validation made the patch the default: kernels #10 and
#11 carried it through the RAM-root and normal-root suspend cycles, normal
desktop use and the lid-wake test ([RAM-root diagnostic](ramroot-diagnostic.md)).
None of those runs hit a GPU fault, so the recovery path itself is still
unexercised on hardware. The patch has no board check, so SM-X710
(gts9wifi) kernels carry it as well.

Default builds enable the candidate: since kernel #11,
`GPU_FAULT_WAIT_EXPERIMENTAL` defaults to 1, and `build-mainline-kernel.sh`
stages the patch through `scripts/stage-gpu-recovery-experiment.sh` for every
`DEVICE`. To build without it, set `GPU_FAULT_WAIT_EXPERIMENTAL=0` with a
fresh `KERNEL_WORKTREE`; staging with 0 refuses a tree that already contains
the candidate. Repeated staging verifies the complete
two-file patch; partial application is rejected. Reverse verification uses
`patch --force` so GNU patch cannot silently change direction on one file.

`scripts/test-gpu-recovery-experiment.py PRISTINE_ADRENO_DIRECTORY` stages
the patch and compiles the actual two retry loops with mocked MMIO and
completion operations. Five cases per loop passed: immediate success,
timeout without pending capture, capture blocked on the recovery lock,
successful retry, and recurrent faults exhausting the retry budget.
Default/repeated/stale/partial staging checks also passed. These are
control-flow tests, not a physical GPU fault-injection test.

## Combined build for the next test session

The three incoming commits through `66c832a` were fast-forwarded into the
local repository. The earlier kernel #9 UFS candidate and its #8 backup
were found and their recorded hashes verified before building on that work.

The new #10 combines **only the two-file GPU patch and the UFS PCS reset
candidate**, on the existing #8 performance/fingerprint base. That candidate
was opt-in then; since kernel #11, `UFS_PCS_RESET_EXPERIMENTAL` defaults to 1.
Source and objects are still at
`/root/ubuntu-gts9u/build/linux-src-performance-20260908` and
`/root/ubuntu-gts9u/build/linux-performance-20260908` in WSL Ubuntu.
They now contain both experiments; neither directory is a pristine #8 tree.

Backups, compile log, and new outputs are preserved in
`/root/ubuntu-gts9u/build/gpu-ufs-combined-20260909.GhW28n/`:

- `before/` preserves the previous #9 Image, vmlinux, System.map, config,
  Module.symvers, and both unchanged GPU sources.
- `candidate-kernel10/` contains Image, Image.gz, vmlinux, System.map and
  version metadata. Build uses LLVM 22, eight jobs, `LOCALVERSION=-dirty`,
  `KBUILD_BUILD_VERSION=10`, and fixed build timestamp
  `Wed Sep 9 16:00:00 CEST 2026`.
- Configuration, Module.symvers, signing key and certificate hashes were
  checked unchanged after compiling. No modules or keys were replaced.

| Artifact | SHA-256 |
| --- | --- |
| Kernel #10 Image | `2105964b0e7b1cdb89da621793e298e8cc8a0d8651eb80839dfccaf767e58fcc` |
| Kernel #10 Image.gz | `2fd64e419667e766b109b10662394fbc83e151e462fe295011240007aec1d594` |
| Packed experimental boot image | `b193714fea21c4d0aa926c89d0e9a77403c6cfe5286d37480c958353b4b8a9cc` |

`scripts/prepare-gpu-ufs-kernel10-boot.py` reproduces the packed candidate
from the audited #8 boot and #10 Image. It checks both input hashes,
preserves the appended DTB and all boot header fields except kernel size,
and verifies the existing unsigned AVB hash-footer format. The output is
`artifacts/boot-gpu-ufs-kernel10-experimental.img`, 100663296 bytes. It has
been installed for normal desktop validation after three successful RAM-root
deep-suspend cycles. The boot image alone does not supply the diagnostic ramdisk. See [RAM-root diagnostic](ramroot-diagnostic.md).

## Pending physical acceptance

The initial storage gate has now passed: three RAM-root deep/RTC cycles and
normal-root validation are recorded in [RAM-root diagnostic](ramroot-diagnostic.md).
GPU fault recovery and long-duration lid/idle coverage remain pending.
For further experiments, establish a recovery path and prepare a diagnostic RAM-root
with all internal UFS filesystems unmounted. Preserve the current Ubuntu boot
set and do not repurpose the owner's WINPE microSD. The packed boot above
alone still uses the installed normal-root ramdisk, so it does not provide
that protection.

In one supervised session, first test repeated real deep-suspend/RTC-wake
cycles from RAM-root and UFS reads; a platform PM test is insufficient.
Only after that succeeds, test normal-root reads/writes, Wi-Fi, display,
brightness, fingerprint and lid/button/idle suspend. Exercise the desktop
with its usual GPU clients and capture any renewed GMU timeouts, faults,
stuck runtime-PM transitions or uninterruptible workers. A controlled GPU
fault-recovery test must show that recovery returns, not merely that an idle
desktop boots. The initial GMU timeout may need a further fix even if the
permanent deadlock is eliminated.

## SM-X710 (gts9wifi): a different GPU defect

The SM-X710's lost GPU writes (single pixels on a fixed lattice, WebGL
damage) were not a recovery problem and are fixed. Their cause was that
rpmhpd never reached sync_state(), which clamps every RPMh rail it has
enabled at its maximum corner; the X710 kernel now builds the video clock
controller in (`CONFIG_SM_VIDEOCC_8550=y`), so rpmhpd syncs at boot and the
write-check battery loses 0 of 296 frames (168 to 197 with rpmhpd held). The
root cause, the fix and the check are in the
[X710 display notes](gts9wifi-display.md). The Ultra has the same
sync_state blockers but shows no loss; the fix and the SM-X710's other
hand-off changes are written and compiled for it (2026-11-25,
[resume notes](resume-recovery.md#deep-sleep-groundwork-on-the-sm-x910-2026-11-25)),
in no kernel that has run there yet. Late
GMU `GX_BW_PERF_VOTE` answers like the ones above were also seen on the X710
while rpmhpd was held, and they persist with it synced: squirtle logged one
or two vote timeouts in each of five boots between 2026-11-22 and 2026-11-24,
all on kernels that build the video clock controller in (that rpmhpd synced
in those boots is inferred from the build; its `state_synced` was not read).
They are not an rpmhpd effect; see the next section.

## GMU vote timeouts: the answer bit that goes missing (2026-11-26)

Both tablets log, up to about once an hour of use, `Message
HFI_H2F_MSG_GX_BW_PERF_VOTE id N timed out waiting for response` and then
`Unexpected message id N on the response queue`: a GPU frequency vote waits
1 s for the GPU controller (GMU), gives up, and the next message finds the
answer. The wait holds the locks that GPU submissions need, so the desktop
freezes for that second (longer while the serial console prints slowly), and
hangcheck often reports a lockup that needs no reset. The investigation is in
`~/ubuntu-gts9u/backups/gpu-gmu-timeouts-rootcause-2026-11-25.md`.

The first four timeouts caught with the gpu-watch tracing kit (blastoise,
kit r8 kernel, 2026-11-26 10:54:43, 11:51:44, 12:01:07 and 12:10:38) show
that the GMU was not late, and all four look alike. Each vote was the first
after a GPU wake-up, sent right after the hfi interrupt is enabled again;
exactly one hfi interrupt ran 196 to 318 us into the wait (in the first
record, with the response queue still empty); when the wait gave up the
answer was in the queue (the vote's sequence number, error 0). The wait polls
only the "answer here" bit (MSGQ) of `GMU2HOST_INTR_INFO`, and that bit was
not there. The hfi interrupt handler writes back to the clear register every
bit it reads, MSGQ and the OOB acknowledge bits among them, although since
upstream df0dff132905 (2018) other code polls for those bits; it runs outside
`gmu->lock`. The register reads were off for those four; with them on, 14 of
the first 15 hfi interrupts found `INTR_INFO` 0 at the handler's entry. Which
bit raises the interrupt, whether MSGQ was among the bits cleared at a
timeout, and what the interrupt mask holds then are not known yet.

Two patches, in every build on both boards, after the sleep fixes:

- `msm-adreno-a6xx-hfi-irq-leave-polled-bits.patch`: the handler clears every
  bit but MSGQ and the OOB acknowledge bits, and leaves those to their
  waiters. A polled bit, or any pending bit but the firmware-fault one, that
  it finds unmasked (the driver masks all others) means the mask no longer
  holds what the driver wrote: it writes the mask again, also when no status
  bit is left, so the line drops, the bit stays, and the mask is right for
  the rest of that power-up; it clears a pending bit as before only if the
  mask does not take. The first such event is logged once per boot: `GMU
  interrupt mask 0x... left 0x... unmasked (status 0x...), written again:
  0x...`.
- `msm-adreno-a6xx-hfi-take-queued-response.patch`: the HFI wait also ends
  when the response queue holds an answer, whether or not its bit came, and
  the queue is read after a read barrier.

`tests/test_gpu_hfi_irq.py` checks the build lines, applies the patches the
way the build does (no offset, with and without `APPLY_SM8550_SLEEP_FIXES`),
and runs the driver's own handler, OOB request and HFI wait, compiled for the
build host, against a simulated GMU in each case the capture leaves open. The
handler's patch alone covers an interrupt that runs while MSGQ and the answer
are both there, and the OOB acknowledges; the wait's patch alone covers every
vote case, among them MSGQ up before the answer shows in the queue (one
reading of the capture), a bit that never comes (the other), and a mask that
does not take; with both, no case fails and the line is always down
afterwards.

Status: written and compiled for arm64 with both tablets' configurations
(no warnings, also with `W=1`); not built into a boot image and **not run on a
tablet yet**. After the first boot with them,

```
journalctl -k | grep -E 'timed out waiting for response|GMU interrupt mask|Timeout waiting for GMU OOB'
```

should show no vote timeout; a `GMU interrupt mask` line, if any, gives the
register values the captures lacked. Not done, and wanted only if timeouts
remain: longer OOB waits, re-sending a failed vote, and taking the devfreq
lock out of the submit path.
