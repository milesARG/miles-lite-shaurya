"""Read and click things inside any app window through Windows UI Automation.

This is how Miles can "see" buttons/links/fields by name and press them,
e.g. "click Send", "press the Sign in button", "what's on my screen".
"""
import difflib
import time

from .util import log

_CLICKABLE = {"ButtonControl", "HyperlinkControl", "MenuItemControl", "TabItemControl", "ListItemControl",
              "CheckBoxControl", "RadioButtonControl", "ComboBoxControl", "TreeItemControl",
              "SplitButtonControl", "EditControl", "DataItemControl", "MenuBarControl"}
_TEXT = {"TextControl", "DocumentControl", "HeaderItemControl"}


def _auto():
    import uiautomation as auto
    auto.SetGlobalSearchTimeout(1)
    return auto


_UIA_NAME, _UIA_TYPE, _BUTTON = 30005, 30003, 50000


def find_first(root, names=(), control_type=None, substring=False):
    """Native (fast) search of a window's whole tree. names: exact names (or substrings if substring=True)."""
    auto = _auto()
    from uiautomation import uiautomation as _impl
    uia = _impl._AutomationClient.instance().IUIAutomation
    conds = []
    for n in names:
        if substring:
            conds.append(uia.CreatePropertyConditionEx(_UIA_NAME, n, 2 | 1))   # match substring, ignore case
        else:
            conds.append(uia.CreatePropertyCondition(_UIA_NAME, n))
    cond = conds[0] if len(conds) == 1 else uia.CreateOrConditionFromArray(conds) if conds else uia.CreateTrueCondition()
    if control_type is not None:
        cond = uia.CreateAndCondition(cond, uia.CreatePropertyCondition(_UIA_TYPE, control_type))
    try:
        el = root.Element.FindFirst(4, cond)          # 4 = TreeScope_Descendants
    except Exception:
        return None
    return auto.Control.CreateControlFromElement(el) if el else None


def _walk(include_text=False, budget=3.0, limit=400):
    auto = _auto()
    win = auto.GetForegroundControl()
    try:
        win = win.GetTopLevelControl() or win
    except Exception:
        pass
    out = []
    t0 = time.time()
    for c, _depth in auto.WalkControl(win, includeTop=False, maxDepth=25):
        if time.time() - t0 > budget or len(out) >= limit:
            break
        try:
            kind = c.ControlTypeName
            if kind not in _CLICKABLE and not (include_text and kind in _TEXT):
                continue
            name = (c.Name or "").strip()
            if not name or c.IsOffscreen:
                continue
            r = c.BoundingRectangle
            if r.width() <= 0 or r.height() <= 0:
                continue
            out.append((c, kind.replace("Control", ""), name[:120]))
        except Exception:
            continue
    return win, out


def list_elements(include_text=False) -> str:
    auto = _auto()
    with auto.UIAutomationInitializerInThread():
        win, items = _walk(include_text)
        title = win.Name or "(untitled window)"
        if not items:
            return f"Active window: '{title}'. I couldn't read any named elements in it."
        seen, lines = set(), []
        for _, kind, name in items:
            key = (kind, name)
            if key in seen:
                continue
            seen.add(key)
            lines.append(f"{kind}: {name}")
            if len(lines) >= 80:
                break
        return f"Active window: '{title}'. Elements:\n" + "\n".join(lines)


def click_element(name: str, kind: str = "", action: str = "click", text: str = "") -> str:
    auto = _auto()
    with auto.UIAutomationInitializerInThread():
        _, items = _walk(include_text=True)
        target = name.lower().strip()
        kind = (kind or "").lower()
        best, best_s = None, 0.0
        for c, k, n in items:
            nl = n.lower()
            if nl == target:
                s = 1.0
            elif nl.startswith(target):
                s = 0.9
            elif target in nl:
                s = 0.8 - min(len(nl) - len(target), 40) * 0.004
            else:
                s = difflib.SequenceMatcher(None, target, nl).ratio() * 0.75
            if kind and kind in k.lower():
                s += 0.1
            if k in ("Text", "Document"):
                s -= 0.05
            if s > best_s:
                best, best_s = (c, k, n), s
        if not best or best_s < 0.55:
            names = ", ".join(sorted({n for _, _, n in items})[:25])
            return f"No element like '{name}' found. Visible elements include: {names}"
        c, k, n = best
        try:
            if action == "type":
                try:
                    c.GetValuePattern().SetValue(text)
                except Exception:
                    c.Click(simulateMove=False)
                    auto.SendKeys(text, interval=0.005)
                return f"Typed into {k} '{n}'."
            if action == "double_click":
                c.DoubleClick(simulateMove=False)
            elif action == "right_click":
                c.RightClick(simulateMove=False)
            elif action == "focus":
                c.SetFocus()
            else:
                try:
                    c.GetInvokePattern().Invoke()
                except Exception:
                    c.Click(simulateMove=False)
            return f"{action.replace('_', ' ').capitalize()}ed {k} '{n}'."
        except Exception as e:
            log.warning("UIA action failed: %s", e)
            return f"Found '{n}' but couldn't {action} it: {e}"
