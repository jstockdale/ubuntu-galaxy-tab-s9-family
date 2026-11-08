# Lens-bus kit (SM-X710, November 2026)

This kit goes on top of the [CX-vote kit](../ufs-cx/README.md) and changes
only the DTB.

**What changes:** CCI1 master 0 carries the rear DW9808 lens and its EEPROM.
It now runs at 400 kHz, the speed Samsung's stock DT uses for every camera
device here (`i2c-freq-mode 1`), instead of sm8550.dtsi's 1 MHz. The kernel and
module tree stay the CX kit's.

**Why:** at 1 MHz the lens answered its power-up sequence and one move. Then a
transfer went wrong, and every transfer after it timed out
(`i2c-qcom-cci ac16000.cci: master 0 queue 0 timeout`) until the module lost
power. libcamera then disabled the lens, so the camera never focused
([cameras](../../docs/gts9wifi-cameras.md)).

`make-kit.sh` proves its packing against the CX kit's own images:
- With the CX DTB, the boot recipe and the vendor_boot repack reproduce the CX
  kit's `boot.img` and `vendor_boot.img` byte for byte.
- The new images differ only in the DTB (`e6e7b957…`), and the only property
  that changes is CCI1 master 0's `clock-frequency`.

## Install, check, roll back (tablet)

Copy the kit to `~/cam-lens`, then:

```sh
sudo sh ~/cam-lens/install.sh       # then: sudo reboot
sudo sh ~/cameras-kit/camera-test.sh rear
sudo sh ~/cameras-kit/lens-diag.sh  # the same boot, Camera app closed
sudo sh ~/cam-lens/rollback.sh      # back to the CX kit; then: sudo reboot
```

`install.sh` stops before writing anything unless all of these hold:
- the kit matches its checksums;
- the CX kit is installed (its marker exists, and `boot` and `vendor_boot`
  hold its images);
- root is writable;
- the battery is at 30 % or more.

It saves both partitions to `/var/backups/ubuntu-gts9u-cam-lens/`. It writes
`vendor_boot` and then `boot`, synchronously, and reads each back.

Roll this kit back before the CX kit's own `rollback.sh`.

**Expect:**
- `camera-test.sh rear`: no CCI timeout or `dw9808` write failure in dmesg,
  and no "Lens initialisation failed" from libcamera.
- `lens-diag.sh`: the open and every step of the focus sweep succeed, and
  the lens is not in a runtime-PM `error` afterwards.
- In the Camera app the picture should come into focus.
