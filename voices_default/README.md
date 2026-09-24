# voices_default

Clips in this folder are registered as cloned voices when LangPal starts, so a
fresh browser always has voices to choose from without anyone having to record
one.

## Layout

```
voices_default/
  tr_ayse.wav      the reference clip
  tr_ayse.txt      its exact transcript
  en_sam.wav       no .txt -- transcribed by the STT model at startup
```

- The **file stem becomes the voice name**, so it must be letters, digits,
  `.`, `_` or `-`. Anything else is skipped with a warning in the log.
- A matching `.txt` is used as the reference transcript. Without one, the clip
  is sent to the STT model and whatever comes back is used.
- Voices that are already registered are left alone, so restarting is cheap.
- **This folder is only ever read.** LangPal does not write transcripts back
  and does not delete anything.

## Choosing clips

- One speaker, no music or background chatter.
- Roughly 5–20 seconds. The hard limit is 120 s.
- WAV. Mono is ideal; 24 kHz matches OmniVoice's own rate.
- The transcript must match the audio **exactly**. Clone quality depends on it
  more than on clip length — a wrong word makes the voice worse.

## What you will see

```
voices_default: 2 clip(s) found in C:\Users\you\LangPal\voices_default
  tr_ayse: registered (tr_ayse.txt)
  en_sam: registered (transcribed)
```

Registration runs on a background thread, so a cold llama-swap loading models
never delays the server starting.

## Turning it off

```bash
python langpal.py --no-default-voices
```

or `LANGPAL_REGISTER_DEFAULT_VOICES=false`. Point it somewhere else with
`--voices-dir` / `LANGPAL_VOICES_DIR`.

## A warning about names

Registering a name that matches a **built-in** speaker on your TTS server
shadows that speaker for as long as the server runs, because voice resolution
checks the registry first. Prefix your clips (`tr_`, `en_`) to keep them
clearly separate.
