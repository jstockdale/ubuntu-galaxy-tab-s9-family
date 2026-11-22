#!/bin/sh
# UFS link stress at HS-G5, no root: rounds of mixed fio load and idle gaps.
#   setsid nohup sh ~/ufs-g5/stress.sh [ROUNDS] > /dev/null 2>&1 &
# Each round is 150 s of load, then 30 s idle, so the link also scales down,
# hibernates and comes back up to G5.  The load:
#   - 1 MB sequential reads at QD16 from a file written once (full speed);
#   - 4 KB random reads at QD32, two jobs, uncapped;
#   - 4 KB random writes at QD8, capped at 10 MB/s;
#   - 128 KB writes capped at 30 MB/s, read back and checked (crc32c).
# The writes come to about 40 MB/s, some 60 GB over ten rounds.  It stops at
# the first fio or verify error, a UFS, I/O or ext4 kernel error, a battery
# under 25 % or over 45 °C, or a thermal zone over 90 °C.
# Log: ~/usertests/ufs-g5-stress-<time>.log; the last line says PASS or STOP.
set -u
export LC_ALL=C
rounds=${1:-10}
dir=$HOME/usertests
host=/sys/devices/platform/soc@0/1d84000.ufshc
bat=$(ls -d /sys/class/power_supply/*battery* | head -1)
log=$dir/ufs-g5-stress-$(date +%Y%m%d-%H%M%S).log
since=$(date '+%Y-%m-%d %H:%M:%S')
mkdir -p "$dir"
for f in ufs-test.bin ufs-stress-read.bin; do
	[ -f "$dir/$f" ] ||
		dd if=/dev/urandom of="$dir/$f" bs=4M count=512 oflag=direct status=none
done

say() { echo "$(date +%T) $*" >> "$log"; }
stop() { say "STOP: $*"; exit 1; }
hottest() {
	for z in /sys/class/thermal/thermal_zone*; do
		echo "$(cat "$z/temp" 2>/dev/null || echo 0) $(cat "$z/type")"
	done | sort -n | tail -1
}
transitions() { awk '/Total transition/ {print $4}' "$host"/devfreq/*/trans_stat; }
kernel_errors() {
	journalctl -k --since "$since" --no-pager -q 2>/dev/null |
		grep -E 'ufshcd.*(rror|fail|abort|reset)|phy initialization timed-out|I/O error|EXT4-fs error|blk_update_request|Buffer I/O' |
		grep -v 'rcg didn'
}

say "start: $rounds rounds; kernel $(uname -v); $(cat /proc/sys/kernel/random/boot_id)"
i=0
while [ "$i" -lt "$rounds" ]; do
	i=$((i + 1))
	t0=$(transitions)
	fio --output-format=terse --terse-version=3 --directory="$dir" \
		--ioengine=libaio --direct=1 --time_based --runtime=150 --group_reporting=0 \
		--name=seqread --filename=ufs-stress-read.bin --size=2G --rw=read --bs=1M --iodepth=16 \
		--name=randread --filename=ufs-test.bin --size=2G --rw=randread --bs=4k \
			--iodepth=32 --numjobs=2 \
		--name=randwrite --filename=ufs-test.bin --size=2G --rw=randwrite --bs=4k \
			--iodepth=8 --rate=,10m \
		--name=verify --filename=ufs-stress-verify.bin --size=1G --rw=write --bs=128k \
			--iodepth=8 --rate=,30m --verify=crc32c --verify_backlog=256 --verify_fatal=1 \
		> "$log.round" 2>&1 &
	fio_pid=$!
	gears=
	while kill -0 "$fio_pid" 2>/dev/null; do
		sleep 15
		gears="$gears $(cat $host/power_info/gear | sed 's/HS_GEAR/G/')"
	done
	wait "$fio_pid"
	rc=$?
	# terse v3: 3 job name, 5 error, 7 read KB/s, 8 read IOPS, 48 write KB/s, 49 write IOPS
	summary=$(awk -F';' '/^3;/ { printf "%s err=%s r=%.0fMB/s,%d w=%.0fMB/s,%d; ", $3, $5, $7/1024, $8, $48/1024, $49 }' "$log.round")
	say "round $i: fio rc=$rc; $summary"
	say "round $i: gear samples:$gears; devfreq transitions $(( $(transitions) - t0 )); hottest $(hottest); battery $(cat "$bat/capacity")% $(cat "$bat/temp" 2>/dev/null)"
	[ "$rc" = 0 ] || { cat "$log.round" >> "$log"; stop "fio exited $rc in round $i"; }
	awk -F';' '/^3;/ { n++; if ($5 != 0) bad = 1 } END { exit bad || n < 5 }' "$log.round" ||
		{ cat "$log.round" >> "$log"; stop "fio reported an error in round $i"; }
	errs=$(kernel_errors)
	[ -z "$errs" ] || { echo "$errs" >> "$log"; stop "kernel errors in round $i"; }
	[ "$(cat "$bat/capacity")" -ge 25 ] || stop 'battery under 25 %'
	[ "$(cat "$bat/temp" 2>/dev/null || echo 0)" -le 450 ] || stop 'battery over 45 °C'
	[ "$(hottest | cut -d' ' -f1)" -le 90000 ] || stop "too hot: $(hottest)"
	[ "$i" -lt "$rounds" ] && sleep 30
done
rm -f "$log.round"
say "PASS: $rounds rounds, no fio, verify or kernel UFS/I/O errors"
