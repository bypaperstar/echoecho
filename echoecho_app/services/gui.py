"""GuiDriver port: drive the macOS guest's SCREEN, not just its shell.

The VM tier (services/vm.py) runs headless agents; PR 14 adds GUI computer-use
so echoecho can operate real Mac apps inside its VM. One port, two backends:

  SshGuiDriver  drives the guest over the same ssh channel as LumeVM, using
                only macOS built-ins — `open -a` to launch apps, `osascript`
                (System Events) to type text and press key shortcuts,
                `screencapture` to grab the screen. Screenshots are written
                straight into the shared workspace mount, so they appear on
                the host live and the type-aware viewer (PR 10) renders them.
  FakeGuiDriver records every action and writes a stub PNG, so the whole
                computer-use path runs keyless + Linux in CI.

Deliberately built-in only (no cliclick/Quartz/pyobjc to preinstall): launch,
type, key, screenshot, wait. Coordinate clicks and a model-driven perceive→act
loop are the follow-up; a scripted app sequence (open TextEdit, write, save,
screenshot) is already a real, testable capability.

macOS TCC caveat (verified live): `open` and `screencapture` work out of the
box, but `osascript`/System Events synthetic keystrokes need Accessibility
permission, which a SIP-enabled vanilla image will not grant to an
SSH-invoked process — the call would hang on an unanswerable GUI prompt. So
every guest GUI command is bounded by a timeout (a blocked keystroke fails
fast with a clear message instead of hanging the task), and the golden image
must pre-grant Accessibility (SIP-disabled build + TCC entry, or a PPPC
profile) for type/key to work. launch + screenshot need no such grant.
"""
import asyncio
import shlex

from echoecho_app import config

GUI_TIMEOUT = 20.0  # a TCC-blocked osascript would otherwise hang forever

# osascript keystroke modifiers, and a few named keys by AppleScript key code.
_MODS = {"cmd": "command down", "command": "command down",
         "shift": "shift down", "opt": "option down", "option": "option down",
         "alt": "option down", "ctrl": "control down", "control": "control down"}
_KEYCODES = {"return": 36, "enter": 36, "tab": 48, "space": 49, "escape": 53,
             "esc": 53, "delete": 51, "left": 123, "right": 124, "down": 125,
             "up": 126}


def _osascript(script):
    return ["osascript", "-e", script]


def _keystroke_script(combo):
    """'cmd+s' / 'cmd+shift+4' / 'return' -> an AppleScript System Events line.
    A bare named key uses `key code`; a char (optionally with modifiers) uses
    `keystroke`."""
    parts = [p.strip().lower() for p in combo.split("+") if p.strip()]
    if not parts:
        raise ValueError("empty key combo")
    *mods, key = parts
    for m in mods:
        if m not in _MODS:
            raise ValueError("unknown modifier %r" % m)
    using = " using {%s}" % ", ".join(_MODS[m] for m in mods) if mods else ""
    if key in _KEYCODES:
        body = "key code %d%s" % (_KEYCODES[key], using)
    elif len(key) == 1:
        body = "keystroke %s%s" % (_as_str(key), using)
    else:
        raise ValueError("unknown key %r (use a single char or %s)"
                         % (key, "/".join(sorted(_KEYCODES))))
    return 'tell application "System Events" to ' + body


def _as_str(text):
    """AppleScript double-quoted string literal (escape \\ and ")."""
    return '"%s"' % text.replace("\\", "\\\\").replace('"', '\\"')


class GuiDriver:
    async def launch(self, app):
        raise NotImplementedError

    async def open_file(self, path, app="TextEdit"):
        """Open a workspace-relative file in a guest desktop application."""
        raise NotImplementedError

    async def type_text(self, text):
        raise NotImplementedError

    async def key(self, combo):
        raise NotImplementedError

    async def screenshot(self, name):
        """Capture the screen to workspace-relative `name`; return the name."""
        raise NotImplementedError

    async def wait(self, seconds):
        await asyncio.sleep(min(float(seconds), 30.0))


class SshGuiDriver(GuiDriver):
    def __init__(self, vm, workspace):
        self.vm = vm            # a LumeVM (ssh_argv / guest_path)
        self.workspace = workspace

    async def _run(self, argv, capture=False):
        """Run a built-in in the guest over ssh; raise on failure or timeout.
        The timeout matters: a keystroke blocked on an Accessibility TCC
        prompt never returns, so bound it and report instead of hanging."""
        remote = " ".join(shlex.quote(a) for a in argv)
        ssh = self.vm.ssh_argv(remote)
        proc = await asyncio.create_subprocess_exec(
            *ssh, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), GUI_TIMEOUT)
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
            raise GuiError(
                "guest GUI command timed out (%s) — likely an Accessibility "
                "permission the VM hasn't granted; launch/screenshot work, "
                "type/key need the golden image's TCC grant" % argv[0])
        if proc.returncode != 0:
            raise GuiError("guest GUI command failed (%s): %s"
                           % (argv[0], err.decode("utf-8", "replace")[-200:]))
        return out

    async def launch(self, app):
        await self._run(["open", "-a", app])

    async def open_file(self, path, app="TextEdit"):
        # `path` is validated by computer.use before it reaches the driver.
        # Pass an unquoted argv item here: _run quotes each argument while it
        # builds the remote shell command.
        guest = config.vm_guest_workspace().rstrip("/") + "/" + path
        # A fresh macOS guest has no default association for .md, so a plain
        # `open file.md` fails. TextEdit is present in the base image and is
        # the right default for EchoEcho's workspace notes.
        await self._run(["open", "-a", app, guest])

    async def type_text(self, text):
        await self._run(_osascript(
            'tell application "System Events" to keystroke %s' % _as_str(text)))

    async def key(self, combo):
        await self._run(_osascript(_keystroke_script(combo)))

    async def screenshot(self, name):
        # screencapture writes straight to the shared workspace mount, so the
        # PNG lands on the host (virtiofs) with no separate transfer
        guest = self.vm.guest_path(name)
        await self._run(["sh", "-c",
                         "mkdir -p \"$(dirname %s)\" && screencapture -x -t png %s"
                         % (guest, guest)])
        return name


class FakeGuiDriver(GuiDriver):
    """CI backend: records actions and writes a 1x1 PNG for each screenshot so
    the worker's artifact/viewer path is exercised without a screen."""

    _PNG_1x1 = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00"
                b"\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx"
                b"\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND"
                b"\xaeB`\x82")

    def __init__(self, workspace):
        from pathlib import Path
        self.workspace = Path(workspace)
        self.actions = []

    async def launch(self, app):
        self.actions.append(("launch", app))

    async def open_file(self, path, app="TextEdit"):
        self.actions.append(("open", path, app))

    async def type_text(self, text):
        self.actions.append(("type", text))

    async def key(self, combo):
        _keystroke_script(combo)  # validate the combo even in the fake
        self.actions.append(("key", combo))

    async def wait(self, seconds):
        self.actions.append(("wait", float(seconds)))

    async def screenshot(self, name):
        from echoecho_app.services import artifacts
        artifacts.write_atomic(self.workspace, name, self._PNG_1x1)
        self.actions.append(("screenshot", name))
        return name


class GuiError(Exception):
    pass


def for_ctx(ctx):
    """Pick the GUI driver: injected (tests) > an SshGuiDriver over the task's
    VM sandbox. Returns None when there's no VM to drive (computer.use then
    reports instead of crashing)."""
    injected = ctx.extra.get("gui_driver")
    if injected is not None:
        return injected
    from echoecho_app.services import vm as vm_mod
    sandbox = ctx.extra.get("sandbox")
    if sandbox is None and config.sandbox_tier() == "vm":
        sandbox = vm_mod.shared_vm(workspace=ctx.workspace)
    if hasattr(sandbox, "ssh_argv"):  # a LumeVM
        return SshGuiDriver(sandbox, ctx.workspace)
    return None
