"""Careers vertical slice (M7, issue #59).

The slice adds a *company-careers-page* source adapter to the
cross-source :class:`~job_apply.features.sources.adapter.SourceAdapter`
contract. Each configured site becomes one
:class:`CareersPageSourceAdapter` instance, registered in the
:class:`~job_apply.features.sources.adapter.AdapterRegistry` under
``SourceAdapter.name`` (e.g. ``"careers:acme"``).

Public surface
--------------

* :class:`CareersPageSite` / :class:`CareersPageConfig` — value
  objects loaded from :mod:`job_apply.config` (env-driven).
* :class:`CareersParserKind` — closed enum of supported payload kinds.
* :class:`CareersHttpClient` — the narrow Protocol the adapter depends
  on for HTTP transport.
* :class:`InMemoryCareersHttpClient` — dict-backed fake used by tests.
* :class:`HttpCareersClient` — production client backed by
  :class:`httpx.Client` (sync). Injected via constructor so tests
  swap it for the in-memory fake or :class:`httpx.MockTransport`.
* :func:`parse_rss` / :func:`parse_html` — minimal RSS (XML) and
  HTML (CSS-selector) parsers. The HTML parser is intentionally tiny
  (regex over the ``<a class="vacancy-link">`` shape) — the issue
  is about the adapter contract, not a full HTML feature.
* :class:`CareersPageSourceAdapter` — the :class:`SourceAdapter`
  implementation: fetches the site URL with retry, parses, and
  delegates normalisation to the shared
  :class:`~job_apply.features.sources.normalizer.VacancyNormalizer`.
"""

from __future__ import annotations

from job_apply.features.careers.adapter import (
    CareersPageSourceAdapter as CareersPageSourceAdapter,
)
from job_apply.features.careers.client import (
    CareersHttpClient as CareersHttpClient,
)
from job_apply.features.careers.client import (
    HttpCareersClient as HttpCareersClient,
)
from job_apply.features.careers.client import (
    InMemoryCareersHttpClient as InMemoryCareersHttpClient,
)
from job_apply.features.careers.config import (
    CareersPageConfig as CareersPageConfig,
)
from job_apply.features.careers.config import (
    CareersPageSite as CareersPageSite,
)
from job_apply.features.careers.parser import (
    CareersParserKind as CareersParserKind,
)
from job_apply.features.careers.parser import (
    parse_html as parse_html,
)
from job_apply.features.careers.parser import (
    parse_rss as parse_rss,
)

__all__ = [
    "CareersHttpClient",
    "CareersPageConfig",
    "CareersPageSite",
    "CareersPageSourceAdapter",
    "CareersParserKind",
    "HttpCareersClient",
    "InMemoryCareersHttpClient",
    "parse_html",
    "parse_rss",
]
