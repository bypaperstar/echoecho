# echoecho demo cheatsheet — the three 60-second scripts

Say each line at the timestamp. Watch the browser tab (`http://127.0.0.1:8765/`)
— it auto-focuses whatever file is being written and flashes changed sections.
Headless equivalents: `bash scripts/demo_check.sh` (all three, asserted), or
`ECHOECHO_FAKE_LLM=1 python3 echoecho.py --script fixtures/demoN.txt` (one at a time).

Before each demo: headphones ON, viewer tab visible, `workspace/` cleared
(`rm -f workspace/*.md workspace/.tasks.jsonl`).

## Demo 1 — live document co-writing

| t    | You say                                                                | What happens |
|------|------------------------------------------------------------------------|--------------|
| 0:00 | **"echoecho"**                                                        | chime; browser shows empty doc.md tab |
| 0:03 | "Let's write a one-page proposal for a team offsite in Lisbon."        | "Nice, starting the doc" — ~3 s later title + skeleton sections render |
| 0:15 | "Add three goals: team bonding, planning next year, and shipping the demo." | instant ack; Goals section appears and flashes |
| 0:30 | "Make it more fun, and add a two-day agenda."                          | doc rewrites; agenda appears while echoecho says "Done — gave it some energy" |
| 0:45 | "Read me just the goals."                                              | echoecho reads them (ambient doc-snapshot injection keeps it doc-aware) |
| 0:55 | **"That's it."**                                                       | end chime; doc stays on screen |

Exercises: wake, doc.edit ×3, full-rewrite + SSE reload, doc-context injection, end phrase.

## Demo 2 — grocery list + recipe search

| t    | You say                                                                | What happens |
|------|------------------------------------------------------------------------|--------------|
| 0:00 | **"echoecho…** help me plan dinners this week. I'm thinking pad thai one night." | "On it, searching" — recipe.search fires; grocery.md tab opens |
| 0:15 | *(result injected)*                                                     | "Found a 30-minute chicken pad thai on RecipeTin Eats — added 9 items to the list"; "## Meals" + items grouped by aisle |
| 0:20 | "And something vegetarian, maybe with halloumi."                        | second search fires |
| 0:35 | *(result injected)*                                                     | "There's a 25-minute halloumi salad on Pinch of Yum — 7 new items; you already had garlic and lime" — dupes not re-added |
| 0:45 | "Actually drop the fish sauce, and add coffee beans."                   | direct grocery.merge edit, ~2 s |
| 0:55 | **"That's it."**                                                        | end chime |

Exercises: follow_ups chaining (search → merge), dedup, say-summaries with counts, direct edits.

## Demo 3 — learning a topic

| t    | You say                                                                | What happens |
|------|------------------------------------------------------------------------|--------------|
| 0:00 | **"echoecho.** Teach me about fermentation in food."                  | tutor starts from its own knowledge immediately while learn.outline fires |
| 0:10 | *(outline lands)*                                                       | notes.md tab appears: 5-section outline + Wikipedia sources; "I've put an outline in your notes — start with how microbes make acid, or jump to sourdough?" |
| 0:20 | "Sourdough."                                                            | learn.deep_dive fires; tutor keeps teaching; ~8 s later the section fills with bullets, an analogy, a quiz question |
| 0:40 | *(tutor asks the quiz question from the notes)*                         | you answer; tutor points at the next unfilled section |
| 0:55 | **"That's it."**                                                        | notes.md survives as a study artifact |

Exercises: talk-while-working, ambient injection steering, notes growing live.

## If voice melts down on demo day

`OPENAI_API_KEY=... python3 echoecho.py --text` — identical FSM and workers, typed
"echoecho" / "that's it"; the browser tab still updates live.
