# MILES LITE — made by Arnav, for Shaurya

Your own personal AI assistant, built by **Arnav** especially for you. Just say "Hey Miles".

This copy is Shaurya's alone: the first time it starts it binds to that laptop, and copied to any other PC
it won't run. (Reinstalling Windows counts as a new PC; ask Arnav if that happens.)

---

## Getting started (Shaurya, start here)

**You need:** Windows 10 or 11, internet for the first setup (about 2.5 GB of downloads), about 6 GB of free
disk space, and a microphone (the laptop's built-in one is fine). Python is already on your laptop; setup
installs everything else (Ollama, the AI model, the speech models) by itself.

1. **Download.** Open the repo's **Releases** page (right-hand side of the repo page) and download
   **`Miles-Lite-for-Shaurya.zip`** from the latest release.
2. **Extract.** Right-click the zip → **Extract All…** and choose `C:\` so you end up with `C:\Miles Lite`.
   Keep the path short, and don't run anything from inside the zip.
3. **Set up (once).** Open `C:\Miles Lite` and double-click **`setup.bat`**. If Windows says
   "Windows protected your PC", click **More info → Run anyway**. Wait until it says *Setup complete*
   (10–20 minutes, depending on your internet).
4. **Start.** Double-click **`Start Miles.bat`**. The first start takes about a minute; Miles greets you when
   it's ready. Allow microphone access if Windows asks.
5. **Talk.** Say **"Hey Miles, …"**, press **Ctrl + Alt + M**, or double-tap the glowing buddy. Try
   "who made you?", "open Chrome", "play some lofi on Spotify", "write a short note about my day".

**Handy to know**
- Start with Windows: right-click the tray icon (bottom right) → **Start with Windows**.
- Typing instead of talking: tray icon → **Type a command…**
- Settings (voice, speed, wake word): tray icon → **Settings**, save, then **Restart Miles**.
- On battery you can set `WAKE_WORD_ON_BATTERY = False` in Settings, so Miles only listens after
  Ctrl + Alt + M or a double-tap.
- Something wrong? Tray icon → **Open log**, and send the log to Arnav.

Miles starts with a blank memory and learns about you as you talk. Say "remember that…" to teach it things.

---

This is Miles tuned for a PC like: **Intel Core i5-8250U (4 cores / 8 threads), 8 GB RAM, Intel UHD 620,
no NVIDIA GPU**, touchscreen. Everything still runs offline on the laptop itself; only web research,
weather, news and email use the internet.

## What runs where

| Part | Main PC (RTX 3060) | Miles Lite (laptop CPU) | Why |
|---|---|---|---|
| AI brain | qwen3-vl 8B (sees the screen) | **qwen3:1.7b** | ~1.4 GB, fast enough on 4 cores; good at tools |
| Screen reading | vision model | **Windows OCR** (built in) | reads a window in ~0.3 s, no download, no GPU |
| Speech recognition | distil-large-v3 + base.en (GPU) | **tiny.en** (listening) + **base.en** (commands) | accurate model runs only on what's said to Miles |
| Voice | Kokoro (GPU) | **Piper** (`en_GB-alan-medium`) | ~0.2–0.5 s per sentence on CPU; Kokoro takes 2–4 s |
| Buddy animation | 30 fps | 6 fps idle, 20 fps active | saves CPU and battery |

Measured on the main PC with 4 CPU threads and the GPU switched off, then scaled for the laptop's CPU
(about 2–2.5× slower):

| | Measured (4 threads) | Laptop estimate |
|---|---|---|
| Instant commands (open, close, play, volume, tabs…) | first word 0.15 s | ~0.5 s, plus speech recognition |
| A spoken answer from the AI | first words 2.4–4.7 s | 5–11 s |
| Rewriting selected text ("refine this prompt") | 3–4 s | 7–10 s |
| Drafting an email | 16 s | 35–40 s |
| Start-up (AI reads its instructions) | 19 s | ~45 s (commands wait for it) |
| RAM | Miles 0.6 GB + AI 2.0 GB | ~2.6 GB |

## Choosing a different model

Set `OLLAMA_MODEL` in `config.py`, then run `ollama pull <model>` and restart Miles.

| Model | RAM | Speed on this laptop | Notes |
|---|---|---|---|
| **qwen3:1.7b** (default) | ~2.2 GB | fastest usable | reliable tool use; writing is basic |
| qwen3:4b-instruct | ~3.8 GB | ~2.5× slower (AI answers 10–20 s) | clearly better writing and judgement, but tight in 8 GB |
| qwen3:0.6b | ~1 GB | fastest | too weak for tool use |

What you get and what you lose compared with the main PC:

- **Same:** all instant commands, Spotify, tabs, email with the address box, memory, writing, "refine this
  prompt" in the same field, undo, reminders, protocols, File Explorer selections, long instructions.
- **Slower:** anything that needs the AI to think (see the tables above).
- **Different:** "what's on my screen" reads the *text* on screen (OCR); it can't describe pictures.
  "Click …" works on visible text ("click Sign in"), not on icons without a label.

## Laptop-friendly settings (`config.py`)

| Setting | Default | Meaning |
|---|---|---|
| `WAKE_WORD_ON_BATTERY` | `True` | `False` = on battery, only Ctrl+Alt+M / double-tap wakes Miles (the mic isn't transcribed at all) |
| `OLLAMA_THREADS` | 4 | CPU cores for the AI |
| `OLLAMA_CONTEXT` | 5120 | conversation window; bigger uses more RAM and is slower |
| `OLLAMA_KV_CACHE` | `f16` | `q8_0` saves ~0.3 GB but reads prompts 2× slower on a CPU (measured) |
| `OLLAMA_USE_GPU` | `False` | `True` tries the Intel graphics via Vulkan: experimental, often not faster |
| `WHISPER_MODEL` | `base.en` | `small.en` is more accurate but ~3× slower |
| `TTS_ENGINE` | `piper` | `edge` = the most natural voice (needs internet); `sapi` = built-in Windows voice |
| `PIPER_VOICE` | `en_GB-alan-medium` | or say "change your voice to Ryan / Northern / Jenny / Alba / Amy…" |
| `BUDDY_FPS_IDLE` / `BUDDY_FPS_ACTIVE` | 6 / 20 | animation smoothness vs CPU use |

## Touchscreen and pen

Double-tap the buddy to talk, drag it to move it, and press-and-hold for its menu (the same as a
right-click). Set `BUDDY_SIZE = 1.3` if it's too small to tap comfortably.

Everything else (commands, memory, the ask box, "this / that / here") works exactly as in the main Miles.
See the main Miles README for the full list of commands.
