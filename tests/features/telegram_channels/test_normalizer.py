"""TDD tests for the Telegram-channels normaliser (M7, issue #58).

The normaliser maps a raw :class:`TelegramChannelMessage` dict (the
adapter's "raw" payload) into the canonical
:class:`~apply_pilot.features.sources.models.Vacancy` row. It is
intentionally a pure mapper: classification has already happened by
the time we get here, so the normaliser assumes the message *is* a
vacancy post.

The natural key is ``f"{channel_id}:{message_id}"`` so the dedup
detector can recognise re-posts and re-edits of the same message.
"""

from __future__ import annotations

import pytest

from apply_pilot.features.telegram_channels import TelegramChannelMessage
from apply_pilot.features.telegram_channels.normalizer import TelegramChannelNormalizer


def _msg(
    *,
    channel_id: str = "@jobs",
    message_id: int = 42,
    text: str = "#vacancy Senior Python Developer\nAcme Corp\nSalary: 250-350k RUB",
    author: str | None = "JobBot",
) -> TelegramChannelMessage:
    return TelegramChannelMessage(
        channel_id=channel_id,
        message_id=message_id,
        text=text,
        author=author,
    )


def _to_dict(message: TelegramChannelMessage) -> dict:
    """Serialise the message to the dict shape the normaliser expects."""
    return {
        "channel_id": message.channel_id,
        "message_id": message.message_id,
        "text": message.text,
        "author": message.author,
    }


class TestTelegramChannelNormalizer:
    def test_source_is_telegram_channel(self) -> None:
        """The produced vacancy carries ``source == "telegram_channel"``."""
        normalizer = TelegramChannelNormalizer()
        vacancy = normalizer.normalize(_to_dict(_msg()))

        assert vacancy.source == "telegram_channel"

    def test_source_id_is_channel_id_colon_message_id(self) -> None:
        """``source_id`` encodes the (channel, message) tuple for dedup."""
        normalizer = TelegramChannelNormalizer()
        vacancy = normalizer.normalize(_to_dict(_msg(channel_id="@jobs", message_id=42)))

        assert vacancy.source_id == "@jobs:42"

    def test_title_uses_first_non_marker_line(self) -> None:
        """The first line of the message becomes the title."""
        normalizer = TelegramChannelNormalizer()
        vacancy = normalizer.normalize(
            _to_dict(_msg(text="#vacancy Senior Python Developer\nAcme Corp"))
        )

        assert vacancy.title == "Senior Python Developer"

    def test_description_uses_full_text(self) -> None:
        """The full text is preserved as the description."""
        normalizer = TelegramChannelNormalizer()
        text = "#vacancy Senior Python Developer\nAcme Corp\nSalary 250-350k"
        vacancy = normalizer.normalize(_to_dict(_msg(text=text)))

        assert vacancy.description == text

    def test_url_uses_telegram_message_link(self) -> None:
        """A canonical ``https://t.me/{channel}/{message_id}`` link is set."""
        normalizer = TelegramChannelNormalizer()
        vacancy = normalizer.normalize(_to_dict(_msg(channel_id="@jobs", message_id=42)))

        assert vacancy.url == "https://t.me/@jobs/42"

    def test_url_handles_numeric_channel_id(self) -> None:
        """A numeric channel id (e.g. ``-100123…``) is appended to ``t.me/c/``.

        Telegram deep-links for supergroup channels drop the leading
        ``-`` from the id; the URL form is ``t.me/c/<id>/<msg>``.
        """
        normalizer = TelegramChannelNormalizer()
        vacancy = normalizer.normalize(_to_dict(_msg(channel_id="-1001234567890", message_id=7)))

        assert vacancy.url == "https://t.me/c/1001234567890/7"

    def test_employer_name_falls_back_to_author(self) -> None:
        """When no explicit employer is in the text, the post author is used."""
        normalizer = TelegramChannelNormalizer()
        vacancy = normalizer.normalize(_to_dict(_msg(author="JobBot")))

        assert vacancy.employer_name == "JobBot"

    def test_employer_name_overrides_author_when_explicit(self) -> None:
        """An explicit ``Company:`` line wins over the channel author."""
        normalizer = TelegramChannelNormalizer()
        vacancy = normalizer.normalize(
            _to_dict(
                _msg(
                    text="#vacancy Senior Python Developer\nCompany: Acme\nSalary: 250k",
                    author="JobBot",
                )
            )
        )

        assert vacancy.employer_name == "Acme"

    def test_raw_data_round_trip(self) -> None:
        """The raw dict is stored verbatim for re-normalisation / debugging."""
        normalizer = TelegramChannelNormalizer()
        raw = _to_dict(_msg())
        vacancy = normalizer.normalize(raw)

        assert vacancy.raw_data == raw

    def test_content_hash_computed(self) -> None:
        """A :attr:`Vacancy.content_hash` is computed for cross-source dedup."""
        normalizer = TelegramChannelNormalizer()
        vacancy = normalizer.normalize(_to_dict(_msg()))

        assert vacancy.content_hash is not None
        # SHA-256 hex digest is 64 chars.
        assert len(vacancy.content_hash) == 64

    def test_invalid_payload_raises(self) -> None:
        """A non-dict payload is rejected."""
        normalizer = TelegramChannelNormalizer()
        with pytest.raises(ValueError, match="dict"):
            normalizer.normalize("not a dict")  # type: ignore[arg-type]

    def test_missing_channel_id_raises(self) -> None:
        """A payload without a ``channel_id`` cannot be normalised."""
        normalizer = TelegramChannelNormalizer()
        with pytest.raises(ValueError, match="channel_id"):
            normalizer.normalize({"message_id": 1, "text": "x"})

    def test_missing_message_id_raises(self) -> None:
        """A payload without a ``message_id`` cannot be normalised."""
        normalizer = TelegramChannelNormalizer()
        with pytest.raises(ValueError, match="message_id"):
            normalizer.normalize({"channel_id": "@jobs", "text": "x"})

    def test_missing_text_raises(self) -> None:
        """A payload without a ``text`` cannot be normalised."""
        normalizer = TelegramChannelNormalizer()
        with pytest.raises(ValueError, match="text"):
            normalizer.normalize({"channel_id": "@jobs", "message_id": 1})

    def test_empty_text_yields_unknown_title(self) -> None:
        """An empty text still normalises — the title falls back to a marker."""
        normalizer = TelegramChannelNormalizer()
        vacancy = normalizer.normalize(_to_dict(_msg(text="")))

        # Title is not empty even when the source text is — the slice
        # never produces a vacancy without something to label it with.
        assert vacancy.title == ""
