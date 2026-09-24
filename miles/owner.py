"""Who this copy of Miles belongs to, and who made it.

This copy was built by Arnav for Shaurya. The first time it starts it binds itself to that PC; copied to any
other PC it won't start, and says whose it is.
"""
import ctypes
import hashlib
import winreg

from .util import DATA, log

CREATOR = "Arnav"
OWNER = "Shaurya"
CREDIT = f"Made by {CREATOR} for {OWNER}"

_LOCK = DATA / "owner.lock"
_SALT = "miles-lite/arnav->shaurya/v1"


def _machine_id() -> str:
    """Windows' own ID for this installation (changes only if Windows is reinstalled)."""
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography", 0,
                        winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as k:
        return winreg.QueryValueEx(k, "MachineGuid")[0]


def _seal(machine: str) -> str:
    return hashlib.sha256(f"{_SALT}|{CREATOR}|{OWNER}|{machine}".encode()).hexdigest()


def first_run() -> bool:
    return not _LOCK.exists()


def check() -> str | None:
    """None if Miles may run here, otherwise the reason it won't. Binds to this PC on the very first start."""
    try:
        seal = _seal(_machine_id())
    except OSError:
        return None                                  # can't read the ID: don't lock the owner out
    if not _LOCK.exists():
        _LOCK.write_text(seal, encoding="utf-8")
        ctypes.windll.kernel32.SetFileAttributesW(str(_LOCK), 0x2)     # hidden
        log.info("bound to this PC for %s", OWNER)
        return None
    if _LOCK.read_text(encoding="utf-8").strip() == seal:
        return None
    return (f"This copy of Miles was made by {CREATOR}, personally for {OWNER}, and only runs on "
            f"{OWNER}'s laptop.\n\nIf you'd like your own Miles, ask {CREATOR}.")
