"""Email: compose in Gmail / Outlook web / default mail app, with a contacts book.

Composing opens a real compose window with recipient, subject and body filled in.
Sending always asks the user first, then presses the page's Send button and only
reports success when the page confirms it ("Message sent").
"""
import difflib
import json
import re
import time
import urllib.parse
import webbrowser

import config
from .util import DATA, log

CONTACTS = DATA / "contacts.json"
EMAIL_RE = re.compile(r"^[\w.+-]+@[\w-]+(\.[\w-]+)+$")


def load_contacts() -> dict:
    try:
        return json.loads(CONTACTS.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_contact(name: str, email: str = "", phone: str = "") -> str:
    contacts = load_contacts()
    key = name.strip().lower()
    entry = contacts.get(key, {"name": name.strip()})
    if email:
        addr = spoken_to_email(email)
        if not EMAIL_RE.match(addr):
            return f"'{email}' doesn't look like a valid email address."
        entry["email"] = addr
    if phone:
        entry["phone"] = re.sub(r"[^\d+]", "", phone)
    contacts[key] = entry
    CONTACTS.write_text(json.dumps(contacts, indent=1), encoding="utf-8")
    return f"Saved {entry['name']}: " + ", ".join(f"{k} {v}" for k, v in entry.items() if k != "name")


def spoken_to_email(text: str) -> str:
    """'john dot doe at gmail dot com' -> 'john.doe@gmail.com'."""
    t = " " + text.strip().lower() + " "
    t = re.sub(r"\s(at the rate of|at the rate|at sign|at)\s", "@", t)
    t = re.sub(r"\s(dot|period|point)\s", ".", t)
    t = re.sub(r"\s(underscore)\s", "_", t)
    t = re.sub(r"\s(dash|hyphen|minus)\s", "-", t)
    t = re.sub(r"\s+", "", t)
    return t.strip(".")


def find_contact(name: str):
    contacts = load_contacts()
    key = name.strip().lower()
    if key in contacts:
        return contacts[key]
    names = list(contacts)
    close = difflib.get_close_matches(key, names, n=1, cutoff=0.75)
    if close:
        return contacts[close[0]]
    for k in names:                       # "rahul" matches "rahul sharma"
        if key and (key in k.split() or k.startswith(key)):
            return contacts[k]
    return None


def resolve(recipient: str):
    """-> (address, None) or (None, reason)."""
    r = recipient.strip()
    if not r:
        return None, "empty"
    if "@" in r or re.search(r"\s(at|at the rate)\s", r.lower()):
        addr = spoken_to_email(r)
        return (addr, None) if EMAIL_RE.match(addr) else (None, f"'{r}' isn't a valid email address")
    c = find_contact(r)
    if c and c.get("email"):
        return c["email"], None
    return None, f"no email address saved for {r}"


def _spell(addr: str) -> str:
    user, _, domain = addr.partition("@")
    return f"{user} at {domain.replace('.', ' dot ')}"


def compose_url(to, cc, subject, body, service):
    q = lambda d: urllib.parse.urlencode({k: v for k, v in d.items() if v}, quote_via=urllib.parse.quote)
    if service == "outlook":
        return "https://outlook.live.com/mail/0/deeplink/compose?" + q({"to": to, "cc": cc, "subject": subject,
                                                                         "body": body})
    if service == "mailto":
        return f"mailto:{urllib.parse.quote(to, safe='@,')}?" + q({"cc": cc, "subject": subject, "body": body})
    return "https://mail.google.com/mail/?view=cm&fs=1&" + q({"to": to, "cc": cc, "su": subject, "body": body})


def _split_name(part: str):
    """'Tulsi <t@x.com>' / 't@x.com (Tulsi)' / 'Tulsi' / 't@x.com' -> (name, address-or-empty)."""
    p = part.strip().strip('"')
    m = re.match(r"^(.*?)\s*<([^>]+)>$", p) or re.match(r"^([^()]+?)\s*\(([^)]+)\)$", p)
    if m:
        a, b = m.group(1).strip(), m.group(2).strip()
        return (a, b) if "@" in b or " at " in f" {b.lower()} " else (b, a)
    if "@" in p or re.search(r"\s(at|at the rate)\s", p.lower()):
        return "", p
    return p, ""


def _known_addresses() -> set:
    return {c.get("email", "").lower() for c in load_contacts().values()}


def _ask_address(ask, name: str, guess: str):
    """Box where the user types/fixes an address. -> (address, None) or (None, reason)."""
    who = f"{name}'s" if name else "the recipient's"
    q = (f"Please check {who} email address. I may have misheard it." if guess else
         f"What's {who} email address?")
    for _ in range(2):
        typed = ask(q, guess)
        if typed is None or not typed.strip():
            return None, f"the user didn't give {who} email address"
        addr = spoken_to_email(typed)
        if EMAIL_RE.match(addr):
            return addr, None
        q, guess = f"'{typed}' doesn't look like an email address. Please type {who} address again.", typed
    return None, f"no valid address for {name or 'the recipient'}"


_PLACEHOLDER = re.compile(r"@(example|test|domain|email|sample|mail)\.(com|org|net)$", re.I)


def _memory_addresses() -> set:
    from . import memory
    return {a.lower() for f in memory.fact_texts() for a in re.findall(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", f)}


def compose(to: str = "", subject: str = "", body: str = "", cc: str = "", send: bool = False,
            confirm=None, status=None, ask=None, heard=False, trusted=(), request="") -> str:
    """ask(question, suggestion) opens a box for the user to type into.

    An address is used as-is only if it's a saved contact, in memory, typed by the user (in the request or an
    ask box). Anything else - misheard speech (heard=True) or an address the AI made up - is shown in the
    box for the user to check or type.
    """
    addrs, problems, saved = [], [], []
    known = _known_addresses() | _memory_addresses() | {t.lower() for t in trusted}
    req = (request or "").lower()
    for part in [p for p in re.split(r"\s*(?:[;,]|\band\b)\s*", to or "") if p.strip()]:
        name, raw = _split_name(part)
        addr, why = resolve(raw or name)
        in_request = bool(addr) and (addr.lower() in req.replace(" ", "") or addr.lower() in spoken_to_email(req))
        invented = bool(addr) and (not in_request or bool(_PLACEHOLDER.search(addr)))
        unsure = bool(addr) and raw and addr.lower() not in known and (heard or invented)
        if ask and (addr is None or unsure):
            guess = addr if addr and not invented else (spoken_to_email(raw) if raw and not addr else "")
            addr, why = _ask_address(ask, name, guess)
            if addr and name:
                save_contact(name, addr)
                saved.append(f"{name} ({addr})")
        (addrs.append(addr) if addr else problems.append(why))
    cc_addrs = [a for a, _ in (resolve(p) for p in re.split(r"[;,]", cc or "") if p.strip()) if a]
    if problems:
        return ("FAILED - no email was written or opened: " + "; ".join(problems) +
                (". Ask the user for the address with ask_user, then compose again." if not ask else
                 ". Don't ask again. Tell the user plainly that the email wasn't written because the address "
                 "is missing."))
    service = config.EMAIL_SERVICE
    url = compose_url(",".join(addrs), ",".join(cc_addrs), subject, body, service)
    webbrowser.open(url)
    who = ", ".join(addrs) if addrs else "no recipient yet"
    summary = f"Compose window opened in {service.capitalize()}: to {who}; subject '{subject}'."
    if saved:
        summary += f" Saved to contacts for next time: {', '.join(saved)}."
    if not send:
        return summary + (" It is NOT sent; it's waiting for the user to review or say 'send it'. Tell them in "
                          "one short sentence - don't read the email out.")
    if not addrs:
        return summary + " Not sent: there's no recipient."
    if confirm and not confirm(f"Shall I send it to {', '.join(_spell(a) for a in addrs)}?"):
        return summary + " The user said not to send it yet, so it's still open as a draft."
    if status:
        status("Sending…")
    return summary + " " + press_send(service)


def press_send(service: str = None, timeout: float = 20) -> str:
    """Press Send in an open compose window and verify the page confirms it."""
    service = service or config.EMAIL_SERVICE
    if service == "mailto":
        return "I can't press send in the default mail app; please send it from there."
    import uiautomation as auto
    from .browser import browser_windows
    from .uia import find_first, _BUTTON
    auto.SetGlobalSearchTimeout(0.5)
    send_names = ("Send ‪(Ctrl-Enter)‬", "Send (Ctrl-Enter)", "Send", "Send (Ctrl+Enter)")
    with auto.UIAutomationInitializerInThread():
        end = time.time() + timeout
        sent_btn = None
        while time.time() < end and sent_btn is None:
            for w in browser_windows():
                title = (w.Name or "").lower()
                if service == "gmail" and "gmail" not in title:
                    continue
                if service == "outlook" and "outlook" not in title:
                    continue
                btn = find_first(w, send_names, _BUTTON)
                if btn:
                    sent_btn = (w, btn)
                    break
            if sent_btn is None:
                time.sleep(0.4)
        if not sent_btn:
            return "I couldn't find the Send button, so it has NOT been sent. It's open for the user to send."
        w, btn = sent_btn
        try:
            btn.GetInvokePattern().Invoke()
        except Exception:
            btn.Click(simulateMove=False)
        end = time.time() + 12
        while time.time() < end:                      # wait for the page's own confirmation
            if find_first(w, ("Message sent", "Your message has been sent"), substring=True):
                log.info("mail: send confirmed")
                return "Sent: the page confirmed 'Message sent'."
            time.sleep(0.4)
        return "I pressed Send but couldn't see the 'Message sent' confirmation. Tell the user to check it."
