"""Tests for the resume text extraction protocol and PlainTextExtractor."""

from __future__ import annotations

import pytest

from job_apply.features.resumes.extractors import (
    ExtractionNotSupportedError,
    PlainTextExtractor,
    TextExtractor,
)


def test_plain_text_extractor_returns_text() -> None:
    """PlainTextExtractor.decode decodes UTF-8 .txt and .md bytes to a str."""
    extractor: TextExtractor = PlainTextExtractor()

    txt_result = extractor.decode(b"hello world\n", content_type="text/plain", filename="a.txt")
    md_result = extractor.decode(b"# Title\n\nbody", content_type="text/markdown", filename="b.md")

    assert txt_result == "hello world\n"
    assert md_result == "# Title\n\nbody"


def test_plain_text_extractor_extract_returns_text() -> None:
    """PlainTextExtractor.extract routes .txt/.md through decode; the contract is str out."""
    extractor: TextExtractor = PlainTextExtractor()

    text = extractor.extract(b"raw bytes", content_type="text/plain", filename="r.txt")

    assert text == "raw bytes"


def test_plain_text_extractor_rejects_unsupported_content_type() -> None:
    """PlainTextExtractor must raise ExtractionNotSupportedError for non-text content."""
    extractor: TextExtractor = PlainTextExtractor()

    with pytest.raises(ExtractionNotSupportedError):
        extractor.extract(b"%PDF-1.4", content_type="application/pdf", filename="cv.pdf")

    with pytest.raises(ExtractionNotSupportedError):
        extractor.extract(
            b"PK\x03\x04",
            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            filename="cv.docx",
        )
