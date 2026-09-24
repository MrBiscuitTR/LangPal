# LangPal

Leveraging your PC's high VRAM and GPU speed to learn languages by talking to
AI, as well as messaging them. Near real-time.

You talk, a local ASR model transcribes, a local LLM answers, and a local TTS
model speaks the answer back — in the language you are practising. Nothing
leaves your machine, and nothing is written to its disk.

```
┌──────────────┐   WAV    ┌──────────┐   text    ┌─────┐   sentences   ┌───────────┐
│   browser    │ ───────► │ Qwen3-ASR│ ────────► │ LLM │ ────────────► │ OmniVoice │
│  VAD + mic   │ ◄─────────────────────────────────────────────────────  24 kHz WAV │
└──────────────┘                                                        └───────────┘
```

## Quick start

```bash
git clone https://github.com/<you>/LangPal
cd LangPal
cp .env.example .env.local      # point it at your endpoints
python langpal.py
```

Open <http://localhost:8080>, press **Start listening**, and talk.

No dependencies — Python 3.9+ and its standard library.

## What you need

Three OpenAI-compatible HTTP endpoints you already run. They can be one server:

| Role | Endpoint | Reference model |
| --- | --- | --- |
| Chat | `/v1/chat/completions` | Gemma 4 26B-A4B |
| Speech to text | `/v1/audio/transcriptions` | Qwen3-ASR 1.7B |
| Text to speech | `/v1/audio/speech`, `/v1/audio/voices` | OmniVoice |

A [llama-swap](https://github.com/mostlygeek/llama-swap) config keeping all
three resident is in [docs/hosting.md](docs/hosting.md#model-stack).

## What it does

- **Hands-free turns.** Adaptive silence detection decides where your turn
  ends; a pre-roll buffer means the first word is never clipped.
- **Interruptions.** Talk over the reply and it stops — playback, the queue,
  and the synthesis already running on the GPU.
- **Speaks as it thinks.** The reply is split into sentences server-side and
  synthesised one at a time, so the first sentence plays while the rest is
  still being generated.
- **Voice cloning.** Upload a clip; it is transcribed automatically, you check
  the transcript, and it becomes a voice you can pick. Clips in
  `voices_default/` are registered at startup so there is always a list.
- **One language setting** drives both recognition and speech, with separate
  overrides when you want to hear one language and speak another.
- **Everything configurable** — endpoints, models, temperature, reasoning,
  system prompt, context length. Blank fields are omitted so your inference
  server's own defaults apply.
- **Mobile-first UI** that works on a phone, an iPad and a desktop.

## Using it from another device

Browsers only allow microphone access in a "secure context", so
`http://10.66.66.2:8080` will not work — you need HTTPS, even on a private
tunnel. One `openssl` command and two environment variables get you there:
[docs/hosting.md](docs/hosting.md#the-microphone-problem-and-the-one-command-that-fixes-it).

Set `LANGPAL_TOKEN` if anyone but you can reach the address.

## Documentation

[docs/](docs/) — [usage](docs/usage.md) ·
[configuration](docs/configuration.md) · [hosting](docs/hosting.md) ·
[architecture](docs/architecture.md) · [API](docs/api.md) ·
[upstream APIs](docs/upstream-apis.md) · [security](docs/security.md) ·
[development](docs/development.md)

## Design constraints

These are deliberate and load-bearing:

- **Standard library only.** No `pip install`, ever.
- **Nothing is written to disk.** `web/`, `.env.local` and `voices_default/`
  are read at startup; nothing else is read and nothing is written. Audio,
  history and uploaded samples live in RAM and die with the process.
- **Settings live in your browser**, not a config file on the host. Each device
  keeps its own; the server validates everything on arrival.
- **Secrets stay server-side.** API keys and the access token are never sent to
  the browser and cannot be set from it.

## Tests

```bash
python -m unittest discover -s tests -v          # 72 tests, no network
LANGPAL_LIVE=1 python -m unittest tests.test_live -v   # against real models
```

## Scope

Built for personal use — one person, or a few trusted people over WireGuard.
It is not hardened for the public internet; see
[docs/security.md](docs/security.md) for what is and is not defended.

## Licence

MIT. See [LICENSE](LICENSE).
