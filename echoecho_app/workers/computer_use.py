"""computer.use: drive a real Mac app inside echoecho's VM (PR 14, the sandbox
ladder's GUI tier).

Runs a sequence of GUI steps against the guest's screen via the GuiDriver
port and captures a screenshot after each, into workspace/screens/<task>/, so
the run is watchable live in the viewer (PR 10 renders images) — that shot
trail IS the screen recording. Steps are a list of dicts:

    {"action": "launch",     "app": "TextEdit"}
    {"action": "open",       "path": "notes.md", "app": "TextEdit"}
    {"action": "type",       "text": "Hello from echoecho"}
    {"action": "key",        "combo": "cmd+s"}
    {"action": "wait",       "seconds": 1}
    {"action": "screenshot", "name": "after-save"}   # explicit extra shot

The VM guarantees isolation, so this runs unconfirmed at voice speed like the
rest of the tier; only writes back to real user documents still need the
outbox + spoken approval (PR 13).
"""
from pathlib import PurePosixPath

from echoecho_app.bus import TaskResult
from echoecho_app.services import gui as gui_mod
from echoecho_app.workers.base import register

KIND = "computer.use"
MAX_STEPS = 40
SHOT_DIR = "screens"


def _vm_configured():
    from echoecho_app import config
    return config.sandbox_tier() == "vm"


def _workspace_path(value):
    """Validate the guest file action cannot escape the shared workspace."""
    path = PurePosixPath(str(value or ""))
    if not str(path) or str(path) == "." or path.is_absolute() or ".." in path.parts:
        raise ValueError("open path must be a workspace-relative file")
    return path.as_posix()


async def _do_step(driver, step, shot_dir, idx):
    action = (step.get("action") or "").lower()
    if action == "launch":
        await driver.launch(step["app"])
        label = "launched %s" % step["app"]
    elif action == "open":
        path = _workspace_path(step.get("path"))
        await driver.open_file(path, step.get("app") or "TextEdit")
        label = "opened %s" % path
    elif action == "type":
        await driver.type_text(step.get("text", ""))
        label = "typed text"
    elif action == "key":
        await driver.key(step["combo"])
        label = "pressed %s" % step["combo"]
    elif action == "click":
        click = getattr(driver, "click", None)
        if click is None:
            raise ValueError("this GUI driver can't click (needs the VNC "
                             "input backend)")
        await click(step["x"], step["y"], step.get("button", 1))
        label = "clicked (%s, %s)" % (step["x"], step["y"])
    elif action == "wait":
        await driver.wait(step.get("seconds", 1))
        label = "waited"
    elif action == "screenshot":
        label = "screenshot"
    else:
        raise ValueError("unknown GUI action %r" % action)
    # a shot after every step (name it after an explicit screenshot step, else
    # by index) — the trail is the recording
    name = step.get("name") or ("step%02d" % idx)
    shot = await driver.screenshot("%s/%s.png" % (shot_dir, name))
    return label, shot


@register(KIND,
          description="drive a Mac app inside echoecho's VM by a sequence of GUI "
                      "steps, capturing screenshots; REQUIRES args.steps, a "
                      "list like [{'action':'launch','app':'TextEdit'}, "
                      "{'action':'open','path':'notes.md','app':'TextEdit'}, "
                      "{'action':'type','text':'hi'}, {'action':'key',"
                      "'combo':'cmd+s'}, {'action':'wait','seconds':1}, "
                      "{'action':'screenshot','name':'result'}] — prose in "
                      "instructions alone does nothing",
          arg_schema={"steps": {
              "type": "array",
              "description": "GUI steps: {action: launch|open|type|key|wait|"
                      "screenshot, ...}",
              "items": {"type": "object"}}},
          # same group as agent.run/doc.edit: both write workspace files AND
          # share the one warm LumeVM — unserialized, an agent.run budget
          # breach could discard() (delete!) the VM under a live GUI task
          serialize="workspace.write",
          advertise_when=_vm_configured)
async def run_computer_use(task, ctx):
    driver = gui_mod.for_ctx(ctx)
    if driver is None:
        return TaskResult(
            say="I can't drive apps here — that needs echoecho's Mac VM, which "
                "isn't set up.",
            priority="interrupt", data={"error": "no gui driver"})

    steps = task.request.args.get("steps") or []
    if not isinstance(steps, list) or not steps:
        # the say text is a steering message TO THE MODEL (it arrives as a
        # task-result injection): playtests showed a vague version being
        # relayed to the user as on-screen advice, so spell out the retry
        return TaskResult(
            say="That needs a re-dispatch: call dispatch_task again with "
                "kind computer.use and args {\"steps\": [{\"action\": "
                "\"open\", \"path\": \"notes.md\", \"app\": \"TextEdit\"}, {\"action\": "
                "\"screenshot\", \"name\": \"result\"}]} adjusted to the "
                "goal — prose instructions can't drive the screen.",
            priority="interrupt", data={"error": "no steps"})
    if len(steps) > MAX_STEPS:
        return TaskResult(
            say="That's more than %d GUI steps — break it into smaller tasks."
                % MAX_STEPS,
            priority="interrupt", data={"error": "too many steps"})

    # agent.run's worker prepares its sandbox before every task; the GUI tier
    # must do the same, or computer.use on a cold daemon (no prior vm task to
    # warm the shared VM) dies on ssh_argv's "VM not prepared"
    prepare = getattr(getattr(driver, "vm", None), "prepare", None)
    if prepare is not None:
        try:
            await prepare()  # idempotent: clone from golden, boot, wait for ssh
        except Exception as exc:
            return TaskResult(
                say="I couldn't start my Mac VM: %s" % exc,
                priority="interrupt", data={"error": str(exc)})

    shot_dir = "%s/%s" % (SHOT_DIR, task.id)
    done, shots = [], []
    try:
        for idx, step in enumerate(steps):
            try:
                label, shot = await _do_step(driver, step, shot_dir, idx)
            except (gui_mod.GuiError, KeyError, ValueError) as exc:
                # stop at the failing step, but keep the shots taken so far so
                # the user can see how far it got
                return TaskResult(
                    say="Stopped on step %d (%s) of the on-screen task: %s"
                        % (idx + 1, step.get("action", "?"), exc),
                    data={"error": str(exc), "completed": done,
                          "screens": shots}, artifacts_touched=shots)
            done.append(label)
            shots.append(shot)
    finally:
        close = getattr(driver, "close", None)
        if close is not None:
            close()

    return TaskResult(
        say="Did %d on-screen step%s in the VM; the screenshots are in your "
            "workspace." % (len(done), "" if len(done) == 1 else "s"),
        priority="interrupt",
        data={"completed": done, "screens": shots},
        artifacts_touched=shots)
