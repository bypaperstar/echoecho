"""Central config: env flags, paths, phrases, timeouts."""
import os
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKSPACE_DIR = REPO_ROOT / "workspace"
FIXTURES_DIR = REPO_ROOT / "fixtures"
MODELS_DIR = REPO_ROOT / "models"
VOSK_MODEL_DIR = MODELS_DIR / "vosk-model-small-en-us-0.15"
TASKS_LOG = WORKSPACE_DIR / ".tasks.jsonl"
ENV_LOCAL = REPO_ROOT / ".env.local"


def load_env_local(path=None):
    """Load KEY=VALUE lines from .env.local into os.environ (gitignored;
    holds OPENAI_API_KEY etc. for local runs). Real env vars win."""
    path = Path(path) if path else ENV_LOCAL
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key and value and key not in os.environ:
            os.environ[key] = value


def _flag(name):
    return os.environ.get(name, "").strip().lower() not in ("", "0", "false", "no")


def echo_text():
    return _flag("ECHO_TEXT")


def echo_fake_llm():
    return _flag("ECHO_FAKE_LLM")


def realtime_model():
    return os.environ.get("ECHO_REALTIME_MODEL", "gpt-realtime-2.1-mini")


def silence_timeout():
    return float(os.environ.get("ECHO_SILENCE_TIMEOUT", "600"))


def input_device():
    """ECHO_INPUT_DEVICE: mic device index or name substring (see
    conversation.audio.resolve_device); "" = follow the system default,
    re-checked at every session start."""
    return os.environ.get("ECHO_INPUT_DEVICE", "").strip()


def output_device():
    """ECHO_OUTPUT_DEVICE: speaker device index or name substring; "" =
    follow the system default, re-checked at every session start."""
    return os.environ.get("ECHO_OUTPUT_DEVICE", "").strip()


WAKE_PHRASE = "echo echo"

# Tuned for voice (PR 6): short utterances, verbal acks before dispatch, weave
# results in naturally, never read URLs aloud.
SYSTEM_PROMPT = (
    "You are Echo, a hands-free voice assistant. Keep every reply short and "
    "speakable — one or two sentences; never lecture. "
    "Use dispatch_task for anything slow; kinds: doc.edit, recipe.search, "
    "grocery.merge, learn.outline, learn.deep_dive. dispatch_task returns "
    "instantly: give a brief verbal ack BEFORE dispatching (\"On it — "
    "searching now\") and keep the conversation going; never wait for a task. "
    "System lines like '[task tN done] ...' report finished background work: "
    "weave them into the conversation naturally, as if you just remembered — "
    "don't read them verbatim. Never read URLs, file paths, or raw markdown "
    "syntax aloud; say where a thing came from instead (\"a 30-minute pad "
    "thai on RecipeTin Eats\"). Use read_artifact to quote workspace files, "
    "summarizing just the part the user asked for. Call end_session when the "
    "user says something like \"that's it\".")

# Matches "that's it", "thats it", "that's all", "that is all", "that is it".
END_PHRASE_RE = re.compile(r"\bthat(?:'s|s| is) (?:it|all)\b", re.IGNORECASE)

PRIORITIES = ("interrupt", "ambient", "silent")
