# Architecture

## Shape

```
  browser                        langpal.py                    your models
 ──────────                     ────────────                  ─────────────
  AudioWorklet
      │ 128-frame blocks
      ▼
  VAD + ring buffer
      │ one utterance
      ▼
  WAV, 16 kHz mono
      │ POST /api/stt ─────────► transcribe() ──────────────►  Qwen3-ASR
      │                           strip "language X<asr_text>"
      │ ◄── {text, language} ───────────┘
      ▼
  POST /api/chat  ────────────► stream_chat() ──────────────►  LLM (SSE)
      │                           strip <think>…</think>
      │                           split into sentences
      │ ◄── SSE: delta / sentence / done
      ▼
  per sentence:
  POST /api/tts   ────────────► synthesize() ───────────────►  OmniVoice
      │ ◄── audio/wav ────────────────┘
      ▼
  decodeAudioData → play in order
```

## Why it is split this way

**Sentence chunking on the server.** The LLM stream is parsed in `langpal.py`,
not the browser, because the same splitter then guarantees TTS never receives a
half-sentence. Splitting client-side would have meant duplicating the
abbreviation and decimal-point rules in JavaScript.

**One HTTP request per sentence, not one long stream.** Synthesis runs at
roughly 0.27× real time on the reference stack, so the next sentence is always
ready before the current one finishes playing. A single streaming audio
response would have been marginally faster to first sound and much harder to
cancel cleanly — with discrete requests, stopping is just `AbortController`
plus revoking the turn.

**Synthesis starts when a sentence arrives, not when it is due to play.** This
is the difference between a seamless reply and an audible stall. An earlier
version prefetched the next sentence only if it was already queued when the
current one began playing — which it never is, because the model is still
generating it. Every join then cost a full round trip. Measured on the
reference stack: gaps of 0.96 s and 0.91 s became 0.00 s. `pumpTts()` keeps up
to `MAX_TTS_INFLIGHT` (2) sentences at the endpoint, and decoding happens on
the fetch path so the buffer is ready the instant the previous sentence ends.

**Two `AudioContext`s.** Capture is pinned to 16 kHz so no resampling is needed
in the common case; playback runs at the device's native rate so the 24 kHz
reply is not degraded on its way out. If the browser refuses the 16 kHz hint,
`resample()` handles it at the end of the utterance.

**Settings in the browser.** The server holds defaults and secrets; each device
keeps its own overrides and sends them per request. This keeps the host's disk
untouched and lets two people use one server with different prompts and
languages. Everything arriving from a browser goes through
`validate_settings()` before it is used.

## A turn, step by step

### 1. Capture

`web/capture-worklet.js` runs on the real-time audio thread and does the
minimum: copy the 128-frame block, compute its RMS, post both to the main
thread. Anything heavier there causes audible glitches.

### 2. Deciding where a turn is

`onFrame()` in `web/app.js` runs a small state machine:

- **idle** — the noise floor tracks the room with an exponential moving average,
  updated *only on quiet frames* so a long sentence cannot drag the floor up
  and deafen the detector mid-conversation. Recent audio goes into a ring
  buffer capped at `PREROLL_CAP_MS` -- long enough to reach back past the
  longest start window, so a confirmed interruption still has its opening
  words.
- A frame is "loud" when `rms > max(floor × 3.2, 0.012) × sensitivity`. While
  the assistant is speaking that threshold is multiplied by `BARGE_MULT` so the
  reply leaking through the speaker cannot open a turn by itself.
- **speech** — entered after `SPEECH_START_MS` of sustained loud frames,
  seeded with the pre-roll so the first word survives. Ends after the
  configured silence, or at `MAX_UTTERANCE_MS`.
- Turns shorter than `MIN_UTTERANCE_MS` are discarded as coughs and clicks.

Entering **speech** while the assistant is talking calls `stopEverything()` —
that is barge-in.

### 3. Transcription

The utterance is resampled if needed, encoded as 16-bit mono WAV in
`encodeWav()`, base64'd and posted to `/api/stt`.

The server rebuilds it as `multipart/form-data` (which is what the OpenAI
transcription API wants) and posts it upstream. Base64-in-JSON for the browser
hop is a deliberate trade: it costs ~33% in transfer for a local-network
request, and buys not having to write a multipart *parser* in the server — only
a builder, which is far easier to audit.

`parse_asr_text()` then strips Qwen3-ASR's language-identification prefix. See
[upstream-apis.md](upstream-apis.md#qwen3-asr) for the exact format.

### 4. The reply

`/api/chat` opens a Server-Sent Events response. `EventSource` cannot POST and
the turn needs a body, so the browser parses SSE by hand in `readSse()`.

Server-side, `stream_chat()` yields only `delta.content`.
`delta.reasoning_content` is dropped, and `strip_think()` removes
`<think>…</think>` spans — carrying its state across chunks, because a tag can
straddle a token boundary.

`split_sentences()` then pulls complete sentences off the buffer:

- a terminator counts only when followed by whitespace or end-of-buffer, so
  `3.5` and `example.com` stay intact
- a terminator at the very end of the buffer is not a boundary until the stream
  finishes — more punctuation or a digit may still arrive
- `.` after a known abbreviation (`Dr.`, `etc.`) does not split
- fragments below `MIN_SENTENCE_CHARS` are held back, so TTS never gets `Ok.`
  on its own
- CJK uses a lower minimum (5), because `今日はいい天気ですね。` is a complete
  sentence at 11 characters and the Latin threshold would hold it forever
- text with no punctuation at all is force-cut at the last space before
  `MAX_SENTENCE_CHARS`

A unit test feeds the same text three characters at a time and asserts the
streamed result matches the one-shot result.

Every 15 seconds without visible output, an SSE comment goes out as a
heartbeat, so a slow first token does not trip an idle-socket timeout.

### 5. Speech

`ttsWorker()` plays sentences strictly in order while fetching the next one in
parallel, so the gap is hidden by the previous sentence's playback. Exactly one
sentence is prefetched — enough to stay ahead, few enough that at most one
synthesis is wasted when you press Stop.

## Cancellation

Two mechanisms, because one is not enough.

**Client side** — `stopEverything()` drops the queue, stops the playing
`AudioBufferSourceNode`, and aborts every in-flight `fetch`.

**Server side** — the browser mints a random id per reply and sends it with
every request for that reply. `/api/stop` revokes it, and `/api/tts` checks it
*before* starting synthesis and again after. A request already queued upstream
when you pressed Stop is therefore dropped without occupying the GPU.

An id the server has never seen is deliberately **not** revoked. This began as
a counter incremented on `/api/stop` and echoed back for the client to adopt,
which fails closed: a page reload reset the browser's copy to zero while the
session cookie kept the server's, and from then on every turn was rejected as
stale until the server restarted. A revocation list only ever rejects work it
was explicitly told about, so nothing can silently wedge.

Aborting the `fetch` alone would not do this: the server would happily finish
synthesising into a closed socket, holding a `--parallel 1` backend hostage for
the next turn.

## Sessions

A session is created on first contact and identified by a 256-bit
server-generated id in an `HttpOnly`, `SameSite=Strict` cookie. Clients cannot
choose their own id — an unrecognised one gets a fresh session rather than
being trusted.

Each holds the conversation history, a lock, and its revoked turn ids (the
most recent `CANCELLED_TURNS`). The store is capped
at `MAX_SESSIONS` with least-recently-used eviction. Everything is in RAM and
dies with the process.

## Files

| File | Role |
| --- | --- |
| `langpal.py` | The whole server. Configuration block, validation, upstream clients, HTTP handler. |
| `web/index.html` | Markup. No inline script or style — the CSP forbids both. |
| `web/style.css` | Mobile-first layout, one media query for tablets and desktops. |
| `web/app.js` | Capture, VAD, WAV encoding, the turn pipeline, playback, panels. |
| `web/capture-worklet.js` | Audio-thread processor. Deliberately tiny. |
| `voices_default/` | Clips registered as voices at startup. Read-only. |
| `tests/test_langpal.py` | 72 unit tests, no network. |
| `tests/test_live.py` | End-to-end tests against real models; opt-in. |

`web/` is read into RAM once at startup and served from a fixed route table, so
path traversal is not expressible. `--dev` re-reads per request instead.
