"""python3 -m livewriter — run the Live Writer server.

Reads OPENAI_API_KEY from the environment or from <repo>/.env.local (same
convention as echoecho.py, without importing the app — this feature stands
alone)."""

import argparse
import asyncio
import os
import sys

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
    ap = argparse.ArgumentParser(prog="livewriter")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=int(os.environ.get("LIVEWRITER_PORT", server.DEFAULT_PORT)))
    ap.add_argument("--fake", action="store_true", default=os.environ.get("LIVEWRITER_FAKE") == "1",
                    help="keyless mode: fake formatter, no ASR (text_input/sim_delta only)")
    ap.add_argument("--asr-model", default=os.environ.get("LIVEWRITER_ASR_MODEL"))
    ap.add_argument("--model", default=os.environ.get("LIVEWRITER_MODEL"),
                    help="formatter model (default gpt-4.1-mini)")
    ap.add_argument("--log-dir", default=None)
    args = ap.parse_args(argv)

    load_env_local()
    if not args.fake and not os.environ.get("OPENAI_API_KEY"):
        print("livewriter: no OPENAI_API_KEY (put it in .env.local) — use --fake for keyless mode",
              file=sys.stderr)
        return 2

    def ready(_srv):
        print("livewriter: listening on http://%s:%d/  (fake=%s)" % (args.host, args.port, args.fake),
              flush=True)

    try:
        asyncio.run(server.serve(host=args.host, port=args.port, fake=args.fake,
                                 log_dir=args.log_dir, asr_model=args.asr_model,
                                 fmt_model=args.model, ready_cb=ready))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
