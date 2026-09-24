#!/usr/bin/env python3
"""Unit tests for LangPal's pure logic. Standard library only.

    python -m unittest discover -s tests -v

These cover the parts where a bug is silent rather than loud: URL
normalisation, the Qwen3-ASR prefix parser, sentence chunking, settings
validation and WAV checking. The live end-to-end test against a real model
stack lives in tests/test_live.py and is skipped unless you opt in.
"""

import io
import json
import os
import sys
import unittest
import wave

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import langpal  # noqa: E402


def make_wav(seconds=1.0, rate=16000, channels=1):
    """Build a silent but structurally valid WAV in memory."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        out.setnchannels(channels)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(b"\0\0" * int(rate * seconds) * channels)
    return buf.getvalue()


class NormalizeBaseUrl(unittest.TestCase):
    def test_adds_scheme_to_bare_host_and_port(self):
        self.assertEqual(langpal.normalize_base_url("10.66.66.2:11435"),
                         "http://10.66.66.2:11435")

    def test_strips_trailing_slash_and_version(self):
        self.assertEqual(langpal.normalize_base_url("http://127.0.0.1:8123/v1/"),
                         "http://127.0.0.1:8123")

    def test_strips_a_full_endpoint_path(self):
        self.assertEqual(
            langpal.normalize_base_url("http://box:8123/v1/audio/speech"),
            "http://box:8123")
        self.assertEqual(
            langpal.normalize_base_url("http://box:8123/v1/chat/completions/"),
            "http://box:8123")

    def test_keeps_a_routing_prefix(self):
        # llama-swap's per-model route must survive, or voice listing breaks.
        self.assertEqual(
            langpal.normalize_base_url("http://h:11435/upstream/omnivoice/v1"),
            "http://h:11435/upstream/omnivoice")
        self.assertEqual(
            langpal.normalize_base_url("h:11435/upstream/omnivoice/v1/audio/voices"),
            "http://h:11435/upstream/omnivoice")

    def test_lowercases_scheme_and_host(self):
        self.assertEqual(langpal.normalize_base_url("HTTP://Box:8123"), "http://box:8123")

    def test_drops_default_ports(self):
        self.assertEqual(langpal.normalize_base_url("https://example.org:443/v1"),
                         "https://example.org")
        self.assertEqual(langpal.normalize_base_url("http://example.org:80"),
                         "http://example.org")

    def test_keeps_ipv6_brackets(self):
        self.assertEqual(langpal.normalize_base_url("http://[::1]:8123/v1"),
                         "http://[::1]:8123")

    def test_drops_query_and_fragment(self):
        self.assertEqual(langpal.normalize_base_url("http://box:8123/v1?a=b#c"),
                         "http://box:8123")

    def test_rejects_bad_input(self):
        for bad in ("", "   ", "ftp://box", "file:///etc/passwd",
                    "http://user:pw@box", "http://box:99999",
                    "http://box\r\nX-Evil: 1", "http://", "x" * 600):
            with self.subTest(bad=bad):
                with self.assertRaises(langpal.BadRequest):
                    langpal.normalize_base_url(bad)


class ParseAsrText(unittest.TestCase):
    def test_strips_the_llama_cpp_prefix(self):
        # Exactly what a live llama-server returned for Qwen3-ASR.
        text, lang = langpal.parse_asr_text(
            "language Turkish<asr_text>Merhaba, bu bir testtir.")
        self.assertEqual(text, "Merhaba, bu bir testtir.")
        self.assertEqual(lang, "Turkish")

    def test_passes_clean_text_through(self):
        text, lang = langpal.parse_asr_text("Just a normal transcript.")
        self.assertEqual(text, "Just a normal transcript.")
        self.assertEqual(lang, "")

    def test_handles_a_missing_language_name(self):
        text, lang = langpal.parse_asr_text("<asr_text>Hello there.")
        self.assertEqual(text, "Hello there.")
        self.assertEqual(lang, "")

    def test_rejects_a_nonsense_language(self):
        text, lang = langpal.parse_asr_text("language 12345<asr_text>Hi.")
        self.assertEqual(text, "Hi.")
        self.assertEqual(lang, "")

    def test_removes_a_stray_closing_tag(self):
        text, _ = langpal.parse_asr_text("language English<asr_text>Hi.</asr_text>")
        self.assertEqual(text, "Hi.")

    def test_empty(self):
        self.assertEqual(langpal.parse_asr_text(""), ("", ""))
        self.assertEqual(langpal.parse_asr_text(None), ("", ""))


class SplitSentences(unittest.TestCase):
    def test_holds_an_incomplete_sentence(self):
        sentences, rest = langpal.split_sentences("This is a long enough sentence")
        self.assertEqual(sentences, [])
        self.assertEqual(rest, "This is a long enough sentence")

    def test_emits_on_a_terminator_followed_by_space(self):
        sentences, rest = langpal.split_sentences("Merhaba, nasilsin? Ben iyiyim")
        self.assertEqual(sentences, ["Merhaba, nasilsin?"])
        self.assertEqual(rest, " Ben iyiyim")

    def test_does_not_split_a_decimal(self):
        sentences, rest = langpal.split_sentences("The answer is 3.5 and that is that")
        self.assertEqual(sentences, [])
        self.assertIn("3.5", rest)

    def test_does_not_split_an_abbreviation(self):
        sentences, _ = langpal.split_sentences("I spoke to Dr. Smith about it today. Yes")
        self.assertEqual(sentences, ["I spoke to Dr. Smith about it today."])

    def test_absorbs_punctuation_runs(self):
        sentences, _ = langpal.split_sentences("Are you serious?! I cannot believe it")
        self.assertEqual(sentences, ["Are you serious?!"])

    def test_holds_back_short_fragments(self):
        # "Ok." alone is not worth a TTS round trip; it merges with what follows.
        sentences, rest = langpal.split_sentences("Ok. Now we continue with more text")
        self.assertEqual(sentences, [])
        self.assertTrue(rest.startswith("Ok."))

    def test_final_flushes_everything(self):
        sentences, rest = langpal.split_sentences("Ok.", final=True)
        self.assertEqual(sentences, ["Ok."])
        self.assertEqual(rest, "")

    def test_cjk_needs_no_trailing_space(self):
        sentences, _ = langpal.split_sentences("今日はいい天気ですね。次")
        self.assertEqual(len(sentences), 1)

    def test_runaway_text_is_cut_at_a_space(self):
        buf = "word " * 200          # no punctuation at all
        sentences, rest = langpal.split_sentences(buf)
        self.assertTrue(sentences)
        for sentence in sentences:
            self.assertLessEqual(len(sentence), langpal.MAX_SENTENCE_CHARS)
        self.assertLess(len(rest), langpal.MAX_SENTENCE_CHARS)

    def test_short_opener_does_not_split_off(self):
        """A greeting shorter than the minimum must merge with what follows.

        Regression: this reply produced three sentences one-shot but two while
        streaming, because `final` was lowering the length bar mid-buffer.
        """
        reply = ("Bonjour ! Bien sur, je serais ravi de discuter avec toi "
                 "pour t'aider a progresser. De quoi aimerais-tu parler ?")
        one_shot, rest = langpal.split_sentences(reply, final=True)
        self.assertEqual(rest, "")
        self.assertEqual(len(one_shot), 2)
        self.assertTrue(one_shot[0].startswith("Bonjour ! Bien sur"))

        buf = ""
        streamed = []
        for i in range(0, len(reply), 4):
            buf += reply[i:i + 4]
            out, buf = langpal.split_sentences(buf)
            streamed.extend(out)
        out, buf = langpal.split_sentences(buf, final=True)
        streamed.extend(out)
        self.assertEqual([s.strip() for s in streamed],
                         [s.strip() for s in one_shot])

    def test_streaming_matches_one_shot(self):
        """Feeding text token by token must produce the same sentences."""
        full = ("Merhaba, bugun nasilsin? Ben cok iyiyim, tesekkur ederim. "
                "Hava guzel ve disari cikmak istiyorum!")
        buf = ""
        streamed = []
        for i in range(0, len(full), 3):
            buf += full[i:i + 3]
            out, buf = langpal.split_sentences(buf)
            streamed.extend(out)
        out, buf = langpal.split_sentences(buf, final=True)
        streamed.extend(out)

        one_shot, rest = langpal.split_sentences(full, final=True)
        self.assertEqual(rest, "")
        self.assertEqual([s.strip() for s in streamed], [s.strip() for s in one_shot])


class StripThink(unittest.TestCase):
    def test_removes_a_whole_block(self):
        out, inside = langpal.strip_think("<think>hmm</think>Hello", False)
        self.assertEqual(out, "Hello")
        self.assertFalse(inside)

    def test_carries_state_across_chunks(self):
        out, inside = langpal.strip_think("before<think>secret", False)
        self.assertEqual(out, "before")
        self.assertTrue(inside)
        out, inside = langpal.strip_think(" more secret</think>after", True)
        self.assertEqual(out, "after")
        self.assertFalse(inside)

    def test_passes_plain_text_through(self):
        out, inside = langpal.strip_think("nothing to hide", False)
        self.assertEqual(out, "nothing to hide")
        self.assertFalse(inside)


class Speakable(unittest.TestCase):
    def test_strips_markdown_and_collapses_whitespace(self):
        self.assertEqual(langpal.speakable("**Hello**   `there`\n\nfriend"),
                         "Hello there friend")

    def test_empty_when_only_markup(self):
        self.assertEqual(langpal.speakable("***"), "")


class ValidateSettings(unittest.TestCase):
    def test_defaults_survive_an_empty_payload(self):
        settings = langpal.validate_settings({})
        self.assertEqual(settings["llm_model"], langpal.DEFAULT_LLM_MODEL)
        self.assertIsNone(settings["temperature"])

    def test_blank_numbers_stay_unset(self):
        settings = langpal.validate_settings({"temperature": "", "max_tokens": None})
        self.assertIsNone(settings["temperature"])
        self.assertIsNone(settings["max_tokens"])

    def test_numbers_are_range_checked(self):
        for payload in ({"temperature": 5}, {"temperature": -1}, {"top_p": 2},
                        {"max_tokens": 0}, {"seed": -5}, {"temperature": True},
                        {"temperature": float("nan")}, {"temperature": "hot"}):
            with self.subTest(payload=payload):
                with self.assertRaises(langpal.BadRequest):
                    langpal.validate_settings(payload)

    def test_urls_are_normalized(self):
        settings = langpal.validate_settings({"llm_url": "box:11435/v1"})
        self.assertEqual(settings["llm_url"], "http://box:11435")

    def test_blank_url_falls_back_to_the_default(self):
        settings = langpal.validate_settings({"llm_url": "   "})
        self.assertEqual(settings["llm_url"],
                         langpal.normalize_base_url(langpal.DEFAULT_LLM_BASE_URL))

    def test_unknown_keys_are_ignored(self):
        settings = langpal.validate_settings({"nonsense": 1, "temperature": 0.5})
        self.assertEqual(settings["temperature"], 0.5)
        self.assertNotIn("nonsense", settings)

    def test_rejects_a_bad_voice_name(self):
        for bad in ("../../etc", "a b", "name/../x", "x" * 65):
            with self.subTest(bad=bad):
                with self.assertRaises(langpal.BadRequest):
                    langpal.validate_settings({"voice": bad})

    def test_accepts_a_good_voice_name(self):
        self.assertEqual(langpal.validate_settings({"voice": "tr_me-1.v2"})["voice"],
                         "tr_me-1.v2")

    def test_rejects_a_bad_language(self):
        with self.assertRaises(langpal.BadRequest):
            langpal.validate_settings({"language": "Turkish; DROP"})

    def test_reasoning_effort_is_an_enum(self):
        self.assertEqual(langpal.validate_settings({"reasoning_effort": "HIGH"})["reasoning_effort"],
                         "high")
        with self.assertRaises(langpal.BadRequest):
            langpal.validate_settings({"reasoning_effort": "extreme"})

    def test_system_prompt_length_is_capped(self):
        with self.assertRaises(langpal.BadRequest):
            langpal.validate_settings({"system_prompt": "x" * (langpal.MAX_SYSTEM_PROMPT_CHARS + 1)})

    def test_rejects_a_non_object(self):
        with self.assertRaises(langpal.BadRequest):
            langpal.validate_settings([1, 2, 3])


class LanguageResolution(unittest.TestCase):
    def test_auto_sends_nothing_to_stt(self):
        settings = langpal.validate_settings({"language": "auto"})
        self.assertEqual(langpal.stt_language_for(settings), "")

    def test_pinned_language_drives_both(self):
        settings = langpal.validate_settings({"language": "Turkish"})
        self.assertEqual(langpal.stt_language_for(settings), "Turkish")
        self.assertEqual(langpal.tts_language_for(settings), "Turkish")

    def test_auto_speaks_the_detected_language(self):
        settings = langpal.validate_settings({"language": "auto"})
        self.assertEqual(langpal.tts_language_for(settings, "French"), "French")

    def test_auto_falls_back_to_english(self):
        settings = langpal.validate_settings({"language": "auto"})
        self.assertEqual(langpal.tts_language_for(settings, ""), "English")

    def test_overrides_win(self):
        settings = langpal.validate_settings(
            {"language": "Turkish", "stt_language": "German", "tts_language": "English"})
        self.assertEqual(langpal.stt_language_for(settings), "German")
        self.assertEqual(langpal.tts_language_for(settings, "French"), "English")

    def test_a_detected_junk_language_is_ignored(self):
        settings = langpal.validate_settings({"language": "auto"})
        self.assertEqual(langpal.tts_language_for(settings, "!!!"), "English")


class ChatPayload(unittest.TestCase):
    def test_unset_fields_are_omitted(self):
        payload = langpal.build_chat_payload([], langpal.validate_settings({}))
        for key in ("temperature", "top_p", "max_tokens", "seed",
                    "reasoning_effort", "chat_template_kwargs"):
            self.assertNotIn(key, payload)
        self.assertTrue(payload["stream"])

    def test_set_fields_are_included(self):
        settings = langpal.validate_settings(
            {"temperature": 0.7, "max_tokens": 128, "reasoning_effort": "low",
             "enable_thinking": False})
        payload = langpal.build_chat_payload([], settings)
        self.assertEqual(payload["temperature"], 0.7)
        self.assertEqual(payload["max_tokens"], 128)
        self.assertEqual(payload["reasoning_effort"], "low")
        self.assertEqual(payload["chat_template_kwargs"], {"enable_thinking": False})

    def test_payload_is_json_serialisable(self):
        settings = langpal.validate_settings({"temperature": 0.5})
        json.dumps(langpal.build_chat_payload(
            [{"role": "user", "content": "hi"}], settings))


class BuildMessages(unittest.TestCase):
    def setUp(self):
        self.session = langpal.Session("sid")
        for i in range(30):
            self.session.history.append({"role": "user", "content": "u%d" % i})
            self.session.history.append({"role": "assistant", "content": "a%d" % i})

    def test_history_is_trimmed(self):
        settings = langpal.validate_settings({"history_messages": 4})
        messages = langpal.build_messages(self.session, "now", settings)
        # system + 4 history + this turn
        self.assertEqual(len(messages), 6)
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[-1], {"role": "user", "content": "now"})

    def test_zero_history_keeps_only_the_system_prompt(self):
        settings = langpal.validate_settings({"history_messages": 0})
        messages = langpal.build_messages(self.session, "now", settings)
        self.assertEqual(len(messages), 2)

    def test_blank_system_prompt_is_omitted(self):
        settings = langpal.validate_settings({"system_prompt": "", "history_messages": 0})
        messages = langpal.build_messages(self.session, "now", settings)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["role"], "user")


class WavValidation(unittest.TestCase):
    def test_accepts_a_real_wav(self):
        self.assertAlmostEqual(
            langpal.inspect_wav(make_wav(2.0), max_seconds=10), 2.0, places=3)

    def test_rejects_non_wav(self):
        with self.assertRaises(langpal.BadRequest):
            langpal.inspect_wav(b"not audio at all, not even close" * 4, max_seconds=10)

    def test_rejects_a_truncated_header(self):
        with self.assertRaises(langpal.BadRequest):
            langpal.inspect_wav(b"RIFF", max_seconds=10)

    def test_rejects_an_overlong_clip(self):
        with self.assertRaises(langpal.BadRequest):
            langpal.inspect_wav(make_wav(5.0), max_seconds=2)

    def test_rejects_an_empty_clip(self):
        with self.assertRaises(langpal.BadRequest):
            langpal.inspect_wav(make_wav(0.0), max_seconds=10)

    def test_base64_round_trip(self):
        import base64
        wav = make_wav(1.0)
        encoded = base64.b64encode(wav).decode()
        self.assertEqual(langpal.decode_wav_b64(encoded, max_seconds=10), wav)

    def test_rejects_bad_base64(self):
        with self.assertRaises(langpal.BadRequest):
            langpal.decode_wav_b64("not base64 !!!", max_seconds=10)
        with self.assertRaises(langpal.BadRequest):
            langpal.decode_wav_b64("", max_seconds=10)
        with self.assertRaises(langpal.BadRequest):
            langpal.decode_wav_b64(None, max_seconds=10)


class MultipartBuilder(unittest.TestCase):
    def test_body_is_well_formed(self):
        body, ctype = langpal._multipart_body(
            {"model": "qwen3-asr", "language": "Turkish", "skipme": ""},
            "file", "utterance.wav", b"RIFFdata", "audio/wav")
        boundary = ctype.split("boundary=", 1)[1].encode()
        self.assertTrue(body.startswith(b"--" + boundary))
        self.assertTrue(body.endswith(b"--" + boundary + b"--\r\n"))
        self.assertIn(b'name="model"', body)
        self.assertIn(b"qwen3-asr", body)
        self.assertIn(b'filename="utterance.wav"', body)
        self.assertIn(b"RIFFdata", body)
        # Empty values are skipped rather than sent as an empty field.
        self.assertNotIn(b"skipme", body)


class ScanDefaultVoices(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp(prefix="langpal-voices-")
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def _write(self, name, data=b"x"):
        with open(os.path.join(self.dir, name), "wb") as fh:
            fh.write(data)

    def test_pairs_wav_with_txt(self):
        self._write("tr_ayse.wav")
        self._write("tr_ayse.txt", "Merhaba".encode())
        self._write("en_bob.wav")
        found = langpal.scan_default_voices(self.dir)
        names = {name: txt for name, _wav, txt in found}
        self.assertEqual(set(names), {"tr_ayse", "en_bob"})
        self.assertIsNotNone(names["tr_ayse"])
        self.assertIsNone(names["en_bob"])

    def test_ignores_other_extensions(self):
        self._write("notes.txt")
        self._write("song.mp3")
        self.assertEqual(langpal.scan_default_voices(self.dir), [])

    def test_skips_an_unusable_name(self):
        self._write("bad name!.wav")
        self.assertEqual(langpal.scan_default_voices(self.dir), [])

    def test_missing_directory_is_not_an_error(self):
        self.assertEqual(langpal.scan_default_voices(os.path.join(self.dir, "nope")), [])


class SessionStoreTest(unittest.TestCase):
    def test_unknown_id_gets_a_fresh_session(self):
        store = langpal.SessionStore(limit=4)
        session, created = store.get_or_create("i-made-this-up")
        self.assertTrue(created)
        self.assertNotEqual(session.sid, "i-made-this-up")

    def test_known_id_is_reused(self):
        store = langpal.SessionStore(limit=4)
        first, _ = store.get_or_create("")
        again, created = store.get_or_create(first.sid)
        self.assertFalse(created)
        self.assertIs(first, again)

    def test_evicts_past_the_limit(self):
        store = langpal.SessionStore(limit=3)
        for _ in range(10):
            store.get_or_create("")
        self.assertLessEqual(len(store._sessions), 3)


class StaticRoutes(unittest.TestCase):
    def test_every_route_points_at_a_real_file(self):
        for route, (filename, ctype) in langpal.STATIC_FILES.items():
            with self.subTest(route=route):
                path = os.path.join(langpal.WEB_DIR, filename)
                self.assertTrue(os.path.isfile(path), path)
                self.assertIn("charset", ctype)

    def test_load_static_reads_them_all(self):
        langpal.STATIC_CACHE.clear()
        langpal.load_static()
        for _route, (filename, _ctype) in langpal.STATIC_FILES.items():
            self.assertIn(filename, langpal.STATIC_CACHE)
            self.assertGreater(len(langpal.STATIC_CACHE[filename]), 0)


class LlamaSwapRouting(unittest.TestCase):
    """llama-swap needs a model id on requests that have no body to route on."""

    def setUp(self):
        self.settings = langpal.validate_settings(
            {"tts_url": "http://10.66.66.2:11435", "tts_model": "omnivoice"})

    def test_bodyless_urls_carry_the_model(self):
        url = langpal._tts_routed_url(self.settings, langpal.TTS_VOICES_PATH)
        self.assertEqual(url, "http://10.66.66.2:11435/v1/audio/voices?model=omnivoice")

    def test_model_name_is_percent_encoded(self):
        settings = langpal.validate_settings(
            {"tts_url": "http://h:1", "tts_model": "my model/v2"})
        url = langpal._tts_routed_url(settings, langpal.HEALTH_PATH)
        self.assertEqual(url, "http://h:1/health?model=my%20model%2Fv2")
        self.assertNotIn(" ", url)

    def test_register_payload_carries_the_model(self):
        """The register POST has a body, so llama-swap routes on the model key."""
        captured = {}

        def fake(url, payload, **kwargs):
            captured["url"] = url
            captured["payload"] = payload
            return {"status": "registered"}

        original = langpal._request_json
        langpal._request_json = fake
        try:
            langpal.register_voice("v", "text", b"RIFF", self.settings)
        finally:
            langpal._request_json = original

        self.assertEqual(captured["payload"]["model"], "omnivoice")
        self.assertEqual(captured["payload"]["name"], "v")

    def test_delete_explains_a_404_from_llama_swap(self):
        def fake(url, payload, **kwargs):
            raise langpal.UpstreamError("HTTP 404 from %s: 404 page not found" % url,
                                        status=404)

        original = langpal._request_json
        langpal._request_json = fake
        try:
            with self.assertRaises(langpal.UpstreamError) as caught:
                langpal.delete_voice("v", self.settings)
        finally:
            langpal._request_json = original

        message = str(caught.exception)
        self.assertIn("upstream/omnivoice", message)
        self.assertIn("no route", message)

    def test_delete_does_not_swallow_other_failures(self):
        def fake(url, payload, **kwargs):
            raise langpal.UpstreamError("cannot reach host", status=None)

        original = langpal._request_json
        langpal._request_json = fake
        try:
            with self.assertRaises(langpal.UpstreamError) as caught:
                langpal.delete_voice("v", self.settings)
        finally:
            langpal._request_json = original
        self.assertEqual(str(caught.exception), "cannot reach host")


class UpstreamErrorStatus(unittest.TestCase):
    def test_status_defaults_to_none(self):
        self.assertIsNone(langpal.UpstreamError("boom").status)

    def test_status_is_kept(self):
        self.assertEqual(langpal.UpstreamError("boom", status=502).status, 502)


class BargeInThresholds(unittest.TestCase):
    """The barge-in maths, checked against constants read out of web/app.js.

    These four numbers decide whether the assistant talks over itself. Getting
    them wrong is not a crash -- playback just stops a word into every reply,
    which is how the bug that prompted this test presented. The constants are
    parsed from the JavaScript rather than copied, so the test cannot drift
    away from what actually ships.
    """

    #                        room    echo   the user's voice
    SCENARIOS = (
        ("headphones, quiet room",   0.002, 0.003, 0.10),
        ("laptop speakers, normal",  0.003, 0.040, 0.15),
        ("speakers loud, poor AEC",  0.004, 0.090, 0.20),
        ("speakers very loud",       0.004, 0.160, 0.25),
        ("noisy room, headphones",   0.020, 0.005, 0.15),
    )

    @classmethod
    def setUpClass(cls):
        import re
        source = io.open(os.path.join(langpal.WEB_DIR, "app.js"),
                         encoding="utf-8").read()
        cls.const = {}
        for name in ("NOISE_FLOOR_MULT", "MIN_RMS", "BARGE_MULT", "ECHO_MULT"):
            match = re.search(r"const %s = ([0-9.]+);" % name, source)
            assert match, "%s is missing from app.js" % name
            cls.const[name] = float(match.group(1))

    def threshold(self, room_rms, echo_rms, sensitivity=5):
        """Mirrors currentThreshold() in app.js."""
        sens_mul = 0.85 ** (sensitivity - 5)
        base = max(room_rms * self.const["NOISE_FLOOR_MULT"],
                   self.const["MIN_RMS"]) * sens_mul
        # echoFloor is peak-held, so it settles at the echo's peak level.
        return max(base * self.const["BARGE_MULT"],
                   echo_rms * self.const["ECHO_MULT"])

    def test_the_assistant_never_interrupts_itself(self):
        for name, room, echo, _user in self.SCENARIOS:
            with self.subTest(scenario=name):
                self.assertLess(
                    echo, self.threshold(room, echo),
                    "%s: echo %.3f clears the bar, so playback cuts itself off"
                    % (name, echo))

    def test_the_user_can_still_interrupt(self):
        for name, room, echo, user in self.SCENARIOS:
            with self.subTest(scenario=name):
                self.assertGreater(
                    user, self.threshold(room, echo),
                    "%s: speech at %.3f cannot be heard over the echo"
                    % (name, user))

    def test_raising_sensitivity_lowers_the_bar(self):
        room, echo = 0.003, 0.003
        bars = [self.threshold(room, echo, s) for s in (1, 5, 10)]
        self.assertGreater(bars[0], bars[1])
        self.assertGreater(bars[1], bars[2])

    def test_echo_floor_dominates_when_playback_is_loud(self):
        """The whole point: a loud speaker must raise the bar, not be ignored."""
        quiet = self.threshold(0.003, 0.003)
        loud = self.threshold(0.003, 0.160)
        self.assertGreater(loud, quiet * 3)

    def _timing(self, name):
        import re
        source = io.open(os.path.join(langpal.WEB_DIR, "app.js"),
                         encoding="utf-8").read()
        return float(re.search(r"const %s = ([0-9.]+);" % name, source).group(1))

    def test_interrupting_is_stricter_than_starting_a_turn(self):
        """A false interruption costs more than a slow one, so the bar is higher."""
        self.assertGreater(self._timing("BARGE_START_MS"),
                           self._timing("SPEECH_START_MS") * 4)

    def test_preroll_covers_the_confirmation_window(self):
        """The regression this guards against truncates every interruption.

        beginUtterance() seeds the recording from the pre-roll ring. If the
        ring is shorter than the time taken to decide speech was speech, the
        opening words are already gone -- and nothing crashes, the transcript
        just quietly starts mid-sentence.
        """
        import re
        source = io.open(os.path.join(langpal.WEB_DIR, "app.js"),
                         encoding="utf-8").read()
        # Derived in the source, so check the expression, not a literal.
        self.assertRegex(
            source,
            r"PREROLL_CAP_MS = Math\.max\(PREROLL_MS, BARGE_START_MS\) \+ \d+")
        # assertTrue, not assertIn: a failed assertIn would print the whole
        # 40 kB of app.js as the "not found in" haystack.
        self.assertTrue(
            "msToSamples(PREROLL_CAP_MS, rate)" in source,
            "the pre-roll ring must be trimmed to PREROLL_CAP_MS, not PREROLL_MS, "
            "or an interruption loses its opening words")

    def test_interruption_stays_responsive(self):
        """Grace plus confirmation is what the user actually waits through."""
        total = self._timing("BARGE_GRACE_MS") + self._timing("BARGE_START_MS")
        self.assertGreaterEqual(self._timing("BARGE_GRACE_MS"), 100)
        self.assertLessEqual(total, 2000)

class ConfigExposure(unittest.TestCase):
    def test_no_secret_leaks_into_the_client_payload(self):
        blob = json.dumps(langpal.default_settings())
        for secret in ("LANGPAL_TOKEN", "api_key", "LLM_API_KEY", "password"):
            self.assertNotIn(secret, blob)
        for key in ("llm_api_key", "stt_api_key", "tts_api_key", "access_token"):
            self.assertNotIn(key, langpal.default_settings())


if __name__ == "__main__":
    unittest.main()
