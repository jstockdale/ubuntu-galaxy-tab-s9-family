-- Installed into the GDM greeter's own ~/.config by ubuntu-gts9u-device
-- (/usr/libexec/ubuntu-gts9u-greeter-audio); never loaded for anyone else.
--
-- The login screen needs no Bluetooth audio.  Left on, the greeter's bluez
-- monitor registers the A2DP endpoints and the HFP/HSP profiles first, and the
-- owner's WirePlumber, started beside it at boot by lingering, gets
-- "RegisterProfile() failed: NotPermitted" until the greeter lets go.  ALSA
-- stays on, so login sounds and the screen reader keep working.
--
-- WirePlumber 0.4 (Ubuntu 24.04) merges this directory with the system one by
-- file name: this runs after 50-bluez-config.lua and before 90-enable-all.lua,
-- whose enable() returns early for a monitor whose 'enabled' is false.  The
-- nil checks keep a missing system file from turning into a Lua error.
if bluez_monitor ~= nil then
  bluez_monitor.enabled = false
end
if bluez_midi_monitor ~= nil then
  bluez_midi_monitor.enabled = false
end
