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
#   sh ~/typec-roles/verify.sh summary LOG...   the counters of saved TCPM logs
# dock, record and now also run on the kernel before the kit, for a baseline.
# They write only below ~/typec-roles/logs.  Reading the TCPM log empties it;
# that is how each trial gets its own (the log keeps its first 1024 lines and
# drops the rest, so a log that is not emptied misses the plug-in).
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

plug_test() { # dock|record|now [COUNT]
	mode=$1
	[ "$(id -u)" = 0 ] || { echo "$mode needs sudo (the TCPM log is root's)" >&2; exit 1; }
	case $mode in dock) total=${2:-10} ;; record) total=${2:-1} ;; now) total=1 ;; esac
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
	trap 'own; echo; echo "stopped; what was recorded is in $dir"; exit 1' INT TERM HUP
	{
		echo "kernel: $(uname -v)"
		echo "build ID: $(build_id) (the kit's: $(cat "$kit/BUILD_ID" 2>/dev/null))"
		echo "sink_host_swap: $(cat "$params/sink_host_swap" 2>/dev/null || echo absent)"
		echo "try_snk_extra_wait_ms: $(cat "$params/try_snk_extra_wait_ms" 2>/dev/null || echo absent)"
		echo "TCPM log: $tcpm"
	} > "$dir/kernel"
	[ "$(build_id)" = "$(cat "$kit/BUILD_ID" 2>/dev/null)" ] ||
		note "the running kernel is not the kit's: this is a baseline run"
	printf 'trial\tverdict\tdata_8s\tpower_8s\tdata_14s\tpower_14s\tpartner\tusb_devices\tbattery\t%s\n' \
		"$counter_names" > "$dir/results.tsv"

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
			echo "gts9u typec-roles: $stamp trial $n" > /dev/kmsg
			echo "trial $n/$total: plug it in now"
			wait_partner present 300 || { bad "trial $n: nothing attached within 5 minutes"; break; }
			sleep 8
			d1=$(role data_role) p1=$(role power_role)
			sleep 6
			d2=$(role data_role) p2=$(role power_role)
		fi
		[ -e "$partner" ] && there=yes || there=no
		cat "$tcpm" > "$out.tcpm.log"
		if [ "$mode" = now ]; then
			dmesg > "$out.dmesg"
		else
			dmesg | sed -n "/gts9u typec-roles: $stamp trial $n\$/,\$p" > "$out.dmesg"
		fi
		state > "$out.state"
		c=$(counters "$out.tcpm.log")
		usb=$(usb_devices | wc -l)
		battery=$(cat /sys/class/power_supply/*battery*/status 2>/dev/null | head -1)
		verdict=-
		if [ "$mode" = dock ]; then
			# The roles, not the fact that a swap was sent: host and sink at
			# both samples, one attach, no role mismatch, still attached.
			if [ "$d1 $p1 $d2 $p2 $there" = 'host sink host sink yes' ] &&
				[ "$(echo "$c" | cut -f1)" = 1 ] && [ "$(echo "$c" | cut -f4)" = 0 ] &&
				[ "$(echo "$c" | cut -f10)" = 0 ]; then
				verdict=PASS
			else
				verdict=FAIL
				fail=1
			fi
		fi
		printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
			"$n" "$verdict" "$d1" "$p1" "$d2" "$p2" "$there" "$usb" "$battery" "$c" >> "$dir/results.tsv"
		case $verdict in -) lead="trial $n:" ;; *) lead="trial $n: $verdict," ;; esac
		echo "$lead data ${d1:-?}/${d2:-?}, power ${p1:-?}/${p2:-?} (at 8 s/14 s), partner $there," \
			"$usb USB devices, battery ${battery:-?}; $(describe "$c")"
		n=$((n + 1))
	done
	own
	echo "recorded in $dir"
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
dock|record|now) plug_test "$@" ;;
'') ;;
*) sed -n '2,12s/^# //p' "$0" >&2; exit 2 ;;
esac

echo "kernel: $(uname -v)"
id=$(build_id)
want=$(cat "$kit/BUILD_ID" 2>/dev/null)
[ -n "$want" ] && [ "$id" = "$want" ] && ok "running the kit's kernel (build ID $(echo "$id" | cut -c1-12))" ||
	bad "build ID $id, the kit's is ${want:-unknown}"
tr '\0' '\n' < /proc/device-tree/compatible | grep -qx 'samsung,gts9uwifi' && ok 'this is an SM-X910' ||
	bad 'this is not an SM-X910'
findmnt -no OPTIONS / | tr ',' '\n' | grep -qx rw && ok 'root is rw' || bad 'root is not rw'

k=$(journalctl -k -b --no-pager -q 2>/dev/null)
# Three checks below read this boot's kernel log; without it none can pass.
[ -n "$k" ] || bad "cannot read this boot's kernel log (journalctl -k -b): the module key, the module" \
	'failures and the USB host controller were not checked'
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
