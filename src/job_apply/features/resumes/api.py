"""Resumes HTTP API.

The router exposes three endpoints under ``/resumes``:

* ``POST   /resumes``   — upload a single file
* ``GET    /resumes``   — list the current user's resumes
* ``GET    /resumes/{id}`` — fetch one resume by id

Multipart parsing
-----------------

The project does not depend on ``python-multipart`` (which is what
FastAPI's :class:`UploadFile` needs at request time). To keep the runtime
dep surface minimal we parse ``multipart/form-data`` ourselves with the
stdlib :mod:`email` package. The helper :func:`_parse_multipart_upload`
is deliberately small and self-contained.

Authentication
--------------

The ``StubAuthDep`` below returns a hard-coded ``UUID`` for now. The auth
slice (issue #11) will replace it with a real session-based check. The
shape is intentionally a drop-in for FastAPI's ``Depends`` so the call
sites do not need to change when the real dependency lands.
"""

from __future__ import annotations

import re
import uuid
from email.message import Message
from email.parser import BytesParser
from typing import Annotated, cast

from fastapi import APIRouter, Depends, HTTPException, Path, Request, status
from sqlalchemy.orm import Session

from job_apply.db import get_db
from job_apply.features.resumes.extractors import PlainTextExtractor
from job_apply.features.resumes.repository import ResumesRepository
from job_apply.features.resumes.schemas import ResumeDTO, ResumeListResponse, UploadedFile
from job_apply.features.resumes.service import ResumesService
from job_apply.shared.errors import DomainError, NotFoundError, ValidationError

# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/resumes", tags=["resumes"])

#: Default tag used in OpenAPI; overridable when mounting the router into a
#: larger app.
_OPENAPI_TAG = "resumes"


# ---------------------------------------------------------------------------
# Authentication stub
# ---------------------------------------------------------------------------

#: A deterministic UUID used by :func:`_stub_current_user` so tests and
#: curl-driven smoke checks can predict the active user. The auth slice
#: (issue #11) will replace this with a session-based dependency; the
#: public type alias below stays the same.
_STUB_CURRENT_USER_ID: uuid.UUID = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _stub_current_user() -> uuid.UUID:
    """Return a hard-coded user id until the auth slice is in place.

    See :data:`StubAuthDep` for the public type alias used by the route
    signatures.
    """
    return _STUB_CURRENT_USER_ID


#: FastAPI dependency alias for the current user. The auth slice will
#: redefine this name (re-binding it to a real session-based dependency)
#: without touching the route signatures below.
StubAuthDep = Annotated[uuid.UUID, Depends(_stub_current_user)]


# ---------------------------------------------------------------------------
# Service factory
# ---------------------------------------------------------------------------


def _build_service(db: Session) -> ResumesService:
    """Build a fully-wired :class:`ResumesService` for a single request."""
    return ResumesService(
        repository=ResumesRepository(db),
        extractor=PlainTextExtractor(),
    )


# ---------------------------------------------------------------------------
# Multipart parsing (stdlib, no python-multipart)
# ---------------------------------------------------------------------------


_BOUNDARY_RE = re.compile(rb'boundary=(?:"([^"]+)"|([^;\r\n]+))', re.IGNORECASE)


def _extract_boundary(content_type: str) -> str:
    """Pull the ``boundary=...`` value out of a ``multipart/form-data`` Content-Type header."""
    match = _BOUNDARY_RE.search(content_type.encode("ascii", errors="ignore"))
    if match is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="multipart/form-data request is missing a boundary parameter.",
        )
    boundary = match.group(1) or match.group(2)
    return boundary.decode("ascii", errors="replace").strip()


def _parse_multipart_upload(body: bytes, content_type: str) -> UploadedFile:
    """Parse a single-file ``multipart/form-data`` body and return an :class:`UploadedFile`.

    Only the first file part is consumed; this matches the M1 contract
    (one resume per upload). The Content-Type of the part is preferred
    over the client-supplied ``content_type`` field, which is more
    authoritative.
    """
    _extract_boundary(content_type)  # validates the boundary parameter exists

    # stdlib's email parser wants a fully-formed RFC 822 message. We give
    # it the bare minimum so it focuses on parsing the multipart body.
    envelope: bytes = (
        b"Content-Type: " + content_type.encode("ascii", errors="replace") + b"\r\n"
        b"MIME-Version: 1.0\r\n"
        b"\r\n" + body
    )
    message: Message = BytesParser().parsebytes(envelope)

    for part in message.walk():
        # Skip the multipart container itself; it has no payload we care
        # about and the Content-Disposition header is not a file upload.
        if part.is_multipart():
            continue
        disposition = part.get("Content-Disposition", "")
        if not disposition.lower().startswith("form-data"):
            continue
        filename = part.get_filename()
        if filename is None:
            # Form fields (e.g. ``csrf_token``) have no filename; skip
            # them and keep looking for the file part.
            continue
        raw_payload = part.get_payload(decode=True)
        if raw_payload is None:
            payload = b""
        elif isinstance(raw_payload, bytes):
            payload = raw_payload
        else:
            # ``compat32`` (the default policy) returns ``str`` for
            # text/* parts and ``bytes`` for everything else. Force
            # bytes here so the rest of the pipeline stays typed.
            payload = cast("str", raw_payload).encode("utf-8", errors="replace")
        part_content_type = part.get_content_type()
        return UploadedFile(
            filename=filename,
            content_type=part_content_type or "application/octet-stream",
            size=len(payload),
            content=payload,
        )

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="multipart/form-data request did not contain a file part.",
    )


# ---------------------------------------------------------------------------
# Error translation
# ---------------------------------------------------------------------------


def _raise_http_from_domain_error(exc: DomainError) -> None:
    """Translate a :class:`DomainError` into the matching ``HTTPException``."""
    if isinstance(exc, NotFoundError):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=exc.message) from exc
    if isinstance(exc, ValidationError):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=exc.message
        ) from exc
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=exc.message
    ) from exc


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post(
    "",
    response_model=ResumeDTO,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a resume",
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "file": {
                                "type": "string",
                                "format": "binary",
                                "description": (
                                    "The resume file. Supported content types: "
                                    "text/plain, text/markdown, application/pdf, "
                                    "application/vnd.openxmlformats-officedocument."
                                    "wordprocessingml.document. PDF/DOCX are accepted "
                                    "but raise NotImplementedError for now."
                                ),
                            }
                        },
                        "required": ["file"],
                    }
                }
            },
        }
    },
)
async def upload_resume(
    request: Request,
    current_user_id: StubAuthDep,  # type: ignore[valid-type]
    db: Session = Depends(get_db),  # noqa: B008
) -> ResumeDTO:
    """Upload a single resume file and return the created record."""
    content_type = request.headers.get("content-type", "")
    if not content_type.lower().startswith("multipart/form-data"):
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=("Resumes must be uploaded as multipart/form-data with a single 'file' part."),
        )

    body = await request.body()
    upload = _parse_multipart_upload(body, content_type)
    service = _build_service(db)
    try:
        return service.upload_resume(user_id=current_user_id, upload=upload)
    except NotImplementedError as exc:
        # The extractor raised because the format is recognised but not
        # implemented yet (PDF/DOCX in the M1 skeleton).
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=str(exc),
        ) from exc
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=exc.message,
        ) from exc
    except DomainError as exc:
        _raise_http_from_domain_error(exc)
        raise  # pragma: no cover - the helper always raises


@router.get(
    "",
    response_model=ResumeListResponse,
    summary="List the current user's resumes",
)
def list_resumes(
    current_user_id: StubAuthDep,  # type: ignore[valid-type]
    db: Session = Depends(get_db),  # noqa: B008
) -> ResumeListResponse:
    """Return every resume owned by the current user, newest first."""
    service = _build_service(db)
    try:
        items = service.list_resumes(user_id=current_user_id)
    except DomainError as exc:
        _raise_http_from_domain_error(exc)
        raise  # pragma: no cover
    return ResumeListResponse(items=items)


@router.get(
    "/{resume_id}",
    response_model=ResumeDTO,
    summary="Fetch a single resume by id",
)
def get_resume(
    resume_id: Annotated[uuid.UUID, Path(...)],
    current_user_id: StubAuthDep,  # type: ignore[valid-type]
    db: Session = Depends(get_db),  # noqa: B008
) -> ResumeDTO:
    """Return the resume identified by ``resume_id`` if it belongs to the current user."""
    service = _build_service(db)
    try:
        return service.get_resume(user_id=current_user_id, resume_id=resume_id)
    except DomainError as exc:
        _raise_http_from_domain_error(exc)
        raise  # pragma: no cover


__all__ = [
    "StubAuthDep",
    "_build_service",
    "_extract_boundary",
    "_parse_multipart_upload",
    "router",
]
