# Lens-pads kit (SM-X710, November 2026)

This kit goes on top of the [BT UART kit](../bt-uart/README.md) and changes
only the DTB.

**What changes:** CCI1 master 0 carries the rear DW9808 lens and its EEPROM.
Its two pins get Samsung's stock pad settings for this board (the same as the
SM-X910's): 4 mA and the TLMM's ordinary weak pull-up. sm8550.dtsi gives them
2 mA and the strong 2.2 kOhm I2C pull-up. The kernel, modules, 10 ms
power-up delay and 1 MHz bus stay as they are.

**Why:** the lens's first write after a power-up still times out sometimes,
right after a recent power-down or one taken mid-move. Writes to a powered
lens never fail. The strong pull-ups go to the SoC's 1.8 V, which stays up
while the module's rail and VIO (L4B) are off. They may feed the unpowered
lens through its pins, so that it never quite resets. This kit tests that
([cameras](../../docs/gts9wifi-cameras.md#cci-timeouts-two-problems-not-one-2026-11-08)).

`make-kit.sh` proves its packing against the installed images:
- With the lens DTB, the boot recipe reproduces the BT UART kit's `boot.img`,
  and the vendor_boot repack reproduces the lens kit's `vendor_boot.img`,
  byte for byte.
- The new images differ only in the DTB (`19de4789…`). Against the lens DTB,
  the only properties that change are `drive-strength` and `bias-pull-up` on
  CCI1 master 0's two default-state pins.

## Install, check, roll back (tablet)

Copy the kit to `~/cam-pads`, then:

```sh
sudo sh ~/cam-pads/install.sh          # then: sudo reboot
sudo sh ~/cameras-kit/camera-test.sh rear
sudo sh ~/cameras-kit/lens-diag.sh     # the same boot, Camera app closed
sudo sh ~/cam-pads/rollback.sh         # back to the BT UART kit; then: sudo reboot
```

`install.sh` stops before writing anything unless all of these hold:
- the kit matches its checksums;
- the BT UART kit is installed: its marker exists, `boot` holds its image
  (`faa774ad…`) and `vendor_boot` holds the lens kit's (`b18510f3…`);
- root is writable;
- the battery is at 30 % or more.

It saves both partitions to `/var/backups/ubuntu-gts9u-cam-pads/`, then
writes `vendor_boot` and `boot`, synchronously, reading each back.

Roll back in reverse order: this kit first, then the BT UART kit's
`rollback.sh`, and so on.

**Expect:**
- no `dw9808` write failure at the probe-time power-up;
- in `lens-diag.sh`, the quick cycles pass: 6 of 6, where the lens kit failed
  on the second.
- If they still fail, the pads are not the cause: roll back.

**Result (boot `e2c815b8`):**
- The lens's probe-time power-up still timed out.
- The weak pull-up at the dtsi's 1 MHz broke the lens's status reads:
  `lens-diag.sh` opens failed with "Device or resource busy" in 3 of 20
  settled cycles and in quick cycle 5.

The pull-up belongs with stock's 400 kHz. Superseded by
[`cam-bus`](../cam-bus/README.md); roll this kit back.
