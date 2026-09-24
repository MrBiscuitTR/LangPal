# LangPal HTTP API

The API the browser talks to. It is not a public API and has no versioning
guarantees — it exists so `web/app.js` can be read alongside it, and so you can
drive LangPal from a script.

All request and response bodies are JSON, except `/api/tts`, which returns
`audio/wav`.

## Conventions

**Authentication.** If `LANGPAL_TOKEN` is set, every route except `/api/login`
requires either the `lp_auth` cookie or `Authorization: Bearer <token>`.
Without it, everything is open.

**Sessions.** The first response sets `lp_sid` (`HttpOnly`, `SameSite=Strict`,
`Secure` when TLS is on). Send it back to keep your conversation history. An
unrecognised id silently gets a fresh session — ids cannot be chosen by the
client.

**Settings.** Most routes accept a `settings` object holding any subset of the
fields in [configuration.md](configuration.md). Every field is validated and
clamped server-side; unknown keys are ignored so an old browser tab keeps
working. Omit it entirely to use the server defaults.

**Turn ids.** `/api/chat` and `/api/tts` accept `turn`, a
`[A-Za-z0-9_-]{8,64}` id the browser mints per reply. `/api/stop` revokes one;
work carrying a revoked id is refused with `409`. Omit it and the work simply
cannot be cancelled.

An id the server has never seen is **not** revoked. That direction is the whole
design: this replaced a counter both sides had to agree on, and a page reload
or one lost response desynchronised it permanently, rejecting every turn
thereafter until the server was restarted.

**Errors.** `{"error": "..."}` with:

| Status | Meaning |
| --- | --- |
| 400 | Invalid input. The message says which field. |
| 401 | Password required or wrong. |
| 404 | No such route. |
| 409 | Stopped — this turn has been revoked. |
| 413 | Body over 12 MiB. The connection closes. |
| 502 | An upstream failed. The message includes a truncated excerpt. |
| 500 | A bug. Details go to the server log, not to you. |

Every response carries a strict `Content-Security-Policy`, `nosniff`,
`no-referrer` and `same-origin` opener policy.

---

## `GET /api/config`

Server defaults, choices and limits, so the UI can seed empty storage.

```json
{
  "defaults": { "llm_url": "http://10.66.66.2:11435", "temperature": null, "...": "..." },
  "languages": ["Turkish", "English", "..."],
  "reasoning_efforts": ["", "none", "low", "medium", "high"],
  "auth_required": false,
  "limits": { "max_utterance_seconds": 90, "max_request_bytes": 12582912, "...": "..." }
}
```

Upstream API keys and the access token are not in `defaults` and never leave
the process. A unit test asserts this.

---

## `POST /api/login`

```json
{ "token": "your-password" }
```

`200` sets the `lp_auth` cookie for 30 days. `401` on a wrong password, after a
0.5 s delay. Compared with `hmac.compare_digest`. When no password is
configured it returns `{"ok": true, "auth_required": false}`.

---

## `POST /api/health`

Probes all three upstreams with the supplied settings.

```json
{ "settings": { "llm_url": "10.66.66.2:11435" } }
```

```json
{ "checks": {
    "llm": { "ok": true, "ms": 4 },
    "stt": { "ok": true, "ms": 3 },
    "tts": { "ok": false, "error": "cannot reach http://127.0.0.1:8123/health (…)" } } }
```

Always `200`; look at each `ok`. 10 s timeout per endpoint.

---

## `POST /api/stt`

```json
{
  "audio_b64": "<base64 of a WAV file>",
  "purpose": "voice_sample",
  "settings": { "language": "Turkish" }
}
```

The WAV is validated in memory before anything is sent upstream: RIFF/WAVE
magic, a parseable header, non-zero duration, and within the limit.

`purpose: "voice_sample"` raises the limit to 120 s and forces language
auto-detect, because a reference clip should be transcribed as it is rather
than coerced into the conversation language. Anything else is a spoken turn:
90 s, and the conversation language is forced.

```json
{
  "text": "Merhaba, bu bir testtir.",
  "language": "Turkish",
  "raw": "language Turkish<asr_text>Merhaba, bu bir testtir."
}
```

`text` is always clean. `language` is what Qwen3-ASR reported, or `""`. `raw`
is the untouched upstream string, for debugging.

---

## `POST /api/chat`

```json
{ "text": "Merhaba, nasılsın?", "turn": "9f3c…", "settings": { "temperature": 0.7 } }
```

Returns `text/event-stream`. The turn is appended to the session history once
it completes.

| Event | Data | Meaning |
| --- | --- | --- |
| `delta` | `{"t": "..."}` | Visible text, thinking already removed |
| `sentence` | `{"i": 0, "text": "..."}` | A complete sentence, ready for `/api/tts` |
| `done` | `{"text": "...", "sentences": 2}` | Finished; `text` is the whole reply |
| `error` | `{"message": "..."}` | Something failed mid-stream |

Lines beginning `:` are heartbeats sent every 15 s of silence; ignore them.

Because the response starts before the upstream is contacted, upstream failures
arrive as an `error` **event**, not an HTTP status. Check for it.

```
event: delta
data: {"t": "Merhaba!"}

event: sentence
data: {"i": 0, "text": "Merhaba! İyiyim, teşekkür ederim, sen nasılsın?"}

event: done
data: {"text": "Merhaba! İyiyim, …", "sentences": 2}
```

---

## `POST /api/tts`

```json
{
  "text": "Merhaba, nasılsın?",
  "detected_language": "Turkish",
  "turn": "9f3c…",
  "settings": { "voice": "tr_ayse" }
}
```

Returns `audio/wav` (24 kHz mono from OmniVoice). Markdown is stripped and
whitespace collapsed before synthesis; a request whose text has nothing
speakable in it is a `400`.

`detected_language` is only consulted when the conversation language is `auto`.
Resolution order: `tts_language` override → pinned `language` →
`detected_language` → `English`.

The turn is checked before synthesis starts and again after it returns, so
pressing Stop discards queued work instead of paying for it.

---

## `POST /api/stop`

```json
{ "turn": "9f3c…" }
```

Revokes that turn, so anything still queued for it is dropped. Returns
`{"ok": true}`. The client does not need to track anything.

## `POST /api/reset`

Clears the session's history, and revokes `turn` if one is given.
`{"ok": true}`.

---

## Voices

### `POST /api/voices`

```json
{ "settings": {} }
```

```json
{ "voices": ["en_nature", "tr_ayse"] }
```

Names that do not match `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$` are filtered out of
whatever the upstream returns.

> A `POST` for a read: the body carries `settings`, since the TTS URL can be
> overridden per device.

### `POST /api/voices/register`

```json
{
  "name": "tr_ayse",
  "ref_text": "Merhaba, bu bir testtir.",
  "audio_b64": "<base64 WAV>",
  "replace": false,
  "settings": {}
}
```

`ref_text` is required — transcribe with `/api/stt` first, or supply your own.
The clip is validated as a WAV within 120 s.

Unless `replace` is true, an existing name is a `400`. Registering over a
built-in speaker shadows it for the lifetime of the TTS process, so it should
be deliberate.

### `POST /api/voices/delete`

```json
{ "name": "tr_ayse", "settings": {} }
```

The name is validated against the same pattern and percent-encoded before it
goes into the upstream URL path.

---

## Static routes

`GET /`, `/index.html`, `/app.js`, `/style.css`, `/capture-worklet.js`.

Served from a fixed route table loaded into RAM at startup, all with
`Cache-Control: no-store`. There is no filesystem lookup, so path traversal has
nothing to traverse.

---

## Driving it from a script

```python
import base64, json, urllib.request

BASE = "http://127.0.0.1:8080"
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())

def post(path, payload):
    request = urllib.request.Request(
        BASE + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    return opener.open(request, timeout=600).read()

with open("utterance.wav", "rb") as fh:
    heard = json.loads(post("/api/stt", {
        "audio_b64": base64.b64encode(fh.read()).decode(),
        "settings": {"language": "Turkish"}}))
print(heard["text"])

audio = post("/api/tts", {"text": heard["text"], "settings": {"language": "Turkish"}})
assert audio[:4] == b"RIFF"
```

`tests/test_live.py` is a fuller worked example, including reading the SSE
stream.
