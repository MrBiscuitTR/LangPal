# Hosting LangPal

## Running it

```bash
python langpal.py                                  # http://127.0.0.1:8080
python langpal.py --host 10.66.66.2 --port 8080    # reachable over WireGuard
python langpal.py --cert cert.pem --key key.pem    # HTTPS
```

| Flag | Environment variable | Meaning |
| --- | --- | --- |
| `--host` | `LANGPAL_HOST` | Bind address. Default `127.0.0.1`. |
| `--port` | `LANGPAL_PORT` | Bind port. Default `8080`. |
| `--cert` | `LANGPAL_TLS_CERT` | TLS certificate (PEM). |
| `--key` | `LANGPAL_TLS_KEY` | TLS private key (PEM). |
| `--voices-dir` | `LANGPAL_VOICES_DIR` | Clips to register at startup. |
| `--no-default-voices` | `LANGPAL_REGISTER_DEFAULT_VOICES=false` | Skip that bootstrap. |
| `--dev` | — | Re-read `web/` on every request instead of caching it. |

`--cert` and `--key` must be given together. Settings from `.env.local` are
read first; real environment variables override the file, and command-line
flags override both.

## The microphone problem, and the one command that fixes it

**Browsers only expose `getUserMedia` in a "secure context."** That means
HTTPS, or a host of `localhost` / `127.0.0.1`. A private address like
`http://10.66.66.2:8080` does **not** qualify — the microphone API is simply
absent from the page. This is browser policy and no server-side choice affects
it.

So if you want to use LangPal from anything other than the machine it runs on,
you need a certificate. Self-signed is fine.

### Generating one

The certificate must carry a **`subjectAltName`** listing the address you
actually type into the browser. Certificates with only a `CN` have been
rejected by Chrome, Firefox and Safari since 2017, so the common one-line
recipe you will find elsewhere does not work:

```bash
# This does NOT work -- no subjectAltName.
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 \
  -keyout key.pem -out cert.pem -sha256 -days 365 -nodes -subj "/CN=localhost"
```

Use this instead, with your own WireGuard address:

```bash
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 \
  -keyout key.pem -out cert.pem -sha256 -days 365 -nodes \
  -subj "/CN=langpal" \
  -addext "subjectAltName=IP:10.66.66.2,IP:127.0.0.1,DNS:localhost" \
  -addext "basicConstraints=critical,CA:FALSE" \
  -addext "keyUsage=critical,digitalSignature" \
  -addext "extendedKeyUsage=serverAuth"
```

Check it before you rely on it:

```bash
openssl x509 -in cert.pem -noout -subject -dates -ext subjectAltName
```

You want to see your IP in the `X509v3 Subject Alternative Name` line. If that
extension is missing, the browser will reject the certificate no matter what
the subject says.

Keep `-days` at 365 or less: iOS and macOS reject server certificates valid for
more than 825 days.

Then point LangPal at it:

```
LANGPAL_TLS_CERT=C:\Users\you\LangPal\cert.pem
LANGPAL_TLS_KEY=C:\Users\you\LangPal\key.pem
```

### Trusting it on each device

The certificate is self-signed, so every device warns once.

- **Desktop Chrome / Edge / Firefox** — *Advanced* → *Proceed*. Sticks for the
  session; usually longer.
- **Android Chrome** — *Advanced* → *Proceed to … (unsafe)*.
- **iOS Safari** — *Show Details* → *visit this website* → *Visit*. If Safari
  refuses to offer that, install the certificate as a profile
  (Settings → General → VPN & Device Management) and then enable it under
  Settings → General → About → Certificate Trust Settings.

A warning you have to click through is not a weakness here: the traffic is
already inside WireGuard, and the certificate is only proving the box to
itself.

### If the microphone is still refused: make the certificate trusted

A self-signed certificate you click past leaves the origin in a half-trusted
state. Browsers treat such an origin as a secure context for *loading* the page
but refuse permission-gated APIs on it, so `getUserMedia` fails with:

> The request is not allowed by the user agent or the platform in the current
> context.

LangPal turns that into a message naming the three things that cause it. Check
them in this order:

1. **The permission prompt.** Dismissed, or blocked earlier for this origin.
   Click the padlock in the address bar and clear the microphone permission,
   then reload.
2. **Windows.** Settings → Privacy & security → Microphone. Both *Microphone
   access* and *Let desktop apps access your microphone* must be on, or every
   browser on the machine is blocked at the OS level.
3. **Certificate trust.** Fixed below.

To fix trust properly, issue the server certificate from your own small CA and
trust the CA once per device. Then there is no warning to click through, and
the origin is fully trusted:

```bash
# 1. A CA, valid for 10 years. Keep ca.key private; it can mint certificates
#    that every device you trust it on will accept.
openssl req -x509 -new -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 \
  -keyout ca.key -out ca.pem -sha256 -days 3650 -nodes \
  -subj "/CN=LangPal local CA" \
  -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
  -addext "keyUsage=critical,keyCertSign,cRLSign"

# 2. A key and signing request for the server.
openssl req -new -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 \
  -keyout key.pem -out server.csr -nodes -subj "/CN=langpal"

# 3. The server certificate, signed by the CA. 398 days keeps Safari happy.
openssl x509 -req -in server.csr -CA ca.pem -CAkey ca.key -CAcreateserial \
  -out cert.pem -days 398 -sha256 \
  -extfile <(printf "subjectAltName=IP:10.66.66.2,IP:127.0.0.1,DNS:localhost\nbasicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=serverAuth")
```

> The `<(...)` process substitution needs bash. In PowerShell, write those four
> lines to `ext.cnf` and pass `-extfile ext.cnf` instead.

Verify before trusting it:

```bash
openssl verify -CAfile ca.pem cert.pem          # expect: cert.pem: OK
openssl x509 -in cert.pem -noout -ext subjectAltName
```

LangPal keeps using `cert.pem` and `key.pem`, so no configuration changes. Now
trust **`ca.pem`** — not the server certificate — on each device:

- **Windows** — double-click `ca.pem` → Install Certificate → Local Machine →
  Place all certificates in the following store → *Trusted Root Certification
  Authorities*. Chrome and Edge use this store.
- **Firefox** — has its own store: Settings → Privacy & Security → Certificates
  → View Certificates → Authorities → Import → tick *Trust this CA to identify
  websites*.
- **Android** — Settings → Security → Encryption & credentials → Install a
  certificate → CA certificate.
- **iOS / iPadOS** — AirDrop or email `ca.pem` to the device, install the
  profile (Settings → General → VPN & Device Management), then **also** enable
  it under Settings → General → About → Certificate Trust Settings. iOS ignores
  the profile until you do that second step.

`ca.key` never leaves your machine, and `.gitignore` already excludes `*.pem`
and `*.key` so none of this can be committed by accident.

### The alternative, for desktop Chrome only

If you would rather not deal with certificates and only use desktop Chrome or
Edge, you can mark the origin as trustworthy per device:

```
chrome://flags/#unsafely-treat-insecure-origin-as-secure
```

Add `http://10.66.66.2:8080`, enable, relaunch. Android Chrome has the same
flag. Firefox and Safari do not, and iOS has no equivalent at all — those
devices need HTTPS.

## Post-quantum key exchange

Not available on a stock Python. Hybrid `X25519MLKEM768` needs **OpenSSL 3.5 or
newer**, and naming a group from Python needs `SSLContext.set_groups()`, which
arrived in **Python 3.14**. Python 3.12 on Windows links OpenSSL 3.0.x and has
no such method.

`harden_tls_context()` asks for the hybrid group anyway and falls back quietly,
so the moment you are on a Python that can do it, you get it. The startup
banner tells you which key exchange actually took effect:

```
TLS enabled, key exchange: OpenSSL defaults (set_groups needs Python 3.14+; you have 3.12.3)
```

In practice this costs you nothing here — the traffic is already wrapped in
WireGuard, which is not post-quantum either unless you have configured a
pre-shared key.

## Access over WireGuard

Bind LangPal to the tunnel address, not `0.0.0.0`:

```
LANGPAL_HOST=10.66.66.2
LANGPAL_PORT=8080
```

Binding to the tunnel interface means the listener is not exposed on your
Wi-Fi or Ethernet at all, which is a stronger guarantee than a firewall rule.

Set a password as well, so a peer's compromised phone is not automatically a
compromised LangPal:

```
LANGPAL_TOKEN=some-long-random-string
```

With it set, every request needs the password; the UI shows a small unlock
screen and stores the result in an `HttpOnly` cookie for 30 days. Without it,
anyone who can reach the address can use it, and the startup banner says so.

## Running it in the background

Windows, no console window:

```
pythonw langpal.py
```

To start it with the machine, make a shortcut to `pythonw langpal.py` in
`shell:startup`, or register a Task Scheduler task with *Run whether user is
logged on or not*.

Redirecting output to a file works; the banner and the voice bootstrap flush as
they go, so the log is useful while the process is still running.

## Model stack

LangPal does not manage models. It expects them to be there. A typical
[llama-swap](https://github.com/mostlygeek/llama-swap) setup keeping all three
resident together:

```yaml
models:
  "gemma4-26b-a4b":
    cmd: >-
      llama-server --model /models/Gemma4-26B-A4B-Q4_K_M.gguf
      --host 127.0.0.1 --port ${PORT}
      -ngl all -fa on -ctk q8_0 -ctv q8_0 -c 65536
      --parallel 1 --jinja --reasoning off
    proxy: "http://127.0.0.1:${PORT}"
    ttl: 600

  "qwen3-asr":
    cmd: >-
      llama-server --model /models/Qwen3-ASR-1.7B-Q8_0.gguf
      --mmproj /models/mmproj-Qwen3-ASR-1.7B-Q8_0.gguf
      --host 127.0.0.1 --port ${PORT}
      -ngl 99 -fa on -c 8192 --threads 16 --parallel 1
    proxy: "http://127.0.0.1:${PORT}"
    ttl: 600

  "omnivoice":
    cmd: docker compose -f /path/omnivoice-docker/docker-compose.yml up
    proxy: "http://127.0.0.1:8123"
    ttl: 0
    checkEndpoint: "/health"

groups:
  "vocal-agent":
    swap: false       # keep all three loaded at once
    exclusive: true
    members: ["gemma4-26b-a4b", "qwen3-asr", "omnivoice"]
```

`swap: false` matters. Without it, every turn evicts and reloads a model and
the conversation stalls for tens of seconds each time.

### Which TTS URL to use

llama-swap picks a backend from the `model` field in the **request body**, so
the bodyless voice-registry calls need help. LangPal appends `?model=<name>` to
them automatically, which covers listing and registering on the plain main
port.

One call cannot be rescued that way: `DELETE /v1/audio/voices/<name>` has no
route pattern in llama-swap at all, so removing a voice fails on the main port
however it is routed. Use the per-model prefix and everything works:

```
LANGPAL_TTS_URL=http://10.66.66.2:11435/upstream/omnivoice
```

That form also triggers the model load, which pointing straight at the
container (`http://127.0.0.1:8123`) does not. The full matrix is in
[upstream-apis.md](upstream-apis.md#what-works-on-which-url).

LangPal's URL cleanup preserves an `/upstream/<model>` prefix precisely so this
stays usable.

## What it costs

Measured on the reference stack, models already resident:

| Step | Time |
| --- | --- |
| Transcribing a 2.2 s clip | ~0.5 s |
| First LLM token | ~0.25 s |
| First sentence ready for speech | ~0.35 s |
| Synthesis | ~0.27 × the length of the audio produced |

Synthesis running at roughly a quarter of real time is what makes the reply
sound continuous: the next sentence is always ready before the current one
finishes. A cold start is different — llama-swap has to load the model, which
took about 10 s for the first token in testing.
