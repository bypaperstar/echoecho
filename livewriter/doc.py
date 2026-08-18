"""Document model shared (by protocol) with the page renderer.

A document is a list of lines; a line is (id, kind, atoms) where atoms are
(char, style-flags) pairs. Ops carry inline-markdown strings ("md") because
that is what LLMs write reliably; both this module and the page parse the same
tiny inline dialect (**bold**, *italic*, `code`, ~~strike~~) into atoms, so
"find" targets can be matched on plain text on either side and land on the
same characters.

The server applies every op here first — the page only ever animates ops the
server accepted, which keeps the two documents convergent by construction.
"""

import json
import re

KINDS = ("h1", "h2", "h3", "p", "li", "quote", "code", "small")

# style flags: b bold, i italic, c code, x strike
_MD_TOKEN = re.compile(r"(\*\*|\*|`|~~)")


def parse_md(md):
    """Inline markdown -> list of [char, flags] atoms. Unclosed markers are
    treated as literal text (dictation must never eat characters)."""
    atoms = []
    flags = set()
    parts = _MD_TOKEN.split(md)
    # Pre-scan for balance: a marker toggles only if it has a closing twin.
    counts = {}
    for p in parts:
        if p in ("**", "*", "`", "~~"):
            counts[p] = counts.get(p, 0) + 1
    open_now = {}
    for p in parts:
        if p in ("**", "*", "`", "~~"):
            flag = {"**": "b", "*": "i", "`": "c", "~~": "x"}[p]
            if flag in flags:
                flags.discard(flag)
                open_now[p] = open_now.get(p, 0) - 1
                counts[p] -= 2
                continue
            if counts.get(p, 0) >= 2:
                flags.add(flag)
                open_now[p] = open_now.get(p, 0) + 1
                continue
            counts[p] = counts.get(p, 0) - 1
            # unbalanced -> literal
            for ch in p:
                atoms.append([ch, _flag_str(flags)])
            continue
        for ch in p:
            atoms.append([ch, _flag_str(flags)])
    return atoms


def _flag_str(flags):
    return "".join(sorted(flags))


def atoms_to_md(atoms):
    """Atoms -> inline markdown (canonical marker order b,i,c,x)."""
    out = []
    prev = ""
    order = ["b", "i", "c", "x"]
    marker = {"b": "**", "i": "*", "c": "`", "x": "~~"}
    for ch, fl in list(atoms) + [["", ""]]:
        if fl != prev:
            # close styles not in new (reverse order), open new ones
            for f in reversed(order):
                if f in prev and f not in fl:
                    out.append(marker[f])
            for f in order:
                if f in fl and f not in prev:
                    out.append(marker[f])
            prev = fl
        out.append(ch)
    return "".join(out)


def plain(atoms):
    return "".join(a[0] for a in atoms)


class Line(object):
    __slots__ = ("id", "kind", "atoms")

    def __init__(self, lid, kind, atoms=None):
        self.id = lid
        self.kind = kind if kind in KINDS else "p"
        self.atoms = atoms or []


class OpError(ValueError):
    pass


class Doc(object):
    """Authoritative doc state. apply() validates + normalizes one op and
    returns the normalized op dict (what gets sent to the page), or raises
    OpError for ops that must be dropped."""

    def __init__(self):
        self.lines = []
        self._next = 0

    # -- queries ----------------------------------------------------------
    def line(self, lid):
        for l in self.lines:
            if l.id == lid:
                return l
        return None

    def to_markdown(self):
        blocks = []
        for l in self.lines:
            md = atoms_to_md(l.atoms)
            if l.kind == "h1":
                blocks.append(("h", "# " + md))
            elif l.kind == "h2":
                blocks.append(("h", "## " + md))
            elif l.kind == "h3":
                blocks.append(("h", "### " + md))
            elif l.kind == "li":
                # consecutive items belong to one list block
                if blocks and blocks[-1][0] == "ul":
                    blocks[-1] = ("ul", blocks[-1][1] + "\n- " + md)
                else:
                    blocks.append(("ul", "- " + md))
            elif l.kind == "quote":
                blocks.append(("q", "> " + md))
            elif l.kind == "code":
                blocks.append(("c", "```\n" + plain(l.atoms) + "\n```"))
            else:
                blocks.append(("p", md))
        out = [b[1] for b in blocks]
        return "\n\n".join(out) + ("\n" if out else "")

    def render_for_prompt(self):
        if not self.lines:
            return "(empty document)"
        return "\n".join(
            "[%d|%s] %s" % (l.id, l.kind, atoms_to_md(l.atoms)) for l in self.lines
        )

    def plain_text(self):
        return "\n".join(plain(l.atoms) for l in self.lines)

    # -- mutation ---------------------------------------------------------
    def apply(self, op):
        """Validate + apply one raw op dict from the formatter. Returns the
        normalized op to forward to the page."""
        if not isinstance(op, dict):
            raise OpError("op not a dict")
        name = op.get("op")
        if name == "chip":
            text = str(op.get("text", "")).strip()
            if not text:
                raise OpError("empty chip")
            return {"op": "chip", "text": text[:120]}
        if name == "new":
            kind = op.get("kind", "p")
            if kind not in KINDS:
                kind = "p"
            md = str(op.get("md", ""))
            lid = self._next
            self._next += 1
            line = Line(lid, kind, parse_md(md))
            after = op.get("after")
            idx = len(self.lines)
            if after is not None:
                for i, l in enumerate(self.lines):
                    if l.id == after:
                        idx = i + 1
                        break
            self.lines.insert(idx, line)
            out = {"op": "new", "id": lid, "kind": kind, "md": md}
            if idx != len(self.lines) - 1:
                out["after"] = after
            return out
        if name == "append":
            line = self.line(_as_id(op.get("line")))
            if line is None:
                raise OpError("append: no line %r" % (op.get("line"),))
            md = str(op.get("md", ""))
            if not md:
                raise OpError("append: empty md")
            line.atoms.extend(parse_md(md))
            return {"op": "append", "line": line.id, "md": md}
        if name == "replace":
            line = self.line(_as_id(op.get("line")))
            if line is None:
                raise OpError("replace: no line %r" % (op.get("line"),))
            find = str(op.get("find", ""))
            md = str(op.get("md", ""))
            if not find:
                raise OpError("replace: empty find")
            text = plain(line.atoms)
            pos = text.rfind(find)
            if pos < 0:
                # models keep writing find WITH markdown ("**5.2**") though
                # matching is on plain text — strip and retry before giving up
                stripped = plain(parse_md(find))
                if stripped != find:
                    pos = text.rfind(stripped)
                    if pos >= 0:
                        find = stripped
            if pos < 0:
                # forgiving retry: case-insensitive (both raw and stripped)
                low = text.lower()
                for cand in (find, plain(parse_md(find))):
                    p = low.rfind(cand.lower())
                    if p >= 0:
                        pos = p
                        find = text[p:p + len(cand)]
                        break
                if pos < 0:
                    raise OpError("replace: %r not in line %d" % (find[:40], line.id))
            new_atoms = parse_md(md)
            line.atoms[pos:pos + len(find)] = new_atoms
            norm = {"op": "replace", "line": line.id, "find": find, "md": md}
            if not plain(line.atoms).strip():
                # the replacement emptied the line: no dangling "- " bullets
                self.lines.remove(line)
                norm["empty_delete"] = True
            return norm
        if name == "delete":
            lid = _as_id(op.get("line"))
            for i, l in enumerate(self.lines):
                if l.id == lid:
                    self.lines.pop(i)
                    return {"op": "delete", "line": lid}
            raise OpError("delete: no line %r" % (op.get("line"),))
        raise OpError("unknown op %r" % (name,))


def _as_id(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return -1


def parse_op_line(raw):
    """One line of formatter output -> op dict or None (blank/fence/prose)."""
    s = raw.strip()
    if not s or s.startswith("```"):
        return None
    if not s.startswith("{"):
        return None
    try:
        return json.loads(s)
    except ValueError:
        # salvage trailing commas / single quotes? Keep strict: drop.
        return None
