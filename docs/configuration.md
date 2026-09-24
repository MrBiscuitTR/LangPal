# Configuration

## Where each setting lives

There are three places, and which one a setting is in follows one rule: **a
secret or a bind address is server-side; everything a user might want to tweak
mid-conversation is in their browser.**

| Where | What | Survives a restart? |
| --- | --- | --- |
| `.env.local` / environment / CLI flags | Bind address, TLS, password, upstream URLs and API keys, and the *defaults* for everything else | Yes |
| Browser `localStorage` | Each device's overrides: endpoints, models, language, voice, sampling, system prompt | Yes, per device |
| Server RAM | Conversation history, sessions | No — cleared on restart |

LangPal never writes to disk. It reads `.env.local`, `web/` and
`voices_default/` at startup and reads nothing afterwards. Audio, transcripts,
history and uploaded voice samples exist only in memory.

The consequence worth knowing: **conversation history is lost when you restart
the server**, and settings are per-browser, so changing the system prompt on
your laptop does not change it on your phone.

## Server-side settings

All optional. The authoritative defaults are in the `CONFIGURATION` block at
the top of `langpal.py`; `.env.example` is a copy-ready template.

### Network

| Variable | Default | Notes |
| --- | --- | --- |
| `LANGPAL_HOST` | `127.0.0.1` | Bind address. Use your WireGuard IP for peers. |
| `LANGPAL_PORT` | `8080` | |
| `LANGPAL_TLS_CERT` | *(unset)* | PEM certificate. Needed for remote microphones. |
| `LANGPAL_TLS_KEY` | *(unset)* | PEM private key. |
| `LANGPAL_TOKEN` | *(unset)* | Shared password. Unset means no authentication. |

### Upstreams

| Variable | Default |
| --- | --- |
| `LANGPAL_LLM_URL` | `http://127.0.0.1:11435` |
| `LANGPAL_STT_URL` | `http://127.0.0.1:11435` |
| `LANGPAL_TTS_URL` | `http://127.0.0.1:8123` |
| `LANGPAL_LLM_MODEL` | `gemma4-26b-a4b` |
| `LANGPAL_STT_MODEL` | `qwen3-asr` |
| `LANGPAL_TTS_MODEL` | `omnivoice` |
| `LANGPAL_LLM_API_KEY` | *(unset)* |
| `LANGPAL_STT_API_KEY` | *(unset)* |
| `LANGPAL_TTS_API_KEY` | *(unset)* |

API keys are **server-side only**. They are never included in `/api/config`,
cannot be set from the browser, and are never echoed in an error message. That
is deliberate: browser-settable keys would end up in `localStorage`.

### Defaults the browser can override

| Variable | Default |
| --- | --- |
| `LANGPAL_LANGUAGE` | `auto` |
| `LANGPAL_VOICE` | *(unset — the TTS server's own default)* |
| `LANGPAL_HISTORY_MESSAGES` | `20` |
| `LANGPAL_SYSTEM_PROMPT` | the built-in conversation-partner prompt |

### Default voices

| Variable | Default |
| --- | --- |
| `LANGPAL_VOICES_DIR` | `voices_default/` next to `langpal.py` |
| `LANGPAL_REGISTER_DEFAULT_VOICES` | `true` |

## URL normalisation

You can paste endpoint URLs in whatever form you happen to have them. They are
normalised once at startup for the server defaults, and again on every request
for whatever the browser sends.

| You paste | You get |
| --- | --- |
| `10.66.66.2:11435` | `http://10.66.66.2:11435` |
| `localhost:8123/` | `http://localhost:8123` |
| `http://127.0.0.1:8123/v1` | `http://127.0.0.1:8123` |
| `HTTPS://Box:443/v1/audio/speech/` | `https://box` |
| `http://h:11435/upstream/omnivoice/v1` | `http://h:11435/upstream/omnivoice` |

What it does: adds a missing `http://`, lowercases the scheme and host, drops a
default port, strips query strings and fragments, and peels known API suffixes
(`/v1`, `/api`, `/v1/chat/completions`, `/v1/audio/speech`, …) off the end,
repeatedly.

What it deliberately keeps: any other path prefix. That last row is the reason
— llama-swap's `/upstream/<model>` route has to survive, or voice listing
breaks.

Rejected outright: anything that is not `http`/`https`, a URL carrying a
username or password, a malformed or out-of-range port, a URL longer than 512
characters, and anything containing whitespace or control characters (which
would allow smuggling headers into the upstream request).

## Generation settings

Set from the browser, validated on the server.

| Setting | Range | Blank means |
| --- | --- | --- |
| `temperature` | 0–2 | field omitted; your inference server's default applies |
| `top_p` | 0–1 | same |
| `max_tokens` | 1–32768 | same |
| `seed` | 0–2³¹−1 | same (output varies run to run) |
| `history_messages` | 0–200 | falls back to `LANGPAL_HISTORY_MESSAGES` |
| `reasoning_effort` | `""`, `none`, `low`, `medium`, `high` | field omitted |
| `enable_thinking` | unset / true / false | the chat template decides |

"Blank means omitted" is the important bit and it is not the usual behaviour.
LangPal does not substitute a default of its own for a blank field — it leaves
the key out of the JSON entirely, so `llama-server --temp 0.7` keeps applying.

`reasoning_effort` is sent as the OpenAI field of the same name.
`enable_thinking` is sent as `chat_template_kwargs.enable_thinking`, which is
what llama.cpp's `--jinja` templates read. Either way, any `<think>…</think>`
the model emits is stripped before it reaches the screen or the speaker, as is
`delta.reasoning_content`.

## Language

One `language` setting drives both directions.

| Value | Sent to STT | Used for TTS |
| --- | --- | --- |
| `auto` | nothing — the model detects it | whatever it detected, else English |
| `Turkish` | `Turkish` | `Turkish` |

Overrides, if you want them to differ:

| Setting | Blank means |
| --- | --- |
| `stt_language` | follow `language` |
| `tts_language` | follow `language` |

Use an English language **name**. Qwen3-ASR ignores ISO codes: `language=tr`
produced a detected-language of *English* in testing, while `language=Turkish`
correctly forced Turkish.

Auto-detect is unreliable on short clips. The same 2.2-second Turkish sample
came back as *English* on one run and *Arabic* on another. Pin the language.

## Listening (browser-only)

Stored in `localStorage`, never sent to the server.

| Setting | Default | Notes |
| --- | --- | --- |
| Silence that ends a turn | 900 ms | 200–5000 |
| Mic sensitivity | 5 | 1–10; higher lowers the threshold |
| Let me interrupt | on | off = microphone muted while the reply plays |
| Send automatically | on | off = the turn is captured but not sent |

Fixed tuning constants live at the top of `web/app.js`: `SPEECH_START_MS`
(140), `BARGE_START_MS` (320), `PREROLL_MS` (320), `MIN_UTTERANCE_MS` (350),
`MAX_UTTERANCE_MS` (60000), `NOISE_FLOOR_MULT` (3.2), `BARGE_MULT` (2.2).

## Default voices

Every `*.wav` in `voices_default/` is registered as a cloned voice at startup:

```
voices_default/
  tr_ayse.wav
  tr_ayse.txt      <- the exact transcript of tr_ayse.wav
  en_sam.wav       <- no .txt: transcribed by the STT model instead
```

- The file stem becomes the voice name, so it must be letters, digits, `.`,
  `_` or `-`. Anything else is skipped with a warning.
- A matching `.txt` is used as the reference transcript. Without one, the clip
  is sent to the STT model and whatever it returns is used.
- Voices already registered are left alone, so restarting is cheap.
- The folder is **only read**. LangPal does not write the transcript back, and
  does not delete anything.
- It runs on a background thread, so a cold llama-swap does not delay startup.
  Progress appears in the log:

```
voices_default: 2 clip(s) found in C:\Users\you\LangPal\voices_default
  tr_ayse: registered (tr_ayse.txt)
  en_sam: registered (transcribed)
```

Turn it off with `--no-default-voices` or
`LANGPAL_REGISTER_DEFAULT_VOICES=false`.

## Limits

Enforced server-side; a client cannot raise them.

| Limit | Value |
| --- | --- |
| Request body | 12 MiB |
| One spoken turn | 90 s |
| A voice reference clip | 120 s |
| Text sent to TTS | 2000 characters |
| A typed message | 8000 characters |
| System prompt | 8000 characters |
| History kept per session | 200 messages |
| Concurrent sessions | 32 (least recently used is dropped) |
| Upstream timeouts | 600 s (10 s for health checks) |
