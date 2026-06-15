"""Cover-letter text generation (M3, issues #31 + #32).

The :class:`CoverLetterGenerator` is a tiny, deterministic seam
between the slice's persistence layer and whatever LLM (or template
engine) actually produces the letter body.

Issue #31 owns the LLM-backed production implementation; this module
defines the protocol that the service depends on and ships a
:class:`StubCoverLetterGenerator` for tests, local development, and
any code path that should never hit the network.

Design notes
------------

* The protocol is intentionally small — ``generate`` is the only
  method the service needs. Swapping in a richer implementation
  (streaming, async, retries) is a non-breaking change because
  callers depend on the protocol, not a concrete class.
* The stub is deterministic: it embeds the style and the user
  comment in the body so tests and local runs produce a stable
  artefact, and the version history stays diff-friendly.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Protocol, runtime_checkable


@runtime_checkable
class CoverLetterGenerator(Protocol):
    """The contract :class:`CoverLetterService` relies on.

    Implementations take the match id plus optional style and
    user-comment hints and return the cover-letter body as a single
    string. Anything that produces text (LLM, template, human editor
    that wrote the text in advance) satisfies this protocol.
    """

    def generate(
        self,
        match_id: uuid.UUID,
        *,
        style: str | None = None,
        user_comment: str | None = None,
    ) -> str: ...


def compute_prompt_hash(
    *,
    match_id: uuid.UUID,
    style: str | None,
    user_comment: str | None,
) -> str:
    """Return the SHA-256 hex digest of the (match_id, style, comment) tuple.

    Used to stamp the ``generation_prompt_hash`` column on a draft
    so the operator can later audit which prompt actually produced
    a given text. The hash is over the public inputs only — secrets
    (API keys) must never be fed in.
    """
    payload = f"{match_id}|{style or ''}|{user_comment or ''}".encode()
    return hashlib.sha256(payload).hexdigest()


class StubCoverLetterGenerator:
    """Deterministic, network-free generator for tests and local dev.

    The body is a fixed template that embeds the inputs so successive
    regenerations are easy to tell apart in logs and snapshots. The
    protocol contract is satisfied — anything that calls
    :meth:`generate` and gets a string back is fine.
    """

    def generate(
        self,
        match_id: uuid.UUID,
        *,
        style: str | None = None,
        user_comment: str | None = None,
    ) -> str:
        lines = [
            "Dear Hiring Team,",
            "",
            f"I am writing to apply for the position referenced by match {match_id}.",
        ]
        if style is not None:
            lines.append(f"Style hint: {style}.")
        if user_comment is not None:
            lines.append(f"Notes: {user_comment}.")
        lines.extend(
            [
                "",
                "My background in software engineering and the requirements "
                "in your posting align well; I would welcome the chance to "
                "discuss how I can contribute to your team.",
                "",
                "Kind regards,",
                "Applicant",
            ]
        )
        return "\n".join(lines)


__all__ = [
    "CoverLetterGenerator",
    "StubCoverLetterGenerator",
    "compute_prompt_hash",
]
