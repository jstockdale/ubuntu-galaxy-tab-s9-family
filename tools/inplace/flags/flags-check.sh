#!/bin/sh
# Evidence and verdict for one boot of the bring-up flag experiment
# (clk_ignore_unused / pd_ignore_unused / regulator_ignore_unused dropped one
# at a time on top of fix2-s2).  Root only, because clk_summary,
# pm_genpd_summary, regulator_summary and the trace buffer live in debugfs
# and tracefs.  Read-only apart from the files it writes under
# /var/log/gts9u-flags/<label>-<boot>/ (group adm, readable over plain SSH).
#
#   sudo sh ~/flags-kit/flags-check.sh <label>
#     label: control | flags-nopd | flags-noreg | flags-noclk | flags-none
#
# Each verdict line is "ok", "CHECK" (look at it) or "info".  The first run
# should be "control" on fix2-s2: later runs diff against the newest control
# capture.  Takes a few seconds; no load on the GPU, UFS or radios.
set -u
export LC_ALL=C
[ "$(id -u)" = 0 ] || { echo 'run with sudo' >&2; exit 1; }
label=${1:-}
case "$label" in
control) want_clk=1 want_pd=1 want_reg=1 ;;
flags-nopd) want_clk=1 want_pd=0 want_reg=1 ;;
flags-noreg) want_clk=1 want_pd=1 want_reg=0 ;;
flags-noclk) want_clk=0 want_pd=1 want_reg=1 ;;
flags-none) want_clk=0 want_pd=0 want_reg=0 ;;
*) echo "usage: sudo sh $0 control|flags-nopd|flags-noreg|flags-noclk|flags-none" >&2; exit 1 ;;
esac

# What clk_disable_unused() switches off on the Stage 2 X710, from the clk_summary
# of boot 972349f7 (fix2-s2, 145 s) and the ops of each clock: the only
# branches enabled in hardware with enable count 0 whose ops can disable them.
expected_clk='disp_cc_mdss_ahb1_clk
disp_cc_mdss_rscc_ahb_clk
disp_cc_mdss_rscc_vsync_clk
disp_cc_sleep_clk
gcc_boot_rom_ahb_clk
gcc_pcie_1_phy_rchng_clk'

boot=$(cut -c1-8 /proc/sys/kernel/random/boot_id)
top=/var/log/gts9u-flags
out=$top/$label-$boot
mkdir -p "$out"
chgrp adm "$top" "$out"
chmod 2750 "$top" "$out"
d=/sys/kernel/debug
tracefs=/sys/kernel/tracing
[ -d "$tracefs/events" ] || tracefs=$d/tracing

ok() { echo "  ok     $*"; }
ck() { echo "  CHECK  $*"; }
inf() { echo "  info   $*"; }
hdr() { echo "== $*"; }

# ---- capture -------------------------------------------------------------
dmesg > "$out/dmesg.txt"
cp /proc/cmdline "$out/cmdline.txt"
cut -d' ' -f1 /proc/uptime > "$out/uptime.txt"
cat /sys/class/drm/card*-DSI-1/dpms 2>/dev/null | head -1 > "$out/dsi-dpms.txt"
cp "$d/clk/clk_summary" "$out/clk_summary.txt" 2>/dev/null
cp "$d/pm_genpd/pm_genpd_summary" "$out/genpd.txt" 2>/dev/null
for g in "$d"/pm_genpd/*/; do
	[ -d "$g" ] || continue
	n=${g%/}; n=${n##*/}
	printf '%s state=%s active_ms=%s idle_ms=%s\n' "$n" \
		"$(cat "$g/current_state" 2>/dev/null)" "$(cat "$g/active_time" 2>/dev/null | tr -d ' ms')" \
		"$(cat "$g/total_idle_time" 2>/dev/null | tr -d ' ms')"
done > "$out/genpd-times.txt"
cp "$d/regulator/regulator_summary" "$out/regulator_summary.txt" 2>/dev/null
for r in /sys/class/regulator/regulator.*; do
	printf '%s state=%s users=%s\n' "$(cat "$r/name")" "$(cat "$r/state" 2>/dev/null)" "$(cat "$r/num_users")"
done | sort > "$out/regulator-state.txt"
cp "$d/interconnect/interconnect_summary" "$out/icc.txt" 2>/dev/null
grep -c 2147483647 "$out/icc.txt" > "$out/icc-intmax-count.txt" 2>/dev/null
cp "$d/devices_deferred" "$out/devices_deferred.txt" 2>/dev/null
find /sys/devices -maxdepth 6 -name state_synced 2>/dev/null | while read -r f; do
	echo "${f%/state_synced} $(cat "$f")"
done | sort > "$out/state_synced.txt"
trace_on=$(cat "$tracefs/events/clk/clk_disable/enable" 2>/dev/null || echo 0)
[ "$trace_on" = 1 ] && cp "$tracefs/trace" "$out/trace.txt"

# Enabled in hardware with enable count 0, from clk_summary: the input of
# clk_disable_unused() (it acts only on those whose ops can disable them).
clk_y0() {
	awk 'NF >= 9 && $2 ~ /^[0-9]+$/ && $3 ~ /^[0-9]+$/ && $9 ~ /^[YN?]$/ {
		if ($2 == 0 && $9 == "Y") print $1 }' "$1" | sort
}
genpd_status() {
	awk 'NR > 2 && /^[^ -]/ && NF >= 2 {print $1, $2}' "$1" | sort
}
[ -s "$out/clk_summary.txt" ] && clk_y0 "$out/clk_summary.txt" > "$out/clk-enabled-count0.txt"
[ -s "$out/genpd.txt" ] && genpd_status "$out/genpd.txt" > "$out/genpd-status.txt"

# ---- verdict -------------------------------------------------------------
hdr "boot $boot, $label, up $(cut -d. -f1 "$out/uptime.txt") s, $(uname -r)"
[ "$(cut -d. -f1 "$out/uptime.txt")" -ge 60 ] ||
	ck 'less than 60 s since boot: the regulator clean-up runs at ~44 s; run this again in a minute'
[ "$(cat "$out/dsi-dpms.txt")" = On ] ||
	ck "the screen is '$(cat "$out/dsi-dpms.txt")': unlock it and run this again, so the captures compare"
cmd=" $(cat /proc/cmdline) "
for t in clk_ignore_unused:$want_clk pd_ignore_unused:$want_pd regulator_ignore_unused:$want_reg; do
	tok=${t%:*} want=${t#*:}
	case "$cmd" in *" $tok "*) have=1 ;; *) have=0 ;; esac
	if [ "$have" = "$want" ]; then
		ok "$tok $( [ "$have" = 1 ] && echo present || echo absent) as $label expects"
	else
		ck "$tok $( [ "$have" = 1 ] && echo present || echo absent): this is not the $label image"
	fi
done
inf "extras: $(tr ' ' '\n' < /proc/cmdline | grep -E '^(initcall_blacklist|icc_core|trace_event|trace_buf_size)' | tr '\n' ' ')"

hdr 'kernel messages of the three late cleanups'
grep -E 'clk: (Not disabling|Disabling) unused clocks|genpd: (Not disabling|Disabling) unused power domains|regulator: Not disabling unused regulators' \
	"$out/dmesg.txt" | sed 's/^/  info   /'
# regulator_late_cleanup() logs every rail it switches off ("<name>: disabling")
# and any it could not ("couldn't disable").
regs=$(grep -E '\] [A-Za-z0-9_.@-]+: (disabling|couldn.t disable: .*)$' "$out/dmesg.txt")
if [ "$want_reg" = 0 ]; then
	if [ -z "$regs" ]; then
		ok 'regulator late cleanup switched nothing off (as the analysis predicts)'
	else
		echo "$regs" | sed 's/^/  CHECK  regulator cleanup: /'
	fi
else
	[ -z "$regs" ] || echo "$regs" | sed 's/^/  CHECK  /'
fi

if [ "$want_clk" = 0 ]; then
	hdr 'clocks switched off by clk_disable_unused()'
	tc=$(sed -n 's/^\[ *\([0-9]*\.[0-9]*\)\] clk: Disabling unused clocks.*/\1/p' "$out/dmesg.txt" | head -1)
	if [ -z "$tc" ]; then
		ck 'no "clk: Disabling unused clocks" line in dmesg'
	elif [ ! -s "$out/trace.txt" ]; then
		ck "clk tracing is not on (trace_event=clk:clk_disable missing?); only the clk_summary diff below is available"
	else
		hd=$(sed -n 's/^# entries-in-buffer\/entries-written: \([0-9]*\)\/\([0-9]*\).*/\1 \2/p' "$out/trace.txt")
		first=$(awk '{for (k = 1; k <= NF; k++) if ($k ~ /^[0-9]+\.[0-9]+:$/) {print substr($k, 1, length($k) - 1); exit}}' "$out/trace.txt")
		inf "trace entries/written: ${hd:-?}; first event at ${first:-?} s; cleanup at $tc s"
		awk -v t0="$tc" -v t1="$tc" 'BEGIN {t1 += 2}
			{ for (i = 1; i <= NF; i++) if ($i ~ /^\[[0-9]+\]$/) break
			  if (i > NF || i < 2) next
			  n = split($(i - 1), a, "-"); pid = a[n]
			  for (k = i + 1; k < NF; k++) if ($k ~ /^[0-9]+\.[0-9]+:$/) break
			  if (k >= NF) next
			  tss = substr($k, 1, length($k) - 1); ts = tss + 0
			  ev = $(k + 1)
			  if (pid == 1 && ts >= t0 + 0 && ts <= t1 && (ev == "clk_disable:" || ev == "clk_unprepare:"))
				print $(k + 2), substr(ev, 1, length(ev) - 1), tss }' "$out/trace.txt" > "$out/clk-disabled-at-boot.txt"
		if [ -n "$first" ] && awk -v f="$first" -v t="$tc" 'BEGIN {exit !(f + 0 > t + 0)}'; then
			ck "the trace buffer wrapped (first event after the cleanup): the list below is incomplete"
		elif [ -n "$hd" ] && [ "${hd% *}" != "${hd#* }" ]; then
			ck "the ring buffer overwrote $(( ${hd#* } - ${hd% *} )) events: if they include the cleanup, the list below is incomplete"
		fi
		sed 's/^/  info   /' "$out/clk-disabled-at-boot.txt"
		got=$(awk '$2 == "clk_disable" {print $1}' "$out/clk-disabled-at-boot.txt" | sort -u)
		want=$(printf '%s\n' "$expected_clk" | sort)
		extra=$(printf '%s\n' "$got" | grep -vxF -e "$want" | grep . || true)
		missing=$(printf '%s\n' "$want" | grep -vxF -e "$got" | grep . || true)
		if [ -z "$extra" ] && [ -z "$missing" ]; then
			ok 'exactly the six clocks the analysis predicted'
		else
			[ -z "$extra" ] || ck "switched off beyond the prediction: $(echo $extra)"
			[ -z "$missing" ] || inf "predicted but not switched off (off already at 13.7 s?): $(echo $missing)"
		fi
	fi
fi

hdr 'difference from the newest control capture'
ctl=$(ls -1dt "$top"/control-* 2>/dev/null | grep -v "/$label-$boot\$" | head -1)
if [ "$label" = control ]; then
	inf "this is the control; enabled-in-hardware with count 0: $(wc -l < "$out/clk-enabled-count0.txt" 2>/dev/null) clocks"
	inf "expected switch-offs once clk_ignore_unused is gone: $(echo $expected_clk)"
	for c in $expected_clk; do
		grep -qxF "$c" "$out/clk-enabled-count0.txt" 2>/dev/null || inf "$c is not enabled-with-count-0 on this boot"
	done
elif [ -z "$ctl" ]; then
	ck 'no control capture yet: run "sudo sh flags-check.sh control" on fix2-s2 first'
else
	inf "control: $ctl"
	# GPU clocks follow rendering: reported, never judged.
	gpu='^(gpu_cc_|gcc_gpu_|gcc_ddrss_gpu_)'
	gone=$(comm -23 "$ctl/clk-enabled-count0.txt" "$out/clk-enabled-count0.txt" 2>/dev/null | grep -v -E "$gpu" | tr '\n' ' ')
	gpug=$(comm -3 "$ctl/clk-enabled-count0.txt" "$out/clk-enabled-count0.txt" 2>/dev/null | tr -d '\t' | grep -E "$gpu" | tr '\n' ' ')
	new=$(comm -13 "$ctl/clk-enabled-count0.txt" "$out/clk-enabled-count0.txt" 2>/dev/null | grep -v -E "$gpu" | tr '\n' ' ')
	if [ "$want_clk" = 0 ]; then
		inf "enabled-with-count-0 in the control, now off: ${gone:-none}"
	else
		[ -z "$gone" ] && ok 'clk_summary: same enabled-with-count-0 set as the control (GPU clocks aside)' ||
			ck "clk_summary: enabled-with-count-0 in the control, now off: $gone"
	fi
	[ -z "$gpug" ] || inf "GPU clocks that differ (they follow rendering): $gpug"
	[ -z "$new" ] || inf "enabled-with-count-0 now, not in the control: $new"
	# The GPU domains follow rendering; mdss/mmcx and the panel rails follow
	# the screen, so a difference there only counts when the screen state
	# matched (capture with the screen on, every time).
	gd=$(diff "$ctl/genpd-status.txt" "$out/genpd-status.txt" 2>/dev/null | grep '^[<>]' | grep -v ' gpu_cc_' | tr '\n' ' ')
	if [ -z "$gd" ]; then
		ok 'power domains: same on/off status as the control (GPU domains ignored)'
	elif [ "$(cat "$ctl/dsi-dpms.txt" 2>/dev/null)" != "$(cat "$out/dsi-dpms.txt")" ]; then
		inf "power domains differ, but the screen was $(cat "$ctl/dsi-dpms.txt" 2>/dev/null) for the control and $(cat "$out/dsi-dpms.txt") now: $gd"
	else
		ck "power domains differ from the control: $gd"
	fi
	rd=$(diff "$ctl/regulator-state.txt" "$out/regulator-state.txt" 2>/dev/null | grep '^[<>]' | tr '\n' ' ')
	[ -z "$rd" ] && ok 'regulators: same state and users as the control' || inf "regulators differ from the control (screen on/off moves the panel rails): $rd"
fi
# A domain on with no active device is what pd_ignore_unused would keep on.
# A domain whose only users are subdomains counts as used while one of them is on.
awk 'NR > 2 && /^[^ -]/ {dom = $1; st[dom] = $2; n++; order[n] = dom; next}
	dom == "" {next}
	/^    / && ($2 == "active" || $2 == "unsupported" || $2 == "resuming") {act[dom] = 1; next}
	/^    / && ($2 == "suspended" || $2 == "suspending" || $2 == "error") {next}
	/^     / {line = $0; gsub(/,/, " ", line); kids[dom] = kids[dom] " " line}
	END {for (i = 1; i <= n; i++) {d = order[i]; if (st[d] != "on" || act[d]) continue
		used = 0; m = split(kids[d], k, " "); for (j = 1; j <= m; j++) if (st[k[j]] == "on") used = 1
		if (!used) print d}}' "$out/genpd.txt" 2>/dev/null |
	grep -v -E '^power-domain-(cpu[0-9]|cluster)$' > "$out/genpd-on-unused.txt"
[ -s "$out/genpd-on-unused.txt" ] && ck "power domains on with no active device: $(tr '\n' ' ' < "$out/genpd-on-unused.txt")" ||
	ok 'no power domain is on without an active device'

hdr 'health (read-only)'
synced=$(awk '{n = $1; sub(/.*\//, "", n); print n "=" $2}' "$out/state_synced.txt" | grep -E '^(17a00000.rsc:power-controller|100000.clock-controller|3d90000.clock-controller|af00000.clock-controller|aaf0000.clock-controller|ade0000.clock-controller|24100000.interconnect|interconnect-1)=' | tr '\n' ' ')
inf "state_synced: $synced"
case "$synced" in
*'24100000.interconnect=0'*'interconnect-1=0'*|*'interconnect-1=0'*'24100000.interconnect=0'*) ok 'Stage 2 interconnect pattern (gem_noc/mc_virt held)' ;;
*) ck 'interconnect state is not the Stage 2 pattern' ;;
esac
inf "INT_MAX lines: $(cat "$out/icc-intmax-count.txt" 2>/dev/null)"
pid=$(cat /sys/class/backlight/*/panel_id 2>/dev/null | head -1)
[ "$pid" = '80 00 04' ] && ok "panel id $pid" || ck "panel id '$pid'"
inf "DSI-1 $(cat /sys/class/drm/card*-DSI-1/enabled 2>/dev/null) dpms $(cat /sys/class/drm/card*-DSI-1/dpms 2>/dev/null) (Off while the screen is blanked)"
cb=$(systemctl show -p Result --value ubuntu-gts9u-panel-coldboot-recover.service 2>/dev/null)
[ "$cb" = success ] && ok 'boot-time panel cycle (coldboot-recover) succeeded' || ck "coldboot-recover result '$cb'"
grep -E 'ana38407 panel id|PM: suspend (entry|exit)' "$out/dmesg.txt" | head -6 | sed 's/^/  info   /'
findmnt -no OPTIONS / | grep -q '^rw' && ok 'root mounted rw' || ck "root mount options: $(findmnt -no OPTIONS /)"
w=$(ls /sys/class/net 2>/dev/null | grep '^wl' | head -1)
[ -n "$w" ] && [ "$(cat "/sys/class/net/$w/operstate")" = up ] && ok "Wi-Fi $w up" || ck "Wi-Fi interface '${w:-none}' not up"
[ -e /sys/class/bluetooth/hci0 ] && ok 'Bluetooth hci0 present' || ck 'no hci0'
u=$(cat /sys/class/udc/*/state 2>/dev/null | head -1)
[ "$u" = configured ] && ok 'USB gadget configured' || inf "USB gadget state '$u' (configured only with a host attached)"
grep -q Samsung-Galaxy-Tab /proc/asound/cards && ok 'sound card present' || ck 'no Samsung sound card in /proc/asound/cards'
for n in FTS1BA90A 'Wacom EMR' 'Book Cover Switch'; do
	grep -q "^N: Name=.*$n" /proc/bus/input/devices && ok "input: $n" || ck "input device '$n' missing"
done
g=$(cat /sys/class/devfreq/3d00000.gpu/cur_freq 2>/dev/null)
[ -n "$g" ] && ok "GPU devfreq at $g Hz" || ck 'no GPU devfreq'
hits=$({ grep -E -i 'underrun|vblank wait timed out|flip_done timed out|dsi_err_worker|TCON_RDY still low|I/O error|EXT4-fs (error|warning)|ufshcd.*(abort|err|fail|timeout)|AER: .*[Ee]rror|PCIe Bus Error|gmu.*(timeout|timed out|fail)|hfi.*(timeout|timed out|fail)|gdsc.*(stuck|timeout|fail)|rpmh.*(timeout|fail)|clk.*(stuck|status stuck)|unbalanced|soft lockup|hung_task|rcu.*stall' "$out/dmesg.txt"
	# Splats, case-sensitive (ramoops is not an oops).  The two WARNINGs every
	# X710 boot prints (dispcc rcg update_config, hci_uart refcount) are
	# counted below instead.
	grep -E 'Internal error: Oops|Unable to handle kernel|Kernel panic|BUG: |WARNING: CPU' "$out/dmesg.txt" |
		grep -v -E 'clk-rcg2\.c|refcount\.c'; } | grep -v -E 'x1-x3 nonzero in violation|OF: overlay: WARNING')
if [ -n "$hits" ]; then
	echo "$hits" | sed 's/^\[ *[0-9.]*\] //' | sort | uniq -c | sort -rn | head -12 | sed 's/^/  CHECK  /'
else
	ok 'no failure signature in dmesg'
fi
# Splats every X710 boot prints and that predate this experiment; more of them
# than in the control (e.g. another RCG that cannot switch) is a finding.
nk=$(grep -c -E "rcg didn.t update its configuration|refcount_t: (addition on 0|underflow)" "$out/dmesg.txt" || true)
echo "$nk" > "$out/known-splats.txt"
if [ -n "$ctl" ] && [ -f "$ctl/known-splats.txt" ] && [ "$nk" -gt "$(cat "$ctl/known-splats.txt")" ]; then
	ck "more known-pattern splats (dispcc rcg, hci_uart refcount) than the control: $nk vs $(cat "$ctl/known-splats.txt")"
	grep -E "rcg didn.t update its configuration|refcount_t: " "$out/dmesg.txt" | head -6 | sed 's/^/  info   /'
else
	[ "$nk" = 0 ] || inf "known splats outside this experiment (dispcc rcg, hci_uart refcount): $nk"
fi

chgrp -R adm "$out"
find "$out" -type f -exec chmod 0640 {} +
echo "saved $(ls "$out" | wc -l) files in $out"
