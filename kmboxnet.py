"""kmbox-net (kmNet) protocol emulator.

VisionLabs' KMBoxNet output backend talks to a kmbox B+/Net over UDP through
kmNetLib.dll.  This module listens on that socket and pretends to be the box,
so VisionLabs' native aim pipeline can drive any sink - here, a Medius box.

Wire format (public kmNet SDK, little-endian, no encryption):

    cmd_head_t   { u32 mac; u32 rand; u32 indexpts; u32 cmd; }         16 bytes
    soft_mouse_t { i32 button; i32 x; i32 y; i32 wheel; i32 point[10]; } 56 bytes
    soft_keyboard{ u8 ctrl; u8 resvel; u8 button[10]; }                12 bytes

Every command expects the box to echo the 16-byte header back; the client
blocks until it sees a reply with matching cmd/indexpts.  `cmd_monitor`
asks the box to stream the physical mouse/keyboard state to a client port
(carried in `rand`, low 16 bits).

Nothing here touches Medius directly: the server calls a `sink` object.
"""
from __future__ import annotations

import socket
import struct
import threading
import time
from typing import Callable, Optional, Protocol

HEAD = struct.Struct("<IIII")
MOUSE = struct.Struct("<iiii")          # button, x, y, wheel (point[10] follows, ignored)
HW_MOUSE = struct.Struct("<BBhhh")      # report_id, buttons, x, y, wheel   (8 bytes)
HW_KBD_LEN = 12                         # report_id, modifiers, keys[10]

CMD_CONNECT = 0xAF3C2828
CMD_MOUSE_MOVE = 0xAEDE7345
CMD_MOUSE_LEFT = 0x9823AE8D
CMD_MOUSE_MIDDLE = 0x97A3AE8D
CMD_MOUSE_RIGHT = 0x238D8212
CMD_MOUSE_WHEEL = 0xFFEEAD38
CMD_MOUSE_AUTOMOVE = 0xAEDE7346
CMD_KEYBOARD_ALL = 0x123C2C2F
CMD_REBOOT = 0xAA8855AA
CMD_BEZIER_MOVE = 0xA238455A
CMD_MONITOR = 0x27388020
CMD_DEBUG = 0x27382021
CMD_MASK_MOUSE = 0x23234343
CMD_UNMASK_ALL = 0x23344343
CMD_SETCONFIG = 0x1D3D3323
CMD_SETVIDPID = 0xFFED3232
CMD_SHOWPIC = 0x12334883
CMD_TRACE_ENABLE = 0xBBCDDDAC

CMD_NAMES = {v: k for k, v in globals().items() if k.startswith("CMD_")}

MOUSE_CMDS = {CMD_MOUSE_MOVE, CMD_MOUSE_LEFT, CMD_MOUSE_MIDDLE, CMD_MOUSE_RIGHT,
              CMD_MOUSE_WHEEL, CMD_MOUSE_AUTOMOVE, CMD_BEZIER_MOVE}

# soft_mouse.button bits
BTN_LEFT, BTN_RIGHT, BTN_MIDDLE, BTN_SIDE1, BTN_SIDE2 = 0x01, 0x02, 0x04, 0x08, 0x10

# cmd_mask_mouse bits carried in head.rand
MASK_LEFT, MASK_RIGHT, MASK_MIDDLE, MASK_SIDE1, MASK_SIDE2 = 1, 2, 4, 8, 16
MASK_WHEEL, MASK_X, MASK_Y = 32, 64, 128


class Sink(Protocol):
    def move(self, dx: int, dy: int) -> None: ...
    def wheel(self, delta: int) -> None: ...
    def buttons(self, bits: int) -> None: ...
    def mask(self, bits: int) -> None: ...
    def unmask_all(self) -> None: ...
    def reset(self) -> None: ...


class KmboxNetServer:
    """One UDP socket, one thread.  Call start()/stop()."""

    def __init__(self, sink: Sink, listen_ip: str = "127.0.0.1", port: int = 8808,
                 *, log: Callable[[str], None] = print, verbose: bool = False,
                 monitor_hz: float = 250.0):
        self.sink = sink
        self.listen_ip = listen_ip
        self.port = int(port)
        self.log = log
        self.verbose = verbose
        self.monitor_hz = max(10.0, float(monitor_hz))

        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._mon_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

        self.client: Optional[tuple[str, int]] = None
        self.mac: int = 0
        self.packets = 0
        self.unknown = 0
        self.last_cmd = ""
        self._last_buttons = 0

        # physical (hardware) state we report on the monitor stream
        self._hw_lock = threading.Lock()
        self._hw_buttons = 0
        self._hw_dx = 0
        self._hw_dy = 0
        self._hw_wheel = 0
        self._hw_dirty = False
        self._monitor_target: Optional[tuple[str, int]] = None

    # ------------------------------------------------------------------ life
    def start(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.listen_ip, self.port))
        sock.settimeout(0.25)
        self._sock = sock
        self._stop.clear()
        self._thread = threading.Thread(target=self._serve, name="kmboxnet-rx", daemon=True)
        self._thread.start()
        self._mon_thread = threading.Thread(target=self._monitor_loop, name="kmboxnet-mon", daemon=True)
        self._mon_thread.start()
        self.log(f"kmbox-net emulator listening on {self.listen_ip}:{self.port}")

    def stop(self) -> None:
        self._stop.set()
        for t in (self._thread, self._mon_thread):
            if t is not None and t.is_alive():
                t.join(timeout=1.0)
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    # ------------------------------------------------------- hardware state
    def set_hw_button(self, bit: int, down: bool) -> None:
        with self._hw_lock:
            if down:
                self._hw_buttons |= bit
            else:
                self._hw_buttons &= ~bit
            self._hw_dirty = True

    def add_hw_motion(self, dx: int, dy: int, wheel: int) -> None:
        with self._hw_lock:
            self._hw_dx += dx
            self._hw_dy += dy
            self._hw_wheel += wheel
            self._hw_dirty = True

    # --------------------------------------------------------------- serve
    def _serve(self) -> None:
        sock = self._sock
        assert sock is not None
        while not self._stop.is_set():
            try:
                data, addr = sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            self.packets += 1
            if self.verbose:
                self.log(f"kmbox rx {addr[0]}:{addr[1]} {len(data)}B {data[:32].hex(' ')}")
            if len(data) < HEAD.size:
                self.unknown += 1
                continue
            mac, rand, index, cmd = HEAD.unpack_from(data, 0)
            try:
                self._handle(cmd, mac, rand, index, data[HEAD.size:], addr)
            except Exception as exc:  # never kill the server on a bad packet
                self.log(f"kmbox handler error for {CMD_NAMES.get(cmd, hex(cmd))}: {exc!r}")
            # every command is acknowledged by echoing the header
            try:
                sock.sendto(HEAD.pack(mac, rand, index, cmd), addr)
            except OSError:
                pass

    def _handle(self, cmd: int, mac: int, rand: int, index: int, body: bytes, addr) -> None:
        self.last_cmd = CMD_NAMES.get(cmd, hex(cmd))
        if cmd == CMD_CONNECT:
            self.client = addr
            self.mac = mac
            self.log(f"kmbox client connected from {addr[0]}:{addr[1]} uuid={mac:08X}")
            self.sink.reset()
            return
        if cmd in MOUSE_CMDS:
            if len(body) < MOUSE.size:
                self.unknown += 1
                return
            button, x, y, wheel = MOUSE.unpack_from(body, 0)
            if button != self._last_buttons:
                self.sink.buttons(button)
                self._last_buttons = button
            if cmd in (CMD_MOUSE_MOVE, CMD_MOUSE_AUTOMOVE, CMD_BEZIER_MOVE):
                if x or y:
                    self.sink.move(x, y)
            elif cmd == CMD_MOUSE_WHEEL:
                if wheel:
                    self.sink.wheel(wheel)
            return
        if cmd == CMD_MONITOR:
            port = rand & 0xFFFF
            if port:
                self._monitor_target = (addr[0], port)
                self.log(f"kmbox monitor stream -> {addr[0]}:{port}")
            else:
                self._monitor_target = None
            return
        if cmd == CMD_MASK_MOUSE:
            self.sink.mask(rand & 0xFF)
            return
        if cmd == CMD_UNMASK_ALL:
            self.sink.unmask_all()
            return
        if cmd == CMD_REBOOT:
            self.sink.reset()
            return
        if cmd in (CMD_KEYBOARD_ALL, CMD_DEBUG, CMD_SETCONFIG, CMD_SETVIDPID,
                   CMD_SHOWPIC, CMD_TRACE_ENABLE):
            return  # acknowledged, not implemented on a mouse-only bridge
        self.unknown += 1
        if self.verbose or self.unknown <= 5:
            self.log(f"kmbox unknown cmd {cmd:08X} ({len(body)}B body)")

    # ------------------------------------------------------------- monitor
    def _monitor_loop(self) -> None:
        period = 1.0 / self.monitor_hz
        while not self._stop.is_set():
            time.sleep(period)
            target = self._monitor_target
            if target is None or self._sock is None:
                continue
            with self._hw_lock:
                if not self._hw_dirty:
                    continue
                buttons, dx, dy, wheel = self._hw_buttons, self._hw_dx, self._hw_dy, self._hw_wheel
                self._hw_dx = self._hw_dy = self._hw_wheel = 0
                self._hw_dirty = False
            clamp = lambda v: max(-32768, min(32767, int(v)))
            packet = HW_MOUSE.pack(1, buttons & 0xFF, clamp(dx), clamp(dy), clamp(wheel)) + bytes(HW_KBD_LEN)
            try:
                self._sock.sendto(packet, target)
            except OSError:
                pass
