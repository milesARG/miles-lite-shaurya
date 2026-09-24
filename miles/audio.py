"""Microphone, voice-activity detection, streaming speech-to-text and the wake word.

How it stays fast on a laptop CPU:
  * Everything the mic hears is transcribed only by the tiny model (tiny.en), just to spot "Miles".
    The accurate model (base.en) runs only on what was actually said to Miles.
  * While you talk, the tiny model re-transcribes the audio about once a second for live captions.
  * The instant you go quiet (~0.1 s), the accurate model starts on the full sentence. If what you
    said is already a complete command it is accepted after ~0.35 s of silence; otherwise after
    END_SILENCE_SEC.
  * Long instructions get longer pauses: the more you've said, and whenever you trail off
    mid-sentence ("...and", "...then"), the longer it waits (up to LONG_PAUSE_SEC).
"""
import collections
import glob
import os
import queue
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import sounddevice as sd

import config
from .util import log

SR = 16000
FRAME = 480  # 30 ms
FRAME_SEC = FRAME / SR
ABORT = "ABORT"

# You're mid-sentence if the transcript ends like this, so keep listening.
_TRAILING = re.compile(r"(\b(and|then|so|but|or|because|to|the|a|an|with|of|for|my|your|that|which|also|like|if|"
                       r"when|after|before|in|on|at|from|about|into|um|uh|plus)|,|-)$", re.I)


def end_silence(voiced_sec: float, text: str) -> float:
    """How much silence ends this utterance: short commands end fast, long instructions may pause."""
    end = config.END_SILENCE_SEC + min(0.5, max(0.0, voiced_sec - 3.0) * 0.07)
    if text and _TRAILING.search(text.strip().rstrip(".…!? ")):
        end = config.LONG_PAUSE_SEC
    return min(end, config.LONG_PAUSE_SEC)


# Whisper likes to invent these on silence/noise.
_HALLUCINATIONS = {"", "you", "thank you", "thanks for watching", "thank you for watching", "bye",
                   "subtitles by the amara org community", "okay", "so", "uh", "um", "hmm", "the", "i"}


_dll_dirs_added = False


def _add_cuda_dll_dirs():
    """pip's nvidia-* wheels put DLLs in site-packages/nvidia/*/bin (CUDA 13: bin/x86_64)."""
    global _dll_dirs_added
    if _dll_dirs_added:
        return
    _dll_dirs_added = True
    for base in sys.path:
        for binp in (glob.glob(os.path.join(base, "nvidia", "*", "bin")) +
                     glob.glob(os.path.join(base, "nvidia", "*", "bin", "x86_64"))):
            try:
                os.add_dll_directory(binp)
            except OSError:
                pass
            os.environ["PATH"] = binp + os.pathsep + os.environ.get("PATH", "")


class Microphone:
    def __init__(self, on_level=None):
        self.q: queue.Queue = queue.Queue()
        self.on_level = on_level
        self.noise = 0.004
        self.muted = False
        self.stream = sd.InputStream(samplerate=SR, channels=1, dtype="float32", blocksize=FRAME,
                                     device=config.MIC_DEVICE, callback=self._callback, latency="low")
        self.stream.start()
        self._calibrate()

    def _callback(self, indata, frames, t, status):
        if not self.muted:
            self.q.put(indata[:, 0].copy())

    def _calibrate(self):
        levels = []
        end = time.time() + 0.6
        while time.time() < end:
            try:
                f = self.q.get(timeout=0.2)
                levels.append(float(np.sqrt(np.mean(f * f))))
            except queue.Empty:
                pass
        if levels:
            self.noise = max(float(np.percentile(levels, 30)), 0.0008)
        log.info("Mic noise floor: %.4f", self.noise)

    def flush(self):
        try:
            while True:
                self.q.get_nowait()
        except queue.Empty:
            pass


class Transcriber:
    def __init__(self):
        _add_cuda_dll_dirs()
        from faster_whisper import WhisperModel
        self.device = None
        want = config.WHISPER_DEVICE
        gpu = config.WHISPER_COMPUTE
        options = [("cuda", gpu), ("cpu", "int8")] if want == "auto" else \
            [(want, gpu if want == "cuda" else "int8")]
        threads = getattr(config, "WHISPER_THREADS", 0)
        last = None
        for dev, ctype in options:
            try:
                t0 = time.time()
                kw = {"cpu_threads": threads} if dev == "cpu" and threads else {}
                self.accurate = WhisperModel(config.WHISPER_MODEL, device=dev, compute_type=ctype, **kw)
                self.fast = WhisperModel(config.LIVE_MODEL, device=dev, compute_type=ctype, **kw)
                noise = (np.random.randn(SR * 2) * 0.01).astype(np.float32)
                for m in (self.fast, self.accurate):            # load CUDA kernels now, not on first command
                    list(m.transcribe(noise, language="en", beam_size=1, vad_filter=False)[0])
                self.device = dev
                log.info("Whisper ready on %s (live %s, final %s) in %.1fs", dev, config.LIVE_MODEL,
                         config.WHISPER_MODEL, time.time() - t0)
                break
            except Exception as e:
                last = e
                log.warning("Whisper on %s failed: %s", dev, e)
        if self.device is None:
            raise RuntimeError(f"Could not load Whisper: {last}")
        self._fast_lock = threading.Lock()
        self._acc_lock = threading.Lock()

    @staticmethod
    def _run(model, audio, beam=1, hotwords=None):
        # vad_filter off: our own VAD already bounds the speech, and Silero tends to clip the first word
        segs, _ = model.transcribe(audio, language="en", beam_size=beam, vad_filter=False,
                                   condition_on_previous_text=False, without_timestamps=True,
                                   hotwords=hotwords)
        return " ".join(s.text.strip() for s in segs if s.no_speech_prob < 0.7).strip()

    def partial(self, audio) -> str:
        """Fast, rough transcript for live captions."""
        with self._fast_lock:
            return _clean(self._run(self.fast, audio, hotwords=config.ASSISTANT_NAME))

    def final(self, audio) -> str:
        """Accurate transcript of a finished sentence."""
        with self._acc_lock:
            return _clean(self._run(self.accurate, audio, beam=config.WHISPER_BEAM,
                                    hotwords=config.WHISPER_HOTWORDS))

    accurate_text = final


class Utterance:
    def __init__(self, text: str, audio, end: float = 0.0):
        self.text, self.audio = text, audio
        self.end = end or time.time()           # when you stopped talking (for latency measurements)


class Listener:
    """Mic + streaming recognition."""

    def __init__(self, mic: Microphone, stt: Transcriber):
        self.mic, self.stt = mic, stt
        self._live_pool = ThreadPoolExecutor(1, thread_name_prefix="stt-live")
        self._final_pool = ThreadPoolExecutor(1, thread_name_prefix="stt-final")
        self._gen = 0

    def _live(self, audio, gen, cb):
        try:
            text = self.stt.partial(audio)
        except Exception as e:
            log.debug("partial failed: %s", e)
            return
        if text and gen == self._gen:
            cb(text)

    def listen(self, start_timeout=None, abort: threading.Event | None = None, on_start=None, on_partial=None,
               is_complete=None, report_level=True, quick=False):
        """Record one utterance and transcribe it.

        Returns an Utterance, None on start timeout, or ABORT.
        on_partial(text) gets live captions; is_complete(text) lets a finished command end early.
        quick=True transcribes with the light live model only (for spotting the wake word in everything the
        mic hears - the accurate model then runs just on what was addressed to Miles).
        """
        self._gen += 1
        gen = self._gen
        try:
            return self._listen(gen, start_timeout, abort, on_start, on_partial, is_complete, report_level, quick)
        finally:
            self._gen += 1                       # late live captions from this utterance are ignored

    def _listen(self, gen, start_timeout, abort, on_start, on_partial, is_complete, report_level, quick=False):
        final = self.stt.partial if quick else self.stt.final
        mic = self.mic
        pre = collections.deque(maxlen=10)       # 300 ms pre-roll so the first syllable isn't lost
        t0 = time.time()
        live_every = config.LIVE_CAPTION_EVERY_SEC * SR
        max_n = config.MAX_UTTERANCE_SEC * SR
        while True:
            frames, started, loud = [], False, 0
            silence, voiced, n, last_voice = 0.0, 0.0, 0, 0
            live_fut, live_n, fin_fut, fin_n = None, 0, None, -1
            while True:
                if abort is not None and abort.is_set():
                    return ABORT
                try:
                    f = mic.q.get(timeout=0.05)
                except queue.Empty:
                    if not started:
                        if start_timeout and time.time() - t0 > start_timeout:
                            return None
                        continue
                    f = None
                if f is not None:
                    rms = float(np.sqrt(np.mean(f * f)))
                    thr = max(mic.noise * config.SPEECH_SENSITIVITY, config.MIN_SPEECH_RMS)
                    show = report_level() if callable(report_level) else report_level
                    if show and mic.on_level:
                        mic.on_level(min(1.0, rms / (thr * 4)))
                    if not started:
                        pre.append(f)
                        if rms > thr:
                            loud += 1
                            if loud >= 3:
                                started = True
                                frames = list(pre)
                                n = last_voice = sum(len(x) for x in frames)
                                if on_start:
                                    on_start()
                        else:
                            loud = 0
                            mic.noise = 0.97 * mic.noise + 0.03 * max(rms, 0.0005)
                            if start_timeout and time.time() - t0 > start_timeout:
                                return None
                        continue
                    frames.append(f)
                    n += len(f)
                    if rms > thr * 0.6:
                        silence = 0.0
                        voiced += FRAME_SEC
                        last_voice = n
                    else:
                        silence += FRAME_SEC

                # live captions while talking
                if (on_partial and silence < 0.2 and n - live_n >= live_every
                        and (live_fut is None or live_fut.done())):
                    live_n = n
                    live_fut = self._live_pool.submit(self._live, np.concatenate(frames), gen, on_partial)

                # the moment you stop talking, start the accurate transcription
                if silence >= config.STOP_DETECT_SEC and voiced >= 0.2 and fin_n < last_voice:
                    if fin_fut is not None and not fin_fut.done():
                        fin_fut.cancel()                    # an older pause's transcript isn't needed any more
                    fin_n = n
                    fin_fut = self._final_pool.submit(final, np.concatenate(frames))

                if fin_fut is not None and fin_n >= last_voice and fin_fut.done() and not fin_fut.cancelled():
                    try:
                        text = fin_fut.result()
                    except Exception as e:
                        log.warning("final transcription failed: %s", e)
                        text = ""
                    if (silence >= end_silence(voiced, text) or
                            (silence >= config.QUICK_END_SEC and (not text or (is_complete and is_complete(text))))):
                        if text and on_partial and gen == self._gen:
                            on_partial(text)
                        return Utterance(text, np.concatenate(frames), time.time() - silence)

                if silence >= config.END_SILENCE_SEC and voiced < 0.2:
                    break                                   # just a click/bump: start over
                if silence >= config.LONG_PAUSE_SEC + 1.0 or n >= max_n:
                    audio = np.concatenate(frames)
                    end = time.time() - silence
                    return Utterance(final(audio), audio, end)
            pre.clear()


_WAKE_RE = None


def _clean(text: str) -> str:
    text = text.strip()
    key = re.sub(r"[^a-z ]", "", text.lower()).strip()
    return "" if key in _HALLUCINATIONS else text


def reset_wake_words():
    global _WAKE_RE
    _WAKE_RE = None


def split_wake(text: str):
    """If `text` addresses Miles, return (True, rest_of_command). Wake word must be near the start."""
    global _WAKE_RE
    if _WAKE_RE is None:
        from .persona import wake_words
        words = "|".join(re.escape(w) for w in wake_words())
        _WAKE_RE = re.compile(rf"\b({words})\b[\s,.!?:;-]*", re.I)
    m = _WAKE_RE.search(text)
    if not m:
        return False, ""
    before = re.findall(r"[a-zA-Z']+", text[:m.start()])
    if len(before) > 2:          # "I drove five miles" -> not a wake word
        return False, ""
    return True, text[m.end():].strip()


# ---- UI sounds ---------------------------------------------------------------
def _tone(freqs, dur=0.09, vol=0.18, sr=44100):
    parts = []
    for f in freqs:
        t = np.linspace(0, dur, int(sr * dur), endpoint=False)
        env = np.minimum(1, np.minimum(t / 0.01, (dur - t) / 0.03))
        parts.append((np.sin(2 * np.pi * f * t) + 0.3 * np.sin(4 * np.pi * f * t)) * env * vol)
    return np.concatenate(parts).astype(np.float32), sr


_SOUNDS = {
    "wake": _tone([880, 1320], 0.06, 0.12),
    "done": _tone([1046, 1568], 0.06, 0.12),
    "error": _tone([440, 330], 0.1),
    "cancel": _tone([660, 440], 0.07, 0.12),
}


def chime(kind="wake"):
    from .player import player
    data, sr = _SOUNDS[kind]
    try:
        player().play_fx(data, sr)
    except Exception:
        pass
