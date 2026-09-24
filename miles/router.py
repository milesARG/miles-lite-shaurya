"""Instant handling for common commands - no AI round-trip, so they run in milliseconds.

Anything not matched here (multi-step tasks, questions, chat, email) goes to the AI brain.
"""
import datetime as dt
import random
import re
from dataclasses import dataclass
from typing import Callable

import config
from . import tools
from .util import folder_alias


def _t():
    return config.USER_TITLE


def line(*options, **kw) -> str:
    """A random in-character line, e.g. line("{app} is up, {t}.", "Opening {app}.", app="Chrome")."""
    return random.choice(options).format(t=_t(), **kw)


@dataclass
class Route:
    run: Callable[[], str]
    ack: str | None = None          # spoken immediately for slow actions


_FILLER = re.compile(r"^(please|can you|could you|would you|will you|kindly|hey|ok|okay|yo|now|just|go ahead and|"
                     r"i want you to|i need you to|i'd like you to)[,\s]+")
_SITES_FOR_SEARCH = "google|youtube|amazon|wikipedia|github|flipkart|maps|images|news|bing|spotify|stackoverflow"
_MUSIC_ANY = r"(something|anything|some music|music|my music|a song|some songs|songs|tunes|some tunes)"
# Things Whisper hears instead of "Spotify"
_MISHEARD = re.compile(r"\b(?:(?:this|the|a) podcast|spot ?if(?:y|i)|spotty ?fy|spot a fly|spot fly|spotifi)\b")
# "type a note about..." wants Miles to write something, not type those exact words
_COMPOSE = re.compile(r"^(?:a|an|some|me a|me an)\s+(?:\w+\s+){0,3}?(?:note|message|paragraph|essay|poem|story|"
                      r"letter|reply|email|mail|summary|caption|post|tweet|bio|description|article|speech|list|"
                      r"joke|haiku|song|review|report|text)\b")


def _clean(text: str) -> str:
    t = text.lower().strip().replace("’", "'")
    t = re.sub(r"[.!?]+$", "", t)
    t = re.sub(r"[,;]+", " ", t)                              # "open, VS code" -> "open VS code"
    t = re.sub(r"\b(uh+|um+|umm+|er+|ah+|hmm+|maybe|actually|basically|kind of|sort of)\b", " ", t)
    t = _MISHEARD.sub("spotify", t)
    t = " ".join(t.split())
    prev = None
    while prev != t:
        prev = t
        t = _FILLER.sub("", t).strip(" ,")
    t = re.sub(r"\s+(please|for me|right now|now|real quick)$", "", t)
    return t.strip(" ,")


def _greeting():
    h = dt.datetime.now().hour
    part = "morning" if h < 12 else "afternoon" if h < 17 else "evening"
    return line(f"Good {part}, {{t}}. How may I assist?", "At your service, {t}.",
                f"Good {part}, {{t}}. All systems are online.", "Online and ready, {t}.")


def _ok(result: str, spoken: str) -> str:
    """Use the friendly line if the action worked, otherwise report what really happened."""
    bad = result.lower().startswith(("couldn't", "no ", "error", "not ", "i can't", "spotify didn't", "unknown"))
    return result if bad else spoken


def _open(target: str):
    apps = tools.CTX.apps
    m = re.fullmatch(r"(.+?) settings?", target)
    if m and m.group(1) in tools._SETTINGS:
        return Route(lambda: (tools.open_settings(m.group(1)), line("{p} settings, {t}.", "Opening {p} settings.",
                                                                     p=m.group(1).capitalize()))[1])
    app, score = apps.find(target)
    url = tools.site_url(target)
    folder = folder_alias(target)

    def launch():
        apps.launch(app)
        return line("{app} is up, {t}.", "Opening {app}.", "{app}, coming right up.", "Launching {app}, {t}.",
                    app=app["name"])
    if app and score >= 86:
        return Route(launch)
    if url and (not app or score < 92):
        return Route(lambda: (tools.open_website(target), line("Opening {x}, {t}.", "{x}, on screen.",
                                                               x=target))[1])
    if folder:
        return Route(lambda: (tools.file_action("open", str(folder)), line("Your {x} folder, {t}.",
                                                                           "Opening {x}.", x=folder.name))[1])
    if app and score >= 72:
        return Route(launch)
    return None          # let the AI figure it out (files, fuzzy names, etc.)


def _close_target(name: str):
    """Only close things that are clearly apps; anything vague goes to the AI (which can ask)."""
    if name in tools._STOPWORDS or len(name) < 3:
        return None
    app, score = tools.CTX.apps.find(name)
    wins, how = tools._match_windows(name, allow_title=False)
    if how != "process" and not (app and score >= 80):
        return None

    def close():
        res = tools.close_app(name)
        return _ok(res, line("{x} closed, {t}.", "Done. {x} is closed.", x=name[:1].upper() + name[1:]))
    return Route(close)


def _play(query: str, service: str | None):
    service = service or config.DEFAULT_MUSIC
    if service == "youtube":
        return Route(lambda: tools.play_youtube(query))
    if re.fullmatch(_MUSIC_ANY, query):
        return Route(lambda: tools.spotify("resume"))
    return Route(lambda: tools.spotify("play", query))


def route(text: str) -> Route | None:
    t = _clean(text)
    if not t:
        return None
    # multi-step requests go to the AI ("open chrome and search cats")
    if re.search(r"\b(and then|then|after that|and also)\b", t) or (
            " and " in t and not re.match(r"^(play|search|google|look up)\b", t)):
        return None
    # "refine this prompt", "search this", "type that in": the AI sees the selection / text box / page
    from .context import wants_selection
    if wants_selection(t) or re.fullmatch(r"(?:search|google|look up|type|read|copy)\s+(?:for\s+)?(?:this|that|it)", t):
        return None

    # --- undo / voice ------------------------------------------------------------
    if re.fullmatch(r"undo(?: (?:that|it|this|the last (?:change|edit)))?|revert (?:that|it)|put it back|"
                    r"change it back", t):
        return Route(_undo)
    m = re.fullmatch(r"(?:(?:talk|speak)(?: a (?:bit|little))?|speed up|slow down)(?: (?:faster|quicker|slower|a bit|"
                     r"a little))?(?: please)?", t)
    if m and re.search(r"faster|quicker|speed up|slower|slow down", t):
        way = "faster" if re.search(r"faster|quicker|speed up", t) else "slower"
        return Route(lambda: tools.voice_settings(way))
    m = re.fullmatch(r"(?:change|switch|set) (?:your |the )?voice to (\w+)(?: voice)?", t)
    if m:
        who = m.group(1)
        return Route(lambda: tools.voice_settings("voice", voice=who))
    if re.fullmatch(r"(?:reset|restore) (?:your |the )?voice", t):
        return Route(lambda: tools.voice_settings("reset"))

    # --- conversation ---------------------------------------------------------
    if re.fullmatch(r"(stop|cancel|never ?mind|nothing|forget it|shut up|be quiet|quiet|silence|that's all|"
                    r"that is all|no thanks|no thank you|abort)", t):
        return Route(lambda: line("Very well, {t}.", "Standing by, {t}.", "Of course.", "As you wish."))
    if re.fullmatch(r"(hi|hello|hey there|good (morning|afternoon|evening)|wake up|you there|are you there|"
                    r"miles|jarvis|friday|daddy's home|i'm (back|home))", t):
        return Route(_greeting)
    if re.fullmatch(r"(thanks|thank you|thank you so much|cheers|good job|nice|great|perfect|awesome|well done)", t):
        return Route(lambda: line("Always a pleasure, {t}.", "My pleasure, {t}.", "Anytime.",
                                  "For you, {t}, always."))
    if re.fullmatch(r"(what('s| is) the time|what time is it|tell me the time|time|current time|the time)", t):
        return Route(lambda: f"It's {dt.datetime.now():%I:%M %p}".replace(" 0", " ") + f", {_t()}.")
    if re.fullmatch(r"(what('s| is) (the |today's )?date( today)?|what day is (it|today)|today's date|date)", t):
        return Route(lambda: f"Today is {dt.datetime.now():%A, %d %B %Y}.")

    # --- persona / briefing / protocols ------------------------------------------
    m = re.fullmatch(r"(?:switch|change|go|swap)\s+(?:to|into|over to)\s+(jarvis|friday)(?:\s+mode)?|"
                     r"(jarvis|friday)\s+mode|(?:activate|enable|bring back)\s+(jarvis|friday)", t)
    if m:
        who = next(g for g in m.groups() if g)
        return Route(lambda: tools.switch_persona(who))
    if re.fullmatch(r"(brief me|(give me )?(a |my |the )?(daily |morning |evening )?briefing|status report|"
                    r"catch me up|give me (an |the )?(update|rundown)|what('s| is) (going on|happening)( today)?|"
                    r"good morning (miles|jarvis|friday))", t):
        return Route(tools.briefing, ack=line("One moment, {t}.", "Compiling your briefing, {t}."))

    # --- windows theme (before protocols so "dark mode" isn't a protocol) ----------
    m = re.fullmatch(r"(?:turn on |enable |switch to |go |activate )?(dark|light) (?:mode|theme)(?: on)?", t)
    if m:
        mode = m.group(1)
        return Route(lambda: _ok(tools.set_theme(mode), line("{m} mode engaged, {t}.", "Going {m}.",
                                                             m=mode.capitalize())))
    m = (re.fullmatch(r"(?:run|start|activate|initiate|engage|execute|begin|launch|enable|go into|switch to)\s+"
                      r"(?:the\s+|my\s+)?(.+?)\s+(?:protocol|routine|mode)", t)
         or re.fullmatch(r"(.+?)\s+(?:protocol|routine|mode)", t))
    if m:
        key, r = tools.find_routine(m.group(1))
        if r:
            return Route(lambda: tools.routine("run", key),
                         ack=line("Initiating {k} protocol, {t}.", "{K} protocol, coming up.", k=key, K=key.title()))

    # --- browser tabs (before open/close so tabs never close the whole browser) ----
    if re.fullmatch(r"(?:close|shut|kill|get rid of)\s+(?:this|the|current|that|my|active|one)?\s*tab", t):
        return Route(lambda: _ok(tools.browser_tab("close"), line("Tab closed, {t}.", "Done.", "Closed it.")))
    m = re.fullmatch(r"(?:close|shut|kill|get rid of)\s+(?:the\s+|my\s+|that\s+)?(.+?)\s+tabs?", t)
    if m:
        title = m.group(1)
        return Route(lambda: tools.browser_tab("close", title))
    if re.fullmatch(r"(?:open\s+)?(?:a\s+)?new tab", t):
        return Route(lambda: (tools.browser_tab("new"), "")[1])
    if re.fullmatch(r"(?:reopen|restore|bring back|undo close)(?: the)?(?: last)?(?: closed)? tab", t):
        return Route(lambda: tools.browser_tab("reopen"))
    m = re.fullmatch(r"(next|previous|last|prior)\s+tab", t)
    if m:
        way = "next" if m.group(1) == "next" else "previous"
        return Route(lambda: (tools.browser_tab(way), "")[1])
    m = re.fullmatch(r"(?:switch|go|jump|take me)\s+(?:back\s+)?to\s+(?:the\s+|my\s+)?(.+?)\s+tab", t)
    if m:
        title = m.group(1)
        return Route(lambda: tools.browser_tab("switch", title))
    m = re.fullmatch(r"(mute|unmute)\s+(?:the\s+|this\s+|that\s+)?(?:(.+?)\s+)?tab", t)
    if m:
        act, title = m.group(1), m.group(2) or ""
        return Route(lambda: tools.browser_tab(act, title))
    if re.fullmatch(r"(?:list|what are|show me|read)\s+(?:my\s+|the\s+)?(?:open\s+)?tabs", t):
        return Route(lambda: tools.browser_tab("list"))
    if re.fullmatch(r"(?:reload|refresh)(?: the| this)?(?: page| tab)?", t):
        return Route(lambda: (tools.browser_tab("reload"), "")[1])

    # --- open / close -----------------------------------------------------
    m = re.fullmatch(r"(?:open|launch|start|run|fire up|bring up|load|boot up|pull up)\s+(?:up\s+)?"
                     r"(?:the\s+|my\s+|a\s+)?(.+?)(?:\s+app|\s+application|\s+program)?", t)
    if m and m.group(1) not in ("it", "this", "that", "a new tab", "new tab"):
        return _open(m.group(1))

    if re.fullmatch(r"(close|exit|quit) (this|it|this window|the window|current window|that)", t):
        from .browser import is_browser_foreground
        if is_browser_foreground():                   # in a browser, "close this" means the tab
            return Route(lambda: _ok(tools.browser_tab("close"), line("Tab closed, {t}.", "Done.")))
        return Route(lambda: (tools.manage_window("this", "close"), line("Closed, {t}.", "Done."))[1])
    m = re.fullmatch(r"(?:close|quit|exit|kill|terminate)\s+(?:the\s+|my\s+)?(.+?)(?:\s+app|\s+window|\s+windows)?", t)
    if m and not re.search(r"\b(pc|computer|system|laptop|windows|everything|all|tabs?)\b", m.group(1)):
        return _close_target(m.group(1))

    # --- windows ----------------------------------------------------------
    if re.fullmatch(r"(minimi[sz]e (all|everything|all windows)|show (me )?(the )?desktop|go to (the )?desktop|"
                    r"clear (the )?screen)", t):
        return Route(lambda: (tools.manage_window("all", "minimize"), line("Desktop, {t}.", "All clear."))[1])
    m = re.fullmatch(r"(minimi[sz]e|maximi[sz]e|restore)\s+(?:the\s+)?(.+)", t)
    if m:
        act = {"minimise": "minimize", "maximise": "maximize"}.get(m.group(1), m.group(1))
        name = m.group(2)
        return Route(lambda: tools.manage_window("this" if name in ("this", "it", "this window") else name, act))
    m = re.fullmatch(r"(?:switch to|go to|focus|show me|bring up)\s+(?:the\s+|my\s+)?(.+)", t)
    if m and tools._match_windows(m.group(1))[0]:
        name = m.group(1)
        return Route(lambda: (tools.manage_window(name, "focus"), line("{x}, {t}.", "Here you go.",
                                                                       x=name.capitalize()))[1])

    # --- volume & media ---------------------------------------------------
    m = re.fullmatch(r"(?:set |change |put )?(?:the )?(?:volume|sound)(?: level)? (?:to |at )?(\d{1,3})(?:\s*(?:%|percent))?", t)
    if m:
        lvl = int(m.group(1))
        return Route(lambda: tools.volume("set", lvl))
    m = re.fullmatch(r"(?:turn |crank )?(?:the )?(?:volume|sound|it|music) (up|down)(?: a (?:bit|little))?(?: by (\d+))?|"
                     r"(louder|quieter|softer)", t)
    if m:
        up = (m.group(1) == "up") or m.group(3) == "louder"
        step = int(m.group(2)) if m.group(2) else 10
        return Route(lambda: tools.volume("up" if up else "down", step))
    if re.fullmatch(r"(mute|unmute)( the)?( volume| sound| audio| pc| computer)?", t):
        act = t.split()[0]
        return Route(lambda: tools.volume(act))
    m = re.fullmatch(r"(pause|stop|resume|unpause|continue)\s+(?:the\s+)?(spotify|music|song|playback)", t)
    if m:
        act = "pause" if m.group(1) in ("pause", "stop") else "resume"
        return Route(lambda: tools.spotify(act) if tools._norm(m.group(2)) == "spotify" or _spotify_up()
                     else (tools.media_control("play_pause"), "")[1])
    if re.fullmatch(r"(pause|resume|unpause|play)( the)?( music| song| video| media| it| playback)?", t):
        return Route(lambda: (tools.media_control("play_pause"), "")[1])
    if re.fullmatch(r"(next|skip)( song| track| video| one| this( song)?)?( on spotify)?|skip it", t):
        return Route(lambda: (tools.media_control("next"), "")[1])
    if re.fullmatch(r"(previous|last|go back)( song| track| video)?( on spotify)?", t):
        return Route(lambda: (tools.media_control("previous"), "")[1])
    if re.fullmatch(r"(what'?s|what is|which song is|what song is)\s+(this song|playing|the song|this track)"
                    r"( now| on spotify)?|name this song", t):
        return Route(lambda: tools.spotify("now_playing"))
    m = re.fullmatch(r"(?:turn on |enable |toggle )?(shuffle|repeat)(?: on spotify)?", t)
    if m:
        act = m.group(1)
        return Route(lambda: tools.spotify(act))

    m = re.fullmatch(r"play (.+?) (?:on|from|in) youtube|play (.+?) videos?|youtube (.+)", t)
    if m:
        return _play(next(g for g in m.groups() if g), "youtube")
    m = re.fullmatch(r"play (.+?) (?:on|from|in) spotify|spotify (?:play )?(.+)", t)
    if m:
        return _play(next(g for g in m.groups() if g), "spotify")
    m = re.fullmatch(r"play (.+)", t)
    if m and m.group(1) not in ("it", "the music", "this"):
        return _play(m.group(1), None)

    # --- search -----------------------------------------------------------
    m = re.fullmatch(rf"(?:search|look up)\s+({_SITES_FOR_SEARCH})\s+for\s+(.+)", t)
    if m:
        site, q = m.group(1), m.group(2)
        return Route(lambda: tools.web_search(q, site))
    m = re.fullmatch(rf"(?:search|google|look up|search for|search the web for)\s+(?:for\s+)?(.+?)"
                     rf"(?:\s+on\s+({_SITES_FOR_SEARCH}))?", t)
    if m:
        q, site = m.group(1), m.group(2) or "google"
        return Route(lambda: tools.web_search(q, site))

    # --- system -----------------------------------------------------------
    if re.fullmatch(r"(take|capture|grab)( a| the)? screenshot|screenshot|screen ?shot", t):
        return Route(lambda: (tools.take_screenshot(), line("Screenshot saved to your Pictures, {t}.",
                                                            "Captured, {t}."))[1])
    if re.fullmatch(r"lock( the| my)?( pc| computer| screen| system| laptop)?", t):
        return Route(lambda: tools.execute("system_power", {"action": "lock"}))
    if re.fullmatch(r"(turn off|switch off) (the )?(screen|display|monitor)s?", t):
        return Route(lambda: tools.execute("system_power", {"action": "screen_off"}))
    m = re.fullmatch(r"(shut ?down|turn off|power off|restart|reboot)( the| my)?( pc| computer| system| laptop)?", t)
    if m:
        act = "restart" if m.group(1) in ("restart", "reboot") else "shutdown"
        return Route(lambda: tools.execute("system_power", {"action": act}))
    if re.fullmatch(r"(put (the |my )?(pc|computer|laptop|system) to sleep|sleep (the )?(pc|computer|laptop))", t):
        return Route(lambda: tools.execute("system_power", {"action": "sleep"}))
    if re.fullmatch(r"cancel (the )?(shutdown|restart)", t):
        return Route(lambda: tools.system_power("cancel_shutdown"))
    if re.search(r"\b(scan|check|analy[sz]e|audit)\b.*\b(threats?|virus(es)?|malware|security|hack(ed|ers?)?|safe|"
                 r"secure|vulnerab\w*)\b", t) or re.fullmatch(r"(security|threat) (scan|check|report)|am i (safe|hacked)|"
                                                              r"is my (pc|computer|system) (safe|secure|hacked)", t):
        return Route(lambda: tools.security_check("scan"),
                     ack=line("Running a full security sweep, {t}. This will take a few seconds.",
                              "Scanning all systems for threats, {t}. Stand by."))
    if re.fullmatch(r"(run |start |do )?(a )?(quick|full) (virus |antivirus |defender )?scan", t):
        kind = "full_virus_scan" if "full" in t else "quick_virus_scan"
        return Route(lambda: tools.security_check(kind))

    m = re.fullmatch(r"(?:what's|what is|how's|how is)? ?(?:the )?weather(?: like)?(?: today| now| outside)?(?: in (.+))?", t)
    if m:
        loc = m.group(1) or ""
        return Route(lambda: tools.weather(loc))
    m = re.fullmatch(r"(?:set |start )?(?:a )?timer (?:for )?(\d+) (second|sec|minute|min|hour)s?|"
                     r"(\d+) (second|minute|hour)s? timer", t)
    if m:
        n = int(m.group(1) or m.group(3))
        unit = (m.group(2) or m.group(4))[:3]
        secs = n * {"sec": 1, "min": 60, "hou": 3600}[unit]
        return Route(lambda: tools.set_reminder("your timer is done", seconds=secs).replace(
            "Scheduled for", "Timer set. It'll ring at"))
    m = re.fullmatch(r"type (?:out )?(.+)", t)
    if m and not _COMPOSE.match(m.group(1)):
        txt = text.strip()
        txt = re.sub(r"^.*?\btype\s+(out\s+)?", "", txt, flags=re.I).rstrip(".")
        return Route(lambda: (tools.type_text(txt), "")[1])
    if re.fullmatch(r"(who (made|built|created|designed|programmed|coded|developed) you|who('s| is) your (creator|"
                    r"maker|developer|owner|boss)|who do you belong to|whose (assistant|ai) are you|who are you|"
                    r"what are you|introduce yourself|tell me about yourself|who owns you)", t):
        from .owner import CREATOR, OWNER
        return Route(lambda: line(f"I'm Miles, {OWNER}'s personal assistant. {CREATOR} built me, especially for you.",
                                  f"{CREATOR} made me, {{t}}. Designed, built and tuned for {OWNER}, and nobody else.",
                                  f"Miles, at your service. Created by {CREATOR}, exclusively for {OWNER}."))
    if re.fullmatch(r"(what can you do|help|what are your (skills|capabilities|abilities)|what do you do)", t):
        return Route(lambda: (f"Nearly anything on this PC, {_t()}. Apps, windows and browser tabs, Spotify and "
                              f"YouTube, email, writing notes and letters, web research and news, documents, files, "
                              f"installing software, reading and clicking anything on screen, security sweeps, "
                              f"reminders, protocols, and long step-by-step jobs. And I remember what you tell me. "
                              f"All thanks to Arnav, who built me."))
    return None


def _undo() -> str:
    """Undo Miles' last in-place edit (each pasted chunk is one undo step), else Ctrl+Z in the active window."""
    import time
    import pyautogui
    lw = tools.CTX.last_write
    u32 = __import__("ctypes").windll.user32
    if lw and time.time() - lw["t"] < 1800 and lw["hwnd"] and u32.IsWindow(lw["hwnd"]):
        tools.focus_hwnd(lw["hwnd"])
        time.sleep(0.2)
        for _ in range(max(1, lw["steps"])):
            pyautogui.hotkey("ctrl", "z")
            time.sleep(0.05)
        tools.CTX.last_write = None
        return line("Undone, {t}. It's back as it was.", "Reverted, {t}.")
    from .context import _TERMINALS, _proc, target_window
    hwnd = target_window()
    if _proc(hwnd) in _TERMINALS:
        return f"There's nothing of mine to undo there, {_t()}."
    tools.focus_hwnd(hwnd)
    pyautogui.hotkey("ctrl", "z")
    return line("Undone, {t}.", "Done.")


def _spotify_up() -> bool:
    from . import spotify
    return spotify.running()
