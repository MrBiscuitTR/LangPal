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

Behind llama-swap, prefer the per-model route for TTS:
`http://10.66.66.2:11435/upstream/omnivoice`. The plain main port works for
everything except removing a voice — see
[upstream-apis.md](upstream-apis.md#what-works-on-which-url).
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

Use an English language **name** — `English`, `French`, `Japanese` — not the
language's own name for itself and not an ISO code. Qwen3-ASR ignores codes:
`language=tr` produced a detected-language of *English* in testing, while
`language=Turkish` correctly forced Turkish.

Capitalisation is normalised server-side, so `french`, `FRENCH` and `French`
all become `French` before anything is sent upstream; `brazilian portuguese`
becomes `Brazilian Portuguese`. The models key off the usual written form, so
this is not cosmetic. The UI offers the common languages as autocomplete
suggestions, but the field is free text — anything the models support works.

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
(140), `BARGE_START_MS` (1000), `PREROLL_MS` (320), `MIN_UTTERANCE_MS` (350),
`MAX_UTTERANCE_MS` (60000), `NOISE_FLOOR_MULT` (3.2), `BARGE_MULT` (1.6),
`ECHO_MULT` (2.0), `ECHO_RISE` (0.30), `ECHO_FALL` (0.02), `BARGE_GRACE_MS`
(300).

`PREROLL_CAP_MS` is derived, not set: `max(PREROLL_MS, BARGE_START_MS) + 250`.
The pre-roll ring has to reach back to the moment speech might have started,
which is the longer of the two start windows. Hard-coding it would mean raising
`BARGE_START_MS` silently truncated every interruption.

### How the assistant avoids interrupting itself

The room's noise floor only adapts on quiet frames, so it freezes as soon as
playback starts. On its own that leaves the bar where a silent room put it,
which the assistant's own voice through a speaker clears easily -- the reply
then cuts itself off a word in.

So a second **echo floor** tracks how loud our own output comes back through
the microphone, as an asymmetric envelope follower: it rises quickly
(`ECHO_RISE`) so a sentence is tracked within a few frames, and falls slowly
(`ECHO_FALL`) so it does not collapse in the pauses between words.

It deliberately does *not* hold the peak. An earlier version did, and since
speech peaks run three to four times its average, the bar landed at roughly
five times the average echo — which nobody talking through a speaker can
clear, so barge-in simply never fired.

It also only learns from frames that are *below* the current threshold, the
same rule the room floor uses. Learning from loud frames means a frame carrying
the user's voice raises the floor, which raises the bar above them: the harder
they talk, the further out of reach it gets.

The bar while sound is audible is whichever is higher:

```
max(room_floor x NOISE_FLOOR_MULT x BARGE_MULT,
    echo_floor x ECHO_MULT) x sensitivity
```

Mic sensitivity scales **both** terms. Scaling only the first left the control
with no effect whatsoever on interrupting, which is exactly when someone would
reach for it. If barge-in is reluctant on your setup, raise it.

Note "audible", not "replying": a sentence still being synthesised makes no
sound, so it contributes no echo and uses the ordinary threshold. Cancelling it
still takes the full `BARGE_START_MS`, because abandoning a reply is just as
disruptive whether or not it has started coming out of the speaker.

`BARGE_MULT` is deliberately small: echo rejection is `ECHO_MULT`'s job, and
stacking both would make a noisy room deaf to real interruptions.

For the first `BARGE_GRACE_MS` of each sentence nothing can trigger a barge-in,
because the echo floor has not caught up with the new audio yet.

Interrupting then needs `BARGE_START_MS` of sustained speech on top of that --
a full second, against 140 ms for starting an ordinary turn. Cutting the
assistant off by accident is far more irritating than taking a moment to be
sure, so the two are tuned very differently. The assistant keeps talking
through that second and stops the instant it is up.

`tests/test_langpal.py::BargeInThresholds` parses these constants out of
`app.js` and checks, across five speaker and headphone scenarios, that the
assistant never clears its own bar and that a person always does.

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
