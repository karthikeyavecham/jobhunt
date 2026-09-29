"""Provider chain: retries, failover, and thinking-model output handling."""

import unittest

import requests

from jobhunt import llm


class FakeResponse(object):
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def chat_payload(content=None, reasoning=None):
    message = {"role": "assistant"}
    if content is not None:
        message["content"] = content
    if reasoning is not None:
        message["reasoning_content"] = reasoning
    return {"choices": [{"message": message}]}


class TestMessageExtraction(unittest.TestCase):
    def test_prefers_content(self):
        self.assertEqual(
            llm._extract_message_text({"content": "hello", "reasoning_content": "ignored"}),
            "hello",
        )

    def test_falls_back_to_reasoning_content(self):
        """LM Studio puts a thinking model's reply here and leaves content empty.

        qwen3.6 does exactly this — reading only `content` made a perfectly
        good reply look like a blank response.
        """
        self.assertEqual(
            llm._extract_message_text({"content": "", "reasoning_content": '{"a": 1}'}),
            '{"a": 1}',
        )

    def test_missing_both_is_empty(self):
        self.assertEqual(llm._extract_message_text({}), "")


class TestCleanJsonText(unittest.TestCase):
    def test_strips_think_blocks(self):
        self.assertEqual(
            llm.clean_json_text("<think>pondering</think>{\"a\": 1}"), '{"a": 1}'
        )

    def test_strips_code_fences(self):
        self.assertEqual(llm.clean_json_text('```json\n{"a": 1}\n```'), '{"a": 1}')

    def test_empty_is_safe(self):
        self.assertEqual(llm.clean_json_text(None), "")


class TestLocalRetries(unittest.TestCase):
    def setUp(self):
        self._orig_post = requests.post
        self.calls = []

    def tearDown(self):
        requests.post = self._orig_post

    def install(self, side_effects):
        def fake_post(url, **kwargs):
            self.calls.append(url)
            effect = side_effects[min(len(self.calls) - 1, len(side_effects) - 1)]
            if isinstance(effect, Exception):
                raise effect
            return effect
        requests.post = fake_post

    def test_retries_the_configured_number_of_times(self):
        """The Mac being asleep must be retried, not failed on first contact."""
        self.install([requests.ConnectionError("refused")])
        provider = llm.OpenAICompatProvider(attempts=5, backoff=0)

        with self.assertRaises(llm.ProviderUnavailable):
            provider.complete("sys", "user")

        self.assertEqual(len(self.calls), 5)

    def test_succeeds_on_a_later_attempt(self):
        self.install([
            requests.ConnectionError("refused"),
            requests.ConnectionError("refused"),
            FakeResponse(200, chat_payload(content="ok")),
        ])
        provider = llm.OpenAICompatProvider(attempts=5, backoff=0)

        self.assertEqual(provider.complete("sys", "user"), "ok")
        self.assertEqual(len(self.calls), 3)

    def test_client_error_fails_fast_without_retrying(self):
        """A bad schema will not fix itself — don't spend 5 attempts on it."""
        self.install([FakeResponse(400, {"error": {"message": "bad schema"}})])
        provider = llm.OpenAICompatProvider(attempts=5, backoff=0)

        with self.assertRaises(llm.ProviderFailed):
            provider.complete("sys", "user")
        self.assertEqual(len(self.calls), 1)

    def test_server_error_is_retried(self):
        self.install([FakeResponse(503, {"error": {"message": "busy"}})])
        provider = llm.OpenAICompatProvider(attempts=3, backoff=0)

        with self.assertRaises(llm.ProviderUnavailable):
            provider.complete("sys", "user")
        self.assertEqual(len(self.calls), 3)

    def test_model_still_loading_is_retried(self):
        self.install([
            FakeResponse(400, {"error": {"message": "Model is still loading"}}),
            FakeResponse(200, chat_payload(content="ok")),
        ])
        provider = llm.OpenAICompatProvider(attempts=3, backoff=0)

        self.assertEqual(provider.complete("sys", "user"), "ok")
        self.assertEqual(len(self.calls), 2)

    def test_out_of_memory_is_not_retried(self):
        """LM Studio's OOM message contains the word 'loading'.

        Seen live when a second model was left resident: "Model loading was
        stopped due to insufficient system resources". That never resolves on
        its own, so retrying it five times just delays the real error.
        """
        self.install([FakeResponse(400, {"error": {"message":
            'Failed to load model "qwen". Error: Model loading was stopped due to '
            'insufficient system resources.'}})])
        provider = llm.OpenAICompatProvider(attempts=5, backoff=0)

        with self.assertRaises(llm.ProviderFailed):
            provider.complete("sys", "user")
        self.assertEqual(len(self.calls), 1)


class TestTransientClassification(unittest.TestCase):
    def test_permanent_markers_win_over_transient_ones(self):
        self.assertFalse(llm._is_transient("Model loading was stopped, still loading"))

    def test_transient_markers(self):
        for text in ["Model is still loading", "please wait, warming up"]:
            self.assertTrue(llm._is_transient(text), text)

    def test_unknown_text_is_not_retried(self):
        self.assertFalse(llm._is_transient("some other problem"))
        self.assertFalse(llm._is_transient(""))


class StubProvider(llm.Provider):
    def __init__(self, name, model="m", raises=None, reply="{}"):
        self.name = name
        self.model = model
        self.raises = raises
        self.reply = reply
        self.calls = 0

    def complete(self, system, user, json_schema=None, max_tokens=1500, temperature=0.1):
        self.calls += 1
        if self.raises:
            raise self.raises
        return self.reply

    def test(self):
        if self.raises:
            return False, str(self.raises)
        return True, "OK"


class TestFallback(unittest.TestCase):
    def test_primary_is_used_when_available(self):
        primary = StubProvider("local", reply='{"ok": true}')
        secondary = StubProvider("anthropic")
        chain = llm.FallbackProvider(primary, secondary)

        self.assertEqual(chain.complete("s", "u"), '{"ok": true}')
        self.assertEqual(secondary.calls, 0)
        self.assertEqual(chain.model_label(), "local:m")

    def test_unreachable_primary_fails_over(self):
        """The Pi must still score when the Mac is asleep."""
        primary = StubProvider("local", raises=llm.ProviderUnavailable("mac asleep"))
        secondary = StubProvider("anthropic", model="claude-opus-5", reply='{"ok": 1}')
        chain = llm.FallbackProvider(primary, secondary)

        self.assertEqual(chain.complete("s", "u"), '{"ok": 1}')
        self.assertEqual(secondary.calls, 1)
        self.assertEqual(chain.model_label(), "anthropic:claude-opus-5")

    def test_bad_primary_output_does_not_fail_over(self):
        """Paying Claude to re-answer input the local model mangled is waste."""
        primary = StubProvider("local", raises=llm.ProviderFailed("garbage json"))
        secondary = StubProvider("anthropic")
        chain = llm.FallbackProvider(primary, secondary)

        with self.assertRaises(llm.ProviderFailed):
            chain.complete("s", "u")
        self.assertEqual(secondary.calls, 0)

    def test_no_secondary_propagates_the_error(self):
        primary = StubProvider("local", raises=llm.ProviderUnavailable("down"))
        chain = llm.FallbackProvider(primary, None)
        with self.assertRaises(llm.ProviderUnavailable):
            chain.complete("s", "u")

    def test_test_reports_fallback_readiness(self):
        primary = StubProvider("local", raises=llm.ProviderUnavailable("mac asleep"))
        secondary = StubProvider("anthropic")
        ok, message = llm.FallbackProvider(primary, secondary).test()
        self.assertTrue(ok)
        self.assertIn("fallback ready", message)


class TestCompleteJson(unittest.TestCase):
    def test_parses_clean_json(self):
        provider = StubProvider("local", reply='{"fit_score": 70}')
        self.assertEqual(provider.complete_json("s", "u", {}), {"fit_score": 70})

    def test_repairs_one_bad_reply(self):
        class Flaky(StubProvider):
            def complete(self, system, user, json_schema=None, max_tokens=1500, temperature=0.1):
                self.calls += 1
                return "not json" if self.calls == 1 else '{"fit_score": 70}'

        provider = Flaky("local")
        self.assertEqual(provider.complete_json("s", "u", {}), {"fit_score": 70})
        self.assertEqual(provider.calls, 2)

    def test_gives_up_after_the_repair_attempt(self):
        provider = StubProvider("local", reply="still not json")
        with self.assertRaises(llm.ProviderFailed):
            provider.complete_json("s", "u", {})


class TestBuildProvider(unittest.TestCase):
    def test_local_only_without_a_key(self):
        provider = llm.build_provider({}, {})
        self.assertIsInstance(provider, llm.OpenAICompatProvider)

    def test_chain_when_a_key_is_present(self):
        provider = llm.build_provider({}, {"anthropic.api_key": "sk-ant-test"})
        self.assertIsInstance(provider, llm.FallbackProvider)

    def test_fallback_can_be_disabled(self):
        provider = llm.build_provider(
            {"llm.fallback.enabled": False}, {"anthropic.api_key": "sk-ant-test"}
        )
        self.assertIsInstance(provider, llm.OpenAICompatProvider)

    def test_attempts_default_to_five(self):
        provider = llm.build_provider({}, {})
        self.assertEqual(provider.attempts, 5)


if __name__ == "__main__":
    unittest.main()
