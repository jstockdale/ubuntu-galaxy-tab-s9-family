# BT UART kit (SM-X710, November 2026)

The kit goes on top of the [lens power-up kit](../cam-lens/README.md), which
sits on top of the [CX-vote kit](../ufs-cx/README.md). It fixes the
Bluetooth UART interrupt storm that disabled IRQ 191 in the boot-time
`pm_test=platform` cycle ([porting log](../../docs/porting-log.md#the-geni-uart-handler-ignored-its-interrupt-while-the-port-suspended-2026-11-08)):

- **The kernel** is the CX kernel plus
  `qcom-geni-serial-serve-irq-while-suspending.patch`, built from 5b08b13.
  It keeps fix6's configuration. Like the lens kit's kernel (the CX one), it
  does not carry dcd0343, the `dw9808_vcm` power-down fix.
- **The DTB** is the lens kit's (`164e1bf8…`): the CX DTB with the rear lens
  rail's 10 ms `startup-delay-us`. It is appended to the kernel in `boot`.
  `vendor_boot` stays the lens kit's image (`b18510f3…`), which carries the
  same DTB. The kit does not write it.
- **The module tree** comes from this kernel build. The build reuses the CX
  build's module signing key (ID `1ded767e…`). Both kernels therefore load
  both module trees. The installed device package 2.58's `qcom_spss_irq.ko`,
  signed for the CX kernel, still loads too.

## Build (host)

```sh
X=/path/to/scratch/build-btirq
mkdir -p $X/linux-gts9wifi/certs
cp -p $CX/linux-gts9wifi/certs/{x509.genkey,signing_key.pem,.signing_key.pem.cmd} \
  $X/linux-gts9wifi/certs/          # CX=the CX build's scratch directory
DEVICE=gts9wifi UBUNTU_WORKDIR=$HOME/ubuntu-gts9u \
  KERNEL_WORKTREE=$X/linux-src-gts9wifi KERNEL_BUILD_DIR=$X/linux-gts9wifi \
  KERNEL_OUT_DIR=$HOME/ubuntu-gts9u/out/kernel-gts9wifi-btirq \
  bash scripts/build-mainline-kernel.sh
bash tools/inplace/bt-uart/make-kit.sh \
  $HOME/ubuntu-gts9u/out/kernel-gts9wifi-btirq \
  $HOME/ubuntu-gts9u/out/kernel-gts9wifi-cx \
  $HOME/ubuntu-gts9u/out/dtb-lens-vdelay/sm8550-samsung-gts9wifi.dtb \
  $HOME/ubuntu-gts9u/backups/bt-uart-lens-kit
```

Copy the key with its `.cmd` file and its times (`cp -p`), so that kbuild
keeps the key. `make-kit.sh` stops in any of these cases:

- a module is not signed with the CX key;
- the module list differs from the CX build's;
- given the CX kernel, the `boot` recipe does not reproduce byte for byte
  either the CX kit's `boot.img` (`47f7df27…`, with the CX DTB) or the lens
  kit's (`57406c80…`, with the lens DTB).

## Install, check, roll back (tablet)

Copy the kit to `~/bt-uart`, then:

```sh
sudo sh ~/bt-uart/install.sh        # then: sudo reboot
sh ~/bt-uart/verify.sh              # kernel, logs, hci0 state
sudo sh ~/bt-uart/verify.sh         # also an HCI command round trip
sudo sh ~/bt-uart/rollback.sh       # back to the lens kit; then: sudo reboot
```

`install.sh` stops before writing anything unless all of these hold:

- the kit matches its checksums;
- the CX kit and the lens kit are installed: both markers exist, `boot`
  holds the lens kit's image (`57406c80…`) and `vendor_boot` holds the lens
  kit's image (`b18510f3…`);
- the running kernel is `7.2.0-rc3-dirty`;
- root is writable;
- the battery is at 30 % or more;
- dpkg is idle and clean;
- there is 600 MB free.

It saves `boot` and the module tree (the CX kit's, which the lens kit kept) to
`/var/backups/ubuntu-gts9u-bt-uart/`
(root-only, hash-checked), swaps the module tree, then writes `boot`
synchronously and reads it back. If the saved Ubuntu boot set holds a
`boot.img`, it keeps it in step.

Roll back in reverse order: this kit first, then the lens kit's
`rollback.sh`, then the CX kit's. If the tablet does not come back, boot TWRP
and write `/var/backups/ubuntu-gts9u-bt-uart/boot-lens.img` to `boot` from
the Ubuntu root. Then run `rollback.sh` on the next boot.

`dpkg -V ubuntu-gts9u-device` reports `qcom_spss_irq.ko` as changed while
the kit is installed, as it does under the G5 and CX kits. The kit's copy
comes from this build.

## The test: `suspend-bt.sh`

```sh
sudo sh ~/bt-uart/suspend-bt.sh platform [N]   # pm_test=platform cycles (default 20)
sudo sh ~/bt-uart/suspend-bt.sh s2idle [N]     # real s2idle, RTC-woken (default 3)
sudo sh ~/bt-uart/suspend-bt.sh deep [N]       # real deep, RTC-woken (default 3)
sh ~/bt-uart/suspend-bt.sh status              # progress and results, no root
sudo sh ~/bt-uart/suspend-bt.sh recover        # if the IRQ was disabled: rebind, address
```

The interrupt is routed to CPU 0, and the storm only kills the suspend when
the task that suspends the UART runs on CPU 0 as well. That would explain
why it hit 1 boot in 32. The test pins that task to the interrupt's CPU (`--no-pin`
turns this off), so every cycle is the bad case. Each cycle:

1. starts an LE scan (`btmgmt find -l`; `--no-scan` skips it);
2. suspends;
3. checks four things:
   - the kernel log since the cycle's marker shows no "nobody cared" or
     "Disabling IRQ";
   - no HCI timeouts;
   - the Bluetooth IRQ returned no `unhandled` result;
   - after resume the controller answers HCI Read Local Version.

A tracefs instance counts the IRQ's handler results. Two kprobes on
`qcom_geni_serial_suspend()` delimit the UART's suspend callback, so the
result line also counts the IRQs that came while the port was suspending.
On the patched kernel, a count above zero there means the fixed path ran.
The run stops at the first failure.

`s2idle` and `deep` run the suspendkit's `gts9u-suspend-cycles.sh` (shipped
in the kit) in the foreground for each cycle, with its hooks and checklist.
They work with `sleep.target` and `suspend.target` masked.

Matrix:

0. Optional, before installing, on the lens kit's (CX) kernel:
   `sudo sh ~/bt-uart/suspend-bt.sh platform 5 --no-scan`. This measures the
   old handler:
   - `unhandled` above 0 while the port was suspending confirms the window;
   - a disabled IRQ in an idle cycle points to the cancels in `stop_rx()` and
     `stop_tx()` asserting the line themselves, rather than received data.

   Afterwards, run `recover`.
1. After installing and rebooting: `verify.sh`, with and without sudo. The
   boot-time panel cycle has already run once on the new kernel.
2. `platform 20`. Expect PASS, with `unhandled 0` in every cycle.
3. `platform 10` with the headphones connected and playing audio, which is
   the most Bluetooth traffic there can be.
4. `platform 10 --no-pin`.
5. `s2idle 3`, then `deep 3`.
6. Reboot several times and run `verify.sh` after each boot: no "nobody
   cared", and hci0 up with its address.

A cycle that fails names its reasons in `cycles.tsv`:

- `irq-disabled`: the storm is back;
- `unhandled-N`: the handler ignored the line N times;
- `hci-timeouts` or `hci-no-answer`: the controller stopped answering;
- `suspend-rc-N`: the suspend itself failed.

Each cycle's trace and kernel log stay in its run directory.
