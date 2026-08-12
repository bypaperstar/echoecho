# Echo v2 — Generic Plan ("do anything," own Mac VM, open workspace)

Companion to `PLAN.md` (v0, demo-scoped). This is the design pass for the generic product: Echo is no
longer limited to five task kinds — it can be asked to do *anything*, it works inside its own sandbox
(up to a full macOS VM), it keeps a free-form workspace of documents of any type, and it reaches the
user's real documents only through a mediated mount + approval flow.

## What we're building (3 sentences)

Echo v2 keeps the entire v0 conversation loop — Vosk "echo echo" wake, Realtime speech-to-speech
session, orchestrator, turn-boundary injections, viewer — and replaces the demo worker zoo with one
generic capability: `agent.run`, which hands a natural-language task to a headless coding-agent
runtime (Claude Code / codex CLI) executing inside a sandbox tier the orchestrator picks. The sandbox
ladder goes from "subprocess jailed to `workspace/`" up to "Echo's own macOS virtual machine" (Apple
Virtualization framework via Lume/Tart: golden image with the agent preinstalled, snapshot/rollback,
SSH control), with the user's chosen document folders mounted read-only and an `outbox/` + spoken
approval flow as the only path for writes back to the real Mac. The workspace becomes a real
directory tree — any file type, any structure, agent-chosen names — rendered by a type-aware viewer,
and every long-running task streams progress/questions back into the live voice session through the
existing Injection mechanism.

## Why this is a Layer-3 swap, not a rewrite

The v0 architecture already isolates all demo-specificity. **Contract A is unchanged**: the voice
model still sees exactly 4 tools (`dispatch_task`, `check_tasks`, `read_artifact`, `end_session`)
and receives `Injection{text, priority}` at turn boundaries. **Contract B is unchanged**:
`async run(task, ctx) -> TaskResult`. The orchestrator (`orchestrator/core.py`), ranker, session
FSM, wake loop, audio, recorder, and viewer plumbing all survive as-is. What changes:

1. **Workers**: `doc.edit` / `recipe.search` / `grocery.merge` / `learn.*` are deleted (kept only as
   optional fast-path plugins, see below). One new worker, `agent.run`, absorbs all of them — "merge
   this into my grocery list" is now just a task an agent executes against `workspace/grocery.md`.
2. **Capability declaration** de-triplicated: v0 hand-writes the kind list three times
   (`@register`, the tool-schema `enum` in `conversation/textmode.py:28`, prose in
   `config.py:90`). v2 workers carry `KIND`, `DESCRIPTION`, `ARG_SCHEMA`; the enum and the system
   prompt fragment are generated from `REGISTRY`, and `load_all()` becomes a `pkgutil` package scan
   (plugin discovery instead of a hardcoded import list).
3. **Artifacts** lose the flat-`*.md` assumption: `services/artifacts.py` gets safe relative-path
   resolution (normpath + prefix check, replacing the `os.path.basename` guard), subdirectories, and
   binary writes; the viewer renders by type (markdown, code w/ highlighting, images, PDF/other as
   download links) and shows a tree instead of tabs.
4. **Task table persists.** v0 loses in-flight tasks on restart, which was fine at 3–15 s per task.
   v2 tasks run minutes to hours, so the table serializes (rehydrate from `.tasks.jsonl` on boot;
   orphaned `running` tasks re-attach to their agent session or report as interrupted).

## The execution engine: rent, don't build

We do not write our own agent loop (planning, tool use, retries, web browsing, coding). The worker
shells out to a headless coding agent — `claude -p --output-format stream-json` (Agent SDK) or
`codex exec` — exactly the seam `workers/code_stub.py` already proves. Echo's job is the *voice
front-end and the sandbox*, not the agent runtime:

- `agent.run` spawns the CLI with `cwd` = the sandbox's workspace mount, streams its JSON events,
  and converts them to Injections: throttled `ambient` progress lines (≤1 per 30 s, "still going on
  the lease review — reading clause 7"), `interrupt` on completion or on a question
  (`needs_input`, already an interrupt in `ranker.py`).
- Steering: `agent.run` accepts `task_id` to resume — the voice model can say "add a budget section
  too" and the worker resumes the same agent session (`--resume <session-id>`). This replaces v0's
  per-kind arguments with conversation.
- Budgets: a per-task wall-clock cap from config (`ECHO_AGENT_TIMEOUT`); on breach the worker kills
  the agent's whole process group, reports `error` (auto-`interrupt`), and leaves partial work
  staged + resumable. Token/cost caps are deferred: `cost_usd` is captured from the agent's terminal
  result as groundwork, but it arrives only at the end (and `codex` emits none), so there is no
  incremental signal to preempt on mid-run — a real cap needs the VM tier's metering. The VM
  snapshot will make "undo that" trivial.
- Testing: a `FakeAgentCLI` fixture (JSONL event replay, same trick as `FakeTransport`/`FakeLLM`)
  keeps the whole loop keyless and Linux-runnable.

## Sandbox ladder

| Tier | Name | What runs where | Blast radius | Ships in |
|---|---|---|---|---|
| 0 | `inproc` | v0-style in-process coroutine | workspace/ only | exists |
| 1 | `shell` | headless agent subprocess, `cwd=workspace/`, host Mac | workspace/ + whatever the CLI's own sandbox allows (Claude Code permission modes: deny-by-default outside cwd, no network prompts auto-accepted) | PR 10 |
| 2 | `vm` | **Echo's own Mac**: macOS VM on Apple Silicon (Virtualization.framework) managed by Lume or Tart — golden image with agent CLI, browsers, dev tools preinstalled; control via SSH; virtiofs mounts | the VM. Snapshot before each task batch; rollback on "undo" | PR 12 |
| 3 | `host` | the user's real Mac (AppleScript/System Events/files outside mounts) | everything | explicit per-action spoken confirmation; mostly out of scope v2 |

Default policy: orchestrator picks `shell` for pure-workspace document work (fast, no VM boot) and
`vm` for anything needing arbitrary code, app automation, or user-document access. One warm VM is
kept resumed while Echo runs (VMs suspend/resume in ~seconds; cold clone from golden image ~30 s).
Apple licensing allows exactly 2 concurrent macOS guests per host — one warm + one scratch is the
ceiling, fine for one user.

**Why macOS in the VM and not a Linux container:** a Linux container (Apple `container` /
Docker) is 10× lighter and covers code + web + documents; we may add it later as tier 1.5 for
cost. But the product promise is "it can do anything *a Mac user* can" — Mac apps, Preview,
Numbers, Safari with the user's kind of environment — and the GUI computer-use step (PR 14) only
makes sense on a Mac guest. Disk cost is real: a golden macOS image is ~40–60 GB.

## User documents: mediated, never direct

- Setup names the shared folders (e.g. `~/Documents`, `~/Desktop`). They appear in the VM at
  `/Volumes/UserDocs` **read-only** (virtiofs). The Echo workspace mounts read-write at
  `/Volumes/EchoWorkspace` — it is the same `workspace/` the viewer renders on the host.
- The agent never writes user documents. Proposed changes land in `workspace/outbox/<task>/` as
  full files + a `CHANGES.md` diff summary. The completion injection says what's staged
  ("rewrote section 3 of lease.md — say 'apply it' to save over the original"); "apply it"
  dispatches a tiny host-side `outbox.apply` worker (tier 0) that copies with a timestamped backup.
- This line is what lets everything *inside* the VM run unconfirmed and voice-speed: reads are
  free, writes are staged, the real Mac is untouched by default.

## Voice UX deltas for long tasks

- Tasks get short spoken handles (worker returns `data.title`, e.g. "the lease review"); the
  generated system prompt teaches the model to refer to tasks by handle, not id.
- `check_tasks` gains elapsed time + last progress line — "how's it going?" works mid-task.
- Cross-session continuity already exists (`[since last session]` injection); with persistence it
  now survives restarts too. A finished 40-minute task announces on the next wake.
- Silence timeout no longer ends the *work* — ending a session leaves VM tasks running (v0 already
  does this for workers; now it matters).

## Prior art (surveyed 2026-08-12)

Every piece exists in the open; the *combination* (always-on wake-word voice + local Mac-VM sandbox
+ mediated user docs) does not appear to.

- **OpenClaw** (ex-Clawdbot/Moltbot) — closest whole product: open-source local-first personal
  agent; shell/browser/files/calendar; 24 chat channels; macOS voice-wake + talk mode. Differences:
  chat-first with voice bolted on (not ambient speech-to-speech), and it acts on your *real*
  machine by default rather than its own VM. Worth studying for channel/skill architecture; also
  the honest "should we just contribute to this instead?" benchmark.
- **Open Interpreter / 01** — the open-source voice-first "language-model computer"; closest in
  spirit to Echo's interaction model; still experimental, explicitly lacking safeguards.
- **Cua + Lume** (trycua) — exactly the "VM with a Mac in it" primitive: macOS VMs on Apple Silicon
  via Virtualization.framework, CLI VM manager (Lume), computer-use SDK/driver. Our tier-2 builds
  on Lume (or **Tart**, the CI-oriented alternative) rather than reimplementing VM management.
- **E2B Desktop / open-computer-use / Surf** — the same idea as cloud Linux desktops; validates the
  sandbox-per-agent model, wrong substrate for a personal Mac product.
- **Products**: OpenAI Operator/ChatGPT Agent (hosted VM + browser/terminal — the workspace-and-VM
  UX, but cloud Linux, no local docs, no voice), Anthropic computer use + Claude Code (our proposed
  runtime), Manus. None ambient-voice, none local.

## Stacked PRs

10. **`echo/10-generic-core`** — registry metadata (`KIND`/`DESCRIPTION`/`ARG_SCHEMA`), generated
    tool enum + prompt fragment, `pkgutil` plugin discovery; artifacts subdir/any-type support +
    path-resolution guard; viewer tree + type-aware rendering; `agent.run` tier-1 worker with
    `FakeAgentCLI` fixtures. Demo kinds move to `plugins/` (kept runnable; no longer in the prompt
    by default). Gate: existing scripted demos rewritten as `agent.run` fixtures pass headless.
11. **`echo/11-long-tasks`** — progress-stream → throttled ambient injections; `needs_input`
    voice round-trip; resume/steering by task handle; task-table persistence + orphan re-attach +
    a persisted announcement watermark (so a completed-while-idle task announces once, even across
    a restart); a per-task wall-clock budget with process-group teardown (token/cost cap deferred
    to the VM tier — see above).
12. **`echo/12-vm-sandbox`** — Lume lifecycle (golden image build script, clone, suspend/resume,
    snapshot, SSH exec), `sandbox=vm` in `agent.run`, workspace virtiofs mount, warm-VM policy.
    Headless CI substitutes a `FakeVM` (local tmpdir + subprocess) behind the same port.
13. **`echo/13-user-docs`** — read-only user-folder mounts, `outbox/` convention, `outbox.apply`
    worker + backup, approval phrases in the prompt.
14. **`echo/14-computer-use`** (stretch) — GUI driving inside the VM (cua driver or
    screenshot+accessibility loop) for Mac-app tasks; screen-recording into the session recorder.

## Risks

1. **VM footprint on a personal Mac** (40–60 GB disk, 4–8 GB RAM warm). Mitigate: single warm VM,
   aggressive suspend, tier-1 default for document work, document the cost up front.
2. **Runaway agents** (cost/time/junk in workspace). Mitigate: a wall-clock budget that kills the
   agent's whole process group (PR 11), snapshots, and everything in the VM being disposable by
   design; `outbox/` keeps the host clean. Cost/token metering waits for the VM tier.
3. **Voice ↔ long-task impedance** (user wakes Echo mid-task, expects instant answers). Mitigate:
   task handles + `check_tasks` elapsed/progress; progress throttling so Echo isn't chatty.
4. **Headless-agent CLI drift** (flags/stream format change). Mitigate: one adapter module per CLI
   behind an `AgentRuntime` port + replay fixtures, same recipe that made Realtime testable.
5. **False sense of safety**: tier 1 runs on the host. Mitigate: tier-1 allowed only
   workspace-scoped, deny-by-default CLI permission mode, everything else auto-routes to the VM.
6. **Python 3.9 constraint** vs. modern SDKs: Lume/Tart are CLIs and the agent runs over
   subprocess/SSH — no new Python deps in-process; keep the 3.9 rule.

## Out of scope for v2

- Multi-user / speaker ID; non-English; packaging/installer.
- Writing our own agent loop, browser automation stack, or computer-use model.
- Windows/Linux hosts; more than one concurrent scratch VM.
- Direct (non-outbox) writes to user documents; host-tier automation beyond explicit one-shot
  confirmations.
- Cloud execution of tasks (all tiers are local; the only network egress is model APIs).
