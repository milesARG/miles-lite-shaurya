"""Miles Lite, made by Arnav for Shaurya.

Miles Lite settings - tuned for a laptop without an NVIDIA GPU (tested target: Intel Core i5-8250U,
8 GB RAM, Intel UHD 620). Edit these values, then restart Miles (tray icon -> Restart)."""

# ---- Identity -------------------------------------------------------------
ASSISTANT_NAME = "Miles"
PERSONA = "jarvis"                      # jarvis (British, "sir") or friday ("boss"); switch by voice any time
USER_TITLE = "sir"                      # set by the persona
USER_NAME = "Shaurya"                   # your name, used to sign emails
HOME_CITY = ""                          # for weather; blank = detect from your IP
SEARCH_REGION = "in-en"                 # web research region (DuckDuckGo code)
NEWS_REGION = "IN"                      # headlines region (Google News country code)

# ---- Apps & services -------------------------------------------------------
DEFAULT_MUSIC = "spotify"               # "play <song>" uses: spotify / youtube
EMAIL_SERVICE = "gmail"                 # gmail / outlook / mailto (default mail app)

# ---- Activation -----------------------------------------------------------
WAKE_WORD_ENABLED = True
WAKE_WORD_ON_BATTERY = True             # False = on battery, only the hotkey / double-tap wakes Miles (saves power)
# Spellings Whisper may produce for "Miles". The wake word must be in the first
# few words: "Hey Miles, open Chrome" / "Miles, what time is it".
WAKE_WORDS = ["miles", "myles", "mylz", "miles'", "mile's", "niles", "smiles"]
HOTKEY = "<ctrl>+<alt>+m"               # press once, then speak (also stops Miles talking)
COMMAND_PORT = 47292                    # local port for `python miles_cmd.py "command"` (localhost only)

# ---- Brain (Ollama, on the CPU) ----------------------------------------------
# Miles Lite runs its own lean Ollama instance (your normal Ollama on 11434 is untouched).
OLLAMA_URL = "http://127.0.0.1:11436"   # 127.0.0.1, not localhost (avoids a 2 s IPv6 delay on Windows)
OLLAMA_PROMPT_CACHE_MB = 0              # llama.cpp's RAM prompt cache (up to 8 GB!) - off on an 8 GB laptop
OLLAMA_KV_CACHE = "f16"                 # conversation memory precision. "q8_0" halves its RAM (~0.3 GB) but reads
                                        # prompts 2x slower on a CPU (measured), so f16 it is
OLLAMA_MODEL = "qwen3:1.7b"             # fast on a 4-core laptop. Smarter but ~2.5x slower: "qwen3:4b-instruct"
FALLBACK_MODEL = "qwen3:4b-instruct"    # used if OLLAMA_MODEL isn't downloaded
VISION_MODEL = ""                       # no vision model: the screen is read with Windows OCR instead
GROUNDING = "norm1000"
OLLAMA_CONTEXT = 5120                   # conversation window (tokens): ~0.6 GB of RAM. Bigger = more RAM, slower
OLLAMA_THREADS = 4                      # = physical CPU cores (the i5-8250U has 4)
OLLAMA_USE_GPU = False                  # True tries the Intel GPU via Vulkan (experimental; often not faster)
OLLAMA_KEEP_ALIVE = "30m"               # keep the model in RAM this long after the last request
MAX_TOOL_ROUNDS = 12                    # tool steps per request (each step costs a few seconds on a laptop CPU)
HISTORY_RESET_MINUTES = 10              # start a fresh conversation after this idle time (memory stays)

# ---- Memory ------------------------------------------------------------------
MEMORY_FACTS_IN_PROMPT = 25             # newest facts shown to the AI each conversation (all are searchable)
MEMORY_RECENT_IN_PROMPT = 0             # past exchanges shown up front. 0 on small models: they copy old replies
                                        # (even wrong ones). "What did I ask earlier?" still searches the history

# ---- Speech to text (faster-whisper on the CPU) --------------------------------
LIVE_MODEL = "tiny.en"                  # captions while you talk + spotting "Miles" (very light)
WHISPER_MODEL = "base.en"               # final text of your command. More accurate, ~3x slower: "small.en"
WHISPER_BEAM = 1
WHISPER_COMPUTE = "int8"                # CPU precision
WHISPER_DEVICE = "cpu"
WHISPER_THREADS = 4
# Words Whisper should expect (improves recognition of names it often mishears)
WHISPER_HOTWORDS = "Miles, Jarvis, Friday, YouTube, Spotify, WhatsApp, Gmail"
MIC_DEVICE = None                       # None = Windows default mic, or a device index

# Voice-activity detection & end-of-speech timing
SPEECH_SENSITIVITY = 3.0                # speech must be this many times louder than background noise
MIN_SPEECH_RMS = 0.006                  # absolute loudness floor for speech
LIVE_CAPTION_EVERY_SEC = 0.8            # how often live captions refresh while you talk (CPU-friendly)
STOP_DETECT_SEC = 0.12                  # silence after which transcription starts (you've probably stopped)
QUICK_END_SEC = 0.35                    # silence that ends a sentence when it's already a complete command
END_SILENCE_SEC = 0.7                   # silence that ends a short command (raise if you get cut off)
LONG_PAUSE_SEC = 1.5                    # longest thinking pause allowed inside a long instruction
MAX_UTTERANCE_SEC = 60                  # longest single instruction you can speak
COMMAND_START_TIMEOUT = 7               # seconds to start talking after the chime
FOLLOW_UP_SEC = 5                       # after a reply, keep listening this long without the wake word (0 = off)

# ---- Voice ----------------------------------------------------------------
TTS_ENGINE = "piper"                    # piper = fast offline neural voice on CPU; edge = online (most natural,
                                        # needs internet); sapi = built-in Windows voice
PIPER_VOICE = "en_GB-alan-medium"       # JARVIS. Others: en_GB-northern_english_male-medium, en_US-ryan-high
PIPER_VOICE_FRIDAY = "en_GB-jenny_dioco-medium"
PIPER_SPEED = 1.15                      # speaking rate ("talk faster/slower" changes it and remembers)
KOKORO_VOICE = "bm_george"              # (unused in Lite: Kokoro is too slow without a GPU)
KOKORO_SPEED = 1.2
KOKORO_GPU = False
# edge-tts (used when TTS_ENGINE = "edge", or as a fallback)
MAX_SPOKEN_SENTENCES = 3                # longer answers continue on the HUD (unless you ask for detail)
VOICE = "en-GB-RyanNeural"
VOICE_RATE = "+12%"
VOICE_PITCH = "-4Hz"

# ---- Safety ---------------------------------------------------------------
CONFIRM_DANGEROUS = True                # ask "are you sure?" before risky actions
AUTO_APPROVE_READ_ONLY = True           # read-only PowerShell commands run without asking
COMMAND_TIMEOUT_SEC = 60                # max run time for PowerShell commands

# ---- Proactive assistant --------------------------------------------------
PROACTIVE_ALERTS = True                 # warn about high CPU/RAM/disk, low battery, Defender off
ALERT_COOLDOWN_MIN = 30
RAM_ALERT_PERCENT = 96                  # 8 GB fills up easily; only warn when it's really full

# ---- Resources ----------------------------------------------------------------
LOW_RAM = True                          # hand start-up-only memory back to Windows

# ---- Buddy (on-screen character) -------------------------------------------
BUDDY_ENABLED = True
BUDDY_SIZE = 1.0                        # scale of the buddy (bigger is easier to tap on a touchscreen)
BUDDY_IDLE_OPACITY = 0.85
BUBBLE_HIDE_AFTER_SEC = 7
BUDDY_FPS_ACTIVE = 20                   # animation frames per second while talking/working
BUDDY_FPS_IDLE = 6                      # ...and while idle (the animation costs CPU and battery)
