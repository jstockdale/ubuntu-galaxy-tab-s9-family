#!/bin/bash
# Check that the early overlays add only the intended operating points: the
# 3.36 GHz prime-CPU OPP on every family board, the 719 MHz GPU OPP on the
# Ultra only.  The board split mirrors gts9u-performance.c.
set -euo pipefail
repo=$(cd "$(dirname "$0")/.." && pwd)
baseline=${1:?usage: test-performance-overlay.sh VALIDATED_BASE_DTB}
stage=$(mktemp -d -t gts9u-perf-test.XXXXXXXX)
trap 'rm -rf -- "$stage"' EXIT
compatible=$(fdtget -t s "$baseline" / compatible)
if grep -qw samsung,gts9uwifi <<<"$compatible"; then
	gpu_opp=1
	overlays=(gts9-performance gts9u-performance)
elif grep -qw samsung,gts9wifi <<<"$compatible"; then
	gpu_opp=0
	overlays=(gts9-performance)
else
	echo "not a Galaxy Tab S9 family DTB: $compatible" >&2
	exit 1
fi
apply() {
	# apply SOURCE.dtb RESULT.dtb: every overlay this board gets, in order
	local src=$1 dst=$2 overlay
	cp "$src" "$stage/apply.dtb"
	for overlay in "${overlays[@]}"; do
		fdtoverlay -i "$stage/apply.dtb" -o "$stage/apply.next.dtb" "$stage/$overlay.dtbo"
		mv "$stage/apply.next.dtb" "$stage/apply.dtb"
	done
	mv "$stage/apply.dtb" "$dst"
}
for overlay in "${overlays[@]}"; do
	dtc -I dts -O dtb -o "$stage/$overlay.dtbo" "$repo/kernel/dts/$overlay.dtso"
done
apply "$baseline" "$stage/after.dtb"
apply "$stage/after.dtb" "$stage/twice.dtb"
dtc -q -I dtb -O dts -s "$stage/after.dtb" > "$stage/after.dts"
dtc -q -I dtb -O dts -s "$stage/twice.dtb" > "$stage/twice.dts"
cmp "$stage/after.dts" "$stage/twice.dts"
cpu=/opp-table-cpu7/opp-3360000000
gpu=/soc@0/gpu@3d00000/opp-table/opp-719000000
test "$(fdtget -t u "$stage/after.dtb" "$cpu" opp-hz)" = '0 3360000000'
test "$(fdtget -t u "$stage/after.dtb" "$cpu" opp-peak-kBps)" = '14928000 14744000 54067200'
added=("$cpu")
if [ "$gpu_opp" = 1 ]; then
	test "$(fdtget -t u "$stage/after.dtb" "$gpu" opp-hz)" = '0 719000000'
	test "$(fdtget -t u "$stage/after.dtb" "$gpu" opp-level)" = 224
	test "$(fdtget -t u "$stage/after.dtb" "$gpu" opp-peak-kBps)" = 16500000
	test "$(fdtget -t x "$stage/after.dtb" "$gpu" qcom,opp-acd-level)" = 882e5ffd
	added+=("$gpu")
elif ! fdtget -p "$baseline" "$gpu" >/dev/null 2>&1 &&
	fdtget -p "$stage/after.dtb" "$gpu" >/dev/null 2>&1; then
	# Other boards keep their own GPU table: errexit ignores a negated test.
	echo "the GPU OPP was added to a board other than the Ultra" >&2
	exit 1
fi
cp "$baseline" "$stage/before.dtb"
# If the source DT already includes the OPPs, compare after removing them from
# both sides. Otherwise deleting them from the baseline is unnecessary.
for node in "${added[@]}"; do
    fdtput -r "$stage/after.dtb" "$node"
    if fdtget -p "$stage/before.dtb" "$node" >/dev/null 2>&1; then
        fdtput -r "$stage/before.dtb" "$node"
    fi
done
dtc -q -I dtb -O dts -s "$stage/before.dtb" > "$stage/before.dts"
dtc -q -I dtb -O dts -s "$stage/after.dtb" > "$stage/reverted.dts"
cmp "$stage/before.dts" "$stage/reverted.dts"
mkdir -p "$stage/tree/drivers/soc/qcom"
touch "$stage/tree/drivers/soc/qcom/Makefile"
bash "$repo/scripts/stage-performance-overlay.sh" "$stage/tree"
header=$stage/tree/drivers/soc/qcom/gts9u-performance-overlay.h
for symbol in gts9u_performance_overlay gts9_performance_overlay gts9_family_overlay; do
	grep -qx "static const unsigned char $symbol\[\] __initconst = {" "$header"
done
cp "$header" "$stage/header-first"
bash "$repo/scripts/stage-performance-overlay.sh" "$stage/tree"
cmp "$stage/header-first" "$header"
test "$(grep -c 'gts9u-performance.o' "$stage/tree/drivers/soc/qcom/Makefile")" = 1
echo "PASS: ${overlays[*]}: OPP values, idempotent application/staging, all other DT content preserved"
