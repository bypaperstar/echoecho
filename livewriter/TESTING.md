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

### 4 — scripted suite after the rule changes
46/50 checks, but the runs exposed the real enemy: **variance**. The same
prompt that produced a perfect team update in the browser run (it. 6) dropped
the Diana assignment and the list here, and the recipe lost its quantities.
Sampling at temperature 1 (gpt-5 models take no temperature) + ASR
nondeterminism means prompt-only fixes cannot pin fidelity.
**Changes:** none yet — evidence gathering.

### 5 — first generative scenarios (gpt-5.2 invents, gpt-5.2 judges)
Four fresh genres (wetland-frog field notes, Q3 meeting notes, garlic-noodle
recipe, airport-pickup email). Recipe 11/11, meeting notes 10/11 — the
mechanics generalize. The bio scenario "failed" with fidelity 0 — traced to a
harness race, not the product: a parallel unit test's session overwrote the
LAST pointer the harness read results from. Also caught: generator-authored
checks were format-brittle ("august 23, 2026" vs "Aug 23").
**Changes:** `ready` carries `session_dir` and the harness reads its own
session's artifacts (never `/last/*`); unit test uses an ephemeral port;
generator guidance: contains-checks must be single words/figures,
format-sensitive expectations go to the judge.

### 6 — browser E2E (headless Chrome, WAV as fake mic)
The full real path — getUserMedia → AudioWorklet resample → websocket → live
ASR → formatter → typewriter — produced **11/11 checks and the ideal
document** (heading, list, Marcus→Diana correction, stop + scratch clean).
The page code works end to end without a human.

### 7 — the editor pass behind the pen
Added the Reviewer: when the pen is idle ≥3 s (≥12 s between passes) it diffs
the full transcript (stop-interrupted utterances marked) against the document
and emits minimal corrective ops; a pass discards itself if new speech
arrived meanwhile. Suite: brainstorm 10/10/10, email 9/9/10 pass, recipe
fidelity 10. Caught: the editor *reinstated* deliberately-dropped filler
("Okay, so, um, quick update…") as "missing content" — seen live in the
recording-pipeline test.
**Changes:** editor prompt narrowed to SUBSTANTIVE content with an explicit
"dropped fillers/meta are correct" clause and a strong when-in-doubt-do-
nothing bias.

### 8 — generative rerun (race-fixed)
Recipe 11/11; the wetland-bio scenario is a beautifully brutal fidelity
stress (dense figures, a temperature correction, a retraction command) and
caught real misses: correction not applied, retracted value kept, "no wind"
invented, label dropped. Ran pre-it7/9 fixes; kept as the hard fixture.

### 9 — team_update stability ×3 + the destructive-op guard
Runs 1-2 (pre-guard): Diana lost again — the trace showed the model, on
hearing the lead-in "One more thing", emitting `delete` on the list item
holding Diana and re-creating "One more thing" as a fresh line. A prompt
can't reliably stop that, so the server now BLOCKS destructive ops
mechanically: `delete` / replace-to-empty are dropped unless the speech batch
contains an explicit correction/deletion word (scratch, remove, no wait, i
mean, change, …). A replace that empties a line now also deletes the line (no
dangling "- " bullets), mirrored in the page. Stopped utterances are kept in
the writer's context marked "(stopped)" so "scratch that last part" has its
antecedent; prose register hardened (no bold, no invented headings, near-
verbatim).
