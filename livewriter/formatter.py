"""The live formatter: speech in, small edit ops out, streamed.

One Formatter per session. Utterances queue up; a single worker batches
whatever is queued into one LLM call so ops never interleave out of order.
The model's output is JSONL — each completed line is validated against the
authoritative Doc and forwarded to the page the moment it parses, so the
first written characters land ~TTFT after the utterance closes (measured
warm TTFT: gpt-4.1-mini ≈ 0.43 s, gpt-4o-mini ≈ 0.35 s, gpt-5.4-mini
(effort none) ≈ 0.57 s — inside the mockup's "realistic" preset band).

Generations implement "stop": halt() bumps the generation; in-flight output
belonging to an older generation is discarded mid-stream and queued
utterances are dropped. openai is imported lazily (sandbox convention).
"""

import asyncio
import os
import re
import time

from . import doc as docmod

DEFAULT_MODEL = "gpt-5.4-mini"

SYSTEM = """You are a live writing engine inside a dictation app. A person talks out loud; you maintain a clean, well-formatted document that updates WHILE they speak. You receive the current document (lines tagged [id|kind]) plus their newest speech, and you reply ONLY with edit operations — one JSON object per line (JSONL). No prose, no fences.

OPERATIONS
{"op":"new","kind":"h1|h2|h3|p|li|quote|code|small","md":"text"}   new line at document end
{"op":"new","kind":"li","md":"text","after":7}                     insert after line 7 (extend an earlier list)
{"op":"append","line":7,"md":" and more text"}                     append to line 7 (include the leading space!)
{"op":"replace","line":7,"find":"plain text","md":"replacement"}   edit inside line 7; find = smallest unique PLAIN-text snippet of that line (no markdown)
{"op":"delete","line":7}                                           remove line 7
{"op":"chip","text":"note"}                                        tiny chip shown to the speaker when you did something non-obvious (dropped a phrase, converted, restructured, corrected)

RULES
- STRICT FIDELITY: write only what the speaker said (cleaned and formatted). NEVER invent sentences, list items, details, or title words beyond their speech. When the new speech is an incomplete fragment ("Two,"), write nothing yet — the rest is coming in the next message; a fragment is never license to complete the thought yourself.
- Write promptly and incrementally; append small pieces rather than waiting for complete thoughts. NEVER re-emit text already in the document — when extending a list or paragraph, add only the new part.
- PUNCTUATION SEAMS: speech often arrives split mid-sentence, so the last line may end with a stray "." while the new speech continues that same sentence (", wedged beneath the door"). Fix the seam: replace the stray terminator so the sentence reads as one, then continue.
- Drop fillers (um, uh, okay so, you know) silently.
- Numbers, dates, prices, units spoken in words -> figures ("about forty milliseconds" -> "~40 ms", "twelve hundred dollars" -> "$1,200"). Bold truly key figures/names with **x**, sparingly.
- An opening like "quick update on X" / "notes on Y" / a spoken title announces the DOCUMENT TITLE in informational registers (notes, updates, plans, specs): write a short h1/h2 heading (title-cased, not a sentence), never a paragraph. In prose/poem registers, opening meta ("here's the opening of the story") is dropped entirely — no heading unless one is dictated.
- Enumerations -> li lines (one item per li, no duplicated fragments). Clear topic shift -> new p or h3. Match the intended register: meeting notes, formal email (salutation line, body p's, sign-off), recipe (ingredients list, steps), poem (one p per verse line), code (kind "code"). LITERARY PROSE is near-verbatim: plain p's split only where commanded or at clear scene/topic breaks, numbers stay as words, NO bolding or emphasis, no lists or headings.
- SELF-CORRECTIONS: "no wait, X" / "actually Y" / "I mean Z" -> fix what is already written via replace; never type the correction words.
- SPOKEN COMMANDS are instructions, never content: "scratch that" (delete what you just wrote — including any lead-in left dangling, e.g. an orphaned "One more thing."), "new paragraph", "make that a list" (restructure your last text into li lines), "change X to Y", "heading ...", "quote ...", "bold X", "just say X" / "make it say X" (write X, nothing else). Meta asides ("hang on", "let's draft an email to Z") set intent — never appear in the document.
- Writing NEW content never destroys OLD content: delete/emptying-replace are reserved for explicit corrections and scratch commands. New speech goes on new lines or appends.
- Headings hold ONLY the title. Body sentences never live on an h1/h2/h3 line — start a new p. Emails and letters get NO heading unless one is dictated — start at the salutation.
- A transition lead-in ("One more thing", "Also", "Next up", "Okay so") announces a NEW thought: put what follows on a new line; never append it to a line about something else.
- "scratch that" removes EXACTLY the LAST WRITTEN addition (unless the speaker names something else) — never more. If it shares a line with older content, surgically remove it with replace; never delete a line that still holds content the speaker wants. If the thought being scratched was interrupted by a stop and never written (marked "(stopped)", nothing landed), there is NOTHING to remove — emit only a chip.
- "new paragraph" is always honored: the next content starts a fresh p, no exceptions.
- Before appending, read the line's current ending: continue grammatically, never duplicate connectives ("and and") or re-open a finished sentence.
- The page must always read as if a good writer typed it: proper capitalization/punctuation, the speaker's voice, light polish.
- Emit ops in reading order; most turns need 1-3 ops. No blank-md ops.

EXAMPLE
DOCUMENT
[0|h2] Trip Planning — Notes
[1|p] Flights are booked for **March 3rd**.
NEW SPEECH (write this now)
"and um, the hotel is about ninety dollars a night. no wait, make that March fourth."
CORRECT OUTPUT
{"op":"append","line":1,"md":" Hotel is about **$90**/night."}
{"op":"replace","line":1,"find":"March 3rd","md":"**March 4th**"}
{"op":"chip","text":"corrected: March 3rd → March 4th"}

EXAMPLE
NEW SPEECH (write this now)
"we need three things. visas, travel insurance, and a rental car."
CORRECT OUTPUT
{"op":"new","kind":"p","md":"We need three things:"}
{"op":"new","kind":"li","md":"Visas"}
{"op":"new","kind":"li","md":"Travel insurance"}
{"op":"new","kind":"li","md":"Rental car"}

EXAMPLE (surgical scratch — the stop interrupted a thought, the rest of the line stays)
DOCUMENT
[4|p] The docs task goes to **Diana**. One more thing.
RECENT SPEECH (newest last; already handled — for context only)
"It goes to Diana" / "One more thing" / "(stopped) we should probably tell the vendor that their firmware is"
LAST WRITTEN (your most recent addition to the document)
" One more thing."
NEW SPEECH (write this now)
"Scratch that last part."
CORRECT OUTPUT
{"op":"replace","line":4,"find":" One more thing.","md":""}
{"op":"chip","text":"scratched the interrupted thought"}"""

PROMPT = """DOCUMENT
%s

RECENT SPEECH (newest last; already handled — for context only)
%s

LAST WRITTEN (your most recent addition to the document)
%s

NEW SPEECH (write this now)
%s"""


class Formatter(object):
    def __init__(self, document, send_op, on_think=None, model=None, api_key=None, log=None,
                 on_batch_done=None):
        self.doc = document
        self.send_op = send_op          # async fn(norm_op, gen, utt_id)
        self.on_think = on_think or (lambda on, queued: None)
        self.on_batch_done = on_batch_done  # async fn(last_utt_id, gen)
        self.model = model or os.environ.get("LIVEWRITER_MODEL", DEFAULT_MODEL)
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self.log = log or (lambda **kw: None)
        self.gen = 0
        self.history = []               # (utt_id, text) already handled
        self.last_written = "(nothing yet)"
        self._batch_added = []
        self._destructive_n = 0
        self._queue = []                # (utt_id, text, t_heard)
        self._wake = asyncio.Event()
        self._task = None
        self._client = None
        self._effort = os.environ.get("LIVEWRITER_EFFORT", "")  # '' = auto
        self.calls = 0
        self.dropped_ops = 0

    def start(self):
        self._task = asyncio.get_event_loop().create_task(self._worker())
        return self._task

    async def close(self):
        if self._task is not None:
            self._task.cancel()

    def submit(self, utt_id, text, t_heard):
        self._queue.append((utt_id, text, t_heard))
        self._wake.set()

    def halt(self):
        """The stop word / stop button: discard queued + in-flight work."""
        self.gen += 1
        self._queue = []
        return self.gen

    # -- internals ----------------------------------------------------------
    async def _worker(self):
        while True:
            await self._wake.wait()
            self._wake.clear()
            while self._queue:
                gen = self.gen
                batch = self._queue[:]
                self._queue = []
                self.on_think(True, 0)
                try:
                    await self._run_batch(batch, gen)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    self.log(type="fmt_error", error=str(e)[:300])
                    if self.gen == gen:
                        try:
                            await self.send_op({"op": "chip", "text": "formatter hiccup — retrying"}, gen, batch[-1][0])
                        except Exception:
                            pass
                finally:
                    self.on_think(False, len(self._queue))
                if self.gen == gen:
                    self.history.extend((u, t) for u, t, _ in batch)
                    del self.history[:-12]
                    if self._batch_added:
                        self.last_written = " / ".join(self._batch_added)[-300:]
                    if self.on_batch_done is not None:
                        try:
                            await self.on_batch_done(batch[-1][0], gen)
                        except Exception:
                            pass

    # words that license destructive ops — without one of these in the new
    # speech, a delete/emptying-replace is the model erasing content on a
    # whim (observed: "One more thing" triggering a delete of the list item
    # it was appended to), and gets dropped mechanically.
    _DESTRUCTIVE_OK = re.compile(
        r"scratch|delete|remove|undo|strike|change|replace|instead|rather|"
        r"actually|correction|wait|no[,.]? not|not |i mean|make (that|it)|forget",
        re.IGNORECASE)

    def _op_allowed(self, op, speech):
        name = op.get("op")
        destructive = name == "delete" or (name == "replace" and not str(op.get("md", "")).strip())
        if not destructive:
            return True
        return bool(self._DESTRUCTIVE_OK.search(speech))

    async def _run_batch(self, batch, gen):
        new_speech = " ".join(t for _, t, _ in batch)
        self._speech = new_speech
        self._batch_added = []
        self._destructive_n = 0
        prompt = PROMPT % (
            self.doc.render_for_prompt(),
            "\n".join('"%s"' % t for _, t in self.history[-8:]) or "(none)",
            '"%s"' % self.last_written,
            '"%s"' % new_speech,
        )
        utt_id = batch[-1][0]
        t0 = time.monotonic()
        self.calls += 1
        self.log(type="fmt_call", utt=utt_id, batch=len(batch), text=new_speech[:400])
        stream = await self._create_stream(prompt)
        buf = ""
        first_op_t = None
        n_ops = 0
        async for ev in stream:
            if self.gen != gen:
                try:
                    await stream.close()
                except Exception:
                    pass
                self.log(type="fmt_aborted", utt=utt_id)
                return
            if ev.type != "response.output_text.delta":
                continue
            buf += ev.delta
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                n_ops += await self._handle_line(line, gen, utt_id)
                if first_op_t is None and n_ops:
                    first_op_t = time.monotonic()
        if buf.strip():
            n_ops += await self._handle_line(buf, gen, utt_id)
            if first_op_t is None and n_ops:
                first_op_t = time.monotonic()
        self.log(type="fmt_done", utt=utt_id, ops=n_ops,
                 ttfop_ms=int(((first_op_t or time.monotonic()) - t0) * 1000),
                 total_ms=int((time.monotonic() - t0) * 1000))

    async def _handle_line(self, raw, gen, utt_id):
        op = docmod.parse_op_line(raw)
        if op is None:
            if raw.strip():
                self.dropped_ops += 1
                self.log(type="fmt_bad_line", line=raw.strip()[:200])
            return 0
        speech = getattr(self, "_speech", "")
        name = op.get("op")
        destructive = name == "delete" or (name == "replace" and not str(op.get("md", "")).strip())
        if destructive:
            if not self._op_allowed(op, speech):
                self.dropped_ops += 1
                self.log(type="fmt_op_blocked", reason="destructive op without a correction command",
                         op=str(op)[:150])
                return 0
            # one scratch = one addition removed; over-deletion was observed
            # ("scratch that last part" deleting a whole list) — cap it unless
            # the speaker asked for a bulk removal
            bulk = re.search(r"\b(all|everything|whole|both|entire|list|section)\b", speech, re.IGNORECASE)
            self._destructive_n += 1
            if self._destructive_n > (6 if bulk else 2):
                self.dropped_ops += 1
                self.log(type="fmt_op_blocked", reason="destructive op cap", op=str(op)[:150])
                return 0
        try:
            norm = self.doc.apply(op)
        except docmod.OpError as e:
            self.dropped_ops += 1
            self.log(type="fmt_op_dropped", reason=str(e)[:200])
            return 0
        if name in ("new", "append") or (name == "replace" and str(op.get("md", "")).strip()):
            added = docmod.plain(docmod.parse_md(str(op.get("md", ""))))
            if added.strip():
                self._batch_added.append(added.strip())
        await self.send_op(norm, gen, utt_id)
        return 1

    async def _create_stream(self, prompt):
        if self._client is None:
            from openai import AsyncOpenAI
            self._client = AsyncOpenAI(api_key=self.api_key)
        kwargs = dict(model=self.model, instructions=SYSTEM, input=prompt,
                      stream=True, max_output_tokens=1200)
        efforts = [self._effort] if self._effort else (
            ["none", "minimal", "low"] if self.model.startswith("gpt-5") else [""])
        last_err = None
        for eff in efforts:
            if eff:
                kwargs["reasoning"] = {"effort": eff}
            else:
                kwargs.pop("reasoning", None)
            try:
                stream = await self._client.responses.create(**kwargs)
                if eff and not self._effort:
                    self._effort = eff  # remember what this model accepts
                return stream
            except Exception as e:
                last_err = e
                if "not supported" not in str(e) and "Unsupported" not in str(e):
                    raise
        raise last_err


REVIEW_SYSTEM = """You are the copy editor working behind a live dictation writer. A person spoke; a fast writer already produced the document. Compare the FULL TRANSCRIPT against the DOCUMENT and emit corrective edit operations (same JSONL op format) ONLY where the document is unfaithful or malformed:
- SUBSTANTIVE spoken content that is missing: facts, names, numbers, tasks, requests, ideas (unless the speaker retracted it, or it is marked (stopped) — never reinstate stopped/scratched material)
- self-corrections that were not applied
- quantities/numbers/dates dropped or left in words
- structure the speaker asked for (lists, headings, new paragraphs) not honored
- duplicated fragments, punctuation seams, filler words that leaked in, command words typed as text
The writer DELIBERATELY drops fillers (um, okay so), meta announcements ("here's the opening", "let's draft an email", "quick update on X" when it became the title), transition lead-ins, and spoken commands — their absence is correct, never an omission. Keep every edit minimal and surgical — never rewrite lines for style, never add anything unspoken, never restate a fact that is already on the page in different words. When in doubt, do nothing: a wrong edit is far worse than no edit.

OPERATIONS (one JSON object per line)
{"op":"new","kind":"h1|h2|h3|p|li|quote|code|small","md":"text","after":7}
{"op":"append","line":7,"md":" more"}
{"op":"replace","line":7,"find":"plain text","md":"replacement"}
{"op":"delete","line":7}
{"op":"chip","text":"note"}"""

REVIEW_PROMPT = """DOCUMENT
%s

FULL TRANSCRIPT (in spoken order; (stopped) = interrupted by a stop command, must stay out)
%s

Emit corrective ops now (or nothing)."""


class Reviewer(object):
    """Slow fidelity pass: runs when the pen is idle, diffs transcript vs doc,
    and repairs what the fast writer missed. Discards its own output if new
    speech arrived while it was thinking — the next idle moment retries."""

    def __init__(self, document, send_op, model=None, api_key=None, log=None):
        self.doc = document
        self.send_op = send_op
        self.model = model or os.environ.get("LIVEWRITER_REVIEW_MODEL", DEFAULT_MODEL)
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self.log = log or (lambda **kw: None)
        self._client = None
        self.passes = 0
        self.fixes = 0

    async def run_pass(self, transcript_lines, gen_of, utt_count_of):
        """transcript_lines: list of (text, stopped_bool). gen_of()/utt_count_of()
        report live state so a stale review can drop itself."""
        gen0, count0 = gen_of(), utt_count_of()
        prompt = REVIEW_PROMPT % (
            self.doc.render_for_prompt(),
            "\n".join(('%s "%s"' % ("(stopped)" if st else "-", t)) for t, st in transcript_lines[-60:]),
        )
        if self._client is None:
            from openai import AsyncOpenAI
            self._client = AsyncOpenAI(api_key=self.api_key)
        self.passes += 1
        t0 = time.monotonic()
        kwargs = dict(model=self.model, instructions=REVIEW_SYSTEM, input=prompt,
                      max_output_tokens=2000)
        if self.model.startswith("gpt-5"):
            kwargs["reasoning"] = {"effort": "low"}
        resp = await self._client.responses.create(**kwargs)
        text = (resp.output_text or "").strip()
        if gen_of() != gen0 or utt_count_of() != count0:
            self.log(type="review_stale")
            return 0
        n = 0
        for raw in text.split("\n"):
            op = docmod.parse_op_line(raw)
            if op is None:
                continue
            if op.get("op") == "chip":
                continue  # the editor speaks through one summary chip below
            try:
                norm = self.doc.apply(op)
            except docmod.OpError as e:
                self.log(type="review_op_dropped", reason=str(e)[:200])
                continue
            await self.send_op(norm, gen0, None)
            n += 1
        if n:
            self.fixes += n
            try:
                await self.send_op({"op": "chip", "text": "🪶 editor tidied %d spot%s" % (n, "" if n == 1 else "s")}, gen0, None)
            except Exception:
                pass
        self.log(type="review_done", ops=n, ms=int((time.monotonic() - t0) * 1000))
        return n


class FakeFormatter(object):
    """Deterministic keyless stand-in: one paragraph per utterance, a couple
    of commands, so plumbing tests can assert exact op sequences."""

    def __init__(self, document, send_op, on_think=None, log=None, on_batch_done=None, **_):
        self.doc = document
        self.send_op = send_op
        self.on_think = on_think or (lambda on, queued: None)
        self.log = log or (lambda **kw: None)
        self.on_batch_done = on_batch_done
        self.model = "fake"
        self.gen = 0
        self.history = []
        self._last_line = None
        self.calls = 0
        self.dropped_ops = 0

    def start(self):
        return None

    async def close(self):
        return None

    def halt(self):
        self.gen += 1
        return self.gen

    def submit(self, utt_id, text, t_heard):
        asyncio.get_event_loop().create_task(self._run(utt_id, text))

    async def _run(self, utt_id, text):
        gen = self.gen
        self.calls += 1
        low = text.lower().strip().rstrip(".!?")
        if low.startswith("heading "):
            ops = [{"op": "new", "kind": "h2", "md": text[8:].strip().rstrip(".").title()}]
        elif low in ("scratch that", "delete that"):
            ops = ([{"op": "delete", "line": self._last_line}, {"op": "chip", "text": "scratched"}]
                   if self._last_line is not None else [{"op": "chip", "text": "nothing to scratch"}])
        else:
            ops = [{"op": "new", "kind": "p", "md": text}]
        for op in ops:
            if self.gen != gen:
                return
            try:
                norm = self.doc.apply(op)
            except docmod.OpError:
                self.dropped_ops += 1
                continue
            if norm["op"] == "new":
                self._last_line = norm["id"]
            elif norm["op"] == "delete":
                self._last_line = None
            await self.send_op(norm, gen, utt_id)
        self.history.append((utt_id, text))
        if self.gen == gen and self.on_batch_done is not None:
            try:
                await self.on_batch_done(utt_id, gen)
            except Exception:
                pass
