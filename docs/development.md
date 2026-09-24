# Development

## Layout

```
langpal.py              the entire server
web/
  index.html            markup; no inline script or style (CSP)
  style.css             mobile-first, one breakpoint at 700px
  app.js                capture, VAD, turn pipeline, playback, panels
  capture-worklet.js    audio-thread processor
voices_default/         clips registered at startup (read-only)
tests/
  test_langpal.py       72 unit tests, no network
  test_live.py          end-to-end against real models, opt-in
docs/
.env.example            copy to .env.local
```

No build step, no bundler, no dependencies. Edit and reload.

## Running

```bash
python langpal.py --dev
```

`--dev` re-reads `web/` on every request, so a browser refresh picks up edits.
Without it, assets are read into RAM once at startup.

## Tests

```bash
python -m unittest discover -s tests -v
```

72 tests, no network, well under a second. They cover URL normalisation, the
Qwen3-ASR prefix parser, sentence chunking, thinking-tag stripping, settings
validation, language resolution, chat payload assembly, history trimming, WAV
validation, the multipart builder, the voice-folder scanner, session eviction,
and that no secret leaks into `/api/config`.

The one worth knowing about:

```python
def test_streaming_matches_one_shot(self):
    """Feeding text token by token must produce the same sentences."""
```

It pushes the same text through `split_sentences` three characters at a time
and asserts the result matches a single call. Streaming splitters are easy to
get subtly wrong in ways that only show up on particular token boundaries.

### Live tests

```bash
LANGPAL_LIVE=1 \
LANGPAL_LLM_URL=10.66.66.2:11435 \
LANGPAL_STT_URL=10.66.66.2:11435 \
LANGPAL_TTS_URL=127.0.0.1:8123 \
python -m unittest tests.test_live -v
```

Skipped unless `LANGPAL_LIVE=1`. They start a server on a spare port and drive
a real turn through it: health, transcription, streaming chat, synthesis, and
that a revoked turn is refused with 409. They register no voices and write
nothing, so your voice registry is untouched. A cold run takes a minute or two
while models load.

## Conventions

**Standard library only.** No `pip install`, ever. If something seems to need a
dependency, it probably needs less code instead.

**Nothing is written to disk at runtime.** Reads are limited to `web/`,
`.env.local` and `voices_default/`, all at startup. If you add a feature that
wants to persist something, put it in the browser's `localStorage` and validate
it server-side on arrival — that is what settings do.

**Validate at the boundary.** Everything from a browser passes through
`validate_settings()` or an explicit check before use. Add new settings to
`default_settings()` *and* `validate_settings()`; the first is what the UI is
seeded with, the second is what is enforced.

**Unset means unset.** A blank generation parameter is omitted from the
upstream request so the inference server's own configuration applies. Do not
substitute a LangPal default.

**Comment the why.** The code says what it does. Comments are for the reason —
usually a quirk of an upstream, a browser rule, or a failure mode that is not
obvious from the line itself.

## Adding a setting

1. Add it to `default_settings()` with a documented default.
2. Validate and clamp it in `validate_settings()`.
3. Use it — probably in `build_chat_payload()` or `synthesize()`.
4. Add a field to `web/index.html` with `data-setting="your_key"`. The form
   binding is generic; if it is numeric, add it to `NUMBER_SETTINGS` in
   `app.js` so blank becomes `null` rather than `0`.
5. Document it in [configuration.md](configuration.md).
6. Test the validation, especially the rejection cases.

Settings that should *not* be browser-settable — secrets, bind addresses —
belong in the `CONFIGURATION` block as module-level constants only.

## Tuning the voice detection

Constants at the top of `web/app.js`:

| Constant | Default | Effect |
| --- | --- | --- |
| `SPEECH_START_MS` | 140 | Sustained speech before a turn opens. Raise if noises trigger it. |
| `BARGE_START_MS` | 1000 | Sustained speech needed to interrupt the assistant. |
| `PREROLL_MS` | 320 | Audio kept from before speech began. Raise if first words are clipped. |
| `PREROLL_CAP_MS` | derived | `max(PREROLL_MS, BARGE_START_MS) + 250`. Never hard-code it: the ring must outlast whichever start window is longer. |
| `MIN_UTTERANCE_MS` | 350 | Below this, discarded as a cough. |
| `NOISE_FLOOR_MULT` | 3.2 | How far speech must beat the room. |
| `MIN_RMS` | 0.012 | Absolute floor for very quiet rooms. |
| `BARGE_MULT` | 2.2 | Extra margin so the reply cannot trigger itself. |
| `FLOOR_ATTACK` | 0.02 | How fast the noise floor tracks the room. |

Silence duration and sensitivity are exposed in Settings rather than hard-coded
— reach for those first.

Debugging is easiest with the level bar: it shows the smoothed RMS, so you can
see whether speech is actually clearing the bar.

## Tuning sentence chunking

In `langpal.py`:

| Constant | Default | Effect |
| --- | --- | --- |
| `MIN_SENTENCE_CHARS` | 12 | Fragments shorter than this are held back. |
| `MIN_SENTENCE_CHARS_CJK` | 5 | Lower, because CJK packs more meaning per character. |
| `MAX_SENTENCE_CHARS` | 320 | Force-cut for text with no punctuation. |
| `SENTENCE_TERMINATORS` | `.!?…。！？；;:\n` | |
| `SENTENCE_ABBREVIATIONS` | `mr`, `dr`, `etc`, … | A `.` after one of these does not split. |

Lowering `MIN_SENTENCE_CHARS` gets audio started sooner but sends more, shorter
requests; each one has fixed overhead, so past a point it gets slower, not
faster.

## Things that will bite you

**The microphone needs a secure context.** Testing from another device over
plain HTTP will fail with no obvious error beyond "microphone blocked". Use
`localhost`, or set up TLS. See [hosting.md](hosting.md).

**`decodeAudioData` detaches its buffer.** The `ArrayBuffer` cannot be reused
after the call. If you add retry logic around playback, re-fetch rather than
replay.

**`AudioContext` starts suspended.** iOS and Chrome require a user gesture.
`resumeContexts()` is called from the button handlers; anything that plays
audio needs to come after a real click.

**One handler instance per connection.** `BaseHTTPRequestHandler` reuses the
instance across keep-alive requests, so any per-request state must be reset in
`_dispatch()` — `_body_read` is the existing example.

**Respond, or consume the body.** If you add a route that can reject before
reading the body, `_send()` already handles it by closing the connection. Do
not work around that; the alternative is a desynchronised connection.

**llama-swap cannot route bodyless requests.** If you add a `GET` to the TTS
server, it will 404 through llama-swap's main port. See
[upstream-apis.md](upstream-apis.md#llama-swap).

## Style

Four-space indent, ~100 column soft limit, double quotes in Python and single
in JavaScript. Functions carry a docstring when what they do is not obvious
from the name; constants carry a comment when the value was chosen rather than
obvious.
