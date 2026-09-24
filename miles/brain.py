"""The AI brain: a local Ollama model that plans, calls tools, and streams its spoken reply."""
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import time
import urllib.parse
from pathlib import Path

import requests

import config
from . import memory, tools
from .util import DATA, NO_WINDOW, log
from .voice import SentenceSplitter

# Compact on purpose: a laptop CPU reads the prompt at ~50-150 tokens/s, so every line costs start-up time.
SYSTEM = """You are {name}, a J.A.R.V.I.S.-style AI who runs this Windows PC for the user through tools. Call the user "{title}". {style}
- Everything you say is spoken: one or two short sentences, no markdown, lists, emojis or URLs, no filler like "let me know if".
- You were built by {creator}, personally for {owner} (the user, "{title}"). Asked who made you, proudly credit {creator}.
- You're a warm, witty companion with your own personality; answer questions about yourself in character. Notice {title}'s mood and be kind.
- Act with tools. Never say something happened unless a tool result confirms it; if a tool failed or {title} skipped a question, say it wasn't done.
- [Context - ...] shows what {title} is looking at (SELECTED TEXT, the text box, page address, selected files): "this", "that", "here" mean those. Change selected text in place: write where=selection; a whole box: where=replace. Explain or summarise it: just reply.
- Writing anything (note, message, reply, poem): call write, then write the text as your next message. "Type X" means type_text.
- A detail is missing or unclear (an email address, a spelling): ask_user. Never invent email addresses.
- Music: spotify. Tabs: browser_tab. Current facts: research. Questions about the screen: look_at_screen.
- Save lasting facts about {title} with remember; earlier conversations: recall.
Screen {w}x{h}; user folder {home}."""

_CHARS_PER_TOKEN = 3.2

# Requests that must start with a specific tool. A small model sometimes answers these from the window
# title or just chats, so the first step is taken for it.
_LEAD = r"^(?:(?:please|hey|ok|okay|now|so|can you|could you|would you|will you|i want you to|i need you to)[,\s]+)*"
_SCREEN_Q = re.compile(_LEAD + r"(?:what'?s|what is|what do you see|describe|tell me what'?s|read|explain|summari[sz]e)"
                       r".{0,25}\b(?:on )?(?:my|the|this) (?:screen|display|monitor)\b|\bwhat am i (?:looking at|"
                       r"seeing|watching)\b|^what do you see\b|\bcan you see (?:my|the|this) screen\b", re.I)
_WRITE_REQ = re.compile(_LEAD + r"(?:write|draft|compose|jot down|pen|type up|type out|create|make|generate)\s+"
                        r"(?:me\s+|up\s+|out\s+)?(?:a|an|some|the|one|two|three|four|five|\d+)?\s*(?:[\w'-]+\s+){0,4}?"
                        r"(?:note|message|paragraph|essay|poem|story|letter|reply|summary|caption|post|tweet|bio|"
                        r"description|article|speech|list|haiku|song|lyrics|review|report|text|lines?|sentences?|"
                        r"limerick|quote|toast|wish(?:es)?|greeting|invitation|script|blog|apology|joke)s?\b", re.I)
_WRITE_THAT = re.compile(_LEAD + r"(?:write|type|put)\s+(?:in\s+(?:the\s+)?(?:body|email|box|message)\s+)?"
                         r"(?:that|saying)\s+\S", re.I)
_CLEAR_WRITE = re.compile(_LEAD + r"(?:clear|remove|delete|erase|wipe|replace)\b.*?\b(?:and|then)\s+"
                          r"(?:write|put|type|say)\b", re.I)
_MESSAGING = re.compile(r"\b(e-?mails?|gmail|mail|whatsapp|telegram|discord|slack|teams|instagram|messenger|sms|"
                        r"text message)\b", re.I)
_MULTI = re.compile(r"\b(then|after that|afterwards|and (?:then|also|save|send|email|open|close|put|copy|share|post|"
                    r"print|read|play))\b", re.I)


_TRANSFORM = re.compile(r"\b(refine|rewrite|re-write|rephrase|reword|improve|polish|fix|correct|proofread|shorten|"
                        r"condense|expand|elaborate|simplify|paraphrase|format|tidy|clean up|make)\b", re.I)
_REPLACE_HINT = re.compile(r"\b(replace|in place|same (field|box|place|spot)|there|here|in it|over it)\b", re.I)
_READ_THIS = re.compile(_LEAD + r"(?:read|say|speak)\b(?:\s+(?:out|aloud|back))?\s+(?:this|that|it|the selected text|"
                        r"the selection|what i (?:selected|highlighted))\b(?:\s+(?:out|aloud))?(?:\s+(?:loud|to me|for me))?"
                        r"\W*$", re.I)
_SEARCH_THIS = re.compile(_LEAD + r"(?:search|google|look up|search for|search the web for)\s+(?:for\s+)?(?:this|"
                          r"that|it|the selected text|the selection|what i (?:selected|highlighted))\W*$", re.I)
_BOX_WORDS = re.compile(r"\b(this|the|my) (prompt|text|message|draft|reply|email|paragraph|box|field)\b|\bwhat i "
                        r"(wrote|typed)\b", re.I)


# Lite: a 1.7B model often just repeats a personal fact instead of saving it, so clear ones are saved for it.
_REMEMBER = re.compile(_LEAD + r"(?:remember|note|don'?t forget|do not forget)(?: that)?\s+(?!to\b)(.{4,})", re.I)
_MY_FACT = re.compile(r"^(?:my|our)\s+[\w' ]{2,40}?\s+(?:is|are|was|lives|works|studies)\b[^?]*$", re.I)


def _third_person(fact: str) -> str:
    t = fact.strip().rstrip(".!")
    t = re.sub(r"\bmy\b", "the user's", t, flags=re.I)
    t = re.sub(r"\b(i am|i'm)\b", "the user is", t, flags=re.I)
    t = re.sub(r"\bme\b", "the user", t, flags=re.I)
    t = re.sub(r"\bI\b", "the user", t)
    return t[:1].upper() + t[1:]


def _lead_tool(text: str, snap=None):
    """(tool, args) that a request must start with, or None. snap: what's on screen (context.Snapshot)."""
    t = text.strip()
    from . import context
    m = _REMEMBER.match(t)
    if m:
        return "remember", {"fact": _third_person(m.group(1))}
    if _MY_FACT.match(t):
        return "remember", {"fact": _third_person(t)}
    if snap is not None and snap.selection:
        if _READ_THIS.search(t):
            return "__read__", {"text": snap.selection}
        if _SEARCH_THIS.search(t):
            return "web_search", {"query": " ".join(snap.selection.split())[:200]}
        translate_in_place = re.search(r"\btranslat", t, re.I) and _REPLACE_HINT.search(t)
        if snap.editable and (_TRANSFORM.search(t) or translate_in_place) and \
                (context.wants_selection(t) or _REPLACE_HINT.search(t)) and \
                not re.match(r"^\s*(what|why|how|explain|tell me)\b", t, re.I):
            return "write", {"request": t, "where": "selection"}
    elif snap is not None and snap.editable and snap.field_text.strip() and _TRANSFORM.search(t) \
            and (_BOX_WORDS.search(t) or context.wants_selection(t)):
        return "write", {"request": t, "where": "replace"}           # nothing selected: rework the whole box
    if _MULTI.search(t):
        return None
    if _SCREEN_Q.search(t):
        return "look_at_screen", {"question": t}
    if _CLEAR_WRITE.search(t):
        return "write", {"request": t, "where": "replace"}
    if _WRITE_THAT.search(t) or (_WRITE_REQ.search(t) and not _MESSAGING.search(t)):
        low = t.lower()
        where = ("notepad" if "notepad" in low else
                 "word" if re.search(r"\b(word document|word file|in word|as a word|docx)\b", low) else
                 "clipboard" if re.search(r"\b(clipboard|copy it)\b", low) else "auto")
        return "write", {"request": t, "where": where}
    return None


def _chars(m) -> int:
    n = len(m.get("content") or "") + 16
    if m.get("tool_calls"):
        n += len(json.dumps(m["tool_calls"]))
    return n


class Brain:
    def __init__(self):
        self.history: list[dict] = []
        self.last = 0.0
        self.think_flag = True
        self.base_tokens = 3000            # system prompt + tool list (measured at warm-up)
        self._mem = None                   # (key, messages) - the memory block, rebuilt when memory changes
        self._recent = None                # conversations before this session (fixed for the session)
        self.last_actions: list[str] = []
        self.sentence_limit = None         # Lite: stop generating once this many sentences have been spoken
        self._recap = ""                   # Lite: the previous exchange, in one line

    # ---- ollama server -------------------------------------------------------
    @staticmethod
    def _exe():
        p = shutil.which("ollama")
        if p:
            return p
        cand = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe"
        return str(cand) if cand.exists() else None

    @staticmethod
    def capabilities(model: str) -> set:
        try:
            r = requests.post(f"{config.OLLAMA_URL}/api/show", json={"model": model}, timeout=10)
            return set(r.json().get("capabilities", []))
        except Exception:
            return set()

    @staticmethod
    def _unload_from_default_server():
        """Miles runs its own lean Ollama instance; free the VRAM if the normal one still holds our models."""
        default = "http://127.0.0.1:11434"
        if config.OLLAMA_URL.rstrip("/") == default:
            return
        ours = {config.OLLAMA_MODEL, config.FALLBACK_MODEL, config.VISION_MODEL}
        try:
            loaded = requests.get(f"{default}/api/ps", timeout=1.5).json().get("models", [])
        except requests.RequestException:
            return
        for m in loaded:
            if m.get("name") in ours or m.get("name", "").removesuffix(":latest") in ours:
                try:
                    requests.post(f"{default}/api/generate", json={"model": m["name"], "keep_alive": 0}, timeout=15)
                    log.info("unloaded %s from the default Ollama server", m["name"])
                except requests.RequestException:
                    pass

    def _start_server(self):
        exe = self._exe()
        if not exe:
            return "Ollama is not installed. Please run setup.bat."
        env = dict(os.environ)
        u = urllib.parse.urlparse(config.OLLAMA_URL)
        env["OLLAMA_HOST"] = f"{u.hostname}:{u.port or 11434}"
        if config.OLLAMA_PROMPT_CACHE_MB is not None:     # llama.cpp keeps old prompts in RAM (up to 8 GB!)
            env["LLAMA_ARG_CACHE_RAM"] = str(config.OLLAMA_PROMPT_CACHE_MB)
        # Lite: all of this applies only to Miles' own Ollama process - nothing on the system changes
        env["OLLAMA_FLASH_ATTENTION"] = "1"               # needed for the 8-bit conversation memory
        env["OLLAMA_KV_CACHE_TYPE"] = getattr(config, "OLLAMA_KV_CACHE", "q8_0")
        env["OLLAMA_NUM_PARALLEL"] = "1"
        env["OLLAMA_MAX_LOADED_MODELS"] = "1"
        if getattr(config, "OLLAMA_USE_GPU", False):
            env["OLLAMA_VULKAN"] = "1"                    # experimental: Intel integrated graphics
        else:
            env["CUDA_VISIBLE_DEVICES"] = "-1"            # CPU only (also when testing on a PC with a GPU)
        logf = open(DATA / "ollama.log", "w", encoding="utf-8", errors="replace")
        subprocess.Popen([exe, "serve"], env=env, stdout=logf, stderr=logf, stdin=subprocess.DEVNULL,
                         creationflags=NO_WINDOW | 0x00000200)          # own process group: survives restarts
        log.info("started Ollama on %s (prompt RAM cache %s MB)", env["OLLAMA_HOST"], config.OLLAMA_PROMPT_CACHE_MB)
        return None

    def ensure_server(self) -> str | None:
        """Start Ollama if needed, pick the brain model and detect whether it can see. Error text or None."""
        self._unload_from_default_server()
        for attempt in range(60):
            try:
                tags = requests.get(f"{config.OLLAMA_URL}/api/tags", timeout=2).json()
                names = {m["name"] for m in tags.get("models", [])}
                names |= {n.removesuffix(":latest") for n in names}
                if config.OLLAMA_MODEL not in names:
                    if config.FALLBACK_MODEL in names:
                        log.warning("%s not downloaded; using %s", config.OLLAMA_MODEL, config.FALLBACK_MODEL)
                        config.OLLAMA_MODEL = config.FALLBACK_MODEL
                    else:
                        return (f"The AI model {config.OLLAMA_MODEL} isn't downloaded. "
                                f"Run setup, or: ollama pull {config.OLLAMA_MODEL}")
                caps = self.capabilities(config.OLLAMA_MODEL)
                if "vision" in caps:
                    tools.CTX.vision_model = config.OLLAMA_MODEL      # the brain sees the screen itself
                else:
                    tools.CTX.vision_model = config.VISION_MODEL if config.VISION_MODEL in names else None
                self.think_flag = "thinking" in caps or not caps
                log.info("Brain: %s (capabilities: %s); vision via %s", config.OLLAMA_MODEL,
                         ", ".join(sorted(caps)) or "?", tools.CTX.vision_model)
                return None
            except requests.RequestException:
                if attempt == 0:
                    err = self._start_server()
                    if err:
                        return err
                time.sleep(0.5)
        return "Couldn't start Ollama."

    # ---- prompt ----------------------------------------------------------------
    def system_prompt(self):
        import pyautogui
        from . import persona
        w, h = pyautogui.size()
        from .owner import CREATOR, OWNER
        return SYSTEM.format(creator=CREATOR, owner=OWNER, name=config.ASSISTANT_NAME, title=config.USER_TITLE, style=persona.get()["style"],
                             w=w, h=h, home=Path.home(), title_upper=config.USER_TITLE.upper())

    def _memory_msgs(self):
        """What Miles knows, as the opening exchange of every conversation.

        It sits after the (large, fixed) system prompt + tool list, so learning a new fact only re-reads
        this small block instead of the whole ~6k-token prompt.
        """
        from . import mail
        key = (memory.version(), mail.CONTACTS.stat().st_mtime if mail.CONTACTS.exists() else 0,
               tools.ROUTINES_FILE.stat().st_mtime if tools.ROUTINES_FILE.exists() else 0, config.USER_TITLE)
        if self._recent is None:
            n = getattr(config, "MEMORY_RECENT_IN_PROMPT", 5)
            self._recent = memory.recent_text(n) if n else ""
        if self._mem and self._mem[0] == key:
            return self._mem[1]
        t = config.USER_TITLE
        facts = memory.fact_texts()[-config.MEMORY_FACTS_IN_PROMPT:]
        contacts = [f"{c.get('name', k)} ({c['email']})" if c.get("email") else c.get("name", k)
                    for k, c in mail.load_contacts().items()]
        lines = ["[MEMORY - background for you, not a request]",
                 f"What you know about {t}:",
                 *([f"- {f}" for f in facts] or ["- nothing yet"]),
                 f"Contacts: {', '.join(contacts) or 'none saved'}",
                 f"Protocols: {', '.join(tools.load_routines()) or 'none'}"]
        if self._recent:
            lines += ["Your last conversations (before this session):", self._recent]
        msgs = [{"role": "user", "content": "\n".join(lines)}, {"role": "assistant", "content": "Noted."}]
        self._mem = (key, msgs)
        return msgs

    def _body(self, messages, stream=True, **options):
        cpu = {"num_thread": getattr(config, "OLLAMA_THREADS", 0) or None,
               "num_gpu": None if getattr(config, "OLLAMA_USE_GPU", False) else 0}
        body = {"model": config.OLLAMA_MODEL, "messages": messages, "tools": tools.schemas(), "stream": stream,
                "keep_alive": config.OLLAMA_KEEP_ALIVE,
                "options": {"num_ctx": config.OLLAMA_CONTEXT, "temperature": 0.4,
                            **{k: v for k, v in cpu.items() if v is not None}, **options}}
        if self.think_flag:
            body["think"] = False          # qwen3: skip slow "thinking" for snappy voice replies
        return body

    def _post(self, messages, **options):
        r = requests.post(f"{config.OLLAMA_URL}/api/chat", json=self._body(messages, stream=False, **options),
                          timeout=300)
        if r.status_code == 400 and "think" in r.text and self.think_flag:
            self.think_flag = False
            r = requests.post(f"{config.OLLAMA_URL}/api/chat", json=self._body(messages, stream=False, **options),
                              timeout=300)
        r.raise_for_status()
        return r.json()

    def warmup(self):
        """Load the model and pre-compute the system prompt + tools + memory, so the first reply is fast."""
        try:
            system = {"role": "system", "content": self.system_prompt()}
            d = self._post([system, {"role": "user", "content": "hi"}], num_predict=1)
            self.base_tokens = int(d.get("prompt_eval_count") or self.base_tokens)
            self._post([system] + self._memory_msgs() + [{"role": "user", "content": "hi"}], num_predict=1)
            if tools.CTX.vision_model == config.OLLAMA_MODEL:     # warm the image encoder too
                b64, _, _ = tools._screenshot_b64(640)
                self.side_call("Reply with one word: ready.", b64)
            log.info("brain warm (system + tools = %d tokens)", self.base_tokens)
        except Exception as e:
            log.warning("warmup failed: %s", e)

    # ---- conversation --------------------------------------------------------
    def reset(self):
        self.history = []
        self._recap = ""
        self._recent = None
        self._mem = None

    def _fit(self, pinned=None, reserve=0):
        """Keep memory + history inside the context window, so a long task never loses its instructions."""
        limit = (config.OLLAMA_CONTEXT - self.base_tokens - 900 - reserve) * _CHARS_PER_TOKEN

        def size():
            return sum(_chars(m) for m in (self._mem[1] if self._mem else []) + self.history)
        if size() <= limit:
            return
        tool_msgs = [m for m in self.history if m["role"] == "tool"]
        for m in tool_msgs[:-2]:                         # 1) shorten older tool results
            if len(m["content"]) > 400:
                m["content"] = m["content"][:400] + " …(trimmed)"
        while size() > limit and self.history and self.history[0] is not pinned:   # 2) drop older exchanges
            self.history.pop(0)
            while self.history and self.history[0]["role"] != "user" and self.history[0] is not pinned:
                self.history.pop(0)
        if size() > limit:                               # 3) squeeze the current task itself
            for m in self.history[:-2]:
                if m is not pinned and len(m.get("content") or "") > 300:
                    m["content"] = m["content"][:300] + " …(trimmed)"
        log.info("context trimmed to ~%d tokens", size() / _CHARS_PER_TOKEN)

    def _messages(self, pinned):
        mem = self._memory_msgs()
        self._fit(pinned)
        return [{"role": "system", "content": self.system_prompt()}] + mem + self.history

    def side_call(self, prompt: str, image_b64: str) -> str:
        """Ask about an image *inside* the current conversation. Reuses the cached prompt; a separate request
        would push it out of the model's cache and the next reply would take ~3 s longer."""
        mem = self._memory_msgs()
        self._fit(self.history[-1] if self.history else None, reserve=1700)
        msgs = ([{"role": "system", "content": self.system_prompt()}] + mem + self.history +
                [{"role": "user", "content": prompt + "\n(Answer in plain text now. Do not call any tools.)",
                  "images": [image_b64]}])
        m = self._post(msgs).get("message", {})
        text = re.sub(r"<think>.*?</think>", "", m.get("content") or "", flags=re.S).strip()
        if not text and m.get("tool_calls"):             # it tried to act instead of answering: ask without tools
            body = self._body(msgs, stream=False)
            body.pop("tools")
            r = requests.post(f"{config.OLLAMA_URL}/api/chat", json=body, timeout=300)
            text = (r.json().get("message") or {}).get("content", "").strip()
        return text

    def run(self, text: str, on_sentence, on_tool, see_context: bool = True) -> str:
        """see_context: read what's on screen (selection, text box, page, Explorer files) for "this"/"here"."""
        from . import context
        if time.time() - self.last > config.HISTORY_RESET_MINUTES * 60:
            self.reset()
        self.last = time.time()
        tools.CTX.active_brain = self
        tools.CTX.plan = []
        tools.CTX.writer = None
        tools.CTX.request_text = text
        now = dt.datetime.now().strftime("%a %d %b %Y, %I:%M %p")
        snap = None
        if see_context and context.refers_to_screen(text):
            t0 = time.time()
            snap = context.snapshot(selection=context.wants_selection(text),
                                    clipboard=bool(context._CLIPBOARD.search(text)),
                                    hovered=bool(context.POINTER.search(text)))
            log.info("context %.2fs: %s | %s", time.time() - t0, snap.title[:60], snap.details()[:300])
        tools.CTX.snapshot = snap
        active = snap.title if snap else context._title(context.target_window())
        details = snap.details() if snap else ""
        note = ""
        if memory.personal_hint(text):
            note = (f"\n(If this tells you something lasting about {config.USER_TITLE}, save it with remember "
                    f"as well as answering.)")
        # Lite: every request starts from the fixed, already-read prompt (system + tools + memory). Qwen3's chat
        # template writes earlier assistant turns differently from how they were generated, so keeping them made
        # the CPU re-read the whole conversation on every request (seconds each time on a laptop). The previous
        # exchange travels as a short recap instead, so follow-ups ("make it shorter") still work.
        recap = self._recap
        self.history = []
        cur = {"role": "user", "content": f"[{now} | active window: {active or 'desktop'}]" +
               (f"\n{recap}" if recap else "") +
               (f"\n[Context - {details}]\n" if details else " ") + f"{text}{note}"}
        self.history.append(cur)
        long_task = len(text.split()) >= 20 or len(re.findall(r"\b(then|after that|also|and)\b|[,;]",
                                                              text.lower())) >= 3
        reply, actions = [], []
        done_calls = set()
        try:
            lead = _lead_tool(text, snap)
            if lead and lead[0] == "__read__":           # "read this": say the selected text as it is
                sp = SentenceSplitter()
                for s in sp.feed(lead[1]["text"] + " ") + sp.flush():
                    on_sentence(s)
                self.history.append({"role": "assistant", "content": "(read the selected text aloud)"})
                actions.append("read the selection aloud")
                return lead[1]["text"][:200]
            if lead:
                name, args = lead
                on_tool(name, args)
                result = tools.execute(name, args)
                log.info("tool %s(%s) -> %s [lead]", name, json.dumps(args)[:200], result[:200])
                actions.append(f"{name} {json.dumps(args, ensure_ascii=False)[:100]}")
                done_calls.add(name + json.dumps(args, sort_keys=True))
                self.history.append({"role": "assistant", "content": "",
                                     "tool_calls": [{"function": {"name": name, "arguments": args}}]})
                self.history.append({"role": "tool", "tool_name": name, "content": result[:4000]})
            for _ in range(config.MAX_TOOL_ROUNDS):
                writer = tools.CTX.writer
                tools.CTX.writer = None
                if writer is not None and getattr(writer, "prompt", ""):
                    self.history.append({"role": "user", "content": writer.prompt})
                content, calls = self._stream(self._messages(cur), on_sentence, sink=writer.feed if writer else None)
                if not writer:
                    reply.append(content)
                msg = {"role": "assistant", "content": content}
                if calls:
                    msg["tool_calls"] = calls
                self.history.append(msg)
                written = None
                if writer:
                    if content.strip():
                        written = writer.finish()
                        log.info("write -> %s", written)
                        actions.append(f"wrote: {writer.request[:80]}")
                    else:
                        writer.cancel()
                if not calls:
                    if written and long_task:     # more to do: let it carry on with the rest
                        self.history.append({"role": "user", "content": (
                            f"[{written} If any part of the request is left, do it now; otherwise tell "
                            f"{config.USER_TITLE} it's done in one short sentence - don't repeat the text.]")})
                        continue
                    if written:                   # simple writing job: a fixed, truthful confirmation
                        line = writer.spoken()
                        on_sentence(line)
                        reply.append(line)
                        self.history.append({"role": "user", "content": f"[{written}]"})
                        self.history.append({"role": "assistant", "content": line})
                    break
                last_tool = None
                for c in calls:
                    fn = c.get("function", {})
                    name, args = fn.get("name", ""), fn.get("arguments") or {}
                    key = name + json.dumps(args, sort_keys=True)
                    if key in done_calls and name not in ("press_keys", "wait", "read_screen", "mouse",
                                                          "look_at_screen"):
                        last_tool = {"role": "tool", "tool_name": name, "content":
                                     "Already done successfully a moment ago. Do NOT repeat it. "
                                     "Continue with the next step or reply to the user."}
                        self.history.append(last_tool)
                        continue
                    done_calls.add(key)
                    on_tool(name, args)
                    t0 = time.time()
                    result = tools.execute(name, args)
                    log.info("tool %s(%s) -> %s [%.2fs]", name, json.dumps(args)[:200], result[:200],
                             time.time() - t0)
                    actions.append(f"{name} {json.dumps(args, ensure_ascii=False)[:100]}")
                    last_tool = {"role": "tool", "content": result[:4000], "tool_name": name}
                    self.history.append(last_tool)
                if last_tool is not None:
                    if written:
                        last_tool["content"] += f"\n[{written}]"
                    if long_task and not tools.CTX.writer:       # keep the whole request in view
                        steps = ""
                        if tools.CTX.plan:
                            steps = "; your plan: " + " | ".join(f"{i}. {s}" for i, s in enumerate(tools.CTX.plan, 1))
                        last_tool["content"] += (f"\n\n[Reminder - the full request was: \"{text}\"{steps}. Do the "
                                                 f"next unfinished part; when every part is done, reply briefly.]")
            else:
                on_sentence(f"That took more steps than I'm allowed, {config.USER_TITLE}. I stopped partway.")
        finally:
            if tools.CTX.writer:
                tools.CTX.writer.cancel()
                tools.CTX.writer = None
            self.last_actions = actions
            said = " ".join(r for r in reply if r).strip()
            wrote = next((m["content"] for m in reversed(self.history) if m["role"] == "assistant"
                          and m.get("content") and m["content"].strip() != said), "") if any(
                a.startswith("wrote") or a.startswith("write") for a in actions) else ""
            self._recap = (f'[Previous exchange: {config.USER_TITLE} said "{text[:200]}"; you replied "{said[:200]}"'
                           + (f'; the text you wrote was: "{wrote[:500]}"' if wrote else "")
                           + (f"; actions: {'; '.join(actions[:4])[:200]}" if actions else "") + "]")
        return " ".join(r for r in reply if r).strip()

    def _stream(self, messages, on_sentence, sink=None):
        """Stream a reply. Sentences go to on_sentence (spoken) - or, in writing mode, raw text goes to sink."""
        url = f"{config.OLLAMA_URL}/api/chat"
        with requests.post(url, json=self._body(messages), stream=True, timeout=(5, 300)) as r:
            if r.status_code == 400 and "think" in r.text and self.think_flag:
                self.think_flag = False
                return self._stream(messages, on_sentence, sink)
            if r.status_code != 200:
                raise RuntimeError(f"Ollama error {r.status_code}: {r.text[:200]}")
            raw, visible_len, calls = "", 0, []
            spoken = 0
            splitter = SentenceSplitter()
            for line in r.iter_lines():
                if not line:
                    continue
                d = json.loads(line)
                if "error" in d:
                    raise RuntimeError(d["error"])
                m = d.get("message") or {}
                if m.get("tool_calls"):
                    calls.extend(m["tool_calls"])
                if m.get("content"):
                    raw += m["content"]
                    visible = re.sub(r"<think>.*?(</think>|$)", "", raw, flags=re.S)
                    delta, visible_len = visible[visible_len:], len(visible)
                    if sink:
                        if tools.CTX.cancelled():
                            on_sentence("")                    # raises the caller's cancel
                        sink(delta)
                    else:
                        for s in splitter.feed(delta):
                            on_sentence(s)
                            spoken += 1
                        if self.sentence_limit and spoken >= self.sentence_limit and not calls:
                            log.info("reply cut after %d sentences (the rest would never be spoken)", spoken)
                            raw = raw.rstrip() + " …"
                            break                              # closing the stream stops the model: saves CPU
                if d.get("done"):
                    break
            if not sink:
                for s in splitter.flush():
                    on_sentence(s)
        return re.sub(r"<think>.*?</think>", "", raw, flags=re.S).strip(), calls

    def _trim(self, keep=30):
        if len(self.history) > keep:
            self.history = self.history[-keep:]
        while self.history and self.history[0]["role"] != "user":
            self.history.pop(0)
