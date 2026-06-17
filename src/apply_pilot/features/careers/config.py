"""Per-site configuration for the careers adapter (M7, issue #59).

The careers slice needs *per-site* state — each company has its own
URL, parser kind, parser id and retry policy. Putting that in a
typed value object keeps the adapter constructor narrow and lets the
operator wire dozens of sites from a single env var without writing
code.

Loading from environment
------------------------

The list of configured sites is read from a single JSON-encoded env
var (``APP_CAREERS_PAGES`` by default — wired in :mod:`apply_pilot.config`).
The format mirrors the dataclass shape::

    [
      {
        "name": "acme",
        "url": "https://acme.example/jobs",
        "kind": "rss",
        "parser_id": "rss-default"
      },
      {
        "name": "globex",
        "url": "https://globex.example/careers",
        "kind": "html",
        "parser_id": "html-default",
        "retry_count": 5,
        "retry_backoff_seconds": 1.5
      }
    ]

The dataclasses live here (not in :mod:`apply_pilot.config`) on
purpose: the careers slice is the only consumer, and pushing the
shape into the slice keeps the public config surface narrow.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from apply_pilot.features.careers.parser import CareersParserKind

#: Default number of attempts (initial call + retries) for a site.
DEFAULT_RETRY_COUNT: int = 3

#: Default base delay (seconds) for the exponential backoff between
#: attempts. The adapter multiplies this by ``2 ** (attempt - 1)``,
#: so a default of ``0.5`` means the first retry waits 0.5s, the
#: second 1.0s, the third 2.0s, etc.
DEFAULT_RETRY_BACKOFF_SECONDS: float = 0.5


@dataclass(frozen=True, slots=True)
class CareersPageSite:
    """One configured company-careers-page.

    Attributes:
        name: Short identifier used in logs, in
            :attr:`SourceAdapter.name` (``careers:<name>``), and as
            the candidate ``Vacancy.source_id`` for the source label
            in dashboards.
        url: The page to scrape. The adapter calls this URL once per
            :meth:`CareersPageSourceAdapter.search` and parses the
            response.
        kind: Payload kind; one of :class:`CareersParserKind`.
        parser_id: Stable id of the parser to use (a future
            per-company ``graphql`` or ``greenhouse-api`` parser
            would surface here). The slice currently treats this as
            a tag that flows through to the normaliser; new values
            are a non-breaking change.
        retry_count: Total number of attempts (initial + retries) on
            transient errors. Must be a positive integer.
        retry_backoff_seconds: Base delay for the exponential
            backoff. Must be a positive float.
    """

    name: str
    url: str
    kind: CareersParserKind
    parser_id: str
    retry_count: int = DEFAULT_RETRY_COUNT
    retry_backoff_seconds: float = DEFAULT_RETRY_BACKOFF_SECONDS

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("CareersPageSite.name must be a non-empty string")
        if not self.url:
            raise ValueError("CareersPageSite.url must be a non-empty string")
        if not isinstance(self.kind, CareersParserKind):
            # Catches the ``"graphql"``-as-``str`` case the tests exercise.
            raise ValueError(f"CareersPageSite.kind must be a CareersParserKind; got {self.kind!r}")
        if self.retry_count < 1:
            raise ValueError(
                f"CareersPageSite.retry_count must be a positive integer; got {self.retry_count}"
            )
        if self.retry_backoff_seconds < 0:
            # ``0`` is allowed: it means "no delay between attempts",
            # which the test suite relies on to keep retry tests fast.
            raise ValueError(
                f"CareersPageSite.retry_backoff_seconds must be non-negative; "
                f"got {self.retry_backoff_seconds}"
            )


@dataclass(frozen=True, slots=True)
class CareersPageConfig:
    """A bundle of configured :class:`CareersPageSite` entries.

    The class is the env-loadable unit; :func:`from_json` is the
    only entry point the production wiring calls. Lookup helpers
    (:meth:`find_by_name`, :meth:`__iter__`) are exposed so the
    startup code that builds the :class:`AdapterRegistry` can stream
    the configured sites without copying the list.
    """

    sites: list[CareersPageSite] = field(default_factory=list)

    def find_by_name(self, name: str) -> CareersPageSite | None:
        """Return the site with ``name``, or ``None`` if absent."""
        for site in self.sites:
            if site.name == name:
                return site
        return None

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self.sites)

    def __len__(self) -> int:
        return len(self.sites)

    @classmethod
    def from_json(cls, raw: str) -> CareersPageConfig:
        """Parse a JSON list of site entries.

        An empty / unset string is treated as "no sites" — the
        adapter registry is then empty and the careers feature is
        effectively disabled. Malformed JSON or per-entry errors
        raise :class:`ValueError` so misconfiguration surfaces at
        boot, not at the first request.
        """
        cleaned = (raw or "").strip()
        if not cleaned:
            return cls(sites=[])
        try:
            payload = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise ValueError(f"APP_CAREERS_PAGES env var is not valid json: {exc.msg}") from exc
        if not isinstance(payload, list):
            raise ValueError(
                "APP_CAREERS_PAGES env var must decode to a JSON list; "
                f"got {type(payload).__name__}"
            )
        sites = [cls._coerce_site(entry) for entry in payload]
        return cls(sites=sites)

    @staticmethod
    def _coerce_site(entry: object) -> CareersPageSite:
        """Build a :class:`CareersPageSite` from a JSON-decoded entry."""
        if not isinstance(entry, dict):
            raise ValueError(
                f"Careers page entry must be a JSON object; got {type(entry).__name__}"
            )
        try:
            kind_value = entry["kind"]
            kind = CareersParserKind(kind_value)
        except KeyError as exc:
            raise ValueError(f"Careers page entry is missing {exc.args[0]!r}") from exc
        except ValueError as exc:
            raise ValueError(f"Careers page entry has unknown kind: {kind_value!r}") from exc
        return CareersPageSite(
            name=str(entry.get("name", "")),
            url=str(entry.get("url", "")),
            kind=kind,
            parser_id=str(entry.get("parser_id", "")),
            retry_count=int(entry.get("retry_count", DEFAULT_RETRY_COUNT)),
            retry_backoff_seconds=float(
                entry.get("retry_backoff_seconds", DEFAULT_RETRY_BACKOFF_SECONDS)
            ),
        )


__all__ = [
    "CareersPageConfig",
    "CareersPageSite",
    "DEFAULT_RETRY_BACKOFF_SECONDS",
    "DEFAULT_RETRY_COUNT",
]
