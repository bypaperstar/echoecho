"""Priority heuristic for finished tasks.

error / needs_input always interrupt; otherwise honor the worker's declared
priority; a result with nothing to say degrades to silent (table-only).
"""
from echo_app.bus import TaskResult
from echo_app.config import PRIORITIES


def rank(result):  # type: (TaskResult) -> str
    if result.data.get("error") or result.data.get("needs_input"):
        return "interrupt"
    if not result.say:
        return "silent"
    if result.priority in PRIORITIES:
        return result.priority
    return "ambient"
