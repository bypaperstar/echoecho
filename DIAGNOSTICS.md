# Diagnostics and developer runs

echoecho writes a private, structured event stream for development and failure
analysis. It is separate from the transcript UI, task journal, and session
recordings: those describe product activity and can contain user-authored
content; diagnostics are intended to contain operational metadata.

The useful first command after reproducing a problem is:

```bash
bash scripts/echoechoctl.sh diagnostics --latest 3 --level warn --tail 100
```

It summarizes complete and still-running processes, counts events by component
and severity, lists correlation IDs, and shows recent warnings/errors and slow
spans. Files are flushed as events occur, so the command also works while the
app is running or after an abrupt crash.

## What is instrumented

The Python daemon records process/startup capabilities, uncaught and asyncio
errors, wake/tether state, viewer lifecycle, audio device and pipeline health,
voice-session and recorder lifecycle, Realtime connect/reconnect/protocol and
response timing, tool calls, injections, and orchestrator task queue/progress/
completion/failure paths.

Worker and service boundaries record agent subprocess preparation/spawn/timeout,
sandbox selection/fallback, VM clone/boot/readiness/recovery/reset, LLM request
timing and cost metadata, and web-search outcomes. GUI automation records the
selected SSH/VNC backend, credential-free endpoint classification, RFB
protocol/authentication phases, bounded input and framebuffer-capture outcomes,
wire/operation totals, and driver recovery. The Mac-only VM note proof has its
own structured run covering VM preparation, login probes, screenshots, and
note verification without retaining commands, credentials, or note content.
Live Writer has its own Python run covering server/client sessions, ASR
transport and protocol health, formatter queues/batches, reviewer staleness,
and send/session failures.

The Electron Orb records process and app lifecycle, unhandled errors, window
load/responsiveness/renderer exits, viewer connectivity, VNC setup and teardown,
control commands, login-item changes, summons/dismissals, and rejected renderer
reports. Detached daemon, Orb development, VM, Live Writer, and update commands
also retain their ordinary stdout/stderr as console logs.

The high-value IDs are propagated wherever that context exists:

- `run_id`: one Python or Electron process invocation.
- `session_id`: one active conversation.
- `task_id`: one orchestrator task, across queue, worker, progress, and result.
- `span_id`: one measured Python operation; `.start` and `.end` share it.
- `parent_run_id`: the launching process when a cross-process parent is known.

Python and Electron are separate processes and normally have separate `run_id`
values. Use `parent_run_id` when present, then timestamps and session/task IDs,
to follow an operation across the boundary.

## Files and retention

The default root is `~/.echoecho/diagnostics/`. Override it for an isolated run
with `ECHOECHO_DIAGNOSTICS_DIR` or the daemon's `--diagnostics-dir` flag.

```text
~/.echoecho/diagnostics/
├── 20260818T..._run-..._daemon.jsonl   Python daemon run
├── 20260818T..._run-..._daemon.part-001.jsonl  rotated Python part
├── orb-run-2026-...-<uuid>.jsonl       Electron run, part zero
├── orb-run-2026-...-<uuid>.1.jsonl     rotated Electron part
├── active-run-...-p<pid>-<component>.json  private Python liveness marker
├── latest*.json                         best-effort run pointers
└── console/
    ├── 20260818T...-<pid>-daemon.log   retained stdout/stderr
    ├── 20260818T...-<pid>-daemon.part-001.log  prior console part
    ├── daemon-current.log -> ...        convenience symlink
    └── ...                              orb, vm, livewriter, update
```

Pointer files are conveniences, not the source of truth or a liveness signal.
Every active Python run has its own strictly named, bounded `0600` marker, so
overlapping instances of one component protect all of their open JSONL parts.
On POSIX the process holds a kernel lock on its marker; clean shutdown removes
it, while a crash releases the lock so retention can prune the stale marker
without being confused by PID reuse. Exact-PID liveness is the portable
fallback. Marker enumeration and parsing are capped, never follow links, and
fail closed rather than allowing stale/crashed markers to create unbounded
retention. The inspector scans bounded JSONL input and
groups records by their embedded run IDs. Python keeps the newest 40-run,
14-day window by default, plus currently active runs (with active-marker
discovery capped at 256), and retains at most ten 10 MiB parts per live run.
Electron keeps 20 runs and 14 days and likewise retains at most ten
5 MiB parts per live run. Each lifecycle console launch keeps at
most five 5 MiB parts (the active file plus four archives), and each component
keeps at most 10 launch roots. That is a default hard ceiling of 250 MiB per
component (1.25 GiB across daemon, Orb, VM, Live Writer, and update). Archived
part 001 is newest; `echoechoctl logs` tails across the parts. A tail scans at
most 10,000 directory entries and 64 MiB of recent input, then emits at most
2,000 lines/8 MiB with each line capped at 64 KiB. All directories/files are created
with private permissions (`0700`/`0600`) where the platform supports POSIX
modes. Shared-directory enumeration is capped. If a startup cannot finish or
enforce retention, that diagnostic sink disables itself (console output is
discarded) instead of delaying the app or adding another unbounded file.

## Event formats

Each line is one JSON object. The two runtimes use intentionally small native
schemas; `scripts/diagnostics.py` normalizes both.

| Meaning | Python | Electron |
|---|---|---|
| time | `ts`, `wall_time`, `monotonic_ms` | `time` |
| identity | `run_id`, `seq` | `run_id`, `seq` |
| source | `component` | `surface` |
| classification | `level`, `event` | `level`, `event` |
| correlation | `context` | `fields` |
| details | `fields` | `fields` |
| timing/error | `duration_ms`, `exception` | usually `fields.duration_ms`, fingerprinted `fields.error` |

Python levels are `debug`, `info`, `warning`, `error`, and `critical`;
Electron uses `debug`, `info`, `warn`, and `error`. The inspector normalizes
`warning` to `warn`. Python spans emit `<name>.start` and `<name>.end`; the end
record owns `duration_ms`, `outcome`, and any exception.

Instrumentation is best-effort and never load-bearing. A failed diagnostic
write increments internal failure/drop counts when possible but does not fail
the app. A clean process writes a terminal summary; absence of that summary is
expected after a kill, crash, full disk, or failed sink.

## Inspection commands

```bash
# Most recent process run, warnings and errors only
bash scripts/echoechoctl.sh diagnostics

# Several runs, including informational lifecycle records
bash scripts/echoechoctl.sh diagnostics --latest 5 --level info --tail 200

# Restrict to one or more source substrings
bash scripts/echoechoctl.sh diagnostics --component realtime --component viewer

# Inspect a Mac-only scripts/vm_note_demo.py run
bash scripts/echoechoctl.sh diagnostics --component vm-note-demo --latest 3

# Show operations at least 250 ms in the slow-span section
bash scripts/echoechoctl.sh diagnostics --slow-ms 250

# Stable machine-readable report for another development agent
bash scripts/echoechoctl.sh diagnostics --latest 10 --json > /tmp/echoecho-diag-summary.json

# Dependencies, paths, key presence (never values), disk, and listening ports
bash scripts/echoechoctl.sh doctor

# Last 100 console lines, rendered safely for a terminal
bash scripts/echoechoctl.sh logs
bash scripts/echoechoctl.sh logs daemon 300

# Deliberately emit the underlying bytes for a trusted local investigation
bash scripts/echoechoctl.sh logs daemon 300 --raw
```

The inspector can also run directly:

```bash
python3 scripts/diagnostics.py --dir /path/to/diagnostics --latest 3
```

Supported filters are `--component`, `--latest`, `--tail`, `--level`, and
`--slow-ms`; add `--json` for automation. An incomplete final line, malformed
record, unreadable file, or unknown future field is reported but does not stop
valid files from being used. The reader treats the directory as untrusted:
terminal controls are escaped, symlinks outside the root are ignored, oversized
records are skipped, and `--latest`/`--tail` are capped at 100/2000. Discovery
stops after 50,000 entries/10,000 matching files; parsing is capped at 256 MiB,
one million records, 1,000 run summaries, and 20,000 retained event views. The
reader retains at most 512 file-error details globally and per run and reports
the number omitted. The report states when one of those limits truncated its
view.

## Reproduction workflow

1. Run `echoechoctl.sh doctor` and preserve its output with the bug notes.
2. Start the app/daemon normally. Note the wall-clock time and the user action
   that triggered the problem; do not delete prior diagnostics first.
3. Reproduce once, then run `diagnostics --latest 3 --level info --tail 200`.
4. Narrow noisy reports with `--component`; use `task_id`, `session_id`, and
   `span_id` to trace the surrounding records.
5. Inspect `logs <component>` only if the structured stream does not explain
   launch/native-process failures. Raw console logs have a weaker privacy
   boundary.
6. After a fix, repeat the same run and compare event counts, error records,
   span durations, and terminal outcome.

For a compact agent handoff, the JSON form is preferable to copying JSONL or
console files: it is bounded, normalized, and redacted a second time.

## Privacy and redaction

Structured diagnostics redact credential-shaped keys and strings. Python also
replaces transcript, audio, prompt, instruction, body, output, and other
content-bearing fields by default. Electron hashes arbitrary error messages
and redacts URLs, host addresses, filesystem paths, and sensitive/content keys.
Python exception stacks retain basenames/line numbers rather than private
directory trees, and unknown web hosts are fingerprinted. The inspector
recursively redacts again before printing either text or JSON.

This is defense in depth, not a guarantee that arbitrary newly added fields are
safe. Prefer counts, lengths, booleans, states, stable enum values, durations,
hashes/fingerprints, and correlation IDs. Never log raw audio, transcripts,
prompts, API payloads, authorization headers, URLs with credentials, or entire
environment/config objects.

`console/*.log` is raw legacy stdout/stderr. It can contain exception text,
paths, model/service messages, product content, or terminal control bytes.
`echoechoctl.sh logs` escapes terminal controls by default; its `--raw` option
is an explicit unsafe opt-in for trusted local output. Review console logs
manually before sharing. Session `recordings/`, `workspace/.events.jsonl`, and
`.tasks.jsonl` have separate product purposes and are not covered by
diagnostics redaction.

Python supports `ECHOECHO_DIAGNOSTICS_INCLUDE_CONTENT=1` for a deliberate local
investigation. That makes the on-disk JSONL sensitive even though the inspector
will still hide known content keys. Use it only in an isolated diagnostics
directory, do not share the raw file, and remove it when the investigation is
complete.

## Configuration

| Setting | Effect | Default |
|---|---|---|
| `ECHOECHO_STATE_DIR` | lifecycle-script state root (`daemon.env`, derived diagnostics root) | `~/.echoecho` |
| `ECHOECHO_DIAGNOSTICS_DIR` | shared structured/console root | `~/.echoecho/diagnostics` |
| `ECHOECHO_DIAGNOSTICS` | enable structured writes (`0` disables) | `1` |
| `ECHOECHO_DIAGNOSTICS_RETENTION_DAYS` | Python structured-run age retention | `14` |
| `ECHOECHO_DIAGNOSTICS_MAX_RUNS` | retained Python/Electron run roots | Python `40`, Electron `20` |
| `ECHOECHO_DIAGNOSTICS_MAX_EVENT_BYTES` | maximum encoded Python event | `32768` |
| `ECHOECHO_DIAGNOSTICS_MAX_RUN_BYTES` | maximum Python JSONL part before rotation | `10485760` |
| `ECHOECHO_DIAGNOSTICS_MAX_BYTES` | maximum Electron JSONL part before rotation | `5242880` |
| `ECHOECHO_DIAGNOSTICS_MAX_PARTS` | retained structured parts per Python/Electron live run | `10` |
| `ECHOECHO_DIAGNOSTICS_MAX_AGE_DAYS` | Electron age retention | `14` |
| `ECHOECHO_CONSOLE_MAX_BYTES` | detached console bytes per part | `5242880` |
| `ECHOECHO_CONSOLE_MAX_PARTS` | console parts per long-lived launch, active included | `5` |
| `ECHOECHO_CONSOLE_MAX_RUNS` | retained console launch roots per component | `10` |
| `ECHOECHO_DIAGNOSTICS_MAX_STRING` | Python string bound | `1200` |
| `ECHOECHO_DIAGNOSTICS_MAX_ITEMS` | Python collection bound | `50` |
| `ECHOECHO_DIAGNOSTICS_MAX_DEPTH` | Python nesting bound | `6` |
| `ECHOECHO_DIAGNOSTICS_MAX_NODES` | Python total values sanitized per event | `512` |
| `ECHOECHO_WAKE_HEARTBEAT_S` | idle wake-mic/detector liveness interval | `60` |
| `LIVEWRITER_MAX_MESSAGE_BYTES` | maximum Live Writer WebSocket message | `1048576` |
| `ECHOECHO_DIAGNOSTICS_INCLUDE_CONTENT` | include Python content fields; sensitive | `0` |

`python3 echoecho.py --no-diagnostics` disables the daemon sink, while
`--diagnostics-dir PATH` relocates it. For both the Orb and daemon to use a
non-default shared path, place `ECHOECHO_DIAGNOSTICS_DIR` in their launch
environment. `~/.echoecho/daemon.env` is sourced by daemon lifecycle commands
and therefore configures the daemon, not an Orb that was already launched.
