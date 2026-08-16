#!/usr/bin/env python3
"""Playtest harness: real-model conversations, fully recorded, then judged.

The scripted demos (--script) replay canned tool calls, so they can never
catch the model *believing* it can't do something. Playtests do: each
scenario in fixtures/playtests/*.json is a real spoken-style conversation
played turn by turn against a REAL model with the production system prompt,
tools, orchestrator, and workers (agent.run shells out to the real agent
CLI). Everything is recorded (events.jsonl + transcript.md via recorder),
the workspace is snapshotted per scenario, programmatic file checks run,
and an LLM judge grades the session against the scenario's success
criteria and red flags.

Two ports:
  --port text           TextRepl + Responses API (ECHOECHO_TEXT_MODEL,
                        default gpt-4o-mini). Fast, cheap.
  --port realtime-text  The ACTUAL voice model (gpt-realtime-2.1-mini by
                        default) over the real Realtime WebSocket, driven
                        with text items instead of audio — same
                        voice_prompt(), same tools, same tool handler. The
                        highest-fidelity headless reproduction of a live
                        voice session.

Usage:
  python3 scripts/playtest.py --port text                 # all scenarios
  python3 scripts/playtest.py --port realtime-text --only live_doc_cowrite
  python3 scripts/playtest.py --port text --no-judge      # skip LLM judge

Results land in playtest-results/<stamp>_<port>/<scenario>/:
  console.log     everything the session printed
  recording/      the session recording (events.jsonl, transcript.md, meta)
  workspace/      what the workers actually produced
  result.json     end reason, programmatic check results
  review.md       judge verdict (unless --no-judge)
plus a top-level report.md summary table.
"""
import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

SCENARIOS_DIR = REPO_ROOT / "fixtures" / "playtests"
RESULTS_ROOT = REPO_ROOT / "playtest-results"
SCENARIO_TIMEOUT = 900  # hard cap per scenario, seconds
RESPONSE_TIMEOUT = 120  # realtime: max wait for one response.done


# ---------------------------------------------------------------- realtime-text port

class RealtimeTextPort:
    """Drives the real Realtime model with text turns: the exact production
    session.update (voice prompt + 4 tools) with output_modalities ["text"]
    and VAD off, user turns as input_text items, function calls handled the
    same way RealtimeClient handles them. Injections surface at turn
    boundaries; interrupt-priority injections trigger an immediate response,
    ambient ones ride along on the next turn — matching the voice client."""

    def __init__(self, turns, session, model, out=print):
        self.turns = list(turns)
        self.session = session
        self.model = model
        self.out = out
        self.transport = None
        self._tool_cb = None

    # ConversationPort surface used by make_tool_handler / orchestrator
    def on_tool(self, cb):
        self._tool_cb = cb

    def inject(self, injection):
        self.session.queue_injection(injection)

    async def end(self):
        self.session.begin_ending("forced")

    async def run(self):
        from echoecho_app import events
        from echoecho_app.conversation.realtime import (
            WebSocketTransport, build_session_update, _system_item)
        from echoecho_app.conversation.session import ACTIVE, ENDING
        self._ACTIVE, self._ENDING = ACTIVE, ENDING
        self._system_item = _system_item
        self._events = events

        self.transport = WebSocketTransport(self.model)
        await self.transport.connect()
        update = build_session_update()
        update["session"]["output_modalities"] = ["text"]
        # no audio is ever appended; disable VAD so turns are ours to drive
        update["session"]["audio"]["input"]["turn_detection"] = None
        await self.transport.send(update)
        events.emit("session", event="connected", model=self.model)
        self.session.wake()

        for raw in self.turns:
            if self.session.state != ACTIVE:
                break
            raw = raw.strip()
            if not raw or raw.startswith("#"):
                continue
            if raw.startswith("~wait"):
                parts = raw.split()
                secs = float(parts[1]) if len(parts) > 1 else 0.5
                await self._wait(secs)
                continue
            if raw.lower() == "echoecho":
                continue  # voice sessions are already awake once connected
            await self._user_turn(raw)

        if self.session.state == ACTIVE:
            self.session.begin_ending("script_end")
        if self.session.state == ENDING:
            self.session.finish()
        events.emit("session", event="closed",
                    detail=self.session.end_reason or "")
        try:
            await self.transport.close()
        except Exception:
            pass

    async def _user_turn(self, text):
        self.out("[user] %s" % text)
        self._events.emit("user_text", text=text)
        s = self.session
        s.note_user_speech_started()
        s.note_user_speech_stopped()
        s.handle_transcript(text)  # end-phrase belt-and-suspenders
        await self.transport.send({
            "type": "conversation.item.create",
            "item": {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": text}]}})
        await self._drain()  # pending task results ride along with the turn
        await self._response_round()

    async def _response_round(self):
        """response.create, then read to a response.done with no function
        calls — tool rounds ack immediately and re-create, like the client."""
        await self.transport.send({"type": "response.create"})
        self.session.note_assistant_response_started()
        while True:
            event = await asyncio.wait_for(self.transport.recv(),
                                           RESPONSE_TIMEOUT)
            t = event.get("type", "")
            if t == "error":
                self.out("[realtime] server error: %s"
                         % json.dumps(event.get("error", {})))
                continue
            if t != "response.done":
                continue
            self.session.note_assistant_response_done()
            resp = event.get("response", {})
            if resp.get("status") not in (None, "completed"):
                self.out("[realtime] response status=%s details=%s"
                         % (resp.get("status"),
                            json.dumps(resp.get("status_details") or {})))
            calls = []
            for item in resp.get("output", []):
                if item.get("type") == "message":
                    text = "".join(c.get("text", "")
                                   for c in item.get("content", [])
                                   if c.get("type") in ("output_text", "text"))
                    if text.strip():
                        self.out("[echoecho] %s" % text)
                        self._events.emit("assistant_text", text=text)
                elif item.get("type") == "function_call":
                    calls.append(item)
            if not calls:
                return
            for call in calls:
                raw = call.get("arguments") or "{}"
                try:
                    args = json.loads(raw) if raw.strip() else {}
                except ValueError:
                    args = {}
                self.out("[tool] %s %s" % (call.get("name"), json.dumps(args)))
                self._events.emit("tool_call", name=call.get("name"), args=args)
                try:
                    result = (self._tool_cb(call["name"], args)
                              if self._tool_cb else {})
                except Exception as exc:
                    result = {"error": str(exc)}
                self.out("[tool] -> %s" % json.dumps(result))
                await self.transport.send({
                    "type": "conversation.item.create",
                    "item": {"type": "function_call_output",
                             "call_id": call.get("call_id"),
                             "output": json.dumps(result)}})
                if call.get("name") == "end_session":
                    return
            if self.session.state != self._ACTIVE:
                return
            await self.transport.send({"type": "response.create"})
            self.session.note_assistant_response_started()

    async def _wait(self, secs):
        deadline = time.monotonic() + secs
        while time.monotonic() < deadline:
            await asyncio.sleep(1.0)
            await self._drain()

    async def _drain(self):
        """Injection gate, faithful to RealtimeClient._tick: non-silent
        injections become system items; interrupt priority speaks now."""
        interrupt = False
        for inj in self.session.drain_injections():
            if inj.priority == "silent":
                continue
            text = inj.text
            if text.startswith("[task"):
                text += " Weave in naturally."
            self.out("[inject/%s] %s" % (inj.priority, inj.text))
            await self.transport.send(self._system_item(text))
            self._events.emit("injection", text=inj.text, priority=inj.priority)
            interrupt = interrupt or inj.priority == "interrupt"
        if interrupt:
            await self._response_round()


# ---------------------------------------------------------------- one scenario (child)

def wipe_workspace(ws):
    for p in ws.iterdir():
        if p.name == ".gitkeep":
            continue
        shutil.rmtree(p) if p.is_dir() else p.unlink()


async def run_scenario(scenario, port_name, outdir):
    from echoecho_app import config, events, recorder
    from echoecho_app.conversation.session import Session
    from echoecho_app.orchestrator.core import Orchestrator
    from echoecho_app.workers.base import load_all
    from echoecho import make_tool_handler

    config.WORKSPACE_DIR.mkdir(exist_ok=True)
    wipe_workspace(config.WORKSPACE_DIR)
    mode = "playtest-%s" % port_name
    events.reset(mode)
    rec = recorder.start(mode)

    session = Session()
    if port_name == "realtime-text":
        model = (os.environ.get("ECHOECHO_REALTIME_MODEL")
                 or "gpt-realtime-2.1-mini")
        port = RealtimeTextPort(scenario["turns"], session, model)
    else:
        from echoecho_app.conversation.textmode import TextRepl
        turns = list(scenario["turns"])

        def input_fn(prompt=""):
            if not turns:
                raise EOFError
            line = turns.pop(0)
            print("%s%s" % (prompt, line))
            return line

        port = TextRepl(session=session, input_fn=input_fn)

    orch = Orchestrator(registry=load_all(), on_injection=port.inject,
                        fake_llm=config.echoecho_fake_llm())
    orch.rehydrate()
    port.on_tool(make_tool_handler(orch, port))
    orch_loop = asyncio.ensure_future(orch.run())
    started = time.monotonic()
    error = None
    try:
        await asyncio.wait_for(port.run(), SCENARIO_TIMEOUT)
        await orch.drain(timeout=10.0)
    except asyncio.TimeoutError:
        error = "scenario timeout after %ds" % SCENARIO_TIMEOUT
    except Exception as exc:
        error = "%s: %s" % (type(exc).__name__, exc)
    finally:
        orch_loop.cancel()
        recorder.stop(end_reason=session.end_reason or error or "script_end")

    # snapshot everything reviewable
    outdir.mkdir(parents=True, exist_ok=True)
    if rec is not None:
        shutil.copytree(rec.dir, outdir / "recording", dirs_exist_ok=True)
    ws_snap = outdir / "workspace"
    shutil.copytree(config.WORKSPACE_DIR, ws_snap, dirs_exist_ok=True)

    checks = run_checks(scenario, ws_snap)
    result = {"scenario": scenario["name"], "port": port_name,
              "duration_s": round(time.monotonic() - started, 1),
              "end_reason": session.end_reason, "error": error,
              "checks": checks,
              "checks_passed": sum(1 for c in checks if c["pass"]),
              "checks_total": len(checks)}
    (outdir / "result.json").write_text(json.dumps(result, indent=2),
                                        encoding="utf-8")
    print("[playtest] %s done: end_reason=%s checks=%d/%d%s"
          % (scenario["name"], session.end_reason,
             result["checks_passed"], result["checks_total"],
             " ERROR=%s" % error if error else ""))
    return result


def run_checks(scenario, ws):
    out = []
    for check in scenario.get("checks", []):
        path = ws / check["file"]
        content = path.read_text(encoding="utf-8") if path.is_file() else None
        if content is None:
            ok, why = False, "file missing"
        elif "contains_any" in check:
            ok = any(s.lower() in content.lower()
                     for s in check["contains_any"])
            why = "contains_any %s" % check["contains_any"]
        elif "not_contains" in check:
            ok = all(s.lower() not in content.lower()
                     for s in check["not_contains"])
            why = "not_contains %s" % check["not_contains"]
        else:
            ok, why = True, "file exists"
        out.append({"file": check["file"], "rule": why, "pass": ok})
    return out


# ---------------------------------------------------------------- judge (parent)

JUDGE_PROMPT = """You are reviewing a recorded playtest of "echoecho", a \
hands-free voice assistant that farms real work out to background agents \
which write files into a live workspace. Judge ONLY from the evidence below.

## The task being tested
%(task)s

## Success criteria
%(criteria)s

## Red flags (failure modes to watch for)
%(red_flags)s

## Programmatic file checks (already executed)
%(checks)s

## Session transcript (rendered from the event log)
%(transcript)s

## Raw event tail (last events, JSONL)
%(events_tail)s

## Final workspace contents
%(workspace)s

Respond with ONLY a JSON object (no markdown fences, no prose) shaped:
{"task_completed": true|false,
 "criteria": [{"criterion": "...", "pass": true|false, "evidence": "short quote or observation"}],
 "red_flags_hit": ["..."],
 "user_experience_notes": "latency, dead air, confusing moments — as the user would feel them",
 "diagnosis": "if anything failed: the most likely root cause, pointing at prompt/tools/orchestration",
 "verdict_summary": "one sentence"}
"""


def _clip(text, limit=6000):
    text = text or "(none)"
    return text if len(text) <= limit else (
        text[:limit // 2] + "\n...[clipped]...\n" + text[-limit // 2:])


def judge_scenario(scenario, outdir):
    transcript = ""
    events_tail = ""
    rec = outdir / "recording"
    if (rec / "transcript.md").is_file():
        transcript = (rec / "transcript.md").read_text(encoding="utf-8")
    if (rec / "events.jsonl").is_file():
        lines = (rec / "events.jsonl").read_text(encoding="utf-8").splitlines()
        events_tail = "\n".join(lines[-60:])
    ws_parts = []
    ws = outdir / "workspace"
    for p in sorted(ws.rglob("*")):
        if p.is_file() and not p.name.startswith("."):
            try:
                body = p.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                body = "(binary)"
            ws_parts.append("### %s\n%s" % (p.relative_to(ws), _clip(body, 3000)))
    result = json.loads((outdir / "result.json").read_text(encoding="utf-8"))

    prompt = JUDGE_PROMPT % {
        "task": scenario["task"],
        "criteria": "\n".join("- " + c for c in scenario["success_criteria"]),
        "red_flags": "\n".join("- " + r for r in scenario.get("red_flags", [])),
        "checks": json.dumps(result["checks"], indent=1),
        "transcript": _clip(transcript, 12000),
        "events_tail": _clip(events_tail, 6000),
        "workspace": _clip("\n\n".join(ws_parts) or "(empty)", 10000),
    }
    proc = subprocess.run(
        ["claude", "-p", "--model", "sonnet", "--output-format", "json"],
        input=prompt, capture_output=True, text=True, timeout=300,
        cwd=str(outdir))
    verdict = None
    try:
        envelope = json.loads(proc.stdout)
        body = (envelope.get("result") or "").strip()
        if body.startswith("```"):
            body = body.strip("`").lstrip("json").strip()
        try:
            verdict = json.loads(body)
        except ValueError:
            # judges sometimes wrap the JSON in prose; take the outermost {...}
            start, end = body.find("{"), body.rfind("}")
            if start < 0 or end <= start:
                raise
            verdict = json.loads(body[start:end + 1])
    except (ValueError, KeyError):
        verdict = {"task_completed": None,
                   "verdict_summary": "judge output unparseable",
                   "raw": proc.stdout[-2000:], "stderr": proc.stderr[-500:]}
    (outdir / "verdict.json").write_text(json.dumps(verdict, indent=2),
                                         encoding="utf-8")
    lines = ["# Judge review — %s" % scenario["name"], "",
             "**Task completed:** %s" % verdict.get("task_completed"),
             "**Summary:** %s" % verdict.get("verdict_summary", ""), ""]
    for c in verdict.get("criteria", []):
        lines.append("- [%s] %s — %s" % ("x" if c.get("pass") else " ",
                                         c.get("criterion"), c.get("evidence")))
    if verdict.get("red_flags_hit"):
        lines += ["", "**Red flags hit:**"] + \
                 ["- " + r for r in verdict["red_flags_hit"]]
    for key in ("user_experience_notes", "diagnosis"):
        if verdict.get(key):
            lines += ["", "**%s:** %s" % (key.replace("_", " "), verdict[key])]
    (outdir / "review.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return verdict


# ---------------------------------------------------------------- orchestration

def load_scenarios(only=None):
    paths = sorted(SCENARIOS_DIR.glob("*.json"))
    scenarios = [json.loads(p.read_text(encoding="utf-8")) for p in paths]
    if only:
        scenarios = [s for s in scenarios if s["name"] in only]
    return scenarios


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", choices=["text", "realtime-text"], default="text")
    ap.add_argument("--only", nargs="*", help="scenario names to run")
    ap.add_argument("--no-judge", action="store_true")
    ap.add_argument("--run-one", metavar="SCENARIO_JSON",
                    help="(internal) run a single scenario in-process")
    ap.add_argument("--outdir", help="(internal) result dir for --run-one")
    args = ap.parse_args()

    from echoecho_app import config
    config.load_env_local()

    if args.run_one:
        scenario = json.loads(Path(args.run_one).read_text(encoding="utf-8"))
        asyncio.run(run_scenario(scenario, args.port, Path(args.outdir)))
        return

    scenarios = load_scenarios(args.only)
    if not scenarios:
        sys.exit("no scenarios matched")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    run_dir = RESULTS_ROOT / ("%s_%s" % (stamp, args.port))
    run_dir.mkdir(parents=True)
    print("[playtest] %d scenario(s), port=%s -> %s"
          % (len(scenarios), args.port, run_dir))

    rows = []
    for scenario in scenarios:
        outdir = run_dir / scenario["name"]
        outdir.mkdir(parents=True, exist_ok=True)
        spath = SCENARIOS_DIR / ("%s.json" % scenario["name"])
        print("[playtest] === %s ===" % scenario["name"])
        proc = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--run-one",
             str(spath), "--port", args.port, "--outdir", str(outdir)],
            capture_output=True, text=True, timeout=SCENARIO_TIMEOUT + 120,
            cwd=str(REPO_ROOT))
        (outdir / "console.log").write_text(proc.stdout + "\n--- stderr ---\n"
                                            + proc.stderr, encoding="utf-8")
        sys.stdout.write(proc.stdout[-1500:])
        try:
            result = json.loads((outdir / "result.json").read_text("utf-8"))
        except (OSError, ValueError):
            result = {"scenario": scenario["name"], "error":
                      "crashed (rc=%s) — see console.log" % proc.returncode,
                      "checks_passed": 0, "checks_total":
                      len(scenario.get("checks", []))}
        verdict = {}
        if not args.no_judge and not (result.get("error") or "").startswith("crashed"):
            print("[playtest] judging %s ..." % scenario["name"])
            verdict = judge_scenario(scenario, outdir) or {}
        rows.append((scenario["name"], result, verdict))

    report = ["# Playtest report — %s (port: %s)" % (stamp, args.port), "",
              "| scenario | completed | checks | end reason | verdict |",
              "|---|---|---|---|---|"]
    for name, result, verdict in rows:
        report.append("| %s | %s | %d/%d | %s | %s |" % (
            name, verdict.get("task_completed", "-"),
            result.get("checks_passed", 0), result.get("checks_total", 0),
            result.get("end_reason") or result.get("error", "?"),
            (verdict.get("verdict_summary") or "").replace("|", "/")))
    for name, result, verdict in rows:
        if verdict.get("diagnosis"):
            report += ["", "## %s — diagnosis" % name, verdict["diagnosis"]]
    (run_dir / "report.md").write_text("\n".join(report) + "\n",
                                       encoding="utf-8")
    print("\n".join(report))
    print("[playtest] full artifacts in %s" % run_dir)


if __name__ == "__main__":
    main()
