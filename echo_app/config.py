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


WAKE_PHRASE = "echo echo"

# Matches "that's it", "thats it", "that's all", "that is all", "that is it".
END_PHRASE_RE = re.compile(r"\bthat(?:'s|s| is) (?:it|all)\b", re.IGNORECASE)

PRIORITIES = ("interrupt", "ambient", "silent")
