"""Worker registry (Contract B): kind -> async run(task, ctx) -> TaskResult.

v2: workers carry their own declaration (description + arg schema) so the
dispatch_task enum and the system-prompt kinds line are GENERATED from the
registry — one source of truth instead of v0's three hand-written copies.
Discovery is a package scan: every module in echo_app.workers registers core
kinds; every module in echo_app.plugins registers optional fast-path kinds
that stay dispatchable but are advertised to the voice model only with
ECHO_PLUGINS=1.
"""
import importlib
import pkgutil

REGISTRY = {}

PLUGINS_PACKAGE = "echo_app.plugins"


def register(kind, description="", arg_schema=None, advertise=True):
    """Attach Contract-B metadata to the worker fn and add it to REGISTRY.
    advertise=False keeps a kind dispatchable (tests, scripts, follow_ups)
    without ever showing it to the voice model."""
    def deco(fn):
        fn.kind = kind
        fn.description = description
        fn.arg_schema = arg_schema or {}
        fn.advertise = advertise
        fn.is_plugin = fn.__module__.startswith(PLUGINS_PACKAGE)
        REGISTRY[kind] = fn
        return fn
    return deco


def _scan(package_name, required):
    """Import every module in a package so @register side effects run.
    Core workers import strictly; a broken optional plugin must not take the
    daemon down, so plugin import errors are reported and skipped."""
    try:
        package = importlib.import_module(package_name)
    except ImportError:
        if required:
            raise
        return
    for info in pkgutil.iter_modules(package.__path__):
        name = package_name + "." + info.name
        try:
            importlib.import_module(name)
        except Exception as exc:
            if required:
                raise
            print("[workers] plugin %s failed to load (%s) — skipped"
                  % (name, exc))


def load_all():
    """Discover workers (strict) + plugins (lenient); return REGISTRY."""
    _scan("echo_app.workers", required=True)
    _scan(PLUGINS_PACKAGE, required=False)
    return REGISTRY


def advertised():
    """Worker fns the voice model may dispatch, sorted by kind. Plugins are
    included only when ECHO_PLUGINS=1 (they stay dispatchable regardless)."""
    from echo_app import config  # lazy: config never imports workers
    return [fn for _, fn in sorted(REGISTRY.items())
            if fn.advertise and (not fn.is_plugin or config.echo_plugins())]


def kinds_enum():
    """The generated dispatch_task kind enum."""
    return [fn.kind for fn in advertised()]


def kinds_fragment():
    """The generated system-prompt line: 'kind (what it does), ...'."""
    return ", ".join("%s (%s)" % (fn.kind, fn.description) if fn.description
                     else fn.kind for fn in advertised())
