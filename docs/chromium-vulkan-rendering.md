# Chromium rendering on SM-X910, 2026-09-09

## Result

Chrome 152.0.7977.82 and ChatGPT now use hardware ANGLE/Vulkan on Mesa
25.2.8 (Turnip Adreno 740). ChatGPT also selects native Wayland when its
socket is available. No GPU-disable flags, software renderer, global Mesa
vendor spoof, or forced driver flush are installed.

The owner confirmed that Discord and Sheets now look correct and both Chrome
and ChatGPT feel substantially smoother. This is qualitative feedback, not a
controlled FPS, CPU or battery measurement. Other Electron applications are
not automatically covered by these two launcher integrations.

## Evidence and rejected configuration

An isolated Chrome profile reproduced the damaged Discord gradient with
ANGLE/OpenGL and rendered it cleanly with ANGLE/Vulkan. Synthetic canvas,
text, gradients and scrolling fixtures provide a repeatable local check.
The authenticated Sheets session could not be reused in the isolated profile;
Sheets validation therefore comes from the owner's actual browser session.

Chrome and the separate ChatGPT Wayland/Vulkan probe reported accelerated
canvas, GPU compositing, rasterization and WebGL. The renderer identified
Turnip Adreno 740 through ANGLE Vulkan. Chromium's separate native Vulkan
compositor status remains disabled; that field does not describe ANGLE's
selected backend.

ChatGPT with Vulkan on X11 failed EGL configuration selection and disabled
GPU compositing. That configuration was rejected. Native Wayland plus Vulkan
passed the GPU check and was then deployed to the normal ChatGPT launcher.
The running applications were restarted and the owner tested those sessions.

Missing colour emoji were a separate font issue: installing
`fonts-noto-color-emoji` restored the emoji font match. It is now a device
package dependency and part of the rootfs package list.

Keep the earlier [Mesa identity correction](chromium-freedreno-identity.md).
Restoring the global Qualcomm vendor spoof recreates a different Skia shader
failure. The precise driver defect behind the remaining GL corruption has
not been traced; the backend comparison establishes a working configuration.

## Persistent integration and recovery

Device package source 2.50 installs `ubuntu-gts9u-chromium-launchers`. It uses
package-owned dpkg diversions for the Chrome and ChatGPT vendor shell launchers,
retaining their originals as `.gts9u-original`. Sourcing the vendor scripts
preserves their executable discovery and Chrome's public PWA launcher path.
Package file triggers also cover installation and updates of those apps.

The launcher flags apply to `samsung,gts9uwifi` and, since 2026-11-06,
`samsung,gts9wifi` (see [SM-X710](#sm-x710-gts9wifi)). Explicit `--use-angle`
and `--ozone-platform` arguments take precedence. Foreign diversions and
edited launchers are not overwritten. Removing the device package restores
the vendor launchers. For a temporary comparison, fully close the relevant
application and launch it with `--use-angle=gl`.

The helper and fonts were deployed directly to the tablet; this did not
install the entire rebuilt device package or change the running kernel.
The full package build was attempted but stopped at its existing IRQ module
signing-key consistency check: the available module does not match the selected
kernel build certificate. No mismatched package was produced or installed.
The launcher lifecycle and shell/JavaScript syntax checks passed separately.
A reboot is not required. The package builder marks all libexec helpers
executable. Lifecycle tests cover repeated installation, original arguments,
explicit flags, vendor updates, Wayland selection and removal.

## SM-X710 (gts9wifi)

The SM-X710 has the same Adreno 740 and Mesa, so the Freedreno GL-path
damage that ANGLE/Vulkan avoids on the Ultra is expected there too; until
2026-11-06 the board gate left it on ANGLE/OpenGL. Both launchers and the
shared runner now give it the same ANGLE/Vulkan (and, where applicable,
Wayland) defaults. The X710's GPU write loss
([display notes](gts9wifi-display.md)) was, as suspected, below the GL
driver: rpmhpd never reached sync_state(). Kernels that let it sync at boot
lose no writes in the GL write-check battery, and on 2026-11-07 WebGL
rendered correctly on the tablet. ANGLE/Vulkan stays the default because of
the Freedreno GL-path damage above, not because of that defect. The
write-check kit drives GL only; to compare Chrome on both backends, close it
and start it once with `--use-angle=gl`.

The kit's `FD_MESA_DEBUG=noubwc` belongs on its own command line only. Left
in `~/.config/environment.d` and a gnome-shell drop-in after the
investigation, it made Chrome's GPU process fail on ANGLE/Vulkan ("Internal
Vulkan error (-8): A requested feature is not supported", then EGLImage and
native pixmap errors), and because the desktop user lingers, logging out did
not clear it. With both files removed and a reboot, the WebGL aquarium
rendered correctly on ANGLE/Vulkan. That test tablet runs Mesa 26.2.6 from
the kisak PPA, which the image builder does not install; the X710 has not been
re-checked on Ubuntu's own Mesa. `systemctl --user show-environment | grep
FD_MESA` shows whether the variable is still set.

## Reproduce

On Linux, run `python3 scripts/test-chromium-launchers.py` for the isolated
launcher tests. In the tablet's graphical session, use Node with native
WebSocket support:

```sh
node scripts/test-chromium-rendering.mjs wayland-gl /absolute/test-output
node scripts/test-chromium-rendering.mjs wayland-vulkan /absolute/test-output
```

The harness uses separate profiles and a synthetic fixture, retaining GPU
reports, screenshots and stderr. Inspect the screenshots as well as feature
status. The earlier identity harness explicitly selects GL so the new default
does not mask its original regression test. Personal browser profiles and
screenshots are excluded from the repository.

## Native application coverage and matching module, device 2.51

The port now discovers native Chromium/Electron applications from their system
`.desktop` entries. Known browser executables and adjacent Electron/Chromium
runtime files identify candidates. Generated overrides in
`/usr/local/share/applications` preserve original quoting, field codes, actions,
icons and desktop IDs. The shared runner selects hardware ANGLE/Vulkan and an
available Wayland socket, respecting explicit flags. It does nothing to driver
selection on other boards. D-Bus activation is disabled in generated entries
so launch requests actually pass through the corrected Exec command.

Package configure/file triggers and a boot/path service cover clean images and
new system application installations. Source updates regenerate overrides;
uninstallation removes them. Administrator-edited overrides remain untouched.
The existing Chrome/ChatGPT diversions continue to cover their terminal/PWA
launches. No polling timer, global Mesa override or background GPU process is
introduced by this discovery mechanism.

This covers detected native apps launched from the system desktop menu. It
cannot guarantee future Chromium versions never regress. Flatpak/Snap sandbox
exports, custom user launchers and arbitrary AppImages are not automatically
rewritten. For a compatible native Electron/AppImage executable outside that
coverage, a launcher can explicitly use:

```sh
/usr/libexec/ubuntu-gts9u-chromium-run /absolute/path/to/application
```

To exclude an app, add `X-GTS9U-Chromium-Defaults=false` to its source entry,
or keep a user-owned desktop entry with explicit rendering flags. User entries
have precedence. Applications must be fully closed and reopened to change the
GPU process. Existing windows are not forcibly restarted by discovery.

The scanner tests use a fresh filesystem tree and cover first installation,
new entries, vendor updates, action arguments, opt-out, unrelated applications,
manual edits and removal. They do not substitute for checking an individual
application's GPU report.

The earlier IRQ packaging failure came from mixing artifacts from different
kernel object trees. The IRQ bridge has now been rebuilt against kernel #11's
Module.symvers and signed with that same tree's certificate; its key matches
the module on the running tablet. The package builder accepts
`KERNEL_BUILD_DIR`, `KERNEL_OUT_DIR` and `SPSS_IRQ_OUT_DIR`, preserving its strict
certificate, configuration and kernel-release checks. A complete device 2.51
package was successfully built. Do not bypass those checks or reuse another
build's module just because the release string matches.

For a nondefault kernel build, first run `build-spss-irq-module.sh` with matching
`KERNEL_WORKTREE`, `KERNEL_BUILD_DIR` and `SPSS_IRQ_OUT_DIR`, then run
`build-device-package.sh` with the same object/module directories and the
corresponding released `KERNEL_OUT_DIR` (containing config and kernel.release).
The normal clean rootfs builder already builds the IRQ bridge immediately
before the device package with its common default tree.

On the SM-X710, run both scripts with `DEVICE=gts9wifi`: that selects
`build/linux-gts9wifi` and `out/kernel-gts9wifi` as the defaults and makes
`build-device-package.sh` rewrite the sensor-tree path. The default
`SPSS_IRQ_OUT_DIR` (`out/spss-irq-module`) is shared by both boards, so give
each board its own when building both. `build-device-package.sh` also needs
`scripts/build-fingerprint-secure-owner.sh` to have run first, and it runs
`chown -R root:root` on its staging tree, so run it as root.

Desktop precedence/Exec behavior follows the
[freedesktop desktop entry specification](https://specifications.freedesktop.org/desktop-entry/latest-single/).

Live discovery found seven entries covering Chrome, Chromium, ChatGPT, VS Code
(including its URL handler), and Claude. All generated entries passed
`desktop-file-validate` (only existing category hints). A fresh VS Code profile
launched through the shared runner rendered correctly and reported accelerated
canvas/compositing/rasterization/WebGL through Turnip ANGLE/Vulkan, with zero
Skia/overlap errors. Its CDP Browser.close request timed out after the screenshot;
the harness terminated that isolated process in its cleanup. No personal VS
Code profile or extensions were modified.

Completing package installation also required rebuilding the already-carried
camera relay version gts9u16. Launchpad Git returned HTTP 500, so the extra
package builder now has a checksum-pinned fallback to Ubuntu's official 0.1.2
source archive, followed by the same three port patches. Archive SHA256:
`270d64724ae0ec3ead9fd8c0d2f5a3d3c46efe51e63c96eb2fb7bdf78c105d28`.
The upstream archive is listed on [Ubuntu's source package page](https://launchpad.net/ubuntu/+source/v4l2-relayd/0.1.2-0ubuntu1).

Device 2.51 and relay gts9u16 are now installed through apt on the tablet,
with no broken-package audit findings. The installed IRQ module SHA256 is
`43531c964f0d21dda0e7d95370adaf2874986a2013344b6fcffff17477731461`;
its certificate key matches kernel #11's existing trusted module. The loaded
secure service was not unloaded. Camera and secure services remain active.
Both discovery units are enabled, and the path unit is active. A temporary,
hidden desktop entry was automatically configured and its override removed
again by the path watcher without manually running the scanner.

The legacy default object tree still differs from its old released config;
its package build correctly fails the configuration guard. The successful,
installed package uses the explicit matching kernel #11 paths above. No old
release configuration was relabelled to hide that distinction.
