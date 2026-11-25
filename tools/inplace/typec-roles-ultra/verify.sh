#!/bin/sh
# Checks after booting the Type-C roles kit, and a recorder for plug-in tests.
#   sh ~/typec-roles/verify.sh                  kernel, key, switches, roles now
#   sudo sh ~/typec-roles/verify.sh dock [N]    N plug-ins of a powered dock
#                                               (default 10), a verdict for each
#   sudo sh ~/typec-roles/verify.sh record [N]  the same without a verdict: a
#                                               charger, a stick, a hub, a cable
#                                               (default 1)
#   sudo sh ~/typec-roles/verify.sh now         save the TCPM log as it is, for
#                                               a boot with the dock attached
#   sudo sh ~/typec-roles/verify.sh swap [N]    N plug-ins of a powered dock
#                                               (default 1) with the host role
#                                               asked for through data_role, as
#                                               a helper in userspace would
#   sh ~/typec-roles/verify.sh summary LOG...   the counters of saved TCPM logs
# dock, record, now and swap also run on the kernel before the kit, for a
# baseline.  They write only below ~/typec-roles/logs.  Reading the TCPM log
# empties it; that is how each trial gets its own (the log keeps its first
# 1024 lines and drops the rest, so a log that is not emptied misses the
# plug-in).  dock, record and swap also read it every second during a
# plug-in and add what they read to the trial's: a dock that re-attaches in
# a loop fills it in seconds.  swap switches the kernel's own request off
# (sink_host_swap) for the run and puts the switch back when it ends, also
# when it is interrupted.
set -u
export LC_ALL=C
kit=$(cd "$(dirname "$0")" && pwd)
fail=0
ok() { echo "ok   $*"; }
bad() { echo "FAIL $*"; fail=1; }
note() { echo "note $*"; }
port=/sys/class/typec/port0
partner=/sys/class/typec/port0-partner
params=/sys/module/sm5714_usbpd/parameters
tab=$(printf '\t')

build_id() { od -An -tx1 -v /sys/kernel/notes | tr -d ' \n' | grep -o '474e5500[0-9a-f]\{40\}' | cut -c9-; }
role() { sed -n 's/.*\[\(.*\)\].*/\1/p' "$port/$1" 2>/dev/null; }
count() { grep -cE -- "$1" "$2"; }

# The counters of one TCPM log, tab-separated: attaches, requests for the
# host role, data role swaps done, data role mismatches, Try.SNK that ended
# as sink, Try.SNK that gave up, soft resets, hard resets, error recoveries,
# lines lost to a full log, and how often an Rp went away again inside
# Try.SNK and the wait went on.  "pending state change" lines are not counted.
counters() {
	printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s' \
		"$(count '\] state change [A-Z_]+ -> (SNK|SRC)_ATTACHED( |$)' "$1")" \
		"$(count 'Requesting DFP before discovery' "$1")" \
		"$(count '\] state change DR_SWAP_SEND -> DR_SWAP_CHANGE_DR( |$)' "$1")" \
		"$(count 'Data role mismatch' "$1")" \
		"$(count '\] state change SNK_TRY_WAIT_DEBOUNCE_CHECK_VBUS -> SNK_ATTACHED( |$)' "$1")" \
		"$(count '\] state change SNK_TRY_WAIT(_DEBOUNCE(_CHECK_VBUS)?)? -> SRC_TRYWAIT( |$)' "$1")" \
		"$(count '\] state change [A-Z_]+ -> SOFT_RESET(_SEND)?( |$)' "$1")" \
		"$(count 'Received hard reset|\] state change [A-Z_]+ -> HARD_RESET_SEND( |$)' "$1")" \
		"$(count '\] state change [A-Z_]+ -> ERROR_RECOVERY( |$)' "$1")" \
		"$(count '\] overflow$' "$1")" \
		"$(count '\] state change SNK_TRY_WAIT_DEBOUNCE(_CHECK_VBUS)? -> SNK_TRY_WAIT( |$)' "$1")"
}
counter_names='attaches	dfp_requests	dr_swaps	role_mismatches	try_snk_kept	try_snk_lost	soft_resets	hard_resets	error_recoveries	overflow	try_snk_rp_lost'
describe() { # the counters as a sentence
	echo "$1" | awk -F '\t' '{
		printf "%s attach%s, host role requested %s, swapped %s, role mismatches %s", $1, $1 == 1 ? "" : "es", $2, $3, $4
		if ($5 + $6 + $11) printf ", Try.SNK kept %s lost %s", $5, $6
		if ($11) printf " (Rp came and went %s)", $11
		if ($7 + $8 + $9) printf ", soft resets %s, hard resets %s, error recoveries %s", $7, $8, $9
		if ($10) printf ", LOG OVERFLOWED"
		printf "\n" }'
}

if [ "${1:-}" = summary ]; then
	shift
	[ $# -gt 0 ] || { echo 'summary needs TCPM log files' >&2; exit 2; }
	for f in "$@"; do
		[ -r "$f" ] || { echo "cannot read $f" >&2; exit 2; }
		echo "$f: $(describe "$(counters "$f")")"
	done
	exit 0
fi

state() { # what the port, the battery and the bus look like now
	for f in data_role power_role power_operation_mode vconn_source; do
		echo "$f: $(cat "$port/$f" 2>/dev/null)"
	done
	if [ -e "$partner" ]; then
		echo "partner: present, PD $(cat "$partner/supports_usb_power_delivery" 2>/dev/null)," \
			"id_header $(cat "$partner/identity/id_header" 2>/dev/null)," \
			"product $(cat "$partner/identity/product" 2>/dev/null)"
	else
		echo 'partner: none'
	fi
	for f in /sys/class/power_supply/*battery*/status /sys/class/power_supply/*battery*/current_now \
		/sys/class/power_supply/*battery*/capacity /sys/class/power_supply/tcpm-source-psy-*/online \
		/sys/class/power_supply/tcpm-source-psy-*/voltage_now \
		/sys/class/power_supply/tcpm-source-psy-*/current_max \
		/sys/class/power_supply/tcpm-source-psy-*/usb_type; do
		[ -r "$f" ] && echo "${f#/sys/class/power_supply/}: $(cat "$f" 2>/dev/null)"
	done
	echo 'usb devices (root hubs left out):'
	usb_devices | sed 's/^/  /'
}
usb_devices() {
	for d in /sys/bus/usb/devices/[0-9]*; do
		case $d in *:*) continue ;; esac
		[ -r "$d/idVendor" ] || continue
		echo "${d##*/} $(cat "$d/idVendor"):$(cat "$d/idProduct") $(cat "$d/speed" 2>/dev/null) Mb/s" \
			"$(cat "$d/manufacturer" 2>/dev/null) $(cat "$d/product" 2>/dev/null)"
	done
}

ask_host() { # FILE: once a partner shows, write "host" to data_role as a
	# helper in userspace would.  TCPM registers the partner when the port is
	# ready, under its contract, the moment before it starts its discovery:
	# twenty writes 5 ms apart to meet that discovery, then five more 300 ms
	# apart.  The first write that is accepted ends it.  FILE gets which
	# write that was and what the data role was right after it.  Ends with
	# the script.
	tries=0
	until [ -e "$partner" ]; do
		kill -0 $$ 2> /dev/null || return 0
		sleep 0.005
	done
	while [ "$tries" -lt 25 ] && kill -0 $$ 2> /dev/null; do
		tries=$((tries + 1))
		# To sysfs >> is the same as >.
		if { echo host >> "$port/data_role"; } 2> /dev/null; then
			echo "write $tries was accepted, the data role right after it: $(role data_role)" > "$1"
			return 0
		fi
		if [ "$tries" -lt 20 ]; then sleep 0.005; else sleep 0.3; fi
	done
	echo "none of $tries writes was accepted" > "$1"
}

plug_test() { # dock|record|now|swap [COUNT]
	mode=$1
	[ "$(id -u)" = 0 ] || { echo "$mode needs sudo (the TCPM log is root's)" >&2; exit 1; }
	case $mode in dock) total=${2:-10} ;; record|swap) total=${2:-1} ;; now) total=1 ;; esac
	case $total in ''|*[!0-9]*|0) echo "not a count: $total" >&2; exit 2 ;; esac
	set -- /sys/kernel/debug/usb/tcpm-*/log
	[ $# = 1 ] && [ -r "$1" ] || { echo 'no single TCPM log in /sys/kernel/debug/usb' >&2; exit 1; }
	tcpm=$1
	[ -d "$port" ] || { echo "no $port" >&2; exit 1; }
	if systemctl is-active --quiet typec-sink-host 2>/dev/null; then
		echo 'typec-sink-host is running and would change the roles itself; stop it first:' >&2
		echo '  sudo systemctl stop typec-sink-host' >&2
		exit 1
	fi
	stamp=$(date +%Y%m%d-%H%M%S)
	dir=$kit/logs/$stamp-$mode
	mkdir -p "$dir" || exit 1
	own() { [ -z "${SUDO_UID:-}" ] || chown -R "$SUDO_UID:${SUDO_GID:-$SUDO_UID}" "$kit/logs"; }
	# swap: the kernel's own request is off for the run and a writer asks
	# instead.  Neither may outlive the run, however it ends.
	was='' writer='' writes=''
	stop_writer() {
		[ -n "$writer" ] || return 0
		# A writer that has said its word has ended; only one that has not
		# is signalled, so that the signal cannot reach a stranger.
		[ -e "$writes" ] || kill "$writer" 2> /dev/null
		wait "$writer" 2> /dev/null
		writer=''
	}
	restore() {
		stop_writer
		[ -z "$was" ] || echo "$was" > "$params/sink_host_swap"
		was=''
	}
	drain() { # SECONDS: wait, and add the TCPM log to the trial's every second
		t=0
		while [ "$t" -lt "$1" ]; do
			sleep 1
			cat "$tcpm" >> "$out.tcpm.log"
			t=$((t + 1))
		done
	}
	trap 'restore; own; echo; echo "stopped; what was recorded is in $dir"; exit 1' INT TERM HUP
	if [ "$mode" = swap ] && [ -e "$params/sink_host_swap" ]; then
		# Written before the first unplug: a kernel that reads the switch
		# only when the port empties then has it for the first plug-in too.
		was=$(cat "$params/sink_host_swap")
		echo N > "$params/sink_host_swap" ||
			{ was=''; echo "cannot write $params/sink_host_swap" >&2; exit 1; }
	fi
	{
		echo "kernel: $(uname -v)"
		echo "build ID: $(build_id) (the kit's: $(cat "$kit/BUILD_ID" 2>/dev/null))"
		echo "sink_host_swap: $(cat "$params/sink_host_swap" 2>/dev/null || echo absent)"
		[ -z "$was" ] || echo "sink_host_swap before this run and after it: $was"
		echo "try_snk_extra_wait_ms: $(cat "$params/try_snk_extra_wait_ms" 2>/dev/null || echo absent)"
		echo "TCPM log: $tcpm"
	} > "$dir/kernel"
	[ "$(build_id)" = "$(cat "$kit/BUILD_ID" 2>/dev/null)" ] ||
		note "the running kernel is not the kit's: this is a baseline run"
	printf 'trial\tverdict\tdata_8s\tpower_8s\tdata_14s\tpower_14s\tpartner\tusb_devices\tbattery\t%s\n' \
		"$counter_names" > "$dir/results.tsv"
	if [ "$mode" = swap ]; then
		printf 'trial\tasked\trefused_in_vdm\tsent\trole_unchanged\tcut_short\twrite\n' > "$dir/swap.tsv"
		refused_total=0
	fi

	n=1
	while [ "$n" -le "$total" ]; do
		out=$dir/trial-$n
		if [ "$mode" = now ]; then
			d1=$(role data_role) p1=$(role power_role)
			d2=$d1 p2=$p1
		else
			[ -e "$partner" ] && echo "trial $n/$total: unplug the USB-C cable at the tablet"
			wait_partner absent 300 || { bad "trial $n: still attached after 5 minutes"; break; }
			cat "$tcpm" > /dev/null
			: > "$out.tcpm.log"
			echo "gts9u typec-roles: $stamp trial $n" > /dev/kmsg
			if [ "$mode" = swap ]; then
				writes=$out.writes
				rm -f "$writes"
				ask_host "$writes" &
				writer=$!
			fi
			echo "trial $n/$total: plug it in now"
			wait_partner present 300 || { bad "trial $n: nothing attached within 5 minutes"; break; }
			drain 8
			d1=$(role data_role) p1=$(role power_role)
			drain 6
			d2=$(role data_role) p2=$(role power_role)
			stop_writer
		fi
		[ -e "$partner" ] && there=yes || there=no
		if [ "$mode" = now ]; then
			cat "$tcpm" > "$out.tcpm.log"
			dmesg > "$out.dmesg"
		else
			cat "$tcpm" >> "$out.tcpm.log"
			dmesg | sed -n "/gts9u typec-roles: $stamp trial $n\$/,\$p" > "$out.dmesg"
		fi
		state > "$out.state"
		c=$(counters "$out.tcpm.log")
		usb=$(usb_devices | wc -l)
		battery=$(cat /sys/class/power_supply/*battery*/status 2>/dev/null | head -1)
		verdict=-
		if [ "$mode" = dock ]; then
			# The roles, not the fact that a swap was sent: host and sink at
			# both samples, one attach, no role mismatch, still attached.  A
			# log that overflowed between two reads a second apart lost lines
			# and cannot show the one attach; only a port that re-attaches or
			# resets in a loop logs that much.
			if [ "$d1 $p1 $d2 $p2 $there" = 'host sink host sink yes' ] &&
				[ "$(echo "$c" | cut -f1)" = 1 ] && [ "$(echo "$c" | cut -f4)" = 0 ] &&
				[ "$(echo "$c" | cut -f10)" = 0 ]; then
				verdict=PASS
			else
				verdict=FAIL
				fail=1
			fi
		fi
		if [ "$mode" = swap ]; then
			# Not the roles: with the kernel's own request off the dock's
			# swap can still end them.  What counts is what the writes did:
			# none cut short by a Soft Reset, none reported done with the
			# role unchanged, and the switch really off.  A log that lost
			# lines between two reads cannot show that.
			asked=$(count '\] AMS DATA_ROLE_SWAP start$' "$out.tcpm.log")
			refused=$(count 'VDM in progress, DATA_ROLE_SWAP not started' "$out.tcpm.log")
			# From AMS_START, or from a ready state that took the swap
			# out of upcoming_state.
			sent=$(count '\] state change [A-Z_]+ -> DR_SWAP_SEND( |$)' "$out.tcpm.log")
			unchanged=$(count 'Data role swap ended with the role unchanged' "$out.tcpm.log")
			cut=$(count '\] state change DR_SWAP_SEND -> [A-Z_]*SOFT_RESET' "$out.tcpm.log")
			queued=$(count 'Swap is still queued, left pending' "$out.tcpm.log")
			# A queued DR_SWAP_SEND, PR_SWAP_SEND or VCONN_SWAP_SEND.
			dropped=$(count 'Partner swapped first, queued [A-Z_]+ dropped' "$out.tcpm.log")
			overflow=$(echo "$c" | cut -f10)
			wrote=$(cat "$writes" 2> /dev/null)
			refused_total=$((refused_total + refused))
			# A write that was accepted with the tablet not the host right
			# after it, and no swap of the tablet's in the log that a reset
			# could have undone since: that write reported what did not happen.
			lied=0
			if [ "$overflow" = 0 ]; then
				case $wrote in *': host') ;; write*) [ "$(echo "$c" | cut -f3)" != 0 ] || lied=1 ;; esac
			fi
			if [ "$overflow" != 0 ]; then
				fail=1
			elif [ "$cut" != 0 ] || [ "$lied" = 1 ] || [ "$(echo "$c" | cut -f2)" != 0 ]; then
				verdict=FAIL
				fail=1
			elif [ "$asked" != 0 ]; then
				verdict=PASS
			fi
			printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$n" "$asked" "$refused" "$sent" "$unchanged" "$cut" \
				"${wrote:-the writer was stopped before it was done}" >> "$dir/swap.tsv"
		fi
		printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
			"$n" "$verdict" "$d1" "$p1" "$d2" "$p2" "$there" "$usb" "$battery" "$c" >> "$dir/results.tsv"
		case $verdict in -) lead="trial $n:" ;; *) lead="trial $n: $verdict," ;; esac
		echo "$lead data ${d1:-?}/${d2:-?}, power ${p1:-?}/${p2:-?} (at 8 s/14 s), partner $there," \
			"$usb USB devices, battery ${battery:-?}; $(describe "$c")"
		if [ "$mode" = swap ]; then
			echo "  data_role: $asked swaps asked for, $refused refused during a VDM exchange, $sent sent," \
				"$unchanged ended with the role unchanged, $cut cut short by a Soft Reset;" \
				"${wrote:-the writer was stopped before it was done}"
			# The third fix's two lines; neither decides the verdict.
			[ "$queued" = 0 ] || echo "  still queued when the port was ready again, left pending: $queued"
			[ "$dropped" = 0 ] || echo "  dropped because the partner swapped first: $dropped"
			[ "$(echo "$c" | cut -f2)" = 0 ] ||
				echo '  the kernel asked for the host role itself: the switch was not off for this plug-in'
			[ "$lied" = 0 ] || echo '  a write reported a swap that did not happen'
			if [ "$overflow" != 0 ]; then
				echo '  the TCPM log overflowed between two reads, so lines were lost: this plug-in' \
					'cannot be judged'
			elif [ "$verdict" = - ]; then
				case $wrote in
				none*) echo '  every write was refused before TCPM started a swap (the log has no AMS' \
					'DATA_ROLE_SWAP): the port was not ready at any of them, or the partner has no PD' ;;
				*) echo '  no swap was asked for: not a PD source with the tablet as its USB device' ;;
				esac
			fi
		fi
		n=$((n + 1))
	done
	restore
	own
	echo "recorded in $dir"
	if [ "$mode" = swap ]; then
		passed=$(awk -F '\t' '$2 == "PASS"' "$dir/results.tsv" | wc -l)
		echo "$passed of $total plug-ins: a swap asked for through data_role, none cut short by a" \
			'Soft Reset, none reported done with the role unchanged'
		[ "$refused_total" != 0 ] ||
			note 'no write fell into a VDM exchange, so the refusal itself was not seen: plug in again'
		# A run in which no plug-in got a verdict has shown nothing.
		judged=$(awk -F '\t' 'NR > 1 && $2 != "-"' "$dir/results.tsv" | wc -l)
		[ "$judged" != 0 ] || bad 'no plug-in could be judged (the lines under each plug-in say why)'
	fi
	if [ "$mode" = dock ]; then
		passed=$(awk -F '\t' '$2 == "PASS"' "$dir/results.tsv" | wc -l)
		echo "$passed of $total plug-ins ended as USB host and power sink with one attach"
		[ "$passed" = "$total" ] || fail=1
	fi
	exit "$fail"
}
wait_partner() { # present|absent SECONDS; absent has to hold for 3 s, a
	# port that re-attaches in a loop is without a partner in between
	left=$(($2 * 5))
	held=0
	while [ "$left" -gt 0 ]; do
		if [ -e "$partner" ]; then
			[ "$1" = present ] && return 0
			held=0
		else
			held=$((held + 1))
			[ "$1" = absent ] && [ "$held" -ge 15 ] && return 0
		fi
		sleep 0.2
		left=$((left - 1))
	done
	return 1
}
case ${1:-} in
dock|record|now|swap) plug_test "$@" ;;
'') ;;
*) sed -n '2,15s/^# //p' "$0" >&2; exit 2 ;;
esac

echo "kernel: $(uname -v)"
id=$(build_id)
want=$(cat "$kit/BUILD_ID" 2>/dev/null)
[ -n "$want" ] && [ "$id" = "$want" ] && ok "running the kit's kernel (build ID $(echo "$id" | cut -c1-12))" ||
	bad "build ID $id, the kit's is ${want:-unknown}"
tr '\0' '\n' < /proc/device-tree/compatible | grep -qx 'samsung,gts9uwifi' && ok 'this is an SM-X910' ||
	bad 'this is not an SM-X910'
# Revision 8, the deep-sleep groundwork (README, "Revision 8"): what the
# SM-X710 kit's revision 14 sleeps deeply with.  On the kit's own kernel from
# revision 8 on (the kit's REVISION) a check that fails is a FAIL; on any
# other kernel it is a note, as revisions 1 to 7 and the release have none of
# it.  Reads only, no root.
r8=0
[ -n "$want" ] && [ "$id" = "$want" ] && [ "$(cat "$kit/REVISION" 2>/dev/null)" -ge 8 ] 2>/dev/null && r8=1
r8_bad() { if [ "$r8" = 1 ]; then bad "$*"; else note "$*"; fi; }
findmnt -no OPTIONS / | tr ',' '\n' | grep -qx rw && ok 'root is rw' || bad 'root is not rw'

k=$(journalctl -k -b --no-pager -q 2>/dev/null)
# Four checks below read this boot's kernel log, and revision 8's that do
# (the early helper's lines, ramoops, the PCIe guard, Bluetooth); without it
# none can pass.
[ -n "$k" ] || bad "cannot read this boot's kernel log (journalctl -k -b): the module key, the module" \
	'failures, the USB host controller, L15B and revision 8'"'"'s kernel-log checks were not checked'
key=$(cat "$kit/KEY_ID" 2>/dev/null)
if [ -n "$k" ]; then
	printf '%s\n' "$k" | grep -q "Loaded X.509 cert .*: ${key:-none}" &&
		ok "the kernel trusts the kit's module key (${key%"${key#????????????}"})" ||
		bad "no 'Loaded X.509 cert' line for the kit's key ${key:-unknown}"
fi

# The two switches of the change, at the driver's defaults.
param_is() { # name value
	v=$(cat "$params/$1" 2>/dev/null)
	[ "$v" = "$2" ] && ok "sm5714_usbpd.$1 = $v" || bad "sm5714_usbpd.$1 is ${v:-missing}, expected $2"
}
param_is sink_host_swap Y
param_is try_snk_extra_wait_ms 600

# The module tree is the kit's: every module is signed with its key.
signer=$(cat "$kit/SIGNER" 2>/dev/null)
total=0
other=0
for ko in $(find "/usr/lib/modules/$(uname -r)" -name '*.ko' -o -name '*.ko.zst' 2>/dev/null); do
	total=$((total + 1))
	[ "$(modinfo -F sig_key "$ko" 2>/dev/null)" = "$signer" ] || other=$((other + 1))
done
[ "$total" -gt 0 ] && [ "$other" = 0 ] && ok "$total modules, all signed with the kit's key" ||
	bad "$other of $total modules are not signed with the kit's key"
# The locked-down kernel refuses a module of another build ("unsigned module
# loading is restricted"); the others are what a kernel without lockdown, or
# a module built against other structures, would log.
modfail='unsigned module loading is restricted|failed to validate module|module verification failed|unavailable key|disagrees about version|Unknown symbol'
if [ -n "$k" ]; then
	n=$(printf '%s\n' "$k" | grep -ciE "$modfail")
	[ "$n" = 0 ] && ok 'no module was refused, and no symbol failures this boot' ||
		{ bad 'modules refused or symbol failures this boot:'
		  printf '%s\n' "$k" | grep -iE "$modfail" | tail -10; }
fi

[ -d "$port" ] && ok "Type-C port registered: data $(role data_role), power $(role power_role)," \
	"partner $([ -e "$partner" ] && echo present || echo none)" || bad "no $port"
# A boot with the dock attached has the port as USB host when the boot's
# suspend cycle runs; the host controller has to come back from it.
dead='xhci-hcd .*(HC died|not responding to stop endpoint command|Host halt failed|Host System Error|Controller not ready at resume)'
if [ -n "$k" ]; then
	cycles=$(printf '%s\n' "$k" | grep -c 'PM: suspend exit')
	n=$(printf '%s\n' "$k" | grep -cE "$dead")
	if [ "$n" != 0 ]; then
		bad 'the USB host controller died this boot:'
		printf '%s\n' "$k" | grep -E "$dead" | tail -5
	elif [ "$cycles" = 0 ]; then
		note 'the USB host controller did not die, but no suspend cycle has run yet this boot: run this again later'
	else
		ok "the USB host controller did not die this boot (suspend cycles so far: $cycles)"
	fi
	# Not a death: the controller came back without its state and was set up
	# again, and the devices behind it were enumerated again.
	n=$(printf '%s\n' "$k" | grep -c 'xhci-hcd .*xHC error in resume')
	[ "$n" = 0 ] || note "the USB host controller came back from a suspend without its state and was set up again (times this boot: $n)"
fi
if [ "$(role data_role)" = host ] && [ -e "$partner" ]; then
	n=$(usb_devices | wc -l)
	[ "$n" -gt 0 ] && ok "USB host, devices on the bus: $n" ||
		note 'USB host with no device on the bus (nothing behind the dock, or its hub did not come up)'
fi
# PM8550 LDO15 (vreg_l15b_1p8: the eUSB2 repeater's 1.8 V and the WCN7850's
# I/O rail) held in HPM, as on the SM-X710 since its kit's revision 14: the
# node has neither regulator-allow-set-load nor regulator-allowed-modes, and
# the regulator reads normal (HPM).  This board's DTB is the pinned release
# one, whose ldo15 still has both (the bootloader's copy, /sys/firmware/fdt,
# keeps them); a kernel with the L15B step of
# kernel/drivers/gts9u-performance.c (2026-11-25) takes them out of the live
# tree at boot and logs a "gts9u-performance: L15B" line.  Revisions 1 to 7
# have no such step, so without its line this is a note.
if [ -n "$k" ]; then
	l15=/proc/device-tree/soc@0/rsc@17a00000/regulators-0/ldo15
	l15_name=$(tr -d '\0' < "$l15/regulator-name" 2>/dev/null)
	l15_mode=
	for r in /sys/class/regulator/regulator.*; do
		[ "$(cat "$r/name" 2>/dev/null)" = vreg_l15b_1p8 ] && l15_mode=$(cat "$r/opmode" 2>/dev/null)
	done
	l15_step=$(printf '%s\n' "$k" | sed -n 's/^.*\(gts9u-performance: L15B.*\)$/\1/p' | tr '\n' ';' | sed 's/;$//; s/;/; /g')
	l15_dt=
	if [ -z "$l15_step" ]; then
		# From revision 8 on the kit's kernel has the step, and then its
		# missing line is a FAIL.
		r8_bad "DT: L15B (vreg_l15b_1p8) not checked: this kernel has no L15B step (no \"gts9u-performance: L15B\" line this boot), and with the pinned DTB L15B may drop to LPM in a sleep (it reads ${l15_mode:-no mode} now)"
	elif [ "$l15_name" != vreg_l15b_1p8 ]; then
		l15_dt="no vreg_l15b_1p8 node at $l15 ($l15_step)"
	elif ! printf '%s\n' "$l15_step" | grep -q '^gts9u-performance: L15B held in HPM'; then
		l15_dt="the kernel's L15B step failed: $l15_step"
	elif [ -e "$l15/regulator-allow-set-load" ] || [ -e "$l15/regulator-allowed-modes" ]; then
		l15_dt="L15B (vreg_l15b_1p8) may drop to LPM: its node still has regulator-allow-set-load or regulator-allowed-modes, though the kernel logged \"$l15_step\" (it reads ${l15_mode:-no mode} now)"
	elif [ "$l15_mode" != normal ]; then
		l15_dt="L15B's node has neither property, but the regulator reads ${l15_mode:-no mode} (its opmode), not normal (HPM)"
	else
		ok "DT: L15B (vreg_l15b_1p8) held in HPM: no regulator-allow-set-load or regulator-allowed-modes, and it reads normal ($l15_step)"
	fi
	if [ -n "$l15_dt" ]; then
		if [ -n "$want" ] && [ "$id" = "$want" ]; then
			bad "DT: $l15_dt"
		else
			note "DT: $l15_dt"
		fi
	fi
fi
# Revision 8's checks (r8 and r8_bad, above).
if [ -n "$k" ]; then
	# The early helper's two Ultra overlays: the SM-X710's board DTS changes
	# for the SoC's sync_state hand-off, and its ramoops region.
	for line in 'gts9u-performance: SoC hand-off: gcc under CX, GMU post-init providers, crypto engine disabled' \
		'gts9u-performance: ramoops in the pmsg carve-out at 0x880900000'; do
		printf '%s\n' "$k" | grep -qF "$line" && ok "kernel log: $line" ||
			r8_bad "no \"$line\" line this boot (kernel/drivers/gts9u-performance.c)"
	done
	# ramoops on that carve-out, with the SM-X710's ECC, and its pmsg.
	if printf '%s\n' "$k" | grep -qF 'ramoops: using 0x200000@0x880900000, ecc: 96' && [ -e /dev/pmsg0 ]; then
		ok 'ramoops: 0x200000@0x880900000, ECC 96, and its pmsg device (the sleep test'"'"'s records survive a reset)'
	else
		r8_bad 'no ramoops on 0x880900000 with ECC 96, or no pmsg device: nothing of a run that never comes back survives the reset'
	fi
fi
# The running DT as that overlay leaves it.
dt=/proc/device-tree/soc@0
if [ -e "$dt/clock-controller@100000/power-domains" ] && [ -e "$dt/gmu@3d6a000/post-init-providers" ] &&
	[ "$(tr -d '\0' < "$dt/crypto@1dfa000/status" 2>/dev/null)" = disabled ]; then
	ok 'DT: gcc under CX (power-domains), the GMU'"'"'s post-init-providers, crypto@1dfa000 disabled'
else
	r8_bad 'DT: gcc has no power-domains, the GMU no post-init-providers, or crypto@1dfa000 is not disabled'
fi
# sync_state: the RPMh power domains, gcc and gpu_cc synced at boot (the
# video clock controller built in, the overlay above).  What holds one back
# is in the kernel's "sync_state() pending due to" lines.
unsynced=
for d in 17a00000.rsc:power-controller 100000.clock-controller 3d90000.clock-controller; do
	[ "$(cat "/sys/bus/platform/devices/$d/state_synced" 2>/dev/null)" = 1 ] || unsynced="$unsynced $d"
done
if [ -z "$unsynced" ]; then
	ok 'sync_state: rpmhpd, gcc and gpu_cc synced at boot'
else
	r8_bad "sync_state: not synced:$unsynced$([ -z "$k" ] || printf '%s\n' "$k" | sed -n 's/^.* \([^ ]*\): sync_state() pending due to \(.*\)$/; \1 waits for \2/p' | tr -d '\n')"
fi
# The interconnect keeps its boot vote until a run releases it (the sleep
# test's release-floor): released at boot it hung the SM-X710's boot cycle.
icc_held=
for d in /sys/bus/platform/devices/*; do
	[ -e "$d/state_synced" ] && [ -e "$d/of_node/#interconnect-cells" ] || continue
	[ "$(cat "$d/state_synced" 2>/dev/null)" = 1 ] || icc_held="$icc_held ${d##*/}"
done
note "interconnect floor: $([ -n "$icc_held" ] && echo "held (unsynced:$icc_held), as every boot of this board holds it until sleep-test.sh release-floor" || echo released)"
# The SoC sleep statistics, built in: what tells a deep sleep.
set -- /sys/bus/platform/drivers/qcom_stats/*.sram
[ -e "$1" ] && ok "qcom_stats: bound to ${1##*/} (/sys/kernel/debug/qcom_stats, root)" ||
	r8_bad 'qcom_stats: no device bound to the qcom_stats driver: no sleep counts'
# The debug switches of the SM-X710 kit's build, each as it boots: only a
# sleep test changes them, until its restart.
for sw in gcc_sm8550.debug_pcie0_off:/sys/module/gcc_sm8550/parameters/debug_pcie0_off:Y \
	gcc_sm8550.debug_pcie0_phy_off:/sys/module/gcc_sm8550/parameters/debug_pcie0_phy_off:N \
	gcc_sm8550.debug_usb_off:/sys/module/gcc_sm8550/parameters/debug_usb_off:N \
	rpmhpd.debug_ret_floor:/sys/module/rpmhpd/parameters/debug_ret_floor:N \
	pcie_qcom.debug_root_port_d3:/sys/module/pcie_qcom/parameters/debug_root_port_d3:N \
	qcom_stats.debug_cxpc_violators:/sys/module/qcom_stats/parameters/debug_cxpc_violators:N; do
	name=${sw%%:*} rest=${sw#*:}
	v=$(cat "${rest%:*}" 2>/dev/null)
	[ "$v" = "${rest##*:}" ] && ok "debug switch $name = $v, as it boots" ||
		r8_bad "debug switch $name is ${v:-missing}, expected ${rest##*:} at boot"
done
if [ -n "$k" ]; then
	# The PCIe power domain's guard in the boot's first suspend (the panel
	# cycle): the host kept its link, and pcie_0_gdsc stayed on.  One that
	# did not hold is a FAIL on any kernel.
	gfirst=$(printf '%s\n' "$k" | sed -n 's/^.*\.pcie: system sleep: \(.*\)$/\1/p' | head -n 1)
	if printf '%s\n' "$k" | grep -qF 'system sleep: the link was kept, but genpd powered its power domain off'; then
		bad 'the PCIe power domain guard did not hold this boot: genpd powered the domain off under a link the host kept'
	elif [ "$gfirst" = 'the link was kept, and its power domain stayed on' ]; then
		ok 'PCIe power domain guard: in this boot'"'"'s first suspend (the panel cycle) the host kept its link and pcie_0_gdsc stayed on'
	elif printf '%s\n' "$k" | grep -q 'PM: suspend exit'; then
		r8_bad "PCIe power domain guard: this boot's first suspend logged ${gfirst:-no \"system sleep:\" line of the PCIe host}"
	else
		note 'no suspend has run yet this boot (the panel cycle runs about 22 s in): the PCIe guard was not checked'
	fi
	# The two geni UARTs, powered down in every system suspend: after the
	# first one, no interrupt disabled and no HCI command timed out, and the
	# Bluetooth controller there.
	bt_dead='nobody cared|Disabling IRQ #|hci[0-9]+: (command 0x[0-9a-f]+ tx timeout|Opcode 0x[0-9a-f]+ failed: -110)'
	if printf '%s\n' "$k" | grep -qE "$bt_dead"; then
		bad 'an interrupt was disabled or a Bluetooth command timed out this boot:'
		printf '%s\n' "$k" | grep -E "$bt_dead" | head -n 5
	elif [ -e /sys/class/bluetooth/hci0 ]; then
		ok 'Bluetooth: hci0, no disabled interrupt and no HCI command timeout this boot'
	else
		r8_bad 'no Bluetooth controller (/sys/class/bluetooth/hci0)'
	fi
fi
case $(cat /sys/class/tty/ttyMSM0/power/wakeup 2>/dev/null) in
disabled) ok 'console ttyMSM0: wakeup disabled (its UART powers down in system suspend)' ;;
*) r8_bad 'console ttyMSM0: wakeup not disabled: uart_suspend_port() then leaves its UART running, and the force suspend powers it down all the same' ;;
esac
# What a confirming sleep needs besides the kernel: the sensors' sleep hook
# of device 2.75 (or the sensors-hook kit), which stops iio-sensor-proxy.
grep -q 'iio-sensor-proxy' /usr/lib/systemd/system-sleep/ubuntu-gts9u-sensors 2>/dev/null &&
	ok 'the sensors'"'"' sleep hook stops iio-sensor-proxy (sleep-test.sh sensors=hook can run)' ||
	note 'the sensors'"'"' sleep hook does not stop iio-sensor-proxy (device 2.72 or before): install tools/inplace/sensors-hook before a confirming sleep (sleep-test.sh sensors=hook refuses without it)'
# The stopgap writes the data role from userspace and would collide with the
# kernel's own request.
if systemctl is-active --quiet typec-sink-host 2>/dev/null ||
	systemctl is-enabled --quiet typec-sink-host 2>/dev/null; then
	bad 'the typec-sink-host stopgap is running or enabled: sudo systemctl disable --now typec-sink-host'
else
	ok 'the typec-sink-host stopgap is neither running nor enabled'
fi
note "plug-in tests: sudo sh $kit/verify.sh dock 10"
[ "$fail" = 0 ] && echo 'all checks passed' || echo 'some checks FAILED'
exit "$fail"
