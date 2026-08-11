"""LLMPort: RealLLM (OpenAI Responses API) | FakeLLM (fixtures/llm/<kind>.json).

RealLLM constructs its client lazily so importing/instantiating it keyless
never fails; calling it keyless raises LLMUnavailable, which workers with a
non-LLM fallback (grocery.merge) catch.
"""
import json
import os
import re
from pathlib import Path

from echo_app import config


class LLMUnavailable(Exception):
    pass


class LLMPort:
    async def complete(self, kind, prompt):  # type: (str, str) -> str
        raise NotImplementedError


class RealLLM(LLMPort):
    def __init__(self, model=None):
        self.model = model or os.environ.get("ECHO_WORKER_MODEL", "gpt-4o-mini")
        self._client = None

    def _client_or_raise(self):
        if self._client is None:
            if not os.environ.get("OPENAI_API_KEY"):
                raise LLMUnavailable("OPENAI_API_KEY not set")
            from openai import AsyncOpenAI  # lazy: keyless import paths never touch this
            self._client = AsyncOpenAI()
        return self._client

    async def complete(self, kind, prompt):
        client = self._client_or_raise()
        resp = await client.responses.create(model=self.model, input=prompt)
        return resp.output_text


class FakeLLM(LLMPort):
    """Returns fixtures/llm/<kind>.json {"output": ...}; missing fixture ->
    LLMUnavailable (exercises workers' keyless fallbacks)."""

    def __init__(self, fixtures_dir=None):
        self.fixtures_dir = Path(fixtures_dir or (config.FIXTURES_DIR / "llm"))

    async def complete(self, kind, prompt):
        path = self.fixtures_dir / (kind + ".json")
        if not path.exists():
            raise LLMUnavailable("no fixture for kind %r at %s" % (kind, path))
        return json.loads(path.read_text(encoding="utf-8"))["output"]


def for_ctx(ctx):  # type: (...) -> LLMPort
    """Pick the LLM for a WorkerContext: injected > ECHO_FAKE_LLM fake > real."""
    injected = ctx.extra.get("llm")
    if injected is not None:
        return injected
    if ctx.fake_llm or config.echo_fake_llm():
        return FakeLLM()
    return RealLLM()


_FENCE_RE = re.compile(r"^```[a-zA-Z]*\n(.*)\n```\s*$", re.DOTALL)


def strip_fences(text):
    """LLMs love wrapping whole-file answers in ```markdown fences; unwrap."""
    m = _FENCE_RE.match(text.strip())
    return m.group(1) if m else text.strip()
