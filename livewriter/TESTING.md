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

### 9a — team_update stability ×3 + the destructive-op guard
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

### 10 — release-candidate: scripted ×2 + 4 fresh generative + the Mac
Scripted: 45/50 and 47/50 — team_update hit **11/11 with judge PASS** (the
guard holds; Diana survives). Fresh generative wave (sports recap, sea poem,
support case, Kyoto itinerary): 39/42 objective checks but harsh judge
verdicts exposing a new command class — mid-flow writing directives ("start
with the big moment:", "end with a couplet:") transcribed as prose — plus
deferred-fragment words being dropped ("Nobody had ever" vanished) and a
new-sentence merge that ate "The lighthouse keeper found the letter".
Meanwhile on the real Mac: WS-mode playtest passed 5/5 first try, but the
browser fake-mic recorded pure silence — server-side peak metering
(audio_peak=0 with 3.2 MB streamed) pinned it to a macOS Chrome quirk: fake
audio capture needs the audio service in-process
(--disable-features=AudioServiceOutOfProcess,AudioServiceSandbox). With the
flag: full demo recorded on the Mac — mic-piped dictation, live ASR,
formatter, typewriter — video+audio muxed to
livewriter-results/live-writer-demo-mac.mp4.
**Changes:** directive-execution + deferred-words + no-merge rules; harness
settle outlasts the editor pass (its fixes never landed in test runs
before); recorder gets the macOS audio-service flags and a --loop option.

### 11-12 — post-fix verification (scripted + all 8 generated scenarios)
Scripted: every judge verdict PASS (brainstorm 10/10/10). Generated rerun
found one systematic op bug: models write `find` WITH markdown
("dissolved oxygen **5.2**") though matching is plain-text — a retraction
and two editor repairs all silently dropped on it (while a chip claimed
success). Also: dictating "subject: X" in an email should write a Subject
line; and the judge's "invented 'no wind'" turned out to be the ASR
mishearing "no wait" — an end-to-end limitation, not a writer bug (the
72→71 correction still applied).
**Changes:** doc.replace retries with markdown-stripped find (unit-tested);
subject-line rule; editor restructures inline enumerations of 3+ items;
abrupt-disconnect teardown quieted.

### 13-14 — final validation
Scripted suite: **50/50 objective checks, all 6 judge verdicts PASS**
(40_brainstorm 10/10/10; the rest 8-10 fidelity with style-polish notes).
Pipeline speed: utterance close → first ink p50 0.72-1.17 s; the ghost tail
covers the wait word-by-word. All 8 generated scenarios: 75/86 objective
checks; remaining judge failures are mostly end-to-end ASR effects (proper
nouns normalized — "Trevor Lane"→"Trevor Lawrence" —, spelled codes) and
date-format-brittle early fixtures, plus occasional dropped/padded details
under dense dictation — the editor pass now also deletes unspoken content.

## Known limitations (honest list for the demo)

- ASR mishearings pass through: rare word substitutions ("no wait"→"no
  wind"), famous-name normalization, spelled codes/IDs. The writer is only
  as faithful as what it hears.
- Long utterances mean the first words of a sentence wait for the sentence
  to close before inking (p50 heard→ink 3-5 s); the gray ghost shows them
  live within ~0.5 s, which is what makes it feel instant.
- Register nuances (separate Ingredients vs Steps sections, numbered vs
  bulleted lists) land ~80% of the time; the judge's style notes track the
  gap.
