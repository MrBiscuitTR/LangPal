# LangPal documentation

LangPal is a small voice-chat front end for a model stack you already run
yourself. You talk, a local ASR model transcribes, a local LLM answers, and a
local TTS model speaks the answer back — in the language you are practising.

Nothing leaves your machine, and nothing is written to its disk.

## Start here

| Document | What it covers |
| --- | --- |
| [usage.md](usage.md) | Using the web UI: talking, interrupting, registering voices |
| [configuration.md](configuration.md) | Every setting, where it lives, what it does |
| [hosting.md](hosting.md) | Running it, the HTTPS requirement, WireGuard access |
| [architecture.md](architecture.md) | How a turn flows through the system |
| [api.md](api.md) | LangPal's own HTTP API |
| [upstream-apis.md](upstream-apis.md) | The llama-swap, Qwen3-ASR and OmniVoice APIs it calls |
| [security.md](security.md) | The threat model and what is deliberately not defended |
| [development.md](development.md) | Layout, tests, how to change things |

## The short version

```bash
python langpal.py --host 127.0.0.1 --port 8080
```

Open `http://localhost:8080`, press **Start listening**, and talk.

For anything other than `localhost` you need HTTPS, because browsers refuse
microphone access outside a secure context. [hosting.md](hosting.md) explains
the one command that gets you there.

## Requirements

- Python 3.9 or newer. No third-party packages — standard library only.
- Three OpenAI-compatible HTTP endpoints you control, for chat completions,
  audio transcription and speech synthesis. They can all be the same server.
- A browser with `AudioWorklet`: Chrome 66+, Firefox 76+, Safari 14.1+.
