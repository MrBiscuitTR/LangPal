# Using LangPal

## The main screen

Everything you need for a conversation is on one screen.

- **Start listening** — asks for microphone permission the first time, then
  turns into **Mute**. The green bar under the transcript shows what the
  microphone is picking up, so you can tell at a glance whether you are being
  heard.
- **Stop** — kills the current reply instantly: playback stops, queued
  sentences are dropped, and the server abandons any synthesis still in flight.
- **or type instead** — a text box, for when you cannot talk or the microphone
  is unavailable.

## Talking

Just talk. LangPal watches the microphone level and decides where your turn
ends:

1. It learns the noise floor of your room continuously while you are silent.
2. Speech has to beat that floor for about 140 ms before a turn opens, so a
   door closing does not start one.
3. The turn ends after about 900 ms of silence. That is adjustable.
4. The ~320 ms *before* you started talking is included, so the first word is
   never clipped.

Then the clip goes to the ASR model, the transcript goes to the LLM, and each
sentence of the reply is synthesised and played as soon as it is complete —
you hear the first sentence while the rest is still being generated.

### Interrupting

Talk over the reply and it stops. That is on by default and relies on your
browser's echo cancellation, which works best with headphones. On speakers, at
high volume, the assistant can occasionally interrupt itself; two ways out:

- turn **Let me interrupt while it is speaking** off in Settings, which mutes
  the microphone while the reply plays, or
- lower **Mic sensitivity**.

### If nothing happens when you talk

- Check the level bar moves. If it does not, the microphone permission or the
  device selection is the problem, not LangPal.
- Raise **Mic sensitivity** in Settings.
- If the page says microphone access is blocked, see
  [hosting.md](hosting.md) — you are almost certainly on plain HTTP.

## Languages

There is one **Conversation language** setting and it drives both directions:
what the ASR model is told to listen for, and what the TTS model speaks.

- **auto** lets Qwen3-ASR detect the language and replies in whatever it
  detected. Convenient, but unreliable on short clips — a two-second Turkish
  sample came back labelled *English* and, on another run, *Arabic*.
- **A language name** — `Turkish`, `French`, `Japanese` — forces it. Do this
  once you know what you are practising.

Use an English language *name*, not an ISO code. Qwen3-ASR silently ignores
`tr` and falls back to auto-detect; `Turkish` works.

To practise comprehension in one language and answer in another, open
**Use different languages for input and output** and set them separately.

## Voices

Open **Voices**.

The list shows every voice your TTS server knows, built-in and cloned. **Use**
selects one; **Remove** unregisters it.

### Adding your own

1. **Audio file** — pick a clip of one person talking, roughly 5–20 seconds,
   as clean as you can get. Any format your browser can decode works (WAV, MP3,
   M4A, OGG); it is converted to mono WAV in the browser.
2. The transcript box says *transcribing… (double check)* and stays locked
   while the ASR model works on it.
3. When it unlocks, **read it against the clip**. Voice cloning quality
   depends on the transcript matching the audio exactly — a wrong word makes
   the clone worse. Fix anything that is off.
4. Give it a name (letters, digits, `.`, `_`, `-`) and press **Register
   voice**. It is selected for you straight away.

If transcription fails, the box unlocks anyway so you can type the transcript
yourself.

To fix a bad clone, either **Remove** it and register again, or tick **Replace
an existing voice with this name**.

> A registered voice shadows a built-in speaker of the same name for as long as
> the TTS server runs. LangPal refuses a name that already exists unless you
> tick *Replace*, so this cannot happen by accident.

### Voices that are always there

Clips in the `voices_default/` folder next to `langpal.py` are registered
automatically at startup, so a fresh browser has voices to choose from without
anyone recording anything. See [configuration.md](configuration.md#default-voices).

## Settings

Settings are stored **in your browser**, not on the server. Each device has its
own, they survive restarts of the server, and nothing is written to the host's
disk.

- **Endpoints** — paste URLs in whatever form you have them; they are cleaned
  up. **Test connection** checks all three and reports round-trip times.
- **Model behaviour** — system prompt, how much history to replay, and the
  sampling parameters. Leave temperature, top-p, max tokens and seed **blank**
  to use whatever your inference server is already configured with; LangPal
  omits blank fields from the request entirely.
- **Reasoning** — sent as `reasoning_effort`, plus an explicit thinking
  on/off switch. Any thinking the model emits anyway is stripped before it is
  shown or spoken.
- **Listening** — silence threshold, sensitivity, barge-in, and whether a
  finished turn is sent automatically.

**Clear conversation** wipes the server-side history for your session.
**Reset settings to server defaults** discards this browser's overrides.

## Keyboard

- `Esc` — closes an open panel, or stops the reply if none is open.

Switching away from the tab stops playback, so a reply never carries on into an
empty room.
