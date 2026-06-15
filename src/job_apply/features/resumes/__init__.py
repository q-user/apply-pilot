"""Resumes vertical slice.

Public re-exports only. The HTTP router lives in :mod:`api` and is
deliberately not re-exported here — wiring it into the application
``FastAPI`` instance is the responsibility of the entry point, not the
slice itself.
"""

from __future__ import annotations

from job_apply.features.resumes.extractors import (
    DocxTextExtractor,
    ExtractionNotSupportedError,
    PdfTextExtractor,
    PlainTextExtractor,
    TextExtractor,
)
from job_apply.features.resumes.models import Resume
from job_apply.features.resumes.repository import ResumesRepository
from job_apply.features.resumes.schemas import ResumeDTO, ResumeListResponse, UploadedFile
from job_apply.features.resumes.service import ResumesService

__all__ = [
    "DocxTextExtractor",
    "ExtractionNotSupportedError",
    "PdfTextExtractor",
    "PlainTextExtractor",
    "Resume",
    "ResumeDTO",
    "ResumeListResponse",
    "ResumesRepository",
    "ResumesService",
    "TextExtractor",
    "UploadedFile",
]
