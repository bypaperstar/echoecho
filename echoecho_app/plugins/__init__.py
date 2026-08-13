"""Optional fast-path plugins: the demo-era workers, kept runnable.

Discovered by the same load_all() package scan as core workers, so their
kinds stay dispatchable (tests, scripts, follow_ups) — but they are
advertised to the voice model only with ECHOECHO_PLUGINS=1. The generic
agent.run kind covers the same ground; these remain as fast paths that
skip an agent round trip.
"""
