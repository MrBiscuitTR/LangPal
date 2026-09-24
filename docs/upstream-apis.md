# Upstream APIs

What LangPal expects from the three services it calls, and the quirks it works
around. Everything here was verified against a live stack
(llama.cpp build `b10760`, omnivoice.cpp, llama-swap) rather than taken from
documentation.

## Summary

| Service | Endpoint | Method | Body |
| --- | --- | --- | --- |
| LLM | `/v1/chat/completions` | POST | JSON, `stream: true` |
| STT | `/v1/audio/transcriptions` | POST | `multipart/form-data` |
| TTS | `/v1/audio/speech` | POST | JSON → `audio/wav` |
| TTS | `/v1/audio/voices` | GET | — |
| TTS | `/v1/audio/voices` | POST | JSON |
| TTS | `/v1/audio/voices/<name>` | DELETE | — |
| TTS | `/health` | GET | — |

Paths are constants at the top of `langpal.py` (`LLM_CHAT_PATH`,
`STT_TRANSCRIBE_PATH`, `TTS_SPEECH_PATH`, `TTS_VOICES_PATH`, `HEALTH_PATH`).
Change them there if your server puts the routes elsewhere.

---

## llama-swap

[llama-swap](https://github.com/mostlygeek/llama-swap) fronts several backends
on one port and loads them on demand, choosing which by the **`model` field in
the request body**.

### Routing requests that have no model in the body

llama-swap reads the `model` field from the request body to decide which
backend to use. `POST /v1/audio/speech` carries one, so it routes fine. The
voice-registry calls are the problem:

```console
$ curl http://10.66.66.2:11435/v1/audio/voices
{"src":"llama-swap","error":{"message":"no model id could be identified",
 "type":"invalid_request_error","code":"not_found"}}
```

**A `?model=` query parameter routes it.** This is the fix LangPal uses, and it
also triggers the model load:

```console
$ curl "http://10.66.66.2:11435/v1/audio/voices?model=omnivoice"
{"voices":[{"name":"en_nature"}]}

$ curl "http://10.66.66.2:11435/v1/audio/voices?model=omnovoice"   # typo
{"src":"llama-swap","error":{"message":"no router for requested model", …}}
```

`_tts_routed_url()` appends it to every bodyless TTS request, and
`register_voice()` puts a `model` key in its JSON body for the same reason.
A server that is not llama-swap ignores both — verified by sending them
straight to omnivoice.cpp, which answered normally.

### What works on which URL

| Request | main port | main port + `?model=` | `/upstream/<model>` | the container |
| --- | :-: | :-: | :-: | :-: |
| `POST /v1/audio/speech` | yes¹ | yes | yes | yes |
| `GET /v1/audio/voices` | no | **yes** | yes | yes |
| `POST /v1/audio/voices` | no | yes² | yes | yes |
| `DELETE /v1/audio/voices/<name>` | **no** | **no** | yes | yes |
| `GET /health` | yes³ | yes | yes | yes |

¹ the body already carries `model`.
² LangPal adds `model` to the register body.
³ llama-swap answers with its *own* health, not the backend's.

### The one that cannot be fixed with a query parameter

`DELETE /v1/audio/voices/<name>` fails on the main port no matter how it is
routed, because llama-swap has no route pattern for that sub-path at all. It
returns Go's plain-text 404 rather than its usual JSON error, which is the
tell:

```console
$ curl -X DELETE "http://10.66.66.2:11435/v1/audio/voices/tr_ayse?model=omnivoice"
404 page not found

$ curl -X DELETE "http://10.66.66.2:11435/upstream/omnivoice/v1/audio/voices/tr_ayse"
{"status":"deleted"}
```

`delete_voice()` detects the 404 and says exactly this, naming the URL to
switch to, rather than passing a bare 404 up to the user.

### Recommended TTS URL

```
LANGPAL_TTS_URL=http://10.66.66.2:11435/upstream/omnivoice
```

Everything works on that form, including removing voices, and llama-swap still
loads the model on demand. The plain main port works for everything except
deletion; the container's own port works for everything but never triggers a
load.

This is why `normalize_base_url()` strips only known API suffixes instead of
discarding the path — `http://h:11435/upstream/omnivoice/v1` has to normalise
to `http://h:11435/upstream/omnivoice`, not `http://h:11435`.

### Keeping models resident

With `swap: true` (the default) every turn evicts the previous model, and a
voice conversation stalls for tens of seconds at each hand-off. Put all three
in a group with `swap: false`; see [hosting.md](hosting.md#model-stack).

---

## LLM — `/v1/chat/completions`

Standard OpenAI streaming chat completions. LangPal sends:

```json
{
  "model": "gemma4-26b-a4b",
  "messages": [{"role": "system", "content": "…"}, {"role": "user", "content": "…"}],
  "stream": true
}
```

plus `temperature`, `top_p`, `max_tokens`, `seed`, `reasoning_effort` and
`chat_template_kwargs` **only when they are set**. A blank field is omitted
entirely so the inference server's own configuration applies.

Only `choices[].delta.content` is consumed. `delta.reasoning_content` — which
llama.cpp emits when `reasoning_format` splits thinking out — is dropped, and
`<think>…</think>` spans in the content are stripped as a second line of
defence.

Responses stream as `data: {...}` lines terminated by `data: [DONE]`. Lines are
read with a 256 KiB cap so a malformed upstream cannot force an unbounded
allocation.

---

## Qwen3-ASR — `/v1/audio/transcriptions`

Multipart, with `file` as a 16 kHz mono WAV plus `model`, `response_format`,
and optionally `language`.

### It prepends a language tag

Qwen3-ASR emits a language-identification token before the transcript, and
llama.cpp passes it through verbatim
([ggml-org/llama.cpp#26749](https://github.com/ggml-org/llama.cpp/issues/26749)):

```console
$ curl -s http://10.66.66.2:11435/v1/audio/transcriptions \
    -F model=qwen3-asr -F file=@t.wav
{"type":"transcript.text.done",
 "text":"language Turkish<asr_text>Merhaba, bu bir testtir.",
 "usage":{"type":"tokens","input_tokens":54,"output_tokens":13,"total_tokens":67}}
```

Two things follow. A client that just displays `text` shows the tag to the
user — so `parse_asr_text()` splits on `<asr_text>`. And the tag is *useful*:
it is a free language detection, which LangPal feeds to TTS so the reply is
spoken in the language you were speaking. Both the tagged and the already-clean
forms are handled, so a server that strips it is fine too.

Note the response shape is `{"type": "transcript.text.done", "text": …}` rather
than OpenAI's plain `{"text": …}`. LangPal reads `text` from either.

### Language must be a name, not a code

```console
$ curl … -F language=Turkish   # → "language Turkish<asr_text>Merhaba. Bu bir testir."
$ curl … -F language=tr        # → "language English<asr_text>Merhaba, bu bir testir."
```

`tr` is silently ignored and the model falls back to auto-detect. Use
`Turkish`, `French`, `Japanese`.

### Auto-detect is unreliable on short clips

The same 2.2-second Turkish clip was labelled **English** on one run and
**Arabic** on another, the second time transcribing it into Arabic script.
Forcing the language fixes it. This is why LangPal recommends pinning a
language and why `auto` falls back to English rather than trusting a junk
result.

### Input format

Send 16-bit PCM mono WAV. The browser records WebM/Opus by default, which this
endpoint does not accept, so `web/app.js` captures raw PCM through an
`AudioWorklet` and builds the WAV itself.

---

## OmniVoice — `/v1/audio/speech` and `/v1/audio/voices`

An [omnivoice.cpp](https://github.com/mudler-agent/omnivoice.cpp)-compatible
server: zero-shot TTS with voice cloning, 24 kHz mono output.

### Synthesis

```console
$ curl -s http://127.0.0.1:8123/v1/audio/speech \
    -H 'Content-Type: application/json' \
    -d '{"model":"omnivoice","input":"Merhaba, bu bir testtir.",
         "language":"Turkish","response_format":"wav","voice":"en_nature"}' \
    -o out.wav
```

| Field | Notes |
| --- | --- |
| `input` | The text. |
| `language` | English language name, same convention as the ASR model. |
| `voice` | A registered or built-in name. Omit for the server's default. |
| `response_format` | `wav` returns a complete file; `pcm` streams s16le. |
| `seed` | Optional, for reproducible output. |

LangPal uses `wav`. `pcm` does stream properly — chunked, with
`X-Accel-Buffering: no` — but per-sentence requests already keep playback
continuous, and discrete requests are far easier to cancel. Output is 24 kHz,
mono, 16-bit.

Measured throughput: 0.78 s to synthesise 2.67 s of audio, and 0.43 s for
1.62 s — about 0.27× real time.

### Listing voices

```console
$ curl http://127.0.0.1:8123/v1/audio/voices
{"voices":[{"name":"en_nature"}]}
```

Built-in and registered voices come back together. LangPal also tolerates a
plain list of strings.

### Registering a cloned voice

```json
POST /v1/audio/voices
{ "name": "tr_ayse", "ref_text": "Merhaba, bu bir testtir.", "wav_b64": "<base64 WAV>" }
```

```json
{"name":"tr_ayse","status":"registered"}
```

`ref_text` must be an accurate transcript of the clip — clone quality depends
on it, which is why LangPal transcribes automatically but locks the field until
you can check it.

Some builds also accept `rvq_b64` / `spk_b64` for pre-encoded latents. LangPal
only uses the WAV path.

> **Name collisions shadow built-ins.** Registering a name that matches a
> built-in speaker makes that speaker unreachable for the lifetime of the TTS
> process, because voice resolution consults the registry first. LangPal
> refuses an existing name unless `replace` is set.

### Deleting

```console
$ curl -X DELETE http://127.0.0.1:8123/v1/audio/voices/tr_ayse
{"status":"deleted"}
```

The name is percent-encoded, and validated against
`^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$` before it ever reaches a URL.

### Health

```console
$ curl http://127.0.0.1:8123/health
{"status":"ok"}
```

Used by `/api/health` and by llama-swap's `checkEndpoint`.

---

## How LangPal calls all of them

- **No redirects.** Upstream URLs are user-supplied, so a redirect could bounce
  a request somewhere unexpected while still carrying the `Authorization`
  header. `_NoRedirects` refuses them outright.
- **Bounded reads.** Responses are capped at 12 MiB and SSE lines at 256 KiB.
- **Truncated errors.** Upstream error bodies are relayed to the client cut to
  400 characters, with any query string stripped from the URL first.
- **Generous timeouts.** 600 s, because a cold llama-swap has to load a model
  from disk before answering. Health checks use 10 s.
