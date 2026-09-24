"""Reading the screen without a GPU: Windows' built-in OCR (the same engine the Snipping Tool uses).

It runs on the CPU in well under a second, needs no model download and almost no RAM, and returns
where every word is - so Miles can describe what's on screen and click any visible text, even in apps
that UI Automation can't see into.
"""
import asyncio
import ctypes
import difflib
import re
import threading

from .util import log

_engine = None
_lock = threading.Lock()


def _get_engine():
    global _engine
    if _engine is None:
        from winrt.windows.media.ocr import OcrEngine
        _engine = OcrEngine.try_create_from_user_profile_languages()
        if _engine is None:
            from winrt.windows.globalization import Language
            _engine = OcrEngine.try_create_from_language(Language("en-US"))
    return _engine


async def _recognize(img):
    from PIL import Image
    from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
    from winrt.windows.storage.streams import DataWriter
    r, g, b, a = img.convert("RGBA").split()
    writer = DataWriter()
    writer.write_bytes(Image.merge("RGBA", (b, g, r, a)).tobytes())         # Windows wants BGRA
    bmp = SoftwareBitmap.create_copy_from_buffer(writer.detach_buffer(), BitmapPixelFormat.BGRA8,
                                                 img.width, img.height)
    return await _get_engine().recognize_async(bmp)


def read(region=None):
    """OCR the screen (or region=(x, y, w, h)). -> list of lines: {"text", "words": [(word, x, y, w, h)]}.
    Coordinates are screen pixels."""
    import pyautogui
    shot = pyautogui.screenshot(region=region)
    ox, oy = (region[0], region[1]) if region else (0, 0)
    scale = 1.0
    limit = 2600                                   # the OCR engine's maximum image side
    if max(shot.size) > limit:
        scale = limit / max(shot.size)
        shot = shot.resize((int(shot.width * scale), int(shot.height * scale)))
    with _lock:
        result = asyncio.run(_recognize(shot))
    lines = []
    for ln in result.lines:
        words = []
        for w in ln.words:
            r = w.bounding_rect
            words.append((w.text, int(ox + r.x / scale), int(oy + r.y / scale), int(r.width / scale),
                          int(r.height / scale)))
        lines.append({"text": ln.text, "words": words})
    return lines


def window_region():
    """The foreground window's rectangle, clipped to the screen."""
    import ctypes.wintypes as wt
    import pyautogui
    r = wt.RECT()
    ctypes.windll.user32.GetWindowRect(ctypes.windll.user32.GetForegroundWindow(), ctypes.byref(r))
    sw, sh = pyautogui.size()
    x0, y0, x1, y1 = max(0, r.left), max(0, r.top), min(sw, r.right), min(sh, r.bottom)
    return (x0, y0, x1 - x0, y1 - y0) if x1 - x0 > 50 and y1 - y0 > 50 else None


def screen_text(active_window_only=True, max_chars=3500) -> str:
    """All readable text, top to bottom, as plain lines."""
    region = window_region() if active_window_only else None
    lines = read(region)
    lines.sort(key=lambda ln: (ln["words"][0][2] // 12, ln["words"][0][1]) if ln["words"] else (0, 0))
    text = "\n".join(ln["text"] for ln in lines if ln["text"].strip())
    return text[:max_chars] + ("\n…(more)" if len(text) > max_chars else "")


def find(target: str, active_window_only=False):
    """Where is this text on screen? -> (x, y) centre of the best match, the matched text, score."""
    t = " ".join(target.lower().split())
    best = (None, "", 0.0)
    for ln in read(window_region() if active_window_only else None):
        words = ln["words"]
        # try every run of consecutive words in the line (so "Sign in" matches two words)
        for i in range(len(words)):
            for j in range(i, min(len(words), i + max(1, len(t.split())) + 2)):
                run = words[i:j + 1]
                phrase = " ".join(w[0] for w in run).lower()
                score = 1.0 if phrase == t else difflib.SequenceMatcher(None, t, phrase).ratio()
                if t in phrase:
                    score = max(score, 0.9 - (len(phrase) - len(t)) * 0.01)
                if score > best[2]:
                    x0, y0 = run[0][1], min(w[2] for w in run)
                    x1 = run[-1][1] + run[-1][3]
                    y1 = max(w[2] + w[4] for w in run)
                    best = (((x0 + x1) // 2, (y0 + y1) // 2), " ".join(w[0] for w in run), score)
    return best


def available() -> bool:
    try:
        return _get_engine() is not None
    except Exception as e:
        log.warning("Windows OCR unavailable: %s", e)
        return False
