# Per-device identity for the Tab S9 family build, sourced by the build scripts.
#
# DEVICE selects the board; everything that differs between family members is
# looked up here and nowhere else, so adding a device is one new case below plus
# its DTS, config fragment and firmware manifest.  Defaults keep the original
# gts9uwifi build byte-for-byte unchanged when DEVICE is unset.
#
#   DEVICE            Ubuntu/pmOS codename: gts9uwifi (SM-X910), gts9wifi (SM-X710)
#   DEVICE_MODEL      Samsung model as the bootloader and installer report it
#   DEVICE_NAME       product name for human-readable titles
#   DEVICE_SLUG       artifact file-name slug
#   DEVICE_LABEL      root filesystem label (scoped per device: a mismatched
#                     kernel/rootfs pairing must fail loudly at mount, never
#                     half-boot the other tablet's userspace)
#   DEVICE_CARD       ALSA sound-card model (keys the UCM profile and the
#                     AudioReach topology file name)
#   DEVICE_CODENAMES  codenames the TWRP installer accepts for this image
#   DEVICE_WLAN       ath12k (WCN7850) or ath11k (WCN6855)
#   DEVICE_BT_FW      Bluetooth firmware files the overlay installs under qca/
#   DEVICE_SENSOR_DIR Samsung sensor-registry directory name under qcom/sm8550/Samsung

DEVICE=${DEVICE:-gts9uwifi}

case "$DEVICE" in
gts9uwifi)
	DEVICE_MODEL=SM-X910
	DEVICE_NAME="Samsung Galaxy Tab S9 Ultra Wi-Fi"
	DEVICE_SLUG=sm-x910
	DEVICE_LABEL=UBTS9U_UFS
	DEVICE_CARD=Samsung-Galaxy-Tab-S9-Ultra
	DEVICE_CODENAMES="gts9u gts9uwifi"
	DEVICE_WLAN=ath12k
	DEVICE_BT_FW="hmtbtfw20.tlv hmtnv20.b21"
	DEVICE_SENSOR_DIR=gts9uwifi
	;;
gts9wifi)
	DEVICE_MODEL=SM-X710
	DEVICE_NAME="Samsung Galaxy Tab S9 Wi-Fi"
	DEVICE_SLUG=sm-x710
	DEVICE_LABEL=UBTS9_UFS
	DEVICE_CARD=Samsung-Galaxy-Tab-S9
	DEVICE_CODENAMES="gts9 gts9wifi"
	DEVICE_WLAN=ath11k
	# No firmware-name in the DTS: hci_qca derives hpbtfw21.tlv from the ROM
	# version and hpnv21[g].bXX from the board id it reads back, falling back
	# to hpnv21[g].bin, so every variant Samsung ships has to be reachable.
	DEVICE_BT_FW="hpbtfw21.tlv hpnv21.bin hpnv21.b9a hpnv21.b9b hpnv21.baa hpnv21.bb7 hpnv21.bb9"
	DEVICE_BT_FW="$DEVICE_BT_FW hpnv21g.bin hpnv21g.b9a hpnv21g.b9b hpnv21g.baa hpnv21g.bb7 hpnv21g.bb9"
	DEVICE_SENSOR_DIR=gts9wifi
	;;
*)
	echo "unknown DEVICE '$DEVICE' (expected gts9uwifi or gts9wifi)" >&2
	exit 1
	;;
esac

DEVICE_DTS=sm8550-samsung-$DEVICE
export DEVICE DEVICE_MODEL DEVICE_SLUG DEVICE_LABEL DEVICE_CARD \
	DEVICE_CODENAMES DEVICE_WLAN DEVICE_BT_FW DEVICE_SENSOR_DIR DEVICE_DTS
