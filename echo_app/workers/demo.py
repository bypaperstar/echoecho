"""Trivial demo worker proving the 3-layer round trip."""
import asyncio

from echo_app.bus import TaskResult
from echo_app.workers.base import register


@register("sleep.echo")
async def run(task, ctx):
    await asyncio.sleep(float(task.request.args.get("sleep", 0.3)))
    return TaskResult(say="Echoing back: %s" % (task.request.instructions or "(nothing)"),
                      priority="interrupt",
                      data={"echoed": task.request.instructions})
