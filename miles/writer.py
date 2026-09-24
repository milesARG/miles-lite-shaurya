"""Writing for the user: notes, messages, replies, poems, essays...

The brain composes the text in its own conversation (so it knows who it's for and what was said), and the
words are typed live, sentence by sentence, into the right place: the text box you're in (an email, a chat),
a fresh Notepad, a Word document, or the clipboard.
"""
import ctypes
import ctypes.wintypes as wt
import random
import re
import subprocess
import threading
import time

import psutil

import config
from .util import log

user32 = ctypes.windll.user32

# "Write me a note" shouldn't land in code editors, terminals or players - those get a fresh Notepad instead.
_NOT_HERE = {"explorer.exe", "code.exe", "cursor.exe", "windowsterminal.exe", "cmd.exe", "powershell.exe",
             "pwsh.exe", "conhost.exe", "spotify.exe", "taskmgr.exe", "python.exe", "pythonw.exe", "steam.exe",
             "vlc.exe", "systemsettings.exe", "idea64.exe", "pycharm64.exe", "rider64.exe", "devenv.exe"}
# Small models like to announce the text first ("Let me refine that for you. Here's a revised version:")
_PREAMBLE = re.compile(r"^\s*(?:(?:sure|certainly|of course|okay|ok|absolutely|alright|here(?:'s| is| are| you go)|"
                       r"let me|i've|i have|below is|this is|refined|revised|improved)\b[^\n]{0,160}?(?::|\n)\s*)+",
                       re.I)


def _foreground():
    hwnd = user32.GetForegroundWindow()
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    try:
        proc = psutil.Process(pid.value).name().lower()
    except Exception:
        proc = "?"
    buf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, buf, 512)
    return hwnd, proc, buf.value


def _focused_field(proc: str):
    """Is the keyboard focus in an editable text box? -> (yes, element name)."""
    import uiautomation as auto
    from .browser import BROWSERS
    try:
        with auto.UIAutomationInitializerInThread():
            c = auto.GetFocusedControl()
            if not c:
                return False, ""
            kind, name = c.ControlTypeName, (c.Name or "")
            if kind not in ("EditControl", "DocumentControl"):
                return False, name
            vp = None
            try:
                vp = c.GetPattern(auto.PatternId.ValuePattern)
            except Exception:
                pass
            if vp is not None:
                if vp.IsReadOnly:
                    return False, name
            elif kind == "DocumentControl" and proc in BROWSERS:      # a web page itself, not a text box
                return False, name
            if proc in BROWSERS and re.search(r"address|search bar|url|find in page", name, re.I):
                return False, name
            return True, name
    except Exception as e:
        log.debug("focused field: %s", e)
        return False, ""


def _focus_mail_body(hwnd) -> bool:
    """In an open Gmail/Outlook compose window, put the cursor in the message body."""
    import uiautomation as auto
    from .uia import find_first
    try:
        with auto.UIAutomationInitializerInThread():
            body = find_first(auto.ControlFromHandle(hwnd), ("Message Body",), substring=True)
            if body:
                body.SetFocus()
                time.sleep(0.15)
                return True
    except Exception as e:
        log.debug("mail body focus: %s", e)
    return False


def _notepads():
    from .tools import windows
    return {h for h, _, p in windows() if p == "notepad.exe"}


def _open_notepad():
    from .tools import focus_hwnd
    before = _notepads()
    subprocess.Popen(["notepad.exe"])
    end = time.time() + 8
    hwnd = None
    while time.time() < end:
        time.sleep(0.15)
        fg, proc, _ = _foreground()
        if proc == "notepad.exe":
            hwnd = fg
            break
        new = _notepads() - before
        if new:
            hwnd = new.pop()
            focus_hwnd(hwnd)
    if not hwnd:
        raise RuntimeError("Notepad didn't open")
    time.sleep(0.35)
    _, _, title = _foreground()
    if not title.lower().startswith("untitled"):         # Notepad restored an old tab: start a fresh one
        import pyautogui
        pyautogui.hotkey("ctrl", "n")
        time.sleep(0.4)
        hwnd = user32.GetForegroundWindow()
    return hwnd


def _md_to_plain(s: str) -> str:
    s = s.replace("**", "").replace("__", "")
    s = re.sub(r"(?m)^[ \t]{0,3}#{1,6}[ \t]+", "", s)
    s = re.sub(r"(?m)^([ \t]*)[*•][ \t]+", r"\1- ", s)
    return s


def _short_title(title: str) -> str:
    t = re.sub(r"\s*[-|—]\s*(google chrome|brave|microsoft edge|mozilla firefox|opera)$", "", title, flags=re.I)
    return (t[:40] + "…") if len(t) > 40 else t or "the active window"


class Writer:
    """Receives the text as the brain streams it and types it into the target."""

    def __init__(self, where: str, request: str):
        self.where = where if where in ("auto", "here", "notepad", "word", "clipboard", "replace",
                                        "selection") else "auto"
        self.request = request
        self.hwnd = None
        self.place = ""
        self.buf = ""
        self.head = True
        self.parts: list[str] = []          # everything written
        self.typed = 0                      # characters actually pasted
        self.pastes = 0                     # each paste is one undo step
        self.prompt = ""                    # Lite: the writing instruction, sent as a user message
        self.lost_focus = False
        self._clip = None

    # ---- before the text arrives -----------------------------------------------------
    def prepare(self) -> str:
        import pyautogui
        import pyperclip
        from .tools import CTX, _is_browser, focus_hwnd
        snap = CTX.snapshot
        if snap is not None and snap.hwnd and user32.IsWindow(snap.hwnd) and \
                user32.GetForegroundWindow() != snap.hwnd and self.where in ("auto", "here", "replace", "selection"):
            focus_hwnd(snap.hwnd)                        # back to the window you were in (selection intact)
            time.sleep(0.2)
        if self.where == "selection":
            if snap is not None and snap.selection:      # pasting over the highlighted text replaces it
                hwnd, _, title = _foreground()
                self.hwnd, self.place = hwnd, f"the selected text in {_short_title(title)}"
            else:
                self.where = "auto"
        if self.where in ("auto", "here", "replace"):
            hwnd, proc, title = _foreground()
            if _is_browser(proc) and re.search(r"gmail|outlook|compose|new message", title, re.I):
                _focus_mail_body(hwnd)
            ok, _ = _focused_field(proc)
            if self.where == "auto" and (not ok or proc in _NOT_HERE):
                self.where = "notepad"
            else:
                self.hwnd, self.place = hwnd, f"the text box in {_short_title(title)}"
                if self.where == "replace":
                    pyautogui.hotkey("ctrl", "a")
                    time.sleep(0.05)
        if self.where == "notepad":
            hwnd, proc, title = _foreground()
            if proc == "notepad.exe" and title.lower().startswith("untitled"):   # a blank one is already open
                self.hwnd = hwnd
            else:
                self.hwnd = _open_notepad()
            self.place = "a new Notepad page"
        elif self.where == "word":
            self.place = "a Word document"
        elif self.where == "clipboard":
            self.place = "the clipboard"
        if self.hwnd:
            try:
                self._clip = pyperclip.paste()
            except Exception:
                self._clip = None
        return self.place

    # ---- while the brain streams ----------------------------------------------------------
    def feed(self, delta: str):
        self.buf += delta
        if self.head:                                   # hold the start back to drop "Here's your note:"
            if len(self.buf) < 90 and "\n" not in self.buf:
                return
            self.buf = _PREAMBLE.sub("", self.buf, count=1).lstrip().lstrip('"“')
            self.head = False
        cut = max([self.buf.rfind("\n")] + [self.buf.rfind(p) + 1 for p in (". ", "! ", "? ", ".\"", "!\"")])
        if cut > 0:
            chunk, self.buf = self.buf[:cut], self.buf[cut:]
            self._emit(chunk)

    def _emit(self, chunk: str):
        chunk = _md_to_plain(chunk)
        if not chunk:
            return
        self.parts.append(chunk)
        if not self.hwnd or self.lost_focus:
            return
        if user32.GetForegroundWindow() != self.hwnd:     # the user moved on: don't type into their window
            self.lost_focus = True
            return
        import pyautogui
        import pyperclip
        pyperclip.copy(chunk)
        time.sleep(0.03)
        pyautogui.hotkey("ctrl", "v")
        time.sleep(0.15)                                 # let the app read the clipboard before it changes
        self.typed += len(chunk)
        self.pastes += 1

    # ---- afterwards -------------------------------------------------------------------------
    def text(self) -> str:
        return "".join(self.parts).strip()

    def finish(self) -> str:
        rest = self.buf
        if self.head:
            rest = _PREAMBLE.sub("", rest, count=1).lstrip().lstrip('"“')
        rest = rest.rstrip().rstrip('"”').rstrip()
        self.buf = ""
        if rest:
            self._emit(rest)
        text = self.text()
        words = len(text.split())
        if not text:
            self.cancel()
            return "Nothing was written."
        if self.where == "word":
            from .tools import create_document
            first = text.split("\n", 1)[0].strip()
            title = first if 0 < len(first) <= 60 and not first.endswith(".") else \
                " ".join(re.sub(r"[^\w\s']", "", self.request).split()[:6]).title() or "Note"
            body = text.split("\n", 1)[1].strip() if title == first and "\n" in text else text
            return f"Wrote {words} words. " + create_document(title, body, "docx")
        import pyperclip
        if self.where == "clipboard":
            pyperclip.copy(text)
            return f"Copied the {words}-word text to the clipboard."
        if self.lost_focus:
            pyperclip.copy(text)
            return (f"Started typing into {self.place}, but the user switched windows, so I stopped and put the "
                    f"whole {words}-word text on the clipboard instead.")
        self._restore_clipboard()
        from .tools import CTX
        CTX.last_write = {"hwnd": self.hwnd, "steps": self.pastes, "t": time.time()}
        return f"Typed {words} words into {self.place}."

    def spoken(self) -> str:
        """What Miles says once the text is written (fixed wording, so it can't claim the wrong place)."""
        t = config.USER_TITLE
        if self.lost_focus:
            return f"You switched windows, {t}, so I put the text on your clipboard instead."
        if self.where == "word":
            return random.choice([f"Done, {t}. It's saved as a Word document.", f"Written and saved in Word, {t}."])
        if self.where == "clipboard":
            return f"Done, {t}. It's on your clipboard."
        if self.where == "notepad":
            return random.choice([f"Done, {t}. It's in Notepad.", f"There you go, {t}. It's written in Notepad.",
                                  f"Written, {t}. Have a look in Notepad."])
        if self.where in ("selection", "replace"):
            return random.choice([f"Done, {t}. It's in place.", f"There you go, {t}. It's updated.",
                                  f"Done, {t}. Say undo if you'd like it back."])
        return random.choice([f"Done, {t}.", f"There you go, {t}.", f"Written, {t}."])

    def cancel(self):
        self._restore_clipboard()

    def _restore_clipboard(self):
        if self._clip is None:
            return
        old, self._clip = self._clip, None

        def put():
            try:
                import pyperclip
                pyperclip.copy(old)
            except Exception:
                pass
        threading.Timer(0.6, put).start()
