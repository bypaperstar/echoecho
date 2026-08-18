# echoecho — always-on voice agent prototype

echoecho is a quick-and-dirty always-on voice agent for your Mac. Say **"echoecho"** to start a session, talk back and forth like ChatGPT voice, and echoecho farms out real work (writing a doc with you live, building a grocery list while searching recipes, tutoring you on a new topic) to background worker agents — weaving results back into the conversation as they land. The session ends when you say **"that's it"** or after 10 minutes of silence.

See **[PLAN.md](PLAN.md)** for the full architecture, decisions (and what was rejected and why), the stacked-PR breakdown, demo scripts, and risks.

## Big pieces

1. **Wake word** — Vosk keyword spotting for "echoecho" (open source, no keys, no training).
2. **Voice loop** — OpenAI Realtime API speech-to-speech (`gpt-realtime-2.1[-mini]`), semantic VAD, barge-in, reconnect-with-backoff so the daemon never dies.
3. **Orchestrator** — a generic in-process task queue: the voice agent dispatches tasks, async workers do them, results are ranked (interrupt / ambient / silent) and injected back into the live conversation at safe turn boundaries. Tasks that finish while echoecho is asleep are surfaced on the next wake as a "[since last session]" note.
4. **Live workspace** — everything workers produce lands in `workspace/` (any file type, subdirectories welcome), rendered live in a browser tab via a tiny SSE auto-refresh viewer: a file tree, type-aware rendering (markdown, code, images, downloads), changed markdown sections flash briefly.

## Mac runbook (from zero to talking)

Laptop speakers are supported. echoecho runs the rendered speaker signal and the
microphone through WebRTC acoustic echo cancellation before uploading audio,
adds conservative residual-echo suppression during playback/tails, and asks
Realtime for far-field input noise reduction. Headphones still provide the
most isolation, but they are no longer required.

```bash
# 1. Clone, then make a plain venv (uv shown; pyenv/python3 -m venv also fine).
#    Do NOT use conda — its portaudio shadows the one bundled in the
#    sounddevice wheel and you get silent audio breakage.
uv venv .venv && source .venv/bin/activate

# 2. Install. requirements-mac.txt adds sounddevice, Vosk, and LiveKit's
#    native WebRTC audio processor (AEC/high-pass processing).
pip install -r requirements-mac.txt

# 3. Sanity-check audio devices (should list your mic + output):
python3 -m sounddevice

# 4. Fetch the Vosk wake-word model (~40 MB into models/, gitignored):
bash scripts/fetch_models.sh

# 5. Key: put it in .env.local (gitignored, loaded at startup)...
echo 'OPENAI_API_KEY=sk-...' > .env.local
#    ...or export it — a real env var always beats .env.local:
export OPENAI_API_KEY=sk-...

# 6. Step zero every time you use a new terminal app: mic check.
#    Speak — you should see the RMS meter move. All zeros = macOS TCC denied
#    the mic to your terminal. Fix:
#      sudo tccutil reset Microphone com.apple.Terminal   # or your app's bundle id
#    then rerun and click "Allow" on the prompt.
python3 echoecho.py --mic-check

# 7. Run the daemon — built-in Mac speakers and mic are supported:
python3 echoecho.py --voice
```

Then open the live workspace at <http://127.0.0.1:8765/>, say **"echoecho"**
(or press enter in the terminal as the manual-wake override), talk, and say
**"that's it"** when you're done. The wake loop idles at ~2% of one core and
$0 of API while no session is open.

### Live UI

The browser tab at <http://127.0.0.1:8765/> (override the port with
`ECHOECHO_VIEWER_PORT`, skip it with `--no-viewer`) is the live "what is
happening" view, in two panes:

- **Left — transcript & activity.** The running conversation as chat bubbles
  (you right, echoecho left) interleaved with plainly-explained activity lines:
  wake events, session state changes, tasks dispatched to background workers,
  worker completions (with what the interrupt/ambient/silent priority means),
  and results being handed back into the live conversation. A collapsible
  "How echoecho works" box sits at the top. Driven by an append-only
  `workspace/.events.jsonl` feed (truncated on each run) served at
  `/transcript`; the header shows the session state badge, connected Realtime
  model, and a running-task counter.
- **Right — workspace docs.** One tab per `workspace/*.md` file, rendered
  with marked.js, auto-focusing the most recently modified file and flashing
  changed sections — exactly as before.

Both panes update from the same SSE stream; `--text` and `--script` runs
populate the transcript the same way voice does, so the whole UI works
keyless and headless.

### Model / cost flags

- Default model is **`gpt-realtime-2.1-mini`** (~3x cheaper audio; use it for
  all development and rehearsal).
- Demo day: `python3 echoecho.py --voice --model gpt-realtime-2.1` for the best
  voice + tool-calling quality. Sessions only exist between wake and
  "that's it", so even 2.1 is cents per demo.
- Worker LLM calls use the Responses API (`gpt-4o-mini` class); override with
  `ECHOECHO_WORKER_MODEL`.

### The three demo one-liners

Full line-by-line scripts with timestamps: [`scripts/demo_cheatsheet.md`](scripts/demo_cheatsheet.md).

1. **Doc co-writing** — "echoecho echo … let's write a one-page proposal for a team offsite in Lisbon." (then goals, agenda, "read me just the goals", "that's it")
2. **Groceries + recipes** — "echoecho echo … help me plan dinners this week, I'm thinking pad thai one night." (then halloumi, "drop the fish sauce", "that's it")
3. **Learning** — "echoecho echo. Teach me about fermentation in food." (then "sourdough", answer the quiz question, "that's it")

Since PR 10 ([PLAN-GENERIC.md](PLAN-GENERIC.md)) the product's one advertised
kind is **`agent.run`**: any ask is handed to a headless coding agent
(`claude -p` / `codex exec`) working inside `workspace/`. The demo workers
above live on as optional fast-path plugins — dispatchable always, advertised
to the voice model only with `ECHOECHO_PLUGINS=1`.

PR 11 makes those agent tasks long-task-shaped: progress streams back as
throttled ambient lines (`ECHOECHO_PROGRESS_INTERVAL`, default 30 s), `check_tasks`
shows elapsed time and the last progress line ("how's it going?" works
mid-task), and tasks are referred to by a short spoken handle. To steer or
extend an earlier task, dispatch `agent.run` again with `args.task_id` — it
resumes the same agent session (`--resume`). A task that ends with a
`QUESTION:` line comes back as an interrupt so echoecho can ask you. The task
table persists to `.tasks.jsonl`, so a finished task announces on the next
wake even across a restart, and a task caught mid-flight is reported as
interrupted (and resumable if its agent session was checkpointed). Runaway
agents are bounded by a wall-clock budget (`ECHOECHO_AGENT_TIMEOUT`, default
15 min); on breach the agent is stopped and the partial work is left staged
and resumable.

PR 12 adds the sandbox ladder's tier 2 — **echoecho's own macOS VM**. `ECHOECHO_SANDBOX=vm`
(or a per-task `args.sandbox="vm"`) runs the agent inside a macOS guest managed by
[Lume](https://github.com/trycua/cua) over SSH, with `workspace/` shared read-write
so the viewer and touched-file detection keep working unchanged. Scratch VMs are
APFS-clones of a golden image, so rollback ("undo that") is a delete + re-clone.
Build the golden image once on the Mac: `AGENT=1 bash scripts/vm_golden.sh` (pulls a
~18 GB base image, installs echoecho's SSH key, allows model API keys through the guest
`sshd`, and installs the `claude` CLI in the guest). The default tier stays `shell`
(host subprocess); the whole VM code path is exercised keyless/Linux via a `FakeVM`
behind the same port, so CI never needs a Mac.

PR 14 adds **GUI computer-use** — the sandbox ladder's GUI tier. The
`computer.use` kind (advertised when `ECHOECHO_SANDBOX=vm`) drives real Mac apps
inside the VM by a sequence of steps (`launch` / `type` / `key` / `wait` /
`screenshot`) via a `GuiDriver` port: `SshGuiDriver` uses only macOS built-ins
(`open`, `osascript`, `screencapture`) over the same SSH channel as the agent
tier, and a screenshot after every step lands in `workspace/screens/<task>/`
(the viewer renders them — that shot trail is the recording). Live-validated
on a real VM: app launch + screenshots work end-to-end (screenshots reach the
host via virtiofs). **Caveat:** synthetic keystrokes (`type`/`key`) need macOS
Accessibility permission, which a SIP-enabled vanilla image won't grant to an
SSH-invoked process — the golden image must pre-grant it (SIP-off build + TCC,
or a PPPC profile); until then those steps fail fast with a clear message
rather than hanging. Coordinate clicks and a model-driven perceive→act loop
are the follow-up.

PR 13 adds **mediated access to your real documents**. Point echoecho at folders with
`ECHOECHO_USER_DOCS=~/Documents:~/Desktop`; they mount **read-only** into the VM, so an
agent can read them but never write them. Proposed edits are staged in
`workspace/outbox/<task>/` (full updated files + a `MANIFEST.json` mapping each to its
original + a `CHANGES.md`), and nothing touches a real document until you say **"apply
it"** — which dispatches the tier-0 `outbox.apply` worker. That worker re-validates
every target against your shared-folder allowlist (the agent's manifest is not
trusted), backs up each original with a timestamp, then writes atomically. With no
folders shared, `outbox.apply` isn't even advertised and the approval flow is absent.

PR 15 adds the **echoecho Orb** — a menu-bar app face for the viewer (`app/`,
Electron) — and an **interactive portal into echoecho's Mac**. echoecho lives in the
menu bar as a code-drawn orb; clicking it (or saying "echoecho" — the wake
event arrives over the same SSE feed the web viewer uses) pours a black
procedural blob out of the menu bar, genie-style, into a transparent
always-on-top scene. Documents and transcript wisps emerge from the blob; the
"echoecho's Mac" item is a live VNC view of the Lume VM — interactive by default
(it's echoecho's Mac; the blast radius is the sandbox, and your shared folders stay
read-only) with a view-only toggle. Lume already runs a password-protected VNC
server even under `--no-display`, so the portal needs no VM changes: the app
asks the viewer's new `/vnc-info` endpoint (or `ECHOECHO_VNC_URL`) for the
endpoint, bridges WebSocket↔TCP locally, and renders it with noVNC. Because
VNC input lands at the virtual-hardware level, *you* can type in the VM even
where the agent's `osascript` keystrokes are still TCC-blocked (see the PR 14
caveat). Run it with `cd app && npm install && npm start`; the blob look-lab
lives in `app/prototypes/`. The web viewer at :8765 is unchanged.

The Orb installs as a real **echoecho.app** (Dock icon, Launchpad, Spotlight):
`bash scripts/echoctl.sh install-app` generates the icon procedurally (a
zero-dependency PNG encoder in `app/lib/icon.js`, `iconutil` → icns), packages
with `@electron/packager`, and drops it in `/Applications`. Opening echoecho.app
shows a **control panel** — daemon / VM / orb status plus Summon, Start/Stop
daemon, Wake / Reset echoecho's Mac, Update & relaunch (git pull → reinstall →
rebuild → reopen), and a start-at-login toggle. The same lifecycle commands
work from a terminal: `scripts/echoctl.sh {status|start-daemon|stop-daemon|
boot-vm|reset-vm|install-app|update|…}`; daemon env pins (e.g.
`ECHOECHO_INPUT_DEVICE`) live in `~/.echo/daemon.env`.

**The app and the wake word live and die together.** Launching echoecho.app
starts the wake-word daemon; quitting — or force-quitting — the app takes the
daemon down with it (the daemon tethers to the app's pid via
`ECHOECHO_TETHER_PID` and exits when that process disappears). The reverse
holds too: `start-daemon` from a terminal launches the app first if needed, so
whenever echoecho is listening there's a Dock icon saying so, and killing that
Dock icon always silences the mic. A bare `python echoecho.py --voice` in a
terminal stays untethered for debugging.

### Live Writer — talk out loud, it writes live

The **Live Writer** button on the control panel (or `bash
scripts/echoechoctl.sh live-writer`, or `python3 -m livewriter`) opens
<http://127.0.0.1:8799/>: dictate anything and a document writes itself while
you talk — the productionized version of `mockups/live-writer-demo.html`,
deliberately independent of the daemon/orchestrator. A streaming
transcription model (`gpt-live-transcribe`) hears you word-by-word (the gray
ghost tail), a server-side segmenter cuts utterances at pauses/punctuation,
and a formatter LLM (`gpt-5.4-mini`, ~0.7-0.9 s to first ink after an
utterance closes) streams small edit ops — new line / append / replace /
delete — that the page types out through the mockup's typewriter engine
(catch-up speed = backlog ÷ window). Fillers dropped, spoken numbers become
figures, enumerations become lists, self-corrections edit what's already on
the page, and "stop", "scratch that", "new paragraph", "make that a list",
"change X to Y", "heading …" work as commands. Saying **"stop."** halts the
pen instantly (generation counters cancel in-flight formatting). A typed
input box does the same keylessly; `LIVEWRITER_FAKE=1` runs the whole
pipeline with fakes for tests.

Playtests: `python3 scripts/livewriter_playtest.py` synthesizes each scenario
turn with TTS and streams it over the page's own websocket at mic pace
(`--browser` goes through headless Chrome's fake-mic path instead), asserts
objective expectations plus latency gates, and `--judge` adds an LLM grader
(fidelity / formatting / commands). `--generate N` has a model invent fresh
dictation scenarios across genres — the proof the mechanics are generic, not
fixture-tuned. Scenarios in `fixtures/livewriter/`; results in
`livewriter-results/<stamp>/report.md`; the iteration-by-iteration log lives
in [`livewriter/TESTING.md`](livewriter/TESTING.md). Keyless unit tests:
`python3 -m pytest tests/test_livewriter.py`.

Headless merge gate (runs all three scripted demos plus the generic agent.run
rewrite of them, asserts artifacts + task log, then the full test suite):
`bash scripts/demo_check.sh`.

### Troubleshooting

| Symptom | Fix |
|---|---|
| RMS meter all zeros in `--mic-check` | macOS TCC denied the mic to your terminal app: `sudo tccutil reset Microphone com.apple.Terminal` (or `com.googlecode.iterm2`, `com.microsoft.VSCode`), rerun, click Allow. |
| Phantom barge-ins / echoecho interrupts itself | Check startup logs for `WebRTC echo cancellation ... enabled`. If it says echo cancellation is unavailable, rerun `pip install -r requirements-mac.txt`; the fallback prevents self-triggering but cannot preserve barge-in. Prefer the built-in mic + speakers as a matched laptop pair, lower speaker volume, or use headphones if an external/very reverberant setup still leaks. |
| Wake word won't fire | Say it as two clear words, "echoecho". Check `python3 -m sounddevice` shows the right default input. Enter in the terminal is the manual-wake override. Rerun `bash scripts/fetch_models.sh` if `models/vosk-model-small-en-us-0.15/` is missing. |
| `pip install vosk` fails / no wheel | You're on an exotic Python; the macOS wheel is `vosk<=0.3.44` (any CPython 3.x). Use the pinned requirements-mac.txt in a plain venv. |
| Silent playback or PortAudio errors in conda | Use a plain uv/pyenv venv — conda's portaudio shadows the wheel-bundled one. |
| WS drops mid-conversation | The client reconnects with backoff automatically (watch for `[realtime] transport lost — reconnect n/3`); if it gives up, the session ends cleanly and the wake loop re-arms. |
| Voice mode melts down on demo day | `python3 echoecho.py --text` — same FSM, same workers, typed "echoecho"/"that's it"; the browser tab still updates live. |
| Viewer port taken | `ECHOECHO_VIEWER_PORT=8899 python3 echoecho.py --voice` (or `--no-viewer`). |

## Headless / sandbox development (no key, no audio)

- `bash scripts/demo_check.sh` — the merge gate: all three demos scripted
  (FakeLLM fixtures; live keyless WordPress/Wikipedia endpoints for search),
  workspace + `.tasks.jsonl` asserted, full pytest suite.
- `ECHOECHO_TEXT=1 ECHOECHO_FAKE_LLM=1 python3 echoecho.py --script fixtures/smoke.txt` — 60-line smoke run.
- `ECHOECHO_FAKE_AGENT_SCRIPT=fixtures/agent/demo_generic python3 echoecho.py --script fixtures/demo_generic.txt`
  — the generic demo: `agent.run` replayed from recorded stream-json fixtures
  (a directory is consumed in sorted order, one file per task; a single
  `.jsonl` replays for every task).
- `ECHOECHO_FAKE_LLM=1 python3 echoecho.py --text` — interactive keyless REPL.
- `python3 -m pytest tests/ -q` — everything except `-m network` live-endpoint tests.

## Silent voice E2E on your Mac (no speakers, real everything)

`scripts/playtest.py` drives the real voice model with text turns; it can
never catch what only the audio path breaks (wake spotting on real capture,
chimes, playback, barge-in, AEC, the recorder taps). The **silent voice
playtests** close that gap on a Mac without playing a sound out loud: every
turn is synthesized with `say(1)` and piped through a virtual loopback
device — [BlackHole 2ch](https://github.com/ExistentialAudio/BlackHole)
(`brew install blackhole-2ch`) — that echoecho is pinned to for both mic and
speaker. echoecho's own voice re-enters its mic through the loop, so the AEC
runs under exactly the laptop-speaker conditions it ships for. A monitor
records the loop the whole time; voiced audio outside the harness's own
playback windows is device-level proof echoecho *spoke*.

```bash
# from a checkout your live daemon is NOT using (a git worktree is perfect):
python3 scripts/voice_playtest.py --preflight     # sanity table (devices,
                                                  # key, model, wake-vs-say,
                                                  # silent loopback, lume)
python3 scripts/voice_playtest.py                 # every scenario but the VM
python3 scripts/voice_playtest.py --include-slow  # + VM/computer-use scenario
python3 scripts/voice_playtest.py --only 10_wake_and_roundtrip
```

Scenarios live in `fixtures/voiceplaytests/*.json` (same spirit as
`fixtures/playtests/`, plus `event_checks` / `http_checks` / `audio_checks`
sections and `~wake`, `~end`, `~play`, `~wait-task`, `~wait-voiced`,
`~say-nowait` turn directives). Shipped coverage: decoy-vs-wake-word +
spoken round-trip, checklist co-writing (viewer `/doc` included),
background-task weaving, `[since last session]` across two wakes, barge-in
(soft), and the Lume VM computer-use + `/vnc-info` portal surface (slow,
opt-in; pin `ECHOECHO_VM_GOLDEN` / `ECHOECHO_VM_SSH_KEY` if your image and key
predate the echoecho rename). Results + per-scenario recordings, workspace
snapshots, and the raw event feed land in `voice-playtest-results/<stamp>/`
with a `report.md`. The daemon under test gets its own viewer port,
recordings dir, and viewer token file, and the harness only ever signals the
PID it spawned — a live daemon elsewhere on the machine is never touched.

## Wake word + Mac audio notes

- `scripts/fetch_models.sh` downloads the Vosk small English model into `models/` (~40 MB, gitignored).
- The Vosk feed is suspended while a session is ACTIVE so echoecho saying "echo" can't self-trigger; wake/end chimes are synthesized sine waves, no asset files.
- During an ACTIVE session, the exact zero-padded PCM that reaches the output
  device is the WebRTC AEC reference. Capture is split into the processor's
  10 ms frames, then sent upstream in low-latency 20 ms Realtime appends. Raw
  `mic.wav` recordings stay untouched for diagnosis; only network audio is
  cleaned. If native AEC is unavailable, echoecho suppresses capture during
  speaker activity and its short acoustic tail, sacrificing barge-in safely.
- Detector behavior on the committed fixtures (`fixtures/audio/`, exercised by `tests/test_wake.py`): both "echoecho" WAVs fire; `decoy_single_echo`, `decoy_speech` and `decoy_gecko` do not ("gecko" decodes as `[unk] echo [unk] echo` — never the contiguous doubled phrase).

### AirPods / switching audio devices

PortAudio freezes its device list when it initializes, so a naive always-on
daemon never sees hardware that appears after startup — AirPods connected
mid-run would be invisible and output would stay on the Mac speakers. echoecho
re-initializes PortAudio and re-resolves both devices at **every session
boundary** (on wake, and again when the wake mic reopens at session end), so
after connecting AirPods just start a new session ("echoecho") — no restart,
no flags, no user action if you follow the system default. Each session logs
and shows what it bound (`🎧 mic: … → speaker: …` in the live transcript).

- `python3 echoecho.py --list-devices` — indexed table of every device with in/out
  channel counts and the current defaults.
- Pin devices with `--input-device SPEC` / `--output-device SPEC`, or the
  `ECHOECHO_INPUT_DEVICE` / `ECHOECHO_OUTPUT_DEVICE` env vars (flags win). A SPEC is a
  device index or a case-insensitive name substring (first match wins); empty
  means "follow the system default at each session start".
- **Bluetooth HFP gotcha:** using AirPods for BOTH input and output forces
  them into phone-call (HFP) mode and the sound quality craters. Best
  practice — built-in mic in, AirPods out:

  ```bash
  python3 echoecho.py --voice --input-device "macbook pro microphone" --output-device "pods"
  ```

## Session recordings — the dev feedback loop

Every `--voice` session records itself so each real use of the product can be
reviewed afterwards: listen to what actually happened, compare it against
what you wanted, and turn the gap into the next fix. A session directory
lands in `recordings/` (gitignored, local only) at every wake:

```
recordings/2026-08-12_183104_voice/
├── session.wav     open this one — stereo review mix: left = you, right = echoecho
├── mic.wav         what the mic heard (24 kHz mono; feed it to an STT to audit wake/VAD)
├── echo.wav        what actually reached the speaker, chimes included
├── transcript.md   review sheet: conversation + tools/tasks/injections, mm:ss offsets
├── events.jsonl    the raw event timeline (flushed per line — survives a crash)
└── meta.json       end reason, durations, devices, model, per-type counts
```

The review habit: `python3 echoecho.py --recordings` for the table of saved
sessions, open the newest `session.wav`, skim `transcript.md` next to it, and
note anything where what echoecho *did* wasn't what you *meant* — missed wakes
and false wakes, wrong tool/kind picked, results injected at awkward moments,
barge-in weirdness. `meta.json`'s `end_reason` tells you how sessions die
(`end_phrase` / `silence_timeout` / `crash` / `transport_closed`).

- Only ACTIVE sessions are recorded — never the idle wake-word listening.
  Recording starts at the wake chime and stops when the session closes.
- `session.wav` alignment is approximate: the echoecho track is shifted by the
  measured in+out device latency (`echo_delay_s` in meta.json) so barge-in
  timing reads true. If PortAudio reported overflows/underflows mid-session
  (`stream_status` in meta.json), treat fine-grained L/R timing with
  suspicion — dropped capture blocks shift the mic track.
- Opt out with `--no-record` (or `ECHOECHO_RECORD=0`); relocate with
  `ECHOECHO_RECORDINGS_DIR`. Disk math: ~5.8 MB per minute of session.
- `ECHOECHO_RECORD=1` also records `--text` / `--script` runs (events + transcript,
  no audio) — handy for reviewing scripted changes with the same tooling.
- Recording is never load-bearing: any failure (full disk, dead dir) prints a
  `[record]` note and the daemon keeps running.

## Status

Prototype complete as a stack of PRs (`echoecho/01-…` through `echoecho/06-…`), each headlessly testable — the full orchestrator/worker/artifact loop runs with no audio and no API key (text REPL + fixtures + FakeTransport event replay). Only the mic/speaker and the real Realtime connection need your Mac. The v2 generic build-out (`echoecho/10-…`, see [PLAN-GENERIC.md](PLAN-GENERIC.md)) replaces the stretch `code` kind with the first-class `agent.run` worker: the first agent CLI found on PATH (`claude`, then `codex`) runs each task with the workspace as its cwd.
