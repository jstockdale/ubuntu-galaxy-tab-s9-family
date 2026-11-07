# DisplayPort over USB-C on the SM-X910

Status: the full chain works — PD negotiation, mode entry, SBU/AUX routing,
link training, EDID, a rendered frame — once the alternate-mode driver is
bound while the tablet is the data host.  Getting that binding to happen
reliably is what the typec re-probe patch fixes; three narrower issues
remain and are documented below with their evidence.

## Architecture

Everything DisplayPort-shaped was already wired when this work started:

- `usb_c_sbu_mux` (gpio-sbu-mux, GPIO183 enable active-low, GPIO180 select)
  routes the AUX pair onto the connector's SBU pins per orientation.
- The PS5169 combo redriver carries the SuperSpeed/DP main-link lanes and
  follows orientation and mode through the Type-C mux framework.
- The QMP combo PHY's DP half feeds `mdss_dp0`, which is enabled with
  `qcom,defer-hpd-until-first-resume` so the external encoder keeps out of
  the panel's one-time cold-boot recovery cycle.
- The connector advertises a DFP_D source alternate mode (pins C/D/E), and
  `displayport = <&mdss_dp0>` plus the msm-dp bridge patches deliver the
  out-of-band HPD into DRM.  `card1-DP-1` exists from boot.

TCPM drives the PD policy through the SM5714 transport, including SOP'
(cable) messages; the status-2 transmit pairs that show up in the tcpm
trace against passive adapters are the cable-marker probes timing out, not
a defect.

## Power

The DP controller is powered only while a sink is plugged, as on stock.
It is a child of the MDSS, so while it ran, the MDSS, `mdss_gdsc` and the
display clock controller stayed on with the panel blanked.  Nothing
resolves past the QMP PHY in the bridge chain (the PS5169 and SM5714
register no DRM bridge), so msm_dp is the connector's HPD bridge, and its
`hpd_enable` used to hold a runtime PM reference for an HPD pin that
nothing drives.  `msm-dp-oob-hpd-no-runtime-pm.patch` drops that reference
when a usb-c-connector's `displayport` property names the controller, and
`msm-dp-balance-plugged-runtime-pm.patch` keeps exactly one reference per
plugged sink, so the controller suspends again after unplug.

With no sink and the panel blanked, `ae90000.displayport-controller`,
`ae00000.display-subsystem` and `af00000.clock-controller` should read
`suspended` in `power/runtime_status`, and `mdss_gdsc` should be `off` in
`/sys/kernel/debug/pm_genpd/pm_genpd_summary`.  To get the old behaviour
back without a rebuild (A/B testing, or if an unblank misbehaves after
the MDSS power collapse), write `on` to the controller's `power/control`.

## The binding race (fixed by the typec re-probe patch)

A dock's DisplayPort mode is usually discovered while the tablet is still
UFP — discovery completes in the same PD exchange that precedes the dock's
DR_Swap.  `typec_displayport` refuses a port that is not the data host
(probe returns -EPROTO, logged as `error -71`), the mode is left registered
but driverless, and nothing re-probes it after the swap completes: the dock
sits entered-but-dark until a physical re-plug happens to win the race.

Two field behaviours fall out of that one defect, both reproduced on a
portable USB-C monitor with PD passthrough:

- Monitor asleep (HPD low): the adapter settles for power + USB and the
  desktop simply never extends.
- Monitor awake (HPD pending): the adapter expects Enter Mode promptly,
  times out, hard-resets, and renegotiates in a ~4 s contract storm
  (`0 mV → 5 V → 9 V → reset`) indefinitely.

The patch (`typec-reprobe-partner-altmodes-on-role-change.patch`) re-probes
driverless partner alternate modes whenever the port's data role changes,
which is the kernel doing what a manual
`echo port0-partner.0 > /sys/bus/typec/drivers/typec_displayport/bind`
was proven to do in recovery testing.

## Known issues (traces on file, fixes pending)

1. **HPD sampled only at Status/Configure on some adapters.**  The test
   adapter never sends Attention; if the monitor wakes after Configure, the
   link stays down (`hpd_link_status=0x0`, EDID 0 bytes) until the mode is
   re-entered.  Plug with the monitor awake, or re-enter the mode (driver
   rebind) to recover.  A possible future mitigation is re-querying DPStatus
   while configured with HPD low.
2. **xHCI can fail to re-initialise (-110) after repeated mode-exit
   cycling** within one session; host USB disappears (`lsusb` empty) until
   reboot.  The combo-PHY/redriver restore path on mode exit deserves a
   pass.  Note that an empty `lsusb` with nothing attached is normal on
   this dual-role port — the host session starts on attach.
3. **Cold-booting with a DP dock attached can leave the panel dark.**  The
   defer-HPD guard covers the encoder, but something in the boot-time
   alternate-mode negotiiation still interacts with the panel recovery
   cycle.  Boot undocked until this is run down.

## Test choreography

For a deterministic bring-up test: boot undocked, let the desktop settle,
wake the monitor, then make a single USB-C attach and keep hands off.
Diagnostics that proved most useful: the tcpm debugfs log for the VDM
ceremony (`Enter cmd 4 → Status cmd 16 → Configure cmd 17`),
`/sys/class/typec` for role/altmode/driver state, DRM debug `0x104` for the
AUX/EDID attempts, and `/sys/kernel/debug/gpio` for the SBU mux state.
