# Echo — always-on voice agent prototype

Echo is a quick-and-dirty always-on voice agent for your Mac. Say **"echo echo"** to start a session, talk back and forth like ChatGPT voice, and Echo farms out real work (writing a doc with you live, building a grocery list while searching recipes, tutoring you on a new topic) to background worker agents — weaving results back into the conversation as they land. The session ends when you say **"that's it"** or after 10 minutes of silence.

See **[PLAN.md](PLAN.md)** for the full architecture, decisions (and what was rejected and why), the stacked-PR breakdown, demo scripts, and risks.

## Big pieces

1. **Wake word** — Vosk keyword spotting for "echo echo" (open source, no keys, no training).
2. **Voice loop** — OpenAI Realtime API speech-to-speech (`gpt-realtime-2.1[-mini]`), semantic VAD, barge-in.
3. **Orchestrator** — a generic in-process task queue: the voice agent dispatches tasks, async workers do them, results are ranked (interrupt / ambient / silent) and injected back into the live conversation at safe turn boundaries.
4. **Live workspace** — everything workers produce is markdown in `workspace/`, rendered live in a browser tab via a tiny SSE auto-refresh viewer.

## Wake word + Mac audio (PR 5)

- `scripts/fetch_models.sh` downloads the Vosk small English model into `models/` (~40 MB, gitignored).
- `python3 echo.py --mic-check` — step zero on the Mac: device list + live RMS meter. All-zero RMS means macOS TCC denied the mic; fix with `sudo tccutil reset Microphone com.apple.Terminal` and re-grant.
- `OPENAI_API_KEY=... python3 echo.py --voice` — always-on daemon: say "echo echo" (or press enter/spacebar+enter as the manual-wake override) to open a session; wake/end chimes are synthesized sine waves, no asset files. The Vosk feed is suspended while a session is ACTIVE so Echo saying "echo" can't self-trigger.
- Detector behavior on the committed fixtures (`fixtures/audio/`, exercised by `tests/test_wake.py`): both "echo echo" WAVs fire; `decoy_single_echo`, `decoy_speech` and `decoy_gecko` do not ("gecko" decodes as `[unk] echo [unk] echo` — never the contiguous doubled phrase, so no false fire; nothing to tune in PR 6).

## Status

Prototype built as a stack of PRs (`echo/01-…` through `echo/06-…`), each headlessly testable — the full orchestrator/worker/artifact loop runs with no audio and no API key (text REPL + fixtures). Only the mic/speaker and the real Realtime connection need your Mac. The Mac runbook lands in the final PR of the stack.
