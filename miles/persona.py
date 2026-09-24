"""Personas: J.A.R.V.I.S. (default) and F.R.I.D.A.Y. - voice, how you're addressed, style, extra wake word.

Switch by voice: "Miles, switch to FRIDAY" / "switch to JARVIS". The choice is remembered.
"""
import json

import config
from .util import DATA

SETTINGS = DATA / "settings.json"

PERSONAS = {
    "jarvis": {
        "label": "J.A.R.V.I.S.", "title": "sir", "voice": config.PIPER_VOICE, "wake": "jarvis",
        "style": ("Your personality is J.A.R.V.I.S. from Iron Man: an impeccably polite British AI, calm, precise "
                  "and unflappable, with dry, understated wit used sparingly. You sound natural and never repeat "
                  "stock phrases."),
        "hello": "JARVIS protocols engaged. At your service, sir.",
    },
    "friday": {
        "label": "F.R.I.D.A.Y.", "title": "boss", "voice": config.PIPER_VOICE_FRIDAY, "wake": "friday",
        "style": ("Your personality is F.R.I.D.A.Y. from the Avengers: warm, quick, confident and a little "
                  "cheeky; casual and efficient. You sound natural and never repeat stock phrases."),
        "hello": "FRIDAY here. What do you need, boss?",
    },
}


def current() -> str:
    return config.PERSONA if config.PERSONA in PERSONAS else "jarvis"


def get() -> dict:
    return PERSONAS[current()]


def wake_words():
    return list(config.WAKE_WORDS) + [get()["wake"]]


def _settings() -> dict:
    try:
        return json.loads(SETTINGS.read_text(encoding="utf-8")) if SETTINGS.exists() else {}
    except Exception:
        return {}


def _save(**kv):
    try:
        s = _settings()
        s.update(kv)
        SETTINGS.write_text(json.dumps(s, indent=1), encoding="utf-8")
    except Exception:
        pass


def apply(name: str, save: bool = True) -> str:
    name = name.lower().strip(" .").replace(".", "")
    if name not in PERSONAS:
        return f"Unknown persona '{name}'. Options: JARVIS, FRIDAY."
    config.PERSONA = name
    p = PERSONAS[name]
    config.USER_TITLE = p["title"]
    config.PIPER_VOICE = _settings().get(f"voice_{name}", p["voice"])      # a voice you picked for this persona
    from . import audio
    audio.reset_wake_words()
    if save:
        _save(persona=name)
    return p["hello"]


# Piper voices by short name ("change your voice to Ryan"). Downloaded on first use (~60 MB each).
VOICES = {"alan": "en_GB-alan-medium", "northern": "en_GB-northern_english_male-medium",
          "alba": "en_GB-alba-medium", "jenny": "en_GB-jenny_dioco-medium", "cori": "en_GB-cori-high",
          "ryan": "en_US-ryan-high", "joe": "en_US-joe-medium", "lessac": "en_US-lessac-high",
          "amy": "en_US-amy-medium", "kristin": "en_US-kristin-medium"}


def voice_setting(action: str, speed=None, voice: str = "") -> str:
    t = config.USER_TITLE
    if action in ("faster", "slower", "speed"):
        new = config.PIPER_SPEED + (0.1 if action == "faster" else -0.1) if action != "speed" else float(speed or 1.15)
        config.PIPER_SPEED = round(min(1.6, max(0.8, new)), 2)
        _save(voice_speed=config.PIPER_SPEED)
        return f"Speaking at {config.PIPER_SPEED:g}x now, {t}."
    if action == "voice":
        v = voice.strip().lower()
        match = VOICES.get(v.split()[0] if v else "") or next((x for x in VOICES.values() if v and v in x.lower()), None)
        if not match:
            return f"I don't have a voice called '{voice}'. Options: {', '.join(n.title() for n in VOICES)}."
        config.PIPER_VOICE = match
        _save(**{f"voice_{current()}": match})
        return f"Voice changed to {match.split('-')[1].replace('_', ' ').title()}, {t}. How do I sound?"
    if action == "reset":
        config.PIPER_VOICE, config.PIPER_SPEED = get()["voice"], 1.15
        s = _settings()
        s.pop(f"voice_{current()}", None)
        s.pop("voice_speed", None)
        try:
            SETTINGS.write_text(json.dumps(s, indent=1), encoding="utf-8")
        except Exception:
            pass
        return f"Back to my usual voice, {t}."
    return "Unknown voice setting."


def load():
    s = _settings()
    apply(s.get("persona", config.PERSONA), save=False)
    if "voice_speed" in s:
        config.PIPER_SPEED = float(s["voice_speed"])
