"""Worker registry (Contract B): kind -> async run(task, ctx) -> TaskResult."""

REGISTRY = {}


def register(kind):
    def deco(fn):
        REGISTRY[kind] = fn
        return fn
    return deco


def load_all():
    """Import every worker module so @register side effects run; return REGISTRY."""
    from echo_app.workers import (demo, doc_edit, recipe, grocery,  # noqa: F401
                                  learn, code_stub)  # noqa: F401
    return REGISTRY
