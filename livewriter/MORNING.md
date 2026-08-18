# Good morning — the Live Writer is running

The page should already be open in Chrome at **http://127.0.0.1:8799/**
(if not, that URL — the server is running on this Mac; or
`bash scripts/echoechoctl.sh live-writer` restarts + opens it).

## Try it

1. Click **🎤 Start talking** (allow the mic once if Chrome asks).
2. Dictate anything — notes, an email, a recipe, a poem, numbers.
   Watch the gray ghost tail hear you word-by-word while the pen writes.
3. Say **"stop."** — the pen halts instantly. **"scratch that"** removes the
   last thing written. Try "make that a list", "new paragraph",
   "change X to Y", "heading …", a mid-sentence self-correction
   ("…goes to Marcus — no wait, Diana").
4. ⚙ has typing-feel settings; 📋 copies markdown; ⬇︎ saves the .md.
5. The typed input box at the bottom does everything the mic does.

## What got made overnight

- The demo screen recording (audio piped silently into Chrome as the mic):
  `livewriter-results/live-writer-demo-mac.mp4` (in this folder's repo).
- PR: https://github.com/bypaperstar/echoecho/pull/28 — includes the control
  panel **Live Writer ✍️** button (usable from the app after merge + update).
- 12+ logged test iterations: `livewriter/TESTING.md`; every run's document,
  event log, latencies and judge verdicts: `livewriter-results/*/report.md`.
- Re-run the tests any time:
  `python3 scripts/livewriter_playtest.py --judge` (scripted suite)
  `python3 scripts/livewriter_playtest.py --generate 4 --judge` (fresh ones)
