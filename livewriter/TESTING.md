# Live Writer — test & improvement log

Every entry is one full test iteration (real ASR + real formatter unless
noted), what it caught, and what changed in response. Detailed artifacts per
run live in `livewriter-results/<stamp>/` (gitignored): per-scenario
`doc.md`, `session.jsonl`, `result.json`, `report.md`.

## Method

- **Scripted scenarios** (`fixtures/livewriter/*.json`) pin the core
  mechanics: filler dropping, figures, lists, name corrections, stop,
  scratch-that, email/recipe/story registers, number-dense speech.
- **Generative scenarios** (`--generate N`) have a strong model invent fresh
  dictations across genres (with disfluencies, self-corrections, spoken
  commands) plus objective expectations — proof the mechanics are generic,
  not tuned to the fixtures.
- **Checks** are objective (contains/regex/list counts/halt events); the
  **judge** (`--judge`, gpt-5.2) grades fidelity / formatting / command
  execution 0-10 and fails anything invented or any command typed as text.
- **Latency** is measured per utterance: first word heard → first ink on the
  page (`heard_to_ink`), utterance closed → first ink (`final_to_ink`), and
  formatter time-to-first-op (`fmt_ttfop`).

## Iterations

### 1 — first real run (10_team_update), 2026-08-18
gpt-live-transcribe + gpt-4.1-mini. 9/11 checks on the first try; correction
(Marcus→Diana), stop, and scratch-that all worked. Caught: no title heading
created; list items duplicated fragments; "Just say X" transcribed literally;
fmt ttfop p50 1.67 s (prompt too fat), heard→ink p50 3.8 s.
**Changes:** tighter system prompt (title-heading rule, "just say X" command,
list anti-duplication), segmenter pause 650→550 ms.

### 2 — full 6-scenario suite
43/50 checks. Latency improved (ttfop p50 ~0.9 s). Caught four real product
bugs: (a) formatter INVENTED list items ("escape room") the speaker never
said — worst-case failure for dictation; (b) punctuation seams
("Tuesday., wedged") when ASR splits a sentence; (c) meta-asides leaking in
("Okay.", "Here's the opening of the story."); (d) body text appended onto a
heading line. Also: the segmenter over-split (17 utts for 6 turns) because
gpt-live-transcribe delivers deltas in bursts, and a mid-sentence spoken
"stop" was semantically transcribed as "staff" — a real stop has
pause + prosody, so the fixture was speaking it wrong.
**Changes:** STRICT-FIDELITY + fragment-holding + punctuation-seam rules and
two few-shot examples in the prompt; pause 550→850 ms with a clause-boundary
(comma at ≥14 words) fast path; fragments (<3 words) held 1.8 s before
emitting; stop spoken as its own turn in the fixture; default formatter
switched to gpt-5.4-mini (reasoning effort none) pending the A/B below.

### 3 — A/B: gpt-5.4-mini vs gpt-4.1-mini, judge on
Full suite twice in parallel. **gpt-5.4-mini (reasoning effort none) wins**:
48/50 checks vs 46/50, formatter first-op p50 713-897 ms vs 1016-1906 ms, and
the invented-content failures disappeared. The trace of the name correction is
the vision working: "goes to Marcus." → wrote Marcus; "no wait, not Marcus" →
replaced with "someone else" (a placeholder!); "It goes to Diana" → replaced
with Diana. Remaining judgment bugs: a lead-in ("One more thing") appended to
the Diana line so a later "scratch that last part" deleted Diana with it; an
email got an invented heading from the meta instruction and ignored "new
paragraph"; and the brainstorm fixture itself was wrong — "stop" halts the pen
but does not erase written text (that is "scratch that", exactly like the
mockup's scene 6), so the fixture now stops *and* scratches.
**Changes:** default model gpt-5.4-mini; prompt rules for lead-ins/new
thoughts, surgical scratch, no-heading-on-emails, always-honor "new
paragraph"; 40_brainstorm speaks "Stop." then "Scratch that."
