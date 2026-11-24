#!/bin/sh
# ufs-phy-power-check.sh: read-only, no root.  Tells whether the SM8550 UFS
# PHY holds its power while the UFS host is runtime-suspended, which it must
# not: ufshcd_suspend() runs ufs_qcom_setup_clocks(off), whose phy_power_off()
# powers the PHY down unless an extra phy_power_on() reference is stuck on it
# (the leak fixed by ufs-qcom-track-phy-power-state-gts9u.patch).  A kernel
# with that leak loses the UFS at the next resume from a sleep that powers
# ufs_mem_phy_gdsc off (every s2idle or deep sleep on SM8550).
#
# How it sees it without root: the regulator core's sysfs requested_microamps
# is the sum of the load requests of the consumers that have the regulator
# enabled (drivers/regulator/core.c requested_microamps_show()).  The UFS PHY
# requests 188000 uA on its vdda-phy supply while it is powered
# (sm8550_ufsphy_vreg_l in phy-qcom-qmp-ufs.c); the other consumers of that
# rail on the SM-X710 and SM-X910 request less (PCIe and eUSB2 PHYs 0, the
# DSI PHY at most 98000, each camera PHY 32200 or 37900 while the camera
# streams): run it with the camera off.
#
# usage: sh ufs-phy-power-check.sh [samples] [interval_seconds]
# Reads sysfs only; writes nothing, resumes nothing.
set -u

samples=${1:-150}
interval=${2:-0.2}
host=/sys/devices/platform/soc@0/1d84000.ufshc
phy=1d80000.phy
load=188000

status_file=$host/power/runtime_status
if [ ! -r "$status_file" ]; then
	echo "no UFS host at $host" >&2
	exit 2
fi
rail=
for r in /sys/class/regulator/regulator.*; do
	if [ -e "$r/$phy-vdda-phy" ]; then
		rail=$r
		break
	fi
done
if [ -z "$rail" ]; then
	echo "no regulator with the consumer $phy-vdda-phy" >&2
	exit 2
fi

echo "UFS PHY vdda-phy: $(cat "$rail/name") ($rail), consumers:" \
	$(ls "$rail" | grep -- '-vdd' | tr '\n' ' ')

susp=0 susp_on=0 act=0 act_on=0 other=0 i=0
while [ "$i" -lt "$samples" ]; do
	s=$(cat "$status_file")
	a=$(cat "$rail/requested_microamps")
	case "$s" in
	suspended)
		susp=$((susp + 1))
		[ "$a" -ge "$load" ] && susp_on=$((susp_on + 1))
		;;
	active)
		act=$((act + 1))
		[ "$a" -ge "$load" ] && act_on=$((act_on + 1))
		;;
	*)
		other=$((other + 1))
		;;
	esac
	i=$((i + 1))
	sleep "$interval"
done

echo "samples $samples: host suspended $susp (PHY powered in $susp_on)," \
	"active $act (PHY powered in $act_on), in transition $other"
if [ "$susp" -eq 0 ]; then
	echo "INCONCLUSIVE: the UFS host was never runtime-suspended in the window; sample longer with the tablet idle"
	exit 3
fi
# A sample reads the status before the rail, so a resume can fall between
# the two reads: one stray sample is tolerated, a stuck reference shows in
# (nearly) every suspended sample.
if [ "$susp_on" -gt 1 ] && [ $((susp_on * 2)) -gt "$susp" ]; then
	echo "IMBALANCE: the UFS PHY stays powered while its host is runtime-suspended; the next sleep's resume will time out every UFS PHY calibration (do not run a sleep test in this boot)"
	exit 1
fi
echo "OK: the UFS PHY powers down with its host"
exit 0
