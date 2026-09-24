"""Finds and launches any installed app by (spoken) name - no paths needed.

Index sources: Start menu (desktop + Store apps), desktop shortcuts, apps in
%LOCALAPPDATA%\\Programs, and command-line tools on PATH. Cached to disk and
refreshed in the background, so lookups are instant.
"""
import difflib
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from .util import DATA, NO_WINDOW, known_folder, log, run_ps

CACHE = DATA / "apps.json"

# Things people say -> what the app is actually called.
ALIASES = {
    "chrome": "google chrome", "google": "google chrome", "browser": "google chrome",
    "edge": "microsoft edge", "vs code": "visual studio code", "vscode": "visual studio code",
    "code": "visual studio code", "visual code": "visual studio code",
    "cloud": "claude", "cloud code": "claude code", "cloud ai": "claude", "claude ai": "claude",
    "cloud desktop": "claude", "clod": "claude", "claud": "claude",
    "word": "word", "ms word": "word", "microsoft word": "word", "excel": "excel",
    "powerpoint": "powerpoint", "power point": "powerpoint", "outlook": "outlook",
    "file explorer": "file explorer", "explorer": "file explorer", "files": "file explorer",
    "my files": "file explorer", "this pc": "file explorer", "my computer": "file explorer",
    "terminal": "terminal", "command prompt": "command prompt", "cmd": "command prompt",
    "control panel": "control panel", "task manager": "task manager", "settings": "settings",
    "calculator": "calculator", "calc": "calculator", "notepad": "notepad", "paint": "paint",
    "camera": "camera", "store": "microsoft store", "microsoft store": "microsoft store",
    "whats app": "whatsapp", "what's app": "whatsapp", "spotify music": "spotify",
    "obs": "obs studio", "vlc": "vlc media player", "photoshop": "adobe photoshop",
}

# Built into Windows; always available even if the Start menu index misses them.
BUILTIN = {
    "notepad": "notepad.exe", "calculator": "calc.exe", "paint": "mspaint.exe",
    "command prompt": "cmd.exe", "powershell": "powershell.exe", "terminal": "wt.exe",
    "file explorer": "explorer.exe", "task manager": "taskmgr.exe", "control panel": "control.exe",
    "settings": "ms-settings:", "microsoft store": "ms-windows-store:", "camera": "microsoft.windows.camera:",
    "snipping tool": "ms-screenclip:", "clock": "ms-clock:", "alarms": "ms-clock:",
    "registry editor": "regedit.exe", "device manager": "devmgmt.msc", "disk management": "diskmgmt.msc",
    "services": "services.msc", "event viewer": "eventvwr.msc", "resource monitor": "resmon.exe",
}

# CLI tools worth launching in a terminal when asked by name.
CLI_TOOLS = {"claude": "Claude Code (CLI)", "python": "Python", "node": "Node.js", "ollama": "Ollama",
             "git": "Git Bash", "wsl": "WSL", "ubuntu": "Ubuntu", "ipython": "IPython", "gemini": "Gemini CLI"}

_BAD = ("uninstall", "readme", "read me", "help", "documentation", "release notes", "website",
        "support center", "license", "manual", "changelog", "what's new", "repair")


def _norm(s: str) -> str:
    s = s.lower().replace("™", "").replace("®", "").replace("&", " and ")
    s = re.sub(r"[^a-z0-9+#]+", " ", s)
    return " ".join(s.split())


class AppIndex:
    def __init__(self):
        self.apps: list[dict] = []
        self._lock = threading.Lock()
        self.ready = threading.Event()
        try:
            self.apps = json.loads(CACHE.read_text(encoding="utf-8"))
            self.ready.set()
        except Exception:
            pass
        threading.Thread(target=self.refresh, daemon=True, name="app-index").start()

    # ---- indexing ----------------------------------------------------------
    def refresh(self):
        t0 = time.time()
        apps = []
        try:
            _, out, _ = run_ps("Get-StartApps | Select-Object Name,AppID | ConvertTo-Json -Compress", 60)
            data = json.loads(out) if out else []
            if isinstance(data, dict):
                data = [data]
            for it in data:
                if it.get("Name") and it.get("AppID"):
                    apps.append({"name": it["Name"], "target": it["AppID"], "kind": "start"})
        except Exception as e:
            log.warning("Start menu index failed: %s", e)

        for d in (known_folder("desktop"), Path(os.environ.get("PUBLIC", r"C:\Users\Public")) / "Desktop"):
            try:
                for f in d.iterdir():
                    if f.suffix.lower() in (".lnk", ".url", ".exe"):
                        apps.append({"name": f.stem, "target": str(f), "kind": "file"})
            except Exception:
                pass

        progs = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs"
        try:
            for sub in progs.iterdir():
                if sub.is_dir():
                    for exe in list(sub.glob("*.exe"))[:3]:
                        if not any(b in exe.stem.lower() for b in ("unins", "update", "crash", "helper")):
                            apps.append({"name": exe.stem, "target": str(exe), "kind": "file"})
        except Exception:
            pass

        for cli, label in CLI_TOOLS.items():
            path = shutil.which(cli)
            if path:
                apps.append({"name": label, "target": cli, "kind": "cli", "cli": cli})

        with self._lock:
            self.apps = apps
        self.ready.set()
        try:
            CACHE.write_text(json.dumps(apps), encoding="utf-8")
        except Exception:
            pass
        log.info("App index: %d apps in %.1fs", len(apps), time.time() - t0)

    # ---- lookup ------------------------------------------------------------
    def _score(self, q: str, qw: set, app: dict) -> float:
        name = _norm(app["name"])
        if not name:
            return 0
        nw = set(name.split())
        if name == q:
            s = 100
        elif name.startswith(q + " "):
            s = 93 - min(len(name) - len(q), 20) * 0.3
        elif nw <= qw:                       # "claude code" contains app "claude"
            s = 88 - (len(qw) - len(nw)) * 3
        elif qw <= nw:                       # "studio code" inside "visual studio code"
            s = 84 - (len(nw) - len(qw)) * 2
        elif q in name:
            s = 76
        elif len(q) >= 2 and q.replace(" ", "") == "".join(w[0] for w in name.split()):
            s = 80                           # acronym: "vsc", "obs"
        else:
            s = difflib.SequenceMatcher(None, q, name).ratio() * 72
        if any(b in name for b in _BAD):
            s -= 30
        if app["kind"] == "cli":
            s -= 4
        return s

    def find(self, query: str, min_score: float = 58):
        """Best match for a spoken app name -> (app, score) or (None, 0)."""
        self.ready.wait(timeout=15)
        q = _norm(query)
        q = re.sub(r"^(the|my|a|an)\s+", "", q)
        q = re.sub(r"\s+(app|application|program|software)$", "", q)
        q = ALIASES.get(q, q)
        if not q:
            return None, 0
        qw = set(q.split())

        # "claude code" is the CLI when it's installed, otherwise the Claude app.
        if q == "claude code" and shutil.which("claude"):
            return {"name": "Claude Code", "target": "claude", "kind": "cli", "cli": "claude"}, 100

        with self._lock:
            apps = list(self.apps)
        best, best_s = None, 0
        for a in apps:
            s = self._score(q, qw, a)
            if s > best_s:
                best, best_s = a, s
        if q in BUILTIN and best_s < 95:
            return {"name": q.title(), "target": BUILTIN[q], "kind": "builtin"}, 99
        if best_s >= min_score:
            return best, best_s
        return None, 0

    def suggestions(self, query: str, n: int = 4):
        q = _norm(query)
        names = [a["name"] for a in self.apps]
        return difflib.get_close_matches(query, names, n=n, cutoff=0.4) or \
            [a["name"] for a in self.apps if any(w in _norm(a["name"]) for w in q.split())][:n]

    @staticmethod
    def launch(app: dict, args: str = ""):
        kind, target = app["kind"], app["target"]
        if kind == "start":
            subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{target}"], creationflags=NO_WINDOW)
        elif kind == "cli":
            home = str(Path.home())
            cmd = app.get("cli", target) + (f" {args}" if args else "")
            if shutil.which("wt"):
                subprocess.Popen(["wt.exe", "-d", home, "cmd", "/k", cmd], creationflags=NO_WINDOW)
            else:
                subprocess.Popen(f'start "" /D "{home}" cmd /k {cmd}', shell=True, creationflags=NO_WINDOW)
        elif kind == "builtin" and target.endswith(":"):
            os.startfile(target)
        elif kind == "builtin":
            subprocess.Popen(f'start "" {target} {args}', shell=True, creationflags=NO_WINDOW)
        else:
            os.startfile(target)

    def names(self):
        return sorted({a["name"] for a in self.apps})
