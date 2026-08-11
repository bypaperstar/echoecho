# Echo — always-on voice agent prototype

Echo is a quick-and-dirty always-on voice agent for your Mac. Say **"echo echo"** to start a session, talk back and forth like ChatGPT voice, and Echo farms out real work (writing a doc with you live, building a grocery list while searching recipes, tutoring you on a new topic) to background worker agents — weaving results back into the conversation as they land. The session ends when you say **"that's it"** or after 10 minutes of silence.

See **[PLAN.md](PLAN.md)** for the full architecture, decisions (and what was rejected and why), the stacked-PR breakdown, demo scripts, and risks.

## Big pieces

1. **Wake word** — Vosk keyword spotting for "echo echo" (open source, no keys, no training).
2. **Voice loop** — OpenAI Realtime API speech-to-speech (`gpt-realtime-2.1[-mini]`), semantic VAD, barge-in.
3. **Orchestrator** — a generic in-process task queue: the voice agent dispatches tasks, async workers do them, results are ranked (interrupt / ambient / silent) and injected back into the live conversation at safe turn boundaries.
4. **Live workspace** — everything workers produce is markdown in `workspace/`, rendered live in a browser tab via a tiny SSE auto-refresh viewer.

## Status

Prototype built as a stack of PRs (`echo/01-…` through `echo/06-…`), each headlessly testable — the full orchestrator/worker/artifact loop runs with no audio and no API key (text REPL + fixtures). Only the mic/speaker and the real Realtime connection need your Mac. The Mac runbook lands in the final PR of the stack.
