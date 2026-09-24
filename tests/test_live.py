#!/usr/bin/env python3
"""End-to-end tests against a real model stack. Skipped unless you opt in.

These start a LangPal server on a spare port and drive a full turn through it:
transcribe a clip, stream a reply, synthesise the first sentence. They need
your LLM, STT and TTS endpoints to be up, and they will load models, so a cold
run can take a couple of minutes.

    # bash
    LANGPAL_LIVE=1 \\
    LANGPAL_LLM_URL=10.66.66.2:11435 \\
    LANGPAL_STT_URL=10.66.66.2:11435 \\
    LANGPAL_TTS_URL=127.0.0.1:8123 \\
    python -m unittest tests.test_live -v

Nothing is written to disk and no voices are registered, so running these
leaves your TTS voice registry untouched.
"""

import base64
import io
import json
import os
import socket
import sys
import threading
import unittest
import urllib.error
import urllib.request
import wave

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import langpal  # noqa: E402

LIVE = os.environ.get("LANGPAL_LIVE", "").lower() in ("1", "true", "yes")


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def tone_wav(seconds=1.5, rate=16000, freq=220.0):
    """A plain sine tone.

    Deliberately not speech: these tests check that the pipeline carries audio
    end to end, not that the ASR model is accurate. Whatever it hallucinates
    from a tone is fine as long as the plumbing holds.
    """
    import math
    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        frames = bytearray()
        for i in range(int(rate * seconds)):
            value = int(12000 * math.sin(2 * math.pi * freq * i / rate))
            frames += value.to_bytes(2, "little", signed=True)
        out.writeframes(bytes(frames))
    return buf.getvalue()


@unittest.skipUnless(LIVE, "set LANGPAL_LIVE=1 and point the URLs at a live stack")
class LiveStack(unittest.TestCase):
    """Drives a real LangPal server over HTTP."""

    @classmethod
    def setUpClass(cls):
        langpal.load_static()
        cls.port = free_port()
        cls.server = langpal.build_server("127.0.0.1", cls.port)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:%d" % cls.port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def post(self, path, payload, timeout=600):
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return resp.headers.get("Content-Type"), resp.read()

    def test_config_is_served(self):
        with urllib.request.urlopen(self.base + "/api/config", timeout=30) as resp:
            config = json.loads(resp.read())
        self.assertIn("defaults", config)
        self.assertIn("limits", config)
        # A secret must never appear in what the browser is handed.
        self.assertNotIn("api_key", json.dumps(config).lower())

    def test_health_reaches_every_upstream(self):
        _ctype, body = self.post("/api/health", {"settings": {}}, timeout=60)
        checks = json.loads(body)["checks"]
        for name in ("llm", "stt", "tts"):
            with self.subTest(endpoint=name):
                self.assertTrue(checks[name]["ok"],
                                "%s unreachable: %s" % (name, checks[name].get("error")))

    def test_transcription_round_trip(self):
        _ctype, body = self.post("/api/stt", {
            "audio_b64": base64.b64encode(tone_wav()).decode(),
            "settings": {"language": "English"},
        })
        result = json.loads(body)
        self.assertIn("text", result)
        # Whatever the model heard, the language-identification prefix must be
        # gone from `text` even though `raw` preserves it verbatim.
        self.assertNotIn("<asr_text>", result["text"])
        if "<asr_text>" in result["raw"]:
            self.assertFalse(result["text"].lower().startswith("language "))
            self.assertTrue(result["raw"].lower().startswith("language "))

    def test_chat_streams_sentences(self):
        request = urllib.request.Request(
            self.base + "/api/chat",
            data=json.dumps({
                "text": "Reply with exactly two short sentences about the sea.",
                "settings": {"language": "English", "max_tokens": 120},
            }).encode(),
            headers={"Content-Type": "application/json"})

        events = []
        with urllib.request.urlopen(request, timeout=600) as resp:
            self.assertTrue(resp.headers.get("Content-Type").startswith("text/event-stream"))
            name = ""
            for raw in resp:
                line = raw.decode("utf-8", "replace").rstrip("\n")
                if line.startswith("event:"):
                    name = line[6:].strip()
                elif line.startswith("data:"):
                    events.append((name, json.loads(line[5:])))

        kinds = [name for name, _payload in events]
        self.assertNotIn("error", kinds, events)
        self.assertIn("done", kinds)
        self.assertIn("sentence", kinds)

        sentences = [payload["text"] for name, payload in events if name == "sentence"]
        self.assertTrue(all(s.strip() for s in sentences))

    def test_synthesis_returns_playable_audio(self):
        ctype, audio = self.post("/api/tts", {
            "text": "This is a test of the speech pipeline.",
            "settings": {"language": "English"},
        })
        self.assertEqual(ctype, "audio/wav")
        self.assertEqual(audio[:4], b"RIFF")
        with wave.open(io.BytesIO(audio), "rb") as wav:
            self.assertGreater(wav.getnframes() / wav.getframerate(), 0.2)

    def test_stop_cancels_a_stale_epoch(self):
        # A fresh cookie-less request creates a session at epoch 0, so bumping
        # it and then presenting epoch 0 has to be refused.
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
        request = urllib.request.Request(
            self.base + "/api/stop", data=b"{}",
            headers={"Content-Type": "application/json"})
        with opener.open(request, timeout=30) as resp:
            epoch = json.loads(resp.read())["epoch"]
        self.assertGreaterEqual(epoch, 1)

        stale = urllib.request.Request(
            self.base + "/api/tts",
            data=json.dumps({"text": "this must not be synthesised",
                             "epoch": epoch - 1, "settings": {}}).encode(),
            headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            opener.open(stale, timeout=60)
        self.assertEqual(caught.exception.code, 409)

    def test_voice_listing_works(self):
        _ctype, body = self.post("/api/voices", {"settings": {}}, timeout=120)
        self.assertIsInstance(json.loads(body)["voices"], list)


if __name__ == "__main__":
    unittest.main()
