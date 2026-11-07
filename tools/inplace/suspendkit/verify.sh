#!/bin/sh
# Non-interactive checks of the suspendkit, without root (the owner is in
# group adm, so the journal and /var/log/gts9u-suspendkit are readable).
# Each line is "ok", "CHECK" (look at it) or "info".
#
#   sh ~/suspendkit/verify.sh
set -u

kit=$(cd "$(dirname "$0")" && pwd)
unit=ubuntu-gts9u-adsp-start-inhibit.service
helper=/usr/libexec/ubuntu-gts9u-adsp-start-inhibit
unitfile=/usr/lib/systemd/system/$unit
ok() { echo "  ok     $*"; }
ck() { echo "  CHECK  $*"; }
inf() { echo "  info   $*"; }
hdr() { echo "== $*"; }

hdr kit
inf "revision $(cat "$kit/REVISION" 2>/dev/null || echo '?'), boot $(cat /proc/sys/kernel/random/boot_id), up $(cut -d. -f1 /proc/uptime) s"
if (cd "$kit" && sha256sum -c --quiet SHA256SUMS 2>/dev/null); then ok 'kit checksums'; else ck 'kit checksum mismatch'; fi

hdr 'ADSP-start sleep lock'
for f in "$helper" "$unitfile"; do
	if [ ! -e "$f" ]; then
		ck "$f missing (sudo sh ~/suspendkit/install.sh)"
	elif cmp -s "$f" "$kit/files/${f##*/}"; then
		ok "$f matches the kit"
	else
		inf "$f differs from the kit (a newer device package?)"
	fi
done
en=$(systemctl is-enabled "$unit" 2>/dev/null)
[ "$en" = enabled ] && ok "$unit enabled" || ck "$unit is '${en:-unknown}'"
installed=$(stat -c %Y "$unitfile" 2>/dev/null || echo 0)
booted=$(( $(date +%s) - $(cut -d. -f1 /proc/uptime) ))
j=$(journalctl -b -u "$unit" -o cat --no-pager 2>/dev/null)
if [ "$installed" -gt "$booted" ]; then
	inf 'installed during this boot: the hold at boot shows from the next boot on'
	printf '%s\n' "$j" | grep -E 'nothing to hold|lock' | tail -n 2 | sed 's/^/  info   /'
elif printf '%s\n' "$j" | grep -q 'audio DSP up after'; then
	ok "this boot: $(printf '%s\n' "$j" | grep -E 'held off until' | tail -n 1)"
	ok "this boot: $(printf '%s\n' "$j" | grep -E 'audio DSP up after' | tail -n 1)"
elif printf '%s\n' "$j" | grep -q 'releasing'; then
	ck "this boot: $(printf '%s\n' "$j" | grep -E 'releasing' | tail -n 1)"
else
	ck "no hold in this boot's journal: $(systemctl show -p ActiveState,Result,ConditionResult --value "$unit" 2>/dev/null | tr '\n' ' ')"
fi
# Ordering: the lock must start before the DSP start, after the boot cycle.
t_lock=$(systemctl show -p ActiveEnterTimestampMonotonic --value "$unit" 2>/dev/null)
t_adsp=$(systemctl show -p ExecMainStartTimestampMonotonic --value ubuntu-gts9u-adsp-boot.service 2>/dev/null)
if [ "${t_lock:-0}" -gt 0 ] && [ "${t_adsp:-0}" -gt 0 ] && [ "$installed" -le "$booted" ]; then
	if [ "$t_lock" -le "$t_adsp" ]; then
		ok "lock ready at $((t_lock / 1000)) ms, DSP start at $((t_adsp / 1000)) ms (boot clock)"
	else
		ck "lock ready at $((t_lock / 1000)) ms, AFTER the DSP start at $((t_adsp / 1000)) ms"
	fi
fi
if systemd-inhibit --list --no-pager --no-legend 2>/dev/null | grep -q 'ubuntu-gts9u-adsp-start'; then
	inf 'the lock is held right now (the DSP is still starting)'
else
	ok 'no lock held now'
fi
inf "audio DSP: $(for d in /sys/class/remoteproc/remoteproc*; do [ "$(cat "$d/name" 2>/dev/null)" = adsp ] && cat "$d/state"; done), fastrpc-adsp $([ -e /dev/fastrpc-adsp ] && echo present || echo missing), $(grep -c '^ *[0-9]' /proc/asound/cards 2>/dev/null) sound card(s)"

hdr 'reliability runner'
sh "$kit/gts9u-suspend-cycles.sh" status 6 2>&1 | sed 's/^/  info   /'
