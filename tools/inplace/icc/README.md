# In-place kit: interconnect late release (SM-X710)

Puts `ubuntu-gts9u-icc-late-release` (helper and unit, exactly as the next
`ubuntu-gts9u-device` ships them) on the running tablet, so every boot
releases gem_noc and mc_virt from their INT_MAX boot floor once the panel
recovery's pm_test cycle has returned.  Background and the open question:
`docs/gts9wifi-interconnect.md`.

Nothing here touches boot, vendor_boot or the packages.  The release itself
cannot be undone on a running kernel; a reboot always brings the floor back.

## Contents

| file | what |
| --- | --- |
| `install.sh` | root: checks, backup, install, enable; `--now` also releases on this boot |
| `verify.sh` | no root: what this boot did, read back |
| `rollback.sh` | root: disable and remove (or mask, once the package ships it) |
| `files/` | the helper and unit, copied from `packaging/ubuntu-gts9u-device` |
| `weekend/icc-cycle-experiment.sh` | root: the weekend experiment (see the doc) |
| `SHA256SUMS` | checked by `install.sh` before anything is changed |

## Stage it (build host)

    tools/inplace/icc/make-kit.sh            # -> work/inplace-icc
    ssh jstockdale@10.42.0.2 'rm -rf ~/icc'  # only if an older kit is there
    scp -r work/inplace-icc jstockdale@10.42.0.2:icc

## Use it (tablet)

    sh ~/icc/verify.sh                        # before: floor held, no record
    sudo sh ~/icc/install.sh --now            # install, enable, release now
    sh ~/icc/verify.sh                        # after: released, INT_MAX 0
    sudo reboot                               # then verify.sh once more: the unit did it at boot

Without `--now` nothing changes until the next boot.  Every step is logged to
`/var/log/gts9u-icc-kit.log`; each boot's decision is in
`/var/log/ubuntu-gts9u-icc-release.log` and `/run/ubuntu-gts9u/icc-release`.

`/usr/libexec/ubuntu-gts9u-icc-late-release check` (no root) shows what the
unit would do right now; `status` lists every provider and its blockers.

## If a boot hangs after the release

The unit has a loop breaker: before it releases, it leaves
`/var/lib/ubuntu-gts9u/icc-release.pending` on disk, and its `ExecStop`
removes the marker again at every clean shutdown or reboot.  When a boot that
released the floor ends any other way (watchdog reset, forced restart, power
loss), the next boot finds the marker, keeps the floor for that one boot and
says why (`verify.sh`: "loop breaker held the floor on this boot").  The boot
after that releases again.  So a hang caused by the release costs one reset,
never a boot loop.  A forced restart for any other reason also costs one
held boot; `sudo systemctl restart ubuntu-gts9u-icc-late-release` releases
it on the spot.

## Turn it off

    sudo sh ~/icc/rollback.sh && sudo reboot

or, keeping the files: `sudo mkdir -p /etc/ubuntu-gts9u && sudo touch
/etc/ubuntu-gts9u/icc-hold` (the directory does not exist by default; the unit then
keeps the floor every boot until the file is removed).  From TWRP, if Linux
is not reachable:

    [ -d /data/home ] || mount -t ext4 /dev/block/by-name/userdata /data
    mkdir -p /data/etc/ubuntu-gts9u && touch /data/etc/ubuntu-gts9u/icc-hold

## When the device package catches up

The files go to the package's own paths with no package owning them, so the
next `ubuntu-gts9u-device` simply takes them over (dpkg replaces unowned
files).  `install.sh` refuses to overwrite a different packaged version;
`rollback.sh` masks a packaged unit instead of deleting package files.
