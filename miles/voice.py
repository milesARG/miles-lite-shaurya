"""Text-to-speech, streamed sentence by sentence.

Miles Lite's default engine is Piper, a local neural voice made for CPUs (~0.1-0.5 s
per sentence on a laptop, no internet, no GPU). edge-tts (online Microsoft voices) and
the built-in Windows voice are fallbacks. Sentences are synthesised ahead while earlier ones
play, short phrases are cached, and live loudness drives the buddy's animation.
"""
import asyncio
import hashlib
import io
import logging
import queue
import re
import threading
import time

import numpy as np
import soundfile as sf

import config
from .player import player
from .util import DATA, log

CACHE_DIR = DATA / "tts_cache"
CACHE_DIR.mkdir(exist_ok=True)

_EMOJI = re.compile("[\U0001F000-\U0001FFFF☀-➿️]")

# How a person would read symbols, units and abbreviations aloud (espeak otherwise says "rupee five hundred",
# "three dot five", "kay em slash aitch"...).
_UNITS = {"km/h": "kilometres per hour", "kmph": "kilometres per hour", "mph": "miles per hour",
          "km": "kilometres", "kg": "kilograms", "GB": "gigabytes", "MB": "megabytes", "KB": "kilobytes",
          "TB": "terabytes", "GHz": "gigahertz", "MHz": "megahertz", "ms": "milliseconds", "fps": "frames per second"}
_CURRENCY = {"₹": "rupees", "$": "dollars", "€": "euros", "£": "pounds"}
_SAY = [
    (re.compile(r"\b([ap])\.m\.(?=\s+[a-z])", re.I), lambda m: m.group(1).upper() + " M"),
    (re.compile(r"\b([ap])\.m\.", re.I), lambda m: m.group(1).upper() + " M."),
    (re.compile(r"\be\.g\.,?", re.I), "for example,"), (re.compile(r"\bi\.e\.,?", re.I), "that is,"),
    (re.compile(r"\betc\.", re.I), "et cetera."), (re.compile(r"\bvs\.?(?=\s)", re.I), "versus"),
    (re.compile(r"\bfeat\.", re.I), "featuring"), (re.compile(r"\bapprox\.", re.I), "approximately"),
    (re.compile(r"\bw/(?=\s)"), "with"), (re.compile(r"\bOK\b"), "okay"),
]


def _time(m):
    h, mins, ampm = int(m.group(1)), int(m.group(2)), (m.group(3) or "").upper().replace(".", "")
    words = f"{h} o'clock" if mins == 0 and not ampm else f"{h}" if mins == 0 else \
        f"{h} oh {mins}" if mins < 10 else f"{h} {mins}"
    return words + (f" {' '.join(ampm)}" if ampm else "")


def normalize_speech(t: str) -> str:
    t = re.sub(r"\b(\d{1,2}):(\d{2})(?:\s*([ap]\.?m(?:\.(?=\s+[a-z]))?))?(?![\d:])", _time, t, flags=re.I)  # 10:45 PM
    for rx, rep in _SAY:
        t = rx.sub(rep, t)
    t = re.sub(r"(?<=\d),(?=\d{3}\b)", "", t)                                            # 1,234 -> 1234
    t = re.sub(r"([₹$€£])\s?(\d+(?:\.\d+)?)(\s?(?:k|m|million|billion|lakh|crore)\b)?",
               lambda m: f"{m.group(2)}{m.group(3) or ''} {_CURRENCY[m.group(1)]}", t, flags=re.I)
    t = re.sub(r"(\d)\s?%", r"\1 percent", t)
    t = re.sub(r"°\s?F\b", " degrees Fahrenheit", t)
    t = re.sub(r"°\s?C\b|°", " degrees", t)
    t = re.sub(r"(\d)\s?(km/h|kmph|mph|km|kg|GB|MB|KB|TB|GHz|MHz|ms|fps)\b",
               lambda m: f"{m.group(1)} {_UNITS[m.group(2)]}", t)
    t = re.sub(r"(\d)\.(\d)", r"\1 point \2", t)                                         # 3.5 -> 3 point 5
    t = re.sub(r"\s*&\s*", " and ", t)
    t = re.sub(r"\s\+\s", " plus ", t)
    t = re.sub(r"\s=\s", " equals ", t)
    t = re.sub(r"(?<=\w)@(?=\w)", " at ", t)
    t = re.sub(r"(?:[A-Za-z]:)?\\?(?:[^\\\s]+\\)+([^\\\s]+)", r"\1", t)                   # C:\x\y\file -> file
    t = re.sub(r"(?<=[A-Za-z])/(?=[A-Za-z])", " or ", t)                                 # and/or
    t = re.sub(r"\s*\(([^)]{1,80})\)", r", \1,", t)                                      # (aside) -> , aside,
    t = re.sub(r"\s*[—–]\s*|\s+-\s+", ", ", t)                                           # dashes: a short pause
    t = re.sub(r"\.{3,}|…", ",", t)
    t = re.sub(r",\s*([,.!?])", r"\1", t)
    t = re.sub(r"^\s*,\s*", "", t)
    return t


def speakable(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    text = _EMOJI.sub("", text)
    text = re.sub(r"https?://\S+", "the link", text)
    text = re.sub(r"[*_#`>|]+", "", text)
    text = re.sub(r"^\s*[-•]\s*", "", text, flags=re.M)
    return normalize_speech(" ".join(text.split())).strip()


# Pause before the next chunk, by how the previous one ended - Kokoro leaves only ~0.1 s between separately
# spoken sentences, which sounds rushed.
_PAUSE = {"?": 0.30, ".": 0.24, "!": 0.24, ",": 0.10, ";": 0.14, ":": 0.14}


def pause_after(chunk: str) -> float:
    return _PAUSE.get(chunk.rstrip("\"'”’) ")[-1:] if chunk.strip() else "", 0.16)


class SentenceSplitter:
    """Feed streamed text chunks, get complete sentences out."""
    _END = re.compile(r"[.!?]+[\"')\]]*\s+")
    _ABBR = re.compile(r"\b(e\.g|i\.e|a\.m|p\.m|mr|mrs|ms|dr|vs|etc|st|no|approx|feat)\.$", re.I)

    def __init__(self):
        self.buf = ""

    def feed(self, chunk: str):
        self.buf += chunk
        out, start = [], 0
        for m in self._END.finditer(self.buf):
            head = self.buf[:m.start() + 1]
            if self._ABBR.search(head) or re.search(r"\b\d\.$", head):
                continue                              # "e.g. " / "Mr. " / "1. " aren't sentence ends
            out.append(self.buf[start:m.end()].strip())
            start = m.end()
        self.buf = self.buf[start:]
        if len(self.buf) > 160:                      # very long clause: split at a comma
            cut = self.buf.rfind(", ", 0, 160)
            if cut > 40:
                out.append(self.buf[:cut + 1].strip())
                self.buf = self.buf[cut + 2:]
        return out

    def flush(self):
        rest, self.buf = self.buf.strip(), ""
        return [rest] if rest else []


class _Piper:
    """Local neural voice for CPUs (Piper): ~0.1-0.5 s per sentence on a laptop, ~60 MB per voice, no GPU."""

    def __init__(self):
        import onnxruntime as ort
        from piper import PiperConfig, PiperVoice
        ort.set_default_logger_severity(3)
        self._ort, self._PiperConfig, self._PiperVoice = ort, PiperConfig, PiperVoice
        self._voices = {}
        self.device = "cpu"
        self._lock = threading.Lock()
        self._get(config.PIPER_VOICE)

    def _get(self, name):
        if name not in self._voices:
            import json
            path = DATA / "piper" / f"{name}.onnx"
            if not path.exists():
                _download_piper_voice(name)
            so = self._ort.SessionOptions()
            so.intra_op_num_threads = getattr(config, "PIPER_THREADS", 2)   # leave cores for speech recognition
            so.inter_op_num_threads = 1
            so.enable_cpu_mem_arena = False
            cfg = self._PiperConfig.from_dict(json.loads(path.with_suffix(".onnx.json").read_text(encoding="utf-8")))
            self._voices[name] = self._PiperVoice(config=cfg, session=self._ort.InferenceSession(
                str(path), so, providers=["CPUExecutionProvider"]))
        return self._voices[name]

    def synth(self, text):
        from piper import SynthesisConfig
        voice = self._get(config.PIPER_VOICE)
        with self._lock:                                  # one at a time: a laptop has only 4 cores
            chunks = list(voice.synthesize(text, SynthesisConfig(length_scale=1 / max(0.5, config.PIPER_SPEED))))
        if not chunks:
            return np.zeros(1, np.float32), 22050
        return np.concatenate([c.audio_float_array for c in chunks]).astype(np.float32), chunks[0].sample_rate


def _download_piper_voice(name: str):
    """Fetch a Piper voice (.onnx + .json, ~60 MB) from the official voice collection."""
    import requests
    lang, speaker, quality = name.split("-")
    base = (f"https://huggingface.co/rhasspy/piper-voices/resolve/main/{lang.split('_')[0]}/{lang}/{speaker}/"
            f"{quality}/{name}")
    folder = DATA / "piper"
    folder.mkdir(parents=True, exist_ok=True)
    for suffix in (".onnx.json", ".onnx"):
        log.info("downloading voice %s%s", name, suffix)
        r = requests.get(base + suffix, timeout=120)
        r.raise_for_status()
        (folder / f"{name}{suffix}").write_bytes(r.content)


class _Kokoro:
    """Local neural TTS. One espeak instance is kept alive (creating it per call costs ~130 ms)."""

    def __init__(self):
        from .audio import _add_cuda_dll_dirs
        _add_cuda_dll_dirs()
        import onnxruntime as ort
        ort.set_default_logger_severity(3)
        from kokoro_onnx import Kokoro
        from phonemizer.backend import EspeakBackend
        model, voices = DATA / "kokoro" / "kokoro-v1.0.onnx", DATA / "kokoro" / "voices-v1.0.bin"
        providers = ["CPUExecutionProvider"]
        if config.KOKORO_GPU:          # grow GPU memory only as needed (the default arena doubles in big steps)
            providers.insert(0, ("CUDAExecutionProvider", {"cudnn_conv_algo_search": "HEURISTIC",
                                                           "arena_extend_strategy": "kSameAsRequested"}))
        so = ort.SessionOptions()
        so.enable_cpu_mem_arena = False
        try:
            sess = ort.InferenceSession(str(model), so, providers=providers)
        except Exception as e:
            log.warning("Kokoro GPU session failed (%s); using CPU", e)
            sess = ort.InferenceSession(str(model), providers=["CPUExecutionProvider"])
        self.k = Kokoro.from_session(sess, str(voices))       # also points phonemizer at bundled espeak
        self.device = "gpu" if "CUDAExecutionProvider" in sess.get_providers() else "cpu"
        self._Espeak = EspeakBackend
        self._espeak = {}                          # one espeak per accent, created once
        self.vocab = self.k.tokenizer.vocab
        self._lock = threading.Lock()

    def _backend(self):
        lang = "en-gb" if config.KOKORO_VOICE.startswith("b") else "en-us"
        if lang not in self._espeak:
            quiet = logging.getLogger("miles.espeak")
            quiet.setLevel(logging.ERROR)
            self._espeak[lang] = self._Espeak(lang, preserve_punctuation=True, with_stress=True, logger=quiet)
        return self._espeak[lang]

    def _voice(self):
        """A voice name, or a blend like "bm_george:0.7,bm_fable:0.3"."""
        spec = config.KOKORO_VOICE
        if ":" not in spec and "," not in spec:
            return spec
        if getattr(self, "_blend", (None,))[0] != spec:
            parts = []
            for p in spec.split(","):
                name, _, w = p.strip().partition(":")
                if name:
                    parts.append((name.strip(), float(w) if w else 1.0))
            total = sum(w for _, w in parts) or 1.0
            style = sum(self.k.get_voice_style(n) * (w / total) for n, w in parts)
            self._blend = (spec, style.astype(np.float32))
        return self._blend[1]

    def synth(self, text):
        with self._lock:
            ph = self._backend().phonemize([text], strip=True)[0]
        ph = "".join(p for p in ph if p in self.vocab).strip()
        audio, sr = self.k.create(ph, voice=self._voice(), speed=config.KOKORO_SPEED, is_phonemes=True)
        return np.asarray(audio, dtype=np.float32), sr


class Speaker:
    def __init__(self):
        self._loop = asyncio.new_event_loop()
        threading.Thread(target=self._loop.run_forever, daemon=True, name="tts-loop").start()
        self._stop = threading.Event()
        self._play_lock = threading.Lock()
        self.level = 0.0
        self.speaking = False
        self.on_sentence = None           # callback(sentence) when a sentence starts playing
        self._mem: dict[str, tuple] = {}
        self._offline = False
        self._local = None                # the offline voice engine (Piper on a laptop, Kokoro on a GPU)
        self._local_ready = threading.Event()
        if config.TTS_ENGINE not in ("kokoro", "piper"):
            self._local_ready.set()

    def load(self):
        """Load the local voice (call from a background thread at startup)."""
        player()                                          # open the audio output now, not on first reply
        if config.TTS_ENGINE in ("kokoro", "piper"):
            try:
                t0 = time.time()
                self._local = _Piper() if config.TTS_ENGINE == "piper" else _Kokoro()
                for w in ("Warm up.", "Warming up the voice, sir."):
                    self._local.synth(w)
                log.info("%s voice ready on %s in %.1fs", config.TTS_ENGINE.capitalize(), self._local.device,
                         time.time() - t0)
            except Exception as e:
                log.warning("%s voice unavailable (%s); using edge-tts", config.TTS_ENGINE, e)
            self._local_ready.set()

    # ---- synthesis ------------------------------------------------------------
    async def _edge(self, text):
        import edge_tts
        comm = edge_tts.Communicate(text, config.VOICE, rate=config.VOICE_RATE, pitch=config.VOICE_PITCH)
        buf = bytearray()
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                buf.extend(chunk["data"])
        return bytes(buf)

    def synth(self, text: str):
        self._local_ready.wait(30)
        key = hashlib.sha1(f"{config.TTS_ENGINE}|{config.PIPER_VOICE}|{config.PIPER_SPEED}|{config.VOICE}|"
                           f"{config.VOICE_RATE}|{config.VOICE_PITCH}|{text}".encode()).hexdigest()
        if key in self._mem:
            return self._mem[key]
        if self._local is not None:
            try:
                res = self._local.synth(text)
                if len(text) <= 80:
                    if len(self._mem) > 300:
                        self._mem.pop(next(iter(self._mem)))
                    self._mem[key] = res
                return res
            except Exception as e:
                log.warning("local voice failed (%s); falling back to edge-tts", e)
        path = CACHE_DIR / f"{key}.mp3"
        mp3 = path.read_bytes() if path.exists() else None
        if mp3 is None and not self._offline:
            try:
                mp3 = asyncio.run_coroutine_threadsafe(self._edge(text), self._loop).result(timeout=12)
                if len(text) <= 80:
                    path.write_bytes(mp3)
            except Exception as e:
                log.warning("edge-tts failed (%s); using offline voice", e)
                self._offline = True
                threading.Timer(120, lambda: setattr(self, "_offline", False)).start()
        if mp3 is None:
            return self._sapi(text)
        data, sr = sf.read(io.BytesIO(mp3), dtype="float32")
        if data.ndim > 1:
            data = data.mean(axis=1)
        res = (data, sr)
        if len(text) <= 80:
            self._mem[key] = res
        return res

    def _sapi(self, text):
        import comtypes.client
        try:
            import comtypes
            comtypes.CoInitialize()
        except Exception:
            pass
        path = str(CACHE_DIR / "_sapi.wav")
        voice = comtypes.client.CreateObject("SAPI.SpVoice")
        stream = comtypes.client.CreateObject("SAPI.SpFileStream")
        stream.Open(path, 3)
        voice.AudioOutputStream = stream
        voice.Speak(text)
        stream.Close()
        data, sr = sf.read(path, dtype="float32")
        return (data if data.ndim == 1 else data.mean(axis=1)), sr

    def prewarm(self, phrases):
        for p in phrases:
            try:
                self.synth(p)
            except Exception:
                pass

    # ---- playback -------------------------------------------------------------
    def _play(self, data, sr):
        p = player()
        p.play_voice(data, sr)
        t0 = time.time()
        win = int(sr * 0.05)
        while p.voice_active():
            if self._stop.is_set():
                p.stop_voice()
                break
            i = int((time.time() - t0) * sr)
            seg = data[i:i + win]
            self.level = float(min(1.0, np.sqrt(np.mean(seg * seg)) * 6)) if len(seg) else 0.0
            time.sleep(0.015)
        self.level = 0.0

    def session(self):
        return SpeechSession(self)

    def say(self, text: str):
        s = self.session()
        sp = SentenceSplitter()
        for sentence in sp.feed(text + " ") + sp.flush():    # sentence by sentence: the first one starts sooner
            s.feed(sentence)
        s.close()
        s.wait()

    def stop(self):
        self._stop.set()
        player().stop_voice()

    @property
    def stopped(self):
        return self._stop.is_set()


class SpeechSession:
    """Sentences fed here are synthesised ahead and spoken in order."""

    def __init__(self, sp: Speaker):
        self.sp = sp
        self.q: queue.Queue = queue.Queue()
        self.done = threading.Event()
        self.spoken: list[str] = []
        self._first = True
        threading.Thread(target=self._run, daemon=True, name="speech").start()

    def feed(self, sentence: str):
        sentence = speakable(sentence)
        if not sentence:
            return
        if self._first and len(sentence) > 70:       # start talking sooner: the opening phrase goes on its own
            cut = sentence.find(", ", 18, 70)
            if cut > 0:
                self._first = False
                self._enqueue(sentence[:cut + 1])
                sentence = sentence[cut + 2:]
        self._first = False
        self._enqueue(sentence)

    def _enqueue(self, sentence: str):
        holder = {}
        ev = threading.Event()

        def work():
            try:
                holder["audio"] = self.sp.synth(sentence)
            except Exception as e:
                log.error("TTS error: %s", e)
            ev.set()
        threading.Thread(target=work, daemon=True).start()   # synthesise ahead of playback
        self.q.put((sentence, holder, ev))

    def close(self):
        self.q.put(None)

    def wait(self, timeout=None):
        self.done.wait(timeout)

    def _run(self):
        with self.sp._play_lock:
            self.sp._stop.clear()
            self.sp.speaking = True
            try:
                prev = None
                while True:
                    item = self.q.get()
                    if item is None or self.sp._stop.is_set():
                        break
                    sentence, holder, ev = item
                    ev.wait(15)
                    if "audio" not in holder or self.sp._stop.is_set():
                        continue
                    data, sr = holder["audio"]
                    if prev is not None:                  # a natural breath between sentences
                        data = np.concatenate([np.zeros(int(sr * pause_after(prev)), np.float32), data])
                    prev = sentence
                    if self.sp.on_sentence:
                        self.sp.on_sentence(sentence)
                    self.spoken.append(sentence)
                    self.sp._play(data, sr)
            finally:
                self.sp.speaking = False
                self.sp.level = 0.0
                self.done.set()
