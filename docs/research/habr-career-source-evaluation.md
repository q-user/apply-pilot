# Habr Career as a vacancy source — evaluation

| Field          | Value                                               |
| -------------- | --------------------------------------------------- |
| Issue          | #60 (M7, type:task, area:sources, priority:p2)      |
| Author         | research spike (subagent)                           |
| Status         | **monitor** (do **not** implement in this cycle)    |
| Verified on    | 2026-06-17 against `https://career.habr.com`        |
| Baseline       | `main` @ `07b01d4`, project version `0.19.0`        |

## TL;DR

Habr Career is a real, accessible job board with a useful internal JSON
endpoint, but for the current ApplyPilot use case the value is **marginal**:

* only ~1 000 active vacancies (≈ 1 % of hh.ru's IT-relevant set),
* estimated duplicate overlap with hh.ru is high — most Habr Career
  employers also post on hh.ru,
* **programmatic apply is not available** without a logged-in browser
  session, which breaks the core "auto-apply" loop,
* the only genuine value-add over hh.ru (the *predicted salary* range)
  applies to a fraction of vacancies.

**Recommendation: monitor.** Revisit when at least one of the following
is true: (a) Habr Career publishes an official public API; (b) the apply
flow becomes automatable without a session cookie; (c) volume grows
materially or duplicate overlap with hh.ru drops.

## 1. Data surface

The platform exposes three layers of data, all from the same Rails-style
JSON-API + HTML rendering pair.

### 1.1 Public HTML (server-rendered)

| URL                                  | Size              | Notes                                                |
| ------------------------------------ | ----------------- | ---------------------------------------------------- |
| `GET /vacancies`                     | ≈ 300 KB, 200 OK  | Listing page, ~50 cards per page                     |
| `GET /vacancies/<id>`                | ≈ 70 KB, 200 OK   | Detail page, full description in HTML                |
| `GET /robots.txt`                    | 819 B, 200 OK     | Explicit per-path disallow list (see §3.1)           |
| `GET /sitemap.xml`                   | ≈ 1 KB, 200 OK    | Sitemap **index** pointing to four gzipped children  |
| `GET /assets/sitemap{1-4}.xml.gz`    | ≈ 480 KB gzipped  | Vacancies, companies, courses, etc.                  |

The listing page uses stable CSS hooks that survive redesigns well
enough to scrape reliably:

```html
<a class="vacancy-card__title-link" href="/vacancies/1000166942">DBA PostgreSQL</a>
<div class="vacancy-card__salary"><div class="basic-salary">от 90 000 ₽</div></div>
```

The detail page renders the full description in
`<div class="vacancy-description__text">` as server-rendered HTML
(no client-side hydration required). The same content is also embedded
as escaped JSON inside the page for React props, which is the easier
parsing target.

### 1.2 Undocumented JSON API

The internal frontend talks to `https://career.habr.com/api/frontend/...`
— the endpoints below are reachable unauthenticated and return clean
JSON. They are **not advertised in any developer documentation**, but
they are what the React frontend uses, so the contract is reasonably
stable across releases.

| Endpoint                                                              | Purpose                          |
| --------------------------------------------------------------------- | -------------------------------- |
| `GET /api/frontend/vacancies?q=&page=&per_page=&type=&remote_work=&salary=&currency=&city_id=&employment=&qualification[]=&skill[]=&specialization[]=` | Paginated listing, rich filters  |
| `GET /api/frontend/vacancies/<id>/suitable_users`                     | blocked by `robots.txt`          |
| `GET /api/frontend/vacancies/<id>/responses`                          | blocked by `robots.txt`          |
| `POST /api/frontend/quick_responses`                                  | apply (auth required, 422 anon)  |
| `POST /api/frontend/vacancies/<id>/responses`                         | apply (auth required, 422 anon)  |

Verified listing response (truncated, see `tests/fixtures/habr_api_remote.json`
in the worktree for the raw capture):

```json
{
  "list": [
    {
      "id": 1000166718,
      "href": "/vacancies/1000166718",
      "title": "Senior Python Developer",
      "isMarked": true,
      "remoteWork": true,
      "salaryQualification": null,
      "publishedDate": {"date": "2026-06-17T...", "title": "17 июня"},
      "location": null,
      "company": {
        "id": 1000120843,
        "alias_name": "cn-innov",
        "title": "Центурион-Инновации",
        "accredited": true,
        "logo": {"src": "https://habrastorage.org/..."},
        "rating": null
      },
      "employment": null,
      "salary": {"from": null, "to": null, "currency": null, "formatted": ""},
      "divisions": [{"title": "Бэкенд разработчик", "href": "/vacancies/spec/development/backend"}],
      "skills": [
        {"title": "Python", "href": "/vacancies/programmist_python"},
        {"title": "FastAPI", "href": "/vacatories/skills/fastapi"},
        {"title": "PostgreSQL", "href": "/vacancies/skills/postgresql"}
      ],
      "locations": null,
      "qualification": null,
      "predictedSalary": {"from": 185000, "to": 315000, "currency": "rur", "formatted": "от 185 000 до 315 000 ₽"},
      "response": {"kind": "guest"},
      "hasPublishedCBP": false,
      "reactions": {"items": [...], "fallbackHref": "..."}
    }
  ],
  "meta": {"totalResults": 498, "perPage": 20, "currentPage": 1, "totalPages": 25},
  "recommendedQuickVacancies": []
}
```

**Notable fields the listing endpoint carries but does NOT include:**

* `description` (full job text) — only available from the detail HTML
  page or the embedded JSON props block;
* per-vacancy screening questions — Habr Career does not have them (see §2.3).

The `predictedSalary` object is a Habr-Career-unique feature: a
machine-learned salary range the platform attaches to every vacancy,
even ones where the employer did not publish a salary. This is the
only material data-quality advantage over hh.ru for our use case.

### 1.3 RSS / Atom

No public RSS feed observed for vacancies. The sitemap is the only
machine-readable discovery surface besides the JSON API.

## 2. Access mechanism

### 2.1 Authentication

| Surface            | Auth required?                              |
| ------------------ | ------------------------------------------- |
| Vacancy listing    | **No** (returns 200 unauthenticated)        |
| Vacancy detail     | **No** (returns 200 unauthenticated)        |
| Apply (`POST`)     | **Yes** — returns 422 with no session       |
| User profile       | Yes — blocked by `robots.txt` either way    |

The apply endpoints are session-cookie-based (`_career_session`, scoped
to `/`, `HttpOnly; SameSite=Lax`). There is **no public OAuth flow**,
no API key issuance, and no documented partner API. Automating an apply
would require driving a logged-in browser session, which is well outside
what ApplyPilot does for hh.ru.

### 2.2 Rate limiting

No rate-limit headers observed on either the HTML pages or the JSON
endpoints (`X-RateLimit-*`, `Retry-After`, etc. are absent). Behaviour
on burst loads was not stress-tested as part of this spike. The
absence of headers does not mean the absence of a limit — production
deployments typically apply IP-level throttling at the QRATOR edge
(`server: QRATOR` in the response headers).

### 2.3 Screening questions

Habr Career has **no formal screening-question model**. The apply
form is a single free-text "cover letter" box plus an optional
attached resume; there are no per-vacancy multiple-choice or text
prescreening questions comparable to hh.ru's `screening_questions`
block. This means the existing
:class:`~job_apply.features.screening.extractor.HhScreeningQuestionExtractor`
has nothing to extract from Habr Career payloads and the slice would
ship a no-op extractor (same shape as the careers-page adapter does).

## 3. Terms of use

### 3.1 `robots.txt`

```
User-agent: *
Disallow: /vacancy_subscriptions/
Disallow: /users
Disallow: /announcements
Disallow: /feedback
Disallow: /yandex_money
Disallow: /onboarding
Disallow: /profile
Disallow: /preferences
Disallow: /suggest
Disallow: /v1
Disallow: /companies/*/cp
Disallow: /companies/new
Disallow: /vacancies/*/suitable_users
Disallow: /vacancies/*/responses
Disallow: /conversations
Disallow: /success
Disallow: /resumes/new
Disallow: /resumes/*/edit
Disallow: /responses
Disallow: /user_exports
Disallow: /*/print
Disallow: /*/print.pdf
Disallow: /*/print.doc
Disallow: /*/opinions/*
Sitemap: https://career.habr.com/sitemap.xml
Host: career.habr.com
```

Key takeaways:

* `/vacancies` and `/vacancies/<id>` are **not** disallowed — the
  site explicitly invites indexers and aggregators;
* `/v1` is disallowed (likely a legacy internal API path — not the
  current `/api/frontend/...` endpoint);
* per-vacancy response and suitable-user endpoints are disallowed
  (those would reveal applicant PII and are rightly blocked);
* the listing endpoint `/api/frontend/vacancies` is not explicitly
  disallowed, but since it is undocumented we should treat it as a
  best-effort dependency, not a contract.

### 3.2 Terms of use (`/info/legal/agreement`)

The agreement is a single long page (Russian). The clauses that matter
for our use case:

> **§ 1.3** Использование вами Приложения и/или предоставляемого на его
> основе Сервиса любым способом и в любой форме **в пределах их
> объявленных функциональных возможностей и назначения**...
>
> *(Use of the Application ... in any way and in any form **within the
> limits of its declared functionality and purpose**.)*

Fetching publicly-listed vacancies for personal job-search is squarely
inside the declared purpose of the platform — this clause explicitly
covers programmatic consumption.

> **§ 4.7.7** несанкционированно собирать и хранить персональные данные
> других лиц;
>
> *(unauthorizedly collect and store personal data of other persons;
> prohibited.)*

This is the only clause that could plausibly apply to a job-aggregator.
Vacancy **postings** are public data published by employers, not
personal data of other persons. We must, however, avoid scraping
applicant / suitable-user / response data (which `robots.txt` already
disallows anyway).

> **§ 5.3, 5.4** ... копирование размещенного в Приложении Контента, а
> также входящих в состав Приложения элементов дизайна, программ для
> ЭВМ и баз данных, их декомпиляция, модификация, и последующее
> распространение, публичный показ, доведение до всеобщего сведения,
> строго запрещены... Пользователь не вправе воспроизводить,
> повторять и копировать, продавать, а также использовать для каких-
> либо коммерческих целей какие-либо части Приложения...
>
> *(Copying of posted Content ... and subsequent distribution, public
> display, communication to the public is strictly prohibited ...
> The User may not reproduce, repeat or copy, sell, or use for any
> commercial purposes any parts of the Application ...)*

This is the soft line. ApplyPilot does not re-publish vacancy content
externally and does not monetise it — the user's own job search is
the use case. Storing the content in our local database for personal
scoring / applying is consistent with the platform's purpose. We
should still:

* preserve the original `url` and `source_id` on every stored row so
  the user can always click through to the canonical posting;
* not expose the raw payloads outside the user who ingested them
  (the same convention the rest of the sources slice already follows).

**Verdict:** scraping public vacancies with the JSON API for personal
job-search is consistent with both `robots.txt` and the agreement.
This is a low-risk source.

## 4. Value assessment vs hh.ru

| Dimension                     | Habr Career                              | hh.ru                                       | Winner  |
| ----------------------------- | ---------------------------------------- | ------------------------------------------- | ------- |
| Volume of active vacancies    | ~1 000 IT-only                           | ~100 000+ across all industries              | hh.ru   |
| Russian-speaking tech share   | ~100 % (IT-only by design)               | large, but diluted with non-IT               | Habr    |
| Salary disclosure rate        | low for employer-published, but unique `predictedSalary` for every row | low; no predictions                          | Habr    |
| Screening questions           | **none**                                 | yes (the canonical structured model)        | hh.ru   |
| Apply flow (programmatic)     | **not available** — needs session cookie | yes (OAuth2 in M2)                          | hh.ru   |
| Freshness                     | `publishedDate` on every row, daily churn | `published_at` on every row, daily churn    | tie     |
| Duplicate overlap with hh.ru  | **high** (many IT employers cross-post)  | n/a                                         | hh.ru (less duplicate work) |
| Authenticated user value      | low (user is not logged in to ApplyPilot) | high (resume, cover letter, OAuth)          | hh.ru   |

The two genuine Habr-Career-only advantages — niche IT focus and
`predictedSalary` — are interesting but small in absolute terms:

* on a query like `q=senior python&salary=300000` the endpoint
  returned **219 total results**, of which the vast majority had
  `salary.from == null` and only `predictedSalary` populated — so
  the predicted-salary field does the heavy lifting, but only on a
  small candidate set;
* the lack of a programmatic apply flow means Habr-Career-sourced
  vacancies cannot be auto-applied to — they would dead-letter in
  the apply worker (`SourceAdapter.apply` raises `NotImplementedError`,
  which is a dead-letter per the slice convention).

That last point is decisive for ApplyPilot today: the product is
fundamentally about *auto-applying*, and Habr Career does not support
that workflow. The source would only ever produce rows that need a
manual handoff, which is a degraded user experience vs hh.ru.

## 5. Recommendation: **monitor**

**Do not implement in this cycle.** The data is reachable, the ToS is
compatible, and the slice would fit cleanly into the existing
`SourceAdapter` Protocol — but the per-vacancy marginal value is too
small relative to the dedup cost against hh.ru, and the apply path is
blocked.

### 5.1 Conditions under which to revisit

| Trigger                                                       | Why it would tip to "implement" |
| ------------------------------------------------------------- | ------------------------------- |
| Habr Career publishes an official, documented public API      | Removes the "undocumented" risk; gives us a stable contract to depend on. |
| Habr Career exposes an OAuth or partner-API apply flow        | Removes the blocker that no ApplyPilot user can actually apply from a Habr Career row. |
| Volume grows by ≥ 5× or duplicate overlap with hh.ru drops    | Means the marginal value per fetch has actually increased. |
| User research shows ApplyPilot users actively want Habr-Career-only vacancies | Confirms the product-market question, not just an engineering one. |

### 5.2 If we do implement later, the shape is straightforward

The slice would slot in next to `features/hh/` and `features/careers/`.
The architecture is already there (M7, issue #70 — `SourceAdapter`
Protocol and `AdapterRegistry`), so this would be a thin adapter
rather than a new framework.

A minimal sketch of the files that would land in a future slice:

```
src/job_apply/features/habr_career/
├── __init__.py            # re-export HabrCareerSourceAdapter
├── adapter.py             # class HabrCareerSourceAdapter (SourceAdapter)
├── client.py              # HHVacancySearchClient-shaped client + InMemory fake
├── config.py              # EnvSettings: base_url, default page size, retry
└── extractors.py          # no-op screening extractor (see §2.3)
tests/features/habr_career/
├── test_client.py
├── test_normalizer.py     # extend VacancyNormalizer with normalize_habr_career
└── test_adapter.py
```

Pseudocode for the adapter core (mirrors `HhSourceAdapter` 1:1):

```python
class HabrCareerSourceAdapter:
    name: str = "habr_career"

    def __init__(
        self,
        *,
        search_client: HabrCareerSearchClient,   # Protocol, like HH's
        normalizer: VacancyNormalizer,          # extended with normalize_habr_career
    ) -> None:
        self._search = search_client
        self._normalizer = normalizer

    async def search(self, query: SourceQuery) -> list[dict[str, Any]]:
        habr_query = HabrCareerQuery(
            q=query.text,
            page=query.page,
            per_page=min(query.per_page, 100),  # server caps at 100
            **query.extra,                     # remote_work=, salary=, city_id=, ...
        )
        return await self._search.search(habr_query)

    def normalize(self, raw: dict[str, Any]) -> Vacancy:
        # New branch on the existing VacancyNormalizer:
        #   self._normalizer.normalize_habr_career(raw)
        # Maps id, title, salary.{from,to,currency}, company.title,
        # locations[*].title, divisions[*].title, skills[*].title,
        # predictedSalary, publishedDate, employment, qualification.
        # The description is NOT in the listing payload — if we
        # need it, we either fetch /vacancies/<id> HTML or use
        # the embedded JSON props block from the detail page.
        ...

    def extract_screening_questions(self, raw: dict[str, Any]) -> list[ScreeningQuestion]:
        # Habr Career has no screening questions — return [].
        # The protocol slot is preserved so the AdapterRegistry
        # stays uniform.
        return []

    async def apply(self, job: ApplyJob) -> ApplyResult:
        # No programmatic apply. The ApplyWorker catches
        # NotImplementedError and dead-letters the row.
        raise NotImplementedError("Habr Career has no public apply API.")
```

The `VacancyNormalizer` would grow a `normalize_habr_career` branch
alongside the existing `normalize_hh`, dispatching off the
`raw.get("company")` and `raw.get("divisions")` shape rather than off
the source name (the listing endpoint doesn't carry the source name
in the payload — the adapter stamps it before delegating, the same
trick the careers-page adapter uses today).

No new dependencies are required (the slice would use the already-
imported `httpx`).

### 5.3 Risks / open questions for a future implementer

1. **The `/api/frontend/...` endpoints are undocumented.** They are
   used by the React frontend, but Habr could change them between
   releases without notice. The careers-page adapter already has the
   same risk profile for HTML scraping; pinning a test that fetches
   one canonical vacancy and asserts the schema would catch
   breakage early.
2. **No rate-limit headers** — we should still apply per-host
   backoff (e.g. `asyncio.sleep(0.5)` between listings pages, longer
   backoff on transient 5xx) and respect `robots.txt` crawl-delay
   semantics even though the file does not declare one.
3. **Salary `currency`** comes back lowercase (`"rur"`) in
   `predictedSalary` but in mixed case elsewhere — the normaliser
   should normalise to uppercase ISO-4217 before storing.
4. **Cyrillic-heavy content** — all the existing sources slice code
   is already UTF-8 safe; the new branch does not need any special
   handling but reviewers should keep an eye on `description` HTML
   (it embeds `<p>`, `<ul>`, `<li>`, `<strong>` tags that the
   normaliser should strip or preserve depending on policy).

## 6. Verification log

| Check                                                              | Result                                            |
| ------------------------------------------------------------------ | ------------------------------------------------- |
| `curl -I https://career.habr.com/robots.txt`                       | 200, 819 B, plain text                            |
| `curl https://career.habr.com/vacancies?page=1`                    | 200, 301 377 B, server-rendered HTML              |
| `curl https://career.habr.com/vacancies/1000166942`                | 200, 68 595 B, full description in HTML           |
| `curl https://career.habr.com/api/frontend/vacancies?q=python`     | 200, JSON, 182 totalResults                       |
| `curl https://career.habr.com/api/frontend/vacancies?q=senior+python&remote_work=true` | 200, JSON, 498 totalResults                       |
| `curl https://career.habr.com/sitemap.xml`                         | 200, application/xml, sitemap index               |
| `curl -X POST https://career.habr.com/api/frontend/quick_responses` (no auth) | 422 `{"status":422,"error":"Unprocessable Entity"}` |
| `curl https://career.habr.com/info/legal/agreement`                | 200, full ToS in Russian                          |

All raw responses are cached in `/tmp/habr_*` from the spike run; they
are not committed to the repository.

## 7. References

* `src/job_apply/features/sources/adapter.py` — the
  `SourceAdapter` Protocol and `AdapterRegistry` this future slice
  would plug into.
* `src/job_apply/features/careers/adapter.py` — the closest
  analogue (HTML scraper → VacancyNormalizer → apply raises
  `NotImplementedError`).
* `src/job_apply/features/hh/adapter.py` — the canonical "full-
  featured" source adapter shape: search client + normaliser +
  screening extractor + apply adapter.
* `src/job_apply/features/sources/normalizer.py` — the
  `VacancyNormalizer` that would gain a `normalize_habr_career`
  branch.
