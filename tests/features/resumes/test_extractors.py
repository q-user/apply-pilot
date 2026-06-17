"""Tests for resume text extractors (PDF, DOCX) and service-level dispatch.

These tests use pypdf's writer API and python-docx to build real PDF/DOCX
files in-memory, then assert that the extractors round-trip the text. The
PDF helper hand-crafts a content stream with the standard Helvetica font so
it does not depend on any third-party PDF generator.
"""

from __future__ import annotations

import io
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import ClassVar
from uuid import UUID, uuid4

import pytest
from docx import Document
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from apply_pilot.features.resumes.extractors import (
    DocxTextExtractor,
    ExtractionNotSupportedError,
    PdfTextExtractor,
)
from apply_pilot.features.resumes.schemas import UploadedFile
from apply_pilot.features.resumes.service import ResumesService

# ---------------------------------------------------------------------------
# Helpers: build PDF / DOCX in-memory
# ---------------------------------------------------------------------------


def _build_pdf_bytes(pages_text: list[str]) -> bytes:
    """Render a multi-page PDF in-memory using :mod:`pypdf`'s writer API.

    The PDF uses the standard Type1 Helvetica font so we do not have to
    embed a font. Each page is a blank page with a content stream that
    draws ``pages_text[i]`` near the top-left corner. The output is a
    valid PDF that :class:`pypdf.PdfReader` can open and decode.
    """
    writer = PdfWriter()
    for text in pages_text:
        page = writer.add_blank_page(width=612, height=792)
        # Escape PDF string characters: backslash, '(' and ')'.
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        content_data = f"BT /F1 18 Tf 50 750 Td ({escaped}) Tj ET".encode("latin-1")
        stream = DecodedStreamObject()
        stream.set_data(content_data)
        page[NameObject("/Contents")] = stream
        font_dict = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        resources = DictionaryObject(
            {
                NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_dict}),
            }
        )
        page[NameObject("/Resources")] = resources
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _build_docx_bytes(paragraphs: list[str]) -> bytes:
    """Render a DOCX in-memory using :mod:`python-docx` with the given paragraphs."""
    doc = Document()
    for paragraph in paragraphs:
        doc.add_paragraph(paragraph)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# PdfTextExtractor
# ---------------------------------------------------------------------------


def test_pdf_extractor_decodes_pdf_bytes() -> None:
    """PdfTextExtractor extracts text from a real single-page PDF."""
    expected = "Hello World from PDF"
    content = _build_pdf_bytes([expected])
    # Sanity check: the bytes start with the PDF magic header.
    assert content[:4] == b"%PDF"
    # Sanity check: pypdf can already read what we just wrote.
    assert expected in PdfReader(io.BytesIO(content)).pages[0].extract_text()

    text = PdfTextExtractor().decode(content, "application/pdf", "cv.pdf")

    assert expected in text


def test_pdf_extractor_with_multiple_pages() -> None:
    """PdfTextExtractor concatenates text from every page of a multi-page PDF."""
    page_one = "First page content"
    page_two = "Second page content"
    page_three = "Third page content"
    content = _build_pdf_bytes([page_one, page_two, page_three])

    text = PdfTextExtractor().decode(content, "application/pdf", "cv.pdf")

    assert page_one in text
    assert page_two in text
    assert page_three in text
    # Pages are joined with '\n' so the result is a single newline-delimited block.
    assert "\n" in text


def test_pdf_extractor_handles_corrupt_pdf() -> None:
    """Garbage bytes that pypdf cannot parse must surface as ExtractionNotSupportedError."""
    with pytest.raises(ExtractionNotSupportedError):
        PdfTextExtractor().decode(
            b"this is definitely not a pdf",
            "application/pdf",
            "broken.pdf",
        )


# ---------------------------------------------------------------------------
# DocxTextExtractor
# ---------------------------------------------------------------------------


def test_docx_extractor_decodes_docx_bytes() -> None:
    """DocxTextExtractor extracts text from a real DOCX with a single paragraph."""
    expected = "Experienced Python developer with a knack for clean code."
    content = _build_docx_bytes([expected])
    # Sanity check: the bytes start with the ZIP magic header that python-docx uses.
    assert content[:4] == b"PK\x03\x04"

    text = DocxTextExtractor().decode(
        content,
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "cv.docx",
    )

    assert expected in text


def test_docx_extractor_with_paragraphs() -> None:
    """DocxTextExtractor joins multiple paragraphs with a newline separator."""
    paragraphs = [
        "Mikhail Petrov",
        "Senior Software Engineer",
        "Skills: Python, SQL, FastAPI",
    ]
    content = _build_docx_bytes(paragraphs)

    text = DocxTextExtractor().decode(
        content,
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "cv.docx",
    )

    for paragraph in paragraphs:
        assert paragraph in text
    # The extractor joins paragraphs with newlines; assert at least one '\n' is present.
    assert "\n" in text


# ---------------------------------------------------------------------------
# Service-level dispatch
# ---------------------------------------------------------------------------


class _FakeResumesRepository:
    """In-memory test double for the service's repository dependency.

    Implements just enough of :class:`ResumesRepository`'s surface to
    satisfy the service. Records the kwargs passed to ``create`` so the
    dispatch test can assert what the service persisted.
    """

    def __init__(self) -> None:
        self.records: list[SimpleNamespace] = []

    def create(
        self,
        *,
        user_id: UUID,
        filename: str,
        content_type: str,
        size: int,
        raw_text: str,
        plain_text: str,
    ) -> SimpleNamespace:
        record = SimpleNamespace(
            id=uuid4(),
            user_id=user_id,
            filename=filename,
            content_type=content_type,
            size=size,
            raw_text=raw_text,
            plain_text=plain_text,
            created_at=datetime.now(UTC),
            updated_at=None,
        )
        self.records.append(record)
        return record

    def get(self, resume_id: UUID) -> SimpleNamespace | None:  # pragma: no cover
        return None

    def list_for_user(self, user_id: UUID) -> list[SimpleNamespace]:  # pragma: no cover
        return []


class _RecordingPdfExtractor:
    """Recording double for :class:`PdfTextExtractor`.

    Captures every call so the test can assert that the service invoked
    the PDF path (not the plain-text one) for an ``application/pdf``
    upload.
    """

    SUPPORTED_CONTENT_TYPES: ClassVar[tuple[str, ...]] = ("application/pdf",)
    TEXT: ClassVar[str] = "PDF DISPATCH TEXT"

    def __init__(self) -> None:
        self.calls: list[tuple[bytes, str, str]] = []

    def decode(self, content: bytes, *, content_type: str, filename: str) -> str:
        self.calls.append((content, content_type, filename))
        return self.TEXT

    def extract(self, content: bytes, *, content_type: str, filename: str) -> str:
        return self.decode(content, content_type=content_type, filename=filename)


def test_service_dispatches_to_pdf_extractor() -> None:
    """The service routes application/pdf uploads to the configured PdfTextExtractor.

    This is the end-to-end dispatch check: when the service receives a
    PDF, it must call the PDF extractor (not the plain-text one) and
    persist the extracted text in the DTO.
    """
    pdf_bytes = _build_pdf_bytes(["ignored payload"])
    upload = UploadedFile(
        filename="resume.pdf",
        content_type="application/pdf",
        size=len(pdf_bytes),
        content=pdf_bytes,
    )
    pdf_extractor = _RecordingPdfExtractor()
    repository = _FakeResumesRepository()
    # The plain-text extractor is intentionally absent: any text/plain
    # dispatch would raise ExtractionNotSupportedError, so the test
    # would fail loudly if the service took the wrong path.
    service = ResumesService(
        repository=repository,  # type: ignore[arg-type]
        extractor=pdf_extractor,  # type: ignore[arg-type]
    )

    resume = service.upload_resume(user_id=uuid4(), upload=upload)

    assert pdf_extractor.calls == [(pdf_bytes, "application/pdf", "resume.pdf")]
    assert resume.plain_text == _RecordingPdfExtractor.TEXT
    assert resume.content_type == "application/pdf"
    assert len(repository.records) == 1
