"""python3 -m livewriter — run the Live Writer server.

Reads OPENAI_API_KEY from the environment or from <repo>/.env.local (same
convention as echoecho.py, without importing the app — this feature stands
alone)."""

import argparse
import asyncio
import os
import platform
import signal
import sys
import threading

from echoecho_app import __version__, diagnostics

from . import server


def load_env_local():
    path = os.path.join(server.REPO, ".env.local")
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    except OSError:
        pass


def main(argv=None):
    raw_default_port = os.environ.get("LIVEWRITER_PORT", str(server.DEFAULT_PORT))
    try:
        default_port = int(raw_default_port)
        invalid_port_env = False
    except (TypeError, ValueError):
        default_port = server.DEFAULT_PORT
        invalid_port_env = True
    ap = argparse.ArgumentParser(prog="livewriter")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=default_port)
    ap.add_argument("--fake", action="store_true", default=os.environ.get("LIVEWRITER_FAKE") == "1",
                    help="keyless mode: fake formatter, no ASR (text_input/sim_delta only)")
    ap.add_argument("--asr-model", default=os.environ.get("LIVEWRITER_ASR_MODEL"))
    ap.add_argument("--model", default=os.environ.get("LIVEWRITER_MODEL"),
                    help="formatter model (default gpt-4.1-mini)")
    ap.add_argument("--log-dir", default=None)
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        if exc.code not in (None, 0):
            diagnostics.configure(
                "livewriter", mode="argument_error", version=__version__,
                python=platform.python_version())
            diagnostics.error("livewriter.argument_invalid")
            diagnostics.shutdown(outcome="error", exit_code=(
                exc.code if isinstance(exc.code, int) else 1))
        raise

    load_env_local()
    diagnostics.configure(
        "livewriter", mode="fake" if args.fake else "live",
        version=__version__, python=platform.python_version(),
        parent_run_id=os.environ.get("ECHOECHO_PARENT_RUN_ID") or None)
    bind_scope = ("loopback" if args.host in (
        "127.0.0.1", "localhost", "::1") else "non_loopback")
    diagnostics.info(
        "livewriter.startup", fake=args.fake, bind_scope=bind_scope,
        port=args.port,
        api_key_present=bool(os.environ.get("OPENAI_API_KEY")),
        asr_model=args.asr_model, formatter_model=args.model)
    if invalid_port_env:
        diagnostics.warning(
            "livewriter.configuration_invalid",
            setting="LIVEWRITER_PORT", fallback_port=default_port,
            value_type=type(raw_default_port).__name__,
            value_length=len(str(raw_default_port)))
    if not args.fake and not os.environ.get("OPENAI_API_KEY"):
        diagnostics.error("livewriter.configuration_invalid",
                          reason="OPENAI_API_KEY missing")
        print("livewriter: no OPENAI_API_KEY (put it in .env.local) — use --fake for keyless mode",
              file=sys.stderr)
        diagnostics.shutdown(outcome="configuration_error")
        return 2

    def ready(_srv):
        print("livewriter: listening on http://%s:%d/  (fake=%s)" % (args.host, args.port, args.fake),
              flush=True)

    previous_term = signal.getsignal(signal.SIGTERM)
    term_cancel = threading.Event()
    term_backstop_started = [False]

    def hard_term_exit():
        if not term_cancel.wait(10.0):
            os._exit(143)

    def on_term(signum, _frame):
        if not term_backstop_started[0]:
            term_backstop_started[0] = True
            threading.Thread(
                target=hard_term_exit, daemon=True,
                name="livewriter-sigterm-hard-exit").start()
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, on_term)
    outcome = "ok"
    try:
        try:
            asyncio.run(server.serve(host=args.host, port=args.port, fake=args.fake,
                                     log_dir=args.log_dir, asr_model=args.asr_model,
                                     fmt_model=args.model, ready_cb=ready))
        except KeyboardInterrupt:
            outcome = "interrupted"
            diagnostics.info("livewriter.interrupted")
        except Exception as exc:
            outcome = "error"
            diagnostics.exception("livewriter.server.failed", exc=exc)
            raise
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        try:
            diagnostics.shutdown(outcome=outcome)
        finally:
            term_cancel.set()
    return 0


if __name__ == "__main__":
    sys.exit(main())
