#!/bin/sh
# Checks of the sensors hook kit, without root (the owner is in group adm, so
# the journal and the kit's log are readable).  Each line is "ok", "CHECK"
# (look at it) or "info".
#   sh ~/sensors-hook/verify.sh
# The three files (this kit's revision, the device package's own, or neither),
# the resume unit as systemd has it loaded, the sensors now (the two clients
# and what iio-sensor-proxy offers on D-Bus), and every system sleep in this
# boot's journal: how long it slept, what the hook said, and how long after
# the return the sensors were ready again.
set -u
export LC_ALL=C

kit=$(cd "$(dirname "$0")" && pwd)
# The system's root: empty on a tablet (the tests point it at a scratch tree).
root=
pkg=ubuntu-gts9u-device
unit=ubuntu-gts9u-sensors-resume.service
hook=$root/usr/lib/systemd/system-sleep/ubuntu-gts9u-sensors
md5sums=$root/var/lib/dpkg/info/$pkg.md5sums
files='usr/lib/systemd/system-sleep/ubuntu-gts9u-sensors 755
usr/libexec/ubuntu-gts9u-sensors-resume 755
usr/lib/systemd/system/ubuntu-gts9u-sensors-resume.service 644'
ok() { echo "  ok     $*"; }
ck() { echo "  CHECK  $*"; }
inf() { echo "  info   $*"; }
hdr() { echo "== $*"; }
revision=$(cat "$kit/REVISION" 2>/dev/null || echo '?')

hdr kit
inf "revision $revision from $(cat "$kit/SOURCE" 2>/dev/null || echo '?'), boot $(cat "$root/proc/sys/kernel/random/boot_id" 2>/dev/null), up $(cut -d. -f1 "$root/proc/uptime" 2>/dev/null) s"
if (cd "$kit" && sha256sum -c --quiet SHA256SUMS >/dev/null 2>&1); then ok 'kit checksums'; else ck 'the kit does not match its checksums'; fi

hdr files
version=$(dpkg-query -W -f '${Version}' "$pkg" 2>/dev/null) || version='?'
inf "$pkg $version"
kit_files=0
while read -r rel mode; do
	target=$root/$rel
	if [ ! -f "$target" ]; then
		ck "/$rel is missing"
		continue
	fi
	cur=$(sha256sum < "$target" | cut -d' ' -f1)
	want=$(awk -v f="files/$rel" '$2 == f {print $1}' "$kit/SHA256SUMS" 2>/dev/null)
	theirs=$(awk -v f="$rel" '$2 == f {print $1}' "$md5sums" 2>/dev/null)
	perm=$(stat -c '%a %U:%G' "$target" 2>/dev/null)
	if [ "$cur" = "$want" ]; then
		kit_files=$((kit_files + 1))
		if [ "$perm" = "$mode root:root" ]; then
			ok "/$rel: this kit's revision $revision ($perm)"
		else
			ck "/$rel: this kit's revision $revision, but $perm (expected $mode root:root)"
		fi
	elif [ -n "$theirs" ] && [ "$(md5sum < "$target" | cut -d' ' -f1)" = "$theirs" ]; then
		inf "/$rel: $pkg $version's own, not this kit's"
	else
		ck "/$rel: neither this kit's revision $revision nor $pkg $version's own ($perm)"
	fi
done <<FILES
$files
FILES
case $kit_files in
3) ok 'all three files are this revision' ;;
0) inf 'the kit is not in place (sudo sh ~/sensors-hook/install.sh)' ;;
*) ck "only $kit_files of the three files are this revision: run install.sh again" ;;
esac
if sh -n "$hook" 2>/dev/null; then ok 'the hook parses'; else ck 'the hook does not parse'; fi

hdr 'the resume unit as systemd has it'
props=$(systemctl show -p Restart -p RestartUSec -p StartLimitBurst -p StartLimitIntervalUSec \
	-p TimeoutStopUSec -p NeedDaemonReload -p ActiveState -p SubState -p Result -p NRestarts "$unit" 2>/dev/null |
	tr '\n' ' ')
inf "$props"
case " $props" in *' NeedDaemonReload=yes '*) ck 'systemd has an older copy of the unit loaded (sudo systemctl daemon-reload)' ;; esac
if [ "$kit_files" = 3 ]; then
	# systemctl show prints the properties in its own order.
	case " $props" in
	*' Restart=on-failure '*) case " $props" in
		*' StartLimitBurst=3 '*) ok 'retries: on failure, three starts at most' ;;
		*) ck 'the unit loaded is not the kit'"'"'s (StartLimitBurst=3)' ;;
		esac ;;
	*) ck 'the unit loaded is not the kit'"'"'s (Restart=on-failure)' ;;
	esac
fi
case " $props" in *' Result=start-limit-hit '*) ck "$unit used its three starts and stays failed until the next resume or boot" ;; esac

hdr 'sensors now'
for u in iio-sensor-proxy.service hexagonrpcd-adsp-sensorspd.service "$unit"; do
	inf "$u: $(systemctl is-active "$u" 2>/dev/null)"
done
props=$(timeout 10 gdbus introspect --system --dest net.hadess.SensorProxy --object-path /net/hadess/SensorProxy \
	--only-properties 2>/dev/null | grep -E 'HasAccelerometer|HasAmbientLight|AccelerometerOrientation|LightLevel ' |
	sed 's/^ *readonly [a-z]* //; s/;$//' | tr '\n' ' ')
case $props in
*'HasAccelerometer = true'*) ok "iio-sensor-proxy: $props" ;;
'') ck 'iio-sensor-proxy does not answer on D-Bus' ;;
*) ck "iio-sensor-proxy: $props" ;;
esac

hdr "this boot's system sleeps (journal)"
journalctl -b -o short-monotonic --no-pager -t systemd-sleep -t ubuntu-gts9u-sensors-resume 2>/dev/null | awk '
	function at(s) { sub(/^\[ */, "", s); sub(/\].*$/, "", s); return s + 0 }
	function text(s) { sub(/^\[[^]]*\] [^ ]+ [^:]+: /, "", s); return s }
	/^\[/ { t = at($0); m = text($0) }
	/Performing sleep operation/ { n++; start[n] = t; next }
	/System returned from sleep operation|Failed to put system to sleep/ {
		if (n && back[n] == "") { back[n] = t; refused[n] = ($0 ~ /Failed to put/) }
		next
	}
	/ubuntu-gts9u-sensors: / { sub(/^ubuntu-gts9u-sensors: /, "", m); said[++s] = sprintf("hook at %.1f s: %s", t, m); next }
	/SSC ready after attempt/ {
		if (n && ready[n] == "") { ready[n] = t; a = m; sub(/.*attempt /, "", a); sub(/[^0-9].*/, "", a); attempt[n] = a }
		next
	}
	/SSC did not become ready|handover storm persisted/ { if (n) failed[n]++; next }
	END {
		if (!n) print "  info   no system sleep in this boot'\''s journal"
		for (i = 1; i <= n; i++) {
			line = sprintf("sleep at %.1f s", start[i])
			if (back[i] == "") {
				print "  CHECK  " line ": no return in the journal"
				continue
			}
			line = line sprintf(refused[i] ? ": refused by the kernel (%.1f s)" : ": back after %.1f s", back[i] - start[i])
			tries = failed[i] ? sprintf(", after %d failed recover%s", failed[i], failed[i] > 1 ? "ies" : "y") : ""
			if (ready[i] != "")
				print "  " (failed[i] ? "CHECK  " : "ok     ") line sprintf("; sensors ready %.1f s after it (attempt %s%s)", ready[i] - back[i], attempt[i], tries)
			else if (i < n)
				print "  info   " line "; the next sleep came before the sensors were ready" tries
			else
				print "  " (failed[i] ? "CHECK  " : "info   ") line "; sensors not ready yet" tries " (the resume unit above)"
		}
		# The hook says something only when a step did not work.
		for (j = 1; j <= s; j++) print "  CHECK  " said[j]
	}'
