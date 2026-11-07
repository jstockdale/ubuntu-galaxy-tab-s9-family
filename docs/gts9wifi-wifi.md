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
* PCIe0 is pinned to Gen2 like stock, the DesignWare link wait is four times
  longer on this board, and `gts9u-pci-rescan.service` re-enumerates the
  endpoint when the first probe loses the race (this 7.2 tree has no link-up
  rescan).

## Firmware (manifest `configs/firmware/x710-bl5-*.toml`)

The chip reports **hw2.1** (chip_id 0x2, subsystem 17cb:0108, board_id 0xff),
and ath11k has no fallback to the hw2.0 directory.

| file | source | note |
| --- | --- | --- |
| `hw2.1/amss.bin`, `hw2.1/m3.bin` | ath11k-firmware `WLAN.HSP.1.1-04866.5-QCAHSPSWPL_V1_V2_SILICONZ_IOE-1` | linux-firmware's LITE build boots and crashes into RDDM on this silicon |
| `hw2.1/board-2.bin` | **Samsung's `bdwlan.elf`**, wrapped by the manifest (`derived:board2`) under `bus=pci,vendor=17cb,device=1103,subsystem-vendor=17cb,subsystem-device=0108,qmi-chip-id=2,qmi-board-id=255` | see below |
| `hw2.1/board.bin` | the same ELF, API-1 fallback | |
| `hw2.1/regdb.bin` | linux-firmware hw2.0 regdb | works with the Samsung board data |

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

## Userspace

* `wireless-regdb` and `iw` are in the rootfs package list; without the
  regulatory database cfg80211 logs a load failure at every boot.
* The chip has no MAC in OTP: the interface comes up with a Qualcomm default
  address (00:03:7f:12:xx:xx, and it changes with the board file).  Stock
  Android reads the device's address from EFS (`/efs/wifi/.mac.info`).
  Giving the interface its real address is still to do.

## Known gaps

* No stable MAC (above).
* Suspend/resume is not functional on this board yet, so Wi-Fi power
  management across sleep is untested.
