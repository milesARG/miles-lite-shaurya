"""Browser tab control for Chromium browsers (Brave, Chrome, Edge, Opera, Vivaldi).

Every tab in the tab strip is a UI Automation TabItem with its own "Close" and
"Mute tab" buttons, so a single tab can be closed/muted/selected by name without
touching the rest of the window.
"""
import difflib
import re
import time

import psutil

from .util import log

BROWSERS = {"brave.exe", "chrome.exe", "msedge.exe", "opera.exe", "vivaldi.exe", "chromium.exe", "arc.exe"}


def _auto():
    import uiautomation as auto
    auto.SetGlobalSearchTimeout(1)
    return auto


def clean_title(name: str) -> str:
    name = re.sub(r"\s+-\s+Memory usage\s+-\s+[\d.,]+\s*[KMG]B.*$", "", name or "")
    name = re.sub(r"^\(\d+\+?\)\s*", "", name)            # "(2) YouTube" -> "YouTube"
    return name.strip()


def _proc_name(pid):
    try:
        return psutil.Process(pid).name().lower()
    except Exception:
        return ""


def browser_windows():
    """Top-level browser windows, foreground-most first."""
    import ctypes
    auto = _auto()
    fg = ctypes.windll.user32.GetForegroundWindow()
    wins = []
    for w in auto.GetRootControl().GetChildren():
        try:                                   # windows can vanish mid-scan (COM errors): skip them
            if w.Name and _proc_name(w.ProcessId) in BROWSERS:
                wins.append(w)
        except Exception:
            continue
    wins.sort(key=lambda w: w.NativeWindowHandle != fg)
    return wins


def is_browser_foreground() -> bool:
    import ctypes
    import ctypes.wintypes as wt
    pid = wt.DWORD()
    ctypes.windll.user32.GetWindowThreadProcessId(ctypes.windll.user32.GetForegroundWindow(), ctypes.byref(pid))
    return _proc_name(pid.value) in BROWSERS


def tabs():
    """[(window, tabitem, title, selected)] across all browser windows."""
    auto = _auto()
    out = []
    for w in browser_windows():
        try:
            for c, _ in auto.WalkControl(w, includeTop=False, maxDepth=12):
                if c.ControlTypeName == "TabItemControl":
                    sel = c.GetPattern(auto.PatternId.SelectionItemPattern)
                    out.append((w, c, clean_title(c.Name), bool(sel and sel.IsSelected)))
        except Exception as e:
            log.debug("tab scan failed: %s", e)
    return out


def _best(all_tabs, title):
    t = title.lower().strip()
    t = re.sub(r"\b(the|tab|tabs|page|website|site|one|that|this)\b", " ", t)
    t = " ".join(t.split())
    if not t:
        return None
    best, best_s = None, 0.0
    for item in all_tabs:
        name = item[2].lower()
        if t == name:
            s = 1.0
        elif t in name:
            s = 0.85 + (0.1 if name.startswith(t) else 0)
        else:
            s = difflib.SequenceMatcher(None, t, name).ratio() * 0.8
            if all(w in name for w in t.split()):
                s = max(s, 0.75)
        if s > best_s:
            best, best_s = item, s
    return best if best_s >= 0.55 else None


def _button(tab, name):
    for k in tab.GetChildren():
        if k.ControlTypeName == "ButtonControl" and (k.Name or "").lower().startswith(name.lower()):
            return k
    return None


def _focus(win):
    try:
        from .tools import focus_hwnd
        focus_hwnd(win.NativeWindowHandle)
        time.sleep(0.12)
    except Exception as e:
        log.debug("focus browser failed: %s", e)


def tab_action(action: str, title: str = "") -> str:
    auto = _auto()
    import pyautogui
    with auto.UIAutomationInitializerInThread():
        all_tabs = tabs()
        if not all_tabs:
            return "No browser window is open."
        wins = browser_windows()
        main_win = wins[0]

        if action == "list":
            return "Open tabs: " + "; ".join(f"{t[2]}{' (current)' if t[3] else ''}" for t in all_tabs[:30])

        if action in ("new", "reopen", "next", "previous"):
            _focus(main_win)
            keys = {"new": ("ctrl", "t"), "reopen": ("ctrl", "shift", "t"), "next": ("ctrl", "tab"),
                    "previous": ("ctrl", "shift", "tab")}[action]
            pyautogui.hotkey(*keys)
            return {"new": "Opened a new tab.", "reopen": "Reopened the last closed tab.",
                    "next": "Next tab.", "previous": "Previous tab."}[action]

        # actions on one specific tab
        if title:
            target = _best(all_tabs, title)
            if not target:
                return f"No open tab matches '{title}'. Open tabs: " + "; ".join(t[2] for t in all_tabs[:15])
        else:                                              # the current tab of the front browser window
            target = next((t for t in all_tabs if t[3] and t[0].NativeWindowHandle == main_win.NativeWindowHandle),
                          None) or next((t for t in all_tabs if t[3]), None)
            if not target:
                return "Couldn't tell which tab is current."
        win, tab, name, _ = target

        if action == "close":
            btn = _button(tab, "Close")
            if btn:
                btn.GetInvokePattern().Invoke()
            else:
                tab.GetSelectionItemPattern().Select()
                _focus(win)
                pyautogui.hotkey("ctrl", "w")
            left = len([t for t in all_tabs if t[0].NativeWindowHandle == win.NativeWindowHandle]) - 1
            return f"Closed the '{name}' tab. {left} tab{'s' if left != 1 else ''} still open."
        if action in ("mute", "unmute"):
            btn = _button(tab, "Mute") or _button(tab, "Unmute")
            if not btn:
                return f"The '{name}' tab has no mute button."
            current = (btn.Name or "").lower()
            if (action == "mute") == current.startswith("mute"):
                btn.GetInvokePattern().Invoke()
            return f"{'Muted' if action == 'mute' else 'Unmuted'} the '{name}' tab."
        if action == "switch":
            try:
                tab.GetSelectionItemPattern().Select()
            except Exception:
                tab.Click(simulateMove=False)
            _focus(win)
            return f"Switched to '{name}'."
        if action == "reload":
            tab.GetSelectionItemPattern().Select()
            _focus(win)
            pyautogui.press("f5")
            return f"Reloaded '{name}'."
        return f"Unknown tab action '{action}'."
