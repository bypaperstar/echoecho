# mockups/ — design prototypes

Repo-shipped, self-contained HTML prototypes for aligning on look & feel
before building the real thing. Nothing here talks to a real model.

Two ways to open one:

1. **Standalone** — double-click the `.html` file (Chrome or Safari on macOS;
   the speaker voices use the system TTS).
2. **From the running app** — the workspace viewer serves every
   `mockups/*.html` at `http://127.0.0.1:8765/proto/<name>` (linked as
   “prototypes” in the viewer header). Repo files are trusted like the
   viewer's own index.html; agent-written workspace files still never run
   as HTML.

## live-writer-demo

The "talk out loud → the agent writes and editorializes live" vision mockup:
a ~60 s scripted dictation (synthesized voice) with simulated ASR + LLM
formatter latency, a typewriter engine that smooths bursty output
(catch-up speed = backlog ÷ window), live word corrections, list
restructuring, instant "stop", a ghost tail of heard-but-unformatted words,
and a tuning sidebar with instant/realistic/stress presets. A **Live mic**
tab runs the same pipeline on your real voice (Chrome) with commands:
"stop", "scratch that", "new paragraph", "make that a list",
"change X to Y", "heading …".

Headless test hooks: `?autoplay=1&mute=1&test=1&fast=4` (scripted run,
milestones in `#testlog`), `?mictest=1&mute=1&test=1&fast=4` (simulated
mic-command sequence). Drive-by-interval keeps it running under
`--virtual-time-budget`, where rAF barely fires.
