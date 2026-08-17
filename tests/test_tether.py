"""The tether watchdog ties the voice daemon's life to echoecho.app's pid."""
import os
import subprocess
import sys
import threading
import time

import echoecho


def test_untethered_without_env(monkeypatch):
    monkeypatch.delenv("ECHOECHO_TETHER_PID", raising=False)
    assert echoecho.start_tether_watchdog() is None


def test_garbage_pid_is_untethered(monkeypatch):
    monkeypatch.setenv("ECHOECHO_TETHER_PID", "not-a-pid")
    assert echoecho.start_tether_watchdog() is None


def test_fires_when_tethered_process_dies():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    died = threading.Event()
    t = echoecho.start_tether_watchdog(pid=proc.pid, interval=0.05,
                                       on_dead=died.set)
    assert t is not None
    # alive: the watchdog must NOT fire while the app process exists
    assert not died.wait(0.3)
    proc.kill()
    proc.wait()
    assert died.wait(5), "watchdog never noticed the app dying"
    t.join(5)
    assert not t.is_alive()


def _dead_pid():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    proc.kill()
    proc.wait()
    return proc.pid


def _run_shutdown_script(dead_pid, preamble=""):
    # the watchdog thread can't fire before start_tether_watchdog returns, so
    # wrapping the start call in the try keeps a fast SIGINT inside it (no
    # flake window between starting the watchdog and entering the try)
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script = (
        "import sys, time\n"
        "sys.path.insert(0, %r)\n"
        "import echoecho\n"
        "%s"
        "try:\n"
        "    echoecho.start_tether_watchdog(pid=%d, interval=0.05)\n"
        "    time.sleep(30)\n"
        "except KeyboardInterrupt:\n"
        "    print('clean-shutdown')\n" % (repo, preamble, dead_pid)
    )
    return subprocess.run([sys.executable, "-c", script], timeout=15,
                          capture_output=True, text=True)


def test_default_on_dead_interrupts_main_thread_like_ctrl_c():
    # the real daemon path: app dies -> watchdog SIGINTs the process ->
    # voice_main's finally/KeyboardInterrupt handling shuts down cleanly
    out = _run_shutdown_script(_dead_pid())
    assert "clean-shutdown" in out.stdout
    assert out.returncode == 0


def test_clean_shutdown_survives_inherited_sig_ign():
    # echoechoctl launches the daemon via `( nohup ... & )` from a
    # non-interactive shell, which hands SIGINT down as SIG_IGN; the watchdog
    # must restore the KeyboardInterrupt handler or the clean path never runs
    preamble = "import signal; signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
    out = _run_shutdown_script(_dead_pid(), preamble)
    assert "clean-shutdown" in out.stdout
    assert out.returncode == 0


def test_quiet_while_tethered_to_ourselves():
    # our own pid always exists: the watchdog just keeps polling
    died = threading.Event()
    t = echoecho.start_tether_watchdog(pid=os.getpid(), interval=0.05,
                                       on_dead=died.set)
    assert t is not None
    time.sleep(0.3)
    assert not died.is_set()
