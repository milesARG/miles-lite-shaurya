"""Miles's on-screen buddy: an animated arc-reactor HUD with a face, plus a HUD speech bubble.

Rendered with Pillow every frame and pushed to a per-pixel-alpha layered window
(UpdateLayeredWindow), so it has real soft glows and floats over the desktop.
Drag to move, double-click to talk, right-click for the menu.
"""
import ctypes
import ctypes.wintypes as wt
import json
import math
import queue
import random
import time
import tkinter as tk
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

import config
from .owner import CREDIT
from .util import DATA, log

_u32 = ctypes.WinDLL("user32", use_last_error=True)
_gdi = ctypes.WinDLL("gdi32")


class _BLEND(ctypes.Structure):
    _fields_ = [("op", ctypes.c_ubyte), ("flags", ctypes.c_ubyte), ("alpha", ctypes.c_ubyte), ("fmt", ctypes.c_ubyte)]


class _BIH(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG), ("biPlanes", wt.WORD),
                ("biBitCount", wt.WORD), ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD),
                ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD),
                ("biClrImportant", wt.DWORD)]


_u32.GetDC.restype = wt.HDC
_u32.GetDC.argtypes = [wt.HWND]
_u32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
_gdi.CreateCompatibleDC.restype = wt.HDC
_gdi.CreateCompatibleDC.argtypes = [wt.HDC]
_gdi.CreateDIBSection.restype = wt.HBITMAP
_gdi.CreateDIBSection.argtypes = [wt.HDC, ctypes.c_void_p, wt.UINT, ctypes.POINTER(ctypes.c_void_p), wt.HANDLE,
                                  wt.DWORD]
_gdi.SelectObject.restype = wt.HGDIOBJ
_gdi.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
_u32.UpdateLayeredWindow.argtypes = [wt.HWND, wt.HDC, ctypes.POINTER(wt.POINT), ctypes.POINTER(wt.SIZE), wt.HDC,
                                     ctypes.POINTER(wt.POINT), wt.DWORD, ctypes.POINTER(_BLEND), wt.DWORD]
_u32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
_u32.GetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int]
_u32.SetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_ssize_t]
_u32.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
_u32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.UINT]

COLORS = {
    "boot": (0, 200, 255), "idle": (0, 190, 255), "listening": (0, 235, 255), "thinking": (255, 176, 60),
    "working": (255, 176, 60), "speaking": (70, 200, 255), "confirm": (255, 120, 70), "error": (255, 80, 90),
    "success": (100, 240, 170), "sleep": (100, 125, 150),
}
HEADERS = {
    "boot": "INITIALISING", "idle": "STANDING BY", "listening": "YOU", "thinking": "PROCESSING",
    "working": "EXECUTING", "speaking": "RESPONDING", "confirm": "AWAITING CONFIRMATION", "error": "ALERT",
    "success": "COMPLETE", "sleep": "WAKE WORD OFF",
}
_FONTS = Path(r"C:\Windows\Fonts")


def _font(names, size):
    for n in names:
        try:
            return ImageFont.truetype(str(_FONTS / n), size)
        except OSError:
            continue
    return ImageFont.load_default()


def _lerp(a, b, t):
    return a + (b - a) * t


def _wrap(text, font, width):
    lines = []
    for para in text.split("\n"):
        cur = ""
        for w in para.split():
            test = f"{cur} {w}".strip()
            if font.getlength(test) <= width:
                cur = test
            else:
                if cur:
                    lines.append(cur)
                cur = w
        lines.append(cur)
    return [l for l in lines if l is not None]


class Buddy:
    def __init__(self, on_talk=None, on_menu=None):
        self.on_talk, self.on_menu = on_talk, on_menu
        s = config.BUDDY_SIZE
        self.s = s
        self.ORB = int(150 * s)                    # orb box size (px)
        self.W, self.H = int(560 * s), int(260 * s)
        self.PAD = int(10 * s)
        self.cmd: queue.Queue = queue.Queue()

        self.root = tk.Tk()
        self.root.title("Miles")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.configure(bg="black")
        work = wt.RECT()
        _u32.SystemParametersInfoW(0x30, 0, ctypes.byref(work), 0)
        self.work = (work.left, work.top, work.right, work.bottom)
        pos = self._load_pos()
        self.ox = pos[0] if pos else work.right - self.ORB // 2 - 24
        self.oy = pos[1] if pos else work.bottom - self.ORB // 2 - 24
        self.root.geometry(f"{self.W}x{self.H}+{self.ox}+{self.oy}")
        self.root.update_idletasks()
        self.hwnd = int(self.root.wm_frame(), 16)
        ex = _u32.GetWindowLongPtrW(self.hwnd, -20)
        _u32.SetWindowLongPtrW(self.hwnd, -20, ex | 0x80000 | 0x80 | 0x8 | 0x08000000)   # layered|tool|topmost|noactivate
        self._init_surface()

        self.f_head = _font(["consolab.ttf", "consola.ttf"], int(12 * s))
        self.f_body = _font(["segoeui.ttf"], int(15 * s))
        self.f_small = _font(["consola.ttf"], int(10 * s))

        # animation state
        self.state = "boot"
        self.header = ""
        self.body = ""
        self.reveal = 0.0
        self.color = np.array(COLORS["boot"], float)
        self.scale = 0.2
        self.bubble = 0.0
        self.bubble_until = 0.0
        self.level = 0.0
        self._level_in = 0.0
        self.t0 = time.time()
        self.last = time.time()
        self.boot_t = time.time()
        self.next_blink = time.time() + 2
        self.blink_t = -1.0
        self.look = np.zeros(2)
        self.emotion, self.emotion_until = "normal", 0.0
        self.hover = False
        self.visible = config.BUDDY_ENABLED
        self._drag = None
        self._bars = np.zeros(64)

        r = self.root
        r.bind("<ButtonPress-1>", self._press)
        r.bind("<B1-Motion>", self._motion)
        r.bind("<ButtonRelease-1>", self._release)
        r.bind("<Double-Button-1>", lambda e: self.on_talk and self.on_talk())
        r.bind("<Button-3>", self._popup)
        r.bind("<Enter>", lambda e: setattr(self, "hover", True))
        r.bind("<Leave>", lambda e: setattr(self, "hover", False))
        self.menu = None
        r.after(16, self._tick)

    # ---- public API (thread-safe) ------------------------------------------------
    def set_state(self, state, body=None, header=None, hold=None, instant=False):
        """instant=True shows the text at once (live captions) instead of typing it out."""
        self.cmd.put(("state", state, body, header, hold, instant))

    def append(self, text):
        self.cmd.put(("append", text))

    def set_level(self, v):
        self._level_in = float(v)

    def emote(self, kind, secs=1.8):
        self.cmd.put(("emote", kind, secs))

    def call(self, fn):
        self.cmd.put(("call", fn))

    def set_visible(self, v):
        self.cmd.put(("visible", v))

    def run(self):
        self.root.mainloop()

    def quit(self):
        self.cmd.put(("quit",))

    # ---- surface ----------------------------------------------------------------
    def _init_surface(self):
        screen = _u32.GetDC(None)
        self.mdc = _gdi.CreateCompatibleDC(screen)
        bih = _BIH(ctypes.sizeof(_BIH), self.W, -self.H, 1, 32, 0, 0, 0, 0, 0, 0)
        self.bits = ctypes.c_void_p()
        self.hbmp = _gdi.CreateDIBSection(screen, ctypes.byref(bih), 0, ctypes.byref(self.bits), None, 0)
        _gdi.SelectObject(self.mdc, self.hbmp)
        _u32.ReleaseDC(None, screen)

    def _layout(self):
        """Orb on the right with bubble to its left, flipped when near the left screen edge."""
        mid = (self.work[0] + self.work[2]) / 2
        self.side = "right" if self.ox > mid else "left"
        h = self.ORB // 2
        oxw = self.W - self.PAD - h if self.side == "right" else self.PAD + h
        oyw = self.H - self.PAD - h
        return oxw, oyw, self.ox - oxw, self.oy - oyw

    def _push(self, img, wx, wy):
        a = np.asarray(img, dtype=np.uint8)
        alpha = a[..., 3:4].astype(np.uint16)
        out = np.empty_like(a)
        out[..., 0] = (a[..., 2] * alpha[..., 0] // 255)
        out[..., 1] = (a[..., 1] * alpha[..., 0] // 255)
        out[..., 2] = (a[..., 0] * alpha[..., 0] // 255)
        out[..., 3] = a[..., 3]
        ctypes.memmove(self.bits, out.ctypes.data, out.nbytes)
        dst, size, src = wt.POINT(int(wx), int(wy)), wt.SIZE(self.W, self.H), wt.POINT(0, 0)
        blend = _BLEND(0, 0, 255, 1)
        _u32.UpdateLayeredWindow(self.hwnd, None, ctypes.byref(dst), ctypes.byref(size), self.mdc,
                                 ctypes.byref(src), 0, ctypes.byref(blend), 2)
        self.root.geometry(f"+{int(wx)}+{int(wy)}")

    # ---- input --------------------------------------------------------------------
    def _press(self, e):
        self._drag = (e.x_root, e.y_root, self.ox, self.oy, False)

    def _motion(self, e):
        if not self._drag:
            return
        x0, y0, ox, oy, _ = self._drag
        dx, dy = e.x_root - x0, e.y_root - y0
        if abs(dx) + abs(dy) > 3:
            self._drag = (x0, y0, ox, oy, True)
            self.ox = min(max(ox + dx, self.work[0] + self.ORB // 2), self.work[2] - self.ORB // 2)
            self.oy = min(max(oy + dy, self.work[1] + self.ORB // 2), self.work[3] - self.ORB // 2)

    def _release(self, e):
        if self._drag and self._drag[4]:
            self._save_pos()
        self._drag = None

    def _popup(self, e):
        if self.on_menu:
            self.on_menu(e.x_root, e.y_root)

    def _load_pos(self):
        try:
            p = json.loads((DATA / "buddy.json").read_text())
            if self.work[0] <= p[0] <= self.work[2] and self.work[1] <= p[1] <= self.work[3]:
                return p
        except Exception:
            pass
        return None

    def _save_pos(self):
        try:
            (DATA / "buddy.json").write_text(json.dumps([self.ox, self.oy]))
        except Exception:
            pass

    # ---- main loop ------------------------------------------------------------------
    def _handle_cmds(self, now):
        while True:
            try:
                c = self.cmd.get_nowait()
            except queue.Empty:
                return
            if c[0] == "state":
                _, st, body, header, hold, instant = c
                self.state = st
                self.header = header or HEADERS.get(st, st.upper())
                if body is not None:
                    self.body = body
                    self.reveal = len(body) if instant else 0
                if st == "idle":
                    self.bubble_until = now + (hold if hold is not None else config.BUBBLE_HIDE_AFTER_SEC)
                elif st == "sleep":
                    self.bubble_until = now + 3
                else:
                    self.bubble_until = float("inf")
                if st == "error":
                    self.emotion, self.emotion_until = "sad", now + 2.5
                if st == "success":
                    self.emotion, self.emotion_until = "happy", now + 1.8
            elif c[0] == "append":
                self.body = (self.body + " " + c[1]).strip() if self.body else c[1]
            elif c[0] == "emote":
                self.emotion, self.emotion_until = c[1], now + c[2]
            elif c[0] == "call":
                try:
                    c[1]()
                except Exception as e:
                    log.exception("ui call failed: %s", e)
            elif c[0] == "visible":
                self.visible = c[1]
            elif c[0] == "quit":
                self.root.destroy()
                return

    def _tick(self):
        try:
            now = time.time()
            dt = min(0.1, now - self.last)
            self.last = now
            self._handle_cmds(now)
            if not self.root.winfo_exists():
                return
            self._animate(now, dt)
            if self.visible:
                frame = self._render(now)
                oxw, oyw, wx, wy = self._layout()
                self._push(frame, wx, wy)
            else:
                self._push(Image.new("RGBA", (self.W, self.H)), -10000, -10000)
        except tk.TclError:
            return
        except Exception as e:
            log.exception("buddy frame failed: %s", e)
        active = self.state not in ("idle", "sleep") or 0.01 < self.bubble < 0.99 or now - self.boot_t < 3 \
            or self.reveal < len(self.body)
        fps = getattr(config, "BUDDY_FPS_ACTIVE", 30) if active else getattr(config, "BUDDY_FPS_IDLE", 20)
        self.root.after(int(1000 / max(1, fps)), self._tick)

    def _animate(self, now, dt):
        st = self.state
        target = np.array(COLORS.get(st, COLORS["idle"]), float)
        self.color += (target - self.color) * min(1, dt * 6)
        active = st not in ("idle", "sleep")
        tscale = 1.0 if active or self.hover else 0.78
        if st == "boot":
            tscale = 1.0
        self.scale = _lerp(self.scale, tscale, min(1, dt * 5))
        show = (now < self.bubble_until and bool(self.body or self.header)) or (self.hover and not active)
        self.bubble = _lerp(self.bubble, 1.0 if show else 0.0, min(1, dt * 8))
        self.reveal = min(len(self.body), self.reveal + dt * (90 if st == "speaking" else 160))
        self.level = _lerp(self.level, self._level_in, min(1, dt * 18))
        if st not in ("listening", "speaking"):
            self._level_in *= 0.9
        # blink
        if now > self.next_blink:
            self.blink_t = now
            self.next_blink = now + random.uniform(2.5, 6.0)
            if random.random() < 0.2:
                self.next_blink = now + 0.25          # double blink
        # eyes follow the cursor
        p = wt.POINT()
        _u32.GetCursorPos(ctypes.byref(p))
        v = np.array([p.x - self.ox, p.y - self.oy], float)
        n = np.linalg.norm(v)
        tgt = v / n * min(1.0, n / 400) if n > 1 else np.zeros(2)
        if st == "thinking" or st == "working":
            tgt = np.array([0.6 * math.sin(now * 2.2), -0.7])
        self.look += (tgt - self.look) * min(1, dt * 7)
        if self.emotion != "normal" and now > self.emotion_until:
            self.emotion = "normal"

    # ---- drawing ----------------------------------------------------------------------
    def _render(self, now):
        img = Image.new("RGBA", (self.W, self.H), (0, 0, 0, 0))
        oxw, oyw, _, _ = self._layout()
        col = tuple(int(c) for c in self.color)
        if self.bubble > 0.02:
            self._draw_bubble(img, oxw, oyw, col, now)
        orb = self._draw_orb(now, col)
        op = 1.0 if self.state not in ("idle", "sleep") or self.hover else config.BUDDY_IDLE_OPACITY
        if op < 0.99:
            orb.putalpha(orb.getchannel("A").point(lambda a: int(a * op)))
        img.alpha_composite(orb, (int(oxw - orb.width / 2), int(oyw - orb.height / 2)))
        return img

    def _draw_orb(self, now, col):
        SS = 2
        size = int(self.ORB * SS)
        R = size / 2 * self.scale
        c = size / 2
        t = now - self.t0
        boot = min(1.0, (now - self.boot_t) / 1.6)
        st = self.state
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        d = ImageDraw.Draw(img, "RGBA")
        bright = tuple(min(255, int(v * 0.6 + 110)) for v in col)

        # soft glow
        lvl = self.level
        glow_r = R * (0.72 + 0.12 * lvl + 0.03 * math.sin(t * 2))
        for i in range(10, 0, -1):
            rr = glow_r * (0.55 + i * 0.05)
            a = int(10 * (1 - i / 11) * (1.4 if st != "idle" else 0.9))
            d.ellipse((c - rr, c - rr, c + rr, c + rr), fill=col + (a,))

        # outer segmented ring
        rot = t * (60 if st in ("thinking", "working") else 18)
        r1 = R * 0.93
        w1 = max(2, int(R * 0.035))
        for k in range(3):
            start = rot + k * 120
            ext = 78 * boot
            d.arc((c - r1, c - r1, c + r1, c + r1), start, start + ext, fill=col + (230,), width=w1)
        # tick ring
        r2o, r2i = R * 0.85, R * 0.80
        rot2 = -t * 10
        for k in range(0, 72):
            if k / 72 > boot:
                break
            ang = math.radians(rot2 + k * 5)
            inner = r2i - (R * 0.03 if k % 6 == 0 else 0)
            a = 200 if k % 6 == 0 else 90
            d.line((c + math.cos(ang) * inner, c + math.sin(ang) * inner,
                    c + math.cos(ang) * r2o, c + math.sin(ang) * r2o), fill=col + (a,), width=max(1, SS))
        # inner ring + fast arcs
        r3 = R * 0.74
        d.ellipse((c - r3, c - r3, c + r3, c + r3), outline=col + (70,), width=max(1, SS))
        spin = t * (320 if st in ("thinking", "working") else 40)
        for k in range(2):
            s0 = spin + k * 180
            d.arc((c - r3, c - r3, c + r3, c + r3), s0, s0 + 50 * boot, fill=bright + (255,), width=max(2, int(R * 0.03)))

        # audio-reactive bars
        if st in ("listening", "speaking") or lvl > 0.02:
            n = len(self._bars)
            target = np.abs(np.sin(np.arange(n) * 0.9 + t * 9)) * 0.5 + np.random.rand(n) * 0.5
            self._bars += (target * lvl - self._bars) * 0.5
            rb = R * 0.58
            for k in range(n):
                ang = math.radians(k * 360 / n - 90)
                ln = R * (0.02 + 0.13 * self._bars[k])
                d.line((c + math.cos(ang) * rb, c + math.sin(ang) * rb,
                        c + math.cos(ang) * (rb + ln), c + math.sin(ang) * (rb + ln)),
                       fill=bright + (220,), width=max(2, int(R * 0.022)))

        # core
        rc = R * 0.52
        breathe = 1 + 0.015 * math.sin(t * 2.4)
        rc *= breathe
        for i in range(6, 0, -1):
            rr = rc * (0.4 + i * 0.1)
            d.ellipse((c - rr, c - rr, c + rr, c + rr), fill=(4 + i * 2, 16 + i * 3, 24 + i * 4, 245))
        d.ellipse((c - rc, c - rc, c + rc, c + rc), outline=col + (255,), width=max(2, int(R * 0.03)))
        self._draw_face(d, c, rc, col, bright, now)

        return img.reduce(SS)

    def _draw_face(self, d, c, rc, col, bright, now):
        st = self.state
        lx, ly = self.look * rc * 0.12
        ex = rc * 0.34
        ey = c - rc * 0.08 + ly
        ew = rc * 0.19
        eh = rc * 0.36 * (1.15 if st == "listening" else 1.0)
        bt = now - self.blink_t
        if 0 <= bt < 0.16:
            eh *= max(0.08, abs(bt - 0.08) / 0.08)
        eye = bright + (255,)
        if st == "sleep":
            for sx in (-1, 1):
                x = c + sx * ex
                d.line((x - ew * 0.7, ey + eh * 0.2, x + ew * 0.7, ey + eh * 0.2), fill=eye, width=int(rc * 0.07))
            return
        for sx in (-1, 1):
            x = c + sx * ex + lx
            if self.emotion == "happy":
                d.arc((x - ew * 0.9, ey - eh * 0.25, x + ew * 0.9, ey + eh * 0.55), 200, 340, fill=eye,
                      width=max(2, int(rc * 0.08)))
            elif self.emotion == "sad":
                h2 = eh * 0.6
                d.rounded_rectangle((x - ew / 2, ey - h2 / 2 + eh * 0.15, x + ew / 2, ey + h2 / 2 + eh * 0.15),
                                    radius=ew / 2, fill=eye)
                d.line((x - ew * 0.8, ey - eh * 0.35 - sx * eh * 0.1, x + ew * 0.8, ey - eh * 0.35 + sx * eh * 0.1),
                       fill=eye, width=max(2, int(rc * 0.05)))
            else:
                d.rounded_rectangle((x - ew / 2, ey - eh / 2, x + ew / 2, ey + eh / 2), radius=min(ew, eh) / 2,
                                    fill=eye)
                if eh > ew:            # little highlight
                    d.ellipse((x - ew * 0.28, ey - eh * 0.35, x - ew * 0.02, ey - eh * 0.12), fill=(255, 255, 255, 170))
        # mouth
        my = c + rc * 0.42 + ly * 0.5
        mx = c + lx * 0.6
        if st == "speaking":
            mh = rc * (0.04 + 0.2 * self.level)
            mw = rc * (0.22 + 0.06 * self.level)
            d.rounded_rectangle((mx - mw / 2, my - mh / 2, mx + mw / 2, my + mh / 2), radius=mh / 2, fill=eye)
        elif self.emotion == "sad" or st == "error":
            d.arc((mx - rc * 0.16, my - rc * 0.02, mx + rc * 0.16, my + rc * 0.22), 200, 340, fill=eye,
                  width=max(2, int(rc * 0.05)))
        elif st in ("thinking", "working"):
            d.line((mx - rc * 0.08, my, mx + rc * 0.08, my), fill=eye, width=max(2, int(rc * 0.05)))
        elif st == "listening":
            d.ellipse((mx - rc * 0.06, my - rc * 0.06, mx + rc * 0.06, my + rc * 0.06), outline=eye,
                      width=max(2, int(rc * 0.04)))
        else:
            d.arc((mx - rc * 0.16, my - rc * 0.2, mx + rc * 0.16, my + rc * 0.06), 20, 160, fill=eye,
                  width=max(2, int(rc * 0.05)))

    def _draw_bubble(self, img, oxw, oyw, col, now):
        s = self.s
        a = self.bubble
        bw = self.W - self.ORB - self.PAD * 3
        x0 = self.PAD if self.side == "right" else self.ORB + self.PAD * 2
        text = self.body[:int(self.reveal)]
        if self.hover and self.state in ("idle", "sleep") and now >= self.bubble_until:
            header, text = "MILES  //  " + HEADERS[self.state], \
                f"Say “Hey Miles” or press Ctrl+Alt+M. Double-click me to talk, right-click for options. {CREDIT}."
        else:
            header = self.header or HEADERS.get(self.state, "")
            if not header.startswith("YOU"):
                header = "MILES  //  " + header
        lines = _wrap(text, self.f_body, bw - 32 * s)
        lh = int(21 * s)
        max_lines = 7
        if len(lines) > max_lines:
            lines = lines[-max_lines:]
        bh = int(40 * s) + lh * max(1, len(lines)) + int(12 * s)
        y1 = oyw + int(18 * s)
        y0 = y1 - bh
        slide = (1 - a) * 12 * s
        x0 += slide if self.side == "right" else -slide
        x1 = x0 + bw
        layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(layer, "RGBA")
        d.rounded_rectangle((x0, y0, x1, y1), radius=int(12 * s), fill=(5, 13, 20, 228), outline=col + (110,),
                            width=1)
        # HUD corner brackets
        L = int(14 * s)
        for (cx, cy, dx, dy) in ((x0, y0, 1, 1), (x1, y0, -1, 1), (x0, y1, 1, -1), (x1, y1, -1, -1)):
            d.line((cx, cy + dy * 3, cx, cy + dy * L), fill=col + (255,), width=2)
            d.line((cx + dx * 3, cy, cx + dx * L, cy), fill=col + (255,), width=2)
        # header + activity indicator
        hx, hy = x0 + 16 * s, y0 + 11 * s
        d.text((hx, hy), header, font=self.f_head, fill=col + (255,))
        hw = self.f_head.getlength(header)
        busy = self.state in ("thinking", "working", "listening", "speaking", "boot")
        clock_w = 0 if busy else self.f_small.getlength("00:00") + 10 * s
        bar_x0, bar_x1 = hx + hw + 12 * s, x1 - 16 * s
        by = hy + 7 * s
        if bar_x1 - bar_x0 > 20:
            d.line((bar_x0, by, bar_x1 - clock_w, by), fill=col + (50,), width=1)
            if busy:
                span = (bar_x1 - bar_x0) * 0.25
                p = (now * 0.9) % 1
                sx = bar_x0 + (bar_x1 - bar_x0 - span) * (0.5 - 0.5 * math.cos(p * 2 * math.pi))
                d.line((sx, by, sx + span, by), fill=col + (255,), width=2)
            else:
                d.text((bar_x1 - self.f_small.getlength(time.strftime("%H:%M")), hy + 1 * s),
                       time.strftime("%H:%M"), font=self.f_small, fill=col + (150,))
        # body
        ty = y0 + 34 * s
        for i, line in enumerate(lines):
            d.text((x0 + 16 * s, ty + i * lh), line, font=self.f_body, fill=(222, 240, 250, 255))
        if self.reveal < len(self.body) and lines and int(now * 3) % 2 == 0:
            lx = x0 + 16 * s + self.f_body.getlength(lines[-1]) + 2
            d.rectangle((lx, ty + (len(lines) - 1) * lh + 4 * s, lx + 7 * s, ty + len(lines) * lh - 3 * s),
                        fill=col + (220,))
        # connector to orb
        oedge = oxw - self.ORB * 0.36 * self.scale if self.side == "right" else oxw + self.ORB * 0.36 * self.scale
        bx = x1 if self.side == "right" else x0
        d.line((bx, y1 - 22 * s, (bx + oedge) / 2, y1 - 22 * s), fill=col + (160,), width=1)
        d.line(((bx + oedge) / 2, y1 - 22 * s, oedge, oyw), fill=col + (160,), width=1)
        d.ellipse((oedge - 3, oyw - 3, oedge + 3, oyw + 3), fill=col + (255,))
        if a < 0.99:
            layer.putalpha(layer.getchannel("A").point(lambda v: int(v * a)))
        img.alpha_composite(layer)
