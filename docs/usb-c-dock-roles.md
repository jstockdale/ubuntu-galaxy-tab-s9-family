# USB-C docks: who supplies power and who is the USB host

State on 2026-11-21.  Applies to both boards (SM-X910 and SM-X710): they share
the SM5714 Type-C controller, its TCPM driver (`kernel/drivers/sm5714_usbpd.c`)
and the connector description (`power-role = "dual"`, `data-role = "dual"`,
`try-power-role = "sink"`).

Everything measured here comes from the SM-X910 and two powered docks, an
Anker hub (PD identity `291a:8383`) and a Satechi Thunderbolt 4 dock
(`310e:0952`).  Where a statement rests on one dock or on a few samples, it
says so.  A first revision of the two TCPM changes ran on that tablet on
2026-11-21:

- the data role change did what it should on all three attaches where it
  came into play, with both docks;
- the power role change worked on three attaches and failed on the fourth,
  the Satechi plugged into the running tablet, which showed something the
  first revision did not handle;
- a boot with the dock attached lost its USB host for another reason, the
  USB power domains in the boot's suspend cycle.

What the tree carries now is the second revision of the power role change
and, for the SM-X910, a change to those power domains.  **Both are compiled
into test kernels and have not run on a tablet yet.**  Nothing of this has run
on the SM-X710.

## What a powered dock should give

A dock with its own power supply should leave the tablet as **power sink and
USB host**: charging, with the dock's hub, Ethernet and display working, and
nothing to do by hand.  Two separate defects stood in the way, and a third
one when the tablet is booted with the dock attached.

## The data role: an endless re-attach

A powered dock attaches as power source and, for the first moments, as USB
host (Source/DFP).  The tablet takes the 9 V contract, becomes Sink/UFP and
runs discovery in that role.  About 270 ms after the last discovery reply the
dock sends DR_Swap to hand over the host role.  TCPM accepts and becomes DFP,
and 1.3 to 5.7 ms after the Accept the dock sends a Soft_Reset whose header
still says Source/DFP.  Two DFPs on one link is a protocol error, so TCPM logs
`Data role mismatch, initiating error recovery`, drops the connection, and
the same sequence starts again.

The Soft_Reset is not an answer to something the tablet says as host: in 7 of
the 29 cases on record the tablet had sent nothing in its new role when it
arrived.  The Anker hub did this on each of its 28 attaches in the traces.
The Satechi dock has two attaches of this kind on record: one ended the same
way, and in the other the dock sent a Hard_Reset right after the tablet's
first message as host.

Why the dock's own swap does not hold is not known; a PD analyser on the CC
line would show what the dock makes of the Accept.  What was measured is the
way round it:

- a DR_Swap **sent by the tablet before its own discovery** was accepted
  3 ms later, both times it was tried (the Anker hub), and discovery then
  ran once, as host, through to DisplayPort alternate mode.
  The first of the two connections held for the ten minutes until the hub was
  disconnected.  With the patch below the same request has been accepted on
  each of the three attaches that had the tablet as sink, twice by the
  Satechi dock and once by the Anker hub, 4 to 9 ms after it went out.  Each
  time the tablet stayed host and sink and charged from the 9 V contract
  (1.66 A), with no `Data role mismatch` and no reset;
- a DR_Swap sent later, while one of TCPM's own discovery messages was
  waiting for its reply, failed 30 times out of 30: the SM5714 reported the
  transmission discarded (29 times), the dock's discovery reply arrived where
  the answer to the swap was expected, and TCPM sent a Soft_Reset.  That is
  why a helper in userspace cannot do this reliably.

`tcpm-request-dfp-as-sink-before-discovery.patch` adds an opt-in for that:
with `tcpc_dev.request_dfp_as_sink` set, TCPM starts the swap from the
`SNK_READY` entry code, before discovery is scheduled, through
`tcpm_ams_start()` so that PD 3.0 collision avoidance still applies.  If the
port is not idle yet, the discovery work retries the request instead of
sending Discover Identity.  It only asks a source whose first PDO advertises
data role swap, and only on a dual-role-data port.

Accept, Reject and Not_Supported settle the question until the dock is
unplugged or a Hard Reset comes, and so does a swap that the dock asks for
and gets first.  Wait is not final: it means "not yet", and giving up there
would leave the dock to its own swap, the one that fails.  After Wait the request is repeated once 100 ms (tDRSwapWait) have
passed, and a request that got no answer or was lost to another message is
repeated as well, up to three requests per attach.  When they are used up the
tablet stays UFP and discovery runs as it always did.  Once the tablet has
started its own discovery it makes no request any more, so a request never
meets a discovery message that is waiting for its reply.  A PD 2.0 dock that
asks for a swap of its own while the tablet's request is still due gets Wait:
that is TCPM's answer to a PD 2.0 partner for as long as discovery is due,
and the request keeps it due a few hundred milliseconds longer at most.

A Hard Reset puts both ends back to their default data roles.  A host role
that came from a swap is given up there, and asked for again after the new
contract if a request is left.  A request that was queued but had not gone
out when the reset came is dropped with it.  A host role taken over for a
dock that kept its own role across the reset (the earlier
`adopt_retained_source_ufp` patch) is still kept.  With `sink_host_swap` off,
a hard reset is handled as it was before this change.

## The power role: Try.SNK that never succeeded

A dock that can also be powered from its host is a dual-role power port, and
both docks here are.  When the tablet happens to see one as a sink first,
TCPM does what `try-power-role = "sink"` asks for: it presents Rd and gives
the dock `tDRPTry` (100 ms) to answer with Rp.  On this controller that wait
is never met.  The SM5714 does not expose its CC comparators, only an
attachment it has debounced itself, and that report is late: inside Try.SNK
it came 124, 130, 130 and 147 ms after Rd went up (four attaches, both
docks), and in two of them the dock's VBUS was already on 26 ms before it.
All 13 Try.SNK attempts recorded with the standard wait ended
`SNK_TRY_WAIT -> SRC_TRYWAIT` with no Rp reported: the tablet became the
power source and the dock never charged it.  PR_Swap is no way back either:
the Satechi answers Wait every time, and with the Anker hub the CC connection
is lost while the roles change.

`tcpm-try-snk-extra-wait.patch` lets a controller extend the wait
(`tcpc_dev.try_snk_extra_wait_ms`).  After `tDRPTry` the port stays in
`SNK_TRY_WAIT`, still presenting Rd; an Rp reported in that time takes the
normal Try.SNK path (`SNK_TRY_WAIT_DEBOUNCE`, VBUS check, `SNK_ATTACHED`), and
without one the port goes on to `SRC_TRYWAIT` as before, only later.  The
driver asks for 600 ms, 700 ms of Rd in all.

The first revision added that time as a second wait and left the states
after it alone.  That was enough three times out of four: twice for the
Satechi in a boot with it attached, once for the Anker hub.  When the Satechi
was plugged into the running tablet it was not.  Its Rp was reported 130 ms
after Rd and was gone again 46 ms later, with VBUS still off.  TCPM was in
the debounce by then, where an Rp that goes away ends the attempt, so the
tablet turned to the source role 15 ms later and powered the dock, as it did
before the change.

That is what a dock does that tries the sink role first itself: it answers
the tablet's Rd as a source, takes its Rp away to look for a source, finds
none because the tablet is still holding Rd, and comes back as a source for
good.  The 433 and 453 ms between Rd and the report of the source that were
measured earlier, with the port forced to sink, would be that whole sequence.
This is read from the timing; nothing has watched the CC line itself.

The revision in the tree makes the extra time one deadline, set when Rd goes
up: `tDRPTry` plus the extra wait.  An Rp that goes away before the deadline
sends the port back to `SNK_TRY_WAIT` for what is left of it, from either
debounce state, and the Rp that comes back is taken as the first one would
have been.  After the deadline, and with the extra wait at 0, every step is
upstream's.  If the reading above is right, the Satechi's lasting Rp is
reported about 450 ms after Rd, inside the 700 ms.  **This revision has not
run on a tablet yet.**

## A boot with the dock attached: the USB host that died (SM-X910)

With the first revision, the SM-X910 booted with the Satechi attached came up
as sink and host, and the dock's devices enumerated.  At 24 s the boot ran
its suspend cycle (`pm_test=platform`, the panel's cold-boot recovery), and
the USB host controller did not come out of it:

```
xhci-hcd xhci-hcd.2.auto: xHCI host not responding to stop endpoint command
xhci-hcd xhci-hcd.2.auto: xHCI host controller not responding, assume dead
xhci-hcd xhci-hcd.2.auto: HC died; cleaning up
```

The tablet went on charging with nothing behind the dock working, and
plugging the dock in again did not bring the host back
(`Host halt failed, -110`).  This is older than the role changes: the two USB
power domains of the SM8550 (`usb30_prim_gdsc`, `usb3_phy_gdsc`) are declared
as switched off in every system suspend, this test cycle included, and the
xHCI resumes as if its state had been kept.  It only showed now because the
tablet is the USB host by the time the cycle runs.

`gcc-sm8550-usb-gdscs-retention.patch` declares both domains as kept in
retention, as upstream does for the same domains on sm8650.  It is in
the SM-X910's test kernel; this branch, which the SM-X710's kernel kit is
built from, does not carry it yet.
**It has not run on a tablet yet**, and one question is open beyond
the boot cycle, where the supply behind the domains stays up: whether the
state also survives a real deep sleep.  Until that is shown, the sleep hook
that takes the port out of host role around each sleep stays.

## Switches

Both behaviours are on by default on both boards and can be changed at run
time; a change applies from the next attach.

| Parameter | Default | Meaning |
| --- | --- | --- |
| `/sys/module/sm5714_usbpd/parameters/sink_host_swap` | `Y` | As PD sink, request the host role before discovery.  `N` restores TCPM's standard behaviour |
| `/sys/module/sm5714_usbpd/parameters/try_snk_extra_wait_ms` | `600` | Extra wait for a source in Try.SNK.  `0` restores the standard 100 ms; values above 2000 count as 2000 |

The driver is built in, so the same names work on the kernel command line
(`sm5714_usbpd.sink_host_swap=0`).  With both fields left at zero TCPM behaves
as it does upstream, which is what every other TCPM controller gets.

## What changes for other things you plug in

- **A PD source that offers data role swap and accepts it becomes the tablet's
  USB device.**  Source capabilities do not tell a dock from a laptop or a
  phone.  A laptop that used to see the tablet as a USB device (gadget
  Ethernet over a C-to-C cable) will now be asked to be the device instead;
  `sink_host_swap=N` gives the old behaviour.  An A-to-C cable to a computer
  is not PD and is not affected.
- **More attaches end with the tablet as USB host**, so what is still wrong
  in host role applies to more of them.  On the SM-X910 the xHCI did not
  survive suspend in host role (the section above), and the sleep hook takes
  the port out of host role for each sleep; a dock's devices disconnect and
  reconnect around it.
- **Try.SNK now works for every dual-role partner**, as the device tree always
  asked: with a power bank, a laptop or a phone the tablet ends up as the
  sink.  Before, the outcome depended on which side was seen first.
- **An accessory that can only be a sink gets VBUS about 0.6 s later** (USB
  stick, keyboard, unpowered hub: about 1.2 s after plugging in instead of
  0.6 s).  Both docks in the traces show Ra on the second CC pin when seen as
  a sink (a powered cable), so the extra wait could be limited to that
  pattern if the delay turns out to matter; it is not, today.

## Checking it on a tablet

Needs a kernel built from this tree.  Anything in userspace that writes
`data_role` or `power_role` to work around the flap has to be stopped first,
or the result cannot be attributed.

The TCPM log is `/sys/kernel/debug/usb/tcpm-8-0033/log` on the SM-X910 and
`tcpm-4-0033/log` on the SM-X710, where the controller sits on another I2C
bus; `tcpm-*` below covers both.  Reading the log empties it, and a full log
(1024 lines) drops what comes next, so empty it before each plug-in.

```sh
sudo sh -c 'cat /sys/kernel/debug/usb/tcpm-*/log' > /dev/null   # empty the log
# plug the powered dock in, wait five seconds
cat /sys/class/typec/port0/data_role /sys/class/typec/port0/power_role
cat /sys/class/power_supply/sm5714-battery/status \
    /sys/class/power_supply/sm5714-battery/current_now
lsusb
sudo sh -c 'cat /sys/kernel/debug/usb/tcpm-*/log'
```

On the SM-X710 the [Type-C roles kit](../tools/inplace/typec-roles/README.md)
carries the kernel, and its `verify.sh dock` runs these steps for each
plug-in, keeps the log and gives a verdict.

Expected: `[host]` and `[sink]`, `Charging` with a positive current, the
dock's hub in `lsusb`.  In the log, after the contract (the bracketed
suffix of each line left out):

```
state change SNK_NEGOTIATE_CAPABILITIES -> SNK_TRANSITION_SINK
state change SNK_TRANSITION_SINK -> SNK_READY
AMS DATA_ROLE_SWAP start
state change SNK_READY -> AMS_START
Requesting DFP before discovery
state change AMS_START -> DR_SWAP_SEND
state change DR_SWAP_SEND -> DR_SWAP_CHANGE_DR
state change DR_SWAP_CHANGE_DR -> SNK_READY
```

followed by one discovery as DFP, and no `Data role mismatch`.  A dock that
answers Wait shows `state change DR_SWAP_SEND -> DR_SWAP_CANCEL` instead of
the last two lines, and the request again 100 ms or more later, up to three
in all.

A dock seen as a sink first should show the extended wait and a late Rp being
taken:

```
state change SNK_TRY -> SNK_TRY_WAIT
pending state change SNK_TRY_WAIT -> SRC_TRYWAIT @ 600 ms
state change SNK_TRY_WAIT -> SNK_TRY_WAIT_DEBOUNCE
state change SNK_TRY_WAIT_DEBOUNCE -> SNK_TRY_WAIT_DEBOUNCE_CHECK_VBUS
state change SNK_TRY_WAIT_DEBOUNCE_CHECK_VBUS -> SNK_ATTACHED
```

The 600 can read a few milliseconds less: it is what is left of the deadline
when the line is written.  Rd goes up 100 ms before the first of these lines,
and the third is the SM5714's report of the source.  A dock whose Rp does not
stay, the Satechi plugged into the running tablet, should now show the port
going back to waiting and taking the Rp that follows:

```
state change SNK_TRY_WAIT_DEBOUNCE_CHECK_VBUS -> SNK_TRY_WAIT
pending state change SNK_TRY_WAIT -> SRC_TRYWAIT @ 520 ms
state change SNK_TRY_WAIT -> SNK_TRY_WAIT_DEBOUNCE
state change SNK_TRY_WAIT_DEBOUNCE -> SNK_TRY_WAIT_DEBOUNCE_CHECK_VBUS
state change SNK_TRY_WAIT_DEBOUNCE_CHECK_VBUS -> SNK_ATTACHED
```

The first of these lines can also start from `SNK_TRY_WAIT_DEBOUNCE`, and
the figure is whatever is left at that moment.  If the wait runs out instead
(`state change SNK_TRY_WAIT -> SRC_TRYWAIT`) with a powered dock, set
`try_snk_extra_wait_ms` to 1500 for one attach to read when its Rp comes, and
the default has to follow that.  A dock whose own power supply is not plugged
in never shows Rp, and the tablet powers it; that is as it should be.

**The Satechi plugged into the running tablet is the case to watch.**  It
is the one the first revision lost.  With `try_snk_extra_wait_ms=0` it
attaches as it always did, with the tablet as power source: no charging, but
a stable connection.

Repeat at least ten times with each dock, including a boot with the dock
attached and a plug after login, and check that the role is the one reported:
the data role, not the fact that a DR_Swap went out.  After a boot with the
dock attached, also check that the kernel log has no `xhci-hcd` line with
`HC died` and that the dock's devices are still there after the first half
minute.  Then the things that must not change: a plain PD charger (charges,
no swap unless it offers one), an unpowered hub and a USB stick (enumerate,
about 0.6 s later than before), and on the SM-X710 the A-to-C cable to the
build host (gadget network up).

## Still open

- Why the dock sends a Soft_Reset after its own swap is not known.  That it
  comes even when the tablet has sent nothing as host (7 of 29) speaks
  against the tablet's first message in the new role as the trigger.
- The second revision of the Try.SNK wait and the USB power domain change
  have not run on a tablet; neither has anything on the SM-X710.  A real
  sleep with a dock attached has not been tried with any of it.
- Whether the USB power domains keep their state through a real deep sleep
  on the SM8550.  If they do, the sleep hook's role change can go, and a
  dock's devices stay connected across a sleep.
- How much of the 124 to 147 ms between Rd and the report of an Rp is the
  dock's and how much the SM5714's own debounce (twice the dock's VBUS was
  on 26 ms before the report).  If the controller's share can be shortened
  far enough, the standard Try.SNK is met and the extra wait is only needed
  for a dock that tries the sink role first itself.
- `tcpm_dr_set()` reports success to userspace when a swap it sent was
  discarded and the role did not change.
- A DATA_ROLE_SWAP AMS can interrupt a VDM that is waiting for its reply;
  that is what turns a late swap into a soft reset.
- A request that the SM5714 reports as discarded (`PD TX complete, status: 1`
  right after the DR_Swap in the log) still waits 60 ms for an answer and
  counts as one of the three.  A dock that starts its own discovery right
  after the contract could use all three up that way; the log of the first
  tests will show whether one does.
- The earlier patch keeps every host role that did not come from a swap
  across a Hard Reset received as sink, not only one taken over from a dock
  that kept its role.  A tablet that attached as power source and became
  sink through a PR_Swap is such a case: the dock goes back to host at the
  reset, and its next message is a data role mismatch.  It needs a PR_Swap
  that succeeds, which this controller does not manage today (next item); a
  marker for the adopted role would close it.
- PR_Swap on this controller: with one dock the CC connection drops in
  `PR_SWAP_SRC_SNK_SOURCE_OFF`.  Once the tablet is attached as power source
  there is still no way to become the sink short of re-plugging.
- `SRC_TRYWAIT` has the same 100 ms window on the source side and is not met
  either; nothing depends on it today.
- Every Request carries Capability Mismatch: the 9 V sink PDO is 1.66 A
  (14.94 W) and `op-sink-microwatt` is 15 W.  The contract is granted all
  the same; lowering `op-sink-microwatt` to 14940000 would clear the flag.
- Booting with a dock attached has its own history (see the
  [DisplayPort notes](gts9u-displayport.md)).  The one boot of that kind
  with these changes ended as described above; DisplayPort through the dock
  was not looked at in it.
