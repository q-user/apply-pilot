"""Resume text extraction.

This module is the seam between raw uploaded bytes and the plain-text
representation we store in the database. Today the only fully-supported
format is ``text/plain`` and ``text/markdown`` (handled by the stdlib +
:class:`PlainTextExtractor`). PDF and DOCX are recognised at the API
boundary so callers see a clean ``ExtractionNotSupportedError`` instead of
silently-empty text, but the actual decoding libraries (``pypdf`` and
``python-docx``) are deliberately out of scope for the M1 skeleton — they
will land as a follow-up that adds the third-party deps.

Design rules:

* A :class:`TextExtractor` is stateless. It can be reused across requests.
* Decoders must raise :class:`ExtractionNotSupportedError` (not a generic
  ``NotImplementedError``) so the FastAPI handler can render a 501 with a
  stable ``code`` and the service-layer test does not have to depend on
  stdlib exception internals.
* Decoders must never raise ``ValidationError``; that is the service
  layer's job (size, content-type allow-list, declared size vs actual).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


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


@runtime_checkable
class TextExtractor(Protocol):
    """Protocol for converting raw resume bytes into plain text."""

    def decode(self, content: bytes, *, content_type: str, filename: str) -> str:
        """Decode ``content`` to text.

        Implementations should raise :class:`ExtractionNotSupportedError`
        for content they do not understand rather than returning an empty
        string. Returning an empty string hides bugs.
        """
        ...

    def extract(self, content: bytes, *, content_type: str, filename: str) -> str:
        """High-level entry point used by the service layer.

        Today this is a thin wrapper over :meth:`decode`, but future
        extractors (e.g. ones that stream pages) will replace this with
        their own implementation.
        """
        ...


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

    def decode(self, content: bytes, *, content_type: str, filename: str) -> str:
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

    def extract(self, content: bytes, *, content_type: str, filename: str) -> str:
        """Return the plain-text representation of ``content``."""
        return self.decode(content, content_type=content_type, filename=filename)


__all__ = [
    "ExtractionNotSupportedError",
    "PlainTextExtractor",
    "TextExtractor",
]
