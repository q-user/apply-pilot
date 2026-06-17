"""TDD tests for the careers vertical slice (M7, issue #59).

The slice adds a *company-careers-page* source adapter to the
cross-source :class:`~apply_pilot.features.sources.adapter.SourceAdapter`
contract. The tests cover four surfaces:

1. **Parsers** — :func:`parse_rss` (XML, stdlib only) and
   :func:`parse_html` (minimal CSS-selector over the
   ``<a class="vacancy-link">`` shape).
2. **HTTP client** — :class:`InMemoryCareersHttpClient` fake;
   :class:`HttpCareersClient` exercised via :class:`httpx.MockTransport`.
3. **Adapter** — :class:`CareersPageSourceAdapter` implements
   :class:`SourceAdapter`, retries on transient errors with the
   per-site backoff, parses with the configured parser, and
   normalises via the shared
   :class:`~apply_pilot.features.sources.normalizer.VacancyNormalizer`.
4. **Config** — :class:`CareersPageSite` / :class:`CareersPageConfig`
   validate input (positive retry count, valid kind, non-empty URL).

The tests follow the project's DI / in-memory-fakes convention: no
``Mock`` library, every collaborator is replaced with a struct.

Retry timing is kept deliberately small (zero backoff) so the suite
runs fast; the exponential growth is unit-tested separately.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field

import httpx
import pytest

from apply_pilot.features.careers.adapter import CareersPageSourceAdapter
from apply_pilot.features.careers.client import (
    HttpCareersClient,
    InMemoryCareersHttpClient,
)
from apply_pilot.features.careers.config import CareersPageConfig, CareersPageSite
from apply_pilot.features.careers.parser import (
    CareersParserKind,
    parse_html,
    parse_rss,
)
from apply_pilot.features.sources.adapter import AdapterRegistry, SourceAdapter, SourceQuery
from apply_pilot.features.sources.normalizer import VacancyNormalizer


def asyncio_run(coro):  # type: ignore[no-untyped-def]
    """Run a coroutine to completion from a sync test."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# RSS parser
# ---------------------------------------------------------------------------


class TestParseRss:
    def test_extracts_items_with_title_and_link(self) -> None:
        """Each ``<item>`` becomes a dict with ``id``, ``title``, ``url``."""
        xml = """<?xml version='1.0' encoding='UTF-8'?>
<rss version="2.0">
  <channel>
    <title>Acme careers</title>
    <item>
      <title>Senior Python Engineer</title>
      <link>https://acme.example/jobs/1</link>
      <guid>https://acme.example/jobs/1</guid>
    </item>
    <item>
      <title>Backend Developer (Go)</title>
      <link>https://acme.example/jobs/2</link>
      <guid>2</guid>
    </item>
  </channel>
</rss>"""
        items = parse_rss(xml)

        assert len(items) == 2
        assert items[0]["title"] == "Senior Python Engineer"
        assert items[0]["url"] == "https://acme.example/jobs/1"
        assert items[0]["id"] == "https://acme.example/jobs/1"
        assert items[1]["title"] == "Backend Developer (Go)"
        assert items[1]["url"] == "https://acme.example/jobs/2"
        # guid wins when present; here the second item uses a plain id.
        assert items[1]["id"] == "2"

    def test_extracts_optional_description_and_pubdate(self) -> None:
        """``description`` and ``published_at`` are passed through when present."""
        xml = """<?xml version='1.0' encoding='UTF-8'?>
<rss version="2.0">
  <channel>
    <item>
      <title>Staff Engineer</title>
      <link>https://acme.example/jobs/3</link>
      <description>Lead the platform team.</description>
      <pubDate>Mon, 01 Dec 2025 10:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>"""
        items = parse_rss(xml)

        assert items[0]["description"] == "Lead the platform team."
        assert items[0]["published_at"] == "Mon, 01 Dec 2025 10:00:00 GMT"

    def test_uses_link_as_id_when_guid_missing(self) -> None:
        """When ``<guid>`` is absent, the link itself becomes the source id."""
        xml = """<?xml version='1.0' encoding='UTF-8'?>
<rss version="2.0">
  <channel>
    <item>
      <title>Solo</title>
      <link>https://acme.example/jobs/4</link>
    </item>
  </channel>
</rss>"""
        items = parse_rss(xml)
        assert items[0]["id"] == "https://acme.example/jobs/4"

    def test_skips_items_without_link(self) -> None:
        """Items with no ``<link>`` are dropped — they cannot be deduped."""
        xml = """<?xml version='1.0' encoding='UTF-8'?>
<rss version="2.0">
  <channel>
    <item><title>No link</title></item>
    <item>
      <title>With link</title>
      <link>https://acme.example/jobs/5</link>
    </item>
  </channel>
</rss>"""
        items = parse_rss(xml)
        assert len(items) == 1
        assert items[0]["title"] == "With link"

    def test_empty_channel_returns_empty_list(self) -> None:
        """An empty ``<channel>`` is a no-op, not an error."""
        assert parse_rss("<rss><channel></channel></rss>") == []

    def test_raises_on_malformed_xml(self) -> None:
        """A non-XML body raises :class:`xml.etree.ElementTree.ParseError`."""
        import xml.etree.ElementTree as ET

        with pytest.raises(ET.ParseError):
            parse_rss("not xml at all <<<")


# ---------------------------------------------------------------------------
# HTML parser
# ---------------------------------------------------------------------------


class TestParseHtml:
    def test_extracts_vacancy_link_anchors(self) -> None:
        """Each ``<a class="vacancy-link">`` becomes a vacancy dict."""
        html = """
        <html><body>
          <a class="vacancy-link" href="/jobs/1">Senior Python Engineer</a>
          <a class="vacancy-link" href="/jobs/2">Backend Developer (Go)</a>
          <a class="other-class" href="/not-a-vacancy">Ignore me</a>
        </body></html>
        """
        items = parse_html(html, base_url="https://acme.example")

        assert len(items) == 2
        assert items[0]["title"] == "Senior Python Engineer"
        assert items[0]["url"] == "https://acme.example/jobs/1"
        assert items[0]["id"] == "/jobs/1"
        assert items[1]["title"] == "Backend Developer (Go)"
        assert items[1]["url"] == "https://acme.example/jobs/2"

    def test_keeps_absolute_urls_as_is(self) -> None:
        """Absolute ``href`` values are not re-rooted against ``base_url``."""
        html = '<a class="vacancy-link" href="https://other.example/jobs/1">Solo</a>'
        items = parse_html(html, base_url="https://acme.example")
        assert items[0]["url"] == "https://other.example/jobs/1"

    def test_handles_anchor_with_additional_classes(self) -> None:
        """``class="... vacancy-link ..."`` (multi-class) still matches."""
        html = '<a class="row vacancy-link featured" href="/jobs/9">Featured</a>'
        items = parse_html(html, base_url="https://acme.example")
        assert len(items) == 1
        assert items[0]["title"] == "Featured"

    def test_skips_empty_titles(self) -> None:
        """Anchors with empty text are dropped."""
        html = """
        <a class="vacancy-link" href="/jobs/1">Real</a>
        <a class="vacancy-link" href="/jobs/2">   </a>
        """
        items = parse_html(html, base_url="https://acme.example")
        assert len(items) == 1

    def test_empty_body_returns_empty_list(self) -> None:
        assert parse_html("<html></html>", base_url="https://x") == []


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class TestCareersPageSite:
    def test_defaults(self) -> None:
        site = CareersPageSite(
            name="acme",
            url="https://acme.example/jobs",
            kind=CareersParserKind.RSS,
            parser_id="rss-default",
        )
        assert site.retry_count == 3
        assert site.retry_backoff_seconds == 0.5

    def test_rejects_unknown_kind(self) -> None:
        with pytest.raises(ValueError, match="kind"):
            CareersPageSite(
                name="acme",
                url="https://acme.example/jobs",
                kind="graphql",  # type: ignore[arg-type]
                parser_id="rss-default",
            )

    def test_rejects_non_positive_retry_count(self) -> None:
        with pytest.raises(ValueError, match="retry_count"):
            CareersPageSite(
                name="acme",
                url="https://acme.example/jobs",
                kind=CareersParserKind.HTML,
                parser_id="html-default",
                retry_count=0,
            )

    def test_rejects_negative_backoff(self) -> None:
        with pytest.raises(ValueError, match="retry_backoff_seconds"):
            CareersPageSite(
                name="acme",
                url="https://acme.example/jobs",
                kind=CareersParserKind.HTML,
                parser_id="html-default",
                retry_backoff_seconds=-1.0,
            )

    def test_rejects_empty_name_or_url(self) -> None:
        with pytest.raises(ValueError, match="name"):
            CareersPageSite(
                name="",
                url="https://acme.example",
                kind=CareersParserKind.RSS,
                parser_id="rss-default",
            )
        with pytest.raises(ValueError, match="url"):
            CareersPageSite(
                name="acme",
                url="",
                kind=CareersParserKind.RSS,
                parser_id="rss-default",
            )


class TestCareersPageConfig:
    def test_from_json_parses_list(self) -> None:
        raw = json.dumps(
            [
                {
                    "name": "acme",
                    "url": "https://acme.example/jobs",
                    "kind": "rss",
                    "parser_id": "rss-default",
                },
                {
                    "name": "globex",
                    "url": "https://globex.example/careers",
                    "kind": "html",
                    "parser_id": "html-default",
                    "retry_count": 5,
                    "retry_backoff_seconds": 1.5,
                },
            ]
        )
        cfg = CareersPageConfig.from_json(raw)
        assert len(cfg.sites) == 2
        assert cfg.sites[0].name == "acme"
        assert cfg.sites[0].kind is CareersParserKind.RSS
        assert cfg.sites[1].retry_count == 5
        assert cfg.sites[1].retry_backoff_seconds == 1.5

    def test_from_json_empty_yields_empty_config(self) -> None:
        assert CareersPageConfig.from_json("").sites == []
        assert CareersPageConfig.from_json("[]").sites == []

    def test_from_json_raises_on_malformed_payload(self) -> None:
        with pytest.raises(ValueError, match="json"):
            CareersPageConfig.from_json("not json")

    def test_find_by_name(self) -> None:
        cfg = CareersPageConfig.from_json(
            json.dumps(
                [
                    {
                        "name": "acme",
                        "url": "https://acme.example/jobs",
                        "kind": "rss",
                        "parser_id": "rss-default",
                    }
                ]
            )
        )
        site = cfg.find_by_name("acme")
        assert site is not None
        assert site.url == "https://acme.example/jobs"
        assert cfg.find_by_name("nope") is None


# ---------------------------------------------------------------------------
# In-memory HTTP client
# ---------------------------------------------------------------------------


class TestInMemoryCareersHttpClient:
    def test_returns_preregistered_response(self) -> None:
        client = InMemoryCareersHttpClient(
            responses={"https://acme.example/jobs": httpx.Response(200, text="<rss/>")}
        )
        response = client.get("https://acme.example/jobs")
        assert response.status_code == 200
        assert response.text == "<rss/>"

    def test_records_call_count(self) -> None:
        client = InMemoryCareersHttpClient(
            responses={"https://acme.example/jobs": httpx.Response(200, text="")}
        )
        client.get("https://acme.example/jobs")
        client.get("https://acme.example/jobs")
        assert client.call_count("https://acme.example/jobs") == 2

    def test_missing_url_raises(self) -> None:
        client = InMemoryCareersHttpClient(responses={})
        with pytest.raises(KeyError):
            client.get("https://nope.example")


# ---------------------------------------------------------------------------
# HTTP client (production) — exercised via httpx.MockTransport
# ---------------------------------------------------------------------------


def _ok_response(text: str) -> httpx.Response:
    return httpx.Response(200, text=text)


class TestHttpCareersClient:
    def test_uses_injected_client(self) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return _ok_response("body")

        transport = httpx.MockTransport(handler)
        client = HttpCareersClient(httpx.Client(transport=transport))

        response = client.get("https://acme.example/jobs")
        assert response.text == "body"
        assert captured[0].url.path == "/jobs"


# ---------------------------------------------------------------------------
# Adapter — world fixture
# ---------------------------------------------------------------------------


@dataclass
class _CareersWorld:
    """Bundle of collaborators a :class:`CareersPageSourceAdapter` consumes in tests."""

    site: CareersPageSite
    http_client: InMemoryCareersHttpClient
    normalizer: VacancyNormalizer
    adapter: CareersPageSourceAdapter
    sleep_calls: list[float] = field(default_factory=list)


def _make_world(
    *,
    name: str = "acme",
    url: str = "https://acme.example/jobs",
    kind: CareersParserKind = CareersParserKind.RSS,
    parser_id: str = "rss-default",
    retry_count: int = 3,
    retry_backoff_seconds: float = 0.0,  # zero for fast tests
    responses: dict[str, httpx.Response | list[httpx.Response]] | None = None,
) -> _CareersWorld:
    site = CareersPageSite(
        name=name,
        url=url,
        kind=kind,
        parser_id=parser_id,
        retry_count=retry_count,
        retry_backoff_seconds=retry_backoff_seconds,
    )
    http_client = InMemoryCareersHttpClient(responses=responses or {})
    normalizer = VacancyNormalizer()
    adapter = CareersPageSourceAdapter(
        site=site,
        http_client=http_client,
        normalizer=normalizer,
    )
    return _CareersWorld(
        site=site,
        http_client=http_client,
        normalizer=normalizer,
        adapter=adapter,
    )


# ---------------------------------------------------------------------------
# Adapter — SourceAdapter protocol conformance
# ---------------------------------------------------------------------------


class TestAdapterConformance:
    def test_satisfies_source_adapter_protocol(self) -> None:
        """``CareersPageSourceAdapter`` is a structural :class:`SourceAdapter`."""
        world = _make_world()
        adapter: SourceAdapter = world.adapter
        assert isinstance(adapter, SourceAdapter)
        assert adapter.name == "careers:acme"

    def test_name_includes_site_prefix(self) -> None:
        """The adapter's name is ``careers:<site>`` so multiple sites coexist."""
        world = _make_world(name="globex")
        assert world.adapter.name == "careers:globex"

    def test_apply_raises_not_implemented(self) -> None:
        """Career pages have no programmatic apply — surface that explicitly."""
        world = _make_world()
        from apply_pilot.features.apply_worker.models import ApplyJob

        job = ApplyJob(  # type: ignore[call-arg]
            match_id=uuid.UUID(int=1),
            user_id=uuid.UUID(int=2),
            vacancy_id=uuid.UUID(int=3),
            idempotency_key="x",
        )
        with pytest.raises(NotImplementedError):
            asyncio_run(world.adapter.apply(job))


# ---------------------------------------------------------------------------
# Adapter — search / normalise / extract_screening_questions
# ---------------------------------------------------------------------------


_RSS_BODY = """<?xml version='1.0' encoding='UTF-8'?>
<rss version="2.0"><channel>
  <item>
    <title>Senior Python Engineer</title>
    <link>https://acme.example/jobs/1</link>
    <guid>1</guid>
  </item>
  <item>
    <title>Backend Developer (Go)</title>
    <link>https://acme.example/jobs/2</link>
    <guid>2</guid>
  </item>
</channel></rss>"""


_HTML_BODY = """
<html><body>
  <a class="vacancy-link" href="/jobs/10">Staff Engineer</a>
  <a class="vacancy-link" href="/jobs/11">Junior Engineer</a>
</body></html>
"""


class TestAdapterSearchRss:
    def test_returns_parsed_items(self) -> None:
        world = _make_world(responses={"https://acme.example/jobs": _ok_response(_RSS_BODY)})
        result = asyncio_run(world.adapter.search(SourceQuery()))

        assert len(result) == 2
        assert result[0]["title"] == "Senior Python Engineer"
        assert result[0]["url"] == "https://acme.example/jobs/1"
        assert result[0]["id"] == "1"
        # The parser is tagged so the normaliser can pick the right branch.
        assert result[0]["parser_id"] == "rss-default"
        assert result[0]["employer_name"] == "acme"

    def test_hits_only_configured_url(self) -> None:
        """The adapter ignores the :class:`SourceQuery` and uses the site URL."""
        world = _make_world(responses={"https://acme.example/jobs": _ok_response(_RSS_BODY)})
        asyncio_run(world.adapter.search(SourceQuery(text="python", area="msk")))
        assert world.http_client.call_count("https://acme.example/jobs") == 1


class TestAdapterSearchHtml:
    def test_returns_parsed_items(self) -> None:
        world = _make_world(
            kind=CareersParserKind.HTML,
            parser_id="html-default",
            responses={"https://acme.example/jobs": _ok_response(_HTML_BODY)},
        )
        result = asyncio_run(world.adapter.search(SourceQuery()))

        assert len(result) == 2
        assert result[0]["title"] == "Staff Engineer"
        assert result[0]["url"] == "https://acme.example/jobs/10"
        assert result[0]["parser_id"] == "html-default"

    def test_unknown_parser_kind_raises(self) -> None:
        """Defensive: an unsupported ``kind`` raises before hitting the network.

        The :class:`CareersPageSite` dataclass validates ``kind`` in
        :meth:`__post_init__`, so a bad kind cannot even be
        constructed. We exercise that guardrail here as the
        "defensive" check: the operator gets a clear ``ValueError``
        at wire-up time, well before the network call.
        """
        from apply_pilot.features.careers.adapter import CareersAdapterError

        with pytest.raises(ValueError, match="kind"):
            CareersPageSite(
                name="acme",
                url="https://acme.example/jobs",
                kind="graphql",  # type: ignore[arg-type] — explicitly bad
                parser_id="graphql",
            )
        # Silence the unused import warning; the import is kept to
        # make the intent obvious to a reader.
        assert CareersAdapterError is not None


class TestAdapterSearchParseErrors:
    """The adapter translates parser-internal errors into :class:`CareersAdapterError`.

    The cross-source :class:`~apply_pilot.features.sources.adapter.SourceAdapter`
    contract says ``search`` raises only :class:`CareersAdapterError`
    on the careers slice. Without the translation a 200 OK with a
    broken body would let ``xml.etree.ElementTree.ParseError`` (RSS)
    or ``re.error`` (HTML, defensive) escape — and the caller would
    need a separate ``except`` for stdlib exceptions. Issue #140.
    """

    def test_malformed_rss_translates_to_careers_adapter_error(self) -> None:
        """A 200 OK with non-XML body raises :class:`CareersAdapterError`.

        ``parse_rss`` delegates to :func:`xml.etree.ElementTree.fromstring`,
        which raises :class:`xml.etree.ElementTree.ParseError` on a
        broken body. The adapter must catch it and re-raise as
        :class:`CareersAdapterError` so callers do not have to know
        about the stdlib type.
        """
        import xml.etree.ElementTree as ET

        from apply_pilot.features.careers.adapter import CareersAdapterError

        world = _make_world(
            responses={"https://acme.example/jobs": _ok_response("<invalid xml <<<")},
        )
        with pytest.raises(CareersAdapterError) as excinfo:
            asyncio_run(world.adapter.search(SourceQuery()))

        # The error message must point at the root cause ("invalid RSS XML")
        # so operators can tell apart transport, 4xx and parse failures.
        assert "invalid RSS XML" in str(excinfo.value)
        # And the stdlib exception must be chained (not silently swallowed)
        # so debuggers can still see the original ``ParseError``.
        assert isinstance(excinfo.value.__cause__, ET.ParseError)

    def test_empty_rss_body_translates_to_careers_adapter_error(self) -> None:
        """An empty body is not valid XML and must surface as a domain error."""
        from apply_pilot.features.careers.adapter import CareersAdapterError

        world = _make_world(
            responses={"https://acme.example/jobs": _ok_response("")},
        )
        with pytest.raises(CareersAdapterError, match="invalid RSS XML"):
            asyncio_run(world.adapter.search(SourceQuery()))

    def test_html_re_error_translates_to_careers_adapter_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An ``re.error`` from the HTML parser is caught at the boundary.

        The current regex is constant and cannot fail on its own, but
        a future regex change (or a malformed pattern supplied by a
        plugin) could surface :class:`re.error`. The adapter must
        translate it the same way it translates :class:`ParseError`
        for RSS — the caller should only ever see
        :class:`CareersAdapterError`.
        """
        import re as _re

        from apply_pilot.features.careers import parser as careers_parser
        from apply_pilot.features.careers.adapter import CareersAdapterError

        class _ExplodingPattern:
            """Stand-in for the compiled regex that raises on ``finditer``."""

            def finditer(self, _body: str):  # type: ignore[no-untyped-def]
                raise _re.error("simulated malformed pattern")

        # ``re.Pattern`` is a built-in C type whose methods are
        # read-only, so we swap the *object* the parser module holds
        # rather than patching its method.
        monkeypatch.setattr(careers_parser, "_VACANCY_LINK_RE", _ExplodingPattern())

        world = _make_world(
            kind=CareersParserKind.HTML,
            parser_id="html-default",
            responses={"https://acme.example/jobs": _ok_response(_HTML_BODY)},
        )
        with pytest.raises(CareersAdapterError) as excinfo:
            asyncio_run(world.adapter.search(SourceQuery()))

        assert "invalid HTML markup" in str(excinfo.value)
        # The stdlib ``re.error`` is preserved on ``__cause__`` so it
        # is not lost for debugging.
        assert isinstance(excinfo.value.__cause__, _re.error)


# ---------------------------------------------------------------------------
# Adapter — normalise
# ---------------------------------------------------------------------------


class TestAdapterNormalize:
    def test_maps_rss_raw_to_vacancy(self) -> None:
        world = _make_world()
        raw = {
            "id": "42",
            "title": "Staff Engineer",
            "url": "https://acme.example/jobs/42",
            "description": "Lead the platform team.",
            "employer_name": "acme",
        }
        vacancy = world.adapter.normalize(raw)

        assert vacancy.source == "careers:acme"
        assert vacancy.source_id == "42"
        assert vacancy.title == "Staff Engineer"
        assert vacancy.url == "https://acme.example/jobs/42"
        assert vacancy.description == "Lead the platform team."
        assert vacancy.employer_name == "acme"
        # Content hash is derived from the canonical fields, not the raw blob.
        assert vacancy.content_hash is not None
        assert len(vacancy.content_hash) == 64  # type: ignore[arg-type]
        assert vacancy.raw_data == raw

    def test_returns_no_screening_questions(self) -> None:
        """Career pages do not expose structured screening questions."""
        world = _make_world()
        assert world.adapter.extract_screening_questions({}) == []


# ---------------------------------------------------------------------------
# Adapter — retry behaviour
# ---------------------------------------------------------------------------


class TestAdapterRetry:
    def test_retries_on_5xx_then_succeeds(self) -> None:
        """Two 503s, then a 200 — the adapter returns the success body."""
        url = "https://acme.example/jobs"
        responses = {
            url: [
                httpx.Response(503, text="busy"),
                httpx.Response(503, text="busy"),
                _ok_response(_RSS_BODY),
            ]
        }
        world = _make_world(
            retry_count=3,
            retry_backoff_seconds=0.0,
            responses=responses,
        )
        result = asyncio_run(world.adapter.search(SourceQuery()))

        assert len(result) == 2
        assert world.http_client.call_count(url) == 3

    def test_retries_on_transport_error(self) -> None:
        """A transport error counts as transient; the adapter retries."""
        from apply_pilot.features.careers.client import CareersTransportError

        url = "https://acme.example/jobs"
        client = InMemoryCareersHttpClient(responses={})
        client.programmatic_errors = {
            url: [
                CareersTransportError("connection reset"),
                CareersTransportError("timeout"),
            ]
        }
        # Replay a successful response after the two errors.
        client.responses[url] = _ok_response(_RSS_BODY)
        site = CareersPageSite(
            name="acme",
            url=url,
            kind=CareersParserKind.RSS,
            parser_id="rss-default",
            retry_count=3,
            retry_backoff_seconds=0.0,
        )
        adapter = CareersPageSourceAdapter(
            site=site, http_client=client, normalizer=VacancyNormalizer()
        )

        result = asyncio_run(adapter.search(SourceQuery()))
        assert len(result) == 2
        assert client.call_count(url) == 3  # 2 errors + 1 success

    def test_exhausts_retry_budget(self) -> None:
        """When every attempt is 5xx, the adapter raises after ``retry_count``."""
        from apply_pilot.features.careers.adapter import CareersAdapterError

        url = "https://acme.example/jobs"
        world = _make_world(
            retry_count=3,
            retry_backoff_seconds=0.0,
            responses={url: httpx.Response(500, text="boom")},
        )
        with pytest.raises(CareersAdapterError, match="500"):
            asyncio_run(world.adapter.search(SourceQuery()))
        # ``retry_count`` attempts in total (the initial call counts as the
        # first attempt; the value is the *total* number of attempts).
        assert world.http_client.call_count(url) == 3

    def test_does_not_retry_on_4xx(self) -> None:
        """A 4xx response is permanent: no retry, the error propagates."""
        from apply_pilot.features.careers.adapter import CareersAdapterError

        url = "https://acme.example/jobs"
        world = _make_world(
            retry_count=5,
            retry_backoff_seconds=0.0,
            responses={url: httpx.Response(404, text="missing")},
        )
        with pytest.raises(CareersAdapterError, match="404"):
            asyncio_run(world.adapter.search(SourceQuery()))
        assert world.http_client.call_count(url) == 1


# ---------------------------------------------------------------------------
# Adapter — registry integration
# ---------------------------------------------------------------------------


class TestAdapterRegistryIntegration:
    def test_adapter_registers_under_careers_name(self) -> None:
        world = _make_world(name="acme")
        registry = AdapterRegistry()
        registry.register(world.adapter)
        assert registry.get("careers:acme") is world.adapter
        assert "careers:acme" in registry.list()

    def test_multiple_sites_registered(self) -> None:
        world_a = _make_world(name="acme")
        world_b = _make_world(name="globex")
        registry = AdapterRegistry()
        registry.register(world_a.adapter)
        registry.register(world_b.adapter)
        assert sorted(registry.list()) == ["careers:acme", "careers:globex"]
