# USB-C docks: who supplies power and who is the USB host

State on 2026-11-21.  Applies to both boards (SM-X910 and SM-X710): they share
the SM5714 Type-C controller, its TCPM driver (`kernel/drivers/sm5714_usbpd.c`)
and the connector description (`power-role = "dual"`, `data-role = "dual"`,
`try-power-role = "sink"`).

Most of what was measured here comes from the SM-X910 and two powered docks,
an Anker hub (PD identity `291a:8383`) and a Satechi Thunderbolt 4 dock
(`310e:0952`); what the SM-X710 showed is told where it belongs.  Where a
statement rests on one dock or on a few samples, it says so.  Three test
kernels ran on the SM-X910 on 2026-11-21:

- the first had the first revision of the two TCPM changes.  The data role
  change did what it should on all three attaches where it came into play,
  with both docks.  The power role change worked on three attaches and
  failed on the fourth, the Satechi plugged into the running tablet, which
  showed something that revision did not handle.  A boot with the dock
  attached lost its USB host for another reason, the USB power domains in
  the boot's suspend cycle;
- the second had the second revision of the power role change and a change
  to those power domains.  Two boots with the Satechi attached and four
  plug-ins into the running tablet, two with each dock, all ended with the
  tablet as power sink and USB host, and both boots kept the USB host
  through the suspend cycle;
- the third added two fixes for a data role swap asked for from userspace
  and a driver that reads its two switches at each attach.  One boot with
  the Satechi attached has run on it, with the same outcome.

The tree now carries a third fix of that kind as well, a NULL check in the
USB controller's glue (`dwc3-qcom`) for both tablets, and the power domain
change in the SM-X710's kernel kit too.  No kernel with the third fix or the
NULL check has run on a tablet, and the power domain change has run on the
SM-X910 only.  **None of the three fixes and neither switch has been tried
on a tablet yet.**

The SM-X710 has the third kernel's TCPM changes installed, without the power
domain change.  On 2026-11-21 it was recorded with both docks on the kernel
before the kit, and booted with the Anker hub attached on the kit's kernel.
In that boot the kernel's own request for the host role was accepted the one
time it was made, and after the boot's suspend cycle in host role the next
USB host controller failed to start (see "A boot with the dock attached").

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
  each of the nine attaches whose log was kept, six times by the Satechi
  dock and three times by the Anker hub: the first request went out whole
  each time (`PD TX complete, status: 0`) and the Accept came 4 to 9 ms
  after it.  Each time the tablet stayed host and sink on the 9 V contract
  (1.66 A), with no `Data role mismatch` and no reset.  A tenth attach, a
  boot with the Satechi attached, ended in the same roles; its log was not
  kept.  On the SM-X710 the request was made once, by the kit's kernel in
  the docked boot below, and the Anker hub with its charger accepted it
  3.5 ms after it went out;
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

### A swap asked for from userspace

The 30 failures above were writes to the port's `data_role`, and two things
in TCPM made each of them worse than a request that is turned down.  Both
are fixed for every TCPM port, without an opt-in:

- `tcpm-no-dr-swap-during-vdm.patch`.  `tcpm_ams_start()` let a data role
  swap start while one of TCPM's own VDM sequences was running, so the
  DR_Swap went out over the discovery message that was waiting for its
  reply.  It now turns the swap down for as long as the VDM state machine
  runs (`vdm_sm_running`: from the moment TCPM queues a VDM, a request of
  its own such as discovery's or an answer to the partner's, or receives
  one of the partner's, until that exchange is over): the writer gets
  `EAGAIN` and the log says `VDM in progress, DATA_ROLE_SWAP not started`.
  The kernel's own request is made before discovery starts and never meets
  this.
- `tcpm-dr-set-checks-data-role.patch`.  `tcpm_dr_set()` told the writer
  that the swap was done whenever the state machine came back without an
  error of its own, also when the request had been discarded and the role
  was the old one.  It now compares the data role with the one asked for,
  and returns `EAGAIN` with `Data role swap ended with the role unchanged`
  in the log when they differ.

A program that writes `data_role` can get an error now where it was told
"done" before; writing again is the answer to both.

A third fix, `tcpm-keep-queued-swap-pending.patch`, comes from reading the
code after those two; no trace showed the fault.  A write to `data_role`,
`power_role` or `vconn_source` puts its swap in `upcoming_state`, where
`AMS_START` takes it from, and waits for the swap to be completed.  A message
of the partner's that TCPM handles before that, such as new
Source_Capabilities or a swap of the partner's own, takes the port through
its ready state first, and the ready state completed every pending swap with
status 0.  The writer had its answer while its swap had not been sent; for a
data role swap the role check above made that answer `EAGAIN`, also when the
swap went out afterwards and was accepted.

`SRC_READY` and `SNK_READY` now leave a pending swap alone while a swap is
still queued (`Swap is still queued, left pending` in the log).  That is the
write's own swap, about to be sent, or a swap request of the partner's that
took its place, about to be accepted.  The states the swap goes through
complete it, or the ready state after them; after the partner's swap the
role check sees the role that swap left.  Where the partner's message has
put another state in the swap's place, the ready state completes the swap
as before, and for a data role swap the role check says what became of it.
A VCONN swap that turns the port's VCONN on ends in
`VCONN_SWAP_SEND_SOFT_RESET`, which completed the pending swap as well; it
now leaves a swap that is still queued alone too.

When the message that got in first was the partner's own swap of the same
kind, the port accepted it and then sent the queued swap as well, which put
the role back.  A queued `DR_SWAP_SEND` is now dropped where the partner's
DR_Swap changes the data role
(`Partner swapped first, queued DR_SWAP_SEND dropped`), and a queued
`PR_SWAP_SEND` or `VCONN_SWAP_SEND` where the port accepts the partner's
PR_Swap or VCONN_Swap (`Partner swapped first, queued PR_SWAP_SEND dropped`,
`Partner swapped first, queued VCONN_SWAP_SEND dropped`).  The role the
queued swap was for is the one the port has then.  All of it is for every
TCPM port and all three role attributes.  None of it has been seen on a
tablet.

None of the three fixes has been tried on a tablet yet.

## The power role: Try.SNK that never succeeded

A dock that can also be powered from its host is a dual-role power port, and
both docks here are.  When the tablet happens to see one as a sink first,
TCPM does what `try-power-role = "sink"` asks for: it presents Rd and gives
the dock `tDRPTry` (100 ms) to answer with Rp.  On this controller that wait
is never met.  The SM5714 does not expose its CC comparators, only an
attachment it has debounced itself, and that report is late: inside Try.SNK
it came 123 to 147 ms after Rd went up (eight attaches: seven with the
Satechi, 123 to 130 ms, and one with the Anker hub, 147 ms), and in four of
them the dock's VBUS was already on 26 to 29 ms before it.
All 13 Try.SNK attempts recorded with the standard wait ended
`SNK_TRY_WAIT -> SRC_TRYWAIT` with no Rp reported: the tablet became the
power source and the dock never charged it.  The one attempt with a powered
dock recorded on the SM-X710, the Satechi with the standard wait on the
kernel before the kit, ended the same way, with the dock's VBUS on 98 ms
after Rd: the tablet stayed power source and host, with the dock's devices
on the bus and the battery discharging.  PR_Swap is no way back either:
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

The second revision makes the extra time one deadline, set when Rd goes
up: `tDRPTry` plus the extra wait.  An Rp that goes away before the deadline
sends the port back to `SNK_TRY_WAIT` for what is left of it, from either
debounce state, and the Rp that comes back is taken as the first one would
have been.  After the deadline, and with the extra wait at 0, every step is
upstream's.

It kept the sink role on all four attaches that went through Try.SNK with
it and left a log, each with the Satechi: two boots with the dock attached
and two plug-ins into the running tablet.  One of each took the path the
first revision lost.  The Rp went away 47 ms after its report, with 528 and
532 ms of the deadline left, and the lasting one was reported 407 and
400 ms after Rd: the reading above, inside the 700 ms.  What tells the two
paths apart in the log is the dock's VBUS.  In the four Satechi attaches
whose first Rp stayed it was on 26 to 29 ms before the report; in the three
whose Rp went away again, the first revision's among them, it was still
off.  The Anker hub attached as a source from the start in its two plug-ins
with this revision, so they did not come through Try.SNK at all.

## The power role, later: asking a dock with its own supply again

**Written and compiled; the swap back has not run on hardware** (2026-11-26).
The partner's in-place PR_Swap ran once on the SM-X910, with the r8 kernel,
when the hub asked for the source role itself (14:52, below); the swap
itself completed and PD then went deaf.  The driver fixes for that, and the
tablet's own in-place PR_Swap, have not run.  The SinkTxOK signal below
(`sink_tx_ok_rp`) is on by default, the user's decision of 2026-11-26 after
that test, which used `otg_rp` at 2 (Rp 3.0 A all the time);
`sink_tx_ok_rp` itself has not run either.

When the Anker hub's own supply goes away for a moment it falls back to an
unpowered sink; Try.SNK finds no source, and the tablet becomes the hub's
power source, as it should.  On 2026-11-26 the tablet then powered the hub
and its devices from its battery for 26 minutes, until the hub was
re-plugged.  The hub said nothing in all that time: no PD message and no CC
change, also when its charger was pulled and plugged in again.

### Why the hub said nothing: SinkTxOK

A PD 3 sink may start a message sequence only while its source presents
Rp 3.0 A ("SinkTxOK"); during the source's own sequences the source shows
"SinkTxNG".  TCPM asks for exactly that (`cc:=5` and `cc:=4` in the TCPM
log), but `sm5714_usbpd` wrote `otg_rp` (default: Rp default) for every Rp
request while a sink was attached.  The hub never saw SinkTxOK, so it never
asked for anything.  At 14:52 the user set `otg_rp` to 2 (Rp 3.0 A all the
time) and pulled and re-plugged the hub's charger: 8.1 s after the tablet's
`SRC_READY`, as the charger went back in, the hub sent a PR_Swap itself
(`PD RX, header: 0xe8a`), the first in any capture.  That settles it.

`sink_tx_ok_rp` (below, on by default) gives TCPM's requests their
meaning under a PD 3 contract: Rp 3.0 A where TCPM asks for SinkTxOK, the
`otg_rp` level for SinkTxNG and for everything before the contract.  TCPM
asks for Rp 3.0 A only as SinkTxOK, after a PD 3 contract: the 900 mA
source PDO gives Rp default otherwise, a PD 2 contract gives SinkTxNG, and a
device without PD never gets a contract, so a USB stick, a passive OTG
adapter or a PD 2 sink never sees 3.0 A.  A PD 3 sink under the 5 V 900 mA
contract has to keep to it; one that draws by the Rp instead runs into the
OTG current limit (`otg_ma`, 1.5 A by default) and is dropped.  Samsung's
driver presents Rp 3.0 A to PD 3 sinks as well, through the chip's Rp auto
control.  This is the fix that lets a powered hub take the source role
back the moment its supply returns; the questions below are the fallback
for a partner that does not ask.  With it on, every PD 3 sink may also
start its own message sequences (a PR_Swap, a DR_Swap, a VDM), as it may
with any PD 3 source; `N` gives back the behaviour before, `otg_rp` for
every Rp request.

With both on, as by default, a two-second pull of the hub's charger can go
either way: the hub's own PR_Swap comes first, or the tablet's question 5 s
after its `SRC_READY` does.  Both end in the same in-place swap in the
driver, armed by the hub's PR_Swap or by its Accept of the tablet's.  Which
one comes first on this hub is not known until it runs.  They do not get in
each other's way: a PR_Swap of the hub's in the 16 ms before the question
goes out is taken and the question dropped; while the question is on the
wire the tablet shows SinkTxNG, and a hub that sends anyway gets a Soft
Reset, after which the questions stop for the session and the hub can ask
again.

### Why PD went deaf after the hub's swap

In the 14:52 capture the in-place swap worked on the wire: Accept, the
kernel lines `Source-to-Sink PR_SWAP: CC state frozen` and `CC hold
applied`, the tablet's PS_RDY with its GoodCRC (`PD TX complete, status:
0`), the hub's PS_RDY, `CC hold released`, VBUS from the hub.  Then nothing
more was received: the hub's Source_Capabilities never arrived, the hub
sent a Hard Reset, every message of the tablet's failed (`status: 2`), TCPM
went to error recovery, and three attaches without a single message
followed, until the hub opened its own CC 11 s after its Hard Reset.  Only
then did PD work again (9 V, 1.66 A, as host).  The USB 2 side of the hub
went with the VBUS gap of the swap; its USB 3 side with the Hard Reset.

The SM5714 runs the PD Protocol Layer in hardware (GoodCRC, retries,
MessageIDs).  Samsung's driver resets it at the end of every power role
swap and tells it when a Hard Reset has been dealt with; `sm5714_usbpd` did
neither.  After the Hard Reset the Protocol Layer waits for the second
("Reset Wait Policy Engine") and passes no message until the chip sees a
detach, which TCPM's error recovery never made: it keeps Rd up on this port,
and the driver kept the attachment.  Why nothing was received right after
the swap is less certain: the hub's PS_RDY carried MessageID 0, and a new
source's first Source_Capabilities carry 0 as well, which a Protocol Layer
that was not reset takes for a repeat.  The driver now does both, and makes
TCPM's error recovery a real detach (switches below):

- the Protocol Layer is reset when the PS_RDY that ends an in-place swap
  has been received (Source-to-Sink) or sent (Sink-to-Source):
  `Source-to-Sink PR_SWAP: protocol layer reset`;
- when TCPM turns PD reception back on after a Hard Reset and the chip says
  it is waiting: `PD protocol layer released after a Hard Reset (PD_STATE3
  0x..)`;
- TCPM's error recovery after an in-place swap or a Hard Reset opens CC, so
  that the partner sees a detach and the port comes back in one reconnect:
  `CC opened for TCPM's error recovery after a swap or Hard Reset`.

A short USB 2 drop at a swap is expected: a PR_Swap takes VBUS to 0 V, and a
self-powered USB 2 device has to let go of the bus without VBUS.  It comes
back about a second after the hub's VBUS; the USB 3 side may stay.

### Asking: the swap back

`tcpm-swap-back-to-sink.patch` lets the controller ask.  With
`tcpc_dev.request_sink_as_source` set, a tablet that is power source to a PD
partner under an explicit contract, and whose preferred role is sink, sends
Get_Source_Cap (new states `GET_SOURCE_CAP` and `GET_SOURCE_CAP_TIMEOUT`)
and asks for the sink role with a PR_Swap when the partner's 5 V PDO has the
Unconstrained Power bit (`E` in the log's `[RUDE]`).  That answer is the
only one that ever leads to a PR_Swap.

- The first question goes out 5 s after the first `SRC_READY` of an
  attach.
- No external power: asked again after 30 s, then 60, 120, 240 and 480 s,
  then every 600 s for as long as the tablet is the source.
- Reject or Not_Supported to Get_Source_Cap, or no answer: the hub may well
  be a plain sink while its supply is away, so it is asked again every
  600 s.  Wait: one step further down the ladder.
- External power: PR_Swap at once.  Accepted, the tablet is to be sink at
  the next ready state, in place or after a reconnect.  Reject, Wait or no
  answer: asked again on the same ladder.  Reject from a partner whose PDO
  does not claim dual-role power, or Not_Supported: no more questions.
- While the port is busy (discovery, a VDM, another message sequence) the
  question waits a second at a time, for at most a minute, then one step
  down the ladder.

The Dual-Role Power bit alone does not decide: with the charger it had on
2026-11-26 (20 V 2.25 A) the hub announced `[UDE]`, no dual-role power, and
accepted a PR_Swap all the same; with the one at boot it announced `[RUDE]`.

**One partner, one session.**  The questions belong to a session: one
partner while it stays attached, or comes back within 60 s of a port reset
(the reconnect of a failed swap).  The tablet cannot tell partners apart
across a reset, so a different one plugged in within that minute continues
the session.  "No more questions" means: not while this partner stays
attached, or comes back within a minute.

**Bounds.**  Per session at most 8 PR_Swap requests and 3 accepted swaps.

- A question or a PR_Swap that was on the wire when a reset came (or sent
  less than 2 s before a port reset, unanswered) stops the session: a hub
  that leaves the bus when asked is asked once per session.  If it then
  stays away for more than a minute, that counts as a failed swap.
- A swap that the partner accepts but cannot complete (its supply is really
  gone while its capabilities still say Unconstrained Power) runs TCPM's
  failure path, one reconnect, and the tablet is source again: a failed
  swap, and the session stops.  So is a sink role lost within a minute of
  the swap with the tablet source again in the same session (a supply that
  collapses under the load).  An unplug soon after a good swap is not one.
- After 2 failed swaps nothing is asked of any partner until the tablet has
  been a power sink for 30 minutes in one stint (counted when the stint
  ends).  A hub used without its charger on purpose therefore sees at most
  one reconnect per session, two in all, and never a loop.
- **A supply that drops now and then** is followed every time: a swap whose
  sink role lasted 10 minutes gives the 8 requests and 3 swaps back.

**Others' choices stand.**  A write of `source` to the port's `power_role`
stops the session; a write of `sink` takes over a request that is queued
or due.  **A PR_Swap that the partner asks for** and that makes the tablet
the source stops the session too: the partner chose, and the tablet does
not ask it to take the role back.  A write of `sink` to `preferred_role`
after another value resumes the questions.  Nothing is sent while a VDM
exchange is under way or discovery is due; the work is deferrable and
freezable, so it never wakes an idle tablet and does nothing in a system
sleep.

**The tablet's own PR_Swap in place.**  The hand test on 2026-11-26 (tablet
source to the hub with its charger back, `echo sink` to `power_role`) ended
in error recovery: `set_cc(RD)` forced the sink mode, the chip dropped its
attachment, and the PS_RDY went nowhere.  The driver applies a Source-to-Sink
PR_Swap in place (CC state frozen, CC hold, `CC_CNTL7` bit 0 toggled, hold
released at the partner's PS_RDY) for a PR_Swap the partner asks for, and
now also when the partner accepts the tablet's own (`Source-to-Sink PR_SWAP:
own request accepted, applying it in place`), as Samsung's driver does for
both.  The swap stays armed only while TCPM goes ahead with it: any other
message from TCPM, or a Hard Reset, drops it (`PR_SWAP: in-place swap
dropped (...)`).  The `CC_CNTL7` bit is cleared wherever the driver forgets a
swap or forces a mode, and once at boot (`CC role bit left set by a swap
before this boot (CC_CNTL7 0x..), cleared`), as Samsung clears it at every
detach.  A Sink-to-Source swap that the tablet asks for is left as it was.

### Switches

Read like the others from the next attach, except where the table says
"each time":

| Parameter | Default | Meaning |
| --- | --- | --- |
| `/sys/module/sm5714_usbpd/parameters/swap_back_to_sink` | `Y` | As power source, ask a PD partner with its own supply for the sink role.  `N`: TCPM's standard behaviour |
| `/sys/module/sm5714_usbpd/parameters/own_pr_swap_in_place` | `Y` | Apply the tablet's own accepted PR_Swap to sink in place.  `N`: the old behaviour, error recovery |
| `/sys/module/sm5714_usbpd/parameters/sink_tx_ok_rp` | `Y` | As PD 3 source, Rp 3.0 A where TCPM asks for SinkTxOK, so that the partner may ask for the source role itself.  `N`: `otg_rp` for every Rp request, the behaviour before |
| `/sys/module/sm5714_usbpd/parameters/swap_protocol_reset` | `Y` | Reset the PD Protocol Layer at the end of an in-place PR_Swap (each time).  `N`: the r8 behaviour, for comparison |
| `/sys/module/sm5714_usbpd/parameters/recovery_detach` | `Y` | Open CC for TCPM's error recovery after an in-place swap or a Hard Reset (each time).  `N`: keep the attachment, as before |
| `/sys/module/sm5714_usbpd/parameters/pr_swap_rd_delay_ms` | `0` | Wait before Rd in an in-place swap to sink (each time); at most 500, keep it at 450 or less |

`pr_swap_rd_delay_ms` is for one failure only: the tablet's PS_RDY goes out
with `status: 0` but the partner's never comes.  Samsung changes the pull
about 375 ms after the Accept and this driver about 65 ms after it; the
PS_RDY follows 200 ms later, about 265 ms plus the wait after the Accept,
and a partner gives up on it after tPSSourceOff, at least 750 ms after the
Accept.  310 matches Samsung's pull change; 450 is the most that leaves a
margin.

Delivering it needs a new kernel image: TCPM and `sm5714_usbpd` are built
in (`CONFIG_TYPEC_TCPM=y`, `CONFIG_TYPEC_SM5714=y`), there is no module to
swap, and nothing in the device tree changes.

### Trying it on a tablet

All on the SM-X910 or the SM-X710 with a kernel from this tree, the TCPM log
emptied before each step as in "Checking it on a tablet" below, and the
kernel log read with

```sh
journalctl -k -b -o short-monotonic |
    grep -E 'swap back|PR_SWAP|protocol layer|CC opened|CC role bit|OTG boost|USB-PD contract|usb [12]-1'
```

`P` below is `/sys/module/sm5714_usbpd/parameters`.  `-o short-monotonic`
gives the kernel's own time stamps, the ones the TCPM log uses.

1. **The hub asks itself** (the 14:52 test, with the fixes), with the
   defaults: `sink_tx_ok_rp` on, `otg_rp` at 0.  With the hub charging the
   tablet, pull the hub's charger (the tablet becomes its source), wait
   10 s, plug it back.  About 5 s after `SRC_READY` the tablet's question
   goes out while the charger is still out (step 3's `partner has no
   external power now` or one of its other answer lines).  Expected in the
   TCPM log, in order: the attach as source up to `SRC_READY` and `cc:=5`;
   the question and its answer; after the charger is back, `PD RX, header:`
   ending in `8a` (the hub's PR_Swap), `SRC_READY -> PR_SWAP_ACCEPT`, the
   Accept (`PD TX, header:` ending in `a3`) with `status: 0`,
   `PR_SWAP_START`, `PR_SWAP_SRC_SNK_TRANSITION_OFF`,
   `vbus:=0`, `VBUS off`, `PR_SWAP_SRC_SNK_SOURCE_OFF`, `cc:=2`,
   `PR_SWAP_SRC_SNK_SOURCE_OFF_CC_DEBOUNCED`, the tablet's PS_RDY (header
   ending in `a6`) with `status: 0`, `CC1: 2 -> 5` (or CC2), the hub's
   PS_RDY (`PD RX`, header ending in `86`), `PR_SWAP_SRC_SNK_SINK_ON`,
   `SNK_STARTUP`, `SNK_DISCOVERY`, `VBUS on`, `SNK_WAIT_CAPABILITIES`, `CC1:
   5 -> 4`, and then, where r8 had nothing, the hub's capabilities (`PD RX,
   header:` ending in `81`: the hub keeps the device role, the tablet stays
   USB host) and its PDOs, `Requesting PDO 1: 9000 mV, 1660 mA`, the hub's
   Accept and PS_RDY, `SNK_READY`.  No `Received hard reset`, no
   `HARD_RESET`, no `ERROR_RECOVERY`.  In the kernel log: `Source-to-Sink
   PR_SWAP: CC state frozen`, `disabled USB OTG boost`, `CC hold applied`,
   `CC hold released`, `Source-to-Sink PR_SWAP: protocol layer reset`, `USB-PD contract: 5000 mV, 3000 mA`, `USB-PD
   contract: 9000 mV, 1660 mA`; `usb 1-1: USB disconnect` at the VBUS gap
   and the hub's USB 2 devices back about a second later.  What else can
   show, and what it means:
   - `Received hard reset` about 150 ms after `CC1: 5 -> 4`, as on r8: the
     reset did not keep the swap clean.  Then expect `PD protocol layer
     released after a Hard Reset (PD_STATE3 0x..)` at the next
     `SNK_WAIT_CAPABILITIES` and the capabilities after it: one Hard Reset,
     no reconnect.
   - `ERROR_RECOVERY`, `PORT_RESET`, and `CC opened for TCPM's error recovery
     after a swap or Hard Reset`, then `disconnected` within a few tens of
     ms: the net under both; one fresh attach, with PD.
   - Repeat once with `echo N | sudo tee $P/swap_protocol_reset`: the r8
     deafness up to the hub's Hard Reset is expected again, and then the
     second line above and charging without a reconnect.  That run tests
     the Hard Reset fix on purpose.  Set it back to `Y`.
2. **The tablet's own PR_Swap**, with `swap_back_to_sink` and
   `sink_tx_ok_rp` at `N` (`echo N | sudo tee $P/swap_back_to_sink
   $P/sink_tx_ok_rp`, so that neither the question nor the hub gets there
   first): the hub without its charger, plug it in (the tablet becomes its
   source), plug the hub's charger in, then `echo sink | sudo tee
   /sys/class/typec/port0/power_role`.  Expected: `PR_SWAP_SEND`,
   the hub's Accept, and from `PR_SWAP_START` on step 1's lines; in the
   kernel log `Source-to-Sink PR_SWAP: own request accepted, applying it in
   place` first, and the hub's Accept is `PD RX, header:` ending in `83`.
   With `own_pr_swap_in_place` at `N` it should end as the hand test did.
   If our PS_RDY has `status: 0` but no PS_RDY comes from the hub and TCPM
   goes to `ERROR_RECOVERY` 450 ms later, repeat with `echo 310 | sudo tee
   $P/pr_swap_rd_delay_ms`.  Afterwards `echo Y` to both switches again,
   and `echo 0` to `pr_swap_rd_delay_ms` if it was set.
3. **The swap back after a short loss of the hub's supply**, with
   `sink_tx_ok_rp` at `N` (`echo N | sudo tee $P/sink_tx_ok_rp`; with it
   on, the hub may ask first, see the last line below): with the hub
   charging the tablet, pull the hub's charger for about two seconds and
   put it back.  The tablet becomes the source, and about 5 s after its
   `SRC_READY` the TCPM log shows `AMS GET_SOURCE_CAPABILITIES start`,
   `AMS_START -> GET_SOURCE_CAP`, `PD TX, header:` ending in `a7`
   (Get_Source_Cap) and the answer.  Each kernel log line it can give,
   prefixed `swap back to sink:`:
   - `partner has external power (vSafe5V PDO 0x2e0190c8), requesting the
     sink role, PR_Swap 1 of 8` (the PDO as sent; 0x2e0190c8 is the 5 V 2 A
     `[RUDE]` one): the hub's supply is back.  Then `PR_Swap accepted, swap
     1 of 3`, step 1's swap and `power sink at 9000 mV 1660 mA`, or
     `power sink at 9000 mV 1660 mA, after a reconnect` if the in-place swap
     did not hold and TCPM's failure path got there.
   - `partner has no external power now, asking again in 30 s, then less
     often down to every 600 s`: no `E` yet; asked again at 30, 60, 120, 240,
     480 s, then every 600 s (TCPM log: `Swap back: no external power,
     asking again in N s`).
   - `Get_Source_Cap answered Reject, not a source now, asking again every
     600 s`, or `Not_Supported` instead of `Reject`: the hub refuses while
     unpowered; it is asked again every 10 minutes.
   - `Get_Source_Cap answered nothing, not a source now, asking again every
     600 s`: no answer within 60 ms; asked again every 10 minutes.  The
     later answers of both kinds go to the TCPM log only (`Swap back:
     Get_Source_Cap answered ..., asking again in 600 s`), as does Wait.
   - `PR_Swap answered Wait, asking again in N s` (or `Reject`, `nothing`):
     asked again on the ladder, at most 8 requests.
   - `PR_Swap request not sent, asking again in N s`: a message of the hub's
     or a write got in first; asked again on the ladder.
   - `sink role lost within a minute of the swap, failed swap 1 of 2; not
     asking again until the partner has been away for a minute`: the swap
     held, but the hub's supply gave out and the tablet is its source again.
   - `Get_Source_Cap cut short by a port reset; not asking again until the
     partner has been away for a minute` (or `PR_Swap ...`): the hub left
     the bus when it was asked; not asked again in this session.  Without
     `by a port reset`, a Soft or Hard Reset came instead of the answer.
   - Any other `...; not asking again until the partner has been away for a
     minute` names its reason: `PR_Swap answered Not_Supported`, `PR_Swap
     answered Reject by a partner that does not claim dual-role power`,
     `the partner's source capabilities do not start with a fixed supply`,
     `8 PR_Swap requests made`, `3 swaps accepted`, `the partner asked for
     the sink role`, `the source role was asked for through power_role`.
   - No line at all when the hub asked first (with `sink_tx_ok_rp` on, as
     by default): then the hub's PR_Swap took the place of the question.
   Afterwards `echo Y | sudo tee $P/sink_tx_ok_rp`.
4. **A partner whose PDOs keep the `E` without a supply**: the hub without
   its charger, plugged in.  Expect exactly one reconnect about 5 s after
   plugging in and then `swap accepted but the partner did not supply power,
   failed swap 1 of 2; not asking again until the partner has been away for
   a minute`; no further reconnect while it stays.  Unplugged for more than a
   minute and plugged in again: once more, `failed swap 2 of 2`; the third
   time `paused after 2 failed swaps until the port has been a power sink
   for 30 minutes`.
5. **A choice of the user's**: as source to the hub with its charger, `echo
   source | sudo tee /sys/class/typec/port0/power_role`: `the source role was
   asked for through power_role; not asking again until the partner has been
   away for a minute`, and no swap afterwards.
6. **Nothing changes** for a USB stick, a keyboard (no `swap back` line, no
   PD traffic), a plain PD charger (the tablet is sink from the start) and,
   with `swap_back_to_sink` at `N`, for the hub.  A PD 3 phone as sink
   shows `cc:=5` after its contract (Rp 3.0 A, SinkTxOK) and keeps charging
   from the tablet without `VBUS off`; a PD 2 one shows `cc:=4` there.

What to save from each run: the TCPM log (it empties when read) and
`journalctl -k -b -o short-monotonic`.

## A boot with the dock attached: the USB host that died

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
retention, as upstream does for the same domains on sm8650.  The kernel
build applies it for both tablets, and the SM-X910's test kernels have had
it since the second.  With it the SM-X910 has been booted three times with
the Satechi attached, and each time the USB host controller came through
the suspend cycle with the dock's devices still on the bus afterwards.

The SM-X710 needs it as well.  On 2026-11-21 it was booted on its kit's
kernel, which does not have the patch, with the Anker hub attached and the
hub's charger unplugged.  It came up as power source and USB host at 14.7 s
and ran the boot's suspend cycle from 22.7 to 26.8 s, with nothing on the
bus: that hub enumerates no device while the tablet powers it (see "Still
open").  At 718 s the hub's charger was plugged in.  The port attached
again, the kernel's own request made it host on a 9 V contract as sink, and
the probe of the new xHCI failed (`Host halt failed, -110`,
`can't setup: -110`), so no device came up.  In the boot before, whose
suspend cycle ran with the port in device role, the same hub attached at
6846 s and the xHCI started without error.  That is one observation, with
the SM-X910's signature.  The SM-X710's kernel kit now carries the patch,
and that kernel has not run on the tablet.  The test that confirms it is
the same boot with the patch and with devices on the bus: the hub's charger
in its PD port, or a self-powered dock.  A boot whose bus stays empty does
not put the host controller to the test.  Both kits' `verify.sh` fail when
they find a host controller that died.

The failed probe had a second effect.  It leaves the xHCI device without
driver data, and 6.6 s later the controller runtime-suspended: the
`dwc3-qcom` glue read the port speeds through that missing data, a NULL
pointer dereference in `dwc3_qcom_suspend()`.  At the next reboot a service
that writes the controller's runtime power setting hung behind it, and the
shutdown stalled for 7.5 minutes.
`dwc3-qcom-check-hcd-before-reading-port-speed.patch`, for both tablets,
treats a port without that data as empty, so the suspend completes.  It
does not bring the host controller back: that is the power domain change's
part, and the xHCI device stays without a driver until the next role
change.  It has not run on a tablet.

One question is open beyond the boot cycle, where the supply behind the
domains stays up: whether the state also survives a real deep sleep.  Until
that is shown, the SM-X910's sleep hook, which takes the port out of host
role around each sleep, stays.  The SM-X710 has no such hook installed, so a
sleep in host role there is that test.

## Switches

Both behaviours are on by default on both boards and can be changed at run
time; a change applies from the next attach.  The driver reads the two
parameters for as long as the port is looking for a partner, the last time
when one shows, and keeps what it read until the port looks for one again,
so one attach is never handled half with the old value and half with the
new.  An attach is not the same as a plug-in.  An error recovery, the one
after a data role mismatch for instance, drops the connection and sends
the port looking again with the dock still plugged in, and the attach that
follows takes the values as they are then.  A Soft Reset keeps the attach
and the values with it, and so does a Hard Reset that the port comes
through attached.

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
  in host role applies to more of them.  On the SM-X910, and once on the
  SM-X710, the xHCI did not come through a suspend in host role without the
  power domain change (the section above).  On the SM-X910 the sleep hook
  still takes the port out of host role for each sleep; a dock's devices
  disconnect and reconnect around it.
- **A program that writes the port's `data_role` can get `EAGAIN`**
  (`Resource temporarily unavailable`): while a VDM exchange is under way,
  from the moment TCPM queues a VDM (discovery's, for one) until that
  exchange is over, and when the swap ended with the role unchanged.  It
  used to be told that the swap was done.  Writing again is the answer to
  both.  A write to `data_role`, `power_role` or `vconn_source` that waits
  behind a message of the partner's is answered later than before: once its
  swap has run, not at the ready state in between.
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

On the SM-X910 the [Type-C roles kit](../tools/inplace/typec-roles-ultra/README.md)
carries a test kernel on top of release v1.2.5.  On the SM-X710 the
[Type-C roles kit](../tools/inplace/typec-roles/README.md) carries a test
kernel on top of the hang-log kit.  Either kit's `verify.sh dock` runs these
steps for each plug-in, keeps the log and gives a verdict, and its
`verify.sh swap` does the same for a swap asked for from userspace.

Expected: `[host]` and `[sink]`, `Charging` with a positive current (`Full`
and no current near 100 %), the dock's hub in `lsusb`.

In the log, after the contract (the bracketed suffix of each line left out):

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
stay, as the Satechi's did not in three attaches of seven, shows the port
going back to waiting and taking the Rp that follows:

```
state change SNK_TRY_WAIT_DEBOUNCE_CHECK_VBUS -> SNK_TRY_WAIT
pending state change SNK_TRY_WAIT -> SRC_TRYWAIT @ 528 ms
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

**The Satechi is the case to watch**, plugged into the running tablet or
attached at boot: its Rp that goes away again is what the first revision
lost.  With `try_snk_extra_wait_ms=0` it attaches as it always did, with the
tablet as power source: no charging, but a stable connection.

Repeat at least ten times with each dock, including a boot with the dock
attached and a plug after login, and check that the role is the one reported:
the data role, not the fact that a DR_Swap went out.  After a boot with the
dock attached and devices behind it, also check that the kernel log has no
`xhci-hcd` line with `HC died` or `Host halt failed` and that the dock's
devices are still there after the first half minute.  Then the things that
must not change: a plain PD charger (charges, no swap unless it offers one),
an unpowered hub and a USB stick (enumerate, about 0.6 s later than before),
and on the SM-X710 the A-to-C cable to the build host (gadget network up).
The Anker hub cannot serve as the unpowered hub: without its charger it
enumerates nothing (see "Still open").

## Still open

- Why the dock sends a Soft_Reset after its own swap is not known.  That it
  comes even when the tablet has sent nothing as host (7 of 29) speaks
  against the tablet's first message in the new role as the trigger.
- The three fixes for a swap asked for from userspace, the switches at run
  time and the `dwc3-qcom` NULL check have not been tried on a tablet, and
  the third fix and the NULL check are in no kernel that has run.  Not done
  on the SM-X710: `dock` and `swap` runs on a kit kernel, the Satechi on a
  kit kernel, and a boot with the dock attached and devices on the bus.  A
  real sleep with a dock attached has not been tried on either tablet.
- Whether the power domain change brings the SM-X710's USB host controller
  through a boot with a dock attached: its kernel with the patch has not run
  on the tablet.  Idle and suspend power with retention on the SM-X710 are
  not measured.
- The Anker hub enumerates no USB device while the tablet powers it: on the
  SM-X710 on the kernel before the kit and on the kit's, and on the SM-X910
  on a kit kernel, each time with a 5 V 900 mA contract and its DisplayPort
  alternate mode entered.  Why is not known.  With its charger it enumerated
  13 devices on the SM-X910.
- Whether the USB power domains keep their state through a real deep sleep
  on the SM8550, and what keeping them in retention costs in sleep.  If the
  state is kept, the sleep hook's role change can go, and a dock's devices
  stay connected across a sleep.
- How much of the 123 to 147 ms between Rd and the report of an Rp is the
  dock's and how much the SM5714's own debounce (four times the dock's VBUS
  was on 26 to 29 ms before the report).  If the controller's share can be
  shortened far enough, the standard Try.SNK is met and the extra wait is
  only needed for a dock that tries the sink role first itself.
- Writes to `power_role` and `vconn_source` (`tcpm_pr_set()`,
  `tcpm_vconn_set()`) have neither of the first two fixes: their swap can
  still be started while a VDM exchange is under way, and after the wait
  they can return 0 with the role unchanged, as there is no role check like
  `tcpm_dr_set()`'s.  That shows when a swap of another kind from the
  partner took the queued swap's place.  Of the three fixes only the third
  covers them.  Nothing here writes `power_role` or `vconn_source`.
- The VDM guard looks at the VDM state machine when the write starts the
  swap.  A PD 3.x source then waits tSinkTx (16 ms) before it sends, and a
  VDM exchange that starts in that time is not looked at again.  A swap
  that a ready state takes from `upcoming_state` goes out without passing
  `tcpm_ams_start()` again, so none of its checks run for it.
- A Soft Reset or a Hard Reset does not complete a pending swap with an
  error: the write waits for the ready state after it.  A port without
  `request_dfp_as_sink` keeps a queued swap through a Hard Reset it
  receives.  That is the one premise of the three drops that a Hard Reset
  can break: it resets the data role and VCONN, so the partner's swap after
  it need not give the role the queued swap was for.
- A request that the SM5714 reports as discarded (`PD TX complete, status: 1`
  right after the DR_Swap in the log) still waits 60 ms for an answer and
  counts as one of the three.  A dock that starts its own discovery right
  after the contract could use all three up that way.  Neither dock here
  does: none of the nine requests on record was discarded.
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
  [DisplayPort notes](gts9u-displayport.md)).  The boots of that kind with
  these changes ended as described above; DisplayPort through the dock was
  not looked at in them.
