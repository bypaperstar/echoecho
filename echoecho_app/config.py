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
RECORDINGS_DIR = REPO_ROOT / "recordings"
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


def echoecho_text():
    return _flag("ECHOECHO_TEXT")


def echoecho_fake_llm():
    return _flag("ECHOECHO_FAKE_LLM")


def echoecho_plugins():
    """ECHOECHO_PLUGINS=1 advertises the demo-era plugin kinds (echoecho_app.plugins)
    to the voice model; they stay dispatchable either way."""
    return _flag("ECHOECHO_PLUGINS")


def fake_agent_script():
    """ECHOECHO_FAKE_AGENT_SCRIPT: JSONL fixture (or directory of them, consumed
    in sorted order) replayed by FakeAgentCLI so agent.run runs keyless."""
    return os.environ.get("ECHOECHO_FAKE_AGENT_SCRIPT", "").strip()


def progress_interval():
    """Min seconds between ambient progress injections per task — long agent
    runs stay felt without echoecho narrating every step."""
    return float(os.environ.get("ECHOECHO_PROGRESS_INTERVAL", "30"))


def agent_timeout():
    """Wall-clock budget (seconds) for one agent.run task; on breach the
    subprocess is killed and the task errors (resumable by task_id)."""
    return float(os.environ.get("ECHOECHO_AGENT_TIMEOUT", "900"))


# -- sandbox ladder tier 2: echoecho's own macOS VM (services/vm.py, PR 12) -------

def sandbox_tier():
    """ECHOECHO_SANDBOX: default tier for agent.run — "shell" (host subprocess,
    cwd=workspace) or "vm" (echoecho's own macOS guest via lume). Per-task
    override: dispatch args {"sandbox": "vm"}."""
    return os.environ.get("ECHOECHO_SANDBOX", "shell").strip() or "shell"


def vm_name():
    return os.environ.get("ECHOECHO_VM_NAME", "echoecho-vm").strip()


def vm_golden():
    """The golden image VM (agent CLI + ssh key preinstalled) that scratch
    VMs are APFS-cloned from; built once by scripts/vm_golden.sh."""
    return os.environ.get("ECHOECHO_VM_GOLDEN", "echoecho-golden").strip()


def vm_guest_user():
    return os.environ.get("ECHOECHO_VM_USER", "lume").strip()


def vm_ssh_key():
    return os.environ.get("ECHOECHO_VM_SSH_KEY", "~/.ssh/echoecho_vm_ed25519").strip()


def vm_guest_workspace():
    """Where the shared workspace appears inside the guest (virtiofs mount;
    macOS guests surface shared dirs under /Volumes/My Shared Files)."""
    return os.environ.get("ECHOECHO_VM_GUEST_WORKSPACE",
                          "/Volumes/My Shared Files/workspace").strip()


def vm_boot_timeout():
    return float(os.environ.get("ECHOECHO_VM_BOOT_TIMEOUT", "180"))


def user_docs():
    """ECHOECHO_USER_DOCS: host folders the user shares with echoecho, separated by
    the OS path separator (':' on macOS) — e.g. ~/Documents:~/Desktop. They
    mount READ-ONLY into the VM; the ONLY path back to them is the outbox +
    spoken approval (workers/outbox.py). Returns absolute, user-expanded
    paths — split ONLY on os.pathsep so a folder name with a space ("My
    Documents", iCloud "Mobile Documents/…") survives intact."""
    raw = os.environ.get("ECHOECHO_USER_DOCS", "")
    out = []
    for part in raw.split(os.pathsep):
        part = part.strip()
        if not part:
            continue
        p = Path(os.path.abspath(Path(part).expanduser()))
        if p not in out:
            out.append(p)
    return out


OUTBOX_DIR = "outbox"  # workspace/outbox/<task>/ : staged user-doc changes
OUTBOX_BACKUP_SUFFIX = ".echoecho-bak"  # <original>.echoecho-bak-<ts> on apply


def vm_pass_env():
    """Env var NAMES forwarded into the guest over SSH (SendEnv; the golden
    image's sshd AcceptEnv-lists them) so the in-guest agent can reach its
    model API. Least privilege: default to ANTHROPIC_API_KEY only — the
    golden image ships the `claude` CLI, which needs nothing else, and the
    vm tier runs untrusted code, so echoecho's other keys (the OpenAI voice key)
    stay out of the guest. A codex guest sets ECHOECHO_VM_PASS_ENV explicitly."""
    raw = os.environ.get("ECHOECHO_VM_PASS_ENV", "ANTHROPIC_API_KEY")
    return [n for n in raw.replace(",", " ").split() if n]


def realtime_model():
    return os.environ.get("ECHOECHO_REALTIME_MODEL", "gpt-realtime-2.1-mini")


def silence_timeout():
    return float(os.environ.get("ECHOECHO_SILENCE_TIMEOUT", "600"))


def input_device():
    """ECHOECHO_INPUT_DEVICE: mic device index or name substring (see
    conversation.audio.resolve_device); "" = follow the system default,
    re-checked at every session start."""
    return os.environ.get("ECHOECHO_INPUT_DEVICE", "").strip()


def output_device():
    """ECHOECHO_OUTPUT_DEVICE: speaker device index or name substring; "" =
    follow the system default, re-checked at every session start."""
    return os.environ.get("ECHOECHO_OUTPUT_DEVICE", "").strip()


def recordings_dir():
    """Where session recordings land (gitignored recordings/ by default);
    ECHOECHO_RECORDINGS_DIR overrides. Resolved at call time for tests."""
    raw = os.environ.get("ECHOECHO_RECORDINGS_DIR", "").strip()
    return Path(raw) if raw else RECORDINGS_DIR


def echoecho_record(mode="voice"):
    """Session recording on/off (the dev feedback loop). ECHOECHO_RECORD unset:
    record real product use (--voice) only. Set: "0"/"false"/"no" disables
    everywhere; anything else enables everywhere (so ECHOECHO_RECORD=1 records
    --text/--script runs too)."""
    raw = os.environ.get("ECHOECHO_RECORD", "").strip().lower()
    if raw == "":
        return mode == "voice"
    return raw not in ("0", "false", "no")


# Brand and wake phrase are the same token. Vosk still decodes this as two
# adjacent "echo" words, so the acoustic detector uses its own decoded form.
WAKE_PHRASE = "echoecho"
WAKE_DECODED_PHRASE = "echo echo"

# Tuned for voice (PR 6): short utterances, verbal acks before dispatch, weave
# results in naturally, never read URLs aloud. The kinds line is generated
# from the worker registry (PR 10) — workers declare themselves once.
SYSTEM_PROMPT_TEMPLATE = (
    "You are echoecho, a hands-free voice assistant. Keep every reply short and "
    "speakable — one or two sentences; never lecture. "
    "Use dispatch_task for anything slow; kinds: %(kinds)s. dispatch_task "
    "returns instantly: give a brief verbal ack BEFORE dispatching (\"On it — "
    "starting now\") and keep the conversation going; never wait for a task. "
    "System lines like '[task tN done] ...' report finished background work: "
    "weave them into the conversation naturally, as if you just remembered — "
    "don't read them verbatim. Never read URLs, file paths, or raw markdown "
    "syntax aloud; say where a thing came from instead (\"a 30-minute pad "
    "thai on RecipeTin Eats\"). Use read_artifact to quote workspace files, "
    "summarizing just the part the user asked for. Long tasks report "
    "progress as '[task tN progress]' lines and check_tasks shows elapsed "
    "time — refer to tasks by what they are (\"the lease review\"), never by "
    "raw ids. To steer, extend, or answer a question from an earlier agent "
    "task, dispatch the same kind again with args.task_id set to that "
    "task's id.%(approval)s Call end_session when the user says something "
    "like \"that's it\".")

# Appended only when the user has shared folders (workers/outbox.py): agents
# can't touch real documents directly, so surface the approval step.
APPROVAL_GUIDANCE = (
    " Your documents are never changed directly: an agent stages proposed "
    "edits and you approve them out loud. When a task reports staged changes, "
    "tell the user what changed and that they can say \"apply it\" to save "
    "over the originals; on \"apply it\", dispatch outbox.apply.")


def system_prompt():
    """The tuned prompt with the kinds line generated from the registry —
    call after load_all() so every advertised worker has registered."""
    from echoecho_app.workers import base  # lazy: workers import config
    approval = APPROVAL_GUIDANCE if user_docs() else ""
    return SYSTEM_PROMPT_TEMPLATE % {
        "kinds": base.kinds_fragment() or "(none)", "approval": approval}

# Matches "that's it", "thats it", "that's all", "that is all", "that is it".
END_PHRASE_RE = re.compile(r"\bthat(?:'s|s| is) (?:it|all)\b", re.IGNORECASE)

PRIORITIES = ("interrupt", "ambient", "silent")
