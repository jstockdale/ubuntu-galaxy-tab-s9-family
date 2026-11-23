#!/bin/sh
# Checks after booting the Type-C roles kit, and a recorder for plug-in tests.
#   sh ~/typec-roles/verify.sh                  kernel, key, switches, the root's
#                                               ext4, UFS, PCIe, GPU, roles now
#                                               (with sudo also the UFS host
#                                               clocks' enable counts)
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
*) sed -n '2,18s/^# //p' "$0" >&2; exit 2 ;;
esac

echo "kernel: $(uname -v)"
id=$(build_id)
want=$(cat "$kit/BUILD_ID" 2>/dev/null)
rev=$(cat "$kit/REVISION" 2>/dev/null)
[ -n "$want" ] && [ "$id" = "$want" ] &&
	ok "running the kit's kernel (build ID $(echo "$id" | cut -c1-12)${rev:+, revision $rev})" ||
	bad "build ID $id, the kit's is ${want:-unknown}"
# ext4 that gave up on a failing disk keeps "rw" among its options and adds
# emergency_ro or shutdown: that root takes no write.
root_opts=$(findmnt -no OPTIONS /)
echo "$root_opts" | tr ',' '\n' | grep -qx rw &&
	! echo "$root_opts" | tr ',' '\n' | grep -qxE 'emergency_ro|shutdown' &&
	ok 'root is rw' || bad "root is not writable (mounted ${root_opts:-unknown})"
# ext4's record of the root.  With an error recorded and deleted files held
# open, a boot after a reset does not mount the root (this port's initramfs
# has no fsck; docs/resume-recovery.md): any error is a FAIL.
root_src=$(findmnt -no SOURCE /)
root_name=$(readlink -f "$root_src" 2>/dev/null)
root_name=${root_name:-$root_src}
root_name=${root_name##*/}
root_errors=$(cat "/sys/fs/ext4/$root_name/errors_count" 2>/dev/null)
case $root_errors in
0) ok "ext4 has recorded no error on the root ($root_name: errors_count 0)" ;;
''|*[!0-9]*) note "cannot read /sys/fs/ext4/$root_name/errors_count: ext4's error count of the root was not checked" ;;
*) bad "ext4 has recorded $root_errors errors on the root ($root_name, the last in" \
	"$(cat "/sys/fs/ext4/$root_name/last_error_func" 2>/dev/null):$(cat "/sys/fs/ext4/$root_name/last_error_line" 2>/dev/null)):" \
	'e2fsck -f it from recovery before the next reset (docs/resume-recovery.md, "Repairing the root from recovery")' ;;
esac

k=$(journalctl -k -b --no-pager -q 2>/dev/null)
# Four checks below read this boot's kernel log; without it none can pass.
[ -n "$k" ] || bad "cannot read this boot's kernel log (journalctl -k -b): the module key, ramoops, the" \
	'module failures and the USB host controller were not checked'
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

# Revision 9's debug switches (gcc-sm8550-gdsc-debug-switches.patch and
# rpmhpd-debug-retention-floor.patch), in revision 10's kernel too: run-time
# module parameters that every boot starts with off.  On the kit's kernel
# each has to be there and read N: sleep-test.sh sets them for a run only,
# and every run ends in a restart.  Another kernel may have none (revision 8
# and before).
sw_others=
for sw in gcc_sm8550.debug_pcie0_off:/sys/module/gcc_sm8550/parameters/debug_pcie0_off \
	gcc_sm8550.debug_pcie0_phy_off:/sys/module/gcc_sm8550/parameters/debug_pcie0_phy_off \
	gcc_sm8550.debug_usb_off:/sys/module/gcc_sm8550/parameters/debug_usb_off \
	rpmhpd.debug_ret_floor:/sys/module/rpmhpd/parameters/debug_ret_floor; do
	v=$(cat "${sw#*:}" 2>/dev/null)
	if [ -n "$want" ] && [ "$id" = "$want" ]; then
		[ "$v" = N ] && ok "debug switch ${sw%%:*} = N (off)" ||
			bad "debug switch ${sw%%:*} is ${v:-missing}, expected N (off): the kit's kernel starts every boot with it off, and only a sleep test sets it, until its restart"
	elif [ -e "${sw#*:}" ]; then
		sw_others="$sw_others ${sw%%:*}=${v:--}"
	fi
done
if [ -z "$want" ] || [ "$id" != "$want" ]; then
	if [ -n "$sw_others" ]; then
		note "debug switches of the running kernel, which is not the kit's:$sw_others"
	else
		note 'the running kernel is not the kit'"'"'s and offers no debug switches (the kernels of revisions 9 and 10 have them)'
	fi
fi

# What the hang-log kit brought is still there: same configuration, same DTB.
cfg=$(zcat /proc/config.gz 2>/dev/null)
for c in CONFIG_DETECT_HUNG_TASK=y CONFIG_SOFTLOCKUP_DETECTOR=y CONFIG_WQ_WATCHDOG=y \
	CONFIG_NETCONSOLE=m CONFIG_PSTORE_RAM=y CONFIG_PSTORE_FTRACE=y; do
	printf '%s\n' "$cfg" | grep -qx "$c" || bad "config: no $c"
done
ok 'config: the hang-log detectors, netconsole and pstore options are as built'
[ "$(cat /proc/sys/kernel/hung_task_timeout_secs 2>/dev/null)" = 60 ] &&
	ok 'kernel.hung_task_timeout_secs = 60' || bad 'kernel.hung_task_timeout_secs is not 60'
if [ -n "$k" ]; then
	printf '%s\n' "$k" | grep -q 'ramoops: using 0x200000@0x880900000, ecc: 96' &&
		ok 'ramoops registered with ECC 96' || bad 'no "ramoops: using 0x200000@0x880900000, ecc: 96" this boot'
fi

path=$(modinfo -n netconsole 2>/dev/null)
signer=$(modinfo -F sig_key netconsole 2>/dev/null)
[ -n "$path" ] && [ "$signer" = "$(cat "$kit/SIGNER" 2>/dev/null)" ] &&
	ok "netconsole module present and signed with the kit's key (${path##*/})" ||
	bad "netconsole module missing or not signed with the kit's key (${path:-none})"
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

# The UFS PHY (revision 8).  The PCS reset before each calibration leaves no
# trace of its own in the log.  The diagnostic built into the kit's kernel
# logs a line at every calibration, from inside the function that now resets
# the PCS on every SM8550 PHY: on the kit's kernel those lines are what can
# be seen of the change, and make-kit.sh checked on the build that this
# calibration has no board check.  On any other kernel nothing tells.
ufs_fail='phy initialization timed-out|Failed to calibrate PHY|Enabling the controller failed|Host init failed|ufshcd_wl_resume failed'
ufs_diag='calibration entry: SW_RESET|after the PCS_READY timeout: SW_RESET|PCS_READY .*SERDES_START|1 s after SERDES_START: SW_RESET|power-up write: PWRDN'
# The PCIe host and its PHY log two of those failures in the same words
# ("qcom-pcie 1c00000.pcie: Host init failed: -16", "qcom-qmp-pcie-phy
# 1c06000.phy: phy initialization timed-out"): their lines are the PCIe
# check's below, not the storage's.
pcie_dev='[0-9a-f]+\.pcie: |qmp-pcie-phy [0-9a-f]+\.phy: '
if [ -n "$k" ]; then
	# The kernel's own lines only: sleep-test.sh writes its verdicts to the
	# kernel log as well, and a failed cycle's verdict quotes the first
	# failure line.
	ku=$(printf '%s\n' "$k" | grep -v 'gts9u sleep-test: ')
	cal=$(printf '%s\n' "$ku" | grep -c 'calibration entry: SW_RESET')
	n=$(printf '%s\n' "$ku" | grep -E "$ufs_fail" | grep -cvE "$pcie_dev")
	if [ "$n" != 0 ]; then
		bad "the UFS PHY or host failed to come up this boot ($n lines; the diagnostic's lines follow theirs):"
		printf '%s\n' "$ku" | grep -E "$ufs_fail|$ufs_diag" | grep -vE "$pcie_dev" | tail -12
	elif [ "$cal" != 0 ]; then
		ok "UFS PHY: $cal calibrations this boot, each logged by the diagnostic of the kit's kernel, none failed"
		note "last calibration: $(printf '%s\n' "$ku" | grep 'calibration entry: SW_RESET' | tail -n 1 |
			sed 's/.*calibration entry: //')"
	elif [ -n "$want" ] && [ "$id" = "$want" ]; then
		bad "no UFS PHY calibration line this boot: the kit's kernel logs one at every calibration" \
			"(the kernel's own log: sudo dmesg | grep 'calibration entry')"
	else
		note 'the running kernel is not the kit'"'"'s: whether it resets the UFS PHY'"'"'s PCS before calibration cannot be seen from userspace'
	fi
	# Revision 10's second diagnostic (qmp-ufs-pwrdn-readback-diagnostic.patch):
	# a line right before each calibration entry, with the PHY's power-up
	# write read back at once and 100 us later.  On the kit's kernel every
	# calibration has one.
	pw=$(printf '%s\n' "$ku" | grep -c 'power-up write: PWRDN')
	if [ "$pw" != 0 ]; then
		note "last power-up write: $(printf '%s\n' "$ku" | grep 'power-up write: PWRDN' | tail -n 1 |
			sed 's/.*power-up write: //') (0x1 is the PHY powered up)"
	fi
	if [ -n "$want" ] && [ "$id" = "$want" ] && [ "$cal" != "$pw" ]; then
		bad "$cal UFS PHY calibrations and $pw power-up write lines this boot: the kit's kernel logs one before every calibration"
	fi
fi

# Revision 10 leaves out unpark-pcie0-pipe-mux.patch: with the PIPE clock
# mux revert the clock's own enable switches PCIe0's pipe clock to the PHY.
# The Wi-Fi's link has to come up at boot as before ("PCIe Gen.2 x1 link
# up"), and no PCIe host or PHY failure line may follow.
pcie_fail='Link failed to come up|Host init failed|phy initialization timed-out|failed to resume'
if [ -n "$k" ]; then
	up=$(printf '%s\n' "$ku" | grep -E '[0-9a-f]+\.pcie: PCIe Gen\.[0-9]+ x[0-9]+ link up' | tail -n 1)
	pf=$(printf '%s\n' "$ku" | grep -E "$pcie_dev" | grep -E "$pcie_fail")
	if [ -n "$pf" ]; then
		bad "the PCIe host or its PHY failed this boot ($(printf '%s\n' "$pf" | wc -l) lines):"
		printf '%s\n' "$pf" | tail -n 5
	elif [ -n "$up" ]; then
		ok "PCIe: the link came up this boot (${up##*.pcie: })"
	elif [ -n "$want" ] && [ "$id" = "$want" ]; then
		bad "no PCIe link-up line this boot: the Wi-Fi's link did not come up (revision 10 switches its pipe clock by the clock's own enable, without unpark-pcie0-pipe-mux.patch)"
	else
		note 'no PCIe link-up line this boot'
	fi
fi

# Revision 10 runs the GPU RSC's sleep and wake sequences at every power
# cycle of the GPU's GMU (msm-adreno-a6xx-fix-stale-rpmh-votes.patch); before
# it neither ran.  One that times out says so.
gpu_rsc='Unable to power off the GPU RSC|Unable to power on the GPU RSC|GPU RSC sequence stuck while waking up the GPU'
if [ -n "$k" ]; then
	n=$(printf '%s\n' "$ku" | grep -cE "$gpu_rsc")
	if [ "$n" = 0 ]; then
		ok 'GPU: no RSC sleep or wake sequence failed this boot'
	else
		bad "the GPU RSC's sleep or wake sequence failed this boot ($n lines):"
		printf '%s\n' "$ku" | grep -E "$gpu_rsc" | tail -n 3
	fi
fi

# Revision 10's ufs-qcom-enable-only-lane-clocks.patch: ufshcd enables the
# UFS host's clocks once while UFS works and not while it idles, and the
# inline crypto engine's driver holds its core and AHB clocks from its probe
# on, and takes them once more while UFS is resumed: no count above 3.
# Before it every runtime resume of the UFS host enabled them once more
# (enable counts of 522 to 525 on 2026-11-23).  debugfs is root's: this runs
# under sudo only.  Reading clk_summary wakes the clock controllers for a
# moment, which a check outside a sleep test can afford.
clk_sum=/sys/kernel/debug/clk/clk_summary
if [ -r "$clk_sum" ]; then
	clks=$(awk '$1 ~ /^(gcc_ufs_phy_(axi|ahb|unipro_core|ice_core)_clk|tcsr_ufs_pad_clkref_en)$/ &&
		$2 ~ /^[0-9]+$/ && !($1 in seen) {
			seen[$1] = 1
			printf "%s%s %s/%s", n++ ? ", " : "", $1, $2, $3
			if ($2 + 0 > m) m = $2 + 0
		} END { printf "|%d", m }' "$clk_sum" 2>/dev/null)
	most=${clks##*|} clks=${clks%|*}
	if [ -z "$clks" ]; then
		note "no UFS host clock in $clk_sum"
	elif [ -z "$want" ] || [ "$id" != "$want" ]; then
		note "UFS host clocks of the running kernel, which is not the kit's (enable/prepare): $clks"
	elif [ "$most" -gt 3 ]; then
		bad "UFS host clocks enabled up to $most times ($clks; enable/prepare): with the kit's kernel no count is above 3"
	else
		ok "UFS host clocks (enable/prepare): $clks"
	fi
else
	note "the UFS host clocks' enable counts need root: sudo sh $kit/verify.sh"
fi

# The root's mount this boot, from the kernel's own lines (fs/ext4/super.c,
# fs/jbd2/journal.c).  An error that the mount found recorded, in the
# superblock or in the journal's, or that ext4 recorded since, fails in any
# boot.  A journal that had to be replayed ("recovery required on readonly
# filesystem"), or that held transactions ext4 did not ask to replay ("JBD2:
# Clearing recovery information on journal", which names no device), is
# what a power loss or a forced restart leaves, and wrong only in the boot
# right after sleep-test.sh's restart: that root was frozen, its journal
# flushed and needs_recovery clear.  That boot is known by the run's
# "SLEEP-TEST RESTART" line in the pmsg that this boot archived
# (ubuntu-gts9u-pstore-archive: its INFO names the archiving boot).
if [ -n "$k" ]; then
	pst=/var/lib/ubuntu-gts9u/pstore
	boot=$(cat /proc/sys/kernel/random/boot_id 2>/dev/null)
	after_run=unknown
	if [ ! -d "$pst" ]; then
		unknown_why="there is no $pst"
	elif [ ! -r "$pst" ] || [ ! -x "$pst" ]; then
		unknown_why="$pst is not readable: group adm, or sudo"
	else
		after_run=no
		for d in "$pst"/[0-9]*; do
			[ -n "$boot" ] && grep -qs "by boot $boot" "$d/INFO" || continue
			! grep -rqs 'gts9u sleep-test: [0-9-]* SLEEP-TEST RESTART: ' "$d" || after_run=yes
		done
	fi
	rn=$root_name
	case $rn in
	''|*[!A-Za-z0-9_-]*) note "cannot name the root's device ($root_src): its ext4 lines this boot were not checked" ;;
	*)
		e=$(printf '%s\n' "$ku" | grep -E "EXT4-fs warning \\(device $rn\\): .*Filesystem error recorded from previous mount|EXT4-fs \\($rn\\): warning: mounting fs with errors|EXT4-fs error \\(device $rn\\)|Orphan file not empty on read-only fs")
		if [ -n "$e" ]; then
			bad "ext4 found or recorded an error on the root ($rn) this boot: e2fsck -f it from recovery" \
				'before the next reset (docs/resume-recovery.md, "Repairing the root from recovery"):'
			printf '%s\n' "$e" | tail -n 5
		else
			ok "the root ($rn) was mounted with no ext4 error found or recorded this boot"
		fi
		r=$(printf '%s\n' "$ku" | grep -E "EXT4-fs \\($rn\\): INFO: recovery required on readonly filesystem|JBD2: Clearing recovery information on journal")
		case $after_run:${r:+found} in
		yes:found)
			bad "this is the boot after a sleep test's restart, and a journal was not empty at the mount: the" \
				'root was not frozen at the restart, or something wrote to it while it was; check it with' \
				'e2fsck -fn from recovery:'
			printf '%s\n' "$r" | tail -n 3 ;;
		yes:)
			ok 'the boot after a sleep test'"'"'s restart: the root'"'"'s journal was empty at the mount (frozen to the restart)'
			o=$(printf '%s\n' "$ku" | grep -E "EXT4-fs \\($rn\\): (orphan cleanup on readonly fs|recovery complete|mounted filesystem)" |
				sed 's/.*EXT4-fs ([^)]*): //' | tr '\n' ';')
			[ -z "$o" ] || note "the root's mount: ${o%;}" ;;
		no:found)
			note "the root's journal was replayed at this boot's mount ($(printf '%s\n' "$r" | head -n 1 | sed 's/.*\] //;s/^kernel: //')):" \
				'what a power loss or a forced restart leaves' ;;
		unknown:found)
			note "the root's journal was replayed at this boot's mount: normal after a power loss, wrong right after a" \
				"sleep test's restart, and whether this boot is one cannot be told ($unknown_why)" ;;
		esac ;;
	esac
fi

[ -d "$port" ] && ok "Type-C port registered: data $(role data_role), power $(role power_role)," \
	"partner $([ -e "$partner" ] && echo present || echo none)" || bad "no $port"
# A boot with the dock attached has the port as USB host when the boot's
# suspend cycle runs; the host controller has to come back from it.  Without
# the power domain change it did not, on the SM-X910 and once here.
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
systemctl is-active --quiet typec-sink-host 2>/dev/null &&
	note 'typec-sink-host is running; stop it before any plug-in test (sudo systemctl stop typec-sink-host)'
note "plug-in tests: sudo sh $kit/verify.sh dock 10"
[ "$fail" = 0 ] && echo 'all checks passed' || echo 'some checks FAILED'
exit "$fail"
