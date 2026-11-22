#!/bin/sh
# On the tablet, as root: the Chromium launcher watcher survives a burst of
# launcher changes like an apt transaction's (device 2.68 / 2.69 and later).
#
#   sudo sh scripts/test-chromium-desktops-burst.sh
#
# Writes and removes one throw-away launcher in /usr/share/applications 30
# times in about 8 s.  Its Exec is `true`, so the watcher's helper ignores it;
# nothing else is changed.  Before StartLimitIntervalSec=0 the sixth start
# within 10 s failed the service and its .path unit until the next boot.
set -u
[ "$(id -u)" = 0 ] || { echo 'run with sudo' >&2; exit 1; }
f=/usr/share/applications/gts9u-burst-test.desktop
[ ! -e "$f" ] || { echo "$f exists already" >&2; exit 1; }
trap 'rm -f "$f"' EXIT
since=$(date '+%F %T')

i=1
while [ "$i" -le 30 ]; do
	printf '[Desktop Entry]\nType=Application\nName=Burst test\nExec=true\nNoDisplay=true\n' > "$f"
	sleep 0.13
	rm -f "$f"
	sleep 0.13
	i=$((i + 1))
done
sleep 5

starts=$(journalctl -u ubuntu-gts9u-chromium-desktops.service --since "$since" \
	--no-pager -o cat | grep -c 'Starting ')
limits=$(journalctl --since "$since" --no-pager -o cat | grep -c 'start-limit')
path=$(systemctl is-failed ubuntu-gts9u-chromium-desktops.path)
service=$(systemctl is-failed ubuntu-gts9u-chromium-desktops.service)
echo "service starts during the burst: $starts"
echo "start-limit messages: $limits"
echo "ubuntu-gts9u-chromium-desktops.path: $path"
echo "ubuntu-gts9u-chromium-desktops.service: $service"
if [ "$limits" != 0 ] || [ "$path" = failed ] || [ "$service" = failed ]; then
	echo FAIL
	exit 1
elif [ "$starts" -lt 6 ]; then
	echo "INCONCLUSIVE: only $starts starts, not enough to reach the old limit"
	exit 2
fi
echo PASS
