"""Medius Bridge - VisionLabs API v7 Python plugin.

Turns a Medius USB passthrough box into the mouse input for VisionLabs.

Two output sources (setting `output_source`):

  * "kmboxnet" (default): the plugin emulates a kmbox-net box on UDP
    127.0.0.1:8808.  VisionLabs' own KMBoxNet mouse-output backend is pointed
    at it, so VisionLabs' native tracking drives the Medius box unchanged, and
    the kmbox "monitor" stream reports the real mouse's buttons/motion back.
  * "detections": the plugin computes aim itself from the detections stream
    and gates it on a physical mouse button read from the box.

Threads
-------
  VisionLabs runner thread   on_detections / on_tracking / on_ui_event: store
                             the latest target, return immediately.
  link thread                connects to the box, reconnects on failure, and
                             runs either the kmbox server or the send loop.
  kmbox rx/monitor threads   (kmboxnet mode) UDP server + monitor stream.
  input thread               reads dev.input_events() for physical buttons/motion.

Nothing here blocks the VisionLabs callbacks, and nothing is held on the box:
if the plugin dies the box's silence timeout drops to plain passthrough.
"""
from __future__ import annotations

import math
import threading
import time
from typing import Any, Optional

from visionlabs import get_setting, load_settings, log, overlay, has_permission

import kmboxnet  # local module: kmbox-net UDP emulator

try:  # medius is installed into the plugin's managed environment via requirements.txt
    import medius
    from medius import (Device, Usage, Button, CatchFilter, InputKind, MediusError, NotFoundError,
                        LockTarget, Direction)
    _MEDIUS_IMPORT_ERROR: Optional[BaseException] = None
except Exception as exc:  # pragma: no cover - reported through the log at start
    medius = None
    Device = Usage = Button = CatchFilter = InputKind = LockTarget = Direction = None
    MediusError = NotFoundError = Exception
    _MEDIUS_IMPORT_ERROR = exc

_BUTTONS = {
    "left": 0, "right": 1, "middle": 2, "side1": 3, "side2": 4,
}

# Firmware 3.4.1 uses protocol 8 at 6 Mbaud. The `medius` package opens the port at the rate its
# own version expects, so the library and firmware must be from the same generation.
_MIN_LIB = (3, 4, 1)
_EXPECTED_PROTO = 8


def _lib_version() -> tuple:
    try:
        return tuple(int(p) for p in medius.version_string().split(".")[:3])
    except Exception:
        return (0, 0, 0)


def _lib_too_old() -> bool:
    return _lib_version() < _MIN_LIB

# --------------------------------------------------------------------------
# Settings snapshot (re-read on every UI event, cheap dict reads elsewhere)
# --------------------------------------------------------------------------
class _Settings:
    def __init__(self) -> None:
        self.refresh()

    def refresh(self) -> None:
        s = load_settings()

        def f(key, default):
            try:
                return float(s.get(key, default))
            except (TypeError, ValueError):
                return float(default)

        self.enabled = bool(s.get("enabled", True))
        self.port = str(s.get("port", "") or "").strip()
        # "kmboxnet": VisionLabs' native KMBoxNet output backend drives the box (recommended)
        # "detections": this plugin computes aim from detections itself
        self.output_source = str(s.get("output_source", "detections") or "detections").lower()
        # steer straight from VisionLabs' tracking target (aimX/aimY); detections only give the
        # frame size, the overlay and a fallback when tracking has no target
        self.steer_from_tracking = bool(s.get("steer_from_tracking", True))
        self.frame_width = int(f("frame_width", 0))
        self.frame_height = int(f("frame_height", 0))
        self.kmbox_ip = str(s.get("kmbox_ip", "127.0.0.1") or "127.0.0.1").strip()
        self.kmbox_port = int(f("kmbox_port", 8808))
        self.kmbox_verbose = bool(s.get("kmbox_verbose", False))
        self.monitor_hz = f("monitor_hz", 250.0)
        self.exact_timing = bool(s.get("exact_timing", False))
        self.disable_riding = bool(s.get("disable_riding", True))
        # motion curve
        self.ease_enabled = bool(s.get("ease_enabled", True))
        self.ease_radius = max(5.0, f("ease_radius", 60.0))
        self.ease_floor = min(1.0, max(0.05, f("ease_floor", 0.3)))
        self.max_speed_px_s = max(100.0, f("max_speed_px_s", 6000.0))
        # Medius renderer options (pushed to the box)
        self.render_mode = str(s.get("render_mode", "despiked") or "despiked").lower()
        self.render_full = bool(s.get("render_full", False))
        self.spread_pct = int(min(200, max(0, f("spread_pct", 100))))
        self.activation_mode = str(s.get("activation_mode", "hold") or "hold").lower()
        self.activation_button = str(s.get("activation_button", "right") or "right").lower()
        self.min_confidence = f("min_confidence", 0.50)
        self.class_filter = {
            c.strip().lower() for c in str(s.get("class_filter", "") or "").split(",") if c.strip()
        }
        self.prefer_tracking_target = bool(s.get("prefer_tracking_target", True))
        self.use_vl_aim_point = bool(s.get("use_vl_aim_point", True))
        self.aim_point = f("aim_point", 20.0) / 100.0
        self.fov_radius = f("fov_radius", 300.0)
        self.gain = min(1.0, max(0.02, f("gain", 0.25)))
        self.mouse_scale = max(0.05, f("mouse_scale", 1.0))
        self.max_step = int(min(127, max(1, f("max_step", 30))))
        self.deadzone = f("deadzone", 2.0)
        self.send_hz = min(500.0, max(30.0, f("send_hz", 240.0)))
        self.invert_y = bool(s.get("invert_y", False))
        self.show_overlay = bool(s.get("show_overlay", True))
        self.log_tracking = bool(s.get("log_tracking", False))
        self.target_timeout_s = min(1.0, max(0.02, f("target_timeout_ms", 120.0) / 1000.0))


_settings = _Settings()

# --------------------------------------------------------------------------
# Shared state
# --------------------------------------------------------------------------
_lock = threading.Lock()
_stop = threading.Event()

# latest aim error in *frame pixels*: how far the crosshair must move (+x right, +y down)
_target_err: Optional[tuple[float, float]] = None
_target_ts: float = 0.0
_target_info: dict[str, Any] = {}

# last known frame size (from the detections stream, or the Advanced override)
_frame_size: tuple[int, int] = (0, 0)
_tracking_drove_ts: float = 0.0   # when on_tracking last set the target itself
_diag_tracking_noframe_logged = False

# aim diagnostics (plugin-aim mode): reset every report period by _send_loop
_diag = {"batches": 0, "dets": 0, "no_frame": 0, "low_conf": 0, "class": 0, "fov": 0,
         "chosen": 0, "moves": 0, "counts": 0, "skip_inactive": 0, "skip_stale": 0,
         "skip_none": 0, "skip_deadzone": 0, "frame": (0, 0), "track_hits": 0, "track_noframe": 0}

# tracking.state cache
_tracking: dict[str, Any] = {}
_last_tracking_log = 0.0

# activation
_button_held = False
_toggled_on = False

_dev = None            # medius.Device owned by the link thread
_dev_lock = threading.Lock()   # serialises fire-and-forget calls from the kmbox and send threads
_link_thread: Optional[threading.Thread] = None
_input_thread: Optional[threading.Thread] = None
_connected = False
_kmbox: Optional[kmboxnet.KmboxNetServer] = None


class _MediusSink:
    """Adapts kmbox-net commands onto a medius.Device."""

    _BTN_MAP = None  # filled lazily once medius has imported

    def __init__(self, dev) -> None:
        self.dev = dev
        self.held = 0
        self.moves = 0
        if _MediusSink._BTN_MAP is None:
            _MediusSink._BTN_MAP = [
                (kmboxnet.BTN_LEFT, Button.LEFT), (kmboxnet.BTN_RIGHT, Button.RIGHT),
                (kmboxnet.BTN_MIDDLE, Button.MIDDLE), (kmboxnet.BTN_SIDE1, Button.SIDE1),
                (kmboxnet.BTN_SIDE2, Button.SIDE2),
            ]

    def move(self, dx: int, dy: int) -> None:
        self.moves += 1
        if self.moves <= 5:
            _safe_log(f"kmbox move #{self.moves}: dx={dx} dy={dy} -> Medius")
        with _dev_lock:
            if _settings.exact_timing:
                self.dev.move_rel_now(int(dx), int(dy))   # bypass riding/spread/render
            else:
                self.dev.move_rel(int(dx), int(dy))

    def wheel(self, delta: int) -> None:
        with _dev_lock:
            self.dev.wheel(int(delta))

    def buttons(self, bits: int) -> None:
        changed = bits ^ self.held
        if not changed:
            return
        with _dev_lock:
            for bit, btn in self._BTN_MAP:
                if changed & bit:
                    if bits & bit:
                        self.dev.press(Usage.button(btn))
                    else:
                        self.dev.soft_release(Usage.button(btn))
        self.held = bits

    def mask(self, bits: int) -> None:
        """kmbox 'mask' = block the user's physical input; Medius calls that a lock."""
        table = [
            (kmboxnet.MASK_LEFT, LockTarget.button(Button.LEFT)),
            (kmboxnet.MASK_RIGHT, LockTarget.button(Button.RIGHT)),
            (kmboxnet.MASK_MIDDLE, LockTarget.button(Button.MIDDLE)),
            (kmboxnet.MASK_SIDE1, LockTarget.button(Button.SIDE1)),
            (kmboxnet.MASK_SIDE2, LockTarget.button(Button.SIDE2)),
            (kmboxnet.MASK_WHEEL, LockTarget.wheel()),
            (kmboxnet.MASK_X, LockTarget.x()),
            (kmboxnet.MASK_Y, LockTarget.y()),
        ]
        with _dev_lock:
            for bit, target in table:
                if bits & bit:
                    self.dev.lock(target, Direction.BOTH)
                else:
                    self.dev.unlock(target, Direction.BOTH)

    def unmask_all(self) -> None:
        self.mask(0)

    def reset(self) -> None:
        self.held = 0
        with _dev_lock:
            try:
                self.dev.reset()
            except Exception:
                pass


def _safe_log(message: str) -> None:
    try:
        log(message)
    except Exception:
        print(message)


def _is_active() -> bool:
    if not _settings.enabled:
        return False
    mode = _settings.activation_mode
    if mode == "always":
        return True
    if mode == "toggle":
        return _toggled_on
    return _button_held  # hold


# --------------------------------------------------------------------------
# VisionLabs callbacks (keep these short)
# --------------------------------------------------------------------------
def on_start():
    if _MEDIUS_IMPORT_ERROR is not None:
        _safe_log(f"Medius Bridge: python package 'medius' failed to import: {_MEDIUS_IMPORT_ERROR!r}")
        return
    _safe_log(f"Medius Bridge starting (medius lib {getattr(medius, 'version_string', lambda: '?')()}, "
              f"abi {getattr(medius, 'abi_version', lambda: '?')()})")
    if _lib_too_old():
        _safe_log("Medius Bridge: the installed 'medius' package is older than 3.4.1 and speaks an older "
                  "protocol; a box on firmware 3.4.1 answers on protocol 8 at 6 Mbaud. VisionLabs did not "
                  "rebuild the plugin environment - delete <VisionLabs>\\PythonEnvs\\com_staybeaming_medius_bridge "
                  "and restart VisionLabs, or pip-install the matching 3.4.1 wheel from the plugin folder into that "
                  "environment's python.exe (see README, Upgrading).")
    _start_threads()


def on_ui_event(event):
    global _toggled_on
    _settings.refresh()
    control = str(event.get("controlId", ""))
    if control == "test_nudge":
        _test_nudge()
        return
    if control == "log_state":
        if _dev is not None:
            threading.Thread(target=_log_box_state, args=(_dev,), daemon=True).start()
        server = _kmbox
        if server is not None:
            who = f"{server.client[0]}:{server.client[1]}" if server.client else "NO CLIENT - VisionLabs has not connected"
            _safe_log(f"kmbox: {who}; {server.packets} packets total, last {server.last_cmd or '-'}")
        if _settings.output_source != "kmboxnet":
            _report_aim_diag(time.monotonic())
        return
    if control in ("render_mode", "render_full", "spread_pct"):
        if _dev is not None:
            threading.Thread(target=_apply_box_options, args=(_dev,), daemon=True).start()
        return
    if control in ("port", "enabled", "output_source", "kmbox_ip", "kmbox_port", "monitor_hz"):
        # force a reconnect / release on the link thread
        _request_reconnect()
    if control == "activation_mode":
        _toggled_on = False


def on_tracking(state):
    """Cache VisionLabs' tracking state; used to prefer its chosen track ID."""
    global _tracking, _last_tracking_log, _target_err, _target_ts, _target_info, _tracking_drove_ts
    global _diag_tracking_noframe_logged
    _tracking = state or {}
    if _settings.steer_from_tracking and _tracking.get("hasTarget"):
        try:
            vx = float(_tracking.get("aimX", 0) or 0)
            vy = float(_tracking.get("aimY", 0) or 0)
        except (TypeError, ValueError):
            vx = vy = 0.0
        fw, fh = _current_frame_size()
        if vx > 0 and vy > 0 and fw > 0 and fh > 0:
            now = time.monotonic()
            with _lock:
                _target_err = (vx - fw / 2.0, vy - fh / 2.0)
                _target_ts = now
                _tracking_drove_ts = now
                _target_info = {**_target_info, "ax": vx, "ay": vy,
                                "track": _tracking.get("targetTrackId"), "src": "tracking"}
            _diag["track_hits"] += 1
        elif vx > 0 and vy > 0:
            _diag["track_noframe"] += 1
            if not _diag_tracking_noframe_logged:
                _diag_tracking_noframe_logged = True
                _safe_log("Medius Bridge: VisionLabs has a target but the frame size is unknown yet - "
                          "waiting for the first detections batch, or set Advanced > Frame width/height")
    if _settings.log_tracking:
        now = time.monotonic()
        if now - _last_tracking_log > 1.0:
            _last_tracking_log = now
            _safe_log(
                "tracking running={running} hasTarget={hasTarget} track={targetTrackId} "
                "aimX={aimX} aimY={aimY} profile={profileName}".format(
                    **{k: _tracking.get(k) for k in
                       ("running", "hasTarget", "targetTrackId", "aimX", "aimY", "profileName")}))


def _current_frame_size() -> tuple[int, int]:
    if _settings.frame_width > 0 and _settings.frame_height > 0:
        return _settings.frame_width, _settings.frame_height
    return _frame_size


def on_detections(batch):
    """Pick a target and store the pixel error for the send loop."""
    global _target_err, _target_ts, _target_info, _frame_size
    frame_w = float(batch.get("frameWidth", 0) or 0)
    frame_h = float(batch.get("frameHeight", 0) or 0)
    _diag["batches"] += 1
    if frame_w <= 0 or frame_h <= 0:
        _diag["no_frame"] += 1
        return
    _diag["frame"] = (int(frame_w), int(frame_h))
    _frame_size = (int(frame_w), int(frame_h))
    cx, cy = frame_w / 2.0, frame_h / 2.0

    preferred_track = None
    if _settings.prefer_tracking_target and _tracking.get("hasTarget"):
        preferred_track = _tracking.get("targetTrackId")

    best = None
    best_dist = float("inf")
    for det in batch.get("detections", []) or []:
        _diag["dets"] += 1
        if float(det.get("confidence", 0.0)) < _settings.min_confidence:
            _diag["low_conf"] += 1
            continue
        if _settings.class_filter:
            name = str(det.get("className", "") or "").lower()
            if name not in _settings.class_filter:
                _diag["class"] += 1
                continue
        b = det.get("bounds", {}) or {}
        x, y = float(b.get("x", 0)), float(b.get("y", 0))
        w, h = float(b.get("width", 0)), float(b.get("height", 0))
        if w <= 0 or h <= 0:
            continue
        ax = x + w / 2.0
        ay = y + h * _settings.aim_point
        dist = math.hypot(ax - cx, ay - cy)
        if dist > _settings.fov_radius:
            _diag["fov"] += 1
            continue
        track = det.get("trackId")
        is_preferred = preferred_track is not None and track == preferred_track
        selected = bool(det.get("selected", False))
        # ranking: VisionLabs' own target first, then selected flag, then nearest to centre
        score = dist - (1e6 if is_preferred else 0) - (5e5 if selected else 0)
        if score < best_dist:
            best_dist = score
            best = (ax, ay, x, y, w, h, track)

    tracking_drives = (_settings.steer_from_tracking
                       and (time.monotonic() - _tracking_drove_ts) <= _settings.target_timeout_s)
    with _lock:
        if tracking_drives:
            pass  # on_tracking owns the target right now; keep detections for overlay/frame size only
        elif best is None:
            _target_err = None
            _target_info = {}
        else:
            ax, ay, x, y, w, h, track = best
            # VisionLabs' own aim point (its head offset / lead / profile logic), in frame pixels
            if (_settings.use_vl_aim_point and preferred_track is not None and track == preferred_track):
                vx = float(_tracking.get("aimX", 0) or 0)
                vy = float(_tracking.get("aimY", 0) or 0)
                if vx > 0 and vy > 0:
                    ax, ay = vx, vy
            _target_err = (ax - cx, ay - cy)
            _target_ts = time.monotonic()
            _diag["chosen"] += 1
            _target_info = {"x": x, "y": y, "w": w, "h": h, "ax": ax, "ay": ay, "track": track}

    if _settings.show_overlay and has_permission("overlay.draw"):
        _draw_overlay(best, frame_w, frame_h)


def on_stop():
    _stop.set()
    for t in (_link_thread, _input_thread):
        if t is not None and t.is_alive():
            t.join(timeout=2.0)


# --------------------------------------------------------------------------
# Overlay
# --------------------------------------------------------------------------
def _draw_overlay(best, frame_w, frame_h):
    items = []
    if best is not None:
        ax, ay, x, y, w, h, track = best
        colour = "#FF40FF40" if _is_active() else "#FFFFC040"
        items.append({"id": "mb-box", "kind": "rectangle",
                      "bounds": {"x": x, "y": y, "width": w, "height": h},
                      "foreground": colour, "thickness": 2, "durationMs": 120})
        items.append({"id": "mb-aim", "kind": "ellipse",
                      "bounds": {"x": ax - 4, "y": ay - 4, "width": 8, "height": 8},
                      "foreground": colour, "fill": colour, "thickness": 1, "durationMs": 120})
        items.append({"id": "mb-line", "kind": "line",
                      "points": [{"x": frame_w / 2, "y": frame_h / 2}, {"x": ax, "y": ay}],
                      "foreground": colour, "thickness": 1, "opacity": 0.6, "durationMs": 120})
    link = "link ok" if _connected else "no box"
    server = _kmbox
    if _settings.output_source == "kmboxnet":
        if server is None:
            state = "kmbox: not listening"
        elif server.client is None:
            state = f"kmbox: waiting for VisionLabs on {server.listen_ip}:{server.port}"
        else:
            state = f"kmbox: {server.packets} pkts, {server.last_cmd}"
    else:
        state = "plugin aim ACTIVE" if _is_active() else "plugin aim idle"
    items.append({"id": "mb-status", "kind": "text", "text": f"Medius {link} | {state}",
                  "bounds": {"x": 8, "y": 8, "width": 0, "height": 0},
                  "foreground": "#FFFFFFFF", "fontSize": 13, "durationMs": 120})
    try:
        overlay.draw(items, clear_previous=True, coordinate_space="frame",
                     source_width=int(frame_w), source_height=int(frame_h))
    except Exception:
        pass


# --------------------------------------------------------------------------
# Medius link thread: connect, reconnect, send loop
# --------------------------------------------------------------------------
_reconnect = threading.Event()


def _request_reconnect() -> None:
    _reconnect.set()


def _start_threads() -> None:
    global _link_thread
    _stop.clear()
    _link_thread = threading.Thread(target=_link_main, name="medius-link", daemon=True)
    _link_thread.start()


def _open_device():
    port = _settings.port
    if port and port.lower() != "auto":
        return Device.open(port)
    return Device.find()


def _link_main() -> None:
    global _dev, _connected, _input_thread
    backoff = 1.0
    while not _stop.is_set():
        if not _settings.enabled:
            time.sleep(0.25)
            continue
        try:
            dev = _open_device()
        except NotFoundError:
            _connected = False
            _safe_log("Medius Bridge: no box found - check the USB2 control cable (waiting)")
            _stop.wait(backoff)
            backoff = min(10.0, backoff * 1.5)
            continue
        except MediusError as exc:
            _connected = False
            status = getattr(exc, "status", None)
            sname = getattr(status, "name", str(status))
            if "PROTO" in sname.upper() or "NO_REPLY" in sname.upper() or "TIMEOUT" in sname.upper():
                # An older library on a 3.4.1 box (or the reverse) lands here: wrong baud, wrong
                # protocol byte, or no answer at all. Say so instead of retrying silently.
                _safe_log(f"Medius Bridge: open failed: {sname} {exc.message} - this is what a library/firmware "
                          f"mismatch looks like (lib {'.'.join(map(str, _lib_version()))} wants proto "
                          f"{_EXPECTED_PROTO}). Box on fw 3.4.1 needs medius 3.4.1; "
                          f"see the log line at start-up for how to rebuild the environment. (waiting)")
            else:
                _safe_log(f"Medius Bridge: open failed: {sname} {exc.message} (waiting)")
            _stop.wait(backoff)
            backoff = min(10.0, backoff * 1.5)
            continue

        backoff = 1.0
        try:
            v = dev.query_version()
            info = dev.device_info()
            _safe_log(f"Medius Bridge: connected fw {v.fw_major}.{v.fw_minor}.{v.fw_patch} "
                      f"proto {v.proto_ver} '{v.name}' cloning {info.product or 'device'} "
                      f"(kind {getattr(info.kind, 'name', info.kind)}) via medius lib "
                      f"{'.'.join(map(str, _lib_version()))}")
            if v.proto_ver != _EXPECTED_PROTO:
                _safe_log(f"Medius Bridge: box speaks proto {v.proto_ver}, this plugin was built for proto "
                      f"{_EXPECTED_PROTO} (fw 3.4.1). Update the box at medius.k4tech.net/dashboard/update "
                          f"or pin an older medius in requirements.txt.")
        except MediusError as exc:
            _safe_log(f"Medius Bridge: handshake query failed: {exc.message}")
        _log_box_state(dev)
        _apply_box_options(dev)

        _dev = dev
        _connected = True
        _reconnect.clear()

        # Physical-input listener on its own handle to the same link.
        input_stop = threading.Event()
        _input_thread = threading.Thread(target=_input_main, args=(dev.clone(), input_stop),
                                         name="medius-input", daemon=True)
        _input_thread.start()

        try:
            if _settings.output_source == "kmboxnet":
                _kmbox_loop(dev)
            else:
                _send_loop(dev)
        except MediusError as exc:
            _safe_log(f"Medius Bridge: link error: {exc.message} - reconnecting")
        except Exception as exc:  # never let the link thread die silently
            _safe_log(f"Medius Bridge: output loop crashed: {exc!r} - reconnecting")
        finally:
            input_stop.set()
            _stop_kmbox()
            _connected = False
            try:
                dev.reset()       # drop any pending injected motion
            except Exception:
                pass
            try:
                dev.close()
            except Exception:
                pass
            _dev = None
            if _input_thread is not None:
                _input_thread.join(timeout=1.0)


def _apply_box_options(dev) -> None:
    """Push renderer/spread settings to the box (persisted in its NVS)."""
    try:
        mode = {
            "off": medius.RenderMode.OFF, "stock": medius.RenderMode.STOCK,
            "despiked": medius.RenderMode.DESPIKED, "unsmoothed": medius.RenderMode.UNSMOOTHED,
        }.get(_settings.render_mode, medius.RenderMode.DESPIKED)
        with _dev_lock:
            dev.set_render(mode, bool(_settings.render_full))
            dev.set_spread(int(_settings.spread_pct))
        _safe_log(f"Medius options applied: render={_settings.render_mode} full={_settings.render_full} "
                  f"spread={_settings.spread_pct}%")
    except Exception as exc:
        _safe_log(f"Medius options apply failed: {exc!r}")


def _log_box_state(dev) -> None:
    """One-line health/option summary so a 'nothing moves' report can be diagnosed from the log."""
    try:
        h = dev.query_health()
        extra = " ".join(f"{k}={getattr(h, k)}" for k in ("rate_confident", "transform_on", "rewrite_on", "patch_on")
                         if hasattr(h, k))  # fields added by fw 3.4.1 (16-bit health word)
        _safe_log(f"Medius health: link={h.link_up} mouse={h.mouse_attached} clone_configured={h.clone_configured} "
                  f"injection_active={h.injection_active} lock_on={h.lock_on} catch_on={h.catch_on} {extra}")
        if not h.clone_configured:
            _safe_log("Medius: clone_configured=False - the game PC has not enumerated the box on USB1; "
                      "nothing injected can reach it until it does")
    except Exception as exc:
        _safe_log(f"Medius health query failed: {exc!r}")
    try:
        riding = dev.query_movement_riding()
        if riding is not None and _settings.disable_riding:
            dev.set_movement_riding(None)
            _safe_log(f"Medius: movement riding was ON ({riding} ms) - turned OFF so idle injection works")
        else:
            _safe_log(f"Medius: movement riding {'off' if riding is None else f'{riding} ms'}")
    except Exception as exc:
        _safe_log(f"Medius riding query failed: {exc!r}")
    try:
        r = dev.query_render()
        sp = dev.query_spread()
        _safe_log(f"Medius render: mode={getattr(r.mode, 'name', r.mode)} full={r.full} ready={r.ready}; "
                  f"spread {sp.percent}% over {sp.span_us} us")
    except Exception as exc:
        _safe_log(f"Medius render/spread query failed: {exc!r}")


_nudge_dir = 1


def _test_nudge() -> None:
    """UI button: move the game-PC cursor 120 counts, alternating right/left on each press."""
    global _nudge_dir
    dev = _dev
    if dev is None:
        _safe_log("Test nudge: no Medius link")
        return
    direction = _nudge_dir
    _nudge_dir = -_nudge_dir

    def run():
        try:
            # 6 steps of 20 counts, 10 ms apart: a visible 120-count sweep rather than a blink
            for _ in range(6):
                with _dev_lock:
                    dev.move_rel_now(20 * direction, 0)
                time.sleep(0.01)
            _safe_log(f"Test nudge sent: 120 counts {'RIGHT' if direction > 0 else 'LEFT'} via move_rel_now")
            _log_box_state(dev)
        except Exception as exc:
            _safe_log(f"Test nudge failed: {exc!r}")
    threading.Thread(target=run, name="medius-nudge", daemon=True).start()


def _stop_kmbox() -> None:
    global _kmbox
    server, _kmbox = _kmbox, None
    if server is not None:
        server.stop()


def _kmbox_loop(dev) -> None:
    """Run the kmbox-net emulator until stop/reconnect; VisionLabs drives the box."""
    global _kmbox
    server = kmboxnet.KmboxNetServer(
        _MediusSink(dev), _settings.kmbox_ip, _settings.kmbox_port,
        log=_safe_log, verbose=_settings.kmbox_verbose, monitor_hz=_settings.monitor_hz)
    try:
        server.start()
    except OSError as exc:
        _safe_log(f"Medius Bridge: cannot bind {_settings.kmbox_ip}:{_settings.kmbox_port} - {exc}. "
                  "Is another kmbox emulator or VisionLabs instance using it?")
        _stop.wait(3.0)
        return
    _kmbox = server
    shown_ip = "127.0.0.1" if _settings.kmbox_ip in ("0.0.0.0", "") else _settings.kmbox_ip
    _safe_log("Medius Bridge: KMBoxNet mode - set VisionLabs mouse output to KMBoxNet, "
              f"IP {shown_ip}, port {_settings.kmbox_port} (any UUID)"
              + (f" [listening on all interfaces]" if shown_ip != _settings.kmbox_ip else ""))
    last_report = time.monotonic()
    last_packets = 0
    while not _stop.is_set() and not _reconnect.is_set():
        _stop.wait(0.5)
        server.verbose = _settings.kmbox_verbose
        now = time.monotonic()
        if now - last_report >= 10.0:
            rate = (server.packets - last_packets) / (now - last_report)
            last_packets, last_report = server.packets, now
            who = f"{server.client[0]}:{server.client[1]}" if server.client else "no client yet"
            _safe_log(f"Medius Bridge: kmbox {who}, {rate:.0f} pkt/s, last {server.last_cmd or '-'}, "
                      f"unknown {server.unknown}")


def _report_aim_diag(now: float) -> None:
    """Every 10 s in plugin-aim mode: what came in, what was filtered, what went out."""
    d = dict(_diag)
    for k in _diag:
        if k != "frame":
            _diag[k] = 0
    with _lock:
        err = _target_err
        info = dict(_target_info)
        age_ms = (now - _target_ts) * 1000.0 if _target_ts else None
    if d["batches"] == 0 and d["track_hits"] == 0:
        why = "NO DETECTION BATCHES and no tracking target from VisionLabs - is the loop running?"
    elif d["track_noframe"] and d["track_hits"] == 0:
        why = "tracking target seen but frame size unknown (no detections batch yet) - set Advanced > Frame width/height"
    elif d["chosen"] == 0 and d["track_hits"] == 0 and d["dets"] == 0:
        why = "batches arrive but contain no detections, and tracking has no target"
    elif d["chosen"] == 0 and d["track_hits"] == 0:
        why = (f"every detection filtered out: {d['low_conf']} below confidence, "
               f"{d['class']} wrong class, {d['fov']} outside FOV")
    elif d["moves"] == 0:
        if d["skip_inactive"] and not d["skip_stale"]:
            why = "target found but aim not activated (button not held / toggle off)"
        elif d["skip_stale"]:
            why = "target found but stale by the time the send loop ran (detections slower than target-hold)"
        elif d["skip_deadzone"]:
            why = "target inside deadzone - nothing to do"
        else:
            why = "no moves sent"
    else:
        why = "OK"
    _safe_log(
        f"Medius aim: frame={d['frame'][0]}x{d['frame'][1]} batches={d['batches']} dets={d['dets']} "
        f"(lowconf={d['low_conf']} class={d['class']} fov={d['fov']}) chosen={d['chosen']} "
        f"tracking_target={d['track_hits']} | "
        f"active={_is_active()} err={None if err is None else (round(err[0]), round(err[1]))} "
        f"age={None if age_ms is None else round(age_ms)}ms track={info.get('track')} src={info.get('src', 'detections')} | "
        f"moves={d['moves']} counts={d['counts']} | {why}")


def _send_loop(dev) -> None:
    """Chase the latest pixel error with proportional relative moves."""
    global _target_err
    carry_x = carry_y = 0.0          # sub-count remainder so slow drifts still land
    next_t = time.monotonic()
    last_report = time.monotonic()
    _safe_log("Medius Bridge: plugin-aim mode - "
              + ("steering from VisionLabs' tracking target (detections as fallback); "
                 if _settings.steer_from_tracking else "steering from the detections stream; ")
              + f"activation={_settings.activation_mode}/{_settings.activation_button} "
              f"conf>={_settings.min_confidence:.2f} fov={_settings.fov_radius:.0f}px "
              f"classes={sorted(_settings.class_filter) or 'any'} gain={_settings.gain:.2f} "
              f"scale={_settings.mouse_scale:.2f} step<={_settings.max_step} dz={_settings.deadzone:.0f} "
              f"{_settings.send_hz:.0f}Hz invertY={_settings.invert_y}")
    while not _stop.is_set() and not _reconnect.is_set():
        period = 1.0 / _settings.send_hz
        next_t += period
        now = time.monotonic()
        if now - last_report >= 10.0:
            last_report = now
            _report_aim_diag(now)
        with _lock:
            err = _target_err
            ts = _target_ts
        active = _is_active()
        if err is None:
            _diag["skip_none"] += 1
        elif not active:
            _diag["skip_inactive"] += 1
        elif (time.monotonic() - ts) > _settings.target_timeout_s:
            _diag["skip_stale"] += 1
        if err is not None and active and (time.monotonic() - ts) <= _settings.target_timeout_s:
            ex, ey = err
            dist = math.hypot(ex, ey)
            if dist > _settings.deadzone:
                # proportional step, then an ease-out taper near the target and a speed cap
                gain = _settings.gain
                if _settings.ease_enabled and dist < _settings.ease_radius:
                    gain *= max(_settings.ease_floor, dist / _settings.ease_radius)
                step_px = dist * gain
                cap_px = _settings.max_speed_px_s * period
                if step_px > cap_px:
                    step_px = cap_px
                ux, uy = ex / dist, ey / dist
                # pixels -> mouse counts
                step_x = (ux * step_px) / _settings.mouse_scale + carry_x
                step_y = (uy * step_px) / _settings.mouse_scale + carry_y
                if _settings.invert_y:
                    step_y = -step_y
                dx = int(max(-_settings.max_step, min(_settings.max_step, round(step_x))))
                dy = int(max(-_settings.max_step, min(_settings.max_step, round(step_y))))
                carry_x = step_x - dx
                carry_y = step_y - dy
                if dx or dy:
                    with _dev_lock:
                        if _settings.exact_timing:
                            dev.move_rel_now(dx, dy)
                        else:
                            dev.move_rel(dx, dy)
                    _diag["moves"] += 1
                    _diag["counts"] += abs(dx) + abs(dy)
                    # assume the move lands; pull the stored error towards zero so we do not
                    # double-send before the next detection arrives
                    with _lock:
                        if _target_err is not None:
                            sx = dx * _settings.mouse_scale
                            sy = (-dy if _settings.invert_y else dy) * _settings.mouse_scale
                            _target_err = (_target_err[0] - sx, _target_err[1] - sy)
            else:
                _diag["skip_deadzone"] += 1
                carry_x = carry_y = 0.0
        else:
            carry_x = carry_y = 0.0
        delay = next_t - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        else:
            next_t = time.monotonic()


# --------------------------------------------------------------------------
# Medius input thread: physical mouse buttons -> activation
# --------------------------------------------------------------------------
def _input_main(dev, input_stop: threading.Event) -> None:
    global _button_held, _toggled_on
    try:
        filters = [CatchFilter.watch_class(medius.Class.BUTTON), CatchFilter.watch_axes()]
        with dev.input_events(filters) as stream:
            while not input_stop.is_set() and not _stop.is_set():
                ev = stream.recv_timeout(50)
                if ev is None:
                    continue
                server = _kmbox
                if ev.kind == InputKind.MOTION:
                    if server is not None:
                        server.add_hw_motion(ev.dx, ev.dy, ev.dz)
                    continue
                if ev.usage is None or ev.usage.kind != medius.Class.BUTTON:
                    continue
                if server is not None:  # feed the kmbox "monitor" stream with real button state
                    server.set_hw_button(1 << int(ev.usage.id), ev.is_press)
                wanted = _BUTTONS.get(_settings.activation_button, 1)
                if int(ev.usage.id) != wanted:
                    continue
                if ev.is_press:
                    _button_held = True
                    if _settings.activation_mode == "toggle":
                        _toggled_on = not _toggled_on
                        _safe_log(f"Medius Bridge: aim {'ON' if _toggled_on else 'OFF'}")
                    elif _settings.activation_mode == "hold":
                        _safe_log(f"Medius Bridge: {_settings.activation_button} button held -> aim ON")
                elif ev.is_release:
                    _button_held = False
                    if _settings.activation_mode == "hold":
                        _safe_log(f"Medius Bridge: {_settings.activation_button} button released -> aim OFF")
    except MediusError as exc:
        _safe_log(f"Medius Bridge: input stream ended: {exc.message}")
    except Exception as exc:
        _safe_log(f"Medius Bridge: input thread error: {exc!r}")
    finally:
        _button_held = False
        try:
            dev.close()
        except Exception:
            pass
