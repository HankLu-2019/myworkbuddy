"""Checks on the librarian's rewrite of a page before it is saved (design 01 §H8). Pure functions."""

import difflib
import re

KEEP_BLOCK = re.compile(r"<!--\s*mwb:keep\s*-->.*?<!--\s*/mwb:keep\s*-->", re.DOTALL)
HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$", re.MULTILINE)
FENCE = re.compile(r"\A\s*```[a-zA-Z]*\n(.*?)\n```\s*\Z", re.DOTALL)

MIN_SIZE_FOR_SHRINK_RULE = 500
MIN_KEPT_RATIO = 0.6
MIN_HEADINGS_KEPT = 0.7
MAX_GROWTH = 3
MAX_CHARS = 60_000
MAX_DIFF_CHARS = 20_000


def clean_output(text: str) -> str:
    """Strip one fence wrapping the whole answer and normalise the trailing newline."""
    m = FENCE.match(text)
    if m:
        text = m.group(1)
    return text.strip() + "\n"


def rejection_reason(current: str, new: str, *, existing_page: bool) -> str | None:
    """Why the rewrite must not be saved, or None if it's acceptable."""
    if len(new) > MAX_CHARS:
        return f"rewrite is {len(new)} characters (limit {MAX_CHARS})"
    kept = KEEP_BLOCK.findall(current)
    missing_keep = [b for b in kept if b not in new]
    if missing_keep:
        return f"{len(missing_keep)} protected <!-- mwb:keep --> block(s) were changed or removed"
    if not existing_page:
        return None
    size = len(current)
    if size > MIN_SIZE_FOR_SHRINK_RULE and len(new) < MIN_KEPT_RATIO * size:
        return f"rewrite would shrink the page from {size} to {len(new)} characters"
    if size > MIN_SIZE_FOR_SHRINK_RULE and len(new) > MAX_GROWTH * size:
        return f"rewrite would grow the page from {size} to {len(new)} characters"
    headings = {h.strip().lower() for h in HEADING.findall(current)}
    if headings and size > MIN_SIZE_FOR_SHRINK_RULE:  # renaming the title of a stub page is fine
        new_headings = {h.strip().lower() for h in HEADING.findall(new)}
        kept_ratio = len(headings & new_headings) / len(headings)
        if kept_ratio < MIN_HEADINGS_KEPT:
            lost = sorted(headings - new_headings)
            return f"rewrite drops {len(lost)} of {len(headings)} headings ({', '.join(lost[:5])})"
    return None


def unified_diff(current: str, new: str, path: str) -> str:
    diff = "".join(
        difflib.unified_diff(current.splitlines(keepends=True), new.splitlines(keepends=True), f"a/{path}", f"b/{path}")
    )
    return diff[:MAX_DIFF_CHARS]
