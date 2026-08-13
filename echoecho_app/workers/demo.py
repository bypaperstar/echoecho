"""Trivial demo worker proving the 3-layer round trip."""
import asyncio

from echoecho_app.bus import TaskResult
from echoecho_app.workers.base import register


@register("sleep.echoecho", advertise=False)  # smoke-test kind, never in the enum
async def run(task, ctx):
    await asyncio.sleep(float(task.request.args.get("sleep", 0.3)))
    return TaskResult(say="echoechoing back: %s" % (task.request.instructions or "(nothing)"),
                      priority="interrupt",
                      data={"echoed": task.request.instructions})
