#!/bin/bash
# Enabled by default after kernel #11 hardware validation, for every SM8550
# PHY (both tablets) since the SM-X710's PCS_READY timeout of 2026-11-21.
# Stage only; never changes running devices or sleep policy.
set -euo pipefail
repo=$(cd "$(dirname "$0")/.." && pwd)
tree=${1:?usage: stage-ufs-resume-experiment.sh KERNEL_TREE}
enabled=${UFS_PCS_RESET_EXPERIMENTAL:-1}
source_file=$tree/drivers/phy/qualcomm/phy-qcom-qmp-ufs.c
patch_file=$repo/kernel/patches/qmp-ufs-assert-pcs-reset-before-calibration-gts9u.patch
marker='SM8550: assert the PCS reset before the calibration tables'
# What a tree prepared before 2026-11-22 carries: the same write, SM-X910 only.
legacy_marker='SM-X910 experiment: assert PCS reset before calibration tables'
case "$enabled" in
    0|1) ;;
    *) echo 'UFS_PCS_RESET_EXPERIMENTAL must be 0 or 1' >&2; exit 1 ;;
esac
test -f "$source_file"
if [ "$enabled" = 0 ] &&
   grep -q -e "$marker" -e "$legacy_marker" "$source_file"; then
    echo 'Experimental UFS reset remains in this tree; use a fresh tree for a default build.' >&2
    exit 1
fi
# Every check that a patch is in the tree, and the removal of the old form,
# reverse it with --force, not --batch: given a patch that is not in the tree
# but would go in, --batch says "Unreversed patch detected!  Ignoring -R." and
# applies it forward (a dry run then passes).
if grep -q "$legacy_marker" "$source_file"; then
    # Take the board-gated form out, exactly, so the current one goes in below.
    legacy=$(cat <<'LEGACY_PCS_RESET'
--- a/drivers/phy/qualcomm/phy-qcom-qmp-ufs.c
+++ b/drivers/phy/qualcomm/phy-qcom-qmp-ufs.c
@@ -1964,6 +1964,12 @@ static int qmp_ufs_phy_calibrate(struct phy *phy)
 	if (ret)
 		return ret;
 
+	/* SM-X910 experiment: assert PCS reset before calibration tables. */
+	if (cfg == &sm8550_ufsphy_cfg &&
+	    of_machine_is_compatible("samsung,gts9uwifi") &&
+	    !cfg->no_pcs_sw_reset)
+		qphy_setbits(pcs, cfg->regs[QPHY_SW_RESET], SW_RESET);
+
 	qmp_ufs_init_registers(qmp, cfg);
 
 	ret = reset_control_deassert(qmp->ufs_reset);
LEGACY_PCS_RESET
)
    printf '%s\n' "$legacy" |
        patch --dry-run --reverse --force --fuzz=0 -d "$tree" -p1
    printf '%s\n' "$legacy" |
        patch --reverse --force --fuzz=0 --no-backup-if-mismatch -d "$tree" -p1
    echo 'stage-ufs-resume-experiment: replaced the SM-X910-only PCS reset with the SM8550 one'
fi
if grep -q "$marker" "$source_file"; then
    # Reject a marker-only or partially edited candidate.
    patch --dry-run --reverse --force --fuzz=0 -d "$tree" -p1 < "$patch_file"
elif [ "$enabled" = 1 ]; then
    patch --dry-run --forward --batch --fuzz=0 -d "$tree" -p1 < "$patch_file"
    patch --forward --batch --fuzz=0 --no-backup-if-mismatch -d "$tree" -p1 < "$patch_file"
fi
