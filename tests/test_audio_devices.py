"""PR 8: device selection + per-session re-resolution (AirPods hot-plug).

All headless: a fake `sounddevice` module is injected into sys.modules (the
real one is not installed on the Linux sandbox and there is no audio
hardware). The fake tracks open streams and counts PortAudio re-inits, so we
can assert the CRITICAL invariant: refresh_devices() (sounddevice._terminate
+ _initialize) never runs while any stream is open, across the exact
voice_main session-boundary sequence (echoecho.start_session_audio /
echoecho.end_session_audio).
"""
import json
import os
import subprocess
import sys
import types

import pytest

import echoecho
from echoecho_app import config, events
from echoecho_app.conversation.audio import (AudioIO, device_label,
                                         refresh_devices, resolve_device)
from echoecho_app.wake.mic import WakeMic

DEVICES = [
    {"name": "MacBook Pro Microphone",
     "max_input_channels": 1, "max_output_channels": 0},
    {"name": "MacBook Pro Speakers",
     "max_input_channels": 0, "max_output_channels": 2},
    {"name": "USB Audio Device",
     "max_input_channels": 2, "max_output_channels": 2},
]

AIRPODS = {"name": "AirPods Pro",
           "max_input_channels": 1, "max_output_channels": 2}


def make_fake_sd(devices, default=(0, 1)):
    sd = types.ModuleType("sounddevice")
    sd.devices = [dict(d) for d in devices]
    sd.open_streams = []       # streams constructed and not yet close()d
    sd.refreshes = 0           # completed _terminate+_initialize cycles
    sd.refresh_violations = 0  # _terminate called WITH open streams (the bug)
    sd.default = types.SimpleNamespace(device=list(default))

    class FakeStream(object):
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.started = False
            sd.open_streams.append(self)  # PortAudio opens at construction

        def start(self):
            self.started = True

        def stop(self):
            self.started = False

        def close(self):
            sd.open_streams.remove(self)

    sd.RawInputStream = FakeStream
    sd.RawOutputStream = FakeStream

    def query_devices(device=None, kind=None):
        if device is not None:
            return sd.devices[device]
        if kind is not None:  # default device for that kind (real sd API)
            return sd.devices[sd.default.device[0 if kind == "input" else 1]]
        return list(sd.devices)

    def _terminate():
        if sd.open_streams:
            sd.refresh_violations += 1

    def _initialize():
        sd.refreshes += 1

    sd.query_devices = query_devices
    sd._terminate = _terminate
    sd._initialize = _initialize
    return sd


@pytest.fixture
def fake_sd(monkeypatch):
    sd = make_fake_sd(DEVICES)
    monkeypatch.setitem(sys.modules, "sounddevice", sd)
    return sd


@pytest.fixture
def feed_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WORKSPACE_DIR", tmp_path)
    return tmp_path


def feed_records(feed_dir):
    path = feed_dir / events.FEED_NAME
    if not path.is_file():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


# -- resolve_device -----------------------------------------------------------


def test_resolve_default_and_index_never_import_sounddevice():
    # sounddevice is NOT installed and NOT faked here: these paths must not
    # even try to import it (resolution short-circuits).
    assert resolve_device(None, "input") is None
    assert resolve_device("", "output") is None
    assert resolve_device("   ", "input") is None
    assert resolve_device("3", "input") == 3
    assert resolve_device(" 12 ", "output") == 12
    assert resolve_device(7, "output") == 7  # already-resolved int passthrough


def test_resolve_substring_case_insensitive(fake_sd):
    assert resolve_device("usb", "input") == 2
    assert resolve_device("MICROPHONE", "input") == 0
    fake_sd.devices.append(dict(AIRPODS))
    assert resolve_device("pods", "output") == 3


def test_resolve_filters_by_kind(fake_sd):
    # "Microphone" exists but has zero output channels -> not an output match
    with pytest.raises(ValueError):
        resolve_device("microphone", "output")
    with pytest.raises(ValueError):
        resolve_device("speakers", "input")


def test_resolve_ambiguous_first_match_wins(fake_sd):
    fake_sd.devices.append(dict(AIRPODS))
    # "pro" matches both "MacBook Pro Speakers" (1) and "AirPods Pro" (3)
    assert resolve_device("pro", "output") == 1


def test_resolve_no_match_lists_available_names(fake_sd):
    with pytest.raises(ValueError) as exc:
        resolve_device("bogus headset", "input")
    msg = str(exc.value)
    assert "bogus headset" in msg
    assert "MacBook Pro Microphone" in msg and "USB Audio Device" in msg
    assert "MacBook Pro Speakers" not in msg  # output-only: not an input option


# -- refresh_devices ----------------------------------------------------------


def test_refresh_devices_is_noop_without_sounddevice():
    assert refresh_devices() is False  # ImportError swallowed, Linux-safe


def test_refresh_devices_guards_missing_private_api(monkeypatch):
    sd = types.ModuleType("sounddevice")  # future version: no _terminate
    monkeypatch.setitem(sys.modules, "sounddevice", sd)
    assert refresh_devices() is False


def test_refresh_devices_reinitializes(fake_sd):
    assert refresh_devices() is True
    assert fake_sd.refreshes == 1
    assert fake_sd.refresh_violations == 0


# -- the voice_main session-boundary sequence ---------------------------------


class StubDetector(object):
    def __init__(self):
        self.resumed = 0

    def resume(self):
        self.resumed += 1


def test_session_sequence_never_refreshes_with_open_streams(fake_sd, feed_dir):
    mic = WakeMic().start()
    detector = StubDetector()
    audio = AudioIO(output_device="pods")
    assert len(fake_sd.open_streams) == 1  # wake mic only

    # AirPods connect while echoecho is IDLE (after PortAudio init)...
    fake_sd.devices.append(dict(AIRPODS))

    # wake: mic fully closes BEFORE the refresh; session streams open after,
    # resolving fresh -> the hot-plugged AirPods are found
    echoecho.start_session_audio(mic, audio, loop=None, send_event=None)
    assert fake_sd.refreshes == 1
    assert fake_sd.refresh_violations == 0
    assert len(fake_sd.open_streams) == 2  # session in+out; wake mic closed
    assert audio._out_stream.kwargs["device"] == 3  # AirPods, resolved at open

    # session end: streams close BEFORE the refresh; wake mic reopens fresh;
    # detector re-armed last
    echoecho.end_session_audio(mic, audio, detector)
    assert fake_sd.refreshes == 2
    assert fake_sd.refresh_violations == 0
    assert len(fake_sd.open_streams) == 1  # wake mic back, session gone
    assert mic._stream.started
    assert detector.resumed == 1


def test_crashed_audio_start_leaves_no_stream_open_across_refresh(fake_sd):
    # The un-testable-on-Mac disaster is PortAudio re-init with an open
    # stream. Worst case: AudioIO.start() opens the input stream, then the
    # OUTPUT stream constructor raises (device vanished between resolve and
    # open). voice_main's finally must close that orphan via audio.stop()
    # BEFORE end_session_audio's refresh — lock the invariant.
    class Boom(Exception):
        pass

    good_stream = fake_sd.RawOutputStream

    def failing_out(**kwargs):
        raise Boom("output device vanished")

    mic = WakeMic().start()
    detector = StubDetector()
    audio = AudioIO()
    fake_sd.RawOutputStream = failing_out
    with pytest.raises(Boom):
        echoecho.start_session_audio(mic, audio, loop=None, send_event=None)
    assert len(fake_sd.open_streams) == 1  # orphaned session input stream
    # voice_main's finally path:
    audio.muted_capture = True
    audio.play_chime("end")
    fake_sd.RawOutputStream = good_stream
    echoecho.end_session_audio(mic, audio, detector)
    assert fake_sd.refresh_violations == 0  # orphan closed before re-init
    assert len(fake_sd.open_streams) == 1   # wake mic back, nothing leaked
    assert detector.resumed == 1


def test_end_session_audio_survives_mic_reopen_failure(fake_sd):
    mic = WakeMic(device="usb").start()
    detector = StubDetector()
    mic.stop()  # as during ACTIVE: start_session_audio closed the wake mic
    del fake_sd.devices[2]  # USB mic unplugged mid-session
    echoecho.end_session_audio(mic, AudioIO(), detector)  # must not raise
    assert detector.resumed == 1  # daemon re-armed despite the dead mic
    assert fake_sd.refresh_violations == 0


# -- WakeMic re-resolution ------------------------------------------------------


def test_wake_mic_reopen_re_resolves_device(fake_sd, capsys):
    fake_sd.devices.append(dict(AIRPODS))
    mic = WakeMic(device="pods").start()
    assert mic._stream.kwargs["device"] == 3
    # device list mutates (something plugged in ahead of the AirPods)
    fake_sd.devices.insert(0, {"name": "Webcam Mic", "max_input_channels": 1,
                               "max_output_channels": 0})
    mic.chunks.put(b"stale")
    mic.reopen()
    assert mic._stream.kwargs["device"] == 4  # re-resolved, not cached
    assert mic.read(timeout=0.01) is None     # stale chunks drained
    assert capsys.readouterr().out.count("[wake] mic: AirPods Pro") == 2


# -- observability: the "audio" event -----------------------------------------


def test_audio_start_emits_resolved_names_into_feed(fake_sd, feed_dir, capsys):
    audio = AudioIO(input_device="usb", output_device="speakers")
    audio.start(loop=None, send_event=None)
    audio.stop()
    recs = [r for r in feed_records(feed_dir) if r["type"] == "audio"]
    assert len(recs) == 1
    assert recs[0]["input"] == "USB Audio Device"
    assert recs[0]["output"] == "MacBook Pro Speakers"
    out = capsys.readouterr().out
    assert "[audio] mic: USB Audio Device -> speaker: MacBook Pro Speakers" in out


def test_audio_default_labels_name_the_actual_defaults(fake_sd, feed_dir):
    AudioIO().start(loop=None, send_event=None).stop()
    rec = [r for r in feed_records(feed_dir) if r["type"] == "audio"][0]
    assert rec["input"] == "MacBook Pro Microphone (system default)"
    assert rec["output"] == "MacBook Pro Speakers (system default)"


def test_device_label_without_sounddevice_stays_polite():
    assert device_label(None, "input") == "system default"
    assert device_label(2, "output") == "device 2"


def test_viewer_renders_audio_event_as_activity_line():
    html = (config.REPO_ROOT / "echoecho_app" / "viewer"
            / "index.html").read_text(encoding="utf-8")
    assert "'audio'" in html
    assert "🎧 mic: " in html and "speaker: " in html


# -- echoecho.py flags / env --------------------------------------------------------


def test_config_device_env_getters(monkeypatch):
    monkeypatch.delenv("ECHOECHO_INPUT_DEVICE", raising=False)
    monkeypatch.delenv("ECHOECHO_OUTPUT_DEVICE", raising=False)
    assert config.input_device() == ""   # "" = follow system default
    assert config.output_device() == ""
    monkeypatch.setenv("ECHOECHO_INPUT_DEVICE", " AirPods ")
    monkeypatch.setenv("ECHOECHO_OUTPUT_DEVICE", "3")
    assert config.input_device() == "AirPods"
    assert config.output_device() == "3"


def test_device_flags_override_env(monkeypatch):
    monkeypatch.setenv("ECHOECHO_INPUT_DEVICE", "from-env-in")
    monkeypatch.setenv("ECHOECHO_OUTPUT_DEVICE", "from-env-out")
    args = echoecho.parse_args(["--voice", "--input-device", "macbook pro microphone",
                            "--output-device", "pods"])
    echoecho.apply_device_args(args)
    assert os.environ["ECHOECHO_INPUT_DEVICE"] == "macbook pro microphone"
    assert os.environ["ECHOECHO_OUTPUT_DEVICE"] == "pods"
    # no flags -> env untouched
    monkeypatch.setenv("ECHOECHO_INPUT_DEVICE", "from-env-in")
    echoecho.apply_device_args(echoecho.parse_args(["--voice"]))
    assert os.environ["ECHOECHO_INPUT_DEVICE"] == "from-env-in"


def test_list_devices_exits_politely_without_sounddevice():
    # Linux sandbox: no sounddevice installed -> nonzero + a helpful pointer
    proc = subprocess.run(
        [sys.executable, str(config.REPO_ROOT / "echoecho.py"), "--list-devices"],
        capture_output=True, text=True, timeout=30)
    assert proc.returncode != 0
    err = proc.stderr + proc.stdout
    assert "sounddevice" in err
    assert "requirements-mac.txt" in err


def test_list_devices_table_marks_capabilities_and_defaults(fake_sd, capsys):
    echoecho.list_devices()
    out = capsys.readouterr().out
    for dev in DEVICES:
        assert dev["name"] in out
    assert "default in" in out and "default out" in out
    line = [ln for ln in out.splitlines() if "USB Audio Device" in ln][0]
    assert line.split()[:3] == ["2", "2", "2"]  # idx, in ch, out ch
