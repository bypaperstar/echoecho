# Echo — always-on voice agent prototype

Echo is a quick-and-dirty always-on voice agent for your Mac. Say **"echo echo"** to start a session, talk back and forth like ChatGPT voice, and Echo farms out real work (writing a doc with you live, building a grocery list while searching recipes, tutoring you on a new topic) to background worker agents — weaving results back into the conversation as they land. The session ends when you say **"that's it"** or after 10 minutes of silence.

See **[PLAN.md](PLAN.md)** for the full architecture, decisions (and what was rejected and why), the stacked-PR breakdown, demo scripts, and risks.

## Big pieces

1. **Wake word** — Vosk keyword spotting for "echo echo" (open source, no keys, no training).
2. **Voice loop** — OpenAI Realtime API speech-to-speech (`gpt-realtime-2.1[-mini]`), semantic VAD, barge-in, reconnect-with-backoff so the daemon never dies.
3. **Orchestrator** — a generic in-process task queue: the voice agent dispatches tasks, async workers do them, results are ranked (interrupt / ambient / silent) and injected back into the live conversation at safe turn boundaries. Tasks that finish while Echo is asleep are surfaced on the next wake as a "[since last session]" note.
4. **Live workspace** — everything workers produce is markdown in `workspace/`, rendered live in a browser tab via a tiny SSE auto-refresh viewer (changed sections flash briefly).

## Mac runbook (from zero to talking)

**HEADPHONES REQUIRED.** There is no echo cancellation over the raw WebSocket:
on speakers, Echo hears itself and barge-in goes haywire. AirPods or any
headphones — this is the one hard demo-day rule.

```bash
# 1. Clone, then make a plain venv (uv shown; pyenv/python3 -m venv also fine).
#    Do NOT use conda — its portaudio shadows the one bundled in the
#    sounddevice wheel and you get silent audio breakage.
uv venv .venv && source .venv/bin/activate

# 2. Install. requirements-mac.txt = requirements.txt + sounddevice, and pins
#    vosk<=0.3.44 (the newest release with a macOS universal2 wheel; plain
#    `pip install vosk` on Apple Silicon resolves to it anyway — the pin just
#    makes it explicit).
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
python3 echo.py --mic-check

# 7. Run the daemon (put on your headphones first):
python3 echo.py --voice
```

Then open the live workspace at <http://127.0.0.1:8765/>, say **"echo echo"**
(or press enter in the terminal as the manual-wake override), talk, and say
**"that's it"** when you're done. The wake loop idles at ~2% of one core and
$0 of API while no session is open.

### Live UI

The browser tab at <http://127.0.0.1:8765/> (override the port with
`ECHO_VIEWER_PORT`, skip it with `--no-viewer`) is the live "what is
happening" view, in two panes:

- **Left — transcript & activity.** The running conversation as chat bubbles
  (you right, Echo left) interleaved with plainly-explained activity lines:
  wake events, session state changes, tasks dispatched to background workers,
  worker completions (with what the interrupt/ambient/silent priority means),
  and results being handed back into the live conversation. A collapsible
  "How Echo works" box sits at the top. Driven by an append-only
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
- Demo day: `python3 echo.py --voice --model gpt-realtime-2.1` for the best
  voice + tool-calling quality. Sessions only exist between wake and
  "that's it", so even 2.1 is cents per demo.
- Worker LLM calls use the Responses API (`gpt-4o-mini` class); override with
  `ECHO_WORKER_MODEL`.

### The three demo one-liners

Full line-by-line scripts with timestamps: [`scripts/demo_cheatsheet.md`](scripts/demo_cheatsheet.md).

1. **Doc co-writing** — "Echo echo … let's write a one-page proposal for a team offsite in Lisbon." (then goals, agenda, "read me just the goals", "that's it")
2. **Groceries + recipes** — "Echo echo … help me plan dinners this week, I'm thinking pad thai one night." (then halloumi, "drop the fish sauce", "that's it")
3. **Learning** — "Echo echo. Teach me about fermentation in food." (then "sourdough", answer the quiz question, "that's it")

Headless merge gate (runs all three scripted, asserts artifacts + task log,
then the full test suite): `bash scripts/demo_check.sh`.

### Troubleshooting

| Symptom | Fix |
|---|---|
| RMS meter all zeros in `--mic-check` | macOS TCC denied the mic to your terminal app: `sudo tccutil reset Microphone com.apple.Terminal` (or `com.googlecode.iterm2`, `com.microsoft.VSCode`), rerun, click Allow. |
| Phantom barge-ins / Echo interrupts itself | You're on speakers. Wear headphones. Also check the wake chime isn't leaking into an external mic. |
| Wake word won't fire | Say it as two clear words, "echo, echo". Check `python3 -m sounddevice` shows the right default input. Enter in the terminal is the manual-wake override. Rerun `bash scripts/fetch_models.sh` if `models/vosk-model-small-en-us-0.15/` is missing. |
| `pip install vosk` fails / no wheel | You're on an exotic Python; the macOS wheel is `vosk<=0.3.44` (any CPython 3.x). Use the pinned requirements-mac.txt in a plain venv. |
| Silent playback or PortAudio errors in conda | Use a plain uv/pyenv venv — conda's portaudio shadows the wheel-bundled one. |
| WS drops mid-conversation | The client reconnects with backoff automatically (watch for `[realtime] transport lost — reconnect n/3`); if it gives up, the session ends cleanly and the wake loop re-arms. |
| Voice mode melts down on demo day | `python3 echo.py --text` — same FSM, same workers, typed "echo echo"/"that's it"; the browser tab still updates live. |
| Viewer port taken | `ECHO_VIEWER_PORT=8899 python3 echo.py --voice` (or `--no-viewer`). |

## Headless / sandbox development (no key, no audio)

- `bash scripts/demo_check.sh` — the merge gate: all three demos scripted
  (FakeLLM fixtures; live keyless WordPress/Wikipedia endpoints for search),
  workspace + `.tasks.jsonl` asserted, full pytest suite.
- `ECHO_TEXT=1 ECHO_FAKE_LLM=1 python3 echo.py --script fixtures/smoke.txt` — 60-line smoke run.
- `ECHO_FAKE_LLM=1 python3 echo.py --text` — interactive keyless REPL.
- `python3 -m pytest tests/ -q` — everything except `-m network` live-endpoint tests.

## Wake word + Mac audio notes

- `scripts/fetch_models.sh` downloads the Vosk small English model into `models/` (~40 MB, gitignored).
- The Vosk feed is suspended while a session is ACTIVE so Echo saying "echo" can't self-trigger; wake/end chimes are synthesized sine waves, no asset files.
- Detector behavior on the committed fixtures (`fixtures/audio/`, exercised by `tests/test_wake.py`): both "echo echo" WAVs fire; `decoy_single_echo`, `decoy_speech` and `decoy_gecko` do not ("gecko" decodes as `[unk] echo [unk] echo` — never the contiguous doubled phrase).

## Status

Prototype complete as a stack of PRs (`echo/01-…` through `echo/06-…`), each headlessly testable — the full orchestrator/worker/artifact loop runs with no audio and no API key (text REPL + fixtures + FakeTransport event replay). Only the mic/speaker and the real Realtime connection need your Mac. A stretch `code` task kind (headless `codex exec` / `claude -p` subprocess) registers automatically when one of those CLIs is on PATH.
