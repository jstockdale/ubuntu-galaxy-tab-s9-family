# Camera-bus kit (SM-X710, November 2026)

This kit goes on top of the [BT UART kit](../bt-uart/README.md) and changes
only the DTB. It replaces the [lens-pads kit](../cam-pads/README.md): roll
that one back first.

**What changes:** the rear camera's two buses run as Samsung's stock DT sets
them for this board:
- CCI0 master 1 (the HI1337) and CCI1 master 0 (the DW9808 lens and its
  EEPROM) run at 400 kHz (`i2c-freq-mode 1`);
- their pins get 4 mA and the TLMM's ordinary weak pull-up.

sm8550.dtsi runs them at 1 MHz, with 2 mA and the strong 2.2 kOhm pull-up. The
kernel, modules and 10 ms lens power-up delay stay as they are. The front
camera's bus keeps the dtsi's settings until that camera is tested.

**Why:** we keep the camera as close to stock as we can. The two settings go
together. With the lens-pads kit (the weak pull-up at 1 MHz), the lens's
status reads failed ("Device or resource busy"). With the dtsi's strong
pull-up, the lens's first write after a quick power-down timed out now and
then ([cameras](../../docs/gts9wifi-cameras.md#cci-timeouts-two-problems-not-one-2026-11-08)).

`make-kit.sh` proves its packing against the installed images:
- With the lens DTB, the boot recipe reproduces the BT UART kit's `boot.img`,
  and the vendor_boot repack reproduces the lens kit's `vendor_boot.img`,
  byte for byte.
- The new images differ only in the DTB (`4ed9c1d9…`). Against the lens DTB
  it changes two `clock-frequency` values (CCI0 bus 1, CCI1 bus 0), plus
  `drive-strength` and `bias-pull-up` on GPIO112-115.

## Install, check, roll back (tablet)

Copy the kit to `~/cam-bus`, then:

```sh
sudo sh ~/cam-pads/rollback.sh          # if the lens-pads kit is installed
sudo sh ~/cam-bus/install.sh            # then: sudo reboot
sudo sh ~/cameras-kit/camera-test.sh rear
sudo sh ~/cam-bus/rollback.sh           # back to the BT UART kit; then: sudo reboot
```

`install.sh` stops before writing anything unless all of these hold:
- the kit matches its checksums;
- the lens-pads kit is not installed;
- the BT UART kit is installed: its marker exists, `boot` holds its image
  (`faa774ad…`) and `vendor_boot` holds the lens kit's (`b18510f3…`);
- root is writable;
- the battery is at 30 % or more.

It saves both partitions to `/var/backups/ubuntu-gts9u-cam-bus/`, then writes
`vendor_boot` and `boot`, synchronously, reading each back.

**Expect:**
- the camera starts and streams as before;
- no `dw9808` write failure when the camera is enabled;
- autofocus as before, quiet and smooth.

Do not run `lens-diag.sh`'s quick cycles: they power the lens down mid-move,
which stock never does.
