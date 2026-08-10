"""How stored text is normalized — the one place that rule lives.

Two kinds of field, two rules, and the difference is not cosmetic:

* **Line fields** (title, role, activity, a named dependency) are rendered
  inline — in list rows, log lines, chips. A newline in one of them breaks the
  surface that displays it, so they collapse to a single line.
* **Prose fields** (a message body, a resolution, a task detail, a note) are
  written by agents as documents: paragraphs, lists, tables. They are rendered
  as prose, so their line structure is content and must survive being stored.

Flattening a prose field destroys that structure at ingest, where no renderer
downstream can ever recover it. Both console readers hit this: a resolution
arrived as one unbroken paragraph, and a task detail whose author had written
it in sections read as a wall — while the SAME field, written through a
different path, kept its newlines. That disagreement is what this module
exists to end: a field's shape must not depend on which code path wrote it.

`clean_line` is deliberately absent. Three modules already carry their own
private copy of it; consolidating those is a separate change, and adding a
fourth caller here would only spread the duplication further.
"""

from __future__ import annotations


def clean_prose(value: str | None, label: str, *,
                required: bool = True) -> str | None:
    """Normalize whitespace WITHIN each line, keeping the lines themselves.

    Horizontal runs collapse to one space, trailing blank lines go, and a run
    of blank lines becomes a single blank line — enough to keep stored text
    tidy without deciding for the author where their paragraphs are.

    Empty means absent: raises when ``required``, else returns None. This
    mirrors the line cleaners so a caller can swap one for the other without
    also changing how it handles emptiness.
    """
    lines = [" ".join(line.split()) for line in (value or "").splitlines()]
    kept: list[str] = []
    for line in lines:
        if not line and (not kept or not kept[-1]):
            continue        # leading blank, or a second blank in a row
        kept.append(line)
    while kept and not kept[-1]:
        kept.pop()
    out = "\n".join(kept)
    if not out:
        if required:
            raise ValueError(f"{label} is required")
        return None
    return out
