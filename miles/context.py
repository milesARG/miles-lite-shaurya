"""What you're looking at right now, so "this", "that", "here" and "the selected text" mean what JARVIS would
take them to mean: highlighted text, the text box with the cursor, the web page's address, files selected in
File Explorer, the element under the mouse, and the clipboard.

Everything is read through Windows UI Automation (no keystrokes). Only when an app doesn't expose its
selection that way, and you've asked about the selection, is it copied with Ctrl+C (the clipboard is put back
afterwards) - never in terminals, where Ctrl+C would stop the running program.
"""
import ctypes
import ctypes.wintypes as wt
import os
import re
import threading
import time
from dataclasses import dataclass, field

import psutil

from .util import log

user32 = ctypes.windll.user32
_OWN_PID = os.getpid()
_TERMINALS = {"windowsterminal.exe", "cmd.exe", "powershell.exe", "pwsh.exe", "conhost.exe", "wezterm-gui.exe",
              "alacritty.exe", "mintty.exe", "putty.exe", "openconsole.exe", "wsl.exe", "bash.exe"}
_CODE_EDITORS = {"code.exe", "cursor.exe", "windsurf.exe"}     # Ctrl+C with nothing selected copies the line
_last_external = [0]

# ---- what a request refers to -------------------------------------------------------------------------
_TEXT_NOUNS = (r"text|prompt|paragraph|sentence|line|lines|word|words|passage|part|code|snippet|message|reply|email|"
               r"draft|question|answer|list|point|bit|section|title|caption|comment|note|query|function|error")
SELECTION = re.compile(
    rf"\b(select(ed|ion)|highlight(ed)?|marked|what i('ve| have)? (selected|highlighted|marked)|"
    rf"(this|that|these|those) ({_TEXT_NOUNS})s?)\b", re.I)
TEXT_VERBS = (r"refine|rewrite|re-write|rephrase|reword|improve|polish|fix|correct|proofread|shorten|condense|"
              r"summari[sz]e|expand|elaborate|simplify|translate|explain|clarify|edit|continue|complete|finish|"
              r"answer|reply to|respond to|read|search|google|look up|define|paraphrase|critique|review|"
              r"format|convert|make")
_VERB_THIS = re.compile(rf"\b({TEXT_VERBS})\b[^.?!]{{0,40}}?\b(this|that|it|these|them)\b", re.I)
_CLIPBOARD = re.compile(r"\b(clipboard|what i (just )?copied|copied text)\b", re.I)


def wants_selection(text: str) -> bool:
    return bool(SELECTION.search(text) or _VERB_THIS.search(text))


_DEICTIC = re.compile(r"\b(this|that|these|those|here|it|selected|highlighted|selection|page|site|website|tab|"
                      r"article|video|file|files|folder|button|icon|link|clipboard|copied|box|field|cursor|mouse|"
                      r"pointing|hovering)\b", re.I)


POINTER = re.compile(r"\b(button|icon|pointing|hovering|mouse|cursor|what'?s this|what is this|this one|that one)\b",
                     re.I)


def refers_to_screen(text: str) -> bool:
    """Does the request point at something on screen? Only then is the context read and sent (it costs time)."""
    return bool(_DEICTIC.search(text))


# ---- which window "this" is in --------------------------------------------------------------------------
def _pid(hwnd) -> int:
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def _proc(hwnd) -> str:
    try:
        return psutil.Process(_pid(hwnd)).name().lower()
    except Exception:
        return "?"


def _title(hwnd) -> str:
    buf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, buf, 512)
    return buf.value


def watch_foreground():
    """Remember the last window you used that isn't Miles' own (its buddy or boxes can take focus)."""
    def loop():
        while True:
            h = user32.GetForegroundWindow()
            if h and _pid(h) != _OWN_PID:
                _last_external[0] = h
            time.sleep(0.3)
    threading.Thread(target=loop, daemon=True, name="fg-watch").start()


def target_window() -> int:
    h = user32.GetForegroundWindow()
    if not h or _pid(h) == _OWN_PID:
        h = _last_external[0] if _last_external[0] and user32.IsWindow(_last_external[0]) else h
    return h


# ---- the snapshot ---------------------------------------------------------------------------------------------
@dataclass
class Snapshot:
    hwnd: int = 0
    proc: str = ""
    title: str = ""
    selection: str = ""
    field_kind: str = ""          # the focused text box, if any
    field_name: str = ""
    field_text: str = ""
    editable: bool = False
    url: str = ""
    files: list = field(default_factory=list)
    hovered: str = ""
    clipboard: str = ""

    def details(self) -> str:
        out = []
        if self.url:
            out.append(f"page address: {self.url}")
        if self.files:
            out.append("files selected in Explorer: " + "; ".join(self.files[:15]) +
                       (f" (+{len(self.files) - 15} more)" if len(self.files) > 15 else ""))
        if self.editable:
            box = f"text cursor in {self.field_kind or 'a text box'}" + (f" '{self.field_name[:60]}'" if self.field_name else "")
            if self.field_text and not self.selection:
                box += f' which contains: """{_short(self.field_text, 700)}"""'
            elif not self.field_text:
                box += " (empty)"
            out.append(box)
        if self.selection:
            where = "in that editable box" if self.editable else "(read-only text, e.g. on a web page)"
            out.append(f'SELECTED TEXT {where}: """{_short(self.selection, 2500)}"""')
        if self.hovered:
            out.append(f"mouse is over: {self.hovered}")
        if self.clipboard:
            out.append(f'clipboard: """{_short(self.clipboard, 700)}"""')
        return "; ".join(out)


def _short(s: str, n: int) -> str:
    s = s.strip()
    return s if len(s) <= n else s[:n] + " …(cut)"


def snapshot(selection=False, clipboard=False, hovered=True, timeout=2.5) -> Snapshot:
    """Read the current context. Never takes longer than `timeout` (a frozen app can't hang Miles)."""
    snap = Snapshot()
    snap.hwnd = target_window()
    snap.proc, snap.title = _proc(snap.hwnd), _title(snap.hwnd)
    t = threading.Thread(target=_fill, args=(snap, selection, clipboard, hovered), daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        log.warning("context: %s was slow to answer; using what I have", snap.proc)
    return snap


def _fill(snap: Snapshot, selection: bool, clipboard: bool, hovered: bool = True):
    import uiautomation as auto
    from .browser import BROWSERS
    with auto.UIAutomationInitializerInThread():
        focused = None
        try:
            if user32.GetForegroundWindow() == snap.hwnd:
                focused = auto.GetFocusedControl()
        except Exception:
            pass
        if focused is not None:
            _field_info(snap, focused, auto)
        if snap.proc in BROWSERS:
            snap.url = _browser_url(snap.hwnd, auto)
        if selection:
            snap.selection = _uia_selection(focused, auto) if focused is not None else ""
            if not snap.selection:
                snap.selection = _copy_selection(snap, focused)
        if hovered:
            snap.hovered = _hovered(auto)
    if snap.proc == "explorer.exe":
        snap.files = _explorer_selection(snap.hwnd, snap.title)
    if clipboard:
        try:
            import pyperclip
            snap.clipboard = pyperclip.paste() or ""
        except Exception:
            pass


def _field_info(snap, c, auto):
    try:
        kind = c.ControlTypeName
        if kind not in ("EditControl", "DocumentControl", "ComboBoxControl"):
            return
        vp = None
        try:
            vp = c.GetPattern(auto.PatternId.ValuePattern)
        except Exception:
            pass
        from .browser import BROWSERS
        if vp is not None and vp.IsReadOnly:
            return
        if vp is None and kind == "DocumentControl" and snap.proc in BROWSERS:
            return                                   # the web page itself, not a text box
        snap.editable = True
        snap.field_kind = {"EditControl": "a text box", "DocumentControl": "a document",
                           "ComboBoxControl": "an input box"}[kind]
        snap.field_name = (c.Name or "").strip()
        text = ""
        if vp is not None:
            text = vp.Value or ""
        if not text:
            try:
                text = c.GetTextPattern().DocumentRange.GetText(4000) or ""
            except Exception:
                pass
        snap.field_text = text
    except Exception as e:
        log.debug("field info: %s", e)


def _uia_selection(c, auto) -> str:
    """Selected text via UI Automation (the focused element or the document around it)."""
    el = c
    for _ in range(6):
        if el is None:
            break
        try:
            tp = el.GetPattern(auto.PatternId.TextPattern)
            if tp is not None:
                text = "".join((r.GetText(20000) or "") for r in tp.GetSelection())
                if text.strip():
                    return text.strip()
        except Exception:
            pass
        try:
            el = el.GetParentControl()
        except Exception:
            break
    return ""


def _copy_selection(snap, focused) -> str:
    """Fallback for apps that don't expose their selection: Ctrl+C, read, then restore the clipboard."""
    name = ""
    try:
        name = (focused.Name or "") if focused is not None else ""
    except Exception:
        pass
    if snap.proc in _TERMINALS or "terminal" in name.lower() or user32.GetForegroundWindow() != snap.hwnd:
        return ""
    import pyautogui
    import pyperclip
    seq = user32.GetClipboardSequenceNumber()
    had_text = bool(user32.IsClipboardFormatAvailable(13))            # CF_UNICODETEXT
    try:
        old = pyperclip.paste() if had_text else None
    except Exception:
        old = None
    for vk, key in ((0x11, "ctrl"), (0x12, "alt"), (0x10, "shift"), (0x5B, "winleft")):
        if user32.GetAsyncKeyState(vk) & 0x8000:                         # still holding the hotkey? let go of it,
            pyautogui.keyUp(key)                                         # or Ctrl+C would become Ctrl+Alt+C
    pyautogui.hotkey("ctrl", "c")
    end = time.time() + 0.5
    while time.time() < end and user32.GetClipboardSequenceNumber() == seq:
        time.sleep(0.02)
    if user32.GetClipboardSequenceNumber() == seq:
        return ""                                                        # nothing was selected
    try:
        text = pyperclip.paste() or ""
    except Exception:
        text = ""
    if old is not None:
        threading.Timer(0.3, lambda: pyperclip.copy(old)).start()
    if snap.proc in _CODE_EDITORS and text.endswith("\n") and text.count("\n") == 1:
        return ""                                                        # VS Code copied the whole line, not a selection
    return text.strip()


def _browser_url(hwnd, auto) -> str:
    from .uia import find_first
    try:
        bar = find_first(auto.ControlFromHandle(hwnd), ("Address and search bar",), 50004)
        v = (bar.GetValuePattern().Value or "").strip() if bar else ""
        if v and not re.match(r"^[a-z]+://", v) and "." in v.split("/")[0] and " " not in v:
            v = "https://" + v
        return v
    except Exception:
        return ""


def _hovered(auto) -> str:
    try:
        pt = wt.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        c = auto.ControlFromPoint(pt.x, pt.y)
        if c is None or c.ProcessId == _OWN_PID:
            return ""
        name = (c.Name or "").strip()
        if not name or len(name) > 120:
            return ""
        return f"{c.ControlTypeName.replace('Control', '').lower()} '{name}'"
    except Exception:
        return ""


def _explorer_selection(hwnd, title) -> list:
    try:
        import comtypes
        import comtypes.client
        comtypes.CoInitialize()
        shell = comtypes.client.CreateObject("Shell.Application", dynamic=True)
        wins = shell.Windows()
        best = []
        for i in range(wins.Count):
            w = wins.Item(i)
            if w is None or int(w.HWND) != hwnd:
                continue
            items = w.Document.SelectedItems()
            paths = [items.Item(j).Path for j in range(items.Count)]
            if (w.LocationName or "") in title or not best:      # several tabs share a window: prefer the visible one
                best = paths
        return best
    except Exception as e:
        log.debug("explorer selection: %s", e)
        return []
