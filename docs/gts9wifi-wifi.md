# Galaxy Tab S9 (SM-X710, gts9wifi): Wi-Fi bring-up notes

The 11" Tab S9 carries the same WCN6855/QCA6490 combo chip as the Ultra's
sibling boards, on PCIe0, but the mainline bring-up needed four things the
Ultra port did not, all of which are now in the device tree, the firmware
manifest and the rootfs overlay.

## Power sequencing (device tree)

* `pwrseq-qcom-wcn` needs the 38.4 MHz XO clock request line (TLMM 204,
  `xo-clk-gpios`) and the AOP PDC initialisation table over QMP
  (`qcom,qmp = <&aoss_qmp>`, `qcom,wlan-pdc-init` with the eleven stock X710
  strings).  Without the PDC votes the PMU never completes power-up after a
  cold power-off: PCIe trains only after the bus scan and the Bluetooth ROM
  never answers.  The Fedora gts9wifi port documents the same finding.
* The rails follow stock X710: S2G 1.012 V (WLAN_AON), S4E 0.968 V
  (rfa0p95), S5G 0.968 V (pmu/pmumx/pmucx).
* TWRP and ABL leave WLAN_EN high, so the sequencer requests it low at probe
  (`cold_reset_wlan`) and holds XO_CLK while either Wi-Fi or Bluetooth is on
  (`kernel/patches/pwrseq-wcn6855-cold-reset-hold-xo-gts9wifi.patch`).
* PCIe0 is pinned to Gen2 like stock and the DesignWare link wait is four
  times longer on this board, so the endpoint enumerates at host init
  (~10.9 s, Gen2 x1).  `gts9u-pci-rescan.service` remains as a safety net
  (see "First probe and the L1 substates" below).

## Firmware (manifest `configs/firmware/x710-bl5-*.toml`)

The chip reports **hw2.1** (chip_id 0x2, subsystem 17cb:0108, board_id 0xff),
and ath11k has no fallback to the hw2.0 directory.

| file | source | note |
| --- | --- | --- |
| `hw2.1/amss.bin`, `hw2.1/m3.bin` | ath11k-firmware `WLAN.HSP.1.1-04866.5-QCAHSPSWPL_V1_V2_SILICONZ_IOE-1` | linux-firmware's LITE build boots and crashes into RDDM on this silicon |
| `hw2.1/board-2.bin` | **Samsung's `bdwlan.elf`**, wrapped by the manifest (`derived:board2`) under `bus=pci,vendor=17cb,device=1103,subsystem-vendor=17cb,subsystem-device=0108,qmi-chip-id=2,qmi-board-id=255` | see below |
| `hw2.1/board.bin` | the same ELF, API-1 fallback | |
| `hw2.1/regdb.bin` | linux-firmware hw2.0 regdb | works with the Samsung board data |

Samsung's own `regdb.bin` (stock `vendor/firmware/qca6490/regdb.bin`,
sha256 `af5640b3…`) is in the manifest too, as the
`GTS9WIFI_ATH11K_REGDB=samsung` alternative the overlay script requires.

All five hw2.1 files are installed twice from the same staged copies: on the
rootfs, and in the vendor_boot ramdisk fragment for the first probe (next
section).

### Why the vendor board data is mandatory

linux-firmware's board-2.bin has an exact entry for this PCI subsystem and
chip id, so ath11k picks it first.  With that generic Qualcomm reference
board file the radio comes up, regulatory reports US, `iw` shows every 5 GHz
channel enabled, and a scan still hears **only 2.4 GHz networks** (14 BSSIDs,
zero on 5 GHz).  With Samsung's board data the same scan found 14 networks
on 5 GHz; association on channel 48 at 80 MHz gives Wi-Fi 6 rates.  The
board file describes the RF front end, so the tablet's own file is not
optional.  `build-rootfs-overlay.sh` installs it by default and keeps the
generic set selectable (`GTS9WIFI_ATH11K_BOARD=linux-firmware`) for
comparisons.

## First probe and the L1 substates

ath11k is built into the gts9wifi kernel.  It probes the endpoint at ~11.7 s,
before `/init` runs (13.5 s) and mounts the root, so with the firmware only on
the rootfs every boot logged:

```
mhi mhi0: Direct firmware load for ath11k/WCN6855/hw2.1/amss.bin failed with error -2
ath11k_pci 0000:01:00.0: failed to power up mhi: -110
ath11k_pci 0000:01:00.0: probe with driver ath11k_pci failed with error -110
```

and `gts9u-pci-rescan` brought Wi-Fi up 10 s later by removing the endpoint
and the root port and rescanning the host bridge (udev initialised `wlp1s0`
at 22 s; the unit took 13.3 s).  That re-creation had a cost: pcie-qcom
enables every ASPM state, L1.1 and L1.2 included, only for the devices present
at host init (`qcom_pcie_host_post_init_2_7_0`), and a re-created device gets
the PCI core's DT default of L0s and L1 alone.  The X710 ran with
`l1_1_aspm=l1_2_aspm=l1_1_pcipm=l1_2_pcipm=0` although stock Kalama enables
the substates on this very port (`qcom,l1-2-th-scale`/`-value` in the stock
pcie0 node, CLKREQ# on GPIO 95 in both trees).

Two ways to have the firmware at the first probe were weighed:

* **The vendor_boot ramdisk (chosen).**  `build-rootfs-overlay.sh` puts the
  hw2.1 set into the initramfs overlay, which `build-android-v4-bundle.sh`
  packs as the vendor ramdisk fragment, next to the Bluetooth firmware that
  hci_qca already needs there.  The fragment lives in **vendor_boot**
  (96 MiB; stock X710 carries 13.4 MB of vendor ramdisk there), not in
  init_boot, so the 8 MiB init_boot budget is untouched.  It grows from
  0.15 MB to 2.9 MB of LZ4 (5.5 MB unpacked, freed at switch_root).  The
  firmware loader waits for the initramfs to be unpacked, so the 11.7 s probe
  finds it.  No kernel change, and an experiment is a vendor_boot repack.
* **ath11k as a module.**  Needs `CONFIG_ATH11K{,_PCI}=m`, a kernel rebuild,
  an in-tree `M=` module build that `build-mainline-kernel.sh` does not do,
  and a signed module set tied to boot.img under lockdown; Wi-Fi would then
  come up after the root mount (~17 s) instead of ~12 s.  It fixes the same
  two problems, at more cost.

After `switch_root` the rootfs copies serve every later load (resume
power-ups, firmware recovery); both copies come from the same staged files.
An amss/m3/board update therefore needs a vendor_boot rebuild as well as the
rootfs one.

`gts9u-pci-rescan.service` now ships in the device package (same unit name,
so it replaces the copy older overlays generated) and runs
`/usr/libexec/ubuntu-gts9u-pci-rescan`:

1. endpoint bound: nothing to do (the normal case now);
2. endpoint present without a driver (a failed probe): probe it again in place
   through `/sys/bus/pci/drivers_probe`, which keeps the device and its L1
   substates;
3. otherwise the old re-creation (root-port rescan, or remove and rescan the
   host bridge), after which it re-enables the L1 substates once the
   interface is up (ath11k keeps ASPM off while its firmware boots).

Re-enabling takes more than writing 1 to `link/l1_*_aspm`: those attributes
only lift a state out of the link's `aspm_disable` mask, and with the
`default` ASPM policy what is enabled stays capped by `link->aspm_default`,
which for a re-created device is the core's L0s+L1
(`aspm_attr_store_common()` -> `policy_to_aspm_state()`).  So when the
substates still read 0 after the writes, the helper sets
`/sys/module/pcie_aspm/parameters/policy` to `powersupersave` (every
supported state on every link; pcie0 is the only PCIe controller enabled on
either board, so this is the state host init gives a boot-time endpoint).  The
`aspm_disable` mask, and with it `off` below, is honoured under every policy,
and a policy other than `default` (someone's deliberate choice) is left alone.

`/etc/ubuntu-gts9u/pcie-l1ss` overrides step 3's choice: `auto` (default),
`on` (always enable), `off` (always disable, the escape hatch if a link
misbehaves in L1.2).  The outcome is left in `/run/ubuntu-gts9u/pci-rescan`
(`aspm_policy=` when the policy had to change).  `off` acts only when the
unit runs (after `basic.target`), so the substates are still on from host
init (~11 s) until then; to keep them off from the start, add
`pcie_aspm.policy=powersave` (L0s and L1 only) to the vendor_boot command
line (`GTS9U_EXTRA_CMDLINE`).

## Userspace

* `wireless-regdb` and `iw` are in the rootfs package list; without the
  regulatory database cfg80211 logs a load failure at every boot.
* **Address.**  The chip has no MAC in OTP: ath11k registers the interface
  with a Qualcomm default (`00:03:7f:12:xx:xx`) whose low bytes change on
  every boot, so the DHCP lease and SSH-by-address move.  Stock Android reads
  the address from EFS, `/efs/wifi/.mac.info` (17 bytes, upper case,
  colon-separated, no newline; `.mac.cob` holds the same value and
  `bluetooth/bt_addr` is the same address minus one).
  `ubuntu-gts9u-wlan-address.service` (device package, gated on
  `samsung,gts9wifi`) mounts EFS `ro,noload` on a mount point of its own,
  reads it, and
  * writes `/run/systemd/network/05-ubuntu-gts9u-wlan.link`
    (`Driver=ath11k_pci`, `Type=wlan`, the naming policies of
    `99-default.link`, `MACAddress=`), which udev applies whenever the
    interface is created later (rescan, re-bind);
  * sets the address with `ip link` on an interface ath11k already created in
    the initramfs, which is still down because the unit runs before
    NetworkManager.
  NetworkManager's default `cloned-mac-address=preserve` keeps it; idle scans
  still use random addresses.  Nothing persists outside `/run`.  The Ultra is
  not included until its WCN7850's runtime address and EFS layout have been
  checked; the helper's driver list and the unit's `ConditionFirmware=` are the
  two places to extend.

## In-place kit

`tools/inplace/wifi/` (README there) installs all of the above on a running
X710 with one reboot: it rebuilds the running vendor_boot with only the ramdisk
fragment changed (byte-exact repack, checked), installs the two helpers, keeps
backups and has a rollback.

## Known gaps

* The address unit is X710-only (above).
* Suspend is policy-off on this board for now; ath11k power-cycles the chip
  across a suspend (about 1.5 s of the resume), but Wi-Fi across sleep with
  the L1 substates on has only seen the boot-time pm_test cycle.
* `regulatory.db` is not in the ramdisk, so the built-in cfg80211 still logs
  one `-2` for it at ~13 s; harmless, the phy is self-managed (US) and the
  rootfs copy from `wireless-regdb` serves every later lookup.
