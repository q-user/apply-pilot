"""Minimal parsers for company-careers-page payloads (M7, issue #59).

The slice supports two payload kinds out of the box:

* **RSS 2.0** — a feed of ``<item>`` elements with ``<title>`` and
  ``<link>`` (and optional ``<guid>``, ``<description>`` and
  ``<pubDate>``). Parsed with the stdlib
  :mod:`xml.etree.ElementTree` to avoid pulling in ``feedparser``.
* **HTML list page** — a hand-rolled regex over
  ``<a class="vacancy-link" …>…</a>`` anchors. The parser is
  intentionally tiny: the issue is about the *adapter contract* and
  *retry*, not a full HTML feature. Replacing it with a CSS-selector
  library (e.g. ``selectolax``) is a follow-up.

Both parsers return a list of raw vacancy dicts with the same shape:

.. code-block:: python

    {
        "id": "<stable id from <guid> or href>",
        "title": "<display title>",
        "url": "<absolute URL>",
        "description": "<optional>",
        "published_at": "<optional, raw RFC-822 for RSS>",
        "parser_id": "<set by the adapter, not by the parser>",
        "employer_name": "<set by the adapter, not by the parser>",
    }

The ``parser_id`` and ``employer_name`` keys are not produced here;
the adapter injects them so the normaliser can dispatch on the
``parser_id`` and stamp the right ``Vacancy.source`` /
``Vacancy.employer_name``.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from enum import StrEnum
from typing import Any
from urllib.parse import urljoin


class CareersParserKind(StrEnum):
    """Closed set of payload kinds the slice knows how to parse.

    A closed set keeps :func:`parse_payload` honest: an unknown kind
    raises immediately rather than silently producing an empty list.
    The string values are the canonical, env-stable names — they are
    the values that appear in the ``APP_CAREERS_PAGES`` env var.
    """

    RSS = "rss"
    HTML = "html"


# ---------------------------------------------------------------------------
# RSS
# ---------------------------------------------------------------------------


def parse_rss(body: str) -> list[dict[str, Any]]:
    """Parse an RSS 2.0 feed and return one dict per ``<item>``.

    Each returned dict carries at least ``id``, ``title`` and ``url``;
    ``description`` and ``published_at`` are included when present.
    Items missing a ``<link>`` are dropped — without a URL the vacancy
    cannot be deduped or opened by the candidate.

    Args:
        body: The raw RSS payload as text.

    Returns:
        A list of vacancy dicts. May be empty (e.g. an empty
        ``<channel>``).

    Raises:
        xml.etree.ElementTree.ParseError: when ``body`` is not valid
        XML. The error is intentionally not wrapped: callers that
        want a domain-level error type (e.g. ``CareersAdapterError``)
        can map :class:`xml.etree.ElementTree.ParseError` at the
        boundary.
    """
    root = ET.fromstring(body)
    items: list[dict[str, Any]] = []
    # The stdlib parser is namespace-agnostic; ``.findall('.//item')``
    # walks the tree, picking up both top-level and Atom-style
    # ``<entry>`` siblings (the latter are simply absent, which is
    # the desired behaviour).
    for item in root.iter("item"):
        link = _strip_text(item.find("link"))
        if not link:
            # An item without a link cannot be opened, applied to, or
            # deduped — drop it on the floor.
            continue
        guid = _strip_text(item.find("guid"))
        raw: dict[str, Any] = {
            "id": guid or link,
            "title": _strip_text(item.find("title")) or "",
            "url": link,
        }
        description = _strip_text(item.find("description"))
        if description:
            raw["description"] = description
        pub_date = _strip_text(item.find("pubDate"))
        if pub_date:
            raw["published_at"] = pub_date
        items.append(raw)
    return items


def _strip_text(element: ET.Element | None) -> str | None:
    """Return ``element.text`` stripped of whitespace, or ``None``.

    ``None`` is the sentinel for "missing" so callers can ``if title:``
    without a truthy-falsy collision on the empty string.
    """
    if element is None:
        return None
    text = element.text
    if text is None:
        return None
    stripped = text.strip()
    return stripped or None


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------


#: Matches ``<a … class="…vacancy-link…" … href="…" …>TITLE</a>``.
#:
#: The class regex accepts both ``class="vacancy-link"`` and
#: ``class="row vacancy-link featured"``; we only require the literal
#: token. The href and inner text are captured separately so callers
#: can re-root relative URLs against ``base_url`` without re-parsing
#: the full tag.
_VACANCY_LINK_RE = re.compile(
    r"""<a\b
        [^>]*?\bclass\s*=\s*["']([^"']*\bvacancy-link\b[^"']*)["']
        [^>]*?\bhref\s*=\s*["']([^"']+)["']
        [^>]*>
        (?P<title>.*?)
    </a>""",
    re.IGNORECASE | re.DOTALL | re.VERBOSE,
)


def parse_html(body: str, *, base_url: str) -> list[dict[str, Any]]:
    """Extract ``<a class="vacancy-link">`` entries from a list page.

    The parser is deliberately minimal — it accepts the common
    multi-class shape and ignores everything else. A real HTML
    feature belongs in a follow-up issue; the slice is about the
    *adapter contract* and *retry*.

    Args:
        body: The raw HTML payload.
        base_url: Origin used to absolutise relative ``href`` values.

    Returns:
        A list of dicts with ``id``, ``title`` and ``url`` keys.
        ``id`` is the raw ``href`` (relative or absolute) — it is the
        natural identity the site exposes; the rest of the system
        treats ``(source, id)`` as a stable key.
    """
    items: list[dict[str, Any]] = []
    for match in _VACANCY_LINK_RE.finditer(body):
        href = match.group(2).strip()
        # ``.*?`` keeps the title raw, including any inner tags
        # (``<span>`` wrappers are common in careers templates). A
        # cheap whitespace collapse is enough for an MVP.
        title = re.sub(r"\s+", " ", match.group("title")).strip()
        if not title:
            # An anchor with empty text is not a usable vacancy row.
            continue
        items.append(
            {
                "id": href,
                "title": title,
                "url": urljoin(base_url, href),
            }
        )
    return items


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def parse_payload(kind: CareersParserKind, body: str, *, base_url: str) -> list[dict[str, Any]]:
    """Dispatch to the right parser for ``kind``.

    Centralised so the adapter has a single call site and so the
    :class:`CareersParserKind` enum stays the source of truth for
    the supported payload shapes.
    """
    if kind is CareersParserKind.RSS:
        return parse_rss(body)
    if kind is CareersParserKind.HTML:
        return parse_html(body, base_url=base_url)
    raise ValueError(f"Unknown careers parser kind: {kind!r}")


__all__ = [
    "CareersParserKind",
    "parse_html",
    "parse_payload",
    "parse_rss",
]
