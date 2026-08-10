"""The stored-text rule itself.

`clean_prose` is shared by the meetings store and the orchestration store, so
the callers' tests only ever exercise it through whichever inputs those
features happen to use. A mutation run proved the gap: deleting the
blank-run rule left every caller-side test green, because none of their
sample bodies contained two blank lines in a row. The rule needs assertions
aimed at the rule.
"""

from __future__ import annotations

import pytest

from deskd.text import clean_prose


def test_horizontal_whitespace_collapses_but_lines_do_not():
    assert clean_prose("a   b\n  c\td  ", "x") == "a b\nc d"


def test_a_run_of_blank_lines_becomes_one():
    """One blank line separates paragraphs; ten express nothing more, and
    letting them through means the console renders an author's stray keystrokes
    as layout."""
    assert clean_prose("a\n\n\n\n b", "x") == "a\n\nb"


def test_leading_and_trailing_blank_lines_are_dropped():
    assert clean_prose("\n\n  a\n\nb  \n\n\n", "x") == "a\n\nb"


def test_a_single_line_value_is_left_alone():
    """Most stored prose is one line, and it must come out byte-identical to
    what the old flattening cleaner produced — that is what makes this safe to
    swap in under existing callers."""
    assert clean_prose("just  one line", "x") == "just one line"


def test_empty_means_absent():
    """Matches the line cleaners exactly, so a caller can swap one for the
    other without also changing how it handles emptiness — every orchestration
    caller passes required=False and stores the None."""
    assert clean_prose("  \n \n ", "body", required=False) is None
    assert clean_prose(None, "body", required=False) is None
    with pytest.raises(ValueError, match="body is required"):
        clean_prose("\n\n", "body")
