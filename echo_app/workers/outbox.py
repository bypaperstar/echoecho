"""outbox.apply: the ONLY path that writes the user's real documents.

Mediated-write flow (PLAN-GENERIC.md): the user's chosen folders mount
read-only into the VM, so an agent can read them but never write them.
Proposed changes are STAGED in workspace/outbox/<task>/ — full updated files
plus a MANIFEST.json mapping each staged file to the absolute original it
would replace, and a CHANGES.md summary. Nothing touches a real document
until the user says "apply it", which dispatches this tier-0 host-side
worker. It re-validates every target against the user's shared-folder
allowlist (the agent's manifest is not trusted), backs up each original with
a timestamp, then copies the staged file over atomically.

MANIFEST.json is a JSON list of entries:
    [{"staged": "lease.md",
      "target": "/Users/me/Documents/lease.md",
      "summary": "rewrote section 3"}]
`staged` is relative to the outbox task dir; `target` is the absolute
original. A target outside every ECHO_USER_DOCS root is refused, not written.
"""
import json
import os
import shutil
import time
from pathlib import Path

from echo_app import config
from echo_app.bus import TaskResult
from echo_app.services import artifacts
from echo_app.workers.base import register

MANIFEST = "MANIFEST.json"


def _has_user_docs():
    return bool(config.user_docs())


def _outbox_root(workspace):
    return Path(workspace) / config.OUTBOX_DIR


def _pick_task_dir(workspace, args):
    """Which outbox/<task>/ to apply: an explicit task_id/outbox arg, else the
    most recently modified staged batch."""
    root = _outbox_root(workspace)
    name = args.get("task_id") or args.get("outbox")
    if name:
        base = Path(str(name)).name  # basename only: no traversal via the arg
        if not base or base.startswith("."):  # "", ".", "..": reject outright
            return None
        cand = root / base
        return cand if cand.is_dir() else None
    subdirs = [p for p in root.iterdir() if p.is_dir()] if root.is_dir() else []
    return max(subdirs, key=lambda p: p.stat().st_mtime) if subdirs else None


def _within_user_docs(target):
    """True iff target's realpath is inside one of the shared-folder roots —
    the mediation gate: the agent-authored manifest cannot point outside."""
    real = os.path.realpath(str(target))
    for root in config.user_docs():
        root_real = os.path.realpath(str(root))
        if real == root_real or real.startswith(root_real + os.sep):
            return True
    return False


def _backup(target, stamp):
    """Copy an existing original aside before overwriting; returns the backup
    path or None if there was nothing to back up. Never clobbers an existing
    backup — an O_EXCL create picks a unique -N suffix — so a real original is
    never lost even if two applies land in the same one-second stamp."""
    if not target.exists():
        return None
    base = target.with_name(target.name + config.OUTBOX_BACKUP_SUFFIX
                            + "-" + stamp)
    for n in range(1000):
        bak = base if n == 0 else base.with_name(base.name + "-%d" % n)
        try:
            fd = os.open(str(bak), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            continue
        with os.fdopen(fd, "wb") as f:
            f.write(target.read_bytes())
        shutil.copystat(str(target), str(bak))
        return bak
    raise OSError("could not create a unique backup for %s" % target)


def _write_over(target, staged_path):
    """Atomic overwrite of a real document: tmp in the target dir + rename."""
    import tempfile
    fd, tmp = tempfile.mkstemp(dir=str(target.parent),
                               prefix="." + target.name + ".", suffix=".echo")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(staged_path.read_bytes())
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, str(target))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@register("outbox.apply",
          description="save the agent's staged changes over your real "
                      "documents (say 'apply it'); backs each one up first",
          arg_schema={"task_id": {"type": "string",
                                  "description": "which staged batch to apply"}},
          advertise_when=_has_user_docs)
async def run_apply(task, ctx):
    if not _has_user_docs():
        return TaskResult(
            say="There are no shared folders set up, so there's nothing I can "
                "save back. You can point Echo at a folder to enable this.",
            data={"error": "no user docs configured"})

    task_dir = _pick_task_dir(ctx.workspace, task.request.args)
    if task_dir is None:
        return TaskResult(say="I don't have any staged changes to apply.",
                          data={"error": "no staged changes"})

    rel = task_dir.relative_to(ctx.workspace).as_posix()
    manifest_raw = artifacts.read(ctx.workspace, rel + "/" + MANIFEST)
    try:
        entries = json.loads(manifest_raw) if manifest_raw else []
    except ValueError:
        return TaskResult(say="The staged changes are unreadable — I won't "
                              "touch your documents.",
                          data={"error": "bad manifest"})
    if not isinstance(entries, list) or not entries:
        return TaskResult(say="There's nothing staged to apply.",
                          data={"error": "empty manifest"})

    # Resolve + validate EVERY entry before touching a single document, and
    # collapse duplicate targets (last staged file wins) so one original is
    # never written — or backed up — twice in a batch. The manifest is
    # untrusted, so unusable/outside/failed entries are counted and reported,
    # never fatal: a bad entry can't abort the batch or hide what was applied.
    planned = {}  # realpath(target) -> (target, staged_path)
    refused, unusable = [], []
    for entry in entries:
        if not isinstance(entry, dict):
            unusable.append(entry)
            continue
        staged, target_raw = entry.get("staged"), entry.get("target")
        if not staged or not target_raw:
            unusable.append(entry)
            continue
        target = Path(str(target_raw)).expanduser()
        if not _within_user_docs(target):
            refused.append(str(target_raw))  # outside every shared folder
            continue
        try:
            staged_path = artifacts.resolve(ctx.workspace,
                                            rel + "/" + str(staged))
        except ValueError:
            refused.append(str(staged))  # staged path escapes the workspace
            continue
        if not staged_path.is_file():
            refused.append(str(staged))  # missing / a directory
            continue
        if target.exists() and not target.is_file():
            refused.append(str(target_raw))  # a dir/device: never copy over it
            continue
        planned[os.path.realpath(str(target))] = (target, staged_path)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    applied, backups, failed = [], [], []
    for target, staged_path in planned.values():
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            bak = _backup(target, stamp)  # true original, once, non-clobbering
            _write_over(target, staged_path)
        except OSError as exc:  # permission, ENOSPC, ...: skip, never abort
            failed.append("%s (%s)" % (target.name, exc))
            continue
        applied.append(target.name)
        if bak is not None:
            backups.append(bak.name)

    skipped = len(refused) + len(unusable) + len(failed)
    if not applied:
        reason = "nothing usable to apply" if not (refused or failed) \
            else "none could be applied safely"
        return TaskResult(
            say="I couldn't apply any of the staged changes (%s), so I left "
                "your documents untouched." % reason,
            data={"error": "nothing applied", "refused": refused,
                  "unusable": len(unusable), "failed": failed})
    say = "Saved %d change%s over your documents%s." % (
        len(applied), "" if len(applied) == 1 else "s",
        " (backed up first)" if backups else "")
    if skipped:
        say += " I skipped %d I couldn't apply safely." % skipped
    return TaskResult(say=say, priority="interrupt",
                      data={"applied": applied, "backups": backups,
                            "refused": refused, "failed": failed,
                            "unusable": len(unusable)})
