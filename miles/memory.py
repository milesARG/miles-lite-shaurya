"""Miles' long-term memory, kept in two small plain files so it costs almost no RAM.

- Facts (data/memory.json): lasting things about the user - name, preferences, people, habits.
  The AI sees them at the start of every conversation and adds to them as it learns.
- Conversation log (data/conversations.jsonl): every exchange with a timestamp. It's read from disk
  only when needed: the last few exchanges carry over after a restart, and recall() searches it.
"""
import collections
import datetime as dt
import difflib
import json
import re
import threading

from .util import DATA, log

FACTS_FILE = DATA / "memory.json"
LOG_FILE = DATA / "conversations.jsonl"
MAX_LOG_BYTES = 8_000_000
_lock = threading.RLock()

_STOP = set("""a an the and or but so to of in on at for from with about into over by is are was were be been am do does
did done have has had i me my mine you your yours he she it its we our they them their this that these those what
which who whom when where why how can could would should will shall may might must just please tell told say said
ask asked remember recall know knew think any some all there here then than also very really yes no not dont
yesterday today tonight earlier ago last time week month again ever""".split())


def _words(text: str) -> set:
    out = set()
    for w in re.findall(r"[a-z0-9@._'-]+", text.lower()):
        w = w.strip("._'-")
        if len(w) < 2 or w in _STOP:
            continue
        out.add(w[:-1] if len(w) > 4 and w.endswith("s") else w)
    return out


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


# ---- facts ----------------------------------------------------------------------
def facts() -> list[dict]:
    try:
        raw = json.loads(FACTS_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except Exception as e:
        log.warning("memory.json unreadable: %s", e)
        return []
    out = []
    for f in raw if isinstance(raw, list) else []:
        if isinstance(f, str) and f.strip():
            out.append({"text": f.strip(), "added": ""})
        elif isinstance(f, dict) and str(f.get("text", "")).strip():
            out.append({"text": str(f["text"]).strip(), "added": f.get("added", "")})
    return out


def fact_texts() -> list[str]:
    return [f["text"] for f in facts()]


def _save(items):
    FACTS_FILE.write_text(json.dumps(items, indent=1, ensure_ascii=False), encoding="utf-8")


def save_texts(texts):
    """Replace all facts (used by the Memory window)."""
    with _lock:
        old = {f["text"]: f.get("added", "") for f in facts()}
        _save([{"text": t, "added": old.get(t) or _now()[:10]} for t in texts if t.strip()])


def _subject(text: str) -> str:
    """"Tulsi's email is x" -> "tulsi's email"; "Favourite colour: blue" -> "favourite colour"."""
    m = re.match(r"^(.{3,60}?)\s*(?::|=|\bis\b|\bare\b)", text.lower().strip())
    if not m:
        return ""
    return re.sub(r"^(the user'?s?|user'?s?|sir'?s?|boss'?s?|his|her|their|my)\s+", "", m.group(1)).strip()


def add(text: str) -> str:
    text = " ".join(str(text).split()).strip().rstrip(".")
    if len(text) < 3:
        return "Nothing to remember."
    with _lock:
        items = facts()
        subj = _subject(text)
        for f in items:
            same_subject = bool(subj) and _subject(f["text"]) == subj
            similar = difflib.SequenceMatcher(None, f["text"].lower(), text.lower()).ratio() > 0.85
            if same_subject or similar:
                old, f["text"], f["added"] = f["text"], text, _now()[:10]
                _save(items)
                return "Already remembered." if old == text else f"Updated memory: {text} (it was: {old})."
        items.append({"text": text, "added": _now()[:10]})
        _save(items)
    log.info("memory + %s", text)
    return f"Remembered: {text}."


def forget(query: str) -> list[str]:
    """Remove facts matching `query`; returns what was removed."""
    q = query.lower().strip()
    everything = q in ("everything", "all", "all facts", "everything about me", "all memories")
    qw = _words(q)
    with _lock:
        keep, gone = [], []
        for f in facts():
            fw = _words(f["text"])
            hit = everything or q in f["text"].lower() or (qw and len(qw & fw) / len(qw) >= 0.6)
            (gone if hit else keep).append(f)
        if gone:
            _save(keep)
    return [f["text"] for f in gone]


# ---- conversation log -------------------------------------------------------------
def log_exchange(user: str, reply: str, actions=()):
    if not user.strip():
        return
    rec = {"t": _now(), "user": user.strip()[:2000], "miles": (reply or "").strip()[:2000]}
    if actions:
        rec["did"] = list(actions)[:12]
    with _lock:
        try:
            with LOG_FILE.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            if LOG_FILE.stat().st_size > MAX_LOG_BYTES:        # keep the newest half
                lines = LOG_FILE.read_text(encoding="utf-8").splitlines(True)
                LOG_FILE.write_text("".join(lines[len(lines) // 2:]), encoding="utf-8")
        except OSError as e:
            log.warning("conversation log: %s", e)


def _records():
    try:
        with LOG_FILE.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    yield json.loads(line)
                except ValueError:
                    pass
    except FileNotFoundError:
        return


def recent(n=5, hours=36) -> list[dict]:
    cutoff = (dt.datetime.now() - dt.timedelta(hours=hours)).isoformat(timespec="seconds")
    return list(collections.deque((r for r in _records() if r.get("t", "") >= cutoff), maxlen=n))


def _when_range(text: str):
    """'yesterday' / 'today' / 'this week' / 'last month' / '23 sep' -> (first_day, last_day) or None."""
    t = (text or "").lower()
    today = dt.date.today()
    if "day before yesterday" in t:
        d = today - dt.timedelta(days=2)
        return d, d
    if "yesterday" in t or "last night" in t:
        d = today - dt.timedelta(days=1)
        return d, d
    if re.search(r"\b(today|this morning|tonight|this evening|earlier)\b", t):
        return today, today
    if re.search(r"\b(this|last|past) week\b", t):
        return today - dt.timedelta(days=7), today
    if re.search(r"\b(this|last|past) month\b", t):
        return today - dt.timedelta(days=31), today
    m = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", t)
    if m:
        d = dt.date.fromisoformat(m.group(1))
        return d, d
    m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*|"
                  r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+(\d{1,2})(?:st|nd|rd|th)?\b", t)
    if m:
        day, mon = (m.group(1), m.group(2)) if m.group(1) else (m.group(4), m.group(3))
        try:
            d = dt.datetime.strptime(f"{int(day)} {mon} {today.year}", "%d %b %Y").date()
            if d > today:
                d = d.replace(year=today.year - 1)
            return d, d
        except ValueError:
            pass
    return None


def _fmt_time(iso: str) -> str:
    try:
        t = dt.datetime.fromisoformat(iso)
    except ValueError:
        return iso
    days = (dt.date.today() - t.date()).days
    day = "today" if days == 0 else "yesterday" if days == 1 else t.strftime("%a %d %b")
    return f"{day} {t:%I:%M %p}".replace(" 0", " ")


def recall(query: str, when: str = "", limit: int = 8) -> str:
    rng = _when_range(when) or _when_range(query)
    qw = _words(query)
    hits = []
    for f in facts():
        s = len(qw & _words(f["text"]))
        if s:
            hits.append((s + 0.5, "9", f"Saved fact: {f['text']}"))
    for r in _records():
        day = r.get("t", "")[:10]
        if rng and not (rng[0].isoformat() <= day <= rng[1].isoformat()):
            continue
        blob = f"{r.get('user', '')} {r.get('miles', '')} {' '.join(r.get('did', []))}"
        s = len(qw & _words(blob)) if qw else 0
        if s or (rng and not qw):
            did = f" [did: {', '.join(r['did'][:5])}]" if r.get("did") else ""
            hits.append((s, r["t"], f"{_fmt_time(r['t'])} - user: {r.get('user', '')[:220]} | you: "
                                    f"{r.get('miles', '')[:220]}{did}"))
    if not hits:
        span = f" from {rng[0]:%d %b}" + (f" to {rng[1]:%d %b}" if rng[1] != rng[0] else "") if rng else ""
        return f"Nothing in memory matches that{span}."
    if rng and not qw:                                   # "what did we do yesterday": in order
        hits = hits[-limit:]
    else:
        hits = sorted(hits, key=lambda h: (h[0], h[1]), reverse=True)[:limit]
    return "\n".join(h[2] for h in hits)


# ---- helpers for the brain ---------------------------------------------------------
_PERSONAL = re.compile(
    r"\b(my name is|call me|i live|i'm from|i am from|i work|i study|i'm studying|i go to|i was born|my birthday|"
    r"i'm \d+|i am \d+|i'm an? |i am an? |my (?:[\w']+ ){0,3}(?:is|are|was)\b|my favou?rite|i (?:really )?(?:like|love|"
    r"hate|prefer|enjoy|dislike|don't like|can't stand)|i usually|i always|i never|i have an? |i've got|"
    r"remember (?:that|this|my))", re.I)


def personal_hint(text: str) -> bool:
    return bool(_PERSONAL.search(text))


def version() -> float:
    try:
        return FACTS_FILE.stat().st_mtime
    except OSError:
        return 0.0


def recent_text(n=5) -> str:
    lines = []
    for r in recent(n):
        lines.append(f"- {_fmt_time(r['t'])}: user said \"{r.get('user', '')[:160]}\" -> you: "
                     f"\"{r.get('miles', '')[:160]}\"")
    return "\n".join(lines)
