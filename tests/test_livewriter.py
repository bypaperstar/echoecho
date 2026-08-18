"""Live Writer keyless tests: doc model + inline markdown, segmenter timing
(fake clock), op-line parsing, and — when websockets is installed — the fake
end-to-end path through the real server. No key, no audio, no network."""

import asyncio
import importlib.util
import json
import sys
import time
import unittest

import pytest

from livewriter import doc as docmod
from livewriter.segmenter import Segmenter, PAUSE_S, STOP_CONFIRM_S

HAS_WS = importlib.util.find_spec("websockets") is not None


# -- inline markdown ----------------------------------------------------------

def test_md_roundtrip_basic():
    atoms = docmod.parse_md("plain **bold** and *ital* and `code` and ~~gone~~")
    assert docmod.plain(atoms) == "plain bold and ital and code and gone"
    assert docmod.atoms_to_md(atoms) == "plain **bold** and *ital* and `code` and ~~gone~~"


def test_md_unbalanced_markers_stay_literal():
    atoms = docmod.parse_md("2 ** 3 is 8")
    assert docmod.plain(atoms) == "2 ** 3 is 8"
    atoms = docmod.parse_md("a * b")
    assert docmod.plain(atoms) == "a * b"


def test_md_nested_styles():
    atoms = docmod.parse_md("**bold *both* bold**")
    assert docmod.plain(atoms) == "bold both bold"
    both = [a for a in atoms if a[1] == "bi"]
    assert docmod.plain(both) == "both"


# -- doc ops ------------------------------------------------------------------

def make_doc():
    d = docmod.Doc()
    d.apply({"op": "new", "kind": "h2", "md": "Title"})
    d.apply({"op": "new", "kind": "p", "md": "We shipped on **Tuesday**."})
    d.apply({"op": "new", "kind": "li", "md": "Tune the noise gate"})
    return d


def test_ops_new_append_replace_delete():
    d = make_doc()
    d.apply({"op": "append", "line": 1, "md": " Latency is **~40 ms**."})
    assert "Latency is **~40 ms**" in d.render_for_prompt()
    norm = d.apply({"op": "replace", "line": 2, "find": "noise gate", "md": "noise **gate**"})
    assert norm["find"] == "noise gate"
    d.apply({"op": "delete", "line": 0})
    assert d.line(0) is None
    md = d.to_markdown()
    assert md.startswith("We shipped")
    assert "- Tune the noise" in md


def test_replace_matches_plain_text_across_styles():
    d = docmod.Doc()
    d.apply({"op": "new", "kind": "p", "md": "goes to **Marcus** today"})
    d.apply({"op": "replace", "line": 0, "find": "Marcus", "md": "**Diana**"})
    assert d.line(0) is not None
    assert docmod.plain(d.line(0).atoms) == "goes to Diana today"


def test_replace_find_with_markdown_markers_still_matches():
    d = docmod.Doc()
    d.apply({"op": "new", "kind": "li", "md": "pH **6.8**, dissolved oxygen **5.2**."})
    # models write find WITH markdown though matching is plain-text
    d.apply({"op": "replace", "line": 0, "find": ", dissolved oxygen **5.2**", "md": ""})
    assert docmod.plain(d.line(0).atoms) == "pH 6.8."


def test_replace_case_insensitive_fallback_and_miss():
    d = docmod.Doc()
    d.apply({"op": "new", "kind": "p", "md": "The Vendor called."})
    d.apply({"op": "replace", "line": 0, "find": "the vendor", "md": "Acme"})
    assert docmod.plain(d.line(0).atoms) == "Acme called."
    with pytest.raises(docmod.OpError):
        d.apply({"op": "replace", "line": 0, "find": "zebra", "md": "x"})


def test_replace_that_empties_a_line_deletes_it():
    d = docmod.Doc()
    d.apply({"op": "new", "kind": "li", "md": "One more thing"})
    norm = d.apply({"op": "replace", "line": 0, "find": "One more thing", "md": ""})
    assert norm.get("empty_delete") is True
    assert d.line(0) is None


def test_formatter_blocks_unlicensed_destructive_ops():
    from livewriter.formatter import Formatter
    f = Formatter.__new__(Formatter)  # only the guard is under test
    assert not f._op_allowed({"op": "delete", "line": 4}, "One more thing")
    assert not f._op_allowed({"op": "replace", "line": 4, "find": "x", "md": ""}, "and here is more")
    assert f._op_allowed({"op": "delete", "line": 4}, "Scratch that last part.")
    assert f._op_allowed({"op": "replace", "line": 4, "find": "Marcus", "md": ""}, "no wait, not Marcus")
    assert f._op_allowed({"op": "replace", "line": 4, "find": "a", "md": "b"}, "anything")
    assert f._op_allowed({"op": "new", "kind": "p", "md": "hi"}, "anything")


def test_new_after_inserts_midway():
    d = make_doc()
    norm = d.apply({"op": "new", "kind": "li", "md": "Retest speaker", "after": 2})
    ids = [l.id for l in d.lines]
    assert ids.index(norm["id"]) == ids.index(2) + 1


def test_bad_ops_raise():
    d = make_doc()
    for bad in [{"op": "append", "line": 99, "md": "x"},
                {"op": "append", "line": 1, "md": ""},
                {"op": "delete", "line": 99},
                {"op": "nope"},
                "not a dict"]:
        with pytest.raises(docmod.OpError):
            d.apply(bad)


def test_list_grouping_in_markdown():
    d = docmod.Doc()
    d.apply({"op": "new", "kind": "li", "md": "one"})
    d.apply({"op": "new", "kind": "li", "md": "two"})
    d.apply({"op": "new", "kind": "p", "md": "tail"})
    assert "- one\n- two\n\ntail" in d.to_markdown()


def test_parse_op_line():
    assert docmod.parse_op_line('{"op":"chip","text":"hi"}') == {"op": "chip", "text": "hi"}
    assert docmod.parse_op_line("```json") is None
    assert docmod.parse_op_line("Sure! Here are the ops:") is None
    assert docmod.parse_op_line("{broken json") is None
    assert docmod.parse_op_line("   ") is None


# -- segmenter ----------------------------------------------------------------

class SegHarness(object):
    def __init__(self):
        self.utts = []
        self.stops = 0
        self.discarded = []
        self.ghosts = []
        self.seg = Segmenter(lambda t, a, b: self.utts.append(t),
                             lambda d: self._stop(d),
                             lambda p, t: self.ghosts.append(p))

    def _stop(self, d):
        self.stops += 1
        self.discarded.append(d)


def test_segmenter_punctuation_boundary():
    h = SegHarness()
    t = 0.0
    for d in ["Hello", " there", " team", "."]:
        h.seg.feed(d, t)
        t += 0.1
    assert h.utts == ["Hello there team."]


def test_segmenter_pause_boundary():
    h = SegHarness()
    h.seg.feed("no punctuation here", 0.0)
    h.seg.tick(PAUSE_S - 0.05)
    assert h.utts == []
    h.seg.tick(PAUSE_S + 0.05)
    assert h.utts == ["no punctuation here"]


def test_segmenter_short_fragment_waits_for_long_pause():
    from livewriter.segmenter import PAUSE_FRAG_S
    h = SegHarness()
    h.seg.feed("Okay.", 0.0)  # < 3 words: no instant emit, and the normal
    assert h.utts == []       # pause is not enough — fragments hold longer
    h.seg.tick(PAUSE_S + 0.1)
    assert h.utts == []
    h.seg.tick(PAUSE_FRAG_S + 0.1)
    assert h.utts == ["Okay."]


def test_segmenter_stop_with_punctuation_fires_instantly_and_discards():
    h = SegHarness()
    for d in ["tell", " the", " vendor", " stop", "."]:
        h.seg.feed(d, 0.0)
    assert h.stops == 1
    assert h.utts == []
    assert h.seg.pending == ""
    assert h.discarded == ["tell the vendor"]  # handed over for context


def test_segmenter_bare_stop_waits_confirm_window():
    h = SegHarness()
    h.seg.feed("we should stop", 0.0)
    assert h.stops == 0
    h.seg.tick(STOP_CONFIRM_S - 0.1)
    assert h.stops == 0
    h.seg.tick(STOP_CONFIRM_S + 0.05)
    assert h.stops == 1


def test_segmenter_stop_mid_sentence_does_not_fire_when_speech_continues():
    h = SegHarness()
    h.seg.feed("we should stop", 0.0)
    h.seg.feed(" shipping bugs", 0.2)  # more words: disarm
    h.seg.tick(0.2 + PAUSE_S + 0.05)
    assert h.stops == 0
    assert h.utts == ["we should stop shipping bugs"]


def test_segmenter_runon_emits_at_max_words():
    h = SegHarness()
    words = " ".join("w%d" % i for i in range(40))
    for w in words.split():
        h.seg.feed(" " + w, 0.0)
    assert len(h.utts) >= 1


# -- fake e2e through the real server ------------------------------------------

@pytest.mark.skipif(not HAS_WS, reason="websockets not installed")
def test_fake_server_end_to_end(tmp_path):
    import socket
    import websockets
    from livewriter import server

    with socket.socket() as s:  # a free port — parallel playtests own 89xx
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    async def scenario():
        task = asyncio.get_event_loop().create_task(
            server.serve(port=port, fake=True, log_dir=str(tmp_path)))
        await asyncio.sleep(0.4)
        ws = await websockets.connect("ws://127.0.0.1:%d/ws" % port)
        await ws.send(json.dumps({"type": "hello"}))
        events = []

        async def drain(timeout):
            try:
                while True:
                    events.append(json.loads(await asyncio.wait_for(ws.recv(), timeout)))
            except asyncio.TimeoutError:
                pass

        await ws.send(json.dumps({"type": "text_input", "text": "heading Field Notes"}))
        await ws.send(json.dumps({"type": "text_input", "text": "The first entry."}))
        await drain(1.0)
        # voice-style deltas incl. an instant stop
        for w in ["Some", " words", " stop", "."]:
            await ws.send(json.dumps({"type": "sim_delta", "text": w}))
        await drain(1.0)
        await ws.close()
        task.cancel()
        return events

    events = asyncio.run(scenario())
    types = [e["type"] for e in events]
    assert "ready" in types and "op" in types and "wrote" in types and "halted" in types
    md_ops = [e for e in events if e["type"] == "op"]
    assert any(e["op"]["kind"] == "h2" for e in md_ops if e["op"]["op"] == "new")
    # the stopped fragment never produced ops
    assert not any("Some words" in json.dumps(e) for e in md_ops)
