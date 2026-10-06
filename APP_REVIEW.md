# App review — October 6, 2026

Echoecho now has a usable desktop control surface, a dedicated GPT-Live voice
engine, clearer failure handling, and repeatable checks across the voice,
task, VM, viewer, dictation and installation paths.

## Findings and changes

| Area | Finding | Result |
| --- | --- | --- |
| Voice | The default used the smaller Realtime model; Live requires a different wire protocol. | GPT-Live 1 handles full-duplex speech and delegates through Responses to GPT-6 Luna. Existing tools remain the source of confirmed task outcomes. Account access failures fall back before audio upload or dispatch. |
| Session startup | Socket connection did not prove the server had accepted settings. | Both voice engines wait for the appropriate startup acknowledgment before microphone uploads. Timeouts are bounded and resources close on failure. |
| Voice lifecycle | Live lacks Realtime turn boundaries, and voice usage arrives cumulatively. | Independent captions, complete UTF-8 context chunks, correctly paired function-call outputs, duplicate-call suppression, an inactivity clock and graceful close with confirmed final usage. Live disconnections return to the wake loop; Realtime preserves recent verified conversation on bounded reconnects. |
| Desktop | Daemon status could imply listening even with missing capture; controls were scattered. | Fresh-capture status, microphone meter and device labels, manual wake/end, pause/resume, typed tasks, progress, cancellation and settings in a redesigned, keyboard-accessible panel. |
| Screen behavior | Voice activity summoned a desktop overlay. | Wake events do not summon the orb. The production orb is not always on top; opening the shared VM remains an explicit action. |
| Task execution | Work had an unbounded active backlog, failures could appear completed, and closing output pipes could bypass the task deadline. | A 128-task backlog cap, three execution slots, existing shared write locks, accurate error/canceled states, cancellation cleanup, and a deadline covering process exit as well as its pipes. GUI timeouts/cancellation also terminate owned process groups so descendants cannot hold pipes open; screenshot paths are shell-quoted. |
| Shared VM | Forced agent cleanup could delete the same guest used by the user. | Forced cleanup stops the guest and preserves its disk, even when local SSH has already exited. Explicit Reset remains the destructive operation. A forced stop can interrupt unsaved guest application work. |
| Audio | Wake microphone chunks could accumulate indefinitely; capture telemetry was invisible. | A three-second queue retains recent chunks, records drop counts, and exposes memory-only capture age and levels. Settings and HTTP commands execute on the audio owner's event loop. Failed native stream closure blocks unsafe PortAudio refreshes. |
| Workspace | Transcript updates could change the selected file, duplicate fetches and overwrite newer content with stale responses. | Pinned file selection, an explicit Follow newest control, update deduplication, generation guards, retryable errors and safe plain-text rendering when CDN libraries are unavailable. |
| Live Writer | Interrupted formatting could be acknowledged as written and claim a retry that never happened. | Failed batches stay unacknowledged and display a repeat-the-thought instruction. Existing streamed edits are not replayed automatically. Requests have bounded timeouts. |
| Local controls | New app controls need a protected boundary. | Per-run bearer authentication, origin/Host checks, bounded JSON bodies and read timeouts, an action allowlist, private atomic settings, navigation guards and no-store responses. |
| Installation | Replacement removed the working app before the copy succeeded. | Stage and verify the bundle first, replace with rollback on failure, use the locked dependencies, bake the actual version, and support install-app-closed. Development app termination is scoped to this repository. |
| Dependencies/checks | Electron and packager were old, and the repository lacked a full app check workflow. | Electron 44.5.1, packager 20.3.0, zero reported npm audit vulnerabilities, pinned CI actions, Python and Electron tests, and headless interaction checks on pull requests and main. |

## Model configuration

- `ECHOECHO_VOICE_MODEL`: `gpt-live-1` by default. Legacy
  `ECHOECHO_REALTIME_MODEL` pins continue to work.
- `ECHOECHO_BACKEND_MODEL`: `gpt-6-luna` for Live delegation, text conversations
  and worker LLM calls. Specific text/worker overrides still win.
- Live Writer retains its existing formatter/transcription model settings;
  agent CLIs retain their own model configuration.
- Private `~/.echoecho/preferences.json` stores only model, devices and recording
  choices. Saved choices override daemon environment pins; CLI flags win last.
- Live costs $0.05 per session minute plus backend use. Wake-word capture is
  local and does not open an API session. See the
  [Live model](https://developers.openai.com/api/docs/models/gpt-live-1) and
  [delegation protocol](https://developers.openai.com/api/docs/guides/live-delegation).

## Validation

Local checks passed: **533 Python tests**, **36 Electron unit tests**, and
**17 browser interaction checks**. Twelve tests requiring unavailable local
fixtures were skipped; three external-network tests remain explicitly opt-in.
The npm audit reports zero vulnerabilities. Shell/JavaScript syntax and Python
compilation also passed.

The keyless suite covers startup acceptance, access-only fallback, independent
captions, complete context injection, duplicate tool calls, overlapping
backend delegations, final usage, inactivity, audio queue limits, owner-loop
commands, paused typed tasks, persisted cancellation, failed task status,
process deadlines, VM disk preservation and installer rollback.

Electron unit checks cover truthful capture states and private settings.
Headless browser checks exercise the actual desktop renderer and workspace
page: ten desktop checks and seven workspace checks, with no page errors.
An actual Electron 44 process also loads and captures the control panel in a
Linux virtual display. Run commands are in README and the CI workflow.

## Remaining local verification

The Mac command connection timed out during the account-access probe. No new
Mac installation, Live account entitlement, microphone capture, audible voice
exchange, or interactive guest desktop has been confirmed in this review.
Screenshots from browser interaction checks use simulated daemon data.
Mac tests should use headphones or injected app audio, leave the Mac mini
alone, and finish with Echoecho closed. The build/install command for that is
`bash scripts/echoechoctl.sh install-app-closed`.
