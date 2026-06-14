# Vertical Slice Architecture — Conventions

This document is the contract that every vertical slice in `job_apply` must
follow. It is intentionally opinionated: the goal is to make each slice
**self-contained**, **independently testable**, and **easy to delete or
replace** without rippling through the rest of the codebase.

If a slice needs to break one of these rules, the answer is almost always
to revisit the slice boundary — not the rule.

---

## 1. Core principles

1. **Slice by business capability, not by technical layer.** Code that
   changes together lives together. There is no `services/` or
   `repositories/` folder that gathers cross-slice code.
2. **Each slice owns its data model, persistence, DTOs, and business
   logic.** Cross-slice imports go through the slice's public contract
   (re-exports), not by reaching into private modules.
3. **Dependencies point inward.** Slices depend on `db`, `config`,
   `runtime`, and `shared`. Slices never depend on each other directly.
   If two slices need to coordinate, an application-level orchestrator
   (e.g. a FastAPI route, a worker tick, a CLI command) wires them.
4. **Shared code is a last resort.** Anything in `src/job_apply/shared/`
   must be used by **more than one slice** and must be **stable**.
   Adding to `shared/` is a public-API decision: changing it later costs
   more than duplicating it once.
5. **Test slices, not layers.** A slice test exercises the public
   behaviour of the slice (use case / handler / service) with
   in-memory or fake dependencies. It does not poke at private helpers
   in isolation.

---

## 2. Repository layout

```text
src/job_apply/
  app.py                # FastAPI factory and entry point (transport)
  config.py             # environment-driven settings dataclasses
  db.py                 # SQLAlchemy engine/session/base primitives
  core.py               # trivial smoke-check helpers
  runtime/              # long-running process + Redis factory
  shared/               # cross-slice primitives (see §7)
    errors.py           # DomainError hierarchy
    logging.py          # configure_logging()
    schemas.py          # IdentifiedSchema, TimestampedSchema
  features/
    <slice_name>/
      __init__.py       # public re-exports
      models.py         # ORM models
      repositories.py   # persistence gateway
      schemas.py        # DTOs and input/output shapes
      service.py        # use cases / business logic

tests/
  features/<slice_name>/
    test_<slice>_service.py     # use-case tests with fakes/in-memory deps
  test_*.py                     # cross-cutting tests (db, app, shared, ...)
```

### Rules

* A slice is a single Python package under `features/<slice_name>/`. The
  name is a stable, kebab-case identifier that appears in route paths,
  queue names, and log fields.
* The slice's `__init__.py` re-exports its **public contract** (DTOs,
  service classes, repository protocols). Internal helpers stay private.
* Slices **must not** import from another slice's private modules. The
  only allowed cross-slice import is the public re-export.
* The slice's tests live under `tests/features/<slice_name>/` and
  mirror the slice's module names.

---

## 3. Slice anatomy

A typical slice is four small files, each with a focused responsibility.

### `models.py` — ORM models

* Inherit from `job_apply.db.Base`.
* One class per database table. Use `Mapped[...]` / `mapped_column` from
  SQLAlchemy 2.x; do not introduce legacy `Column` declarations.
* Prefer narrow types (`String(64)`, `Enum[...]`) over free-form
  `String(255)`.
* Add a server-side `created_at` / `updated_at` if the entity is
  persisted; mirror the values on the DTO via `TimestampedSchema`.

### `repositories.py` — persistence gateway

* One class per aggregate root. Constructor takes the SQLAlchemy
  `Session` (or a session factory for read-only paths).
* Methods translate domain operations into ORM operations
  (`create`, `get`, `list`, `update`, `delete`, `get_by_*`).
* Return ORM objects, **not** DTOs. The service layer maps ORM → DTO.
* Never import from `fastapi`, `pydantic`, or any transport layer.

### `schemas.py` — DTOs

* One Pydantic `BaseModel` per transfer shape (`CreateXInput`,
  `UpdateXInput`, `XDTO`, `XListResponse`).
* Use `IdentifiedSchema` for any DTO that carries an `id` and
  `TimestampedSchema` for any DTO that mirrors persisted timestamps.
* Use `model_config = ConfigDict(extra="forbid", frozen=False)` for
  mutable input DTOs and `frozen=True` for value objects that must not
  change after construction.
* DTOs are the **only** public shape of a slice. Repositories and
  services may return ORM objects internally; they must convert to
  DTOs before handing anything to a transport layer.

### `service.py` — use cases

* One class per aggregate (`<Slice>Service`). Constructor takes the
  repository and any other collaborators (gateways, clock, idempotency
  store) **by injection**.
* Methods accept DTOs (or plain dataclasses) and return DTOs. The
  service is the only place where ORM ↔ DTO translation happens.
* Raise `DomainError` subclasses (see §6) for business-rule failures.
* Never read environment variables directly. Accept configuration
  through the constructor.

---

## 4. Dependency injection

* All cross-cutting collaborators (DB session, Redis, HTTP clients,
  clocks, idempotency stores) are passed in through constructors.
* Slices do not import the module-level singletons from
  `job_apply.db` or `job_apply.runtime` at use-case time. Production
  wiring (FastAPI dependency, worker constructor) is the only place
  that touches them.
* For tests, prefer fakes: an in-memory `dict` stands in for a
  repository, `fakeredis` for Redis, a stub gateway for HTTP. Avoid
  `unittest.mock.MagicMock` for state-bearing collaborators; it makes
  tests brittle and obscures the slice's actual contract.

```python
# tests/features/orders/test_order_service.py
def test_create_order_returns_created_entity() -> None:
    fake_repo = InMemoryOrdersRepository()
    service = OrdersService(fake_repo)

    result = service.create_order(CreateOrderInput(customer_name="Mikhail"))

    assert result.customer_name == "Mikhail"
    assert fake_repo.by_id(result.id) is not None
```

---

## 5. Testing

* **TDD first.** Write the failing use-case test, then the code that
  makes it pass, then refactor.
* **Test behaviour, not layers.** A test that exercises
  `Repository.create` in isolation is rarely worth keeping; a test that
  exercises `Service.create_order` end-to-end (with a fake repository)
  is.
* **Fakes over mocks.** In-memory repositories, stub gateways,
  `fakeredis`. Use `unittest.mock` only at external boundaries where
  no fake is practical (e.g. simulating a 503 from a third-party HTTP
  API).
* **Pure unit tests by default.** A slice test must not require a real
  database, Redis, network, or filesystem. Reach for the integration
  suite (separate `tests/integration/`) only when verifying the
  *integration* is the point.
* **One assertion idea per test.** It is fine to have several asserts
  as long as they all assert the same behaviour.

### Test layout

```text
tests/
  features/<slice>/
    test_<slice>_service.py   # use cases
    test_<slice>_repository.py # only if the repository has non-trivial logic
  test_*.py                    # cross-cutting (db, app, shared, runtime)
```

---

## 6. Errors

Slices raise `DomainError` subclasses from `job_apply.shared.errors`.
The transport layer (FastAPI handler, CLI, worker) is responsible for
translating `code` / `message` into a response.

| Class             | `code`             | When to use                                      |
| ----------------- | ------------------ | ------------------------------------------------ |
| `DomainError`     | `domain_error`     | Base class; raise subclasses, not this directly. |
| `NotFoundError`   | `not_found`        | The referenced entity does not exist.            |
| `ValidationError` | `validation_error` | A business rule was violated by valid input.     |
| `ConflictError`   | `conflict`         | Uniqueness, optimistic lock, or state conflict.  |

Add a new subclass only when the **machine-readable code** needs to be
distinct. Do not invent a subclass for every message variant.

```python
raise NotFoundError.for_entity("Order", order_id)
```

---

## 7. The `shared/` package

`shared/` exists for primitives that are **used by more than one slice**
and that are **stable enough to be a public API**. The bar is high.

### Currently in `shared/`

* `errors.py` — `DomainError` and a small set of subclasses.
* `logging.py` — `configure_logging()` (idempotent root logger config).
* `schemas.py` — `IdentifiedSchema`, `TimestampedSchema`, and a
  `to_dict()` helper.

### Rules for adding to `shared/`

* The new primitive is already used (or about to be used) by two or
  more slices.
* The new primitive is **small** and **opinion-light**. Generic
  abstractions (a `BaseRepository`, a `BaseService`) do **not** belong
  here — they belong in the slice until at least two slices need the
  same shape.
* Adding a new symbol to `shared/` is a SemVer-minor event; removing or
  renaming one is SemVer-major.

If a candidate is **not yet** used by two slices, leave it inside the
slice that needs it. The cost of moving code from a slice to `shared/`
is small; the cost of removing a premature abstraction is not.

---

## 8. Logging

* Use `configure_logging()` from `job_apply.shared.logging` at process
  start. It is safe to call from the FastAPI lifespan, from a CLI
  `main()`, and from a worker's `BaseProcess.run()`.
* It honours `APP_LOG_LEVEL` and `APP_LOG_JSON`. JSON output is the
  default for production; human-readable text is opt-in for local
  development.
* Loggers are named after the module path: `job_apply.features.orders.service`.
  Do not log from inside the repository except for debug-level
  diagnostics; the service is the right place for narrative logs.

---

## 9. Configuration

* Slices never read `os.environ` directly. Accept settings through the
  constructor; the entry point wires environment variables to
  dataclasses from `job_apply.config`.
* New settings dataclasses follow the same shape as the existing
  ones: a `@dataclass(frozen=True)` value type and a `get_*_settings()`
  builder that reads the environment.
* Environment variable names are prefixed with `APP_` (e.g.
  `APP_DATABASE_URL`, `APP_LOG_LEVEL`). This avoids collisions with
  third-party tooling and makes the project's surface obvious in a
  process listing.

---

## 10. Database migrations

* Schema changes are part of the slice that introduces them. When a
  slice adds or alters a table, it also adds an Alembic revision.
* Generate revisions with `uv run alembic revision --autogenerate -m
  "<slice>: <change>"` and review the result — autogenerate is a
  starting point, not a final answer.
* Apply migrations in CI before the integration tests run.
* Never hand-edit a revision that has already been merged to `main`;
  add a follow-up revision instead.

---

## 11. Adding a new vertical slice — checklist

1. Create the package: `src/job_apply/features/<slice>/` with an empty
   `__init__.py`.
2. Add ORM models in `models.py` (or a more descriptive name for the
   aggregate).
3. Add the persistence gateway in `repositories.py`.
4. Define DTOs in `schemas.py`. Re-use `IdentifiedSchema` /
   `TimestampedSchema` where they fit.
5. Implement the service in `service.py`. Raise `DomainError`
   subclasses; do not raise `HTTPException` from inside a slice.
6. Write tests first: a failing `test_<slice>_service.py` that drives
   the use case end-to-end with an in-memory repository.
7. Wire the slice into the appropriate transport: a FastAPI router, a
   worker tick, a CLI command. Keep the wiring small and explicit.
8. If the slice added a table, generate an Alembic revision.
9. Run `uv run pytest -n auto`, `uv run ruff check .`, `uv run ruff
   format .`, and `uv run ty check src/`. All must be clean.
10. Document any new public contract changes in `README.md` (append
    only) and the changelog.

---

## 12. When to deviate

These rules exist to keep slices cheap to add and safe to delete. They
are not a religion. The only time to deviate is when following the
rule would make the code **less** clear, **less** testable, or **less**
isolated than ignoring it. When that happens:

* Open a discussion issue describing the constraint and the proposed
  deviation.
* Update this document in the same change so the next slice author
  sees the precedent.

---

## 13. Worked example — the `users` auth slice (M1, issue #11)

The `users` slice is the canonical reference for how a vertical slice
that *does* talk to the database, exposes an HTTP surface, and owns a
custom security primitive is laid out.

### Layout

```text
src/job_apply/features/users/
  __init__.py     # public re-exports (User, AuthService, schemas, security)
  models.py       # User ORM model + a cross-dialect GUID TypeDecorator
  schemas.py      # Pydantic v2 DTOs (UserCreate, UserRead, AuthToken, ...)
  repository.py   # InMemoryUsersRepository + SqlAlchemyUsersRepository
  service.py      # AuthService (register, login, resolve_user_id, logout)
  security.py     # password hashing + in-memory token store
  api.py          # FastAPI router (/auth/register, /auth/login, ...)

alembic/versions/
  <hash>_add_users_table.py  # hand-written migration (no autogenerate)

tests/features/users/
  test_security.py          # hashing + token primitives (pure)
  test_auth_service.py      # use cases with InMemoryUsersRepository (pure)
  test_users_repository.py  # SqlAlchemyUsersRepository against sqlite (integration)
  test_auth_api.py          # /auth/* endpoints with FastAPI TestClient (integration)
```

### Conventions worth copying

* **Own your model.** Other slices import :class:`User` from
  ``job_apply.features.users``. The field set — `id: UUID`,
  `email: str` (unique), `hashed_password: str`, `is_active: bool`,
  `created_at` / `updated_at` — is the stable public surface. New
  fields are additive; renaming or removing anything is a SemVer-major
  event.
* **DTOs mirror the model.** ``UserRead`` re-uses
  :class:`TimestampedSchema` from ``shared/schemas.py`` for the
  timestamp pair, but does *not* re-use :class:`IdentifiedSchema`
  because the auth slice uses ``UUID`` ids and the shared base assumes
  ``int``. The slice's ``__init__.py`` re-exports the public DTOs so
  callers do not have to know the internal file layout.
* **Repository contract is a Protocol, not an ABC.** Both
  ``InMemoryUsersRepository`` and ``SqlAlchemyUsersRepository`` satisfy
  :class:`UsersRepository`. ``AuthService`` accepts the protocol, so
  tests pass the in-memory fake and production passes the SQLAlchemy
  implementation. No inheritance, no shared base class.
* **Errors are domain-first.** ``AuthService`` raises
  :class:`DuplicateEmailError` (a :class:`ConflictError` subclass) and
  :class:`AuthenticationError` (a plain ``Exception`` so the HTTP layer
  always returns 401). The FastAPI router translates them to 409 / 401
  with a stable ``{"code", "message"}`` body.
* **Security primitives live in the slice.** Hashing and the token
  store are in ``security.py`` because right now only the auth slice
  needs them. If a second slice ever does, that is the moment to
  consider moving them to ``shared/`` — not before.
* **Migration is hand-written, not autogenerated.** The
  ``users`` migration mirrors the ORM model by hand. The dev DB is
  sqlite and the production target is Postgres; autogenerating against
  sqlite would emit a sqlite-flavoured column type that does not match
  the production schema.
