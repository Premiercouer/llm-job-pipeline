"""One model call: system prompt + user prompt in, text + normalized usage out.

Every stage that talks to a model goes through a backend with the same contract, so callers get
the same usage fields and the same failure classes whatever runs underneath:

    backend.complete(system_prompt, user_prompt, timeout) -> Result
    raises TransientError  (retry with a fresh call may succeed: timeout, network, 5xx, 429)
           InputError      (this input can never succeed: prompt too long)
           FatalError      (environment / contract problem: stop the whole run)

Backends (pick one with make_backend("cli" | "cli:<model>" | "api" | "api:<model>")):

ClaudeCliBackend      `claude -p` single-turn and tool-less: the system prompt goes in a private
                      0600 file, the call runs in an empty private cwd with CLAUDE.md discovery and
                      auto-memory off, and the CLI's JSON envelope is checked after the fact (one
                      turn, no tool use, no permission denials, one model). Cost: the CLI's own
                      total_cost_usd.
AnthropicApiBackend   the Messages API through the official `anthropic` SDK (optional dependency):
                      the system prompt is one cached block (1h TTL), the input is the single user
                      message, no tools. The SDK's own retries are off so that the pipeline's retry
                      policy is the only one. Cost: list-price estimate from the usage fields.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path

from common import run_dirs


class LLMError(Exception):
    retryable = False


class TransientError(LLMError):
    retryable = True


class InputError(LLMError):
    """The input itself makes the call fail; a retry would send the same input."""


class FatalError(LLMError):
    """Every further call would fail the same way (auth, unusable CLI, contract violation)."""


@dataclass
class Usage:
    input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    duration_ms: int | None = None
    models: list[str] = field(default_factory=list)

    @property
    def input_side_tokens(self) -> int:
        return self.input_tokens + self.cache_creation_input_tokens + self.cache_read_input_tokens

    def as_dict(self) -> dict:
        return {**asdict(self), "input_side_tokens": self.input_side_tokens}


@dataclass
class Result:
    text: str
    usage: Usage
    backend: str
    raw: dict | None = None


def parse_json_object(text: str) -> dict:
    """The model's reply as a JSON object (a single ```json fence is tolerated)."""
    text = (text or "").strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.S)
    if fenced:
        text = fenced.group(1)
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"reply is not JSON: {e.msg}") from None
    if not isinstance(obj, dict):
        raise ValueError("reply is not a JSON object")
    return obj


# ----------------------------------------------------------------------- Claude Code CLI backend
CLI_ENV = {"CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1", "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}
FATAL_MESSAGES = ("Not logged in", "Invalid API key", "Please run /login")
INPUT_MESSAGES = ("Prompt is too long",)


def find_claude() -> str:
    """Locate the Claude Code CLI: $CLAUDE_BIN, then PATH, then the desktop-app bundle."""
    explicit = os.environ.get("CLAUDE_BIN")
    if explicit:
        if not os.access(explicit, os.X_OK):
            raise FatalError(f"CLAUDE_BIN is not executable: {explicit}")
        return explicit
    on_path = shutil.which("claude")
    if on_path:
        return on_path
    root = Path.home() / "Library/Application Support/Claude/claude-code"
    bundled = [p for pat in ("*/claude.app/Contents/MacOS/claude", "*/*/claude.app/Contents/MacOS/claude")
               for p in root.glob(pat) if os.access(p, os.X_OK)]

    def version_key(p: Path):
        version = p.relative_to(root).parts[0]
        return tuple(int(m.group()) if (m := re.match(r"\d+", part)) else 0 for part in version.split("."))

    if bundled:
        return str(max(bundled, key=version_key))
    raise FatalError("Claude Code CLI not found; set CLAUDE_BIN to its path")


class ClaudeCliBackend:
    name = "claude-cli"

    def __init__(self, model: str | None = None, claude_bin: str | None = None, label: str = "llm"):
        self.model = model
        self.claude_bin = claude_bin or find_claude()
        self.run_dir = run_dirs.new_run_dir(label)
        self.cwd = self.run_dir / "cwd"
        self.cwd.mkdir(mode=0o700)  # stays empty: the model has nothing to read
        self._prompt_files: dict[str, Path] = {}

    def _prompt_file(self, system_prompt: str) -> Path:
        key = hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()[:16]
        if key not in self._prompt_files:
            path = self.run_dir / f"{key}.system_prompt.md"
            run_dirs.write_private(path, system_prompt)
            self._prompt_files[key] = path
        return self._prompt_files[key]

    def command(self, prompt_file: Path) -> list[str]:
        cmd = [self.claude_bin, "-p", "--output-format", "json", "--no-session-persistence",
               "--strict-mcp-config", "--disable-slash-commands", "--tools", "",
               "--permission-mode", "dontAsk", "--system-prompt-file", str(prompt_file)]
        return cmd + (["--model", self.model] if self.model else [])

    def complete(self, system_prompt: str, user_prompt: str, timeout: float) -> Result:
        env = {**os.environ, **CLI_ENV}
        env.pop("CLAUDECODE", None)  # allow launching from inside a Claude Code terminal
        try:
            p = subprocess.run(self.command(self._prompt_file(system_prompt)), input=user_prompt,
                               capture_output=True, text=True, cwd=self.cwd, env=env, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise TransientError(f"timeout after {timeout}s") from None
        except OSError as e:
            raise FatalError(f"cannot start Claude CLI {self.claude_bin}: {e}") from None
        try:
            env_obj = json.loads(p.stdout)
        except (json.JSONDecodeError, TypeError):
            env_obj = None
        envelope = env_obj if isinstance(env_obj, dict) else None
        self._classify(envelope, p.stderr, p.returncode)
        usage = self._usage(envelope)
        self._check_contract(envelope, usage)
        return Result(text=str(envelope.get("result") or ""), usage=usage, backend=self.name, raw=envelope)

    @staticmethod
    def _classify(envelope: dict | None, stderr: str, returncode: int) -> None:
        first = ((stderr or "").strip().splitlines() or [""])[0]
        if first.startswith(("error: unknown option", "error: option '")):
            raise FatalError(f"Claude CLI rejected the command: {first[:200]}")
        if envelope and envelope.get("is_error"):
            message = str(envelope.get("result") or "").strip()
            status = envelope.get("api_error_status")
            if status in (401, 403):
                raise FatalError(f"API authentication/authorization failed (HTTP {status}): {message[:200]}")
            if message.startswith(FATAL_MESSAGES):
                raise FatalError(message[:200])
            if message.startswith(INPUT_MESSAGES):
                raise InputError(message[:200])
            raise TransientError(f"model call failed: {message[:200] or envelope.get('subtype')}")
        if returncode != 0 or envelope is None:
            detail = ((stderr or "").strip().splitlines()[-1:] or ["no output"])[0]
            raise TransientError(f"claude exited {returncode}: {detail[:200]}")
        if envelope.get("subtype") != "success":
            raise TransientError(f"model call failed ({envelope.get('subtype')})")

    @staticmethod
    def _usage(envelope: dict) -> Usage:
        u = envelope.get("usage") if isinstance(envelope.get("usage"), dict) else {}
        models = sorted(envelope.get("modelUsage") or {}) if isinstance(envelope.get("modelUsage"), dict) else []
        cost = envelope.get("total_cost_usd")
        return Usage(**{k: int(u.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens",
                                                         "cache_read_input_tokens", "output_tokens")},
                     cost_usd=float(cost) if isinstance(cost, (int, float)) else None,
                     duration_ms=envelope.get("duration_ms"), models=models)

    @staticmethod
    def _check_contract(envelope: dict, usage: Usage) -> None:
        problems = []
        if envelope.get("num_turns") != 1:
            problems.append(f"num_turns={envelope.get('num_turns')} (must be 1)")
        if envelope.get("permission_denials"):
            problems.append("permission denials")
        stu = (envelope.get("usage") or {}).get("server_tool_use")
        if isinstance(stu, dict) and any(v for v in stu.values() if isinstance(v, (int, float))):
            problems.append(f"server tool use: {stu}")
        if len(usage.models) > 1:
            problems.append(f"multiple models in one call: {usage.models}")
        if problems:
            raise FatalError("single-turn contract violated: " + "; ".join(problems))

    def close(self) -> None:
        run_dirs.remove(self.run_dir)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


# ----------------------------------------------------------------------- Anthropic API backend
# USD per million tokens: (input, output, cache read), from the Anthropic pricing page as of
# 2026-10-11. A 1h cache write costs 2x input. Haiku 5.5: prompts up to 100K tokens only.
PRICES = {
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 0.10),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
    "claude-fable-5-1": (10.00, 50.00, 0.25),
}
DEFAULT_API_MODEL = "claude-opus-5-5"


def list_price_cost(model: str, u: Usage) -> float | None:
    if model not in PRICES:
        return None
    inp, out, read = PRICES[model]
    return round((u.input_tokens * inp + u.cache_creation_input_tokens * 2 * inp +
                  u.cache_read_input_tokens * read + u.output_tokens * out) / 1e6, 6)


class AnthropicApiBackend:
    name = "anthropic-api"

    def __init__(self, model: str | None = None, effort: str | None = None, max_tokens: int = 16000,
                 fallbacks: bool = False, sdk=None, client=None):
        """`fallbacks` opts into server-side refusal fallbacks. It is off by default: a run must use one
        model throughout (the screening guardrail stops a run whose model changes), so a declined
        request should fail that one input instead of being answered by another model."""
        self.model = model or DEFAULT_API_MODEL
        self.effort, self.max_tokens, self.fallbacks = effort, max_tokens, fallbacks
        try:
            self.sdk = sdk or importlib.import_module("anthropic")
        except ImportError:
            raise FatalError('the API backend needs the anthropic SDK: pip install "llm-job-pipeline[api]"') from None
        self.client = client or self.sdk.Anthropic(max_retries=0)

    def complete(self, system_prompt: str, user_prompt: str, timeout: float) -> Result:
        sdk = self.sdk
        params = {"model": self.model, "max_tokens": self.max_tokens,
                  "system": [{"type": "text", "text": system_prompt,
                              "cache_control": {"type": "ephemeral", "ttl": "1h"}}],
                  "messages": [{"role": "user", "content": user_prompt}]}
        if self.effort:
            params["output_config"] = {"effort": self.effort}
        try:
            api = self.client.with_options(timeout=timeout)
            if self.fallbacks:
                msg = api.beta.messages.create(**params, betas=["server-side-fallback-2026-07-01"],
                                               fallbacks="default")
            else:
                msg = api.messages.create(**params)
        except (sdk.AuthenticationError, sdk.PermissionDeniedError, sdk.NotFoundError) as e:
            raise FatalError(f"{type(e).__name__}: {getattr(e, 'message', e)}") from None
        except sdk.BadRequestError as e:
            message = str(getattr(e, "message", e))
            if "prompt is too long" in message.lower():
                raise InputError(message[:200]) from None
            raise FatalError(f"request rejected: {message[:200]}") from None
        except (sdk.RateLimitError, sdk.APITimeoutError, sdk.APIConnectionError) as e:
            raise TransientError(f"{type(e).__name__}: {getattr(e, 'message', e)}") from None
        except sdk.APIStatusError as e:
            if e.status_code >= 500:
                raise TransientError(f"API error {e.status_code}") from None
            raise FatalError(f"API error {e.status_code}: {getattr(e, 'message', e)}") from None

        if msg.stop_reason == "refusal":
            category = getattr(getattr(msg, "stop_details", None), "category", None)
            raise InputError(f"model declined this input (category: {category})")
        if msg.stop_reason == "max_tokens":
            raise TransientError(f"output hit max_tokens={self.max_tokens}")
        if any(b.type == "tool_use" for b in msg.content):
            raise FatalError("single-turn contract violated: tool use in a tool-less call")
        u = msg.usage
        usage = Usage(input_tokens=u.input_tokens or 0,
                      cache_creation_input_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
                      cache_read_input_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
                      output_tokens=u.output_tokens or 0, models=[msg.model])
        usage.cost_usd = list_price_cost(msg.model, usage)
        text = "".join(b.text for b in msg.content if b.type == "text")
        return Result(text=text, usage=usage, backend=self.name, raw=None)

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def make_backend(spec: str = "cli", *, model: str | None = None, label: str = "llm", **api_options):
    """"cli", "cli:<model>", "api" or "api:<model>"; an explicit `model` wins over the spec's."""
    kind, _, spec_model = spec.partition(":")
    model = model or spec_model or None
    if kind == "cli":
        return ClaudeCliBackend(model=model, label=label)
    if kind == "api":
        return AnthropicApiBackend(model=model, **api_options)
    raise ValueError(f"unknown backend {spec!r}: use cli, cli:<model>, api or api:<model>")
