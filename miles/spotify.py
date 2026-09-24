"""Spotify desktop control through UI Automation (no API keys, no login).

play(query): opens spotify:search:<query>, waits for the fresh "Search results"
grid and presses the top result's own Play button. Player-bar buttons
(Play/Pause/Next/Previous/Shuffle/Repeat) are pressed directly too.
"""
import os
import re
import time
import urllib.parse

import psutil

from .util import log


def _auto():
    import uiautomation as auto
    auto.SetGlobalSearchTimeout(0.5)
    return auto


_STOP = {"the", "a", "an", "of", "for", "you", "and", "to", "in", "on", "my", "me", "by", "feat", "ft", "song",
         "songs", "music", "some", "by", "is", "it", "i"}


def _norm(s: str) -> str:
    s = (s or "").lower().replace("+", " ")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", s).split())


def window():
    auto = _auto()
    for w in auto.GetRootControl().GetChildren():
        try:
            if w.Name and psutil.Process(w.ProcessId).name().lower() == "spotify.exe":
                return w
        except Exception:
            pass
    return None


def running() -> bool:
    return any((p.info["name"] or "").lower() == "spotify.exe" for p in psutil.process_iter(["name"]))


def _ensure(timeout=15):
    w = window()
    if w:
        return w, False
    os.startfile("spotify:")
    end = time.time() + timeout
    while time.time() < end:
        time.sleep(0.4)
        w = window()
        if w:
            return w, True
    return None, True


def _first_row(w, wait=0.05):
    grid = w.DataGridControl(Name="Search results", searchDepth=30)
    if not grid.Exists(wait):
        return None
    rows = grid.GetChildren()
    return rows[0] if rows else None


def _row_info(row):
    """-> (play_button, title, kind, artist). The row's own Name can be stale; its children are current."""
    auto = _auto()
    play_btn, subtitle, title = None, "", ""
    for c, _ in auto.WalkControl(row, includeTop=False, maxDepth=7):
        name = c.Name or ""
        if not title and c.ControlTypeName == "HyperlinkControl" and name:
            title = name
        if play_btn is None and c.ControlTypeName == "ButtonControl" and name.startswith("Play"):
            play_btn = c
        if not subtitle and " • " in name and not name.lower().startswith(("more options", "song more")):
            subtitle = name
    kind, _, who = subtitle.partition(" • ")
    return play_btn, title or row.Name, kind.strip(), who.strip()


def _is_playing(w, wait=0.0) -> bool:
    end = time.time() + wait
    while True:
        bar = w.GroupControl(Name="Player controls", searchDepth=25)
        if bar.Exists(0.1) and bar.ButtonControl(Name="Pause", searchDepth=3).Exists(0.05):
            return True
        if time.time() >= end:
            return False
        time.sleep(0.15)


def play(query: str) -> str:
    """Search Spotify, press Play on the top result and verify that music actually started."""
    auto = _auto()
    with auto.UIAutomationInitializerInThread():
        w, launched = _ensure()
        if not w:
            return "Spotify didn't start."
        before = _first_row(w, wait=1.0)
        before_name = _row_info(before)[1] if before else None
        os.startfile("spotify:search:" + urllib.parse.quote(query))
        want = _norm(query)
        want_words = {x for x in want.split() if len(x) > 1 and x not in _STOP}
        t0 = time.time()
        matched_at = None
        deadline = 16 if launched else 10
        while time.time() - t0 < deadline:
            combo = w.ComboBoxControl(Name="What do you want to play?", searchDepth=25)
            try:
                val = combo.GetValuePattern().Value if combo.Exists(0.05) else ""
            except Exception:
                val = ""
            if _norm(val) == want:
                matched_at = matched_at or time.time()
                row = _first_row(w)
                if not row:
                    time.sleep(0.1)
                    continue
                btn, title, kind, who = _row_info(row)
                words = set(_norm(f"{title} {who}").split())
                changed = before_name is None or title != before_name
                # results genuinely for this search: a real query word in the top result, or (after a
                # while) a list that has at least changed since before the search
                fresh = bool(want_words & words) or (changed and time.time() - matched_at > 2.5) \
                    or (not want_words and changed)
                if fresh and btn:
                    k = kind.lower().replace("explicit", "").strip()
                    desc = title + (f" by {who}" if k in ("song", "music video") and who
                                    else f" ({k})" if k else "")
                    prev_title = w.Name or ""
                    for attempt in range(2):
                        try:
                            btn.GetInvokePattern().Invoke()
                        except Exception:
                            pass
                        # wait for the window title ("Artist - Song") to show the new track
                        end = time.time() + 3.0
                        title = ""
                        while time.time() < end:
                            title = (window() or w).Name or ""
                            if " - " in title and title != prev_title:
                                break
                            time.sleep(0.15)
                        if (" - " in title and title != prev_title) or _is_playing(w, wait=0.5):
                            if " - " in title:
                                artist, _, song = title.partition(" - ")
                                desc = f"{song} by {artist}"
                            log.info("spotify: playing %s in %.2fs", desc, time.time() - t0)
                            return f"Playing {desc} on Spotify."
                        row = _first_row(w)                        # element went stale: find it again
                        if row:
                            btn = _row_info(row)[0]
                        if not btn:
                            break
                    return f"I found {desc} on Spotify but playback didn't start."
            time.sleep(0.1)
        return f"Spotify opened the search for '{query}' but I couldn't start it in time."


def control(action: str) -> str:
    """resume / pause / next / previous / shuffle / repeat via the player bar."""
    auto = _auto()
    names = {"resume": "Play", "play": "Play", "pause": "Pause", "next": "Next", "previous": "Previous",
             "shuffle": "Enable Shuffle", "repeat": "Enable repeat"}
    with auto.UIAutomationInitializerInThread():
        w, launched = _ensure()
        if not w:
            return "Spotify isn't running."
        end = time.time() + (10 if launched else 2)
        while time.time() < end:
            bar = w.GroupControl(Name="Player controls", searchDepth=25)
            if bar.Exists(0.2):
                labels = [c.Name for c in bar.GetChildren()]
                if action in ("resume", "play") and "Pause" in labels:
                    return "Spotify is already playing."
                if action == "pause" and "Play" in labels:
                    return "Spotify is already paused."
                btn = bar.ButtonControl(Name=names.get(action, action), searchDepth=3)
                if btn.Exists(0.2):
                    btn.GetInvokePattern().Invoke()
                    return {"resume": "Resumed Spotify.", "play": "Resumed Spotify.", "pause": "Paused Spotify.",
                            "next": "Skipped to the next track.", "previous": "Back to the previous track.",
                            "shuffle": "Shuffle toggled.", "repeat": "Repeat toggled."}.get(action, "Done.")
            time.sleep(0.3)
        return f"Couldn't find Spotify's {action} button."


def now_playing() -> str:
    auto = _auto()
    with auto.UIAutomationInitializerInThread():
        w = window()
        if not w:
            return "Spotify isn't running."
        title = w.Name or ""
        if " - " in title:                     # while playing, the window title is "Artist - Song"
            artist, _, song = title.partition(" - ")
            return f"Now playing {song} by {artist}."
        return "Nothing is playing on Spotify right now."        # paused: title goes back to "Spotify ..."
