# Book Cover Keyboard support and diagnostics

## EF-DX915: reconnecting cover and missing touchpad

The driver now runs Samsung's application initialization for every recognized
cover instead of only EF-DX920. Skipping it on EF-DX915 was a plausible cause of
the reported roughly three-second connection cycles; this is not yet confirmed
on that cover. The touchpad previously had no input device and its packets were
discarded. The driver now exposes a separate three-slot clickpad, using Samsung's
22-byte packet layout and model-specific dimensions. It also completes the
touch-controller CRC/resolution handshake and releases contacts on errors,
reset, hall changes and detach. Libinput handles pointer motion and gestures.

This change compiles for ARM64 and the actual packet handler passes synthetic
tests with memory/undefined-behavior sanitizers. **EF-DX915 hardware validation
is still required.** Kernel changes take effect only after installing a build
containing the new kernel; installing Companion alone adds diagnostics only.

In **Tab Companion → About → Troubleshooting → Save keyboard cover logs**,
keep the affected cover connected and reproduce the fault during the 20-second
capture. The confirmation explicitly warns that cover key presses, releases and
repeats are recorded for 20 seconds after authorization. Do not type passwords
or sensitive information during this period. The physical key codes are recorded
before remapping, and capture stops automatically even if the UI disappears.
The JSON is saved privately in `Documents/Tab Companion/Diagnostics` (using the
localized Documents directory), with a local state-directory fallback if that
folder is unavailable. The app displays the saved path. Review it before sharing
and attach it to the issue with:

- The printed cover model, tablet model and whether the issue also occurs in Android.
- Whether the problem occurs while idle, typing, touching the pad or moving the cover.
- Whether the cover was attached before boot or connected after login.

The report contains build/package versions, controller firmware identity,
connection/IRQ/recovery counters sampled once per second, input-device and
libinput classification, axis geometry, GNOME touchpad settings and relevant
current/previous-boot kernel and firmware-service logs. Missing tools or logs
are reported without aborting collection. Historical key codes, current touch
coordinates, device serials and user files are excluded. The separate
`keyboard_capture` section contains only the explicitly authorized test-period
key events and can reveal what was typed. Nothing is uploaded automatically.
Driver diagnostics with `driver_revision=2` include
initialization attempts/errors and touchpad packet/error counters.
`driver_revision=3` adds the board, the hall state and counters for hall
packets and wake arming, and no longer names the last key. It is readable by
root only: sampled fast enough, its counters time every keystroke (as the data
IRQ's line in `/proc/interrupts` still does for anyone).
`cover_state` next to it is the world-readable part (attached, connected,
protocol id) that Tab Companion reads.

Only the hardware service may record keys: `BeginKeyboardCapture` needs a
one-time token that the root collector writes after authentication, readable
by root and the `input` group alone.

## EF-DX920 firmware background

The Book Cover Keyboard Slim talks to an STM32G0 that lives **in the tablet**,
on I2C6 at address `0x2a`, not in the cover.  That controller runs one of two
applications, and only one of them works here:

| Version | Under Ubuntu | Under One UI |
|---|---|---|
| **V37** (Samsung's X910 blob) | works | works |
| V34 (older application) | announces no protocol ID; cover is dead | works |

The mainline driver reproduces Samsung's V37 start-up sequence.  V34 speaks a
different one: the controller boots fine, pulses the connection line every
~2.1 s and never sends `0xd6`.  That is the whole failure, and it looks exactly
like a wiring or timing problem, which is why it cost several sessions.

## Checking

```
sudo cat /sys/bus/i2c/drivers/samsung-gts9u-stm32-pogo/*-002a/diagnostics
cat /sys/bus/i2c/drivers/samsung-gts9u-stm32-pogo/*-002a/cover_state
```

The Linux I2C adapter number is not stable when other controllers are added to
the kernel, so diagnostics and the automatic restorer locate the device through
its bound driver rather than assuming that it will always be `6-002a`.

`flash_version=00370037` is healthy.  `flash_version=00340034` means the
controller has been put back to V34 and the cover will not work until it is
restored.

## Restoring

`ubuntu-gts9u-pogo-firmware.service` does it automatically, once per boot,
whenever the version is not V37.  **Nothing else is required** — in particular
not the cover and not a charger.

The cover used to be required, on the assumption that the controller shared the
accessory's power rail.  Measured with the cover detached and `pogo_vddo`
disabled, the ROM bootloader still answered with its product id and flash
version: the rail that gets cut feeds the keyboard, while the controller sits on
the tablet's own I2C6.  So a controller found on V34 is repaired on the first
boot after an install whether or not the cover is anywhere near the tablet.

The only remaining condition is 15 % battery, and that is not protecting the
flash: the STM32 ROM bootloader cannot be erased, it answers on every boot, and
the driver re-reads all 52 KiB before reporting success, so an interrupted write
is simply completed on the next boot.  The threshold only avoids starting the
job on a tablet that is about to die mid-boot.

To run it by hand:

```
sudo /usr/libexec/ubuntu-gts9u-pogo-firmware-update
```

To stop it from ever writing:

```
sudo systemctl mask ubuntu-gts9u-pogo-firmware.service
```

## What still is not known

Nothing in this port ships a V34 image, so the downgrade does not come from
here.  The likely source is Samsung's own `stm32_pogo_v3` driver with its
vendor blobs, under One UI or under the Ubuntu Touch port.  If the cover stops
working after booting one of those, check `flash_version` first.
