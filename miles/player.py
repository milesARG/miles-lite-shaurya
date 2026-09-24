"""One always-open, low-latency audio output (WASAPI). Voice and UI sounds are mixed into it,
so playback starts in a few milliseconds instead of reopening the device for every sentence."""
import threading

import numpy as np
import sounddevice as sd

from .util import log


class Player:
    def __init__(self):
        self._lock = threading.Lock()
        self.voice = None
        self.vpos = 0
        self.fx: list[list] = []
        self._open()

    def _open(self):
        self.stream = None
        attempts = []
        try:
            api = next(a for a in sd.query_hostapis() if "WASAPI" in a["name"])
            dev = api["default_output_device"]
            attempts.append((dev, int(sd.query_devices(dev)["default_samplerate"]),
                             sd.WasapiSettings(auto_convert=True)))
        except Exception:
            pass
        attempts.append((None, 24000, None))
        for dev, sr, extra in attempts:
            try:
                s = sd.OutputStream(device=dev, samplerate=sr, channels=1, dtype="float32", latency="low",
                                    extra_settings=extra, callback=self._cb)
                s.start()
                self.stream, self.sr = s, sr
                log.info("Audio out: %s @ %d Hz, latency %.0f ms", sd.query_devices(s.device)["name"], sr,
                         s.latency * 1000)
                return
            except Exception as e:
                log.warning("audio output %s failed: %s", dev, e)

    def _cb(self, out, frames, t, status):
        buf = np.zeros(frames, np.float32)
        with self._lock:
            if self.voice is not None:
                chunk = self.voice[self.vpos:self.vpos + frames]
                buf[:len(chunk)] += chunk
                self.vpos += len(chunk)
                if self.vpos >= len(self.voice):
                    self.voice = None
            for item in self.fx[:]:
                data, pos = item
                chunk = data[pos:pos + frames]
                buf[:len(chunk)] += chunk
                item[1] = pos + len(chunk)
                if item[1] >= len(data):
                    self.fx.remove(item)
        out[:, 0] = np.clip(buf, -1.0, 1.0)

    def _resample(self, data, sr):
        """Band-limited (FFT) resampling: the 24 kHz voice on a 48 kHz device stays clean. Straight-line
        interpolation, used before, adds a faint metallic hiss above 12 kHz."""
        data = np.asarray(data, dtype=np.float32)
        if sr == self.sr or len(data) < 16:
            return data
        pad = 512                                     # silence on both ends keeps the FFT's wrap-around out
        x = np.concatenate([np.zeros(pad, np.float32), data, np.zeros(pad, np.float32)])
        m = int(round(len(x) * self.sr / sr))
        spec = np.fft.rfft(x)
        out = np.zeros(m // 2 + 1, dtype=complex)
        k = min(len(spec), len(out))
        out[:k] = spec[:k]
        y = np.fft.irfft(out, m) * (m / len(x))
        p = int(round(pad * self.sr / sr))
        return y[p:m - p].astype(np.float32)

    def _ensure(self):
        if self.stream is None or not self.stream.active:
            self._open()

    def play_voice(self, data, sr):
        self._ensure()
        d = self._resample(data, sr)
        with self._lock:
            self.voice, self.vpos = d, 0

    def voice_active(self):
        return self.voice is not None

    def stop_voice(self):
        with self._lock:
            self.voice = None

    def play_fx(self, data, sr):
        self._ensure()
        d = self._resample(data, sr)
        with self._lock:
            self.fx.append([d, 0])


_player = None
_plock = threading.Lock()


def player() -> Player:
    global _player
    with _plock:
        if _player is None:
            _player = Player()
        return _player
