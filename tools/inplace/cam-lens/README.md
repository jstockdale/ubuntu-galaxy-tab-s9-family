# Lens power-up kit (SM-X710, November 2026)

This kit goes on top of the [CX-vote kit](../ufs-cx/README.md) and changes
only the DTB.

**What changes:** the rear module's rail, `rear_cam_vio` (GPIO15
`RCAM_LDO_EN` from L4B), now waits 10 ms after it is switched on
(`startup-delay-us`). Both the HI1337 and the DW9808 lens hold it. The kernel
and module tree, and the lens bus at the dtsi's 1 MHz, stay the CX kit's.

**Why:** the lens failed right after a power-up. Its first register write
timed out (`i2c-qcom-cci ac16000.cci: master 0 queue 0 timeout`, then
`dw9808-vcm 14-000c: register 0x02 write failed: -110`). Runtime PM then
latched the failed resume, so every later open returned `EINVAL` until the
driver was rebound. libcamera disabled the lens, so the camera never focused
([cameras](../../docs/gts9wifi-cameras.md)).
- `dw9808_vcm` waits 1 ms after enabling the rail.
- Samsung's CamX data for this actuator (X710XXS5CYG1, the same on the
  SM-X910) powers it up as `RCAM_LDO_EN`, 1 ms, then VIO 1.8 V, 10 ms, before
  its first write.
- The first version of this kit ran the bus at stock's 400 kHz instead, and
  the lens failed the same way.

`make-kit.sh` proves its packing against the CX kit's own images:
- With the CX DTB, the boot recipe and the vendor_boot repack reproduce the CX
  kit's `boot.img` and `vendor_boot.img` byte for byte.
- The new images differ only in the DTB (`164e1bf8…`), and the only property
  that changes is `startup-delay-us` on `rear_cam_vio`.

## Install, check, roll back (tablet)

Copy the kit to `~/cam-lens`. If the 400 kHz kit is installed, run its
`rollback.sh` first (this kit's is the same); no reboot is needed in between.
Then:

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
- `lens-diag.sh`: the open, every step of the focus sweep and every one of
  the 20 power cycles succeed, and the lens is not in a runtime-PM `error`
  afterwards.
- In the Camera app the picture should come into focus.
