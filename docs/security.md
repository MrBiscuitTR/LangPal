# Security notes

LangPal is built for one person and a handful of trusted friends inside a
WireGuard tunnel. That shapes every decision below. It is **not** hardened for
the public internet and should not be exposed there.

## Threat model

**Defended against**

- A buggy or malicious browser client exhausting the host's RAM.
- Anything the server writes to disk being used against you — because it writes
  nothing.
- Model output being rendered as markup, or reaching the speaker as markdown.
- Credentials leaking into browser storage, logs or error messages.
- A compromised or misconfigured upstream redirecting requests elsewhere.
- Cookie theft via JavaScript, and cross-site request forgery.
- Path traversal in static file serving and in voice names.
- Keep-alive desynchronisation from unread request bodies.

**Explicitly not defended against**

- **Server-Side Request Forgery.** Any authenticated user can point LangPal at
  an arbitrary `http(s)` URL and see the response. This is inherent — the whole
  purpose is to call endpoints you nominate — and it is accepted. It is also
  why the access token matters when the server is not on loopback.
- **Denial of service.** Limits bound memory per request, not aggregate load.
  Someone with access can keep the GPU busy indefinitely.
- **A hostile network peer.** WireGuard is the perimeter. LangPal assumes
  everyone who can reach it is meant to.
- **`http.server`'s general robustness.** It is the standard library's
  development server. Adequate for a handful of trusted users on a private
  network; not a hardened front end.

## What is enforced

### Input validation

Everything from a browser goes through `validate_settings()` before use.
Numbers are type- and range-checked (`NaN` and infinities rejected, `bool`
rejected for numeric fields), enums are checked against fixed tuples, and
unknown keys are dropped rather than passed through.

URLs go through `normalize_base_url()`, which rejects:

| Rejected | Why |
| --- | --- |
| Non-`http`/`https` schemes | `file://`, `gopher://` and friends |
| `user:pass@host` | credentials would reach logs and error messages |
| Whitespace or control characters | header smuggling into the upstream request |
| Malformed or out-of-range ports | |
| Over 512 characters | |

Voice names must match `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$` before being
percent-encoded into a URL path. `../../etc/passwd` and `a/b` are both
rejected, and there is a test for each.

Language names must match a letters-and-spaces pattern, so a detected language
from an upstream cannot become an injection vector when it is passed on to TTS.

### Memory limits

| Limit | Value |
| --- | --- |
| Request body | 12 MiB, checked against `Content-Length` before reading |
| Spoken turn | 90 s |
| Voice clip | 120 s |
| Upstream response | 12 MiB |
| One upstream SSE line | 256 KiB |
| History per session | 200 messages |
| Sessions | 32, least-recently-used evicted |

Audio is validated with the `wave` module in a `BytesIO` before it is forwarded,
so a malformed or enormous clip is rejected here rather than upstream.

### No disk writes

At runtime LangPal reads `web/`, `.env.local` and `voices_default/` at startup,
and nothing afterwards. It never writes or deletes. Audio, transcripts,
conversation history and uploaded voice samples exist only in RAM and die with
the process.

This is a deliberate constraint, and it is why settings live in the browser
rather than a config file.

### Secrets

`LANGPAL_LLM_API_KEY`, `LANGPAL_STT_API_KEY`, `LANGPAL_TTS_API_KEY` and
`LANGPAL_TOKEN` are server-side only. They are not in `default_settings()`, so
they cannot appear in `/api/config`; they cannot be set from a browser; and
they are never echoed in an error. A unit test asserts no secret-shaped key
appears in the client payload.

Request logging is one line per request — method, path, status. No bodies, no
headers, no tokens.

### Authentication

Optional, off by default. With `LANGPAL_TOKEN` set, every route except
`/api/login` requires it.

- Compared with `hmac.compare_digest`, so the comparison time does not leak how
  much of a guess was right.
- A wrong password costs 0.5 s, which makes online guessing impractical without
  affecting normal use.
- Stored in an `HttpOnly` cookie, so JavaScript — including anything injected
  into the page — cannot read it.
- `SameSite=Strict`, so another site cannot make authenticated requests.
- `Secure` is added automatically when TLS is on.

Session ids are 256 bits from `secrets.token_urlsafe` and **server-generated**.
A client presenting an id the server does not recognise gets a fresh session
rather than being trusted, so a session cannot be hijacked by guessing.

### Response headers

Sent on every response:

```
Content-Security-Policy: default-src 'none'; script-src 'self'; style-src 'self';
  img-src 'self' data:; media-src 'self' blob:; connect-src 'self';
  form-action 'self'; frame-ancestors 'none'; base-uri 'none'
X-Content-Type-Options: nosniff
Referrer-Policy: no-referrer
Cross-Origin-Opener-Policy: same-origin
```

The CSP has no `unsafe-inline`, so `index.html` contains no inline `<script>`
or `<style>` and none can be injected. `connect-src 'self'` means the page
cannot exfiltrate to another origin even if script execution were achieved.

Model output is inserted with `textContent`, never `innerHTML`, so a reply
containing `<img onerror=…>` is displayed as text.

The `Server` header is just `LangPal` — the Python version is not advertised.

### Upstream calls

- **Redirects are refused.** Following one could send an `Authorization` header
  to a host you did not nominate.
- Error bodies are truncated to 400 characters before being relayed.
- Query strings are stripped from URLs in error messages.
- Multipart bodies are *built*, never parsed. There is no multipart parser in
  the codebase to get wrong, and the boundary is 128 random bits.

### Keep-alive integrity

A response sent without consuming the request body — an auth rejection, a 404,
an oversized upload — leaves those bytes in the socket, where the next read
would parse them as a fresh request line. An early version did exactly this:

```
"POST /api/voices HTTP/1.1" 401 -
code 400, message Bad request syntax ('{}')
```

`_send()` now detects an unread body and sets `Connection: close`. This is the
class of bug that becomes request smuggling once a proxy is in front, so it is
fixed generally rather than per-route.

## Microphone access and TLS

Browsers expose `getUserMedia` only in a secure context. Serving over plain
HTTP to anything but `localhost` means no microphone at all. See
[hosting.md](hosting.md#the-microphone-problem-and-the-one-command-that-fixes-it).

A self-signed certificate is appropriate here. The traffic is already inside
WireGuard; the certificate exists to satisfy the browser's secure-context rule,
not to prove identity to a stranger.

Hybrid post-quantum key exchange is requested and falls back silently — it
needs OpenSSL 3.5+ and Python 3.14+, which a stock Python 3.12 does not have.
The startup banner reports what actually took effect.

## If you want to expose this more widely

Don't — but if you must:

1. Put a real reverse proxy (Caddy, nginx) in front with a real certificate.
2. Set `LANGPAL_TOKEN` to something long and random.
3. Add rate limiting at the proxy; LangPal has none.
4. Keep the bind address on loopback and let only the proxy reach it.
5. Understand that authenticated users can still make the server fetch
   arbitrary URLs. If that matters, put it on a network segment where it
   doesn't.
