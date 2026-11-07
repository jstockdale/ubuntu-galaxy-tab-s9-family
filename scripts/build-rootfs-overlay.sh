#!/bin/bash
# Assemble the microSD rootfs overlay that the TWRP ZIP installs: the isolated
# ath12k modules built alongside this kernel, plus the proprietary firmware
# staged from the device.
#
# Scope note: this covers Hito 1 and 2, that is, everything needed for the
# kernel to reach userspace with Wi-Fi and a working GPU/ADSP firmware set.
# The UCM profile, udev rules and recovery services of the reference port are
# added later, one at a time and only once the matching regression has been
# observed under Ubuntu.
#
# No proprietary blob is ever copied into this repository, and nothing is
# flashed here.
set -euo pipefail

repo=$(cd "$(dirname "$0")/.." && pwd)
. "$repo/scripts/lib/device.sh"
base=${UBUNTU_WORKDIR:-/root/ubuntu-gts9u}
kernel_out=${KERNEL_OUT_DIR:-$base/out/kernel-$DEVICE}
overlay=${OVERLAY_OUT_DIR:-$base/out/rootfs-overlay}
initramfs_overlay=${INITRAMFS_OVERLAY_OUT_DIR:-$base/out/initramfs-overlay}
# The staged blobs live in the reference port's firmware package directory,
# where scripts/stage-stock-*.sh puts them after verifying pinned checksums.
firmware=${FIRMWARE_DIR:-'/mnt/c/Users/agcar/Desktop/Aplicaciones/Custom Roms/GALAXY TAB S9 ULTRA/Ubuntu Touch/PostmarketOS/pmaports/device/testing/firmware-samsung-gts9uwifi'}

test -d "$kernel_out/modules-root" || {
	echo "missing kernel modules: $kernel_out/modules-root" >&2
	echo 'run scripts/build-mainline-kernel.sh first' >&2
	exit 1
}
test -d "$firmware" || {
	echo "missing staged firmware: $firmware" >&2
	echo 'run the reference port stage-stock-*.sh helpers first' >&2
	exit 1
}

# Fail fast and completely: a missing blob halfway through leaves a partial
# overlay that would silently ship without, say, Wi-Fi firmware.
required="
a740_zap.mdt a740_zap.b00 a740_zap.b01 a740_zap.b02 a740_sqe.fw
gmu_gen70200.bin
adsp.mdt adsp_dtb.mdt adspr.jsn adsps.jsn adspua.jsn cdspr.jsn
$DEVICE_CARD-tplg.bin
$DEVICE_BT_FW
stm32_gts9family.bin
sensor-hexagonfs.tar.gz
"
case "$DEVICE_WLAN" in
ath12k)
	wlan_dir=ath12k/WCN7850/hw2.0
	required="$required official-amss.bin m3.bin official-board-2.bin qrd-board.bin regdb.bin"
	;;
ath11k)
	wlan_dir=ath11k/WCN6855/hw2.0
	required="$required wcn6855-hw20-amss.bin wcn6855-hw20-m3.bin wcn6855-hw20-regdb.bin"
	required="$required wcn6855-hw20-board-2.bin wcn6855-hw21-amss.bin wcn6855-hw21-m3.bin"
	required="$required samsung-board-2.bin bdwlan.elf regdb.bin"
	;;
*) echo "no firmware layout for DEVICE_WLAN=$DEVICE_WLAN" >&2; exit 1 ;;
esac
absent=
for f in $required; do
	[ -f "$firmware/$f" ] || absent="$absent $f"
done
if [ -n "$absent" ]; then
	echo "missing staged firmware in $firmware:" >&2
	for f in $absent; do echo "  $f" >&2; done
	echo 'stage them with scripts/stage-firmware.py (configs/firmware/README.md;' >&2
	echo 'the sensor tree needs --sscregistrygen) or the reference port' >&2
	echo 'stage-stock-*.sh helpers' >&2
	exit 1
fi

for path in "$overlay" "$initramfs_overlay"; do
	case "$path" in
		"$base"/out/*) rm -rf -- "$path" ;;
		*) echo "unsafe overlay path: $path" >&2; exit 1 ;;
	esac
done

mkdir -p \
	"$overlay/etc/modules-load.d" \
	"$overlay/etc/modprobe.d" \
	"$overlay/usr/lib/firmware/qcom/sm8550" \
	"$overlay/usr/lib/firmware/$wlan_dir" \
	"$overlay/usr/lib/firmware/qca" \
	"$overlay/usr/lib/firmware/keyboard_stm" \
	"$overlay/usr/share/qcom/sm8550/Samsung/$DEVICE_SENSOR_DIR" \
	"$initramfs_overlay/usr/lib/firmware/qca"

# --- isolated ath12k modules ----------------------------------------------
# These and boot.img are one signed set under kernel lockdown: installing them
# from a different build makes modprobe fail with "Operation not permitted".
cp -a "$kernel_out/modules-root/." "$overlay/"
# ath12k is the isolated signed module; ath11k is built into the gts9wifi kernel.
if [ "$DEVICE_WLAN" = ath12k ]; then
	printf 'ath12k\n' > "$overlay/etc/modules-load.d/ath12k.conf"
fi
# QCOMTEE is loaded manually during fingerprint bring-up.  Autoprobing it
# before logging is available turned a secure-firmware incompatibility into an
# early boot loop; `modprobe qcomtee` still works explicitly despite blacklist.
cat > "$overlay/etc/modprobe.d/gts9u-qcomtee.conf" <<'EOF'
# Load manually while diagnosing the EL721 secure-world bridge.
blacklist qcomtee
EOF
if [ "${DEVICE:-}" = gts9wifi ]; then
	# Keeps the DDR bandwidth monitor, and with it the interconnect's
	# sync_state(), out of the boot: an interconnect that leaves INT_MAX before
	# the boot-time pm_test cycle hangs that cycle on this board (Stage 3 and
	# 3b, 2026-11-07; config-gts9wifi.fragment).  `modprobe icc_bwmon` by hand
	# is the experiment switch.
	cat > "$overlay/etc/modprobe.d/gts9u-bwmon.conf" <<'EOF'
# Bring-up experiment switch: load by hand with `modprobe icc_bwmon`.
blacklist icc_bwmon
EOF
fi

# --- Adreno 740 -----------------------------------------------------------
for f in a740_zap.mdt a740_zap.b00 a740_zap.b01 a740_zap.b02 a740_sqe.fw \
	gmu_gen70200.bin; do
	install -m 0644 "$firmware/$f" "$overlay/usr/lib/firmware/qcom/$f"
done

# --- ADSP: Samsung's own signed image plus the mandatory adsp_dtb ----------
for f in "$firmware"/adsp.mdt "$firmware"/adsp.b* \
	"$firmware"/adsp_dtb.mdt "$firmware"/adsp_dtb.b*; do
	install -m 0644 "$f" "$overlay/usr/lib/firmware/qcom/sm8550/${f##*/}"
done
# pd-mapper reads /sys/class/remoteproc/*/firmware and scans that same
# directory, so the protection-domain maps must sit next to adsp.mdt.
for f in adspr.jsn adsps.jsn adspua.jsn cdspr.jsn; do
	install -m 0644 "$firmware/$f" "$overlay/usr/lib/firmware/qcom/sm8550/$f"
done
install -m 0644 "$firmware/$DEVICE_CARD-tplg.bin" \
	"$overlay/usr/lib/firmware/qcom/sm8550/$DEVICE_CARD-tplg.bin"

wlan=$overlay/usr/lib/firmware/$wlan_dir
case "$DEVICE_WLAN" in
ath12k)
	# --- Wi-Fi WCN7850 ------------------------------------------------
	# The official linux-firmware amss: Samsung's downstream HMT amss never
	# raises WMI ready under mainline ath12k, because it expects the
	# phy_ucode QMI download that only cnss2 implements.
	install -m 0644 "$firmware/official-amss.bin" "$wlan/amss.bin"
	# Canonical linux-firmware M3 image. Samsung ships no m3 for kiwi, and
	# feeding phy_ucode20.elf as M3 is not equivalent.
	install -m 0644 "$firmware/m3.bin" "$wlan/m3.bin"
	# The official container comes first and has no matching X910/board-id
	# 255 entry, so ath12k falls back to the proven QRD ELF in board.bin.
	# That ELF wrapper must stay: the Samsung HMT.2.0 board data crashes the
	# official HMT.1.1 amss with an MHI RDDM, and stripping the wrapper does
	# not help.
	install -m 0644 "$firmware/official-board-2.bin" "$wlan/board-2.bin"
	install -m 0644 "$firmware/qrd-board.bin" "$wlan/board.bin"
	install -m 0644 "$firmware/regdb.bin" "$wlan/regdb.bin"
	;;
ath11k)
	# --- Wi-Fi WCN6855 ------------------------------------------------
	# linux-firmware's own WCN6855 set, pinned by the manifest, plus
	# Samsung's X710 board data: the manifest derives samsung-board-2.bin
	# from bdwlan.elf, a one-entry board-2 container under the exact name
	# ath11k derives for this tablet (bus=pci,...,subsystem-device=0108,
	# qmi-chip-id=2,qmi-board-id=255).  It stays ELF-wrapped: every WCN6855
	# entry linux-firmware ships is, and ath11k reads the magic to tell the
	# firmware which BDF form it sends.  The same ELF is also board.bin, the
	# API-1 fallback.
	#
	# Samsung's board data is required: with linux-firmware's generic entry
	# for 17cb:0108/chip-id 2 the radio came up on 2.4 GHz only (every 5 GHz
	# channel enabled under a US regdomain, zero 5 GHz networks heard); with
	# the Samsung container the same scan found 14.  linux-firmware's regdb
	# works alongside it.  Both stay selectable for experiments:
	#   GTS9WIFI_ATH11K_BOARD  samsung | linux-firmware
	#   GTS9WIFI_ATH11K_REGDB  linux-firmware | samsung
	ath11k_board=${GTS9WIFI_ATH11K_BOARD:-samsung}
	ath11k_regdb=${GTS9WIFI_ATH11K_REGDB:-linux-firmware}
	case "$ath11k_board" in
	samsung) board2_src=$firmware/samsung-board-2.bin ;;
	linux-firmware) board2_src=$firmware/wcn6855-hw20-board-2.bin ;;
	*) echo "GTS9WIFI_ATH11K_BOARD=$ath11k_board: expected linux-firmware or samsung" >&2; exit 1 ;;
	esac
	case "$ath11k_regdb" in
	samsung) regdb_src=$firmware/regdb.bin ;;
	linux-firmware) regdb_src=$firmware/wcn6855-hw20-regdb.bin ;;
	*) echo "GTS9WIFI_ATH11K_REGDB=$ath11k_regdb: expected linux-firmware or samsung" >&2; exit 1 ;;
	esac
	echo "ath11k hw2.1 board data: $ath11k_board, regdb: $ath11k_regdb"
	install -m 0644 "$firmware/wcn6855-hw20-amss.bin" "$wlan/amss.bin"
	install -m 0644 "$firmware/wcn6855-hw20-m3.bin" "$wlan/m3.bin"
	install -m 0644 "$regdb_src" "$wlan/regdb.bin"
	install -m 0644 "$board2_src" "$wlan/board-2.bin"
	install -m 0644 "$firmware/bdwlan.elf" "$wlan/board.bin"
	# The tablet's QCA6490 reports hw2.1 (chip_id 2, subsystem 17cb:0108) and
	# ath11k has no fallback to hw2.0.  The linux-firmware LITE amss boots
	# and crashes into RDDM on this silicon (confirmed on hardware); the
	# IOE build 04866.5 from the ath11k-firmware repository, which the
	# Fedora gts9wifi port runs, is what works here.
	hw21=$overlay/usr/lib/firmware/ath11k/WCN6855/hw2.1
	mkdir -p "$hw21"
	install -m 0644 "$firmware/wcn6855-hw21-amss.bin" "$hw21/amss.bin"
	install -m 0644 "$firmware/wcn6855-hw21-m3.bin" "$hw21/m3.bin"
	install -m 0644 "$regdb_src" "$hw21/regdb.bin"
	install -m 0644 "$board2_src" "$hw21/board-2.bin"
	install -m 0644 "$firmware/bdwlan.elf" "$hw21/board.bin"
	;;
esac

# --- CS35L45 speaker protection ------------------------------------------
# Staged for parity with the reference port. The codec probes without these,
# and protection is not claimed to work until it is measured.
for f in cs35l45-dsp1-spk-prot.wmfw cs35l45-dsp1-spk-prot.bin \
	cs35l45-dsp1-spk-prot-calib.bin; do
	[ -f "$firmware/$f" ] && install -m 0644 "$firmware/$f" \
		"$overlay/usr/lib/firmware/$f"
done

# --- Bluetooth ------------------------------------------------------------
# hci_qca is built-in and probes long before the microSD is mounted, so the
# same files also go into the vendor_boot ramdisk fragment.  They must be
# under /usr/lib, never /lib: in the initramfs lib is a symlink to usr/lib and
# creating a directory over it broke the boot of pmOS v0.69.
for f in $DEVICE_BT_FW; do
	install -m 0644 "$firmware/$f" "$overlay/usr/lib/firmware/qca/$f"
	install -m 0644 "$firmware/$f" "$initramfs_overlay/usr/lib/firmware/qca/$f"
done

# --- EF-DX920 keyboard controller -----------------------------------------
# Samsung's tablet-side STM32 boot ROM is always recoverable.  The device
# service requests an update only when its application is older, the exact
# official blob passes the pinned SHA-256 check, and the tablet is charging.
install -m 0644 "$firmware/stm32_gts9family.bin" \
	"$overlay/usr/lib/firmware/keyboard_stm/stm32_gts9family.bin"

# --- sensors: HexagonFS tree ----------------------------------------------
# Required above, not optional: without the tree hexagonrpcd serves an empty
# directory, SSC never publishes a sensor, and rotation and the light sensor
# fail on every boot with nothing but a late "SSC did not become ready".  The
# first X710 images shipped exactly that while this was only a note.
#
# --strip-components=1 is required.  The tarball nests everything under
# sensor-hexagonfs/, and without it hexagonrpcd looks one level too high
# and reports "Could not open /../sns_reg_version: No such file".
sensor_tree=$overlay/usr/share/qcom/sm8550/Samsung/$DEVICE_SENSOR_DIR
tar -xzf "$firmware/sensor-hexagonfs.tar.gz" -C "$sensor_tree" \
	--strip-components=1
# Without the completion marker and the timestamp cache the DSP rebuilds the
# registry at boot and closes the session doing so (see
# ubuntu-gts9u-sensor-registry); without the skeletons the PD cannot start.
for f in sensors/registry/sensors_registry sensors/registry/sns_reg_config \
	sensors/sns_reg.conf sensors/sns_reg_version socinfo/soc_id \
	dsp/adsp/sns_tppe.so; do
	[ -f "$sensor_tree/$f" ] || {
		echo "sensor-hexagonfs.tar.gz has no $f" >&2
		exit 1
	}
done

# --- bring-up logger (GTS9U_BRINGUP_SDLOG=1 only) ---------------------------
# A board with no working panel, no account yet and no USB gadget has no way to
# show its kernel log, and Samsung's sec_log_buf route only ever carries the
# bootloader's lines through TWRP.  This unit writes dmesg, the journal, the
# previous boot's ramoops records and the DRM connector state onto the microSD
# card during the first three minutes of every boot; TWRP reads the card.
# Never on by default: it mounts and writes removable media on every boot.
# The sources live in scripts/bringup-sdlog/ (the in-place kits install the
# same files).
if [ "${GTS9U_BRINGUP_SDLOG:-0}" = 1 ]; then
	echo 'bring-up logger enabled: kernel logs will be written to the microSD card'
	sdlog=$repo/scripts/bringup-sdlog
	mkdir -p "$overlay/usr/libexec" "$overlay/usr/lib/systemd/system" \
		"$overlay/etc/systemd/system/multi-user.target.wants" \
		"$overlay/etc/systemd/system/timers.target.wants"
	install -m 0755 "$sdlog/gts9u-bringup-sdlog" "$overlay/usr/libexec/gts9u-bringup-sdlog"
	install -m 0644 "$sdlog/gts9u-bringup-sdlog.service" "$sdlog/gts9u-bringup-sdlog.timer" \
		"$overlay/usr/lib/systemd/system/"
	ln -sf /usr/lib/systemd/system/gts9u-bringup-sdlog.timer \
		"$overlay/etc/systemd/system/timers.target.wants/gts9u-bringup-sdlog.timer"
	ln -sf /usr/lib/systemd/system/gts9u-bringup-sdlog.service \
		"$overlay/etc/systemd/system/multi-user.target.wants/gts9u-bringup-sdlog.service"
fi

# --- bring-up USB gadget (GTS9U_USB_GADGET=1, defaults to the SD logger) -----
# A board without working Wi-Fi has no way in once it boots: Ubuntu exposes no
# USB function and adb does not exist here.  This unit builds a composite
# gadget on the Type-C port whenever the TCPM leaves the dwc3 in device mode
# (tablet plugged into a PC): an NCM network interface at 10.42.0.2/24 with
# sshd behind it, and an ACM serial port with a login getty on ttyGS0.  Host
# mode (hubs, sticks, keyboards) is untouched because the role switch stays
# with the Type-C controller.  Never on by default in release images.
if [ "${GTS9U_USB_GADGET:-${GTS9U_BRINGUP_SDLOG:-0}}" = 1 ]; then
	echo 'bring-up USB gadget enabled: NCM network 10.42.0.2 + ACM console on the Type-C port'
	mkdir -p "$overlay/usr/libexec" "$overlay/usr/lib/systemd/system" \
		"$overlay/etc/systemd/system/multi-user.target.wants" \
		"$overlay/etc/NetworkManager/system-connections"
	cat > "$overlay/usr/libexec/gts9u-usb-gadget" <<'EOF'
#!/bin/sh
# Bring-up USB gadget: NCM network (usb0) + ACM serial (ttyGS0) on the dwc3.
# Idempotent; configfs only.  The data role itself is left to the Type-C
# controller, so nothing enumerates unless a USB host is attached.
set -u
g=/sys/kernel/config/usb_gadget/gts9u
mountpoint -q /sys/kernel/config || mount -t configfs none /sys/kernel/config || exit 0
for i in $(seq 1 20); do [ -n "$(ls /sys/class/udc 2>/dev/null)" ] && break; sleep 1; done
udc=$(ls /sys/class/udc 2>/dev/null | head -n1)
[ -n "$udc" ] || { echo 'gts9u-usb-gadget: no UDC registered, skipping' >&2; exit 0; }
if [ -d "$g" ] && [ -n "$(cat "$g/UDC" 2>/dev/null)" ]; then
	echo 'gts9u-usb-gadget: already bound' >&2
	exit 0
fi
mkdir -p "$g" || exit 0
cd "$g" || exit 0
echo 0x1d6b > idVendor
echo 0x0104 > idProduct
echo 0x0200 > bcdUSB
mkdir -p strings/0x409 configs/c.1/strings/0x409
echo Samsung > strings/0x409/manufacturer
tr -d '\0' < /proc/device-tree/model > strings/0x409/product 2>/dev/null || echo 'Galaxy Tab S9' > strings/0x409/product
cut -c1-16 /etc/machine-id > strings/0x409/serialnumber 2>/dev/null || echo gts9u > strings/0x409/serialnumber
echo 'NCM network + ACM console' > configs/c.1/strings/0x409/configuration
echo 250 > configs/c.1/MaxPower
mkdir -p functions/ncm.usb0 functions/acm.GS0
# Stable, locally administered MACs derived from the machine id, so the host
# sees the same link every boot instead of a fresh random one.
h=$(sha256sum /etc/machine-id 2>/dev/null | cut -c1-20)
if [ ${#h} -eq 20 ]; then
	echo "02$(echo "$h" | cut -c1-10 | sed 's/../:&/g')" > functions/ncm.usb0/dev_addr 2>/dev/null
	echo "06$(echo "$h" | cut -c11-20 | sed 's/../:&/g')" > functions/ncm.usb0/host_addr 2>/dev/null
fi
[ -e configs/c.1/ncm.usb0 ] || ln -s functions/ncm.usb0 configs/c.1/ncm.usb0
[ -e configs/c.1/acm.GS0 ] || ln -s functions/acm.GS0 configs/c.1/acm.GS0
echo "$udc" > UDC || { echo "gts9u-usb-gadget: binding to $udc failed" >&2; exit 0; }
echo "gts9u-usb-gadget: bound to $udc (role: $(cat /sys/class/usb_role/*/role 2>/dev/null | head -n1))" >&2
exit 0
EOF
	chmod 0755 "$overlay/usr/libexec/gts9u-usb-gadget"
	cat > "$overlay/usr/lib/systemd/system/gts9u-usb-gadget.service" <<'EOF'
[Unit]
Description=Bring-up: USB NCM network and ACM console gadget on the Type-C port
After=sys-kernel-config.mount systemd-udevd.service
Wants=sys-kernel-config.mount
# The panel cold-boot recovery runs a platform suspend/resume cycle ~30 s into
# the boot; a gadget bound before it is dropped by the controller's resume, so
# bind after that cycle (and re-bind after every later resume, see the
# system-sleep hook).
After=ubuntu-gts9u-panel-coldboot-recover.service
Before=NetworkManager.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/sh /usr/libexec/gts9u-usb-gadget
EOF
	ln -sf /usr/lib/systemd/system/gts9u-usb-gadget.service \
		"$overlay/etc/systemd/system/multi-user.target.wants/gts9u-usb-gadget.service"
	# Re-bind the gadget after any suspend/resume (dwc3 drops the pull-up).
	mkdir -p "$overlay/usr/lib/systemd/system-sleep"
	cat > "$overlay/usr/lib/systemd/system-sleep/gts9u-usb-gadget" <<'EOF'
#!/bin/sh
# Rebind the bring-up USB gadget to the UDC after resume.
[ "$1" = post ] || exit 0
g=/sys/kernel/config/usb_gadget/gts9u
[ -d "$g" ] || exit 0
udc=$(ls /sys/class/udc 2>/dev/null | head -n1)
[ -n "$udc" ] || exit 0
echo '' > "$g/UDC" 2>/dev/null
sleep 1
echo "$udc" > "$g/UDC" 2>/dev/null
exit 0
EOF
	chmod 0755 "$overlay/usr/lib/systemd/system-sleep/gts9u-usb-gadget"
	# Login prompt on the ACM port (115200 8N1 on the host side, /dev/ttyACM*),
	# started by udev only once the gadget actually creates ttyGS0 so a boot
	# with no USB host attached does not leave a failed getty behind.
	mkdir -p "$overlay/usr/lib/udev/rules.d"
	cat > "$overlay/usr/lib/udev/rules.d/90-gts9u-usb-gadget-getty.rules" <<'EOF'
SUBSYSTEM=="tty", KERNEL=="ttyGS[0-9]*", TAG+="systemd", ENV{SYSTEMD_WANTS}+="serial-getty@%k.service"
EOF
	# NetworkManager owns usb0 with a fixed address; the host side takes 10.42.0.1
	# and (in NM "shared" mode) routes and resolves for the tablet, so carry a
	# default route and DNS through it, at a metric Wi-Fi (600) wins over.
	cat > "$overlay/etc/NetworkManager/system-connections/gts9u-usb.nmconnection" <<'EOF'
[connection]
id=gts9u-usb
type=ethernet
interface-name=usb0
autoconnect=true
autoconnect-priority=10

[ipv4]
method=manual
addresses=10.42.0.2/24
gateway=10.42.0.1
dns=10.42.0.1;
route-metric=2000

[ipv6]
method=link-local
EOF
	chmod 0600 "$overlay/etc/NetworkManager/system-connections/gts9u-usb.nmconnection"
fi

# --- late PCIe endpoint rescan -------------------------------------------
# Mainline's DesignWare host waits ~1 s for link training and never rescans a
# link that trains later; the X710's QCA6490 needs longer after WLAN_EN.  The
# kernel wait is extended for gts9wifi, and this unit is the safety net: if
# no endpoint sits behind the root port once userspace is up, rescan.  It is a
# no-op on boards whose Wi-Fi enumerated at boot.
mkdir -p "$overlay/usr/lib/systemd/system" \
	"$overlay/etc/systemd/system/multi-user.target.wants"
cat > "$overlay/usr/lib/systemd/system/gts9u-pci-rescan.service" <<'EOF'
[Unit]
Description=Rescan the PCIe bus if the Wi-Fi endpoint trained its link late
After=basic.target
ConditionPathExists=/sys/bus/pci/rescan

[Service]
Type=oneshot
# The root port's own rescan (pci_rescan_bus_bridge_resize) re-sizes its
# windows for a late endpoint; the global /sys/bus/pci/rescan does not and
# leaves a 2 MiB BAR with "can't assign; no space".  An endpoint found without a
# driver bound is dropped first so its BARs are re-read.
# A hot-added endpoint behind windows that were carved for an empty slot gets
# "can't assign; no space" and even the root port's own rescan does not grow
# them, so after a failed attempt re-create the root port: remove 00:00.0 and
# rescan the host bridge, which sizes the windows for the endpoint present.
ExecStart=/bin/sh -c 'for i in 1 2 3 4; do ep=/sys/bus/pci/devices/0000:01:00.0; if [ -e "$ep/driver" ]; then exit 0; fi; if [ -e "$ep" ]; then echo 1 > "$ep/remove"; sleep 1; echo 1 > /sys/bus/pci/devices/0000:00:00.0/remove; sleep 2; echo 1 > /sys/bus/pci/rescan; else sleep 3; echo 1 > /sys/bus/pci/devices/0000:00:00.0/rescan; fi; sleep 6; done; [ -e /sys/bus/pci/devices/0000:01:00.0/driver ] || echo "gts9u-pci-rescan: no driver-bound PCIe endpoint after rescans"'

[Install]
WantedBy=multi-user.target
EOF
ln -sf /usr/lib/systemd/system/gts9u-pci-rescan.service \
	"$overlay/etc/systemd/system/multi-user.target.wants/gts9u-pci-rescan.service"

# --- staged-firmware provenance -------------------------------------------
# A staging produced by scripts/stage-firmware.py records which stock package
# and bootloader revision it came from.  Ship the record so a running system
# can answer "which firmware is this installation matched to" exactly.
if [ -f "$firmware/PROVENANCE.json" ]; then
	mkdir -p "$overlay/usr/share/ubuntu-gts9u"
	install -m 0644 "$firmware/PROVENANCE.json" \
		"$overlay/usr/share/ubuntu-gts9u/firmware-provenance.json"
fi

find "$overlay" -type f -exec chmod 0644 {} +
find "$overlay" -type f -name '*.ko*' -exec chmod 0644 {} +
[ -f "$overlay/usr/lib/systemd/system-sleep/gts9u-usb-gadget" ] && \
	chmod 0755 "$overlay/usr/lib/systemd/system-sleep/gts9u-usb-gadget"
# NetworkManager refuses keyfiles that are group/world readable.
[ -d "$overlay/etc/NetworkManager/system-connections" ] && \
	find "$overlay/etc/NetworkManager/system-connections" -type f -exec chmod 0600 {} +

echo '=== overlay ==='
du -sh "$overlay" "$initramfs_overlay"
find "$overlay" -type f | wc -l | sed 's/^/files: /'
