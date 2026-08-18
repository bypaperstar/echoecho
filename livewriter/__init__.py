"""livewriter — the Live Writer: talk out loud, a real agent writes and
editorializes the document live.

This package is deliberately independent of the echoecho daemon/orchestrator:
it is the productionized version of mockups/live-writer-demo.html, with the
simulated ASR replaced by OpenAI realtime transcription (gpt-live-transcribe
streams word-by-word deltas mid-speech) and the rule-based formatter replaced
by a streaming LLM that emits small JSONL edit operations.

Run it: python3 -m livewriter          (serves http://127.0.0.1:8799/)
Keyless: LIVEWRITER_FAKE=1 python3 -m livewriter
"""

__version__ = "0.1"
