'use strict';
/* LangPal front end.
 *
 * Responsibilities, in order of how a turn flows:
 *   1. capture microphone audio and decide where one spoken turn starts/ends
 *   2. encode it as 16 kHz mono WAV and POST it to /api/stt
 *   3. stream the reply from /api/chat, one sentence at a time
 *   4. fetch each sentence's audio from /api/tts and play it in order
 *
 * Everything the server needs to know about configuration travels in the
 * request body, read from localStorage. The server re-validates all of it.
 */

/* ---------------------------------------------------------------------------
 * Tuning
 * ------------------------------------------------------------------------- */

// The server reports a turn we cancelled ourselves with this exact message.
// It is the expected outcome of pressing Stop or interrupting, not a fault,
// so it is never shown. Anything else from the server is a real error.
const STOPPED_MESSAGE = 'stopped';

const STORAGE_SETTINGS = 'langpal.settings.v1';   // mirrors the server's schema
const STORAGE_LOCAL = 'langpal.local.v1';         // browser-only preferences

const TARGET_RATE = 16000;        // what we send to the ASR model
const VOICE_SAMPLE_RATE = 24000;  // reference clips: match OmniVoice's own rate

const SPEECH_START_MS = 140;      // sustained speech before a turn opens
// Interrupting takes a second of talking, measured as elapsed time rather
// than a run of loud frames -- see the note on BARGE_HOLD_MS. Deliberately
// far stricter than SPEECH_START_MS: cutting the assistant off by mistake is
// much more annoying than taking a moment to be sure. It keeps talking
// through the window, then stops outright when the second is up.
const BARGE_START_MS = 1000;      // how long you must be talking to interrupt
const MAX_UTTERANCE_MS = 60000;   // hard stop, below the server's 90 s limit
const PREROLL_MS = 320;           // audio kept from just before speech began
// The ring has to hold everything back to the moment speech might have
// started, which is the longest of the two start windows -- otherwise the
// opening words of an interruption are gone by the time it is confirmed.
// Derived rather than hard-coded so it cannot fall behind BARGE_START_MS.
const PREROLL_CAP_MS = Math.max(PREROLL_MS, BARGE_START_MS) + 250;
const MIN_UTTERANCE_MS = 350;     // shorter than this is a cough, not a turn

const NOISE_FLOOR_MULT = 3.2;     // speech must beat the room by this much
const MIN_RMS = 0.012;            // absolute floor, for very quiet rooms
const BARGE_MULT = 1.6;           // modest margin over the room floor; echo
                                  // rejection is ECHO_MULT's job, and stacking
                                  // both would deafen us in a noisy room
const FLOOR_ATTACK = 0.02;        // how fast the noise floor tracks the room

// Echo handling. The room's noise floor only adapts on quiet frames, so it
// freezes the moment playback starts and the bar stays where a silent room
// put it -- which the assistant's own voice then clears, cutting itself off.
// A separate peak-hold floor tracks how loud our output comes back: it rises
// instantly and decays slowly, so it stays high through the gaps between
// sentences instead of collapsing and re-triggering on the next one.
// Expected echo is computed from the buffer we are playing, not inferred
// from the microphone. ECHO_GAIN is the coupling between the two -- how
// much of what we play comes back -- learned continuously, because it
// depends on the speakers, the volume and the room.
const ECHO_MULT = 2.0;            // speech must beat the expected echo by this
const ECHO_GAIN_INIT = 0.5;       // starting guess before anything is learned
const ECHO_GAIN_RISE = 0.10;      // per frame, towards a stronger coupling
const ECHO_GAIN_FALL = 0.02;      // per frame, towards a weaker one
// The microphone hears a sample later than we scheduled it: output buffering
// plus the trip through the air. Read the playback envelope from slightly
// behind the playhead, over a window wide enough to forgive the estimate.
const ECHO_LAG_S = 0.06;
const ECHO_WINDOW_S = 0.06;

// How long evidence of speech survives without a fresh frame above the bar.
// Generous, because while the assistant is talking the bar only drops in the
// gaps between its words -- so a person talking steadily is detected in
// bursts, with the assistant's own syllables masking them in between.
const BARGE_HOLD_MS = 600;
const BARGE_GRACE_MS = 300;       // deaf at the start of a sentence, while
                                  // the echo floor is still converging. Short
                                  // because the floor is peak-held and so
                                  // settles on the first loud frame.

const LEVEL_DECAY = 0.82;         // smoothing for the on-screen level bar

// How many sentences may be at the TTS endpoint at once. Two is enough to
// keep the next one ready before the current finishes, and few enough that
// pressing Stop wastes at most one synthesis. A typical backend runs with
// --parallel 1 and serialises them anyway.
const MAX_TTS_INFLIGHT = 2;

const DEFAULT_LOCAL = {
  silenceMs: 900,                 // silence that ends a turn
  sensitivity: 5,                 // 1 (least) .. 10 (most)
  bargeIn: true,                  // allow interrupting the assistant
  autoSend: true,                 // send as soon as the turn ends
};

// Setting names whose form fields hold numbers, and which must become null
// (not 0, not "") when left blank so the server omits them upstream.
const NUMBER_SETTINGS = new Set([
  'temperature', 'top_p', 'max_tokens', 'seed', 'tts_seed', 'history_messages',
]);

const TRISTATE_SETTINGS = new Set(['enable_thinking']);

/* ---------------------------------------------------------------------------
 * State
 * ------------------------------------------------------------------------- */

const el = (id) => document.getElementById(id);

const state = {
  config: null,
  settings: {},
  local: Object.assign({}, DEFAULT_LOCAL),
  listening: false,
  muted: false,
  busy: false,            // a turn is in flight
  stopPending: null,      // promise for an in-progress /api/stop
  chatController: null,
  turnId: null,           // identifies the reply being produced, for Stop
  aborters: new Set(),    // every in-flight fetch Stop should cancel
  generation: 0,          // bumped by Stop; a turn from an older one is dropped
  detectedLanguage: '',
  sampleWav: null,        // ArrayBuffer of the clip in the voice form
};

const audio = {
  stream: null,
  captureCtx: null,
  playCtx: null,
  node: null,
  source: null,
  rate: TARGET_RATE,
  level: 0,
};

const vad = {
  mode: 'idle',           // idle | speech
  floor: MIN_RMS,
  echoGain: ECHO_GAIN_INIT,  // learned coupling: mic level per unit of playback
  speechSince: 0,            // when the current run of evidence began
  lastLoudAt: 0,             // last frame that cleared the bar
  chunks: [],
  totalSamples: 0,
  seededSamples: 0,       // how much of chunks came from the pre-roll, not speech
  preroll: [],
  prerollSamples: 0,
  loudSamples: 0,         // consecutive loud samples while idle
  quietSamples: 0,        // consecutive quiet samples while speaking
};

const tts = {
  pending: [],            // sentence texts not yet sent for synthesis
  inflight: [],           // { text, audio: Promise<AudioBuffer> }, in play order
  streaming: false,       // the chat stream is still producing sentences
  running: false,         // the playback worker loop is alive
  source: null,           // currently playing AudioBufferSourceNode
  finishCurrent: null,    // resolves the in-progress playDecoded promise
  channel: null,          // samples of the playing buffer, for echo prediction
  channelRate: 0,         // its sample rate
  playStartedAt: 0,       // playCtx.currentTime when it started
  startedAt: 0,           // when the current sentence began, for the grace window
};

/* ---------------------------------------------------------------------------
 * Small helpers
 * ------------------------------------------------------------------------- */

function setStatus(text, kind) {
  const node = el('status');
  node.textContent = text;
  node.classList.toggle('is-error', kind === 'error');
  node.classList.toggle('is-live', kind === 'live');
}

async function api(path, body, signal) {
  const resp = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
    signal: signal,
  });
  let data = null;
  try { data = await resp.json(); } catch (err) { data = null; }
  if (!resp.ok) throw new Error((data && data.error) || ('HTTP ' + resp.status));
  return data || {};
}

// btoa() needs a binary string; build it in chunks so a long clip does not
// blow the argument limit of String.fromCharCode.
function toBase64(bytes) {
  const CHUNK = 0x8000;
  let out = '';
  for (let i = 0; i < bytes.length; i += CHUNK) {
    out += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK));
  }
  return btoa(out);
}

const msToSamples = (ms, rate) => Math.round((ms / 1000) * rate);

// Identifies one reply, so Stop can revoke exactly that work on the server.
// Generated here rather than counted on both sides: a number the browser and
// the server each track independently drifts apart the first time a reload
// or a dropped response desynchronises them, and there is no way back.
function newTurnId() {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
}

/* ---------------------------------------------------------------------------
 * Settings persistence
 * ------------------------------------------------------------------------- */

function loadStored(key, fallback) {
  try {
    const raw = localStorage.getItem(key);
    if (!raw) return Object.assign({}, fallback);
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== 'object') return Object.assign({}, fallback);
    return Object.assign({}, fallback, parsed);
  } catch (err) {
    // Corrupt or blocked storage must not stop the app from starting.
    return Object.assign({}, fallback);
  }
}

function saveStored(key, value) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch (err) {
    setStatus('settings could not be saved in this browser', 'error');
  }
}

function settingsForRequest() {
  return state.settings;
}

/* ---------------------------------------------------------------------------
 * Transcript rendering
 * ------------------------------------------------------------------------- */

function addMessage(role, text) {
  const node = document.createElement('div');
  node.className = 'msg ' + role;
  // textContent, never innerHTML: model output is untrusted text.
  node.textContent = text;
  el('transcript').appendChild(node);
  scrollTranscript();
  return node;
}

function note(text, isError) {
  const node = addMessage('note', text);
  if (isError) node.classList.add('is-error');
  return node;
}

function scrollTranscript() {
  const box = el('transcript');
  box.scrollTop = box.scrollHeight;
}

/* ---------------------------------------------------------------------------
 * WAV encoding and resampling
 * ------------------------------------------------------------------------- */

// Box-average resampler. Averaging across the whole source window (rather than
// picking one sample) gives cheap anti-aliasing, which matters when going from
// 48 kHz to 16 kHz.
function resample(input, fromRate, toRate) {
  if (fromRate === toRate) return input;
  const ratio = fromRate / toRate;
  const outLength = Math.floor(input.length / ratio);
  const out = new Float32Array(outLength);
  for (let i = 0; i < outLength; i++) {
    const start = Math.floor(i * ratio);
    const end = Math.min(Math.floor((i + 1) * ratio), input.length);
    let sum = 0;
    let count = 0;
    for (let j = start; j < end; j++) { sum += input[j]; count++; }
    out[i] = count ? sum / count : 0;
  }
  return out;
}

// 44-byte canonical RIFF header + signed 16-bit little-endian mono PCM, which
// is what both llama.cpp's audio loader and omnivoice.cpp expect.
function encodeWav(samples, rate) {
  const buffer = new ArrayBuffer(44 + samples.length * 2);
  const view = new DataView(buffer);
  const ascii = (offset, text) => {
    for (let i = 0; i < text.length; i++) view.setUint8(offset + i, text.charCodeAt(i));
  };

  ascii(0, 'RIFF');
  view.setUint32(4, 36 + samples.length * 2, true);
  ascii(8, 'WAVE');
  ascii(12, 'fmt ');
  view.setUint32(16, 16, true);          // PCM chunk size
  view.setUint16(20, 1, true);           // format 1 = PCM
  view.setUint16(22, 1, true);           // mono
  view.setUint32(24, rate, true);
  view.setUint32(28, rate * 2, true);    // byte rate
  view.setUint16(32, 2, true);           // block align
  view.setUint16(34, 16, true);          // bits per sample
  ascii(36, 'data');
  view.setUint32(40, samples.length * 2, true);

  let offset = 44;
  for (let i = 0; i < samples.length; i++) {
    // Clamp before scaling; a value above 1.0 would wrap around to silence.
    const clamped = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(offset, clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff, true);
    offset += 2;
  }
  return buffer;
}

function concatChunks(chunks, total) {
  const out = new Float32Array(total);
  let offset = 0;
  for (const chunk of chunks) { out.set(chunk, offset); offset += chunk.length; }
  return out;
}

/* ---------------------------------------------------------------------------
 * Microphone capture and voice activity detection
 * ------------------------------------------------------------------------- */

// RMS of the audio we are playing right now, as the microphone should be
// hearing it. Zero when nothing is playing, and -- crucially -- near zero
// in the pauses between words, which is where interruptions get detected.
function playbackLevel() {
  if (!tts.channel || !audio.playCtx) return 0;
  const elapsed = audio.playCtx.currentTime - tts.playStartedAt - ECHO_LAG_S;
  if (elapsed < 0) return 0;

  const data = tts.channel;
  const rate = tts.channelRate;
  const half = Math.round((ECHO_WINDOW_S * rate) / 2);
  const centre = Math.round(elapsed * rate);
  const from = Math.max(0, centre - half);
  const to = Math.min(data.length, centre + half);
  if (to <= from) return 0;

  let sum = 0;
  for (let i = from; i < to; i++) sum += data[i] * data[i];
  return Math.sqrt(sum / (to - from));
}

// What the microphone should be picking up from us alone.
function expectedEcho() {
  return playbackLevel() * vad.echoGain;
}

function currentThreshold() {
  // Higher sensitivity lowers the bar. 0.85^(s-5) maps 1..10 to ~1.9..0.44.
  const sensMul = Math.pow(0.85, (state.local.sensitivity || 5) - 5);
  const base = Math.max(vad.floor * NOISE_FLOOR_MULT, MIN_RMS) * sensMul;
  if (!audioAudible()) return base;
  // While sound is playing, the bar is whichever is higher: the usual
  // threshold with a little margin, or a margin above the echo we predict
  // from the buffer itself. In a pause the second term collapses and this
  // becomes ordinary listening.
  //
  // sensMul applies to both terms. It used to scale only the first, which
  // made Mic sensitivity do nothing whatsoever for interrupting -- the one
  // knob a user reaches for when barge-in will not fire.
  return Math.max(base * BARGE_MULT, expectedEcho() * ECHO_MULT * sensMul);
}

// Is a reply in progress at all? True from the first sentence being queued
// until the last has finished playing. Speaking over it should cancel it.
function replyActive() {
  return tts.running || tts.source !== null;
}

// Is sound actually coming out of the speaker right now? Only then is there
// echo to compensate for. These differ for the second or so a sentence
// spends being synthesised, where treating silence as though it were echo
// left the threshold keyed to an echo floor of zero.
function audioAudible() {
  return tts.source !== null;
}

// The browser's own wording for a refused microphone -- "not allowed by the
// user agent or the platform in the current context" -- covers half a dozen
// unrelated causes and points at none of them. Name the likely ones instead.
function describeMicError(err) {
  const name = (err && err.name) || '';

  if (name === 'NotFoundError' || name === 'DevicesNotFoundError') {
    return 'No microphone found. Check that one is connected and enabled.';
  }
  if (name === 'NotReadableError' || name === 'TrackStartError') {
    return 'The microphone is in use by another application, or the system '
         + 'refused access to it. Close anything else that is recording.';
  }
  if (name === 'SecurityError') {
    return 'The browser blocked microphone access because this page is not a '
         + 'trusted secure context. See docs/hosting.md.';
  }
  if (name === 'NotAllowedError' || name === 'PermissionDeniedError') {
    // Three genuinely different causes share this one error name, so list
    // them in the order they are worth checking.
    return 'Microphone permission was refused. Three things cause this: '
         + '(1) the permission prompt was dismissed or previously blocked -- '
         + 'clear it from the padlock icon in the address bar; '
         + '(2) Windows is blocking it -- Settings, Privacy & security, '
         + 'Microphone, and turn on access for desktop apps; '
         + '(3) you clicked through a certificate warning to reach this page, '
         + 'and the browser will not grant the microphone on an origin whose '
         + 'certificate it does not trust. See docs/hosting.md for how to make '
         + 'the certificate genuinely trusted.';
  }
  return 'Could not open the microphone: ' + (err && (err.message || err.name) || err);
}

async function ensureAudio() {
  if (audio.stream) return;

  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    throw new Error(
      window.isSecureContext
        ? 'this browser has no microphone API'
        : 'microphone blocked: the page must be served over HTTPS or from localhost'
    );
  }

  try {
    audio.stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        // Browser-side acoustic echo cancellation is what makes barge-in work
        // when the reply is coming out of the same device's speaker.
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    });
  } catch (err) {
    throw new Error(describeMicError(err));
  }

  // Two contexts on purpose: capture is pinned to 16 kHz so no resampling is
  // needed in the common case, while playback runs at the device's native rate
  // so the 24 kHz reply is not degraded on the way out.
  try {
    audio.captureCtx = new AudioContext({ sampleRate: TARGET_RATE, latencyHint: 'interactive' });
  } catch (err) {
    audio.captureCtx = new AudioContext();
  }
  audio.rate = audio.captureCtx.sampleRate;   // honoured or not, we adapt
  audio.playCtx = new AudioContext();

  await audio.captureCtx.audioWorklet.addModule('/capture-worklet.js');
  audio.source = audio.captureCtx.createMediaStreamSource(audio.stream);
  audio.node = new AudioWorkletNode(audio.captureCtx, 'capture-processor');
  audio.node.port.onmessage = (event) => onFrame(event.data);
  audio.source.connect(audio.node);
  // Not connected to the destination: we never want to hear ourselves.
}

async function resumeContexts() {
  // Both contexts start suspended until a user gesture on iOS and Chrome.
  if (audio.captureCtx && audio.captureCtx.state === 'suspended') await audio.captureCtx.resume();
  if (audio.playCtx && audio.playCtx.state === 'suspended') await audio.playCtx.resume();
}

function resetVad() {
  vad.mode = 'idle';
  vad.chunks = [];
  vad.totalSamples = 0;
  vad.seededSamples = 0;
  vad.preroll = [];
  vad.prerollSamples = 0;
  vad.loudSamples = 0;
  vad.quietSamples = 0;
  vad.speechSince = 0;
  vad.lastLoudAt = 0;
}

// Populated only while the settings panel is open, and throttled: this is a
// diagnostic, not something to spend a render quantum on.
let readoutDue = 0;

function updateReadout(rms, threshold) {
  const node = el('vad-readout');
  if (!node || el('settings').hidden) return;
  const now = Date.now();
  if (now < readoutDue) return;
  readoutDue = now + 100;

  const pct = (v) => (v * 1000).toFixed(1);
  let held = '';
  if (replyActive()) {
    const secs = vad.speechSince ? (now - vad.speechSince) / 1000 : 0;
    held = '  |  talking ' + secs.toFixed(1) + '/' +
           (BARGE_START_MS / 1000).toFixed(1) + 's';
  }
  node.textContent =
    'level ' + pct(rms) +
    '  |  bar ' + pct(threshold) +
    '  |  room ' + pct(vad.floor) +
    '  |  echo ' + pct(expectedEcho()) +
    '  |  ' + (rms > threshold ? 'HEARD' : 'below') +
    (audioAudible() ? '  |  speaking' : '') + held;
}

function onFrame(frame) {
  const samples = frame.samples;
  const rms = frame.rms;
  const rate = audio.rate;

  // Level bar: decay slowly so it reads as a meter rather than a strobe.
  audio.level = Math.max(rms, audio.level * LEVEL_DECAY);
  el('level').style.width = Math.min(100, audio.level * 600).toFixed(1) + '%';

  if (!state.listening || state.muted) return;

  // Half-duplex mode: ignore the microphone entirely while the reply plays.
  if (replyActive() && !state.local.bargeIn) return;

  // A turn is already being transcribed or answered, so do not queue another.
  // The exception is barge-in over audible speech: beginUtterance() cancels
  // the turn in progress, and refusing here would make it impossible to
  // interrupt while the model is still generating.
  if (state.busy && vad.mode === 'idle' && !replyActive()) return;

  const threshold = currentThreshold();
  const loud = rms > threshold;
  updateReadout(rms, threshold);

  // Learn how much of what we play comes back, from frames that look like
  // echo alone. Loud frames are skipped: one carrying the user's voice would
  // inflate the coupling, raising the bar above them, so the harder they
  // talked the further out of reach it would get.
  //
  // Only worth measuring while the playback is loud enough to dominate.
  const bootstrapping = audioAudible() && Date.now() - tts.startedAt < BARGE_GRACE_MS;
  const playing = playbackLevel();
  // The measurement is rms / playing, so a quiet playback frame divides by
  // almost nothing and throws the estimate wildly off. Only measure where
  // our own output clearly dominates whatever else is in the room.
  const measurable = playing > Math.max(MIN_RMS * 2, vad.floor * 4);
  if (audioAudible() && vad.mode === 'idle' && measurable &&
      (bootstrapping || !loud)) {
    const observed = rms / playing;
    const step = observed > vad.echoGain ? ECHO_GAIN_RISE : ECHO_GAIN_FALL;
    vad.echoGain += (observed - vad.echoGain) * step;
  }

  if (vad.mode === 'idle') {
    // Track the room's noise floor from quiet frames only, so a long sentence
    // cannot drag the floor up and deafen us mid-conversation.
    //
    // And never while our own audio is playing. Once the echo floor lifts the
    // bar, the echo itself registers as "quiet" and would be learnt as room
    // noise: within about half a second vad.floor reaches the echo level, and
    // base * BARGE_MULT (3.2 x 1.6 = 5.1x the echo) swamps the echo term that
    // is supposed to be governing this. That is a second, slower inflation
    // path to exactly the failure ECHO_MULT was introduced to prevent.
    if (!loud && !audioAudible()) {
      vad.floor = vad.floor * (1 - FLOOR_ATTACK) + rms * FLOOR_ATTACK;
    }

    vad.preroll.push(samples);
    vad.prerollSamples += samples.length;
    const prerollCap = msToSamples(PREROLL_CAP_MS, rate);
    while (vad.prerollSamples > prerollCap && vad.preroll.length > 1) {
      vad.prerollSamples -= vad.preroll.shift().length;
    }

    // The first moments of a sentence are the worst time to judge: the echo
    // floor is still bootstrapping on the new audio.
    if (bootstrapping) {
      vad.loudSamples = 0;
      return;
    }

    if (replyActive()) {
      // Interrupting is judged on elapsed time, not on a run of loud frames.
      // While the assistant is talking the bar only drops in the gaps between
      // its words, so a person speaking normally is detected in bursts. A run
      // of loud frames is therefore unsatisfiable however long they talk --
      // only shouting one continuous note ever managed it.
      const now = Date.now();
      if (loud) {
        if (!vad.speechSince) vad.speechSince = now;
        vad.lastLoudAt = now;
      } else if (vad.speechSince && now - vad.lastLoudAt > BARGE_HOLD_MS) {
        vad.speechSince = 0;   // they stopped; the evidence lapses
      }
      if (vad.speechSince && now - vad.speechSince >= BARGE_START_MS) {
        beginUtterance();
      }
      return;
    }

    // Starting an ordinary turn, with nothing playing to mask it. Here a run
    // of loud frames is exactly the right test, and much harder for a stray
    // noise to fake. Decay rather than reset, so the dips between words do
    // not keep restarting the count.
    vad.loudSamples = loud
      ? vad.loudSamples + samples.length
      : Math.max(0, vad.loudSamples - samples.length * 1.5);
    if (vad.loudSamples >= msToSamples(SPEECH_START_MS, rate)) beginUtterance();
    return;
  }

  // mode === 'speech'
  vad.chunks.push(samples);
  vad.totalSamples += samples.length;
  vad.quietSamples = loud ? 0 : vad.quietSamples + samples.length;

  if (vad.quietSamples >= msToSamples(state.local.silenceMs, rate)) {
    endUtterance();
  } else if (vad.totalSamples >= msToSamples(MAX_UTTERANCE_MS, rate)) {
    endUtterance();
  }
}

function beginUtterance() {
  // Speaking over the assistant cancels it, which is the whole point of
  // barge-in. Also cancels any queued synthesis on the server.
  const interrupting = replyActive();
  if (interrupting) stopEverything();

  vad.mode = 'speech';
  vad.chunks = vad.preroll.slice();      // keep the word that opened the turn
  vad.totalSamples = vad.prerollSamples;
  // Remember how much of this is lead-in, so the short-utterance filter below
  // measures speech rather than the pre-roll, which is now over a second long.
  vad.seededSamples = vad.prerollSamples;
  vad.preroll = [];
  vad.prerollSamples = 0;
  vad.loudSamples = 0;
  vad.quietSamples = 0;
  // Say so when we cut the assistant off, so a self-interruption is visible
  // rather than looking like the reply simply stopped.
  setStatus(interrupting ? 'interrupted, listening…' : 'listening…', 'live');
}

function endUtterance() {
  const samples = concatChunks(vad.chunks, vad.totalSamples);
  const rate = audio.rate;
  // Length of the speech itself, excluding the pre-roll lead-in. Measuring the
  // whole clip would make this test meaningless, since the pre-roll alone is
  // longer than MIN_UTTERANCE_MS.
  const spokenSamples = vad.totalSamples - vad.seededSamples;
  resetVad();

  const ms = (spokenSamples / rate) * 1000;
  if (ms < MIN_UTTERANCE_MS) {
    setStatus(state.listening ? 'ready' : 'idle');
    return;
  }
  submitAudio(samples, rate, !state.local.autoSend).catch((err) => {
    setStatus(String(err.message || err), 'error');
    state.busy = false;
  });
}

/* ---------------------------------------------------------------------------
 * One conversational turn
 * ------------------------------------------------------------------------- */

// `reviewOnly` transcribes but does not send: the text lands in the input box
// so it can be corrected first. That is what the "send automatically" setting
// turns off -- discarding the recording outright would just lose it.
async function submitAudio(samples, rate, reviewOnly) {
  // After a barge-in the previous turn is still unwinding from its abort.
  // Wait briefly rather than discarding what the user just said.
  for (let i = 0; i < 40 && state.busy; i++) {
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
  if (state.busy) return;
  state.busy = true;
  try {
    if (state.stopPending) await state.stopPending;   // let the stop land first
    setStatus('transcribing…');

    const generation = state.generation;
    const controller = new AbortController();
    state.aborters.add(controller);
    let result;
    try {
      const pcm = resample(samples, rate, TARGET_RATE);
      const wav = encodeWav(pcm, TARGET_RATE);
      result = await api('/api/stt', {
        audio_b64: toBase64(new Uint8Array(wav)),
        settings: settingsForRequest(),
      }, controller.signal);
    } catch (err) {
      if (err.name === 'AbortError') return;   // Stop was pressed
      throw err;
    } finally {
      state.aborters.delete(controller);
    }

    // Stop may have landed while the clip was being transcribed. Drop it
    // rather than answering something the user has already abandoned.
    if (state.generation !== generation) return;

    const text = (result.text || '').trim();
    if (!text) {
      setStatus('nothing recognised');
      return;
    }
    state.detectedLanguage = result.language || '';

    if (reviewOnly) {
      const input = el('text-input');
      input.value = text;
      input.focus();
      setStatus('check it, then press Send');
      return;
    }

    addMessage('user', text);
    await runChat(text);
  } finally {
    state.busy = false;
    if (state.listening) setStatus('ready', 'live');
  }
}

async function sendTypedText(text) {
  if (state.busy) return;
  state.busy = true;
  try {
    if (state.stopPending) await state.stopPending;
    addMessage('user', text);
    await runChat(text);
  } catch (err) {
    setStatus(String(err.message || err), 'error');
  } finally {
    state.busy = false;
    if (state.listening) setStatus('ready', 'live');
  }
}

async function runChat(text) {
  const turn = newTurnId();
  state.turnId = turn;
  const controller = new AbortController();
  state.chatController = controller;
  el('btn-stop').disabled = false;
  setStatus('thinking…');

  const bubble = addMessage('assistant', '');
  tts.streaming = true;

  try {
    const resp = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        text: text,
        turn: turn,
        settings: settingsForRequest(),
      }),
      signal: controller.signal,
    });

    if (!resp.ok) {
      let message = 'HTTP ' + resp.status;
      try { message = (await resp.json()).error || message; } catch (err) { /* keep */ }
      throw new Error(message);
    }

    await readSse(resp, (event, data) => {
      if (event === 'delta') {
        bubble.textContent += data.t;
        scrollTranscript();
      } else if (event === 'sentence') {
        // Queue it and start synthesising straight away, rather than
        // waiting for its turn to play.
        tts.pending.push(data.text);
        pumpTts();
        startTtsWorker();
      } else if (event === 'done') {
        if (!bubble.textContent) bubble.remove();
      } else if (event === 'error') {
        // A stop we asked for normally aborts the fetch before this event
        // is read, but the two race; when the event wins, do not surface it.
        if (data.message !== STOPPED_MESSAGE) {
          note(data.message, true);
          setStatus(data.message, 'error');
        }
      }
    });
  } catch (err) {
    // AbortError is our own Stop. STOPPED_MESSAGE is the server agreeing,
    // which arrives as a 409 when a revoked turn is rejected outright --
    // neither is a fault worth putting on screen.
    if (err.name !== 'AbortError' && String(err.message) !== STOPPED_MESSAGE) {
      note(String(err.message || err), true);
      setStatus(String(err.message || err), 'error');
    }
    if (!bubble.textContent) bubble.remove();
  } finally {
    tts.streaming = false;
    state.chatController = null;
  }
}

// Minimal Server-Sent Events reader over fetch(). EventSource cannot POST, and
// the turn needs a body, so the framing is parsed here instead.
async function readSse(resp, onEvent) {
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    let split;
    while ((split = buffer.indexOf('\n\n')) !== -1) {
      const block = buffer.slice(0, split);
      buffer = buffer.slice(split + 2);

      let event = 'message';
      const dataLines = [];
      for (const line of block.split('\n')) {
        if (line.startsWith('event:')) event = line.slice(6).trim();
        else if (line.startsWith('data:')) dataLines.push(line.slice(5).trim());
        // lines starting with ':' are heartbeat comments; ignore them
      }
      if (!dataLines.length) continue;
      try {
        onEvent(event, JSON.parse(dataLines.join('\n')));
      } catch (err) {
        /* a malformed frame should not kill the stream */
      }
    }
  }
}

/* ---------------------------------------------------------------------------
 * Speech playback
 * ------------------------------------------------------------------------- */

async function fetchSentenceAudio(text) {
  const controller = new AbortController();
  state.aborters.add(controller);
  try {
    const resp = await fetch('/api/tts', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        text: text,
        turn: state.turnId,
        detected_language: state.detectedLanguage,
        settings: settingsForRequest(),
      }),
      signal: controller.signal,
    });
    if (!resp.ok) {
      let message = 'HTTP ' + resp.status;
      try { message = (await resp.json()).error || message; } catch (err) { /* keep */ }
      throw new Error(message);
    }
    // Decode here rather than at playback time: doing it on this path
    // means the buffer is ready the instant the previous sentence ends.
    return await decodeAudio(await resp.arrayBuffer());
  } finally {
    state.aborters.delete(controller);
  }
}

// Callback form, wrapped: older Safari has no promise-returning overload.
function decodeAudio(raw) {
  if (!audio.playCtx) audio.playCtx = new AudioContext();
  return new Promise((resolve, reject) => {
    // decodeAudioData detaches the ArrayBuffer; nothing else may reuse it.
    audio.playCtx.decodeAudioData(
      raw,
      resolve,
      (err) => reject(err || new Error('could not decode the reply audio'))
    );
  });
}

// Start synthesising whatever is waiting, up to the concurrency limit.
// Called when a sentence arrives and again whenever a slot frees, so work
// begins as early as possible instead of at playback time.
function pumpTts() {
  while (tts.inflight.length < MAX_TTS_INFLIGHT && tts.pending.length) {
    const text = tts.pending.shift();
    const audio = fetchSentenceAudio(text);
    // Stop drops the whole list, so a rejection here may end up with nobody
    // awaiting it. Attach a no-op handler to keep it from surfacing as an
    // unhandled rejection; the worker still sees the rejection when it awaits.
    audio.catch(() => {});
    tts.inflight.push({ text: text, audio: audio });
  }
}

function playDecoded(decoded) {
  return new Promise((resolve) => {
    const source = audio.playCtx.createBufferSource();
    source.buffer = decoded;
    source.connect(audio.playCtx.destination);

    // This promise MUST settle on every path. If it does not, ttsWorker
    // waits on it forever, tts.running stays true, and replyActive() is
    // permanently true -- which leaves the microphone stuck in barge-in
    // mode and the app silently deaf. Idempotent, so stopping mid-playback
    // and the natural end cannot both resolve it twice.
    let settled = false;
    const finish = () => {
      if (settled) return;
      settled = true;
      if (tts.source === source) tts.source = null;
      if (tts.finishCurrent === finish) tts.finishCurrent = null;
      resolve();
    };

    source.onended = finish;
    tts.source = source;
    tts.finishCurrent = finish;   // stopEverything() calls this directly
    tts.channel = decoded.getChannelData(0);
    tts.channelRate = decoded.sampleRate;
    tts.playStartedAt = audio.playCtx.currentTime;
    tts.startedAt = Date.now();
    source.start();
  });
}

function startTtsWorker() {
  if (tts.running) return;
  tts.running = true;
  ttsWorker().catch((err) => {
    if (err && err.name !== 'AbortError') setStatus(String(err.message || err), 'error');
  }).finally(() => {
    tts.running = false;
    // The reply is over: stop predicting echo from a buffer that has
    // finished playing.
    tts.channel = null;
    el('btn-stop').disabled = true;
    if (state.listening) setStatus('ready', 'live');
  });
}

// Plays sentences strictly in order. Synthesis has already been started by
// pumpTts() when each sentence arrived, so this loop usually finds the next
// buffer already decoded and the join between sentences is inaudible.
async function ttsWorker() {
  for (;;) {
    pumpTts();

    if (!tts.inflight.length) {
      // Nothing synthesising. Either more sentences are still coming, or
      // the reply is finished and this worker is done.
      if (tts.streaming || tts.pending.length) {
        await new Promise((resolve) => setTimeout(resolve, 25));
        continue;
      }
      return;
    }

    const job = tts.inflight.shift();
    pumpTts();   // a slot just freed, so start the sentence after next

    let decoded;
    try {
      decoded = await job.audio;
    } catch (err) {
      if (err.name === 'AbortError') return;
      // Same race as above: a sentence cancelled by Stop comes back 409.
      if (String(err.message) !== STOPPED_MESSAGE) {
        note(String(err.message || err), true);
      }
      continue;
    }

    setStatus('speaking…', 'live');
    try {
      await playDecoded(decoded);
    } catch (err) {
      note(String(err.message || err), true);
    }
  }
}

/* ---------------------------------------------------------------------------
 * Stop
 * ------------------------------------------------------------------------- */

// Halts playback, drops queued sentences, aborts in-flight requests and bumps
// revoking the turn server-side so work already queued upstream is dropped too.
function stopEverything() {
  // Any turn already in flight belongs to the previous generation and must
  // not carry on to the model once it comes back.
  state.generation++;
  tts.pending.length = 0;
  tts.inflight.length = 0;
  tts.streaming = false;

  if (tts.source) {
    const source = tts.source;
    tts.source = null;
    // Leave onended attached: stop() fires it, which settles the waiting
    // playDecoded promise. Clearing it here is what used to hang the worker.
    try { source.stop(); } catch (err) { /* already finished */ }
    try { source.disconnect(); } catch (err) { /* already detached */ }
  }
  // Belt and braces: settle it ourselves in case onended does not fire.
  if (tts.finishCurrent) tts.finishCurrent();

  tts.channel = null;

  if (state.chatController) {
    state.chatController.abort();
    state.chatController = null;
  }
  for (const controller of state.aborters) controller.abort();
  state.aborters.clear();

  el('btn-stop').disabled = true;
  setStatus(state.listening ? 'ready' : 'idle', state.listening ? 'live' : null);

  // Tell the server to revoke this turn, so requests already queued upstream
  // are dropped rather than synthesised into a socket nobody is reading.
  // Nothing depends on the reply: the local stop has already happened, and a
  // turn id the server never hears about simply runs to completion there.
  const turn = state.turnId;
  state.turnId = null;
  state.stopPending = api('/api/stop', turn ? { turn: turn } : {})
    .catch(() => { /* best effort */ })
    .finally(() => { state.stopPending = null; });
  return state.stopPending;
}

/* ---------------------------------------------------------------------------
 * Microphone button
 * ------------------------------------------------------------------------- */

async function toggleMic() {
  const button = el('btn-mic');

  if (!state.listening) {
    button.disabled = true;
    try {
      await ensureAudio();
      await resumeContexts();
    } catch (err) {
      setStatus(String(err.message || err), 'error');
      note(String(err.message || err), true);
      button.disabled = false;
      return;
    }
    button.disabled = false;
    state.listening = true;
    state.muted = false;
    resetVad();
    audio.stream.getAudioTracks().forEach((track) => { track.enabled = true; });
    button.textContent = 'Mute';
    button.classList.add('listening');
    button.classList.remove('muted');
    setStatus('ready', 'live');
    return;
  }

  // Already started: this toggles the mute state.
  state.muted = !state.muted;
  // Disable the track as well, so the browser's recording indicator is honest.
  audio.stream.getAudioTracks().forEach((track) => { track.enabled = !state.muted; });
  if (state.muted) resetVad();
  button.textContent = state.muted ? 'Unmute' : 'Mute';
  button.classList.toggle('muted', state.muted);
  button.classList.toggle('listening', !state.muted);
  setStatus(state.muted ? 'muted' : 'ready', state.muted ? null : 'live');
}

/* ---------------------------------------------------------------------------
 * Settings form
 * ------------------------------------------------------------------------- */

function settingFields() {
  return Array.from(document.querySelectorAll('[data-setting]'));
}

function localFields() {
  return Array.from(document.querySelectorAll('[data-local]'));
}

function fillForm() {
  for (const field of settingFields()) {
    const key = field.dataset.setting;
    const value = state.settings[key];
    if (TRISTATE_SETTINGS.has(key)) {
      field.value = value === true ? 'true' : value === false ? 'false' : '';
    } else {
      field.value = value === null || value === undefined ? '' : String(value);
    }
  }
  for (const field of localFields()) {
    const key = field.dataset.local;
    if (field.type === 'checkbox') field.checked = Boolean(state.local[key]);
    else field.value = String(state.local[key]);
  }
}

function readForm() {
  const next = Object.assign({}, state.settings);

  for (const field of settingFields()) {
    const key = field.dataset.setting;

    // A <select> that has not been populated yet reports "". Reading it would
    // silently wipe a configured voice if the user changed another setting
    // before the voice list finished loading.
    if (field.tagName === 'SELECT' && field.options.length === 0) continue;

    const raw = field.value.trim();

    if (TRISTATE_SETTINGS.has(key)) {
      next[key] = raw === 'true' ? true : raw === 'false' ? false : null;
    } else if (NUMBER_SETTINGS.has(key)) {
      // Blank means "unset": the server then omits the field entirely and the
      // inference server's own default applies.
      next[key] = raw === '' ? null : Number(raw);
      if (raw !== '' && !Number.isFinite(next[key])) next[key] = null;
    } else {
      next[key] = raw;
    }
  }
  state.settings = next;
  saveStored(STORAGE_SETTINGS, next);

  const local = Object.assign({}, state.local);
  for (const field of localFields()) {
    const key = field.dataset.local;
    if (field.type === 'checkbox') local[key] = field.checked;
    else {
      const num = Number(field.value);
      if (Number.isFinite(num)) local[key] = num;
    }
  }
  state.local = local;
  saveStored(STORAGE_LOCAL, local);
}

function populateChoices() {
  const languages = el('language-list');
  languages.replaceChildren();
  for (const name of state.config.languages) {
    const option = document.createElement('option');
    option.value = name;
    languages.appendChild(option);
  }

  const reasoning = el('s-reasoning');
  reasoning.replaceChildren();
  for (const value of state.config.reasoning_efforts) {
    const option = document.createElement('option');
    option.value = value;
    option.textContent = value === '' ? "leave to the server" : value;
    reasoning.appendChild(option);
  }
}

/* ---------------------------------------------------------------------------
 * Voices
 * ------------------------------------------------------------------------- */

function renderVoiceOptions(voices) {
  const select = el('s-voice');
  const current = state.settings.voice || '';
  select.replaceChildren();

  const blank = document.createElement('option');
  blank.value = '';
  blank.textContent = "the TTS server's default voice";
  select.appendChild(blank);

  for (const name of voices) {
    const option = document.createElement('option');
    option.value = name;
    option.textContent = name;
    select.appendChild(option);
  }
  // Keep a configured-but-missing voice visible rather than silently switching.
  if (current && !voices.includes(current)) {
    const option = document.createElement('option');
    option.value = current;
    option.textContent = current + ' (not registered)';
    select.appendChild(option);
  }
  select.value = current;
}

function renderVoiceList(voices) {
  const list = el('voice-list');
  list.replaceChildren();

  if (!voices.length) {
    const empty = document.createElement('li');
    empty.textContent = 'No voices registered yet.';
    list.appendChild(empty);
    return;
  }

  for (const name of voices) {
    const row = document.createElement('li');

    const label = document.createElement('span');
    label.className = 'name';
    label.textContent = name;
    row.appendChild(label);

    if (state.settings.voice === name) {
      const current = document.createElement('span');
      current.className = 'current';
      current.textContent = 'in use';
      row.appendChild(current);
    } else {
      const use = document.createElement('button');
      use.type = 'button';
      use.className = 'ghost';
      use.textContent = 'Use';
      use.addEventListener('click', () => {
        state.settings.voice = name;
        saveStored(STORAGE_SETTINGS, state.settings);
        fillForm();
        refreshVoices();
      });
      row.appendChild(use);
    }

    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'ghost';
    remove.textContent = 'Remove';
    remove.addEventListener('click', async () => {
      if (!window.confirm('Remove the voice "' + name + '"?')) return;
      remove.disabled = true;
      try {
        await api('/api/voices/delete', { name: name, settings: settingsForRequest() });
        if (state.settings.voice === name) {
          state.settings.voice = '';
          saveStored(STORAGE_SETTINGS, state.settings);
        }
        await refreshVoices();
      } catch (err) {
        el('voices-status').textContent = String(err.message || err);
        remove.disabled = false;
      }
    });
    row.appendChild(remove);

    list.appendChild(row);
  }
}

async function refreshVoices() {
  const status = el('voices-status');
  status.textContent = 'loading…';
  try {
    const data = await api('/api/voices', { settings: settingsForRequest() });
    renderVoiceList(data.voices);
    renderVoiceOptions(data.voices);
    status.textContent = data.voices.length + ' voice(s)';
  } catch (err) {
    status.textContent = String(err.message || err);
    renderVoiceList([]);
  }
}

// Decode whatever the browser can read (wav, mp3, m4a, ogg…) and re-encode it
// as mono WAV, because that is what the TTS and ASR servers accept.
async function loadVoiceSample(file) {
  const info = el('v-file-info');
  const transcript = el('v-transcript');
  const register = el('btn-register');

  state.sampleWav = null;
  register.disabled = true;
  transcript.value = '';
  transcript.disabled = true;
  transcript.placeholder = 'transcribing… (double check)';
  info.textContent = 'decoding…';

  try {
    if (!audio.playCtx) audio.playCtx = new AudioContext();
    const raw = await file.arrayBuffer();
    const decoded = await audio.playCtx.decodeAudioData(raw);

    // Mix to mono: cloning voices from one channel of a stereo recording loses
    // half the signal when the take is hard-panned.
    const length = decoded.length;
    const mono = new Float32Array(length);
    for (let ch = 0; ch < decoded.numberOfChannels; ch++) {
      const data = decoded.getChannelData(ch);
      for (let i = 0; i < length; i++) mono[i] += data[i] / decoded.numberOfChannels;
    }

    const pcm = resample(mono, decoded.sampleRate, VOICE_SAMPLE_RATE);
    state.sampleWav = encodeWav(pcm, VOICE_SAMPLE_RATE);

    const seconds = pcm.length / VOICE_SAMPLE_RATE;
    const limit = state.config.limits.max_voice_sample_seconds;
    info.textContent = seconds.toFixed(1) + ' s, mono, ' + VOICE_SAMPLE_RATE + ' Hz';
    if (seconds > limit) {
      info.textContent += ' — too long, the limit is ' + limit + ' s';
      state.sampleWav = null;
      transcript.placeholder = 'pick a shorter clip';
      return;
    }

    // Suggest the file name as the voice name, cleaned to what the server
    // accepts, but only when the user has not typed one already.
    const nameField = el('v-name');
    if (!nameField.value.trim()) {
      const stem = file.name.replace(/\.[^.]+$/, '').replace(/[^A-Za-z0-9._-]/g, '_');
      nameField.value = stem.slice(0, 64).replace(/^[^A-Za-z0-9]+/, '');
    }

    await transcribeSample();
  } catch (err) {
    info.textContent = 'could not read this file: ' + (err.message || err);
    transcript.placeholder = 'no transcript';
  }
}

async function transcribeSample() {
  const transcript = el('v-transcript');
  const register = el('btn-register');
  if (!state.sampleWav) return;

  transcript.disabled = true;
  transcript.placeholder = 'transcribing… (double check)';

  try {
    const result = await api('/api/stt', {
      audio_b64: toBase64(new Uint8Array(state.sampleWav)),
      purpose: 'voice_sample',
      settings: settingsForRequest(),
    });
    transcript.value = result.text || '';
    transcript.placeholder = result.text
      ? 'check this against the clip'
      : 'nothing recognised — type the transcript yourself';
  } catch (err) {
    transcript.value = '';
    transcript.placeholder = 'transcription failed: ' + (err.message || err) + ' — type it yourself';
  } finally {
    // Unlocked either way: a failed transcription still lets the user type one.
    transcript.disabled = false;
    register.disabled = false;
  }
}

async function registerVoice() {
  const status = el('register-status');
  const button = el('btn-register');
  const name = el('v-name').value.trim();
  const refText = el('v-transcript').value.trim();

  if (!state.sampleWav) { status.textContent = 'choose an audio file first'; return; }
  if (!name) { status.textContent = 'give the voice a name'; return; }
  if (!refText) { status.textContent = 'a transcript is required'; return; }

  button.disabled = true;
  status.textContent = 'registering…';
  try {
    await api('/api/voices/register', {
      name: name,
      ref_text: refText,
      replace: el('v-replace').checked,
      audio_b64: toBase64(new Uint8Array(state.sampleWav)),
      settings: settingsForRequest(),
    });
    status.textContent = 'registered';

    // Select it straight away: registering a voice you then have to go and
    // pick from a menu is one step too many.
    state.settings.voice = name;
    saveStored(STORAGE_SETTINGS, state.settings);
    fillForm();

    el('v-file').value = '';
    el('v-name').value = '';
    el('v-transcript').value = '';
    el('v-transcript').disabled = true;
    el('v-file-info').textContent = '';
    el('v-replace').checked = false;
    state.sampleWav = null;
    await refreshVoices();
  } catch (err) {
    status.textContent = String(err.message || err);
  } finally {
    button.disabled = !state.sampleWav;
  }
}

/* ---------------------------------------------------------------------------
 * Wiring
 * ------------------------------------------------------------------------- */

function openPanel(id) {
  el(id).hidden = false;
  if (id === 'voices') refreshVoices();
}

function closePanel(id) {
  readForm();
  el(id).hidden = true;
}

function wire() {
  el('btn-mic').addEventListener('click', toggleMic);
  el('btn-stop').addEventListener('click', () => {
    stopEverything();
    // Discard anything half-captured, so the next thing said is a clean
    // turn. Barge-in deliberately does not do this: it keeps the pre-roll,
    // which is the start of what the user is saying.
    resetVad();
  });

  el('text-form').addEventListener('submit', (event) => {
    event.preventDefault();
    const input = el('text-input');
    const text = input.value.trim();
    if (!text) return;
    input.value = '';
    // Typing implies you want to hear the reply, so make sure playback is
    // allowed: this click is the user gesture iOS requires.
    if (!audio.playCtx) audio.playCtx = new AudioContext();
    resumeContexts();
    sendTypedText(text);
  });

  el('btn-settings').addEventListener('click', () => openPanel('settings'));
  el('settings-close').addEventListener('click', () => closePanel('settings'));
  el('btn-voices').addEventListener('click', () => openPanel('voices'));
  el('voices-close').addEventListener('click', () => closePanel('voices'));

  // Persist on every change so a closed tab never loses a setting.
  for (const field of settingFields().concat(localFields())) {
    field.addEventListener('change', readForm);
  }

  el('btn-health').addEventListener('click', async () => {
    readForm();
    const out = el('health-result');
    out.textContent = 'checking…';
    try {
      const data = await api('/api/health', { settings: settingsForRequest() });
      out.textContent = ['llm', 'stt', 'tts']
        .map((k) => k + ': ' + (data.checks[k].ok ? data.checks[k].ms + ' ms' : 'FAILED'))
        .join('   ');
      const failed = ['llm', 'stt', 'tts'].filter((k) => !data.checks[k].ok);
      for (const key of failed) note(key + ' — ' + data.checks[key].error, true);
    } catch (err) {
      out.textContent = String(err.message || err);
    }
  });

  el('btn-reset').addEventListener('click', async () => {
    // Stop first: clearing the transcript while a reply is still arriving
    // would leave it talking into an empty screen.
    stopEverything();
    try {
      await api('/api/reset', {});
      el('transcript').replaceChildren();
      note('conversation cleared');
    } catch (err) {
      note(String(err.message || err), true);
    }
  });

  el('btn-defaults').addEventListener('click', () => {
    if (!window.confirm('Discard your settings and reload the server defaults?')) return;
    state.settings = Object.assign({}, state.config.defaults);
    saveStored(STORAGE_SETTINGS, state.settings);
    state.local = Object.assign({}, DEFAULT_LOCAL);
    saveStored(STORAGE_LOCAL, state.local);
    fillForm();
    refreshVoices();
  });

  el('btn-refresh-voices').addEventListener('click', refreshVoices);

  el('v-file').addEventListener('change', (event) => {
    const file = event.target.files && event.target.files[0];
    if (file) loadVoiceSample(file);
  });

  el('btn-register').addEventListener('click', registerVoice);

  el('login-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const error = el('login-error');
    error.textContent = '';
    try {
      await api('/api/login', { token: el('login-token').value });
      el('login-token').value = '';
      el('gate').hidden = true;
      await boot();
    } catch (err) {
      error.textContent = String(err.message || err);
    }
  });

  // Stopping is the one action worth a keyboard shortcut.
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
      if (!el('settings').hidden) { closePanel('settings'); return; }
      if (!el('voices').hidden) { closePanel('voices'); return; }
      stopEverything();
    }
  });

  // Leaving the tab should not leave audio playing into an empty room.
  document.addEventListener('visibilitychange', () => {
    if (document.hidden && replyActive()) stopEverything();
  });
}

async function boot() {
  let config;
  try {
    const resp = await fetch('/api/config');
    if (resp.status === 401) { el('gate').hidden = false; return; }
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    config = await resp.json();
  } catch (err) {
    el('app').hidden = false;
    setStatus('cannot reach the LangPal server: ' + (err.message || err), 'error');
    return;
  }

  state.config = config;
  state.settings = loadStored(STORAGE_SETTINGS, config.defaults);
  state.local = loadStored(STORAGE_LOCAL, DEFAULT_LOCAL);

  populateChoices();
  fillForm();
  el('app').hidden = false;
  setStatus('press Start listening');

  if (!window.isSecureContext) {
    note('This page is not a secure context, so the browser will not allow '
       + 'microphone access. Serve LangPal over HTTPS, or open it from '
       + 'localhost. Typing still works.', true);
  }

  refreshVoices();
}

wire();
boot();
