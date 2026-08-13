# echoecho — Prototype Plan

## What we're building (3 sentences)

echoecho is an always-on voice agent for the user's Mac: a Vosk-based wake loop listens for "echoecho," then opens a ChatGPT-like OpenAI Realtime speech-to-speech session that stays live until 10 minutes of silence or an end phrase like "that's it." During conversation, the voice agent dispatches tasks (doc editing, recipe search, topic learning) to a generic in-process orchestrator that runs async workers, ranks results by urgency, and injects speech-ready summaries back into the live session at safe turn boundaries — so echoecho weaves finished work into the ongoing conversation. Everything renders into a `workspace/` folder of markdown files shown in a browser tab via a tiny SSE auto-refresh viewer, and the entire orchestrator/worker/artifact loop runs headlessly (text REPL, fake LLM, no audio, no API key) in our Linux sandbox — only mic/speaker and the real Realtime connection are Mac-only.

## Decisions

- **Wake word**: Vosk grammar-mode keyword spotting — `pip install vosk` (resolves to 0.3.44 universal2 wheel on macOS; pin `vosk<=0.3.44` there) + `vosk-model-small-en-us-0.15` (~40 MB from alphacephei.com). `KaldiRecognizer(model, 16000, '["echo", "[unk]"]')` with partial words; fire on "echoecho" in a partial/final, then reset. Verified in our sandbox at 51x realtime (~2% of a core always-on, 0.3–0.7 s latency). Zero training, zero keys, identical code path sandbox↔Mac. *Rejected*: Porcupine (free tier dead June 2026), openWakeWord (ONNX broken on Apple Silicon, issue #336), livekit-wakeword (kept as documented fallback only — needs a training run and Python 3.11+).
- **Voice**: OpenAI Realtime API over raw WebSocket (`wss://api.openai.com/v1/realtime?model=...`, Bearer auth, GA API — no beta header). Model `gpt-realtime-2.1-mini` as the dev/default flag, `gpt-realtime-2.1` for demo day. `semantic_vad` with `interrupt_response: true`, input transcription enabled, 24 kHz mono pcm16 via `input_audio_buffer.append` / `response.output_audio.delta`, client-side `conversation.item.truncate` on barge-in. Exactly 4 tools: `dispatch_task`, `check_tasks`, `read_artifact`, `end_session`.
- **Orchestration**: one Python asyncio process; ConversationAgent → inbox `asyncio.Queue[TaskRequest]` → orchestrator with in-memory task table + worker registry (`kind -> async handler`) → workers as `asyncio.create_task` → `TaskResult{say, priority, data, follow_ups[], artifacts_touched[]}` → priority ranking (interrupt/ambient/silent) → single turn-boundary injection gate into the voice session. Chaining is the generic `follow_ups` mechanism (recipe.search auto-fires grocery.merge); the orchestrator knows nothing about recipes or docs. No Redis, no IPC, no multi-process.
- **Workers + search**: 5 task kinds — `doc.edit` (LLM full-file markdown rewrite), `recipe.search` (WordPress REST `wp-json/wp/v2/search` on the sandbox-verified whitelist recipetineats.com / pinchofyum.com / bbcgoodfood.com → `recipe-scrapers` 15.10.0), `grocery.merge` (LLM dedupe/aisle-group, keyless regex fallback), `learn.outline` / `learn.deep_dive` (Wikipedia opensearch + Action API `list=search` + `prop=extracts`; the REST related endpoint is decommissioned — avoided). Worker LLM calls use the Responses API (gpt-4o-mini-class; hosted `web_search` tool available Mac-side) behind an `ECHOECHO_FAKE_LLM=1` fixture switch. Fallbacks coded: DuckDuckGo HTML endpoint, then a hardcoded dish→URL map. *Rejected*: Brave (free tier dead), `ddgs` package (needs Python ≥3.10); Tavily documented as non-OpenAI research fallback.
- **Language/runtime**: Python, 3.9-compatible syntax (no `match`, no `X | Y` unions), exact pins `openai==2.48.0` + `websockets==15.0.1` (verified pip-resolvable on the sandbox's 3.9; also fine on the Mac's 3.11+ via plain uv venv — not conda, which shadows sounddevice's bundled PortAudio). No `openai-agents` SDK (needs ≥3.10); the raw Realtime client is ~150 lines and is what we want for mockability anyway. Node 24 + `@openai/agents-realtime` is the documented escape hatch, not built. Audio via `sounddevice` (pip wheel bundles PortAudio on macOS, no brew).
- **Live view**: `workspace/` markdown files (doc.md, grocery.md, notes.md) + one ~60-line stdlib HTTP+SSE viewer (`GET /` page with marked.js from CDN, one tab per file, auto-focus most recently modified; `GET /events` SSE from a 250 ms mtime poll). All writes atomic (tmp + `os.rename`). Headless "viewer" is `cat` / `curl`.

Conflict calls between the two designs: package dir is `echoecho_app/` not `echoecho/` (a package named `echoecho` next to `echoecho.py` collides in Python imports); priority tiers are three (interrupt/ambient/silent), not four (a separate "notify" tier adds a distinction the gate doesn't act on differently); silence timer resets on user `speech_started` **and** completed assistant responses (Design B — prevents timing out a user who's listening); PR 1 proves the full lifecycle with a scripted keyless agent (Design B's ordering — de-risks the star component first) while keeping Design A's viewer-with-textmode and wake-with-audio PR pairings; branch naming `echoecho/NN-slug` per spec.

## Architecture

### Session lifecycle state machine (`conversation/session.py`)

```
            "echoecho" (Vosk partial) / typed line / spacebar override
  ┌──────┐ ─────────────────────────────────────────────────────────►  ┌────────┐
  │ IDLE │                                                             │ ACTIVE │
  └──────┘ ◄───────────────────────────────────┐                       └───┬────┘
    • wake loop feeding Vosk (~2% core)        │                           │
    • no Realtime WS, zero API cost            │        end_session() tool │
    • orchestrator alive; late worker      ┌───┴────┐   OR transcript      │
      results land in workspace + table    │ ENDING │◄─ regex \bthat'?s    │
    • viewer serving                       └────────┘   (it|all)\b        │
                                             • spoken sign-off  OR 10-min silence
                                               (skip on timeout)  timer expiry
                                             • close WS, end chime
                                             • in-flight workers keep running
                                             • resume wake loop, fresh recognizer

IDLE → ACTIVE:  pause wake feed (echoecho saying "echo" can't self-trigger), play chime,
                connect Realtime WS, session.update (instructions, 4 tools,
                semantic_vad + interrupt_response, input transcription), inject
                "[since last session] ..." item for tasks finished while IDLE,
                start silence clock.
ACTIVE:         full-duplex voice; silence clock resets on input_audio_buffer.
                speech_started and on each completed assistant response; barge-in
                flushes playback + sends conversation.item.truncate{item_id,
                audio_end_ms} from PlaybackTracker's played-ms bookkeeping; at
                ~55 min (60-min API cap) best-effort summary + reconnect + replay
                (cookbook pattern).
Text mode:      identical FSM — typed "echoecho" / "that's it", stdin lines reset
                the clock, orchestrator/workers/viewer untouched.
```

### Component diagram

```
                Mac only                                     everywhere (sandbox-identical)
┌──────────────────────────────────────┐   ┌─────────────────────────────────────────────┐
│ mic 16k int16 (sounddevice, 100ms)   │   │ LAYER 2: ORCHESTRATOR (orchestrator/core)   │
│   └► wake/detector.py  Vosk grammar  │   │  • inbox: asyncio.Queue[TaskRequest]        │
│      '["echo","[unk]"]' ─ WAKE ──┐   │   │  • task table {task_id: Task}               │
└──────────────────────────────────┼───┘   │  • registry: kind -> Worker.run             │
                                   ▼       │  • follow_ups[] re-enqueued generically     │
┌──────────────────────────────────────┐   │  • ranker: interrupt / ambient / silent     │
│ LAYER 1: CONVERSATION AGENT          │   │  • .tasks.jsonl append-only log             │
│  port.py = ConversationPort (swap):  │   └────┬───────────────────────▲────────────────┘
│   • realtime.py  gpt-realtime-2.1    │        │ create_task           │ TaskResult
│     [-mini], raw WS, semantic_vad,   │        ▼                       │ {say, priority,
│     barge-in truncate, reconnect     │   ┌─────────────────────────────────────────────┐
│   • textmode.py  stdin REPL +        │   │ LAYER 3: WORKERS (workers/base.py protocol) │
│     Responses-API tool loop (keyed)  │   │  doc.edit │ recipe.search │ grocery.merge   │
│   • scripted.py  keyless fixtures    │   │  learn.outline │ learn.deep_dive │ (code)   │
│  session.py = lifecycle FSM          │   │  via services/llm.py (Real|Fake) and        │
│  audio.py 24k pcm16 I/O (Mac only)   │   │  services/web.py (WP search, recipe-        │
└──────────┬───────────────▲───────────┘   │  scrapers, Wikipedia — keyless, verified)   │
 Contract A│               │               └──────────────┬──────────────────────────────┘
 4 tools ──┘   Injection{text,priority}                   ▼ atomic tmp+rename
 dispatch_task/check_tasks/                workspace/*.md ──► viewer/server.py (stdlib
 read_artifact/end_session                 HTTP+SSE, 250ms mtime poll) ──► browser tab
                                           (marked.js tabs, auto-focus latest file)
```

Two narrow contracts keep the layers honest: **Contract A** (conversation↔orchestrator) is the 4 tools down and `Injection{text, priority}` up; **Contract B** (orchestrator↔workers) is `async run(task, ctx) -> TaskResult`. All three demos flow through these unchanged — demo specificity lives only in worker modules and one line each in the system prompt's task-kind list.

### Message flow: one task round-trip (voice → orchestrator → worker → voice)

```
 1. User (voice): "I'm thinking pad thai one night."
 2. Realtime server → response.done containing function_call
    {name:"dispatch_task", arguments:{kind:"recipe.search", instructions:"pad thai"}}
 3. Client enqueues TaskRequest to orchestrator inbox and IMMEDIATELY sends
    conversation.item.create {type:"function_call_output", call_id,
    output:'{"task_id":"t3","status":"queued"}'} + response.create
    → echoecho says "On it — searching for a good pad thai" and keeps talking.
    (The voice loop NEVER blocks on a worker.)
 4. Orchestrator: registry["recipe.search"] runs as asyncio task → WP REST search
    → fetch top URLs (browser UA) → recipe_scrapers.scrape_html → best pick.
 5. Worker returns TaskResult{say:"Found a 30-minute chicken pad thai on RecipeTin
    Eats — added 9 items to your list", priority:"interrupt", data:{...},
    follow_ups:[TaskRequest(kind="grocery.merge", ingredients=[...])],
    artifacts_touched:["grocery.md"]}.
 6. Orchestrator re-enqueues the grocery.merge follow-up (generic chaining), logs to
    .tasks.jsonl; grocery.md is rewritten atomically → SSE fires → browser rerenders.
 7. Injection gate waits for a safe turn boundary: last event == response.done AND no
    speech_started without a matching speech_stopped. Then:
      conversation.item.create {role:"system", text:"[task t3 done] Found a
      30-minute chicken pad thai... Weave in naturally."}
      + (priority==interrupt) response.create  → echoecho speaks the result.
    ambient → item only, woven into next natural turn; silent → task table only
    (surfaced by check_tasks). Never response.create mid-user-speech.
 8. After artifacts_touched, gate also injects an ambient compact snapshot of the
    changed file, so "read me just the goals / the list" always works.
    In text mode, steps 7–8 print "[task t3 done] ..." lines into the REPL.
```

## Repo layout

```
echoecho/
├── README.md                      # quickstart; Mac runbook (uv venv, TCC mic reset, model download, headphones rule)
├── requirements.txt               # openai==2.48.0, websockets==15.0.1, vosk, recipe-scrapers (all 3.9-safe)
├── requirements-mac.txt           # + sounddevice==0.5.5; note: pin vosk<=0.3.44 on macOS
├── echoecho.py                        # entrypoint: --text/--voice/--fake-llm/--model/--script/--mic-check; wires layers, runs daemon loop
├── echoecho_app/
│   ├── config.py                  # env flags (ECHOECHO_TEXT, ECHOECHO_FAKE_LLM, ECHOECHO_REALTIME_MODEL), paths, end-phrase regex, timeouts (silence=600s)
│   ├── bus.py                     # typed contracts: TaskRequest, Task, TaskResult{say,priority,data,follow_ups,artifacts_touched}, Injection
│   ├── conversation/
│   │   ├── port.py                # ConversationPort ABC: run(), inject(Injection), on_tool(cb), end() — Contract A
│   │   ├── session.py             # IDLE/ACTIVE/ENDING FSM, silence timer (injectable clock), end-phrase regex, 55-min reconnect, injection gate
│   │   ├── realtime.py            # raw Realtime WS client: session.update, event pump, tool handling, barge-in truncate (~150 lines)
│   │   ├── audio.py               # Mac-only (import-guarded): 24k pcm16 capture/playback, PlaybackTracker (played-ms per item), chimes
│   │   ├── textmode.py            # stdin/stdout REPL: Responses-API tool loop implementing Contract A (keyed, no audio)
│   │   └── scripted.py            # keyless fixture-driven agent (canned turns + tool calls) for sandbox CI
│   ├── orchestrator/
│   │   ├── core.py                # inbox queue, task table, worker registry, follow_up chaining, injection policy
│   │   ├── ranker.py              # priority heuristic: error/needs_input or primary result → interrupt; enrichment → ambient; bookkeeping → silent
│   │   └── log.py                 # append-only workspace/.tasks.jsonl + replay for debugging
│   ├── workers/
│   │   ├── base.py                # Worker protocol + @register('kind') decorator — Contract B
│   │   ├── doc_edit.py            # doc.edit: LLM full-file markdown rewrite (demo 1)
│   │   ├── recipe.py              # recipe.search: WP REST whitelist search → recipe-scrapers; follow_ups=[grocery.merge] (demo 2)
│   │   ├── grocery.py             # grocery.merge: LLM dedupe/aisle-group into grocery.md; keyless regex-dedup fallback (demo 2)
│   │   ├── learn.py               # learn.outline + learn.deep_dive: Wikipedia opensearch/list=search/extracts + LLM (demo 3)
│   │   └── code_stub.py           # stretch: 'code' kind via `codex exec` / `claude -p` asyncio subprocess (~15 lines)
│   ├── services/
│   │   ├── llm.py                 # LLMPort: RealLLM (Responses API, hosted web_search Mac-side) | FakeLLM (fixtures/)
│   │   ├── web.py                 # UA-headered fetch, WP wp-json search, DDG-HTML fallback (uddg decode), Wikipedia Action API
│   │   └── artifacts.py           # workspace read/list/mtime + atomic tmp+os.rename writes
│   ├── wake/
│   │   ├── detector.py            # pure detect(chunk_bytes)->bool: Vosk grammar recognizer, doubled-word match, reset-after-trigger
│   │   └── mic.py                 # Mac-only: sounddevice RawInputStream 16k/int16/100ms → queue
│   └── viewer/
│       ├── server.py              # stdlib http.server: GET / (page), /doc?f=, /events (SSE, 250ms mtime poll)
│       └── index.html             # marked.js CDN render, tab per file, auto-focus latest, flash changed sections
├── workspace/                     # runtime artifacts: doc.md, grocery.md, notes.md, .tasks.jsonl (gitignored except .gitkeep)
├── models/                        # vosk-model-small-en-us-0.15/ (downloaded, gitignored)
├── fixtures/                      # FakeLLM outputs, scripted conversation turns per demo, recorded Realtime event JSONL, wake WAVs
├── scripts/
│   ├── fetch_models.sh            # curl + unzip the ~40MB Vosk model
│   ├── demo_check.sh              # headless merge gate: run all 3 scripted demos, assert workspace/*.md + .tasks.jsonl
│   └── demo_cheatsheet.md         # the three 60-second scripts, line by line
└── tests/
    ├── test_orchestrator.py       # dispatch→result→follow_up chaining, ranking, task table, jsonl log
    ├── test_session.py            # FSM transitions, silence timeout (fake clock), end-phrase regex, wake pause/resume
    ├── test_realtime_events.py    # replay recorded server-event JSON via FakeTransport: tool dispatch, gate, truncate math
    ├── test_wake.py               # stream fixture WAV chunks through detector: fires on 'echoecho', silent on decoys
    ├── test_workers_fake.py       # ECHOECHO_FAKE_LLM worker paths end-to-end to files; atomic-write behavior
    ├── test_workers_live.py       # network-marked: live WP search, recipe-scrapers, Wikipedia (all sandbox-verified reachable)
    └── test_viewer.py             # GET /, /doc, SSE reload event within 500ms of a file touch
```

## Stacked PRs

1. **`echoecho/01-core-skeleton` — Skeleton, typed bus, orchestrator core, session FSM, scripted text agent.** Lands: repo scaffold, exact pins, `bus.py` dataclasses, `orchestrator/core.py` (inbox, task table, registry, follow_up chaining, injection policy), `ranker.py`, `log.py`, `conversation/port.py` + `session.py` FSM + `scripted.py` keyless agent + bare text REPL, and a trivial sleep-then-echo worker — proving the whole 3-layer shape and lifecycle with zero network/keys/audio. Verified headlessly: `pytest tests/test_orchestrator.py tests/test_session.py`; `ECHOECHO_TEXT=1 ECHOECHO_FAKE_LLM=1 python3 echoecho.py --script fixtures/smoke.txt` asserts the printed transcript: "echoecho" → dispatch → queued ack → delayed result injected at turn boundary → "that's it" / config-shortened silence timeout → IDLE.

2. **`echoecho/02-workers-artifacts` — The five real workers + services with fixture mode.** Lands: `services/artifacts.py` (atomic writes), `services/llm.py` (RealLLM/FakeLLM), `services/web.py` (WP REST search on the verified whitelist, DDG-HTML fallback, Wikipedia Action API), all five workers registered by kind, recipe→grocery chaining via generic `follow_ups` only. Verified headlessly: `pytest tests/test_workers_fake.py` fully offline (fixtures, atomic writes, regex-dedup fallback); `pytest -m network tests/test_workers_live.py` hits the live keyless endpoints (all previously confirmed reachable from this sandbox); `cat workspace/*.md` shows real artifacts.

3. **`echoecho/03-viewer-textmode` — SSE live viewer + real keyed text mode.** Lands: `viewer/server.py` + `index.html` (marked.js tabs, mtime-poll SSE, auto-focus latest file), `textmode.py` upgraded to a real Responses-API tool loop implementing the full 4-tool Contract A, ambient doc-snapshot injection after `artifacts_touched`, injection prompt wording. Verified headlessly: `pytest tests/test_viewer.py` (SSE reload within 500 ms of touching doc.md; no partial file ever readable); all PR1–2 tests still green under `ECHOECHO_FAKE_LLM=1`; scripted demo-2 transcript piped through the REPL reproduces search → chained merge → injected say-line end-to-end; keyed smoke run documented for the first Mac session.

4. **`echoecho/04-realtime-transport` — Realtime voice agent, headlessly tested via event replay.** Lands: `conversation/realtime.py` raw-WS client (session.update with instructions/4 tools/semantic_vad + interrupt_response/input transcription; instant `function_call_output` for dispatch_task; turn-boundary injection gate; `conversation.item.truncate` barge-in math; end_session tool + transcript regex; silence-timer wiring to speech events; 55-min summary-and-reconnect stub) behind a transport port, plus a FakeTransport that replays recorded server-event JSONL and records every client send. Verified headlessly (no key): `pytest tests/test_realtime_events.py` runs full scripted sessions — including adversarial orderings (result arriving mid-user-speech, mid-response) — asserting every client event name/payload shape against the GA docs before the Mac ever connects.

5. **`echoecho/05-wake-audio-mac` — Vosk wake word + Mac audio I/O (voice complete).** Lands: `wake/detector.py` (grammar `'["echo","[unk]"]'`, partials, doubled-word trigger, recognizer reset, suspend-during-ACTIVE), `wake/mic.py`, `conversation/audio.py` (24 kHz pcm16 capture → base64 append; playback from output_audio.delta with PlaybackTracker; chimes), `scripts/fetch_models.sh`, `echoecho.py --mic-check` (device list + live RMS meter) and a spacebar manual-wake override. Verified headlessly: Vosk runs natively in the sandbox, so `pytest tests/test_wake.py` feeds fixture WAVs through the exact Mac detection code (fires on "echoecho", silent on decoys); PlaybackTracker truncation math unit-tested with a fake stream; only device-open lines are Mac-only (import-guarded, skipped in Linux CI).

6. **`echoecho/06-demo-polish` — Daemon hardening, runbook, demo gate.** Lands: wake/end chimes wired, viewer section-diff CSS flash, "[since last session]" injection of tasks completed while IDLE, WS reconnect/backoff so the daemon never dies, `code_stub.py` stretch worker, prompt tuning (short utterances, verbal acks), `scripts/demo_cheatsheet.md`, README Mac runbook (uv venv, `python3 -m sounddevice` check, `sudo tccutil reset Microphone com.apple.Terminal`, headphones rule, model/cost flags), `demo_check.sh` promoted to the merge gate. Verified headlessly: `demo_check.sh` runs all three scripted demos (scripted agent + FakeLLM + live keyless network) asserting final `workspace/*.md` contents and the `.tasks.jsonl` event sequence; a chaos test kills the fake transport mid-session and asserts clean return to IDLE with wake re-armed.

## Demo scripts

**Demo 1 — live document co-writing (60s).** 0:00 "echoecho" → chime, browser shows empty doc.md tab. 0:03 "Let's write a one-page proposal for a team offsite in Lisbon." — "Nice, starting the doc"; ~3s later title + skeleton sections render. 0:15 "Add three goals: team bonding, planning next year, and shipping the demo." — instant ack; Goals section appears and flashes. 0:30 "Make it more fun, and add a two-day agenda." — doc rewrites, agenda appears while echoecho says "Done — gave it some energy." 0:45 "Read me just the goals." — echoecho reads them (ambient doc-snapshot injection keeps it doc-aware). 0:55 "That's it." → end chime, doc stays on screen. *Exercises*: wake, doc.edit ×3, full-rewrite + SSE reload, doc-context injection, end phrase.

**Demo 2 — grocery list + recipe search (60s).** 0:00 "echoecho… help me plan dinners this week. I'm thinking pad thai one night." — "On it, searching" (recipe.search fires; grocery.md tab opens). 0:15 result injected: "Found a 30-minute chicken pad thai on RecipeTin Eats — added 9 items to the list"; screen shows "## Meals" + items grouped by aisle. 0:20 "And something vegetarian, maybe with halloumi." 0:35 "There's a 25-minute halloumi salad on Pinch of Yum — 7 new items; you already had garlic and lime." — dupes not re-added. 0:45 "Actually drop the fish sauce, and add coffee beans." — direct grocery.merge edit, ~2s. 0:55 "That's it." *Exercises*: follow_ups chaining (search→merge), dedup, say-summaries with counts, direct edits.

**Demo 3 — learning a topic (60s).** 0:00 "echoecho. Teach me about fermentation in food." — tutor starts from its own knowledge immediately while learn.outline fires. 0:10 notes.md tab appears with a 5-section outline + Wikipedia sources; injected event → "I've put an outline in your notes — start with how microbes make acid, or jump to sourdough?" 0:20 "Sourdough." — learn.deep_dive fires; tutor keeps teaching; ~8s later the section fills with bullets, an analogy, and a quiz question. 0:40 Tutor asks the quiz question from the notes; user answers; tutor points at the next unfilled section. 0:55 "That's it." — notes.md survives as a study artifact. *Exercises*: talk-while-working, ambient injection steering, notes growing live.

## Risks & quick-dirty mitigations

1. **Realtime integration is untestable where we develop (no key/audio in sandbox) — riskiest code ships least-tested.** Mitigate: PR 4's FakeTransport replay tests assert every client event against documented GA shapes; keep the client ~150 lines behind a port; validate the whole tool loop keyed-but-audio-free via `--text` on the Mac first; budget one on-Mac integration session on `-mini`; text mode is the demo-day fallback if voice melts down.
2. **Acoustic echo: echoecho's speaker output re-enters the mic** (raw WebSocket has no AEC), causing phantom barge-ins. Mitigate: hard demo-day rule — AirPods/headphones; also suspend the wake listener during ACTIVE and ignore `speech_started` <300 ms after playback begins.
3. **Barge-in truncation bookkeeping is the fiddliest voice code** (wrong `audio_end_ms` = model "remembers" unheard words). Mitigate: PlaybackTracker isolated and unit-tested with a fake stream; failure degrades to slightly stale context, not a crash; prompt the model to keep utterances short so barge-in rarely triggers.
4. **Vosk grammar spotting false accepts/misses** (no trained model). Mitigate: require the doubled phrase in one partial, reset recognizer after each trigger, pause the feed during ACTIVE; spacebar manual-wake as demo insurance; drop-in fallback is a trained livekit-wakeword "echoecho" ONNX behind the same `detect(chunk)->bool` seam.
5. **Injection timing races** (response.create mid-user-speech; two responses writing to the conversation). Mitigate: ONE gate owns all response.create calls, keyed on response.done + no open speech_started; PR 4 replay tests cover adversarial orderings; worst case an interrupt degrades to ambient — annoying, not broken.
6. **Python 3.9 / dual-interpreter drift.** Mitigate: exact pins verified by pip resolution today, 3.9-syntax rule, all tests run on 3.9, plain uv venv on the Mac (conda PortAudio shadowing gotcha); Node port documented as escape hatch, not built.
7. **External data rot** (allrecipes/seriouseats already 403 datacenter IPs; endpoints change). Mitigate: whitelist only the three sandbox-verified sites; DDG-HTML fallback coded; hardcoded dish→URL map for the exact demo dishes as last resort; live tests marked separately so rot flags without blocking merges; rehearse the demo dishes the day before.
8. **macOS mic permission (TCC) silently returning zeros.** Mitigate: `echoecho.py --mic-check` as step zero of setup; README documents `sudo tccutil reset Microphone com.apple.Terminal`.
9. **Cost / 60-min session cap.** Mitigate: sessions exist only between wake and end (idle = ~2% CPU, $0 API); dev on `gpt-realtime-2.1-mini` (~3x cheaper audio); 55-min summary-and-reconnect is best-effort and practically unreachable in 60-second demos.
10. **Dead air while workers run (3–15 s).** Mitigate: dispatch_task returns instantly so the model acks and keeps talking; every result carries a speech-ready `say` sentence; PR 6 reserves explicit prompt-tuning time and the learning demo rehearses talk-while-working.

## Out of scope for v0

- Trained wake-word model (livekit-wakeword path documented, not built), speaker ID / multi-user, hotword sensitivity tuning.
- WebRTC/Electron/browser audio transport, echo cancellation beyond "wear headphones", TTS/STT chained fallback pipeline.
- Multi-process orchestration, Redis/celery, persistence beyond workspace files + `.tasks.jsonl`, task retries/timeout policies beyond a single try.
- Diff/patch-based doc editing (full-file rewrite only), two-way editable web UI, multi-document projects.
- Real coding worker beyond the ~15-line `codex exec` stub; general web research beyond the hosted `web_search` tool; any non-whitelisted recipe sites.
- Long-term memory across days, calendars/reminders/home-automation integrations, auth, packaging/installer, production error handling, non-English support.
