"""Shared helpers: paths, logging, PowerShell, Windows known folders."""
import logging
import os
import re
import subprocess
import sys
import winreg
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
NO_WINDOW = 0x08000000

log = logging.getLogger("miles")


def setup_logging():
    handlers = [logging.FileHandler(DATA / "miles.log", encoding="utf-8")]
    if sys.stdout is not None:
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("httpx", "httpcore", "urllib3", "faster_whisper", "comtypes", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    for noisy in ("phonemizer", "kokoro_onnx"):
        logging.getLogger(noisy).setLevel(logging.ERROR)


def run_ps(script: str, timeout: float = 30):
    """Run a PowerShell snippet hidden. Returns (returncode, stdout, stderr)."""
    full = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8; $ProgressPreference='SilentlyContinue'; "
            + script)
    r = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", full],
        capture_output=True, timeout=timeout, creationflags=NO_WINDOW)
    return (r.returncode, r.stdout.decode("utf-8", "replace").strip(),
            r.stderr.decode("utf-8", "replace").strip())


def run_hidden(args, **kw):
    return subprocess.Popen(args, creationflags=NO_WINDOW, **kw)


# ---- Known folders (handles OneDrive-redirected Desktop/Documents) ---------
_SHELL_FOLDERS = {
    "desktop": "Desktop",
    "documents": "Personal",
    "pictures": "My Pictures",
    "music": "My Music",
    "videos": "My Video",
    "downloads": "{374DE290-123F-4565-9164-39C4925E467B}",
}
_FOLDER_ALIASES = {
    "desktop": "desktop", "my desktop": "desktop",
    "documents": "documents", "document": "documents", "my documents": "documents", "docs": "documents",
    "downloads": "downloads", "download": "downloads", "my downloads": "downloads",
    "pictures": "pictures", "picture": "pictures", "photos": "pictures", "my pictures": "pictures",
    "music": "music", "my music": "music", "songs": "music",
    "videos": "videos", "video": "videos", "my videos": "videos", "movies": "videos",
    "home": "home", "user folder": "home", "my folder": "home",
}
_known_cache = {}


def known_folder(key: str) -> Path | None:
    key = _FOLDER_ALIASES.get(key.lower().strip(), key.lower().strip())
    if key in _known_cache:
        return _known_cache[key]
    path = None
    if key == "home":
        path = Path.home()
    elif key in _SHELL_FOLDERS:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as k:
                val, _ = winreg.QueryValueEx(k, _SHELL_FOLDERS[key])
                path = Path(os.path.expandvars(val))
        except OSError:
            path = Path.home() / key.capitalize()
    _known_cache[key] = path
    return path


def folder_alias(text: str) -> Path | None:
    t = re.sub(r"\b(the|folder|directory|my)\b", " ", text.lower())
    t = " ".join(t.split())
    if t in _FOLDER_ALIASES or text.lower().strip() in _FOLDER_ALIASES:
        return known_folder(_FOLDER_ALIASES.get(t) or _FOLDER_ALIASES[text.lower().strip()])
    return None


def resolve_path(p: str) -> Path:
    """Turn 'downloads/report.pdf', '~/x', '%APPDATA%', 'D drive' etc. into a real path."""
    p = (p or "").strip().strip('"').strip("'")
    m = re.fullmatch(r"([a-zA-Z])\s*(?:drive|:)?\s*", p)
    if m:
        return Path(f"{m.group(1).upper()}:\\")
    p = os.path.expandvars(os.path.expanduser(p))
    norm = p.replace("\\", "/")
    first, _, rest = norm.partition("/")
    kf = folder_alias(first)
    if kf is not None:
        return kf / rest if rest else kf
    path = Path(p)
    if not path.is_absolute():
        path = Path.home() / path
    return path


def user_search_roots():
    roots = [known_folder(k) for k in ("desktop", "downloads", "documents", "pictures", "music", "videos")]
    roots.append(Path.home())
    seen, out = set(), []
    for r in roots:
        if r and r.exists() and str(r).lower() not in seen:
            seen.add(str(r).lower())
            out.append(r)
    return out
