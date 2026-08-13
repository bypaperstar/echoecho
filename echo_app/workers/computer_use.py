"""computer.use: drive a real Mac app inside Echo's VM (PR 14, the sandbox
ladder's GUI tier).

Runs a sequence of GUI steps against the guest's screen via the GuiDriver
port and captures a screenshot after each, into workspace/screens/<task>/, so
the run is watchable live in the viewer (PR 10 renders images) — that shot
trail IS the screen recording. Steps are a list of dicts:

    {"action": "launch",     "app": "TextEdit"}
    {"action": "type",       "text": "Hello from Echo"}
    {"action": "key",        "combo": "cmd+s"}
    {"action": "wait",       "seconds": 1}
    {"action": "screenshot", "name": "after-save"}   # explicit extra shot

The VM guarantees isolation, so this runs unconfirmed at voice speed like the
rest of the tier; only writes back to real user documents still need the
outbox + spoken approval (PR 13).
"""
from echo_app.bus import TaskResult
from echo_app.services import gui as gui_mod
from echo_app.workers.base import register

KIND = "computer.use"
MAX_STEPS = 40
SHOT_DIR = "screens"


def _vm_configured():
    from echo_app import config
    return config.sandbox_tier() == "vm"


async def _do_step(driver, step, shot_dir, idx):
    action = (step.get("action") or "").lower()
    if action == "launch":
        await driver.launch(step["app"])
        label = "launched %s" % step["app"]
    elif action == "type":
        await driver.type_text(step.get("text", ""))
        label = "typed text"
    elif action == "key":
        await driver.key(step["combo"])
        label = "pressed %s" % step["combo"]
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
          description="drive a Mac app inside Echo's VM by a sequence of GUI "
                      "steps (launch/type/key), capturing screenshots",
          arg_schema={"steps": {
              "type": "array",
              "description": "GUI steps: {action: launch|type|key|wait|"
                             "screenshot, ...}",
              "items": {"type": "object"}}},
          advertise_when=_vm_configured)
async def run_computer_use(task, ctx):
    driver = gui_mod.for_ctx(ctx)
    if driver is None:
        return TaskResult(
            say="I can't drive apps here — that needs Echo's Mac VM, which "
                "isn't set up.",
            priority="interrupt", data={"error": "no gui driver"})

    steps = task.request.args.get("steps") or []
    if not isinstance(steps, list) or not steps:
        return TaskResult(say="I need a list of steps to run on screen.",
                          data={"error": "no steps"})
    if len(steps) > MAX_STEPS:
        return TaskResult(
            say="That's more than %d GUI steps — break it into smaller tasks."
                % MAX_STEPS,
            priority="interrupt", data={"error": "too many steps"})

    shot_dir = "%s/%s" % (SHOT_DIR, task.id)
    done, shots = [], []
    for idx, step in enumerate(steps):
        try:
            label, shot = await _do_step(driver, step, shot_dir, idx)
        except (gui_mod.GuiError, KeyError, ValueError) as exc:
            # stop at the failing step, but keep the shots taken so far so the
            # user can see how far it got
            return TaskResult(
                say="Stopped on step %d (%s) of the on-screen task: %s"
                    % (idx + 1, step.get("action", "?"), exc),
                data={"error": str(exc), "completed": done,
                      "screens": shots}, artifacts_touched=shots)
        done.append(label)
        shots.append(shot)

    return TaskResult(
        say="Did %d on-screen step%s in the VM; the screenshots are in your "
            "workspace." % (len(done), "" if len(done) == 1 else "s"),
        priority="interrupt",
        data={"completed": done, "screens": shots},
        artifacts_touched=shots)
