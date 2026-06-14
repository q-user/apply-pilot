"""Resumes use cases.

The service is the only module that knows about both the repository and
the text extractor. It is the right place for cross-cutting concerns:

* Enforce the file-size limit and the content-type allow-list.
* Sanity-check the declared size against the actual byte length.
* Translate :class:`ExtractionNotSupportedError` into a domain-level
  ``NotImplementedError`` (so the API can render a 501 with a stable
  code) and ``ValidationError`` for every other input problem.
* Map ORM rows to DTOs.

The constructor accepts dependencies by injection so tests can swap the
repository for an in-memory fake and the extractor for a stub.
"""

from __future__ import annotations

import uuid

from job_apply.config import ResumeSettings, get_resume_settings
from job_apply.features.resumes.extractors import (
    ExtractionNotSupportedError,
    TextExtractor,
)
from job_apply.features.resumes.models import Resume
from job_apply.features.resumes.repository import ResumesRepository
from job_apply.features.resumes.schemas import ResumeDTO, UploadedFile
from job_apply.shared.errors import NotFoundError, ValidationError

# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class ResumesService:
    """Use cases for the resumes slice."""

    #: MIME types the API will accept. PDF and DOCX pass the allow-list
    #: at the service layer but are rejected by the extractor with a
    #: ``NotImplementedError`` until ``pypdf`` / ``python-docx`` land.
    ALLOWED_CONTENT_TYPES: tuple[str, ...] = (
        "text/plain",
        "text/markdown",
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )

    def __init__(
        self,
        repository: ResumesRepository,
        extractor: TextExtractor,
        settings: ResumeSettings | None = None,
    ) -> None:
        self._repository = repository
        self._extractor = extractor
        self._settings = settings or get_resume_settings()

    # ------------------------------------------------------------------
    # Public use cases
    # ------------------------------------------------------------------

    def upload_resume(self, *, user_id: uuid.UUID, upload: UploadedFile) -> ResumeDTO:
        """Validate, extract, and persist a freshly uploaded resume.

        Returns a fully-populated :class:`ResumeDTO`. The raw and plain
        text are stored identically for ``.txt`` / ``.md`` uploads; future
        extractors can populate them differently without changing the
        contract.
        """
        self._validate_upload(upload)
        plain_text = self._extract_text(upload)

        record = self._repository.create(
            user_id=user_id,
            filename=upload.filename,
            content_type=upload.content_type,
            size=upload.size,
            raw_text=plain_text,
            plain_text=plain_text,
        )
        return self._to_dto(record)

    def get_resume(self, *, user_id: uuid.UUID, resume_id: uuid.UUID) -> ResumeDTO:
        """Return a single resume if it exists **and** belongs to ``user_id``.

        A resume that exists but belongs to another user is treated as
        ``NotFoundError`` to avoid leaking the existence of someone else's
        resource.
        """
        record = self._repository.get(resume_id)
        if record is None or record.user_id != user_id:
            raise NotFoundError.for_entity("Resume", resume_id)
        return self._to_dto(record)

    def list_resumes(self, *, user_id: uuid.UUID) -> list[ResumeDTO]:
        """Return every resume owned by ``user_id``, newest first."""
        records = self._repository.list_for_user(user_id)
        return [self._to_dto(record) for record in records]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _validate_upload(self, upload: UploadedFile) -> None:
        """Reject the upload with :class:`ValidationError` on any rule violation."""
        if not upload.filename:
            raise ValidationError("Uploaded file is missing a filename.")
        if upload.size < 0:
            raise ValidationError(f"Uploaded file size must be non-negative; got {upload.size}.")
        if upload.size > self._settings.max_file_size_bytes:
            limit_mb = self._settings.max_file_size_bytes // (1024 * 1024)
            raise ValidationError(
                f"Uploaded file is {upload.size} bytes, which exceeds the {limit_mb} MB limit."
            )
        if upload.content_type not in self.ALLOWED_CONTENT_TYPES:
            raise ValidationError(
                f"Content type {upload.content_type!r} is not supported. "
                f"Allowed types: {self.ALLOWED_CONTENT_TYPES}."
            )
        if len(upload.content) != upload.size:
            raise ValidationError(
                f"Declared size {upload.size} does not match actual byte length "
                f"{len(upload.content)}."
            )

    def _extract_text(self, upload: UploadedFile) -> str:
        """Run the configured extractor, wrapping unsupported content in NotImplementedError."""
        try:
            return self._extractor.extract(
                upload.content, content_type=upload.content_type, filename=upload.filename
            )
        except ExtractionNotSupportedError as exc:
            # Surface as a generic NotImplementedError so the FastAPI handler
            # can map it to a 501 with a stable code; the original detail
            # is preserved on the chained exception.
            raise NotImplementedError(str(exc)) from exc

    @staticmethod
    def _to_dto(record: Resume) -> ResumeDTO:
        """Map an ORM row to its public DTO."""
        return ResumeDTO(
            id=record.id,
            user_id=record.user_id,
            filename=record.filename,
            content_type=record.content_type,
            size=record.size,
            raw_text=record.raw_text,
            plain_text=record.plain_text,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )


__all__ = ["ResumesService"]
