"""Use-case tests for the resumes vertical slice.

The tests drive ``ResumesService`` end-to-end with a fake in-memory
repository, so they exercise the use case (validation, extraction, persistence
of the resulting DTO) without standing up a real SQLAlchemy session.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from job_apply.features.resumes.extractors import PlainTextExtractor
from job_apply.features.resumes.schemas import UploadedFile
from job_apply.features.resumes.service import ResumesService
from job_apply.shared.errors import NotFoundError, ValidationError

# ---------------------------------------------------------------------------
# Test fakes
# ---------------------------------------------------------------------------


class InMemoryResumesRepository:
    """In-memory test double for :class:`ResumesRepository`.

    The class deliberately does **not** subclass
    :class:`job_apply.features.resumes.repository.ResumesRepository`: that
    would require standing up a SQLAlchemy session, which is exactly the
    coupling the tests are designed to avoid. Instead, this fake
    implements the same surface the service depends on (a duck-typed
    protocol) using a plain ``dict`` keyed by ``uuid.UUID``.

    Records are exposed as :class:`types.SimpleNamespace` instances so
    the service can use the same attribute-access (``record.id``) it
    uses against the real ORM objects.
    """

    def __init__(self) -> None:
        self._by_id: dict[uuid.UUID, SimpleNamespace] = {}

    def create(
        self,
        *,
        user_id: uuid.UUID,
        filename: str,
        content_type: str,
        size: int,
        raw_text: str,
        plain_text: str,
    ) -> SimpleNamespace:
        new_id = uuid.uuid4()
        record = SimpleNamespace(
            id=new_id,
            user_id=user_id,
            filename=filename,
            content_type=content_type,
            size=size,
            raw_text=raw_text,
            plain_text=plain_text,
            created_at=datetime.now(UTC),
            updated_at=None,
        )
        self._by_id[new_id] = record
        return record

    def get(self, resume_id: uuid.UUID) -> SimpleNamespace | None:
        return self._by_id.get(resume_id)

    def list_for_user(self, user_id: uuid.UUID) -> Iterable[SimpleNamespace]:
        return [r for r in self._by_id.values() if r.user_id == user_id]


@pytest.fixture
def user_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def other_user_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def service() -> ResumesService:
    return ResumesService(
        repository=InMemoryResumesRepository(),  # type: ignore[arg-type]
        extractor=PlainTextExtractor(),
    )


@pytest.fixture
def txt_upload() -> UploadedFile:
    return UploadedFile(
        filename="resume.txt",
        content_type="text/plain",
        size=len(b"John Doe\nSenior Engineer\n"),
        content=b"John Doe\nSenior Engineer\n",
    )


# ---------------------------------------------------------------------------
# upload_resume
# ---------------------------------------------------------------------------


def test_upload_txt_resume_creates_record(
    service: ResumesService, user_id: uuid.UUID, txt_upload: UploadedFile
) -> None:
    """Uploading a .txt resume stores filename, plain text, size, content type."""
    resume = service.upload_resume(user_id=user_id, upload=txt_upload)

    assert resume.id is not None
    assert resume.user_id == user_id
    assert resume.filename == "resume.txt"
    assert resume.content_type == "text/plain"
    assert resume.size == txt_upload.size
    assert resume.plain_text == "John Doe\nSenior Engineer\n"
    assert resume.raw_text == "John Doe\nSenior Engineer\n"
    assert resume.created_at is not None


def test_upload_markdown_resume_creates_record(service: ResumesService, user_id: uuid.UUID) -> None:
    """Uploading a .md resume also goes through the text extractor successfully."""
    upload = UploadedFile(
        filename="resume.md",
        content_type="text/markdown",
        size=len(b"# Mikhail\n\n- python\n- sql\n"),
        content=b"# Mikhail\n\n- python\n- sql\n",
    )

    resume = service.upload_resume(user_id=user_id, upload=upload)

    assert resume.plain_text == "# Mikhail\n\n- python\n- sql\n"
    assert resume.content_type == "text/markdown"


def test_upload_pdf_resume_raises_not_implemented(
    service: ResumesService, user_id: uuid.UUID
) -> None:
    """Uploading a PDF must raise a clear NotImplementedError from the extractor layer."""
    pdf_content = b"%PDF-1.4\n"
    upload = UploadedFile(
        filename="resume.pdf",
        content_type="application/pdf",
        size=len(pdf_content),
        content=pdf_content,
    )

    with pytest.raises(NotImplementedError):
        service.upload_resume(user_id=user_id, upload=upload)


def test_upload_oversized_file_raises_validation(
    service: ResumesService, user_id: uuid.UUID
) -> None:
    """Uploading a file larger than the configured limit must raise ValidationError."""
    big_content = b"x" * (10 * 1024 * 1024 + 1)
    upload = UploadedFile(
        filename="huge.txt",
        content_type="text/plain",
        size=len(big_content),
        content=big_content,
    )

    with pytest.raises(ValidationError):
        service.upload_resume(user_id=user_id, upload=upload)


def test_upload_unsupported_content_type_raises_validation(
    service: ResumesService, user_id: uuid.UUID
) -> None:
    """Content types outside the allow-list must be rejected as ValidationError."""
    upload = UploadedFile(
        filename="resume.bin",
        content_type="application/octet-stream",
        size=4,
        content=b"\x00\x01\x02\x03",
    )

    with pytest.raises(ValidationError):
        service.upload_resume(user_id=user_id, upload=upload)


def test_upload_inconsistent_size_raises_validation(
    service: ResumesService, user_id: uuid.UUID
) -> None:
    """If the declared size disagrees with the actual byte length, reject the upload."""
    upload = UploadedFile(
        filename="lie.txt",
        content_type="text/plain",
        size=999,
        content=b"short",
    )

    with pytest.raises(ValidationError):
        service.upload_resume(user_id=user_id, upload=upload)


# ---------------------------------------------------------------------------
# list_resumes
# ---------------------------------------------------------------------------


def test_list_resumes_returns_users_resumes(
    service: ResumesService, user_id: uuid.UUID, other_user_id: uuid.UUID
) -> None:
    """``list_resumes`` returns only the resumes owned by the requested user."""
    upload_a = UploadedFile(
        filename="a.txt",
        content_type="text/plain",
        size=4,
        content=b"body",
    )
    upload_b = UploadedFile(
        filename="b.txt",
        content_type="text/plain",
        size=4,
        content=b"more",
    )
    upload_c = UploadedFile(
        filename="c.txt",
        content_type="text/plain",
        size=4,
        content=b"mine",
    )
    service.upload_resume(user_id=user_id, upload=upload_a)
    service.upload_resume(user_id=user_id, upload=upload_b)
    service.upload_resume(user_id=other_user_id, upload=upload_c)

    mine = service.list_resumes(user_id=user_id)

    assert len(mine) == 2
    assert {r.filename for r in mine} == {"a.txt", "b.txt"}
    assert all(r.user_id == user_id for r in mine)


# ---------------------------------------------------------------------------
# get_resume
# ---------------------------------------------------------------------------


def test_get_resume_returns_record(
    service: ResumesService, user_id: uuid.UUID, txt_upload: UploadedFile
) -> None:
    """A stored resume can be retrieved by id."""
    created = service.upload_resume(user_id=user_id, upload=txt_upload)

    fetched = service.get_resume(user_id=user_id, resume_id=created.id)

    assert fetched.id == created.id
    assert fetched.plain_text == created.plain_text


def test_get_resume_wrong_user_raises_not_found(
    service: ResumesService, user_id: uuid.UUID, other_user_id: uuid.UUID, txt_upload: UploadedFile
) -> None:
    """A user can only read their own resumes; everyone else sees NotFoundError."""
    created = service.upload_resume(user_id=user_id, upload=txt_upload)

    with pytest.raises(NotFoundError):
        service.get_resume(user_id=other_user_id, resume_id=created.id)


def test_get_resume_unknown_id_raises_not_found(
    service: ResumesService, user_id: uuid.UUID
) -> None:
    """Looking up a random UUID returns NotFoundError."""
    with pytest.raises(NotFoundError):
        service.get_resume(user_id=user_id, resume_id=uuid.uuid4())
