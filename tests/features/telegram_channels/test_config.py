"""TDD tests for the Telegram-channels configuration (M7, issue #58).

The slice reads its configuration from environment variables via
:func:`apply_pilot.features.telegram_channels.config.get_telegram_channels_settings`.
The list of watched channels is a comma-separated string of
``@username`` or numeric channel ids; the poll interval is a
positive float (seconds). The tests pin the env contract and the
validation rules so a follow-up wiring change does not silently
break the slice.
"""

from __future__ import annotations

import pytest

from apply_pilot.features.telegram_channels import (
    TelegramChannelConfig,
    get_telegram_channels_settings,
)


def _set_env(monkeypatch: pytest.MonkeyPatch, **values: str) -> None:
    """Populate the env vars the settings builder reads."""
    for key, value in values.items():
        monkeypatch.setenv(key, value)


class TestTelegramChannelsSettingsParsing:
    def test_default_poll_interval(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Without ``TELEGRAM_CHANNELS_POLL_INTERVAL`` we get a sensible default."""
        _set_env(monkeypatch, TELEGRAM_CHANNELS="@jobs")
        settings = get_telegram_channels_settings()

        assert settings.poll_interval_seconds == pytest.approx(60.0)
        assert [c.identifier for c in settings.channels] == ["@jobs"]

    def test_explicit_poll_interval(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A custom ``TELEGRAM_CHANNELS_POLL_INTERVAL`` is honoured."""
        _set_env(
            monkeypatch,
            TELEGRAM_CHANNELS="@jobs",
            TELEGRAM_CHANNELS_POLL_INTERVAL="5",
        )
        settings = get_telegram_channels_settings()

        assert settings.poll_interval_seconds == pytest.approx(5.0)

    def test_comma_separated_channels(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A comma-separated ``TELEGRAM_CHANNELS`` is split into a list."""
        _set_env(monkeypatch, TELEGRAM_CHANNELS="@jobs, @remote, @backend")
        settings = get_telegram_channels_settings()

        assert [c.identifier for c in settings.channels] == [
            "@jobs",
            "@remote",
            "@backend",
        ]

    def test_whitespace_only_entries_are_dropped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Trailing commas / spaces do not produce empty channel entries."""
        _set_env(monkeypatch, TELEGRAM_CHANNELS="@jobs, , @remote, ")
        settings = get_telegram_channels_settings()

        assert [c.identifier for c in settings.channels] == ["@jobs", "@remote"]

    def test_unset_env_returns_empty_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Without ``TELEGRAM_CHANNELS`` the settings object has no channels."""
        monkeypatch.delenv("TELEGRAM_CHANNELS", raising=False)
        settings = get_telegram_channels_settings()

        assert settings.channels == []
        assert settings.poll_interval_seconds == pytest.approx(60.0)


class TestTelegramChannelsSettingsValidation:
    def test_invalid_poll_interval_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A non-numeric ``TELEGRAM_CHANNELS_POLL_INTERVAL`` is rejected."""
        _set_env(
            monkeypatch,
            TELEGRAM_CHANNELS="@jobs",
            TELEGRAM_CHANNELS_POLL_INTERVAL="not-a-number",
        )
        with pytest.raises(ValueError, match="TELEGRAM_CHANNELS_POLL_INTERVAL"):
            get_telegram_channels_settings()

    def test_zero_poll_interval_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A non-positive ``TELEGRAM_CHANNELS_POLL_INTERVAL`` is rejected."""
        _set_env(
            monkeypatch,
            TELEGRAM_CHANNELS="@jobs",
            TELEGRAM_CHANNELS_POLL_INTERVAL="0",
        )
        with pytest.raises(ValueError, match="positive"):
            get_telegram_channels_settings()

    def test_negative_poll_interval_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A negative ``TELEGRAM_CHANNELS_POLL_INTERVAL`` is rejected."""
        _set_env(
            monkeypatch,
            TELEGRAM_CHANNELS="@jobs",
            TELEGRAM_CHANNELS_POLL_INTERVAL="-1",
        )
        with pytest.raises(ValueError, match="positive"):
            get_telegram_channels_settings()


class TestTelegramChannelConfig:
    def test_identifier_required(self) -> None:
        """An empty identifier is rejected — Telegram never accepts it."""
        with pytest.raises(ValueError, match="identifier"):
            TelegramChannelConfig(identifier="")

    def test_display_name_optional(self) -> None:
        """``display_name`` is optional and defaults to ``None``."""
        config = TelegramChannelConfig(identifier="@jobs")
        assert config.display_name is None

    def test_display_name_propagates(self) -> None:
        """An explicit ``display_name`` is preserved."""
        config = TelegramChannelConfig(identifier="@jobs", display_name="Jobs Channel")
        assert config.display_name == "Jobs Channel"
