"""Whether a Profesia posting has been closed by its employer.

A closed posting keeps answering 200 at the same URL with the title still
shown; the only sign is a fixed notice under the title. Checked on 9 real
offers (6 closed, 3 open) on 2026-10-26: the phrase matched exactly the closed
ones. Nothing looser counts, because the cost is asymmetric: an open posting
taken for closed moves a live application to the archive.
"""

import re
from typing import Literal

CLOSED_NOTICE = "ukončila zverejnenie ponuky"
APPLY_BUTTON = "Reagovať na ponuku"
"""Shown on open postings only. Required for "open" so that a challenge page
with a title of its own is reported as unchecked rather than as open."""

PostingState = Literal["closed", "open", "unknown"]


def is_closed(html: str) -> bool:
    """True only when the page carries Profesia's closing notice."""
    return CLOSED_NOTICE in re.sub(r"\s+", " ", html)


def posting_state(status: int, html: str) -> PostingState:
    """What one fetch says about a posting.

    Anything that is not a posting page — an error, a captcha, a maintenance
    page, all of which can answer 200 — is "unknown": it says nothing about the
    posting and must never count as closed.
    """
    if status != 200 or "<h1" not in html.lower():
        return "unknown"
    if is_closed(html):
        return "closed"
    return "open" if APPLY_BUTTON in html else "unknown"
