// SPDX-License-Identifier: MIT
//
// Multi-finger taps on the touchscreen, as Tab Companion's Touch page sets
// them: a two-finger tap and a three-finger tap each do nothing, a right
// click or a middle click.
//
// GNOME has no touchscreen gesture for these clicks.  Shell sees every touch
// before the client does, in stage coordinates, so it can tell a short
// multi-finger tap from a pinch or a swipe and click between the fingers,
// whatever the screen's rotation.  The touches themselves still reach the
// client: a tap is only known once the last finger lifts, too late to take
// it back, and a short two- or three-finger touch does nothing in the usual
// applications.
import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const SCHEMA = 'io.github.agcarbajo.TabCompanion';
const KEYS = {2: 'touch-two-finger-tap', 3: 'touch-three-finger-tap'};
const BUTTONS = {
    'secondary-click': Clutter.BUTTON_SECONDARY,
    'middle-click': Clutter.BUTTON_MIDDLE,
};

// A tap, and nothing that real multi-touch does: every finger down within
// LAND_MS of the first and all of them down together, every finger up within
// LIFT_MS of the first to lift and all up within TAP_MS of the first landing,
// and none moved more than SLOP logical pixels from where it landed (about
// 4-5 mm at these screens' 200 % scale).  A pinch, a scroll, a rotation, a
// resting finger or a Shell swipe fails at least one of these; whatever
// fails is left exactly as the client received it.
const LAND_MS = 100;
const LIFT_MS = 150;
const TAP_MS = 800;
const SLOP = 24;

export default class TabCompanionTouch extends Extension {
    enable() {
        this._settings = new Gio.Settings({schema_id: SCHEMA});
        const seat = Clutter.get_default_backend().get_default_seat();
        this._pointer = seat.create_virtual_device(
            Clutter.InputDeviceType.POINTER_DEVICE);
        this._clickId = 0;
        this._reset();
        this._capturedId = global.stage.connect(
            'captured-event', this._event.bind(this));
    }

    disable() {
        if (this._capturedId) {
            global.stage.disconnect(this._capturedId);
            this._capturedId = 0;
        }
        if (this._clickId) {
            GLib.source_remove(this._clickId);
            this._clickId = 0;
        }
        this._pointer = null;
        this._settings = null;
        this._touches = null;
    }

    _reset() {
        this._touches = new Map();  // sequence -> where it landed
        this._points = [];          // every landing point of this touch
        this._start = 0;
        this._firstLift = 0;
        this._peak = 0;
        this._spoiled = false;
    }

    _event(_stage, event) {
        const type = event.type();
        if (type !== Clutter.EventType.TOUCH_BEGIN &&
            type !== Clutter.EventType.TOUCH_UPDATE &&
            type !== Clutter.EventType.TOUCH_END &&
            type !== Clutter.EventType.TOUCH_CANCEL)
            return Clutter.EVENT_PROPAGATE;

        const device = event.get_source_device?.() ?? event.get_device();
        if (device?.get_device_type() !== Clutter.InputDeviceType.TOUCHSCREEN_DEVICE)
            return Clutter.EVENT_PROPAGATE;

        const sequence = event.get_event_sequence();
        const [x, y] = event.get_coords();
        const time = event.get_time();

        if (type === Clutter.EventType.TOUCH_BEGIN) {
            if (this._touches.size === 0)
                this._reset();
            if (this._points.length === 0)
                this._start = time;
            else if (time - this._start > LAND_MS)
                this._spoiled = true;
            this._touches.set(sequence, {x, y});
            this._points.push({x, y});
            this._peak = Math.max(this._peak, this._touches.size);
            return Clutter.EVENT_PROPAGATE;
        }

        const landed = this._touches.get(sequence);
        if (!landed)
            return Clutter.EVENT_PROPAGATE;

        if (type === Clutter.EventType.TOUCH_UPDATE) {
            if (Math.hypot(x - landed.x, y - landed.y) > SLOP)
                this._spoiled = true;
            return Clutter.EVENT_PROPAGATE;
        }

        if (type === Clutter.EventType.TOUCH_CANCEL)
            this._spoiled = true;
        if (!this._firstLift)
            this._firstLift = time;
        else if (time - this._firstLift > LIFT_MS)
            this._spoiled = true;
        this._touches.delete(sequence);
        if (this._touches.size > 0)
            return Clutter.EVENT_PROPAGATE;

        const fingers = this._points.length;
        if (!this._spoiled && fingers === this._peak && fingers in KEYS &&
            time - this._start <= TAP_MS)
            this._tap(fingers);
        this._reset();
        return Clutter.EVENT_PROPAGATE;
    }

    _tap(fingers) {
        const button = BUTTONS[this._settings.get_string(KEYS[fingers])];
        if (!button || this._clickId)
            return;

        let x = 0, y = 0;
        for (const point of this._points) {
            x += point.x / this._points.length;
            y += point.y / this._points.length;
        }

        // After the client has had the last touch end.
        this._clickId = GLib.idle_add(GLib.PRIORITY_DEFAULT, () => {
            this._clickId = 0;
            if (!this._pointer)
                return GLib.SOURCE_REMOVE;
            const now = Clutter.CURRENT_TIME;
            this._pointer.notify_absolute_motion(now, x, y);
            this._pointer.notify_button(now, button, Clutter.ButtonState.PRESSED);
            this._pointer.notify_button(now, button, Clutter.ButtonState.RELEASED);
            return GLib.SOURCE_REMOVE;
        });
    }
}
