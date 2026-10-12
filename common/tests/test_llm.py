"""common.llm: the backend contract, offline. The API backend runs against a fake SDK module that
mirrors the anthropic package's exception classes and response objects; the CLI backend runs
against tools/fake_claude.py and a misbehaving stand-in."""
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

REPO = Path(__file__).resolve().parents[2]
os.environ.setdefault("LJP_WORKSPACE", tempfile.mkdtemp(prefix="ljp_llm_"))
sys.path.insert(0, str(REPO))
from common import llm  # noqa: E402


def fake_sdk():
    class APIError(Exception):
        def __init__(self, message="error", status_code=None):
            super().__init__(message)
            self.message, self.status_code = message, status_code

    class APIStatusError(APIError): pass
    class APIConnectionError(APIError): pass
    class APITimeoutError(APIConnectionError): pass
    class BadRequestError(APIStatusError): pass
    class AuthenticationError(APIStatusError): pass
    class PermissionDeniedError(APIStatusError): pass
    class NotFoundError(APIStatusError): pass
    class RateLimitError(APIStatusError): pass

    class Anthropic:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
    return NS(**{c.__name__: c for c in (APIError, APIStatusError, APIConnectionError, APITimeoutError,
                                         BadRequestError, AuthenticationError, PermissionDeniedError,
                                         NotFoundError, RateLimitError, Anthropic)})


def message(text='{"ok": true}', model="claude-opus-5-5", stop="end_turn", blocks=None, **usage):
    u = {"input_tokens": 3, "cache_creation_input_tokens": 1000, "cache_read_input_tokens": 0,
         "output_tokens": 200, **usage}
    return NS(content=blocks or [NS(type="text", text=text)], model=model, stop_reason=stop,
              stop_details=NS(category="cyber") if stop == "refusal" else None, usage=NS(**u))


class FakeClient:
    def __init__(self, reply):
        self.reply, self.requests, self.timeouts = reply, [], []
        self.messages = NS(create=self._create)
        self.beta = NS(messages=NS(create=self._create))

    def with_options(self, timeout):
        self.timeouts.append(timeout)
        return self

    def _create(self, **params):
        self.requests.append(params)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


class ApiBackendTests(unittest.TestCase):
    def backend(self, reply, **kw):
        self.sdk = fake_sdk()
        self.client = FakeClient(reply)
        return llm.AnthropicApiBackend(sdk=self.sdk, client=self.client, **kw)

    def test_request_shape(self):
        b = self.backend(message(), effort="low")
        b.complete("SYSTEM", "USER", timeout=30)
        req = self.client.requests[0]
        self.assertEqual(req["model"], "claude-opus-5-5")
        self.assertEqual(req["system"], [{"type": "text", "text": "SYSTEM",
                                          "cache_control": {"type": "ephemeral", "ttl": "1h"}}])
        self.assertEqual(req["messages"], [{"role": "user", "content": "USER"}])
        self.assertEqual(req["output_config"], {"effort": "low"})
        self.assertNotIn("tools", req)
        self.assertNotIn("fallbacks", req)
        self.assertEqual(self.client.timeouts, [30])

    def test_sdk_retries_are_off(self):
        sdk = fake_sdk()
        b = llm.AnthropicApiBackend(sdk=sdk)
        self.assertEqual(b.client.kwargs, {"max_retries": 0})

    def test_usage_and_list_price_cost(self):
        r = self.backend(message(cache_read_input_tokens=12000)).complete("S", "U", 30)
        self.assertEqual(r.text, '{"ok": true}')
        self.assertEqual(r.usage.input_side_tokens, 13003)
        # 3*4 + 1000*8 + 12000*0.20 + 200*20 per million
        self.assertAlmostEqual(r.usage.cost_usd, (12 + 8000 + 2400 + 4000) / 1e6)
        self.assertEqual(r.usage.models, ["claude-opus-5-5"])

    def test_error_classes(self):
        sdk = fake_sdk()
        cases = [(sdk.AuthenticationError("bad key", 401), llm.FatalError),
                 (sdk.PermissionDeniedError("no", 403), llm.FatalError),
                 (sdk.NotFoundError("no such model", 404), llm.FatalError),
                 (sdk.BadRequestError("prompt is too long: 1200000 tokens", 400), llm.InputError),
                 (sdk.BadRequestError("unknown field", 400), llm.FatalError),
                 (sdk.RateLimitError("slow down", 429), llm.TransientError),
                 (sdk.APITimeoutError("timeout"), llm.TransientError),
                 (sdk.APIConnectionError("reset"), llm.TransientError),
                 (sdk.APIStatusError("overloaded", 529), llm.TransientError)]
        for exc, expected in cases:
            with self.subTest(exc=type(exc).__name__):
                b = llm.AnthropicApiBackend(sdk=sdk, client=FakeClient(exc))
                with self.assertRaises(expected):
                    b.complete("S", "U", 30)

    def test_stop_reasons_and_tool_use(self):
        with self.assertRaises(llm.InputError):
            self.backend(message(stop="refusal")).complete("S", "U", 30)
        with self.assertRaises(llm.TransientError):
            self.backend(message(stop="max_tokens")).complete("S", "U", 30)
        with self.assertRaises(llm.FatalError):
            self.backend(message(blocks=[NS(type="tool_use")])).complete("S", "U", 30)

    def test_fallbacks_are_opt_in(self):
        b = self.backend(message(), fallbacks=True)
        b.complete("S", "U", 30)
        req = self.client.requests[0]
        self.assertEqual(req["fallbacks"], "default")
        self.assertEqual(req["betas"], ["server-side-fallback-2026-07-01"])

    def test_missing_sdk_is_a_clear_fatal_error(self):
        if "anthropic" in sys.modules or any((Path(p) / "anthropic").exists() for p in sys.path if p):
            self.skipTest("anthropic is installed")
        with self.assertRaises(llm.FatalError):
            llm.AnthropicApiBackend()


class FactoryTests(unittest.TestCase):
    def test_specs(self):
        os.environ["CLAUDE_BIN"] = str(REPO / "tools" / "fake_claude.py")
        os.chmod(REPO / "tools" / "fake_claude.py", 0o755)
        with llm.make_backend("cli:some-model") as b:
            self.assertEqual((b.name, b.model), ("claude-cli", "some-model"))
        with self.assertRaises(ValueError):
            llm.make_backend("openai")

    def test_list_price_unknown_model(self):
        self.assertIsNone(llm.list_price_cost("unknown", llm.Usage(output_tokens=1)))

    def test_list_price_sonnet_cache_read(self):
        u = llm.Usage(input_tokens=1_000_000, cache_creation_input_tokens=1_000_000,
                      cache_read_input_tokens=1_000_000, output_tokens=1_000_000)
        self.assertAlmostEqual(llm.list_price_cost("claude-sonnet-5-5", u), 2.00 + 4.00 + 0.10 + 10.00)


class CliBackendTests(unittest.TestCase):
    def setUp(self):
        os.chmod(REPO / "tools" / "fake_claude.py", 0o755)

    def test_fake_cli_round_trip(self):
        system = 'Role\n\n<FILE path="instructions/title_screening.md">\nrules\n</FILE>\n'
        user = 'go\n\n<TITLE_INPUT>\n[{"source_key": "a", "position": "Data Analyst"}]\n</TITLE_INPUT>'
        with llm.ClaudeCliBackend(claude_bin=str(REPO / "tools" / "fake_claude.py")) as b:
            r = b.complete(system, user, timeout=30)
        self.assertEqual(json.loads(r.text), {"accepted_ids": ["a"], "rejected_ids": []})
        self.assertEqual(r.usage.models, ["fake-model"])

    def write_cli(self, envelope: dict, rc: int = 0) -> str:
        path = Path(tempfile.mkdtemp()) / "cli.py"
        path.write_text(f"#!{sys.executable}\nimport sys, json\nsys.stdin.read()\n"
                        f"print(json.dumps({envelope!r}))\nsys.exit({rc})\n")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return str(path)

    def test_multi_turn_reply_violates_the_contract(self):
        cli = self.write_cli({"type": "result", "subtype": "success", "is_error": False, "num_turns": 2,
                              "result": "{}", "usage": {}, "modelUsage": {"m": {}}})
        with llm.ClaudeCliBackend(claude_bin=cli) as b, self.assertRaises(llm.FatalError):
            b.complete("S", "U", 30)

    def test_cli_error_classes(self):
        cases = [({"is_error": True, "result": "Not logged in · Please run /login"}, 1, llm.FatalError),
                 ({"is_error": True, "result": "Prompt is too long"}, 1, llm.InputError),
                 ({"is_error": True, "result": "API Error: 529 overloaded"}, 1, llm.TransientError),
                 ({"is_error": True, "api_error_status": 401, "result": "Invalid key"}, 1, llm.FatalError)]
        for env, rc, expected in cases:
            with self.subTest(env=env), llm.ClaudeCliBackend(claude_bin=self.write_cli(env, rc)) as b:
                with self.assertRaises(expected):
                    b.complete("S", "U", 30)


if __name__ == "__main__":
    unittest.main()
