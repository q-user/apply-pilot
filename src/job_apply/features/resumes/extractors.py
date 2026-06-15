"""Resume text extraction.

This module is the seam between raw uploaded bytes and the plain-text
representation we store in the database.

Three concrete extractors are provided, one per supported format:

* :class:`PlainTextExtractor` handles ``text/plain`` and ``text/markdown``
  (no third-party deps; just a UTF-8 decode with ``errors="replace"``).
* :class:`PdfTextExtractor` handles ``application/pdf`` via
  :mod:`pypdf`.
* :class:`DocxTextExtractor` handles
  ``application/vnd.openxmlformats-officedocument.wordprocessingml.document``
  via :mod:`python-docx`.

All three implement the :class:`TextExtractor` protocol. The service
layer is wired with a content-type -> extractor registry so adding a new
format is a matter of registering another implementation.

Design rules:

* A :class:`TextExtractor` is stateless. It can be reused across requests.
* Decoders must raise :class:`ExtractionNotSupportedError` (not a generic
  ``NotImplementedError``) so the FastAPI handler can render a 501 with a
  stable ``code`` and the service-layer test does not have to depend on
  stdlib exception internals.
* Library-level errors (corrupt PDF, broken DOCX zip, ...) are caught
  inside the decoder and re-raised as :class:`ExtractionNotSupportedError`
  so the rest of the stack only has to reason about a single failure
  mode. The original exception is chained via ``raise ... from exc`` for
  debuggability.
* Decoders must never raise ``ValidationError``; that is the service
  layer's job (size, content-type allow-list, declared size vs actual).
"""

from __future__ import annotations

import io
from typing import Protocol, runtime_checkable

import docx
import pypdf

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ExtractionNotSupportedError(NotImplementedError):
    """Raised when a decoder cannot extract text from the supplied content.

    Inherits from :class:`NotImplementedError` so existing ``pytest.raises``
    checks for ``NotImplementedError`` keep working, while also carrying a
    stable ``code`` attribute for the API to surface in error responses.
    """

    code: str = "extraction_not_supported"

    def __init__(self, message: str, *, content_type: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.content_type = content_type


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class TextExtractor(Protocol):
    """Protocol for converting raw resume bytes into plain text."""

    #: MIME types this implementation can decode. Used by the service
    #: layer to build a content-type -> extractor registry when a single
    #: instance is injected.
    SUPPORTED_CONTENT_TYPES: tuple[str, ...]

    def decode(self, content: bytes, content_type: str, filename: str) -> str:
        """Decode ``content`` to text.

        Implementations should raise :class:`ExtractionNotSupportedError`
        for content they do not understand (wrong MIME type, corrupt
        bytes, encrypted file, ...) rather than returning an empty
        string. Returning an empty string hides bugs.
        """
        ...

    def extract(self, content: bytes, content_type: str, filename: str) -> str:
        """High-level entry point used by the service layer.

        Today this is a thin wrapper over :meth:`decode`, but future
        extractors (e.g. ones that stream pages) will replace this with
        their own implementation.
        """
        ...


# ---------------------------------------------------------------------------
# Plain text
# ---------------------------------------------------------------------------


class PlainTextExtractor:
    """Text extractor for ``text/plain`` and ``text/markdown`` content.

    The decoder is intentionally trivial: UTF-8 decode, with ``errors="replace"``
    so a stray non-UTF-8 byte does not 500 the upload. Anything that is not
    a plain-text MIME type is rejected with :class:`ExtractionNotSupportedError`
    so the service layer can return a clear 501.
    """

    #: MIME types this extractor can decode. Stored as a tuple so it can
    #: be iterated deterministically in tests and debug output.
    SUPPORTED_CONTENT_TYPES: tuple[str, ...] = ("text/plain", "text/markdown")

    def decode(self, content: bytes, content_type: str, filename: str) -> str:
        """Decode ``content`` as UTF-8 text.

        Unknown content types raise :class:`ExtractionNotSupportedError`
        with the original MIME type attached for diagnostics.
        """
        if content_type not in self.SUPPORTED_CONTENT_TYPES:
            raise ExtractionNotSupportedError(
                f"PlainTextExtractor does not support content type {content_type!r}; "
                f"supported types: {self.SUPPORTED_CONTENT_TYPES}",
                content_type=content_type,
            )
        return content.decode("utf-8", errors="replace")

    def extract(self, content: bytes, content_type: str, filename: str) -> str:
        """Return the plain-text representation of ``content``."""
        return self.decode(content, content_type, filename)


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------


class PdfTextExtractor:
    """Text extractor for ``application/pdf`` content.

    Uses :mod:`pypdf` to walk every page of the document and concatenate
    the extracted text with a single ``\\n`` separator. Library-level
    errors (malformed PDF, encrypted file, garbage bytes) are caught and
    re-raised as :class:`ExtractionNotSupportedError` so callers only have
    to handle a single failure mode.
    """

    SUPPORTED_CONTENT_TYPES: tuple[str, ...] = ("application/pdf",)

    def decode(self, content: bytes, content_type: str, filename: str) -> str:
        """Decode ``content`` as a PDF and return text for every page.

        Parameters
        ----------
        content:
            Raw PDF bytes (the body of the upload).
        content_type:
            Must be ``application/pdf``; anything else raises
            :class:`ExtractionNotSupportedError`.
        filename:
            Original filename, used in error messages to help users
            identify which file failed.
        """
        if content_type not in self.SUPPORTED_CONTENT_TYPES:
            raise ExtractionNotSupportedError(
                f"PdfTextExtractor does not support content type {content_type!r}; "
                f"supported types: {self.SUPPORTED_CONTENT_TYPES}",
                content_type=content_type,
            )
        try:
            reader = pypdf.PdfReader(io.BytesIO(content))
            # Iterating ``reader.pages`` is what actually triggers parsing
            # in pypdf; doing it here means library errors surface inside
            # the try/except instead of leaking to the caller.
            page_texts = [page.extract_text() or "" for page in reader.pages]
        except Exception as exc:  # pypdf raises PdfReadError, KeyError, etc. for bad input
            raise ExtractionNotSupportedError(
                f"Failed to parse PDF {content_type!r} from {filename!r}: {exc}",
                content_type=content_type,
            ) from exc
        return "\n".join(page_texts)

    def extract(self, content: bytes, content_type: str, filename: str) -> str:
        """Return the plain-text representation of ``content``."""
        return self.decode(content, content_type, filename)


# ---------------------------------------------------------------------------
# DOCX
# ---------------------------------------------------------------------------


class DocxTextExtractor:
    """Text extractor for Word ``.docx`` documents.

    Uses :mod:`python-docx` to load the document and join the text of
    every paragraph with a single ``\\n`` separator. Library-level errors
    (corrupt zip, wrong file type, missing parts) are caught and re-raised
    as :class:`ExtractionNotSupportedError`.
    """

    #: The fully-qualified MIME type for ``.docx`` per RFC 6838 / ECMA-376.
    SUPPORTED_CONTENT_TYPES: tuple[str, ...] = (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )

    def decode(self, content: bytes, content_type: str, filename: str) -> str:
        """Decode ``content`` as a DOCX and return text for every paragraph.

        Parameters
        ----------
        content:
            Raw DOCX bytes (the body of the upload).
        content_type:
            Must be the DOCX MIME type; anything else raises
            :class:`ExtractionNotSupportedError`.
        filename:
            Original filename, used in error messages.
        """
        if content_type not in self.SUPPORTED_CONTENT_TYPES:
            raise ExtractionNotSupportedError(
                f"DocxTextExtractor does not support content type {content_type!r}; "
                f"supported types: {self.SUPPORTED_CONTENT_TYPES}",
                content_type=content_type,
            )
        try:
            document = docx.Document(io.BytesIO(content))
        except Exception as exc:  # python-docx raises PackageNotFoundError, etc. for bad input
            raise ExtractionNotSupportedError(
                f"Failed to parse DOCX {content_type!r} from {filename!r}: {exc}",
                content_type=content_type,
            ) from exc
        return "\n".join(paragraph.text for paragraph in document.paragraphs)

    def extract(self, content: bytes, content_type: str, filename: str) -> str:
        """Return the plain-text representation of ``content``."""
        return self.decode(content, content_type, filename)


__all__ = [
    "DocxTextExtractor",
    "ExtractionNotSupportedError",
    "PdfTextExtractor",
    "PlainTextExtractor",
    "TextExtractor",
]
