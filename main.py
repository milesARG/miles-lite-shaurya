"""Miles - a JARVIS-style voice assistant that controls your Windows PC.

Say "Hey Miles, ..." or press Ctrl+Alt+M, then speak.
"""
import ctypes
import os
import queue
import random
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")    # numpy's BLAS reserves ~500 MB for threads it never uses

try:                                            # crisp UI + correct mouse coordinates on scaled displays
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    ctypes.windll.user32.SetProcessDPIAware()

if "--restart" in sys.argv:
    time.sleep(2)
_mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\MilesLiteSingleton")
if ctypes.windll.kernel32.GetLastError() == 183:  # already running
    sys.exit(0)

import config  # noqa: E402
from miles.util import DATA, log, run_ps, setup_logging  # noqa: E402
from miles import owner  # noqa: E402

setup_logging()
FIRST_RUN = owner.first_run()
_refused = owner.check()
if _refused:
    ctypes.windll.user32.MessageBoxW(None, _refused, f"Miles  -  {owner.CREDIT}", 0x40 | 0x40000)
    sys.exit(0)
log.info("Miles Lite - %s", owner.CREDIT)

import psutil  # noqa: E402
import requests  # noqa: E402

from miles import audio, context, memory, persona, router, tools  # noqa: E402
from miles.apps import AppIndex  # noqa: E402
from miles.audio import ABORT, Listener, Microphone, Transcriber, split_wake  # noqa: E402
from miles.brain import Brain  # noqa: E402
from miles.buddy import Buddy  # noqa: E402
from miles.voice import Speaker, speakable  # noqa: E402

class _Title:
    """How Miles addresses you ("sir" / "boss"); follows the active persona."""

    def __str__(self):
        return config.USER_TITLE

    def __format__(self, spec):
        return format(str(self), spec)

    def capitalize(self):
        return str(self).capitalize()


T = _Title()
YES = re.compile(r"\b(yes|yeah|yep|yup|sure|do it|go ahead|confirm(ed)?|affirmative|proceed|ok(ay)?|of course|"
                 r"absolutely|please do|go for it|correct|right)\b")
NO = re.compile(r"\b(no|nope|nah|don'?t|do not|cancel|stop|negative|wait|never ?mind|abort|not)\b")
# Small models tack these on despite being told not to; JARVIS wouldn't say them.
FILLER = re.compile(r"^(just )?(let me know|is there anything else|anything else i can|feel free to|if you need "
                    r"anything|if there'?s anything|i'?m here if you need|i'?m here to (help|assist)|don'?t hesitate|"
                    r"say the word|you may review it)", re.I)

TOOL_LABELS = {
    "open_app": "Launching {name}", "close_app": "Closing {name}", "manage_window": "{action} {name}",
    "open_website": "Opening {url}", "web_search": "Searching: {query}", "play_youtube": "Finding “{query}”",
    "find_files": "Searching files for “{name}”", "file_action": "{action}: {path}", "read_screen": "Scanning the window",
    "click_element": "{action} “{name}”", "type_text": "Typing…", "press_keys": "Pressing {keys}",
    "security_check": "Running security sweep", "system_status": "Reading system sensors",
    "run_command": "Running a command", "weather": "Checking the weather", "set_reminder": "Setting a reminder",
    "look_at_screen": "Looking at the screen", "volume": "Adjusting volume", "media_control": "Media: {action}",
    "browser_tab": "Tab: {action} {title}", "spotify": "Spotify: {action} {query}",
    "compose_email": "Drafting an email to {to}", "send_email": "Sending the email", "research": "Researching: {query}",
    "read_webpage": "Reading the page", "news": "Fetching headlines", "briefing": "Compiling your briefing",
    "create_document": "Writing “{title}”", "organize_folder": "Organizing {path}", "install_app": "Installing {name}",
    "routine": "Protocol: {action} {name}", "click_on_screen": "Clicking {target}", "save_contact": "Saving {name}",
    "switch_persona": "Switching to {name}", "write": "Writing: {request}", "ask_user": "Waiting for your answer",
    "remember": "Saving to memory", "recall": "Searching memory: {query}", "plan": "Planning the steps",
    "get_context": "Looking at what you've selected", "voice_settings": "Adjusting my voice",
}

def _complete(text: str) -> bool:
    """A finished command can end after a short pause: one the fast router knows, or a question."""
    return text.rstrip().endswith("?") or router.route(text) is not None


def prewarm_lines():
    return [f"Very well, {T}.", f"Standing by, {T}.", "Of course.", f"Always a pleasure, {T}.", "Anytime.",
            f"My pleasure, {T}.", f"No answer, so I'll hold off, {T}.", f"Sorry, {T}. Yes or no?", "As you wish.",
            f"Done, {T}.", "Consider it done.", f"Tab closed, {T}.", f"Spotify is up, {T}.", "Volume 50%."]


class Cancelled(Exception):
    pass


class Miles:
    def __init__(self):
        self.ui = Buddy(on_talk=self.talk, on_menu=self.popup_menu)
        self.trigger = threading.Event()
        self.cancel = threading.Event()
        self.typed: queue.Queue = queue.Queue()
        self.wake_enabled = config.WAKE_WORD_ENABLED
        self.busy = False
        self.session = None
        self.mic = None
        self.stt = None
        persona.load()
        self.speaker = Speaker()
        self.speaker.on_sentence = self._on_audio
        self.spoke_end = 0.0                    # when you stopped talking (latency tracing)
        self._latency_from = None
        self.brain = Brain()
        self.task_brain = Brain()               # separate memory for protocol steps / scheduled commands
        tools.CTX.apps = AppIndex()
        tools.CTX.announce = self.announce
        tools.CTX.confirm = self.confirm
        tools.CTX.ask = self.ask
        tools.CTX.status = lambda text: self.ui.set_state("working", text)
        tools.CTX.run_steps = self.run_steps
        tools.CTX.cancelled = self.cancel.is_set
        tools.CTX.side_call = lambda prompt, b64: (tools.CTX.active_brain or self.brain).side_call(prompt, b64)
        self.tray = None
        self.last_activity = time.time()

    def run_steps(self, steps):
        """Run plain-English commands one after another (protocols, scheduled tasks). Returns their results."""
        results = []
        for i, step in enumerate(steps, 1):
            self.ui.set_state("working", step, header=f"PROTOCOL  ·  STEP {i}/{len(steps)}")
            try:
                r = router.route(step)
                if r:
                    res = r.run() or "Done."
                else:
                    said = []
                    res = (self.task_brain.run(step, said.append, lambda n, a: None, see_context=False)
                           or " ".join(said) or "Done.")
                    self.task_brain.reset()
            except Exception as e:
                res = f"Error: {e}"
            log.info("step %r -> %s", step, str(res)[:150])
            results.append(str(res))
            time.sleep(0.25)
        return results

    # ------------------------------------------------------------------ startup
    def start(self):
        threading.Thread(target=self._boot, daemon=True, name="miles").start()
        self._start_tray()
        self.ui.run()
        os._exit(0)

    def _boot(self):
        try:
            audio._add_cuda_dll_dirs()                      # before any GPU library loads (Whisper + voice)
            context.watch_foreground()
            self.ui.set_state("boot", f"Bringing neural core online…  {owner.CREDIT}.")
            err = self.brain.ensure_server()
            self.task_brain.think_flag = self.brain.think_flag
            if not err:
                threading.Thread(target=self.brain.warmup, daemon=True).start()
            voice = threading.Thread(target=lambda: (self.speaker.load(), self.speaker.prewarm(prewarm_lines())),
                                     daemon=True, name="voice-load")
            voice.start()                                   # loads in parallel with speech recognition
            self.ui.set_state("boot", "Calibrating audio sensors…")
            self.mic = Microphone(on_level=self.ui.set_level)
            self.ui.set_state("boot", "Loading speech recognition…")
            self.stt = Transcriber()
            self.listener = Listener(self.mic, self.stt)
            self.ui.set_state("boot", "Loading voice…")
            voice.join(60)
            self._start_hotkey()
            threading.Thread(target=self._command_server, daemon=True, name="ipc").start()
            if config.PROACTIVE_ALERTS:
                threading.Thread(target=self._watchdog, daemon=True, name="watchdog").start()
            log.info("Miles online (whisper on %s)", self.stt.device)
            if err:
                self.say(err, state="error")
            else:
                h = time.localtime().tm_hour
                part = "morning" if h < 12 else "afternoon" if h < 17 else "evening"
                me, you = owner.CREATOR, owner.OWNER
                if FIRST_RUN:
                    self.say(f"Hello {you}. I'm Miles, your personal assistant, built for you by {me}. "
                             f"Say “Hey Miles” or press Control Alt M whenever you need me.", state="success")
                else:
                    self.say(random.choice([f"Good {part}, {you}. Miles online. Built by {me}, at your service.",
                                            f"Good {part}, {you}. All systems online, courtesy of {me}.",
                                            f"Welcome back, {you}. {me}'s finest work is up and running.",
                                            f"Good {part}, {you}. Miles, made by {me}, reporting for duty."]),
                             state="success")
            self.ui.set_state("idle" if self.wake_enabled else "sleep")
            if config.LOW_RAM:
                threading.Thread(target=self._ram_janitor, daemon=True, name="ram").start()
            self._loop()
        except Exception as e:
            log.exception("fatal")
            self.ui.set_state("error", f"Startup failed: {e}. See data\\miles.log")

    def _command_server(self):
        """Accept typed commands from local scripts: `python miles_cmd.py "open spotify"`."""
        import socket
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            srv.bind(("127.0.0.1", config.COMMAND_PORT))
        except OSError as e:
            log.warning("command port busy: %s", e)
            return
        srv.listen(4)
        while True:
            conn, _ = srv.accept()
            with conn:
                conn.settimeout(3)
                try:
                    data = b""
                    while len(data) < 200_000:              # long instructions arrive in several packets
                        chunk = conn.recv(65536)
                        if not chunk:
                            break
                        data += chunk
                        if len(chunk) < 65536:
                            break
                    text = data.decode("utf-8", "replace").strip()
                    if text:
                        self.submit(text)
                        conn.sendall(b"ok\n")
                except OSError:
                    pass

    def _start_hotkey(self):
        from pynput import keyboard
        try:
            keyboard.GlobalHotKeys({config.HOTKEY: self.talk}).start()
        except Exception as e:
            log.warning("hotkey failed: %s", e)

    # ------------------------------------------------------------------ main loop
    def talk(self):
        """Hotkey / double-click: interrupt whatever is happening and listen."""
        if self.speaker.speaking:
            self.speaker.stop()
        if self.busy:
            self.cancel.set()
        self.trigger.set()

    def _loop(self):
        while True:
            kind, text = self._wait_trigger()
            try:
                self.busy = True
                if kind == "hotkey":
                    self.mic.flush()
                    text = self._listen_command()
                elif kind == "wake" and not text:
                    # just "Hey Miles": chime and keep listening - no spoken reply, no lost audio
                    text = self._listen_command()
                if text:
                    self.conversation(text, "typed" if kind == "typed" else "voice")
            except Exception as e:
                log.exception("loop error")
                self.ui.set_state("error", str(e))
            finally:
                self.busy = False
                self.last_activity = time.time()
                self.cancel.clear()
                self._finish_speech()
                if self.ui.state not in ("error",):
                    self.ui.set_state("idle" if self.wake_enabled else "sleep")

    def _wait_trigger(self):
        while True:
            if not self.typed.empty():
                self.trigger.clear()
                return "typed", self.typed.get()
            if self.trigger.is_set():
                self.trigger.clear()
                self.cancel.clear()
                return "hotkey", None
            if not self.wake_enabled or self._saving_battery():
                self.mic.flush()                # nobody's listening: don't let mic audio pile up in memory
                self.trigger.wait(0.3)
                continue
            live = [False]

            def on_partial(text):
                woke, rest = split_wake(text)
                if woke:                                   # show your words the moment "Miles" is heard
                    live[0] = True
                    self.ui.set_state("listening", rest or "…", header="YOU", instant=True)

            def is_complete(text):
                woke, rest = split_wake(text)
                return not woke or not rest or _complete(rest)

            # the tiny model listens for "Miles"; only speech addressed to Miles gets the accurate model
            utt = self.listener.listen(abort=self.trigger, on_partial=on_partial, is_complete=is_complete,
                                       report_level=lambda: live[0], quick=True)
            if utt is ABORT or utt is None or not utt.text:
                if live[0]:
                    self.ui.set_state("idle", hold=0)
                continue
            woke, rest = split_wake(utt.text)
            log.info("heard: %s%s", utt.text, "  [WAKE]" if woke else "")
            if woke:
                self.spoke_end = utt.end
                if rest and router.route(rest) is None:       # not an instant command: get the accurate text
                    accurate = self.stt.final(utt.audio)
                    w2, rest2 = split_wake(accurate)
                    rest = rest2 if w2 else (accurate or rest)
                    self.ui.set_state("listening", rest or "…", header="YOU", instant=True)
                return "wake", rest
            if live[0]:
                self.ui.set_state("idle", hold=0)

    _battery = (0.0, False)

    def _saving_battery(self) -> bool:
        """On battery with WAKE_WORD_ON_BATTERY off: don't keep the mic + speech model running."""
        if getattr(config, "WAKE_WORD_ON_BATTERY", True):
            return False
        t, on_batt = self._battery
        if time.time() - t > 30:
            b = psutil.sensors_battery()
            on_batt = bool(b and not b.power_plugged)
            self._battery = (time.time(), on_batt)
        return on_batt

    def _captions(self, text):
        self.ui.set_state("listening", text, header="YOU", instant=True)

    def _listen_command(self, chime=True, timeout=None, follow_up=False):
        """Listen for a command, showing your words live. No flush: audio already spoken is kept."""
        if chime:
            audio.chime("wake")
        self.ui.set_state("listening", "…", header="YOU" + ("  ·  FOLLOW-UP" if follow_up else ""), instant=True)
        utt = self.listener.listen(start_timeout=timeout or config.COMMAND_START_TIMEOUT,
                                   abort=self.trigger if follow_up else None, on_partial=self._captions,
                                   is_complete=lambda t: _complete(split_wake(t)[1] or t), quick=True)
        if utt is None or utt is ABORT:
            return None
        text = utt.text
        self.spoke_end = utt.end
        if not (text and router.route(split_wake(text)[1] or text) is not None):
            text = self.stt.final(utt.audio) or text      # not an instant command: use the accurate model
            self._captions(text) if text else None
        woke, rest = split_wake(text)
        if woke and rest:
            text = rest
        if not text and not follow_up:
            self.ui.set_state("idle", "I didn't catch that.", hold=2)
        return text

    def conversation(self, text, mode="voice"):
        while text:
            reply = self.handle(text, mode)
            asked = reply.rstrip().endswith("?")
            window = 8 if asked else config.FOLLOW_UP_SEC
            if window <= 0 or self.trigger.is_set():
                return
            text = self._listen_command(chime=False, timeout=window, follow_up=True)
            mode = "voice"

    # ------------------------------------------------------------------ handling
    def _on_audio(self, sentence):
        """First words of a reply are playing: log how long after you stopped talking that was."""
        start, self._latency_from = self._latency_from, None
        if start:
            log.info("latency: first words %.2fs after you stopped talking", time.time() - start)

    def handle(self, text: str, mode: str = "voice") -> str:
        log.info("USER: %s", text)
        tools.CTX.input_mode = mode
        self.ui.set_state("thinking", text, header="YOU  ·  PROCESSING", instant=True)
        t0 = time.time()
        self._latency_from = self.spoke_end if mode == "voice" and t0 - self.spoke_end < 30 else t0
        if mode == "voice":
            log.info("latency: text ready %.2fs after you stopped talking", t0 - self.spoke_end)
        r = router.route(text)
        if r:
            if r.ack:
                self.say(r.ack, state="working")
            try:
                reply = r.run()
            except Exception as e:
                log.exception("route failed")
                reply = f"That didn't work, {T}. {e}"
            log.info("fast path %.2fs -> %s", time.time() - t0, reply)
            memory.log_exchange(text, reply or "(done)")
            if reply:
                self.say(reply, state="speaking")
            else:
                audio.chime("done")
                self.ui.set_state("success", f"“{text}”", hold=2)
                time.sleep(0.3)
            return reply or ""

        first = [True]
        spoken = [0]
        detailed = re.search(r"\b(explain|tell me (about|more)|describe|read|in detail|summar|story|list|"
                             r"step by step|how (do|does|to))\b", text.lower())

        def on_sentence(s):
            if self.cancel.is_set():
                raise Cancelled
            s = speakable(s)
            if not s or FILLER.match(s):
                return
            if first[0]:
                self.ui.set_state("speaking", s)
                first[0] = False
            else:
                self.ui.append(s)
            if detailed or spoken[0] < config.MAX_SPOKEN_SENTENCES:   # extra detail stays on the HUD only
                self._feed(s)
                spoken[0] += 1

        def on_tool(name, args):
            if self.cancel.is_set():
                raise Cancelled
            try:
                label = TOOL_LABELS.get(name, name.replace("_", " ")).format_map(_Safe(args))
            except Exception:
                label = name
            self.ui.set_state("working", label, header=f"EXECUTING  ·  {name.upper()}")
            first[0] = True

        reply = ""
        try:
            # a laptop CPU writes ~5-10 words/s: stop the model once it has said what will be spoken
            self.brain.sentence_limit = None if detailed else config.MAX_SPOKEN_SENTENCES
            reply = self.brain.run(text, on_sentence, on_tool)
        except Cancelled:
            log.info("cancelled by user")
            return ""
        except requests.ConnectionError:
            on_sentence(f"I've lost contact with my neural core, {T}. Ollama isn't responding.")
            self.ui.set_state("error", "Ollama isn't responding.")
        except Exception as e:
            log.exception("brain failed")
            on_sentence(f"Something went wrong, {T}.")
            self.ui.set_state("error", str(e)[:200])
        log.info("AI path %.2fs -> %s", time.time() - t0, reply)
        memory.log_exchange(text, reply, self.brain.last_actions)
        self._finish_speech()
        return reply

    # ------------------------------------------------------------------ speech
    def _feed(self, sentence):
        if self.session is None:
            self.session = self.speaker.session()
            self.mic.muted = True
        self.session.feed(sentence)

    def _finish_speech(self):
        if self.session is not None:
            self.session.close()
            self.session.wait()
            self.session = None
        if self.mic:
            self.mic.muted = False
            self.mic.flush()

    def say(self, text, state="speaking"):
        self._finish_speech()
        self.ui.set_state(state, text)
        if self.mic:
            self.mic.muted = True
        try:
            self.speaker.say(text)
        finally:
            if self.mic:
                self.mic.muted = False
                self.mic.flush()

    def announce(self, text):
        """Speak something unprompted (reminders, scan results, alerts)."""
        audio.chime("done")
        time.sleep(0.25)
        self.ui.set_state("speaking", text)
        if self.mic:
            self.mic.muted = True
        self.speaker.say(text)
        if self.mic and not self.busy:
            self.mic.muted = False
        if not self.busy:
            self.ui.set_state("idle")

    def confirm(self, question: str) -> bool:
        self._finish_speech()
        for attempt in range(2):
            self.say(question if attempt == 0 else f"Sorry, {T}. Yes or no?", state="confirm")
            self.ui.set_state("confirm", question, header="CONFIRM  ·  SAY YES OR NO")
            utt = self.listener.listen(
                start_timeout=8, on_partial=lambda t: self.ui.set_state("confirm", t, header="YOU", instant=True),
                is_complete=lambda t: bool(YES.search(t.lower()) or NO.search(t.lower())), quick=True)
            if utt is None or utt is ABORT:
                self.say(f"No answer, so I'll hold off, {T}.", state="idle")
                return False
            ans = utt.text.lower()
            log.info("confirm answer: %s", ans)
            if NO.search(ans):
                return False
            if YES.search(ans):
                self.ui.set_state("working", "Confirmed.")
                return True
        return False

    def ask(self, question: str, suggestion: str = ""):
        """A box pops up where you type something Miles couldn't get (an email address, a spelling...).
        Returns the text, or None if you skip it."""
        self._finish_speech()
        done = threading.Event()
        box = {"text": None, "win": None}
        self.ui.call(lambda: self._ask_box(question, suggestion, box, done))
        self.ui.set_state("confirm", question, header="TYPE YOUR ANSWER IN THE BOX")
        if self.mic:
            self.mic.muted = True
        try:
            self.speaker.say(question)
        finally:
            if self.mic:
                self.mic.muted = False
                self.mic.flush()
        end = time.time() + 180
        while not done.wait(0.2):
            if self.cancel.is_set() or time.time() > end:
                self.ui.call(lambda: box["win"] and box["win"].destroy())
                log.info("ask: %s -> (no answer)", question)
                return None
        log.info("ask: %s -> %s", question, box["text"])
        self.ui.set_state("working", "Got it." if box["text"] else "Skipped.")
        if box["text"]:
            tools.CTX.typed_answers.add(box["text"].strip().lower())
        return box["text"]

    def _ask_box(self, question, suggestion, box, done):
        import tkinter as tk
        bg, fg, accent = "#060d13", "#e6f7ff", "#00e5ff"
        top = tk.Toplevel(self.ui.root)
        box["win"] = top
        top.title(f"{config.ASSISTANT_NAME} needs a detail")
        top.configure(bg=bg)
        top.attributes("-topmost", True)
        top.resizable(False, False)
        w = 580
        tk.Label(top, text=f"{config.ASSISTANT_NAME.upper()}  //  I NEED A DETAIL", bg=bg, fg=accent,
                 font=("Consolas", 10, "bold")).pack(anchor="w", padx=18, pady=(16, 6))
        tk.Label(top, text=question, bg=bg, fg=fg, font=("Segoe UI", 12), wraplength=w - 36,
                 justify="left").pack(anchor="w", padx=18, pady=(0, 10))
        e = tk.Entry(top, bg="#0b1b25", fg=fg, insertbackground=accent, relief="flat", font=("Segoe UI", 13),
                     highlightthickness=1, highlightcolor=accent, highlightbackground="#12384a")
        e.pack(fill="x", padx=18, ipady=7)
        if suggestion:
            e.insert(0, suggestion)
            e.select_range(0, "end")
        row = tk.Frame(top, bg=bg)
        row.pack(fill="x", padx=18, pady=(12, 16))
        tk.Label(row, text="Enter = OK   ·   Esc = skip", bg=bg, fg="#5d8799",
                 font=("Segoe UI", 9)).pack(side="left")

        def finish(value):
            if done.is_set():
                return
            box["text"] = value
            done.set()
            top.destroy()
        style = dict(relief="flat", font=("Segoe UI", 10, "bold"), padx=14, pady=4, cursor="hand2", bd=0)
        tk.Button(row, text="OK", bg=accent, fg="#021016", activebackground="#7ff3ff",
                  command=lambda: finish(e.get().strip()), **style).pack(side="right")
        tk.Button(row, text="Skip", bg="#12384a", fg=fg, activebackground="#1b4d63",
                  command=lambda: finish(None), **style).pack(side="right", padx=(0, 8))
        e.bind("<Return>", lambda _: finish(e.get().strip()))
        e.bind("<Escape>", lambda _: finish(None))
        top.protocol("WM_DELETE_WINDOW", lambda: finish(None))
        top.update_idletasks()
        h = top.winfo_reqheight()
        top.geometry(f"{w}x{h}+{(top.winfo_screenwidth() - w) // 2}+{top.winfo_screenheight() // 3}")
        top.after(80, lambda: self._grab_focus(top, e))

    @staticmethod
    def _grab_focus(top, widget):
        """Windows won't let a background app take focus; tools.focus_hwnd works around that."""
        try:
            tools.focus_hwnd(ctypes.windll.user32.GetParent(top.winfo_id()))
        except Exception:
            pass
        top.focus_force()
        widget.focus_set()

    # ------------------------------------------------------------------ memory & RAM
    def _ram_janitor(self):
        """Give Windows back the RAM that was only needed while starting up (model loading, CUDA set-up).
        Measured: ~1.7 GB -> ~0.25 GB with no slowdown. Repeats after long idle spells."""
        import ctypes.wintypes as wt
        k32, psapi = ctypes.WinDLL("kernel32"), ctypes.WinDLL("psapi")
        k32.GetCurrentProcess.restype = wt.HANDLE
        psapi.EmptyWorkingSet.argtypes = [wt.HANDLE]
        time.sleep(45)                               # let the warm-ups finish first
        last = 0.0
        while True:
            idle = time.time() - self.last_activity
            if not self.busy and (last == 0.0 or (idle > 300 and time.time() - last > 1800)):
                import gc
                gc.collect()
                before = psutil.Process().memory_info().rss
                psapi.EmptyWorkingSet(k32.GetCurrentProcess())
                last = time.time()
                log.info("RAM: working set %.0f MB -> %.0f MB", before / 1e6, psutil.Process().memory_info().rss / 1e6)
            time.sleep(60)

    def _memory_window(self):
        import tkinter as tk
        bg, fg, accent = "#060d13", "#e6f7ff", "#00e5ff"
        top = tk.Toplevel(self.ui.root)
        top.title(f"{config.ASSISTANT_NAME} - memory")
        top.configure(bg=bg)
        top.attributes("-topmost", True)
        w, h = 640, 480
        top.geometry(f"{w}x{h}+{(top.winfo_screenwidth() - w) // 2}+{(top.winfo_screenheight() - h) // 3}")
        tk.Label(top, text=f"{config.ASSISTANT_NAME.upper()}  //  MEMORY", bg=bg, fg=accent,
                 font=("Consolas", 10, "bold")).pack(anchor="w", padx=18, pady=(14, 2))
        tk.Label(top, text="What I know about you - one fact per line. Edit, add or delete lines, then Save.",
                 bg=bg, fg="#8fb3c4", font=("Segoe UI", 9)).pack(anchor="w", padx=18, pady=(0, 8))
        row = tk.Frame(top, bg=bg)
        row.pack(side="bottom", fill="x", padx=18, pady=(8, 14))
        txt = tk.Text(top, bg="#0b1b25", fg=fg, insertbackground=accent, relief="flat", font=("Segoe UI", 11),
                      wrap="word", highlightthickness=1, highlightcolor=accent, highlightbackground="#12384a",
                      padx=10, pady=8)
        txt.pack(fill="both", expand=True, padx=18)
        txt.insert("1.0", "\n".join(memory.fact_texts()))
        status = tk.Label(row, text="", bg=bg, fg="#5d8799", font=("Segoe UI", 9))
        status.pack(side="left")

        def save():
            memory.save_texts([ln.strip() for ln in txt.get("1.0", "end").splitlines() if ln.strip()])
            status.config(text=f"Saved {len(memory.fact_texts())} facts.")
        style = dict(relief="flat", font=("Segoe UI", 10, "bold"), padx=14, pady=4, cursor="hand2", bd=0)
        tk.Button(row, text="Save", bg=accent, fg="#021016", command=save, **style).pack(side="right")
        tk.Button(row, text="Close", bg="#12384a", fg=fg, command=top.destroy, **style).pack(side="right",
                                                                                              padx=(0, 8))
        tk.Button(row, text="Conversation history", bg="#12384a", fg=fg,
                  command=lambda: subprocess.Popen(["notepad.exe", str(memory.LOG_FILE)]) if memory.LOG_FILE.exists()
                  else None, **style).pack(side="right", padx=(0, 8))
        top.after(80, lambda: self._grab_focus(top, txt))

    # ------------------------------------------------------------------ proactive alerts
    def _watchdog(self):
        last = {}
        cpu_hist = []
        time.sleep(60)

        def alert(key, msg):
            if time.time() - last.get(key, 0) > config.ALERT_COOLDOWN_MIN * 60 and not self.busy:
                last[key] = time.time()
                log.info("ALERT %s: %s", key, msg)
                self.announce(msg)

        n = 0
        while True:
            try:
                cpu_hist = (cpu_hist + [psutil.cpu_percent(1)])[-4:]
                if len(cpu_hist) == 4 and min(cpu_hist) > 90:
                    top = tools.system_status("processes").split("\n")[0].replace("Top CPU: ", "")
                    alert("cpu", f"{T.capitalize()}, your CPU has been above 90 percent for a while. "
                                 f"The biggest users are {top.split(',')[0]}.")
                if psutil.virtual_memory().percent > getattr(config, "RAM_ALERT_PERCENT", 93):
                    alert("ram", f"Memory is nearly full, {T}. Closing a few apps would help.")
                du = psutil.disk_usage(os.environ.get("SystemDrive", "C:") + "\\")
                if du.free < 5e9:
                    alert("disk", f"Your system drive has only {du.free / 1e9:.1f} gigabytes left, {T}. "
                                  f"I can clean temporary files if you like.")
                b = psutil.sensors_battery()
                if b and not b.power_plugged and b.percent < 15:
                    alert("battery", f"Battery is at {b.percent:.0f} percent, {T}. You may want to plug in.")
                if n % 30 == 0:
                    _, out, _ = run_ps("(Get-MpComputerStatus).RealTimeProtectionEnabled", 30)
                    if out.strip().lower() == "false":
                        alert("defender", f"Warning, {T}. Windows Defender real-time protection is switched off. "
                                          f"Your PC is exposed to threats.")
            except Exception as e:
                log.debug("watchdog: %s", e)
            n += 1
            time.sleep(60)

    # ------------------------------------------------------------------ menus & tray
    def menu_items(self):
        return [
            (f"Miles  ·  {owner.CREDIT}", self.about, None),
            None,
            ("Talk to Miles  (Ctrl+Alt+M)", self.talk, None),
            ("Type a command…", lambda: self.ui.call(self._ask_text), None),
            None,
            ("Wake word “Hey Miles”", self.toggle_wake, lambda: self.wake_enabled),
            ("Show buddy", self.toggle_buddy, lambda: self.ui.visible),
            ("Start with Windows", self.toggle_startup, _startup_enabled),
            None,
            ("Scan PC for threats", lambda: self.submit("scan my pc for threats"), None),
            ("System status", lambda: self.submit("how is my pc doing"), None),
            ("Memory…", lambda: self.ui.call(self._memory_window), None),
            None,
            ("Settings", lambda: subprocess.Popen(["notepad.exe", str(ROOT / "config.py")]), None),
            ("Open log", lambda: subprocess.Popen(["notepad.exe", str(DATA / "miles.log")]), None),
            ("Restart Miles", self.restart, None),
            ("Quit", self.quit, None),
        ]

    def about(self):
        threading.Thread(target=lambda: ctypes.windll.user32.MessageBoxW(
            None, f"Miles Lite, a personal AI assistant.\n\nMade by {owner.CREATOR}, especially for {owner.OWNER}."
                  f"\n\nThis copy is {owner.OWNER}'s alone and runs only on this PC.",
            f"About Miles  -  {owner.CREDIT}", 0x40 | 0x40000), daemon=True).start()

    def submit(self, text):
        self.typed.put(text)
        self.trigger.set()

    def toggle_wake(self):
        self.wake_enabled = not self.wake_enabled
        self.ui.set_state("idle" if self.wake_enabled else "sleep",
                          "Wake word on. Say “Hey Miles”." if self.wake_enabled else
                          "Wake word off. Press Ctrl+Alt+M to talk.", hold=3)
        if self.tray:
            self.tray.update_menu()

    def toggle_buddy(self):
        self.ui.visible = not self.ui.visible
        if self.tray:
            self.tray.update_menu()

    def toggle_startup(self):
        f = _startup_file()
        if f.exists():
            f.unlink()
        else:
            pyw = Path(sys.executable).with_name("pythonw.exe")
            f.write_text(f'CreateObject("WScript.Shell").Run """{pyw}"" ""{ROOT / "main.py"}""", 0, False\n',
                         encoding="utf-8")
        if self.tray:
            self.tray.update_menu()

    def restart(self):
        pyw = Path(sys.executable).with_name("pythonw.exe")
        subprocess.Popen([str(pyw if pyw.exists() else sys.executable), str(ROOT / "main.py"), "--restart"])
        self.quit()

    def quit(self):
        try:
            if self.tray:
                self.tray.stop()
        except Exception:
            pass
        self.ui.quit()
        threading.Timer(1.0, lambda: os._exit(0)).start()

    def popup_menu(self, x, y):
        import tkinter as tk
        m = tk.Menu(self.ui.root, tearoff=0, bg="#08131b", fg="#cfefff", activebackground="#0a3a4d",
                    activeforeground="#ffffff", bd=0, font=("Segoe UI", 10))
        for it in self.menu_items():
            if it is None:
                m.add_separator()
                continue
            label, fn, checked = it
            if checked:
                label = ("✓  " if checked() else "     ") + label
            else:
                label = "     " + label
            m.add_command(label=label, command=fn)
        m.tk_popup(x, y)

    def _ask_text(self):
        import tkinter as tk
        top = tk.Toplevel(self.ui.root)
        top.title("Miles")
        top.configure(bg="#060d13")
        top.attributes("-topmost", True)
        top.resizable(False, False)
        w, h = 600, 200
        top.geometry(f"{w}x{h}+{(top.winfo_screenwidth() - w) // 2}+{top.winfo_screenheight() // 3}")
        tk.Label(top, text="MILES  //  TYPE A COMMAND", bg="#060d13", fg="#00e5ff",
                 font=("Consolas", 10, "bold")).pack(anchor="w", padx=16, pady=(14, 2))
        tk.Label(top, text="Enter = send   ·   Shift+Enter = new line   ·   long, multi-step instructions welcome",
                 bg="#060d13", fg="#5d8799", font=("Segoe UI", 9)).pack(anchor="w", padx=16, pady=(0, 6))
        e = tk.Text(top, bg="#0b1b25", fg="#e6f7ff", insertbackground="#00e5ff", relief="flat",
                    font=("Segoe UI", 12), highlightthickness=1, highlightcolor="#00e5ff",
                    highlightbackground="#12384a", wrap="word", height=4, padx=8, pady=6)
        e.pack(fill="both", expand=True, padx=16, pady=(0, 14))

        def go(_=None):
            t = e.get("1.0", "end").strip()
            top.destroy()
            if t:
                self.submit(t)
            return "break"
        e.bind("<Return>", go)
        e.bind("<Shift-Return>", lambda _: None)           # a normal new line
        e.bind("<Escape>", lambda _: top.destroy())
        top.after(50, lambda: self._grab_focus(top, e))

    def _start_tray(self):
        try:
            import pystray
            from PIL import Image, ImageDraw
        except Exception:
            return
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.ellipse((4, 4, 60, 60), outline=(0, 229, 255, 255), width=5)
        d.ellipse((20, 20, 44, 44), fill=(0, 229, 255, 255))
        items = []
        for it in self.menu_items():
            if it is None:
                items.append(pystray.Menu.SEPARATOR)
                continue
            label, fn, checked = it
            items.append(pystray.MenuItem(label, (lambda f: lambda icon, item: f())(fn),
                                          checked=(lambda c: lambda item: c())(checked) if checked else None,
                                          default=label.startswith("Talk")))
        self.tray = pystray.Icon("Miles", img, f"Miles — {owner.CREDIT}", pystray.Menu(*items))
        self.tray.run_detached()


class _Safe(dict):
    def __missing__(self, key):
        return ""


def _startup_file():
    return Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "Miles.vbs"


def _startup_enabled():
    return _startup_file().exists()


if __name__ == "__main__":
    Miles().start()
