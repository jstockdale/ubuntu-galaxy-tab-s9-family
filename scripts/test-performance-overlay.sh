#!/bin/bash
# Check that the early overlays add only what they are meant to: the 3.36 GHz
# prime-CPU OPP on every family board; on the Ultra also the 719 MHz GPU OPP,
# the UFS inline crypto engine clock, and the SM-X710's sync_state hand-off
# (gcc under CX, the GMU's post-init providers, the crypto engine disabled)
# and ramoops region, compared with the SM-X710 DTB's values when one is
# given. The board split mirrors gts9u-performance.c.
set -euo pipefail
repo=$(cd "$(dirname "$0")/.." && pwd)
baseline=${1:?usage: test-performance-overlay.sh VALIDATED_BASE_DTB [SM-X710_DTB]}
stage=$(mktemp -d -t gts9u-perf-test.XXXXXXXX)
trap 'rm -rf -- "$stage"' EXIT
compatible=$(fdtget -t s "$baseline" / compatible)
if grep -qw samsung,gts9uwifi <<<"$compatible"; then
	gpu_opp=1
	overlays=(gts9-performance gts9u-performance gts9u-sync-state gts9u-ramoops)
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
ufs=/soc@0/ufshc@1d84000
# UFS OPP: the ICE core clock rate it adds (gcc-kalama.c's corners).
ufs_opps=(75000000:100000000 150000000:201500000 300000000:403000000)
test "$(fdtget -t u "$stage/after.dtb" "$cpu" opp-hz)" = '0 3360000000'
test "$(fdtget -t u "$stage/after.dtb" "$cpu" opp-peak-kBps)" = '14928000 14744000 54067200'
# props NODE: its property names, sorted; added nodes must have exactly these
# and no children, since the comparison below deletes them whole.
props() { fdtget -p "$stage/after.dtb" "$1" | sort | tr '\n' ' '; }
nstrings() { fdtget -t bx "$1" "$ufs" clock-names | tr ' ' '\n' | grep -cx 0; }
test "$(props "$cpu")" = 'opp-hz opp-peak-kBps '
test -z "$(fdtget -l "$stage/after.dtb" "$cpu")"
added=("$cpu")
if [ "$gpu_opp" = 1 ]; then
	test "$(fdtget -t u "$stage/after.dtb" "$gpu" opp-hz)" = '0 719000000'
	test "$(fdtget -t u "$stage/after.dtb" "$gpu" opp-level)" = 224
	test "$(fdtget -t u "$stage/after.dtb" "$gpu" opp-peak-kBps)" = 16500000
	test "$(fdtget -t x "$stage/after.dtb" "$gpu" qcom,opp-acd-level)" = 882e5ffd
	test "$(props "$gpu")" = 'opp-hz opp-level opp-peak-kBps qcom,opp-acd-level '
	test -z "$(fdtget -l "$stage/after.dtb" "$gpu")"
	added+=("$gpu")
	# GCC_UFS_PHY_ICE_CORE_CLK (141) joins the UFS clocks, after the others.
	gcc=$(fdtget -t u "$baseline" /soc@0/clock-controller@100000 phandle)
	test "$(fdtget -t u "$stage/after.dtb" "$ufs" clocks)" = \
		"$(fdtget -t u "$baseline" "$ufs" clocks) $gcc 141"
	test "$(fdtget -t s "$stage/after.dtb" "$ufs" clock-names)" = \
		"$(fdtget -t s "$baseline" "$ufs" clock-names) ice_core_clk"
	# fdtget -t s joins with spaces; count the NUL-terminated strings too.
	test "$(nstrings "$stage/after.dtb")" = "$(( $(nstrings "$baseline") + 1 ))"
	for opp in "${ufs_opps[@]}"; do
		# opp-hz is 64-bit: fdtget prints each rate as two cells, high first.
		test "$(fdtget -t u "$stage/after.dtb" "$ufs/opp-table/opp-${opp%:*}" opp-hz)" = \
			"$(fdtget -t u "$baseline" "$ufs/opp-table/opp-${opp%:*}" opp-hz) 0 ${opp#*:}"
	done
	# The SoC hand-off: the SM-X710's three board DTS changes.  The values
	# are phandles of the base tree (fixups resolved against /__symbols__).
	gcc_node=/soc@0/clock-controller@100000
	gmu_node=/soc@0/gmu@3d6a000
	crypto_node=/soc@0/crypto@1dfa000
	pmsg_node=/reserved-memory/sec-pmsg@880900000
	rpmhpd=$(fdtget -t u "$baseline" /soc@0/rsc@17a00000/power-controller phandle)
	gpucc=$(fdtget -t u "$baseline" /soc@0/clock-controller@3d90000 phandle)
	! fdtget -p "$baseline" "$gcc_node" | grep -qx power-domains
	! fdtget -p "$baseline" "$gmu_node" | grep -qx post-init-providers
	! fdtget -p "$baseline" "$crypto_node" | grep -qx status
	test "$(fdtget -t u "$stage/after.dtb" "$gcc_node" power-domains)" = "$rpmhpd 0"
	test "$(fdtget -t u "$stage/after.dtb" "$gmu_node" post-init-providers)" = "$gcc $gpucc"
	test "$(fdtget -t s "$stage/after.dtb" "$crypto_node" status)" = disabled
	# ramoops: the SM-X710's layout on the carve-out the pinned DTB already
	# reserves (reg and no-map unchanged; the comparison below checks).
	! fdtget -p "$baseline" "$pmsg_node" | grep -qx compatible
	test "$(fdtget -t s "$stage/after.dtb" "$pmsg_node" compatible)" = ramoops
	for prop in record-size:262144 console-size:1048576 pmsg-size:131072 ftrace-size:131072 ecc-size:96; do
		test "$(fdtget -t u "$stage/after.dtb" "$pmsg_node" "${prop%:*}")" = "${prop#*:}"
	done
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
# The UFS properties the Ultra overlay rewrites go back to the baseline's.
if [ "$gpu_opp" = 1 ]; then
	# shellcheck disable=SC2046 # one fdtput argument per cell or string
	fdtput -t u "$stage/after.dtb" "$ufs" clocks $(fdtget -t u "$baseline" "$ufs" clocks)
	# shellcheck disable=SC2046
	fdtput -t s "$stage/after.dtb" "$ufs" clock-names $(fdtget -t s "$baseline" "$ufs" clock-names)
	for opp in "${ufs_opps[@]}"; do
		node=$ufs/opp-table/opp-${opp%:*}
		# shellcheck disable=SC2046
		fdtput -t u "$stage/after.dtb" "$node" opp-hz $(fdtget -t u "$baseline" "$node" opp-hz)
	done
	# The properties the hand-off and ramoops overlays add come out again.
	fdtput -d "$stage/after.dtb" "$gcc_node" power-domains
	fdtput -d "$stage/after.dtb" "$gmu_node" post-init-providers
	fdtput -d "$stage/after.dtb" "$crypto_node" status
	fdtput -d "$stage/after.dtb" "$pmsg_node" compatible record-size console-size pmsg-size ftrace-size ecc-size
fi
dtc -q -I dtb -O dts -s "$stage/before.dtb" > "$stage/before.dts"
dtc -q -I dtb -O dts -s "$stage/after.dtb" > "$stage/reverted.dts"
cmp "$stage/before.dts" "$stage/reverted.dts"
mkdir -p "$stage/tree/drivers/soc/qcom"
touch "$stage/tree/drivers/soc/qcom/Makefile"
bash "$repo/scripts/stage-performance-overlay.sh" "$stage/tree"
header=$stage/tree/drivers/soc/qcom/gts9u-performance-overlay.h
for symbol in gts9u_performance_overlay gts9u_sync_state_overlay gts9u_ramoops_overlay \
	gts9_performance_overlay gts9_family_overlay; do
	grep -qx "static const unsigned char $symbol\[\] __initconst __aligned(8) = {" "$header"
done
cp "$header" "$stage/header-first"
bash "$repo/scripts/stage-performance-overlay.sh" "$stage/tree"
cmp "$stage/header-first" "$header"
test "$(grep -c 'gts9u-performance.o' "$stage/tree/drivers/soc/qcom/Makefile")" = 1
# With the SM-X710's DTB as a second argument (an Ultra run only): the
# properties the hand-off and ramoops overlays give the Ultra are that DTB's,
# phandles compared by the paths they point to.
if [ "$gpu_opp" = 1 ] && [ -n "${2:-}" ]; then
	x710=$2
	grep -qw samsung,gts9wifi <<<"$(fdtget -t s "$x710" / compatible)"
	path_of() { # DTB PHANDLE: the path of the node with that phandle
		local dtb=$1 want=$2 node
		for node in /soc@0/clock-controller@100000 /soc@0/clock-controller@3d90000 \
			/soc@0/rsc@17a00000/power-controller; do
			[ "$(fdtget -t u "$dtb" "$node" phandle)" = "$want" ] && { echo "$node"; return; }
		done
		echo "phandle $want"
	}
	resolve() { # DTB NODE PROP CELLS-PER-ENTRY: the property, its phandles as paths
		local dtb=$1 cells=$4 i=0 v out=
		for v in $(fdtget -t u "$dtb" "$2" "$3"); do
			if [ $((i % cells)) = 0 ]; then out="$out $(path_of "$dtb" "$v")"; else out="$out $v"; fi
			i=$((i + 1))
		done
		echo "${out# }"
	}
	apply "$baseline" "$stage/compare.dtb"
	test "$(resolve "$stage/compare.dtb" "$gcc_node" power-domains 2)" = \
		"$(resolve "$x710" "$gcc_node" power-domains 2)"
	test "$(resolve "$stage/compare.dtb" "$gmu_node" post-init-providers 1)" = \
		"$(resolve "$x710" "$gmu_node" post-init-providers 1)"
	test "$(fdtget -t s "$x710" "$crypto_node" status)" = disabled
	for prop in compatible:s reg:x record-size:u console-size:u pmsg-size:u ftrace-size:u ecc-size:u; do
		test "$(fdtget -t "${prop#*:}" "$stage/compare.dtb" "$pmsg_node" "${prop%:*}")" = \
			"$(fdtget -t "${prop#*:}" "$x710" "$pmsg_node" "${prop%:*}")"
	done
	fdtget -p "$x710" "$pmsg_node" | grep -qx no-map
	fdtget -p "$stage/compare.dtb" "$pmsg_node" | grep -qx no-map
	echo "PASS: the hand-off and ramoops properties are the SM-X710 DTB's ($x710)"
fi
echo "PASS: ${overlays[*]}: OPP, UFS ICE, sync_state and ramoops values, idempotent application/staging, all other DT content preserved"
