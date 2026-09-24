#!/usr/bin/env python3
"""LangPal -- voice chat against a local, OpenAI-compatible model stack.

LangPal is a thin, auditable broker between a browser and three OpenAI-compatible
HTTP endpoints that you already run yourself (typically behind one llama-swap):

    LLM  ->  POST {llm_url}/v1/chat/completions      (streaming)
    STT  ->  POST {stt_url}/v1/audio/transcriptions  (multipart, 16 kHz mono WAV)
    TTS  ->  POST {tts_url}/v1/audio/speech          (JSON in, WAV out)
             GET/POST/DELETE {tts_url}/v1/audio/voices  (voice cloning registry)

Design rules this file sticks to, deliberately:

  * Standard library only. No pip install, ever.
  * LangPal never writes to or deletes from disk. It reads its own web/ assets,
    an optional .env file and the voices_default/ folder at startup, and reads
    nothing else afterwards. Audio, conversation history and uploaded voice
    samples live in RAM only and die with the process.
  * Per-user settings live in the browser (localStorage) and arrive with each
    request. Everything the browser sends is re-validated here before use.
  * Secrets (upstream API keys, the access token) are server-side only and are
    never echoed back to the browser.

Run it:

    python langpal.py                      # http://127.0.0.1:8080
    python langpal.py --host 10.66.66.2 --port 8080
    python langpal.py --cert cert.pem --key key.pem   # needed for remote mics

Microphone note: browsers only expose getUserMedia in a "secure context".
http://localhost works; http://10.66.66.2:8080 does not. For remote devices you
must serve HTTPS (--cert/--key) or whitelist the origin per device. The cert
needs a subjectAltName covering the IP you actually browse to -- see
docs/hosting.md for the exact openssl invocation.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hmac
import io
import json
import os
import re
import secrets
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(HERE, "web")


# ---------------------------------------------------------------------------
# .env loading (read-only)
# ---------------------------------------------------------------------------

def _load_env_file(path: str) -> None:
    """Merge KEY=VALUE lines from `path` into os.environ.

    Real environment variables always win, so an exported value overrides the
    file. Malformed lines are skipped silently -- this is a convenience loader,
    not something to depend on for correctness.
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        # Strip one layer of matching quotes, the usual .env convention.
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        if key and key not in os.environ:
            os.environ[key] = val


# .env.local first so it takes precedence over a shared .env.
for _name in (".env.local", ".env"):
    _load_env_file(os.path.join(HERE, _name))


def log(message: str) -> None:
    """Print to stdout and flush.

    Redirecting stdout to a file makes it block-buffered, which would otherwise
    hide startup warnings and the voice bootstrap's progress until exit.
    """
    print(message, flush=True)


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name) or default)
    except ValueError:
        return default


def _env_flag(name: str, default: bool = False) -> bool:
    raw = _env(name).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


# ===========================================================================
# CONFIGURATION
#
# Every value below can be overridden with an environment variable, or via the
# .env.local file next to this script. Values marked "client-settable" are
# defaults only -- the browser may override them per device, and whatever it
# sends is re-validated by validate_settings() before it is used.
# ===========================================================================

# --- Where LangPal itself listens -----------------------------------------

# Bind address. 127.0.0.1 keeps it on this machine only. Set it to your
# WireGuard address (e.g. 10.66.66.2) to let your own peers reach it.
BIND_HOST = _env("LANGPAL_HOST", "127.0.0.1")
BIND_PORT = _env_int("LANGPAL_PORT", 8080)

# Optional TLS. Browsers refuse microphone access over plain http:// to
# anything other than localhost, so remote devices need these two files.
# The certificate MUST carry a subjectAltName for the address you browse to;
# a bare "CN=localhost" cert is rejected by every current browser.
#   LANGPAL_TLS_CERT=C:\Users\you\LangPal\cert.pem
#   LANGPAL_TLS_KEY=C:\Users\you\LangPal\key.pem
TLS_CERT_FILE = _env("LANGPAL_TLS_CERT")
TLS_KEY_FILE = _env("LANGPAL_TLS_KEY")

# Preferred TLS key-exchange groups, strongest first. The hybrid post-quantum
# group X25519MLKEM768 only exists in OpenSSL 3.5+, and naming it requires
# Python 3.14's SSLContext.set_groups(). On anything older this list is ignored
# and OpenSSL's own defaults (X25519/P-256) apply -- see harden_tls_context().
TLS_GROUPS = "X25519MLKEM768:X25519:P-256"

# Optional shared password. Empty = no authentication at all, which is only
# sane when BIND_HOST is loopback or a private tunnel address you control.
ACCESS_TOKEN = _env("LANGPAL_TOKEN")

# --- Upstream endpoints ----------------------------------------------------
#
# These are BASE urls, not full endpoint paths. Anything you paste gets run
# through normalize_base_url(), which fixes up the usual copy/paste damage:
# a missing scheme, a trailing slash, a trailing /v1, or a full endpoint path
# such as .../v1/audio/speech. A meaningful path prefix is preserved, which
# matters for llama-swap's /upstream/<model> routing (see below).
#
# llama-swap routing caveat, verified against a live instance:
#   llama-swap picks the backend from the "model" field in the request body.
#   GET /v1/audio/voices has no body, so llama-swap answers 404 "no model id
#   could be identified". Point the TTS base url either straight at the
#   omnivoice container (http://127.0.0.1:8123) or at llama-swap's explicit
#   per-model route (http://10.66.66.2:11435/upstream/omnivoice). Both work;
#   the second also triggers the model load.
DEFAULT_LLM_BASE_URL = _env("LANGPAL_LLM_URL", "http://127.0.0.1:11435")
DEFAULT_STT_BASE_URL = _env("LANGPAL_STT_URL", "http://127.0.0.1:11435")
DEFAULT_TTS_BASE_URL = _env("LANGPAL_TTS_URL", "http://127.0.0.1:8123")

# Model ids as your server knows them. For llama-swap these are the keys in
# its config; it uses them to decide which backend to spin up.
DEFAULT_LLM_MODEL = _env("LANGPAL_LLM_MODEL", "gemma4-26b-a4b")
DEFAULT_STT_MODEL = _env("LANGPAL_STT_MODEL", "qwen3-asr")
DEFAULT_TTS_MODEL = _env("LANGPAL_TTS_MODEL", "omnivoice")

# Bearer tokens for upstreams that need one. Server-side only: these are never
# sent to the browser and cannot be set from it. Local llama.cpp needs none.
LLM_API_KEY = _env("LANGPAL_LLM_API_KEY")
STT_API_KEY = _env("LANGPAL_STT_API_KEY")
TTS_API_KEY = _env("LANGPAL_TTS_API_KEY")

# --- Upstream API paths ----------------------------------------------------
#
# Appended to the normalized base url. Change these only if your server puts
# the OpenAI-compatible routes somewhere other than /v1/...
LLM_CHAT_PATH = "/v1/chat/completions"
LLM_MODELS_PATH = "/v1/models"
STT_TRANSCRIBE_PATH = "/v1/audio/transcriptions"
TTS_SPEECH_PATH = "/v1/audio/speech"
TTS_VOICES_PATH = "/v1/audio/voices"
HEALTH_PATH = "/health"

# Path fragments normalize_base_url() strips off the end of a pasted URL.
# Longest-first so that "/v1/audio/speech" is matched before "/v1".
URL_STRIP_SUFFIXES = (
    "/v1/audio/transcriptions",
    "/v1/audio/translations",
    "/v1/chat/completions",
    "/v1/audio/speech",
    "/v1/audio/voices",
    "/v1/completions",
    "/v1/models",
    "/api/v1",
    "/openai/v1",
    "/health",
    "/api",
    "/v1",
)

# --- Default voice bootstrap ----------------------------------------------
#
# On startup LangPal registers every WAV in this folder as a cloned voice, so
# users get a usable voice list without having to record anything. Each
# sample.wav is paired with sample.txt holding the exact transcript of the
# clip; if the .txt is missing the clip is transcribed with the STT model
# instead. Nothing is ever written back -- the .txt files are read-only inputs.
#
# Runs in a background thread so a cold model load never delays the server.
DEFAULT_VOICES_DIR = _env("LANGPAL_VOICES_DIR") or os.path.join(HERE, "voices_default")
# Recognised as audio, but not usable here: the TTS registry takes WAV and the
# standard library cannot decode these. Used only to warn instead of ignoring.
AUDIO_EXTENSIONS = (".mp3", ".m4a", ".aac", ".ogg", ".opus", ".flac", ".wma", ".webm")
REGISTER_DEFAULT_VOICES = _env_flag("LANGPAL_REGISTER_DEFAULT_VOICES", True)

# --- Generation defaults (client-settable) ---------------------------------

# None means "send nothing and let the inference server's own defaults apply".
# That is the documented behaviour for temperature, top_p, max_tokens and seed.
DEFAULT_TEMPERATURE = None
DEFAULT_TOP_P = None
DEFAULT_MAX_TOKENS = None
DEFAULT_SEED = None

# Reasoning control. "" sends nothing. Otherwise sent as OpenAI's
# reasoning_effort field. DEFAULT_ENABLE_THINKING, when not None, is sent as
# chat_template_kwargs.enable_thinking, which is what llama.cpp's --jinja
# templates look at for Qwen/Gemma-style thinking toggles.
DEFAULT_REASONING_EFFORT = ""            # "" | none | low | medium | high
DEFAULT_ENABLE_THINKING = None           # None | True | False
REASONING_EFFORT_VALUES = ("", "none", "low", "medium", "high")

# Any <think>...</think> the model emits anyway is stripped before it reaches
# the screen or the TTS queue.
THINK_OPEN_RE = re.compile(r"<(think|thinking|reasoning)>", re.I)
THINK_CLOSE_RE = re.compile(r"</(think|thinking|reasoning)>", re.I)

DEFAULT_SYSTEM_PROMPT = _env("LANGPAL_SYSTEM_PROMPT") or (
    "You are LangPal, a warm and patient conversation partner for language "
    "practice. Your replies are spoken aloud, so keep them short -- two or "
    "three sentences -- and write them the way a person actually talks. "
    "Reply in the same language the user is speaking unless they ask for "
    "another. Never use markdown, bullet points, emoji, asterisks or stage "
    "directions. If the user makes a mistake, answer naturally first, then "
    "offer one brief correction."
)

# How many previous messages (user + assistant, counted together) to replay as
# context. 0 means no history at all; the system prompt is always included.
DEFAULT_HISTORY_MESSAGES = _env_int("LANGPAL_HISTORY_MESSAGES", 20)
MAX_HISTORY_MESSAGES = 200

# --- Language (client-settable) --------------------------------------------
#
# Qwen3-ASR and OmniVoice both want a language *name* in English ("Turkish",
# not "tr" -- an ISO code is silently ignored by Qwen3-ASR, which then falls
# back to auto-detect). One setting drives both by default:
#
#   "auto"  -> nothing is sent to STT; Qwen3-ASR detects the language and
#              reports it in its output prefix, and that detected name is
#              handed to TTS so the reply is spoken in the same language.
#   a name  -> forced on STT and used for TTS.
#
# Auto-detect is unreliable on short clips (a two-second Turkish sample was
# variously labelled English and Arabic in testing), so pinning the language is
# recommended once you know what you are practising.
#
# stt_language / tts_language break the two apart when you want, say, Turkish
# input answered in English.
DEFAULT_LANGUAGE = _env("LANGPAL_LANGUAGE", "auto")
DEFAULT_STT_LANGUAGE_OVERRIDE = ""
DEFAULT_TTS_LANGUAGE_OVERRIDE = ""

# Offered in the UI dropdown. Free text is accepted too -- OmniVoice covers
# ~600 languages and Qwen3-ASR ~50, so this list is a convenience, not a limit.
LANGUAGE_CHOICES = (
    "Turkish", "English", "French", "German", "Spanish", "Italian",
    "Portuguese", "Dutch", "Russian", "Polish", "Ukrainian", "Czech",
    "Romanian", "Hungarian", "Greek", "Swedish", "Norwegian", "Danish",
    "Finnish", "Arabic", "Persian", "Hebrew", "Hindi", "Urdu", "Bengali",
    "Japanese", "Korean", "Chinese", "Vietnamese", "Thai", "Indonesian",
)

# A language name must look like a name: letters, spaces, hyphens, apostrophes.
LANGUAGE_RE = re.compile(r"^[A-Za-z\u00C0-\u024F][A-Za-z\u00C0-\u024F '\-]{0,31}$")

# --- Qwen3-ASR output parsing ----------------------------------------------
#
# Qwen3-ASR emits a language-identification prefix that llama.cpp passes
# through verbatim (ggml-org/llama.cpp#26749). A real response looks like:
#
#   {"type":"transcript.text.done",
#    "text":"language Turkish<asr_text>Merhaba, bu bir testtir."}
#
# So we split on the tag, keep the detected language, and hand the clean text
# to the LLM. Servers that already strip it pass straight through untouched.
ASR_TEXT_TAG = "<asr_text>"
ASR_LANG_PREFIX_RE = re.compile(r"^\s*language\s+(.+?)\s*$", re.I)
ASR_STRAY_TAG_RE = re.compile(r"</?asr_text>", re.I)

# --- TTS -------------------------------------------------------------------

DEFAULT_VOICE = _env("LANGPAL_VOICE")     # "" = server's built-in default voice
DEFAULT_TTS_SEED = None                   # None = server default (varies per call)

# Registered voice names. Kept tight because the name is interpolated into a
# URL path on DELETE and into a JSON body on register.
VOICE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# --- Sentence chunking -----------------------------------------------------
#
# The LLM is streamed token by token, split into sentences here, and each
# sentence is synthesised separately so audio starts before the reply is done.
SENTENCE_TERMINATORS = ".!?\u2026\u3002\uff01\uff1f\uff1b;:\n"
MIN_SENTENCE_CHARS = 12      # below this, keep accumulating (avoids "Ok." clips)
# CJK scripts carry far more meaning per character, so "今日はいい天気ですね。"
# is a complete sentence at 11 characters. Judging it by the Latin minimum
# would hold every short Japanese or Chinese sentence back forever.
MIN_SENTENCE_CHARS_CJK = 5
MAX_SENTENCE_CHARS = 320     # hard flush, at a space if we can find one
# A '.' closing one of these is not a sentence end, so "Dr. Smith" stays whole.
SENTENCE_ABBREVIATIONS = (
    "mr", "mrs", "ms", "dr", "prof", "st", "sr", "jr", "vs", "etc", "e.g",
    "i.e", "no", "fig", "approx", "dk", "sn", "bkz",
)

# --- Limits ----------------------------------------------------------------
#
# These keep a buggy or hostile client from exhausting RAM. Audio travels as
# base64 inside JSON, which costs ~33% over the raw bytes; the caps below
# account for that.
MAX_REQUEST_BYTES = 12 * 1024 * 1024     # any single request body
MAX_UTTERANCE_SECONDS = 90               # one spoken turn
MAX_VOICE_SAMPLE_SECONDS = 120           # a voice-cloning reference clip
MAX_USER_TEXT_CHARS = 8000
MAX_TTS_TEXT_CHARS = 2000
MAX_SYSTEM_PROMPT_CHARS = 8000
MAX_REF_TEXT_CHARS = 2000
MAX_URL_CHARS = 512
MAX_SSE_LINE_BYTES = 256 * 1024          # one upstream SSE line
MAX_ERROR_ECHO_CHARS = 400               # how much upstream error text we relay

# Cancelled turn ids remembered per session. Only needs to outlast requests
# already queued upstream when Stop was pressed, which is a handful.
CANCELLED_TURNS = 32

# A turn id is minted by the browser, one per reply.
TURN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

# Concurrent sessions kept in RAM; the least recently used is dropped past this.
MAX_SESSIONS = 32

# Upstream timeouts, seconds. Generous because a cold llama-swap has to load a
# model off disk before it answers the first request.
LLM_TIMEOUT = 600
STT_TIMEOUT = 600
TTS_TIMEOUT = 600
VOICES_TIMEOUT = 600
HEALTH_TIMEOUT = 10

# How often to send an SSE comment while waiting on the LLM, so idle-socket
# timeouts do not kill a slow first token.
SSE_HEARTBEAT_SECONDS = 15

# Per-connection socket timeout. Must exceed SSE_HEARTBEAT_SECONDS.
CONNECTION_TIMEOUT = 120

# Static assets, loaded into RAM at startup. Serving from a fixed dict rather
# than the filesystem means path traversal is not expressible.
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/capture-worklet.js": ("capture-worklet.js", "application/javascript; charset=utf-8"),
}

# Sent on every response. The CSP is strict: no inline script, no external
# origins, so a reflected string cannot become executable.
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; media-src 'self' blob:; connect-src 'self'; "
        "form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
}


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

# Raised when the client has gone away mid-response. ssl.SSLError is in here
# because a TLS socket whose peer vanished raises ssl.SSLEOFError, which is not
# a BrokenPipeError and so slipped through the old handling entirely.
CONNECTION_ERRORS = (BrokenPipeError, ConnectionResetError, ssl.SSLError,
                     TimeoutError)


class BadRequest(Exception):
    """Client sent something invalid. Becomes a 400."""


class TooLarge(BadRequest):
    """Request body exceeds MAX_REQUEST_BYTES. Becomes a 413."""


class UpstreamError(Exception):
    """An upstream endpoint failed or was unreachable. Becomes a 502.

    `status` is the upstream HTTP status when there was one, else None. It lets
    callers distinguish "this route does not exist" from "the box is down".
    """

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class Aborted(Exception):
    """The session's stop button was pressed; drop this work."""


# ---------------------------------------------------------------------------
# URL normalisation
# ---------------------------------------------------------------------------

def normalize_base_url(raw: str, *, field: str = "url") -> str:
    """Turn whatever the user pasted into a usable scheme://host[:port][/prefix].

    Handles the cases people actually paste:

        10.66.66.2:11435                     -> http://10.66.66.2:11435
        localhost:8123/                      -> http://localhost:8123
        http://127.0.0.1:8123/v1             -> http://127.0.0.1:8123
        HTTPS://Box:443/v1/audio/speech/     -> https://box
        http://h:11435/upstream/omnivoice/v1 -> http://h:11435/upstream/omnivoice

    The last case is why only known API suffixes are stripped rather than the
    whole path: llama-swap's /upstream/<model> prefix has to survive.

    Raises BadRequest on anything unusable.
    """
    raw = (raw or "").strip()
    if not raw:
        raise BadRequest("%s is empty" % field)
    if len(raw) > MAX_URL_CHARS:
        raise BadRequest("%s is too long" % field)
    # Control characters or spaces in a URL would let a crafted value smuggle
    # extra headers into the upstream request.
    if any(ch in raw for ch in "\r\n\t\0 "):
        raise BadRequest("%s contains whitespace or control characters" % field)

    # No scheme, or a bare "host:port" that urlsplit would read as scheme:path.
    if "//" not in raw:
        raw = "http://" + raw.lstrip("/")

    try:
        parts = urllib.parse.urlsplit(raw)
    except ValueError as exc:
        raise BadRequest("%s is not a valid URL (%s)" % (field, exc)) from exc

    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise BadRequest("%s must be http or https, got %r" % (field, scheme))
    # Credentials in the URL would end up in logs and error messages.
    if parts.username or parts.password:
        raise BadRequest("%s must not contain a username or password" % field)

    try:
        host = parts.hostname       # already lowercased by urlsplit
        port = parts.port           # raises ValueError on a malformed port
    except ValueError as exc:
        raise BadRequest("%s has an invalid port (%s)" % (field, exc)) from exc
    if not host:
        raise BadRequest("%s has no host" % field)

    # IPv6 literals need their brackets back.
    netloc = ("[%s]" % host) if ":" in host else host
    if port is not None:
        if not 1 <= port <= 65535:
            raise BadRequest("%s has an invalid port" % field)
        # Drop the port when it is the scheme default, purely for tidiness.
        is_default = (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
        if not is_default:
            netloc = "%s:%d" % (netloc, port)

    # Query strings and fragments are meaningless on a base URL; drop them.
    path = parts.path.rstrip("/")
    lowered = path.lower()
    # Strip repeatedly: ".../upstream/x/v1/models" needs two passes to become
    # ".../upstream/x".
    changed = True
    while changed and path:
        changed = False
        for suffix in URL_STRIP_SUFFIXES:
            if lowered.endswith(suffix):
                path = path[: -len(suffix)].rstrip("/")
                lowered = path.lower()
                changed = True
                break

    return "%s://%s%s" % (scheme, netloc, path)


# Normalise the configured bases once, at import. This means /api/config hands
# the UI exactly the URLs that will be used, and a typo in .env.local fails
# loudly at startup instead of on the first request of a conversation.
def _normalize_configured(value, name):
    try:
        return normalize_base_url(value, field=name)
    except BadRequest as exc:
        raise SystemExit("LangPal: %s" % exc)


DEFAULT_LLM_BASE_URL = _normalize_configured(DEFAULT_LLM_BASE_URL, "LANGPAL_LLM_URL")
DEFAULT_STT_BASE_URL = _normalize_configured(DEFAULT_STT_BASE_URL, "LANGPAL_STT_URL")
DEFAULT_TTS_BASE_URL = _normalize_configured(DEFAULT_TTS_BASE_URL, "LANGPAL_TTS_URL")


# ---------------------------------------------------------------------------
# Settings: defaults, and validation of whatever the browser sends
# ---------------------------------------------------------------------------

def default_settings() -> dict:
    """The server-side defaults, as handed to a browser with empty storage."""
    return {
        "llm_url": DEFAULT_LLM_BASE_URL,
        "llm_model": DEFAULT_LLM_MODEL,
        "stt_url": DEFAULT_STT_BASE_URL,
        "stt_model": DEFAULT_STT_MODEL,
        "tts_url": DEFAULT_TTS_BASE_URL,
        "tts_model": DEFAULT_TTS_MODEL,
        "temperature": DEFAULT_TEMPERATURE,
        "top_p": DEFAULT_TOP_P,
        "max_tokens": DEFAULT_MAX_TOKENS,
        "seed": DEFAULT_SEED,
        "reasoning_effort": DEFAULT_REASONING_EFFORT,
        "enable_thinking": DEFAULT_ENABLE_THINKING,
        "system_prompt": DEFAULT_SYSTEM_PROMPT,
        "history_messages": DEFAULT_HISTORY_MESSAGES,
        "language": DEFAULT_LANGUAGE,
        "stt_language": DEFAULT_STT_LANGUAGE_OVERRIDE,
        "tts_language": DEFAULT_TTS_LANGUAGE_OVERRIDE,
        "voice": DEFAULT_VOICE,
        "tts_seed": DEFAULT_TTS_SEED,
    }


def _opt_number(raw, field, low, high, *, integer=False):
    """Validate an optional number. None / "" mean "unset -- omit it upstream"."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool):                      # bool is an int subclass
        raise BadRequest("%s must be a number" % field)
    if not isinstance(raw, (int, float)):
        raise BadRequest("%s must be a number" % field)
    if raw != raw or raw in (float("inf"), float("-inf")):   # NaN / infinities
        raise BadRequest("%s must be finite" % field)
    if not low <= raw <= high:
        raise BadRequest("%s must be between %s and %s" % (field, low, high))
    return int(raw) if integer else float(raw)


def _opt_bool(raw, field):
    if raw is None or raw == "":
        return None
    if not isinstance(raw, bool):
        raise BadRequest("%s must be true or false" % field)
    return raw


def normalize_language_name(name: str) -> str:
    """Capitalise a language name the way the models expect.

    Qwen3-ASR and OmniVoice are given the English name, and they key off its
    usual written form: "French" is recognised where "french" and "FRENCH" are
    not reliably. Each word is capitalised, so "brazilian portuguese" becomes
    "Brazilian Portuguese".

    Hyphenated names keep their internal capitalisation as typed, which is the
    conservative choice -- guessing at "Sino-Tibetan" versus "Sino-tibetan"
    would be wrong as often as it was right.
    """
    return " ".join(word[:1].upper() + word[1:].lower()
                    for word in name.split() if word)


def _language(raw, field, *, allow_auto=False, allow_empty=False):
    raw = (raw or "").strip()
    if not raw:
        if allow_empty:
            return ""
        raise BadRequest("%s is empty" % field)
    if allow_auto and raw.lower() == "auto":
        return "auto"
    if not LANGUAGE_RE.match(raw):
        raise BadRequest("%s must be a language name in English, such as "
                         "'English' or 'French'" % field)
    # Normalised after validation, so the models always receive the form they
    # recognise however the user typed it.
    return normalize_language_name(raw)


def validate_settings(raw) -> dict:
    """Merge client-supplied settings over the defaults, validating every field.

    Unknown keys are dropped rather than rejected, so an older browser tab with
    stale localStorage keeps working after an upgrade.
    """
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise BadRequest("settings must be an object")

    out = default_settings()

    for key, fallback in (
        ("llm_url", DEFAULT_LLM_BASE_URL),
        ("stt_url", DEFAULT_STT_BASE_URL),
        ("tts_url", DEFAULT_TTS_BASE_URL),
    ):
        value = raw.get(key)
        out[key] = normalize_base_url(
            value if isinstance(value, str) and value.strip() else fallback,
            field=key,
        )

    for key in ("llm_model", "stt_model", "tts_model"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            value = value.strip()
            if len(value) > 128 or any(c in value for c in "\r\n\0"):
                raise BadRequest("%s is invalid" % key)
            out[key] = value

    out["temperature"] = _opt_number(
        raw.get("temperature", out["temperature"]), "temperature", 0, 2)
    out["top_p"] = _opt_number(raw.get("top_p", out["top_p"]), "top_p", 0, 1)
    out["max_tokens"] = _opt_number(
        raw.get("max_tokens", out["max_tokens"]), "max_tokens", 1, 32768, integer=True)
    out["seed"] = _opt_number(raw.get("seed", out["seed"]), "seed", 0, 2 ** 31 - 1, integer=True)
    out["tts_seed"] = _opt_number(
        raw.get("tts_seed", out["tts_seed"]), "tts_seed", 0, 2 ** 31 - 1, integer=True)

    effort = raw.get("reasoning_effort", out["reasoning_effort"])
    effort = (effort or "").strip().lower()
    if effort not in REASONING_EFFORT_VALUES:
        raise BadRequest("reasoning_effort must be one of %s" % (REASONING_EFFORT_VALUES,))
    out["reasoning_effort"] = effort

    out["enable_thinking"] = _opt_bool(
        raw.get("enable_thinking", out["enable_thinking"]), "enable_thinking")

    if "system_prompt" in raw:
        prompt = raw.get("system_prompt")
        if not isinstance(prompt, str):
            raise BadRequest("system_prompt must be text")
        if len(prompt) > MAX_SYSTEM_PROMPT_CHARS:
            raise BadRequest("system_prompt is longer than %d characters" % MAX_SYSTEM_PROMPT_CHARS)
        out["system_prompt"] = prompt.strip()

    history = _opt_number(
        raw.get("history_messages", out["history_messages"]),
        "history_messages", 0, MAX_HISTORY_MESSAGES, integer=True)
    out["history_messages"] = DEFAULT_HISTORY_MESSAGES if history is None else history

    out["language"] = _language(
        raw.get("language", out["language"]) or "auto", "language", allow_auto=True)
    out["stt_language"] = _language(
        raw.get("stt_language", out["stt_language"]), "stt_language",
        allow_auto=True, allow_empty=True)
    out["tts_language"] = _language(
        raw.get("tts_language", out["tts_language"]), "tts_language", allow_empty=True)

    voice = raw.get("voice", out["voice"])
    voice = (voice or "").strip()
    if voice and not VOICE_NAME_RE.match(voice):
        raise BadRequest("voice name may only contain letters, digits, . _ and -")
    out["voice"] = voice

    return out


def stt_language_for(settings: dict) -> str:
    """The language to force on STT, or "" for auto-detect."""
    override = settings.get("stt_language") or ""
    chosen = override or settings.get("language") or "auto"
    return "" if chosen.lower() == "auto" else chosen


def tts_language_for(settings: dict, detected: str = "") -> str:
    """The language to speak in.

    Priority: explicit TTS override > pinned shared language > whatever
    Qwen3-ASR detected on this turn > English as a last resort.
    """
    override = settings.get("tts_language") or ""
    if override:
        return override
    shared = settings.get("language") or "auto"
    if shared.lower() != "auto":
        return shared
    if detected and LANGUAGE_RE.match(detected):
        return detected
    return "English"


# ---------------------------------------------------------------------------
# Upstream HTTP helpers
# ---------------------------------------------------------------------------

class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Refuse redirects outright.

    Upstream URLs are user-supplied, so following a redirect would let a
    misconfigured or compromised endpoint bounce us somewhere unexpected while
    still carrying the Authorization header.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise UpstreamError(
            "upstream returned a %d redirect, which LangPal refuses to follow" % code)


_OPENER = urllib.request.build_opener(_NoRedirects)


def _safe_url(url: str) -> str:
    """Strip any query string before a URL goes into an error message."""
    return url.split("?", 1)[0]


def _upstream_request(url, *, data=None, method=None, headers=None, api_key="", timeout=60):
    """Issue one upstream request and return the open response object.

    The caller owns the response and must close it. Errors are normalised into
    UpstreamError carrying a short, truncated excerpt of the upstream body --
    long enough to debug, short enough not to dump a model's worth of text.
    """
    hdrs = dict(headers or {})
    if api_key:
        hdrs["Authorization"] = "Bearer " + api_key
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        return _OPENER.open(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        try:
            # Stripped: upstream error bodies often end in a newline, which
            # would break a sentence when this is embedded in a longer message.
            body = exc.read().decode("utf-8", "replace").strip()
        except Exception:                      # noqa: BLE001 - body is best effort
            body = ""
        finally:
            exc.close()
        raise UpstreamError("HTTP %d from %s: %s" % (
            exc.code, _safe_url(url), body[:MAX_ERROR_ECHO_CHARS]),
            status=exc.code) from exc
    except urllib.error.URLError as exc:
        raise UpstreamError("cannot reach %s (%s)" % (_safe_url(url), exc.reason)) from exc
    except (socket.timeout, TimeoutError) as exc:
        raise UpstreamError("timed out talking to %s" % _safe_url(url)) from exc


def _request_json(url, payload, *, api_key="", timeout=60, raw=False, method=None):
    """Send an optional JSON body and read the whole response back."""
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    resp = _upstream_request(url, data=body, method=method, headers=headers,
                             api_key=api_key, timeout=timeout)
    try:
        data = resp.read(MAX_REQUEST_BYTES + 1)
    finally:
        resp.close()
    if len(data) > MAX_REQUEST_BYTES:
        raise UpstreamError("response from %s is too large" % _safe_url(url))
    if raw:
        return data
    try:
        return json.loads(data or b"{}")
    except ValueError as exc:
        raise UpstreamError("%s did not return JSON: %s" % (
            _safe_url(url), data[:MAX_ERROR_ECHO_CHARS].decode("utf-8", "replace"))) from exc


def _multipart_body(fields: dict, file_field: str, filename: str,
                    file_bytes: bytes, file_type: str):
    """Build a multipart/form-data body. Builder only -- LangPal never parses one.

    The boundary is random, so it cannot collide with content, and every field
    name and value here is server-controlled or already validated.
    """
    boundary = "----LangPal" + secrets.token_hex(16)
    sep = ("--" + boundary).encode()
    out = bytearray()
    for name, value in fields.items():
        if value is None or value == "":
            continue
        out += sep + b"\r\n"
        out += ('Content-Disposition: form-data; name="%s"\r\n\r\n' % name).encode()
        out += str(value).encode("utf-8") + b"\r\n"
    out += sep + b"\r\n"
    out += ('Content-Disposition: form-data; name="%s"; filename="%s"\r\n'
            % (file_field, filename)).encode()
    out += ("Content-Type: %s\r\n\r\n" % file_type).encode()
    out += file_bytes + b"\r\n"
    out += sep + b"--\r\n"
    return bytes(out), "multipart/form-data; boundary=" + boundary


# ---------------------------------------------------------------------------
# Audio validation
# ---------------------------------------------------------------------------

def inspect_wav(data: bytes, *, max_seconds: int, field: str = "audio") -> float:
    """Validate a WAV in memory and return its duration in seconds.

    Parsing the header here means a malformed or wildly long clip is rejected
    before it costs a GPU minute upstream. Nothing touches disk -- the parse
    happens in a BytesIO.
    """
    if len(data) < 44:
        raise BadRequest("%s is too short to be a WAV file" % field)
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise BadRequest("%s is not a WAV file" % field)
    try:
        with wave.open(io.BytesIO(data), "rb") as wav:
            rate = wav.getframerate()
            frames = wav.getnframes()
            channels = wav.getnchannels()
    except (wave.Error, EOFError) as exc:
        raise BadRequest("%s is not a readable WAV file (%s)" % (field, exc)) from exc
    if rate <= 0 or channels <= 0:
        raise BadRequest("%s has an invalid WAV header" % field)
    seconds = frames / float(rate)
    if seconds <= 0:
        raise BadRequest("%s contains no audio" % field)
    if seconds > max_seconds:
        raise BadRequest("%s is %.1f s long; the limit is %d s" % (field, seconds, max_seconds))
    return seconds


def decode_wav_b64(b64: str, *, max_seconds: int, field: str = "audio") -> bytes:
    """Decode base64 WAV from the browser and sanity-check it."""
    if not isinstance(b64, str) or not b64:
        raise BadRequest("%s is missing" % field)
    # Check the encoded length first so a huge string is rejected without
    # allocating the decoded copy alongside it.
    if len(b64) > MAX_REQUEST_BYTES:
        raise BadRequest("%s is too large" % field)
    try:
        data = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise BadRequest("%s is not valid base64" % field) from exc
    inspect_wav(data, max_seconds=max_seconds, field=field)
    return data


# ---------------------------------------------------------------------------
# Speech to text
# ---------------------------------------------------------------------------

def parse_asr_text(text: str):
    """Split Qwen3-ASR's `language X<asr_text>...` prefix off the transcript.

    Returns (clean_text, detected_language). Both llama.cpp's passthrough form
    and an already-cleaned response are handled, so this is safe to run on any
    OpenAI-compatible transcription result.
    """
    text = (text or "").strip()
    language = ""
    lowered = text.lower()
    if ASR_TEXT_TAG in lowered:
        idx = lowered.index(ASR_TEXT_TAG)
        head, body = text[:idx], text[idx + len(ASR_TEXT_TAG):]
        match = ASR_LANG_PREFIX_RE.match(head)
        if match:
            candidate = match.group(1).strip()
            if LANGUAGE_RE.match(candidate):
                language = candidate
        text = body
    # Drop any stray tag the server left behind, then tidy whitespace.
    text = ASR_STRAY_TAG_RE.sub("", text).strip()
    return text, language


def transcribe(audio_wav: bytes, settings: dict, *, language: str = "") -> dict:
    """POST a WAV to the STT endpoint and return {text, language, raw}."""
    fields = {
        "model": settings["stt_model"],
        "response_format": "json",
    }
    if language:
        # Qwen3-ASR wants the English name ("Turkish"); an ISO code is ignored.
        fields["language"] = language
    body, content_type = _multipart_body(
        fields, "file", "utterance.wav", audio_wav, "audio/wav")

    url = settings["stt_url"] + STT_TRANSCRIBE_PATH
    resp = _upstream_request(url, data=body, method="POST",
                             headers={"Content-Type": content_type,
                                      "Accept": "application/json"},
                             api_key=STT_API_KEY, timeout=STT_TIMEOUT)
    try:
        payload = resp.read(MAX_REQUEST_BYTES + 1)
    finally:
        resp.close()
    try:
        parsed = json.loads(payload or b"{}")
    except ValueError as exc:
        raise UpstreamError("STT did not return JSON: %s" % payload[:MAX_ERROR_ECHO_CHARS]
                            .decode("utf-8", "replace")) from exc

    raw_text = parsed.get("text")
    if not isinstance(raw_text, str):
        raise UpstreamError("STT response has no text field")
    clean, detected = parse_asr_text(raw_text)
    return {"text": clean, "language": detected, "raw": raw_text}


# ---------------------------------------------------------------------------
# Sentence chunking
# ---------------------------------------------------------------------------

def _is_abbreviation(buf: str, dot_index: int) -> bool:
    """True if the '.' at dot_index closes a known abbreviation like "Dr."."""
    start = dot_index
    while start > 0 and (buf[start - 1].isalpha() or buf[start - 1] == "."):
        start -= 1
    word = buf[start:dot_index].lower().strip(".")
    return word in SENTENCE_ABBREVIATIONS


def split_sentences(buf: str, *, final: bool = False):
    """Pull complete sentences off the front of a streaming buffer.

    Returns (sentences, remainder). A terminator only counts when followed by
    whitespace or end-of-buffer, which keeps "3.5" and "example.com" intact.
    CJK terminators need no trailing space. Short fragments are held back until
    they reach MIN_SENTENCE_CHARS so TTS is never handed a one-word clip.
    """
    sentences = []
    start = 0
    i = 0
    while i < len(buf):
        ch = buf[i]
        if ch in SENTENCE_TERMINATORS:
            # Absorb runs like "?!" or "..." into this sentence.
            end = i + 1
            while end < len(buf) and buf[end] in SENTENCE_TERMINATORS:
                end += 1
            # A terminator at the very end is only a boundary once the stream
            # is done -- more punctuation or a decimal digit may still arrive.
            if end >= len(buf) and not final:
                break
            after_is_break = end >= len(buf) or buf[end].isspace()
            cjk = ch in "\u3002\uff01\uff1f\uff1b"
            if (after_is_break or cjk) and not (ch == "." and _is_abbreviation(buf, i)):
                candidate = buf[start:end].strip()
                minimum = MIN_SENTENCE_CHARS_CJK if cjk else MIN_SENTENCE_CHARS
                # `final` must not lower this bar. It only means "flush the
                # trailing remainder", which the block below does. Letting it
                # split short mid-buffer fragments made a one-shot call disagree
                # with the streaming path: "Bonjour ! Bien sur, ..." came out as
                # three sentences one way and two the other.
                if len(candidate) >= minimum:
                    sentences.append(candidate)
                    start = end
            i = end
            continue
        i += 1

    remainder = buf[start:]

    # Runaway sentence with no usable punctuation: cut it at the last space.
    while len(remainder) >= MAX_SENTENCE_CHARS:
        cut = remainder.rfind(" ", MIN_SENTENCE_CHARS, MAX_SENTENCE_CHARS)
        if cut <= 0:
            cut = MAX_SENTENCE_CHARS
        chunk = remainder[:cut].strip()
        if not chunk:
            break
        sentences.append(chunk)
        remainder = remainder[cut:].lstrip()

    if final:
        tail = remainder.strip()
        if tail:
            sentences.append(tail)
        remainder = ""

    return sentences, remainder


def strip_think(text: str, in_think: bool):
    """Remove <think>...</think> spans from a streaming chunk.

    Returns (visible_text, still_inside_think). The flag is carried across
    chunks because a tag can straddle a token boundary.
    """
    out = []
    pos = 0
    while pos < len(text):
        if in_think:
            match = THINK_CLOSE_RE.search(text, pos)
            if not match:
                return "".join(out), True
            pos = match.end()
            in_think = False
        else:
            match = THINK_OPEN_RE.search(text, pos)
            if not match:
                out.append(text[pos:])
                break
            out.append(text[pos:match.start()])
            pos = match.end()
            in_think = True
    return "".join(out), in_think


# Markdown scaffolding the model may emit despite the system prompt. Shown on
# screen as-is, but stripped before the text reaches TTS.
SPEAKABLE_STRIP_RE = re.compile(r"[*_`#|>\[\]]")


def speakable(text: str) -> str:
    """Flatten a sentence into something worth sending to TTS."""
    text = SPEAKABLE_STRIP_RE.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------------------
# Text to speech
# ---------------------------------------------------------------------------

def _tts_routed_url(settings: dict, path: str) -> str:
    """A TTS URL carrying llama-swap's ?model= routing hint.

    llama-swap chooses a backend from the "model" field in the request body, so
    a bodyless request (GET /v1/audio/voices, GET /health) cannot be routed and
    is answered with "no model id could be identified". Passing the model as a
    query parameter routes it, and also triggers the model load.

    Anything that is not llama-swap ignores the extra parameter, so this is
    safe to send unconditionally -- verified against omnivoice.cpp directly.
    """
    return "%s%s?model=%s" % (settings["tts_url"], path,
                              urllib.parse.quote(settings["tts_model"], safe=""))


def synthesize(text: str, settings: dict, language: str) -> bytes:
    """Ask the TTS endpoint for one WAV. Returns the raw bytes."""
    payload = {
        "model": settings["tts_model"],
        "input": text,
        "language": language,
        "response_format": "wav",
    }
    if settings.get("voice"):
        payload["voice"] = settings["voice"]
    if settings.get("tts_seed") is not None:
        payload["seed"] = settings["tts_seed"]

    url = settings["tts_url"] + TTS_SPEECH_PATH
    audio = _request_json(url, payload, api_key=TTS_API_KEY, timeout=TTS_TIMEOUT, raw=True)
    if audio[:4] != b"RIFF":
        raise UpstreamError("TTS returned something that is not a WAV: %s"
                            % audio[:MAX_ERROR_ECHO_CHARS].decode("utf-8", "replace"))
    return audio


def list_voices(settings: dict):
    """GET the registered + built-in voice names from the TTS server."""
    url = _tts_routed_url(settings, TTS_VOICES_PATH)
    data = _request_json(url, None, api_key=TTS_API_KEY, timeout=VOICES_TIMEOUT, method="GET")
    raw = data.get("voices", []) if isinstance(data, dict) else []
    names = []
    for item in raw:
        # omnivoice.cpp returns [{"name": "..."}]; tolerate a plain string list.
        name = item.get("name") if isinstance(item, dict) else item
        if isinstance(name, str) and VOICE_NAME_RE.match(name):
            names.append(name)
    return sorted(set(names))


def register_voice(name: str, ref_text: str, wav: bytes, settings: dict):
    """Register a cloned voice from a reference clip and its transcript."""
    url = settings["tts_url"] + TTS_VOICES_PATH
    payload = {
        # Present so llama-swap can route this POST; the TTS server ignores it.
        "model": settings["tts_model"],
        "name": name,
        "ref_text": ref_text,
        "wav_b64": base64.b64encode(wav).decode("ascii"),
    }
    return _request_json(url, payload, api_key=TTS_API_KEY, timeout=VOICES_TIMEOUT)


def delete_voice(name: str, settings: dict):
    """Remove a registered voice. `name` must already have passed VOICE_NAME_RE."""
    url = "%s%s/%s?model=%s" % (
        settings["tts_url"], TTS_VOICES_PATH, urllib.parse.quote(name, safe=""),
        urllib.parse.quote(settings["tts_model"], safe=""))
    try:
        return _request_json(url, None, api_key=TTS_API_KEY,
                             timeout=VOICES_TIMEOUT, method="DELETE")
    except UpstreamError as exc:
        if exc.status == 404:
            # llama-swap only pattern-matches /v1/audio/voices, not the
            # /<name> sub-path, so DELETE cannot reach the backend through its
            # main port however it is routed. The per-model prefix can.
            raise UpstreamError(
                "could not delete %r: %s. If your TTS endpoint is llama-swap's "
                "main port, it has no route for %s/<name>. Set the TTS URL to "
                "%s/upstream/%s, or point it straight at the TTS server."
                % (name, exc, TTS_VOICES_PATH, settings["tts_url"],
                   settings["tts_model"]), status=404) from exc
        raise


# ---------------------------------------------------------------------------
# Default voice bootstrap
# ---------------------------------------------------------------------------

def scan_default_voices(directory: str):
    """List (name, wav_path, txt_path_or_None) for every WAV in `directory`.

    The voice name is the file stem, so `tr_ayse.wav` registers as `tr_ayse`
    and pairs with `tr_ayse.txt`. Stems that are not valid voice names are
    skipped with a warning rather than silently mangled.
    """
    found = []
    try:
        entries = sorted(os.listdir(directory))
    except OSError:
        return found
    for entry in entries:
        stem, ext = os.path.splitext(entry)
        ext = ext.lower()
        if ext != ".wav":
            # Say so rather than ignoring it. The registry only takes WAV, and
            # there is no stdlib decoder for anything else, so an mp3 dropped
            # in here would otherwise just never appear in the voice list.
            if ext in AUDIO_EXTENSIONS:
                log("  voices_default: %s is not a WAV -- convert it, or add it "
                    "through the Voices panel, which decodes in the browser" % entry)
            continue
        wav_path = os.path.join(directory, entry)
        if not os.path.isfile(wav_path):
            continue
        if not VOICE_NAME_RE.match(stem):
            log("  voices_default: skipping %r (name must be letters, digits, . _ -)" % entry)
            continue
        txt_path = os.path.join(directory, stem + ".txt")
        found.append((stem, wav_path, txt_path if os.path.isfile(txt_path) else None))
    return found


def register_default_voices(settings: dict, directory: str) -> None:
    """Register every clip in `directory` that is not registered already.

    Read-only with respect to the folder. Failures are logged and skipped so
    one bad clip cannot stop the rest.
    """
    entries = scan_default_voices(directory)
    if not entries:
        return
    log("voices_default: %d clip(s) found in %s" % (len(entries), directory))
    try:
        existing = set(list_voices(settings))
    except UpstreamError as exc:
        log("voices_default: TTS not reachable, skipping bootstrap (%s)" % exc)
        return

    for name, wav_path, txt_path in entries:
        if name in existing:
            log("  %s: already registered" % name)
            continue
        try:
            with open(wav_path, "rb") as fh:
                wav = fh.read()
            inspect_wav(wav, max_seconds=MAX_VOICE_SAMPLE_SECONDS, field=name)

            ref_text = ""
            source = "transcribed"
            if txt_path:
                with open(txt_path, "r", encoding="utf-8") as fh:
                    ref_text = fh.read().strip()[:MAX_REF_TEXT_CHARS]
                if ref_text:
                    source = "%s.txt" % name

            if not ref_text:
                # No transcript supplied: ask the ASR model for one. Language is
                # left to auto-detect since these clips can be in any language.
                ref_text = transcribe(wav, settings)["text"][:MAX_REF_TEXT_CHARS]

            if not ref_text:
                log("  %s: no transcript and ASR returned nothing, skipped" % name)
                continue

            register_voice(name, ref_text, wav, settings)
            log("  %s: registered (%s)" % (name, source))
        except (OSError, BadRequest, UpstreamError) as exc:
            log("  %s: failed -- %s" % (name, exc))


def start_default_voice_bootstrap(directory: str) -> None:
    """Kick off register_default_voices() on a daemon thread.

    Backgrounded because a cold llama-swap may spend a minute loading omnivoice
    and qwen3-asr, and the UI should be up long before that finishes.
    """
    if not REGISTER_DEFAULT_VOICES or not os.path.isdir(directory):
        return
    settings = default_settings()

    def run():
        try:
            register_default_voices(settings, directory)
        except Exception as exc:               # noqa: BLE001 - never kill the server
            log("voices_default: bootstrap aborted -- %r" % exc)

    threading.Thread(target=run, name="voice-bootstrap", daemon=True).start()


# ---------------------------------------------------------------------------
# LLM streaming
# ---------------------------------------------------------------------------

def build_chat_payload(messages, settings: dict) -> dict:
    """Assemble the /v1/chat/completions body.

    Fields left unset in the settings are omitted entirely so the inference
    server's own defaults apply -- the documented behaviour for temperature,
    top_p, max_tokens and seed.
    """
    payload = {
        "model": settings["llm_model"],
        "messages": messages,
        "stream": True,
    }
    for key in ("temperature", "top_p", "seed"):
        if settings.get(key) is not None:
            payload[key] = settings[key]
    if settings.get("max_tokens") is not None:
        payload["max_tokens"] = settings["max_tokens"]
    if settings.get("reasoning_effort"):
        payload["reasoning_effort"] = settings["reasoning_effort"]
    if settings.get("enable_thinking") is not None:
        # llama.cpp --jinja reads this out of chat_template_kwargs.
        payload["chat_template_kwargs"] = {"enable_thinking": settings["enable_thinking"]}
    return payload


def stream_chat(messages, settings: dict):
    """Yield assistant content deltas from the streaming chat endpoint.

    Only `delta.content` is yielded. `delta.reasoning_content`, which llama.cpp
    emits when reasoning_format splits thinking out, is deliberately dropped so
    it never reaches the screen or the speaker.
    """
    url = settings["llm_url"] + LLM_CHAT_PATH
    body = json.dumps(build_chat_payload(messages, settings)).encode("utf-8")
    resp = _upstream_request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
        api_key=LLM_API_KEY, timeout=LLM_TIMEOUT)
    try:
        while True:
            line = resp.readline(MAX_SSE_LINE_BYTES)
            if not line:
                break
            line = line.strip()
            if not line or line.startswith(b":") or not line.startswith(b"data:"):
                continue
            chunk = line[5:].strip()
            if chunk == b"[DONE]":
                break
            try:
                event = json.loads(chunk)
            except ValueError:
                continue                       # tolerate a stray keep-alive line
            for choice in event.get("choices") or []:
                delta = choice.get("delta") or {}
                piece = delta.get("content")
                if isinstance(piece, str) and piece:
                    yield piece
    finally:
        resp.close()


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

class Session:
    """One browser's conversation. Lives in RAM and dies with the process."""

    def __init__(self, sid: str):
        self.sid = sid
        self.history = []          # [{"role": "user"|"assistant", "content": str}]
        self.cancelled = []        # turn ids Stop has revoked, newest last
        self.last_seen = time.monotonic()
        self.lock = threading.Lock()

    def cancel(self, turn: str) -> None:
        """Revoke a turn, so work still queued for it is dropped."""
        with self.lock:
            if turn in self.cancelled:
                return
            self.cancelled.append(turn)
            if len(self.cancelled) > CANCELLED_TURNS:
                del self.cancelled[:-CANCELLED_TURNS]

    def is_cancelled(self, turn: str) -> bool:
        """Whether this turn has been revoked.

        An id we have never seen is *not* cancelled. That direction matters:
        a turn the server cannot place is a new one, and refusing it would
        wedge the conversation with no way back.
        """
        return bool(turn) and turn in self.cancelled


class SessionStore:
    """Bounded, thread-safe session table with least-recently-used eviction."""

    def __init__(self, limit: int = MAX_SESSIONS):
        self._sessions = {}
        self._limit = limit
        self._lock = threading.Lock()

    def get_or_create(self, sid: str):
        """Return (session, is_new). An sid we do not recognise gets a fresh one."""
        with self._lock:
            session = self._sessions.get(sid) if sid else None
            created = False
            if session is None:
                # Server-generated, 256 bits: a client cannot pick or guess
                # another user's session id.
                new_sid = secrets.token_urlsafe(32)
                session = Session(new_sid)
                self._sessions[new_sid] = session
                created = True
                while len(self._sessions) > self._limit:
                    oldest = min(self._sessions.values(), key=lambda s: s.last_seen)
                    self._sessions.pop(oldest.sid, None)
            session.last_seen = time.monotonic()
            return session, created


SESSIONS = SessionStore()


def build_messages(session: Session, user_text: str, settings: dict):
    """System prompt + trimmed history + this turn."""
    messages = []
    if settings.get("system_prompt"):
        messages.append({"role": "system", "content": settings["system_prompt"]})
    limit = settings["history_messages"]
    if limit > 0:
        messages.extend(session.history[-limit:])
    messages.append({"role": "user", "content": user_text})
    return messages


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

STATIC_CACHE = {}          # filename -> bytes, filled by load_static()
DEV_MODE = False           # --dev re-reads web/ per request
TLS_ENABLED = False


def load_static() -> None:
    """Read web/ into RAM once. A missing file is a startup error, not a 404."""
    for _route, (filename, _ctype) in STATIC_FILES.items():
        if filename in STATIC_CACHE:
            continue
        with open(os.path.join(WEB_DIR, filename), "rb") as fh:
            STATIC_CACHE[filename] = fh.read()


def _read_static(filename: str) -> bytes:
    if DEV_MODE:
        with open(os.path.join(WEB_DIR, filename), "rb") as fh:
            return fh.read()
    return STATIC_CACHE[filename]


class Handler(BaseHTTPRequestHandler):
    server_version = "LangPal"
    sys_version = ""                      # do not advertise the Python version
    protocol_version = "HTTP/1.1"
    timeout = CONNECTION_TIMEOUT
    # Set once the current request's body has been read off the socket. See
    # the note in _send() for why this matters.
    _body_read = False

    # -- plumbing ----------------------------------------------------------

    def version_string(self):
        """Just "LangPal". The base class would append a trailing space here."""
        return self.server_version

    def log_message(self, fmt, *args):
        """One compact line per request, to stdout. No bodies, no tokens."""
        sys.stdout.write("%s %s\n" % (self.log_date_time_string(), fmt % args))
        sys.stdout.flush()

    def _request_has_unread_body(self) -> bool:
        """True if this request carried a body we never read off the socket."""
        if self._body_read:
            return False
        try:
            return int(self.headers.get("Content-Length") or 0) > 0
        except ValueError:
            return True

    def _send(self, status, body=b"", content_type="application/json; charset=utf-8",
              extra_headers=None):
        # Answering without consuming the request body (an auth rejection, a
        # 404, an oversized upload) leaves those bytes in the socket, where
        # keep-alive would make the next read parse them as a fresh request
        # line. Closing the connection is the only correct response.
        if self._request_has_unread_body():
            self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if self.close_connection:
            self.send_header("Connection", "close")
        for key, value in SECURITY_HEADERS.items():
            self.send_header(key, value)
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD" and body:
            self.wfile.write(body)

    def _send_json(self, status, payload, extra_headers=None):
        self._send(status, json.dumps(payload).encode("utf-8"), extra_headers=extra_headers)

    def _error(self, status, message):
        self._send_json(status, {"error": message})

    def _try_error(self, status, message):
        """Send an error response, tolerating a client that has already left.

        Aborting a fetch -- which the Stop button does to every request in
        flight -- closes the socket while the handler may still be working.
        Writing the reply then raises, and because that write happens inside an
        `except` block, the new exception escapes the handler's own error
        handling and unwinds all the way into socketserver, printing a
        traceback for what is really just a client that hung up.
        """
        try:
            self._error(status, message)
        except CONNECTION_ERRORS:
            pass
        return None

    def _read_body(self) -> bytes:
        """Read exactly Content-Length bytes, refusing oversized bodies."""
        raw_len = self.headers.get("Content-Length")
        if raw_len is None:
            raise BadRequest("Content-Length is required")
        try:
            length = int(raw_len)
        except ValueError as exc:
            raise BadRequest("Content-Length is not a number") from exc
        if length < 0:
            raise BadRequest("Content-Length is negative")
        if length > MAX_REQUEST_BYTES:
            # Rejected without reading it. On a keep-alive connection the
            # unread remainder would be parsed as the next request, so this
            # connection has to close.
            self.close_connection = True
            raise TooLarge("request body is larger than the %d byte limit"
                           % MAX_REQUEST_BYTES)
        data = self.rfile.read(length) if length else b""
        self._body_read = True
        if len(data) != length:
            raise BadRequest("request body was truncated")
        return data

    def _read_json(self) -> dict:
        data = self._read_body()
        try:
            parsed = json.loads(data or b"{}")
        except ValueError as exc:
            raise BadRequest("request body is not valid JSON") from exc
        if not isinstance(parsed, dict):
            raise BadRequest("request body must be a JSON object")
        return parsed

    def _cookies(self) -> dict:
        """Parse the Cookie header without http.cookies' quoting quirks."""
        out = {}
        for raw in self.headers.get_all("Cookie") or []:
            for part in raw.split(";"):
                name, _, value = part.strip().partition("=")
                if name:
                    out[name.strip()] = value.strip()
        return out

    def _cookie_header(self, name, value, *, max_age=None):
        bits = ["%s=%s" % (name, value), "Path=/", "HttpOnly", "SameSite=Strict"]
        if TLS_ENABLED:
            bits.append("Secure")
        if max_age is not None:
            bits.append("Max-Age=%d" % max_age)
        return "; ".join(bits)

    # -- authentication ----------------------------------------------------

    def _authorized(self) -> bool:
        """True when no token is configured, or the client presents the right one."""
        if not ACCESS_TOKEN:
            return True
        supplied = self._cookies().get("lp_auth", "")
        if not supplied:
            header = self.headers.get("Authorization", "")
            if header.startswith("Bearer "):
                supplied = header[7:].strip()
        # compare_digest keeps the comparison time independent of the guess.
        return hmac.compare_digest(supplied, ACCESS_TOKEN)

    def _session(self):
        """Resolve the session cookie, minting one if needed.

        Returns (session, set_cookie_header_or_None).
        """
        sid = self._cookies().get("lp_sid", "")
        # Reject anything that does not look like one of our own ids before it
        # reaches the store, so junk cookies cannot churn the table.
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", sid or ""):
            sid = ""
        session, created = SESSIONS.get_or_create(sid)
        header = self._cookie_header("lp_sid", session.sid) if created else None
        return session, header

    # -- routing -----------------------------------------------------------

    def do_GET(self):
        self._dispatch("GET")

    def do_HEAD(self):
        self._dispatch("HEAD")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method):
        # One handler instance serves every request on a keep-alive
        # connection, so per-request state has to be reset here.
        self._body_read = False
        path = urllib.parse.urlsplit(self.path).path
        try:
            # Login is the only route reachable without a token.
            if path == "/api/login" and method == "POST":
                return self._handle_login()
            if not self._authorized():
                if path in STATIC_FILES and method in ("GET", "HEAD"):
                    # Serve the shell so it can render its password prompt.
                    return self._handle_static(path)
                return self._error(401, "authentication required")

            if method in ("GET", "HEAD") and path in STATIC_FILES:
                return self._handle_static(path)
            if method == "GET" and path == "/api/config":
                return self._handle_config()
            if method == "POST":
                route = {
                    "/api/health": self._handle_health,
                    "/api/stt": self._handle_stt,
                    "/api/chat": self._handle_chat,
                    "/api/tts": self._handle_tts,
                    "/api/stop": self._handle_stop,
                    "/api/reset": self._handle_reset,
                    "/api/voices": self._handle_voices_list,
                    "/api/voices/register": self._handle_voice_register,
                    "/api/voices/delete": self._handle_voice_delete,
                }.get(path)
                if route:
                    return route()
            return self._error(404, "not found")
        except TooLarge as exc:
            return self._try_error(413, str(exc))
        except BadRequest as exc:
            return self._try_error(400, str(exc))
        except UpstreamError as exc:
            return self._try_error(502, str(exc))
        except Aborted:
            # Routine: the Stop button invalidated this request mid-flight.
            return self._try_error(409, "stopped")
        except CONNECTION_ERRORS:
            return None                        # client hung up; nothing to say
        except Exception as exc:               # noqa: BLE001 - last-resort guard
            # Log the detail locally, tell the client only that it broke.
            self.log_message("unhandled error on %s: %r", path, exc)
            return self._try_error(500, "internal error")

    # -- handlers ----------------------------------------------------------

    def _handle_static(self, path):
        filename, ctype = STATIC_FILES[path]
        try:
            body = _read_static(filename)
        except OSError:
            return self._error(500, "asset missing")
        # No caching: the UI is tiny, and a stale app.js after an edit is worse
        # than re-fetching 30 kB over a tunnel.
        self._send(200, body, content_type=ctype,
                   extra_headers={"Cache-Control": "no-store"})

    def _handle_login(self):
        if not ACCESS_TOKEN:
            return self._send_json(200, {"ok": True, "auth_required": False})
        payload = self._read_json()
        supplied = payload.get("token")
        if not isinstance(supplied, str):
            supplied = ""
        if not hmac.compare_digest(supplied, ACCESS_TOKEN):
            time.sleep(0.5)                    # take the edge off online guessing
            return self._error(401, "wrong password")
        cookie = self._cookie_header("lp_auth", ACCESS_TOKEN, max_age=30 * 24 * 3600)
        return self._send_json(200, {"ok": True}, extra_headers={"Set-Cookie": cookie})

    def _handle_config(self):
        """Server defaults and limits, so the UI can seed empty localStorage.

        Upstream API keys and ACCESS_TOKEN are not in default_settings() and
        never leave the process.
        """
        _session, cookie = self._session()
        payload = {
            "defaults": default_settings(),
            "languages": list(LANGUAGE_CHOICES),
            "reasoning_efforts": list(REASONING_EFFORT_VALUES),
            "auth_required": bool(ACCESS_TOKEN),
            "limits": {
                "max_utterance_seconds": MAX_UTTERANCE_SECONDS,
                "max_voice_sample_seconds": MAX_VOICE_SAMPLE_SECONDS,
                "max_user_text_chars": MAX_USER_TEXT_CHARS,
                "max_tts_text_chars": MAX_TTS_TEXT_CHARS,
                "max_system_prompt_chars": MAX_SYSTEM_PROMPT_CHARS,
                "max_ref_text_chars": MAX_REF_TEXT_CHARS,
                "max_request_bytes": MAX_REQUEST_BYTES,
            },
        }
        extra = {"Set-Cookie": cookie} if cookie else None
        return self._send_json(200, payload, extra_headers=extra)

    def _handle_health(self):
        """Probe all three upstreams with the caller's settings."""
        body = self._read_json()
        settings = validate_settings(body.get("settings"))
        results = {}
        for name, url in (
            ("llm", settings["llm_url"] + LLM_MODELS_PATH),
            ("stt", settings["stt_url"] + LLM_MODELS_PATH),
            ("tts", _tts_routed_url(settings, HEALTH_PATH)),
        ):
            started = time.monotonic()
            try:
                resp = _upstream_request(url, method="GET", timeout=HEALTH_TIMEOUT)
                try:
                    resp.read(4096)
                finally:
                    resp.close()
                results[name] = {"ok": True, "ms": int((time.monotonic() - started) * 1000)}
            except UpstreamError as exc:
                results[name] = {"ok": False, "error": str(exc)[:MAX_ERROR_ECHO_CHARS]}
        return self._send_json(200, {"checks": results})

    def _handle_stt(self):
        """Transcribe one clip -- either a spoken turn or a voice sample."""
        body = self._read_json()
        settings = validate_settings(body.get("settings"))
        is_sample = body.get("purpose") == "voice_sample"
        audio = decode_wav_b64(
            body.get("audio_b64"),
            max_seconds=MAX_VOICE_SAMPLE_SECONDS if is_sample else MAX_UTTERANCE_SECONDS,
        )
        # A voice sample is transcribed verbatim, so let the model detect its
        # language rather than forcing the conversation language onto it.
        language = "" if is_sample else stt_language_for(settings)
        return self._send_json(200, transcribe(audio, settings, language=language))

    def _handle_tts(self):
        body = self._read_json()
        settings = validate_settings(body.get("settings"))
        session, _cookie = self._session()
        turn = self._turn_id(body)

        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            raise BadRequest("text is required")
        text = speakable(text)[:MAX_TTS_TEXT_CHARS]
        if not text:
            raise BadRequest("text has nothing speakable in it")

        detected = body.get("detected_language")
        detected = detected if isinstance(detected, str) else ""
        if detected and not LANGUAGE_RE.match(detected):
            detected = ""
        language = tts_language_for(settings, detected)

        # Cheap pre-flight check: if Stop was pressed while this request sat in
        # the queue, skip the synthesis entirely instead of burning the GPU.
        if session.is_cancelled(turn):
            raise Aborted()
        audio = synthesize(text, settings, language)
        if session.is_cancelled(turn):
            raise Aborted()
        return self._send(200, audio, content_type="audio/wav",
                          extra_headers={"Cache-Control": "no-store"})

    def _turn_id(self, body):
        """Read the optional turn id. Absent means the work is uncancellable."""
        turn = body.get("turn")
        if turn is None or turn == "":
            return ""
        if not isinstance(turn, str) or not TURN_ID_RE.match(turn):
            raise BadRequest("turn must be 8-64 characters of letters, digits, _ or -")
        return turn

    def _handle_stop(self):
        """Revoke a turn, cancelling anything still queued for it."""
        body = self._read_json()
        session, cookie = self._session()
        turn = self._turn_id(body)
        if turn:
            session.cancel(turn)
        extra = {"Set-Cookie": cookie} if cookie else None
        return self._send_json(200, {"ok": True}, extra_headers=extra)

    def _handle_reset(self):
        body = self._read_json()
        session, cookie = self._session()
        turn = self._turn_id(body)
        if turn:
            session.cancel(turn)
        with session.lock:
            session.history = []
        extra = {"Set-Cookie": cookie} if cookie else None
        return self._send_json(200, {"ok": True}, extra_headers=extra)

    def _handle_voices_list(self):
        body = self._read_json()
        settings = validate_settings(body.get("settings"))
        return self._send_json(200, {"voices": list_voices(settings)})

    def _handle_voice_register(self):
        body = self._read_json()
        settings = validate_settings(body.get("settings"))

        name = (body.get("name") or "").strip()
        if not VOICE_NAME_RE.match(name):
            raise BadRequest("voice name must be 1-64 characters of letters, digits, . _ or -")

        ref_text = body.get("ref_text")
        if not isinstance(ref_text, str) or not ref_text.strip():
            raise BadRequest("ref_text is required -- transcribe the clip first")
        ref_text = ref_text.strip()[:MAX_REF_TEXT_CHARS]

        wav = decode_wav_b64(body.get("audio_b64"),
                             max_seconds=MAX_VOICE_SAMPLE_SECONDS, field="voice sample")

        # Registering over a built-in speaker shadows it for the lifetime of the
        # TTS process, so require an explicit replace for any existing name.
        if not body.get("replace") and name in list_voices(settings):
            raise BadRequest("a voice named %r already exists -- delete it first, "
                             "or re-register with replace" % name)
        register_voice(name, ref_text, wav, settings)
        return self._send_json(200, {"ok": True, "name": name})

    def _handle_voice_delete(self):
        body = self._read_json()
        settings = validate_settings(body.get("settings"))
        name = (body.get("name") or "").strip()
        if not VOICE_NAME_RE.match(name):
            raise BadRequest("invalid voice name")
        delete_voice(name, settings)
        return self._send_json(200, {"ok": True, "name": name})

    # -- the streaming turn ------------------------------------------------

    def _handle_chat(self):
        """Stream the LLM reply as Server-Sent Events.

        Events:
          delta    {"t": "..."}             incremental visible text
          sentence {"i": 0, "text": "..."}  a complete sentence, ready for TTS
          done     {"text": "...", "sentences": n}
          error    {"message": "..."}
        """
        body = self._read_json()
        settings = validate_settings(body.get("settings"))
        session, cookie = self._session()

        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            raise BadRequest("text is required")
        text = text.strip()[:MAX_USER_TEXT_CHARS]

        turn = self._turn_id(body)
        if session.is_cancelled(turn):
            raise Aborted()

        with session.lock:
            messages = build_messages(session, text, settings)

        # Past this point the response has started, so problems are reported as
        # an SSE `error` event rather than an HTTP status.
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        # Chunked, not close-delimited. Framing the body by closing the socket
        # works over plain HTTP, but under TLS Python closes the SSLSocket
        # without sending close_notify, so the browser sees a truncated stream
        # and reports a network error even though every byte arrived. A chunked
        # body is self-framing, so the end of the stream is unambiguous.
        self.send_header("Transfer-Encoding", "chunked")
        for key, value in SECURITY_HEADERS.items():
            self.send_header(key, value)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()

        def write_chunk(data):
            """Write one HTTP chunk: length in hex, CRLF, payload, CRLF."""
            self.wfile.write(b"%x\r\n" % len(data) + data + b"\r\n")

        def emit(event, payload):
            frame = "event: %s\ndata: %s\n\n" % (event, json.dumps(payload))
            write_chunk(frame.encode("utf-8"))

        buffer = ""
        visible = []
        index = 0
        in_think = False
        last_beat = time.monotonic()

        try:
            for piece in stream_chat(messages, settings):
                if session.is_cancelled(turn):
                    raise Aborted()
                shown, in_think = strip_think(piece, in_think)
                if not shown:
                    # Still inside a think block: keep the socket warm.
                    if time.monotonic() - last_beat > SSE_HEARTBEAT_SECONDS:
                        write_chunk(b": ping\n\n")
                        last_beat = time.monotonic()
                    continue
                visible.append(shown)
                buffer += shown
                emit("delta", {"t": shown})
                last_beat = time.monotonic()

                sentences, buffer = split_sentences(buffer)
                for sentence in sentences:
                    if speakable(sentence):
                        emit("sentence", {"i": index, "text": sentence})
                        index += 1

            tail, buffer = split_sentences(buffer, final=True)
            for sentence in tail:
                if speakable(sentence):
                    emit("sentence", {"i": index, "text": sentence})
                    index += 1

            full = "".join(visible).strip()
            if full:
                with session.lock:
                    session.history.append({"role": "user", "content": text})
                    session.history.append({"role": "assistant", "content": full})
                    # Keep RAM bounded regardless of what the client asks for.
                    if len(session.history) > MAX_HISTORY_MESSAGES:
                        del session.history[:-MAX_HISTORY_MESSAGES]
            emit("done", {"text": full, "sentences": index})

        except Aborted:
            emit("error", {"message": "stopped"})
        except UpstreamError as exc:
            emit("error", {"message": str(exc)})
        except (BrokenPipeError, ConnectionResetError):
            return                              # client navigated away mid-stream
        except Exception as exc:                # noqa: BLE001 - keep the stream framed
            self.log_message("chat stream failed: %r", exc)
            try:
                emit("error", {"message": "internal error"})
            except Exception:                   # noqa: BLE001
                pass
        finally:
            # The zero-length chunk terminates the body. Without it the client
            # keeps waiting, whatever else went wrong above.
            try:
                self.wfile.write(b"0\r\n\r\n")
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def harden_tls_context(context: ssl.SSLContext) -> str:
    """Prefer modern (and, where available, hybrid post-quantum) key exchange.

    SSLContext.set_groups() arrived in Python 3.14 and only understands
    X25519MLKEM768 when the interpreter is linked against OpenSSL 3.5 or newer.
    Everywhere else this is a no-op and OpenSSL's defaults apply, which are
    still X25519/P-256 -- fine, just not post-quantum. Returns a short string
    describing what actually took effect, for the startup banner.
    """
    setter = getattr(context, "set_groups", None)
    if setter is None:
        return "OpenSSL defaults (set_groups needs Python 3.14+; you have %s)" % (
            ".".join(str(n) for n in sys.version_info[:3]))
    try:
        setter(TLS_GROUPS)
        return TLS_GROUPS
    except (ssl.SSLError, ValueError, OSError):
        # OpenSSL rejects the whole list if it cannot parse any group name,
        # which is exactly what happens on OpenSSL < 3.5.
        try:
            setter("X25519:P-256")
            return "X25519:P-256 (no PQ group in %s)" % ssl.OPENSSL_VERSION
        except (ssl.SSLError, ValueError, OSError):
            return "OpenSSL defaults"


def build_server(host, port, certfile="", keyfile=""):
    """Create the listening server, wrapping it in TLS when a cert is given."""
    global TLS_ENABLED
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    if certfile and keyfile:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        groups = harden_tls_context(context)
        context.load_cert_chain(certfile, keyfile)
        httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
        TLS_ENABLED = True
        log("TLS enabled, key exchange: %s" % groups)
    return httpd


def main(argv=None):
    global DEV_MODE

    parser = argparse.ArgumentParser(
        description="LangPal -- voice chat against a local OpenAI-compatible stack")
    parser.add_argument("--host", default=BIND_HOST, help="bind address (default %(default)s)")
    parser.add_argument("--port", type=int, default=BIND_PORT,
                        help="bind port (default %(default)s)")
    parser.add_argument("--cert", default=TLS_CERT_FILE,
                        help="TLS certificate file; needed for remote microphone access")
    parser.add_argument("--key", default=TLS_KEY_FILE, help="TLS private key file")
    parser.add_argument("--voices-dir", default=DEFAULT_VOICES_DIR,
                        help="folder of default voice clips to register at startup")
    parser.add_argument("--no-default-voices", action="store_true",
                        help="skip the voices_default bootstrap")
    parser.add_argument("--dev", action="store_true",
                        help="re-read web/ on every request instead of caching it")
    args = parser.parse_args(argv)

    DEV_MODE = args.dev
    if bool(args.cert) != bool(args.key):
        parser.error("--cert and --key must be given together")

    try:
        load_static()
    except OSError as exc:
        parser.error("cannot read web assets from %s (%s)" % (WEB_DIR, exc))

    try:
        httpd = build_server(args.host, args.port, args.cert, args.key)
    except ssl.SSLError as exc:
        print("TLS setup failed: %s" % exc, file=sys.stderr)
        return 1
    except OSError as exc:
        print("cannot bind %s:%d -- %s" % (args.host, args.port, exc), file=sys.stderr)
        return 1

    scheme = "https" if TLS_ENABLED else "http"
    shown_host = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    print("LangPal on %s://%s:%d" % (scheme, shown_host, args.port))
    print("  LLM  %s  (%s)" % (DEFAULT_LLM_BASE_URL, DEFAULT_LLM_MODEL))
    print("  STT  %s  (%s)" % (DEFAULT_STT_BASE_URL, DEFAULT_STT_MODEL))
    print("  TTS  %s  (%s)" % (DEFAULT_TTS_BASE_URL, DEFAULT_TTS_MODEL))
    print("  auth %s" % ("password required" if ACCESS_TOKEN
                         else "open (no LANGPAL_TOKEN set)"))

    loopback = args.host in ("127.0.0.1", "::1", "localhost")
    if not loopback and not TLS_ENABLED:
        print("")
        print("  ! Serving plain HTTP on a non-loopback address.")
        print("  ! Browsers block microphone access outside a secure context, so")
        print("  ! remote devices will not be able to record. See docs/hosting.md.")
    if not loopback and not ACCESS_TOKEN:
        print("  ! No LANGPAL_TOKEN set: anyone who can reach this address can use it.")

    # Redirecting stdout to a file makes it block-buffered, which would hide
    # the banner (and every warning above it) until the process exits.
    sys.stdout.flush()

    if not args.no_default_voices:
        start_default_voice_bootstrap(args.voices_dir)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
