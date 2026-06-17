"""TDD tests for the Telegram-channels vacancy classifier (M7, issue #58).

Telegram channels mix vacancy posts with discussion, news, and
self-promotion. The classifier is a fast keyword-driven pre-filter
that runs **before** the (more expensive) normaliser + ingest path so
the scanner can skip irrelevant traffic cheaply.

The classifier is intentionally simple — a list of case-insensitive
"vacancy" markers, and a list of "this is a discussion / non-vacancy
post" markers that win when both fire. The list lives in
:data:`apply_pilot.features.telegram_channels.classifier.DEFAULT_VACANCY_MARKERS`
and is overridable on construction so a channel-specific subset can
be plugged in by callers that know the channel's vocabulary.
"""

from __future__ import annotations

from apply_pilot.features.telegram_channels.classifier import TelegramChannelClassifier


def _msg(text: str) -> str:
    return text


class TestTelegramChannelClassifier:
    def test_classifier_accepts_explicit_vacancy_hashtag(self) -> None:
        """A post with the ``#vacancy`` hashtag is a vacancy."""
        classifier = TelegramChannelClassifier()
        assert classifier.is_vacancy_post(_msg("#vacancy Senior Python Developer")) is True

    def test_classifier_accepts_russian_vacancy_hashtag(self) -> None:
        """The Russian ``#вакансия`` hashtag is a vacancy marker."""
        classifier = TelegramChannelClassifier()
        assert classifier.is_vacancy_post(_msg("#вакансия Senior Python Developer")) is True

    def test_classifier_accepts_hiring_keyword(self) -> None:
        """The word ``hiring`` (case-insensitive) is a vacancy marker."""
        classifier = TelegramChannelClassifier()
        assert classifier.is_vacancy_post(_msg("We are hiring a Senior Go Engineer")) is True

    def test_classifier_accepts_keyword_anywhere(self) -> None:
        """The marker can appear anywhere in the post, not just at the start."""
        classifier = TelegramChannelClassifier()
        text = "Some intro text, then a #vacancy announcement follows."
        assert classifier.is_vacancy_post(text) is True

    def test_classifier_rejects_casual_chat(self) -> None:
        """A casual conversation that does not mention vacancies is rejected."""
        classifier = TelegramChannelClassifier()
        assert classifier.is_vacancy_post(_msg("Hey folks, what's up?")) is False

    def test_classifier_rejects_news_post(self) -> None:
        """Industry news without a vacancy marker is rejected."""
        classifier = TelegramChannelClassifier()
        assert (
            classifier.is_vacancy_post(_msg("Breaking: a new Python 3.13 release is out.")) is False
        )

    def test_classifier_rejects_empty_text(self) -> None:
        """An empty post is not a vacancy."""
        classifier = TelegramChannelClassifier()
        assert classifier.is_vacancy_post("") is False

    def test_classifier_rejects_post_with_anti_keyword(self) -> None:
        """A post that explicitly *isn't* a vacancy (e.g. ``#novacancy``) is rejected.

        The anti-marker wins over a generic vacancy keyword to guard
        against false positives.
        """
        classifier = TelegramChannelClassifier()
        text = "We are hiring? No, just #novacancy this week, sorry."
        assert classifier.is_vacancy_post(text) is False

    def test_classifier_uses_custom_markers(self) -> None:
        """A caller can supply a custom vacancy marker list."""
        classifier = TelegramChannelClassifier(vacancy_markers=("#job",))
        assert classifier.is_vacancy_post(_msg("#job Senior Python Developer")) is True
        # The default ``#vacancy`` marker no longer applies.
        assert classifier.is_vacancy_post(_msg("#vacancy Senior Python Developer")) is False

    def test_classifier_is_case_insensitive(self) -> None:
        """Marker matching is case-insensitive — Telegram hashtags are too."""
        classifier = TelegramChannelClassifier()
        assert classifier.is_vacancy_post(_msg("#VACANCY Senior Python")) is True
        assert classifier.is_vacancy_post(_msg("#Vacancy Senior Python")) is True

    def test_classifier_handles_none_text(self) -> None:
        """``None`` text is treated as a non-vacancy post (not a crash)."""
        classifier = TelegramChannelClassifier()
        assert classifier.is_vacancy_post(None) is False  # type: ignore[arg-type]
