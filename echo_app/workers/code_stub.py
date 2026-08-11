"""Stretch 'code' kind: shell out to a headless CLI coding agent.

Wraps `codex exec "<prompt>"` (or `claude -p "<prompt>"`) as an asyncio
subprocess with cwd=workspace/. Registered ONLY if one of the CLIs is on
PATH, so sandbox/demo runs without them are completely unaffected.
"""
import asyncio
import shutil

from echo_app.bus import TaskResult
from echo_app.workers.base import register

# Preference order: (executable, argv prefix).
CLIS = [("codex", ["codex", "exec"]), ("claude", ["claude", "-p"])]


def find_cli():
    for name, argv in CLIS:
        if shutil.which(name):
            return list(argv)
    return None


async def run_code(task, ctx):
    argv = find_cli()
    if argv is None:
        return TaskResult(say="No coding CLI (codex/claude) is installed.",
                          priority="interrupt", data={"error": "no cli"})
    proc = await asyncio.create_subprocess_exec(
        *(argv + [task.request.instructions]), cwd=str(ctx.workspace),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    out, _ = await proc.communicate()
    text = out.decode("utf-8", "replace").strip()
    if proc.returncode != 0:
        return TaskResult(say="The code task failed (%s exited %d)."
                          % (argv[0], proc.returncode),
                          priority="interrupt",
                          data={"error": "exit %d" % proc.returncode,
                                "output": text[-2000:]})
    last = text.splitlines()[-1] if text else "done"
    return TaskResult(say="Code task finished: %s" % last[:200],
                      priority="interrupt", data={"output": text[-4000:]})


if find_cli():  # register only when a CLI actually exists on PATH
    register("code")(run_code)
