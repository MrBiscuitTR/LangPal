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
| Longest single turn | 150 s | 15–240; see below |
| Let me interrupt | on | off = microphone muted while the reply plays |
| Send automatically | on | off = the turn is captured but not sent |

Fixed tuning constants live at the top of `web/app.js`: `SPEECH_START_MS`
(140), `BARGE_START_MS` (1000), `PREROLL_MS` (320), `MIN_UTTERANCE_MS` (350),
`NOISE_FLOOR_MULT` (3.2), `BARGE_MULT` (1.6),
`ECHO_MULT` (2.0), `ECHO_GAIN_RISE` (0.10), `ECHO_GAIN_FALL` (0.02),
`ECHO_LAG_S` (0.06), `ECHO_WINDOW_S` (0.06), `BARGE_HOLD_MS` (600),
`BARGE_GRACE_MS` (300).

`PREROLL_CAP_MS` is derived, not set: `max(PREROLL_MS, BARGE_START_MS) + 250`.
The pre-roll ring has to reach back to the moment speech might have started,
which is the longer of the two start windows. Hard-coding it would mean raising
`BARGE_START_MS` silently truncated every interruption.

### How long a turn can be

A turn ends when you stop talking, or when it hits the length cap — whichever
comes first. Hitting the cap sends what you said so far and says so in the
transcript, rather than cutting you off with no explanation.

**The cap is LangPal's, not the model's.** Qwen3-ASR happily transcribes far
longer material — whole songs, if you feed it through llama-swap's own UI. 171 s
below is simply where the measurement stopped, not where quality fell off:
coverage and throughput were still flat there.

| Audio | Transcribed | Coverage |
| --- | --- | --- |
| 120 s | 2.1 s | 27/28 topics |
| 150 s | 2.6 s | 34/35 topics |
| 171 s | 3.1 s | 38/40 topics |

Repeated *identical* audio does degenerate — 178 s of one looping sentence came
back as 29,574 characters with the ending missing — but that is the repetition,
not the length, and real speech does not look like that.

The ceiling is **transport**, and it is self-imposed. LangPal uploads audio as
base64 inside a JSON body, which inflates it by a third; at 32 kB/s for 16 kHz
mono 16-bit, `MAX_REQUEST_BYTES` is reached at about 295 s. The maximum is set
to 240 s to leave room, and `MAX_UTTERANCE_SECONDS` on the server matches.

Base64-in-JSON is a deliberate trade, made so the server never has to parse
multipart — only build it (see [architecture.md](architecture.md)). It costs a
third of the bandwidth on a local network, which is cheap, and this ceiling,
which only matters for material far longer than a spoken turn. Going beyond
240 s means changing the upload format or raising `MAX_REQUEST_BYTES`; neither
is worth it for conversation practice, where turns run to seconds.

Three limits therefore have to agree, in three different files, so
`tests/test_langpal.py::TurnLengthLimits` checks them against each other —
raising the browser's cap alone fails the suite.

### How interruption works

Two problems pull against each other: the assistant must not cut itself off on
its own echo, and you must be able to cut it off whenever you like.

Earlier versions tried to infer the echo level from the microphone and set a
bar above it. That is guesswork, and it fails completely if the browser's echo
canceller is attenuating the microphone during playback — no threshold can
recover a signal that is not there.

So the echo is **predicted from the buffer being played** instead. We know
exactly what we are sending to the speaker, so `playbackLevel()` reads its RMS
at the current playhead (a little behind it, and over a wide window, to allow
for output buffering and the trip through the air). Multiply by `vad.echoGain`, a
coupling factor learned continuously from quiet frames, and that is what the
microphone should be hearing from us alone:

```
max(room_floor x NOISE_FLOOR_MULT x BARGE_MULT,
    predicted_echo x ECHO_MULT) x sensitivity
```

The payoff is the pauses. Between words the playback level goes to nothing, the
second term collapses, and the bar drops to ordinary listening — speech is then
unmistakable. Someone talking over a reply reaches such a gap within a few
hundred milliseconds.

Mic sensitivity scales **both** terms. Scaling only the first left the control
with no effect whatsoever on interrupting, which is exactly when someone would
reach for it.

The room floor (`vad.floor`) is frozen whenever our own audio is audible. It
skips loud frames, but once the echo term raises the bar the echo itself counts
as quiet, so it was being learnt as room noise — within half a second the floor
had climbed to the echo level and `base x BARGE_MULT` swamped everything else.
That was a second, slower route to the same deadlock.

### Deciding you are interrupting

Interrupting is judged on **elapsed time**, not on a run of loud frames.

That distinction is the whole thing. Because the bar only drops in the gaps
between the assistant's own words, speech over a reply is detected in bursts —
heard for a moment, masked for a moment, repeatedly. Demanding an unbroken run
of loud frames on top of that is unsatisfiable by ordinary talking however long
it goes on. In testing, only shouting a single continuous vowel ever managed
it, which is exactly what you would predict: one long note is the only thing
that stays above the bar without interruption.

So the first frame that clears the bar opens a window, later ones extend it,
and it lapses after `BARGE_HOLD_MS` of nothing at all. Interrupting means "you
have been talking for `BARGE_START_MS`", gaps included. Simulated against a
realistic 180 ms-heard / 320 ms-masked burst pattern, the old rule never fires
and this one fires at 1.01 s.

`BARGE_HOLD_MS` has to be longer than the assistant's own words can mask you,
or the window keeps resetting; it must stay shorter than `BARGE_START_MS`, or
evidence outlives the decision it feeds.

When the window completes, the reply **stops** — playback, the queue, and the
synthesis already running. It is not turned down. An earlier version ducked the
volume as an intermediate step; that existed only to make the loudness run
achievable, and it is unnecessary once the window replaces it.

### Seeing what it is doing

Settings has a live readout under **Listening**, active while that panel is
open:

```
level 34.1  |  bar 12.0  |  room 3.1  |  echo 2.4  |  HEARD  |  speaking  |  talking 0.4/1.0s
```

Talk over a reply and watch it. `HEARD` flickering on and off is normal and
expected — that is the burst detection described above, and `talking` should
keep climbing through it. If `talking` keeps resetting to 0.0, `BARGE_HOLD_MS`
is too short for your setup. If `level` never passes `bar` at all, raise Mic
sensitivity. If `level` barely moves while the assistant speaks, the browser's
echo canceller is suppressing your voice — use headphones, or turn off *Let me
interrupt while it is speaking* for half-duplex.

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
