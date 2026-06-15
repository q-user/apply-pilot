"""Failing tests for the LLM client (issue #29).

Two implementations live in the slice:

* :class:`HttpLLMClient` — talks to an OpenAI-compatible
  ``/v1/chat/completions`` endpoint via ``httpx``. The tests use
  :class:`httpx.MockTransport` so no network is ever touched.
* :class:`InMemoryLLMClient` — dict-backed fake used by the rest of
  the test suite.

The tests are organised by *behaviour* (serialization, headers, error
handling) rather than by implementation, so a refactor that swaps the
HTTP library does not invalidate them.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping

import httpx
import pytest

from job_apply.features.scoring.llm import (
    HttpLLMClient,
    InMemoryLLMClient,
    LLMSettings,
)

# ---------------------------------------------------------------------------
# InMemoryLLMClient
# ---------------------------------------------------------------------------


class TestInMemoryClient:
    def test_returns_preloaded_response(self) -> None:
        client = InMemoryLLMClient(responses={"hello": "world"})

        assert client.complete_sync("hello") == "world"

    def test_function_based_response(self) -> None:
        """A callable can be used instead of a static dict; the client
        passes the prompt through."""
        client = InMemoryLLMClient(responses=lambda prompt: f"echo: {prompt}")

        assert client.complete_sync("hi") == "echo: hi"

    def test_missing_prompt_raises(self) -> None:
        """An unknown prompt is a hard error in tests — surfaces
        mismatches between test fixtures and code immediately."""
        client = InMemoryLLMClient(responses={})

        with pytest.raises(KeyError):
            client.complete_sync("never-configured")

    @pytest.mark.asyncio
    async def test_async_complete_returns_preloaded_response(self) -> None:
        client = InMemoryLLMClient(responses={"q": "a"})

        result = await client.complete("q")

        assert result == "a"

    def test_callable_receives_prompt(self) -> None:
        """The function-based fake receives the prompt argument; tests
        that want to inspect temperature/max_tokens use a different
        approach (e.g. capture the prompt and assert on the prompt
        contents)."""
        seen: list[str] = []

        def fake(prompt: str) -> str:
            seen.append(prompt)
            return "ok"

        client = InMemoryLLMClient(responses=fake)
        client.complete_sync("captured prompt")

        assert seen == ["captured prompt"]


# ---------------------------------------------------------------------------
# HttpLLMClient: serialization
# ---------------------------------------------------------------------------


class TestHttpClientSerialization:
    @pytest.mark.asyncio
    async def test_sends_post_to_chat_completions(self) -> None:
        """The client POSTs to ``{base_url}/v1/chat/completions``."""
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(
                200,
                json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]},
            )

        transport = httpx.MockTransport(handler)
        client = HttpLLMClient(
            settings=LLMSettings(api_key="sk-test", base_url="https://api.example.com"),
            transport=transport,
        )

        await client.complete("hi")

        assert len(captured) == 1
        assert captured[0].method == "POST"
        assert captured[0].url.path == "/v1/chat/completions"

    @pytest.mark.asyncio
    async def test_sends_authorization_header(self) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "ok"}}]},
            )

        transport = httpx.MockTransport(handler)
        client = HttpLLMClient(
            settings=LLMSettings(api_key="sk-abc", base_url="https://api.example.com"),
            transport=transport,
        )

        await client.complete("hi")

        assert captured[0].headers["Authorization"] == "Bearer sk-abc"

    @pytest.mark.asyncio
    async def test_sends_expected_request_body(self) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "ok"}}]},
            )

        transport = httpx.MockTransport(handler)
        client = HttpLLMClient(
            settings=LLMSettings(
                api_key="sk-test",
                base_url="https://api.example.com",
                model="gpt-4o-mini",
            ),
            transport=transport,
        )

        await client.complete("hello", temperature=0.3, max_tokens=512)

        body = json.loads(captured[0].content)
        assert body["model"] == "gpt-4o-mini"
        assert body["messages"] == [{"role": "user", "content": "hello"}]
        assert body["temperature"] == 0.3
        assert body["max_tokens"] == 512

    @pytest.mark.asyncio
    async def test_returns_message_content(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "the answer",
                            }
                        }
                    ]
                },
            )

        transport = httpx.MockTransport(handler)
        client = HttpLLMClient(
            settings=LLMSettings(api_key="sk-test", base_url="https://api.example.com"),
            transport=transport,
        )

        result = await client.complete("any prompt")

        assert result == "the answer"

    @pytest.mark.asyncio
    async def test_default_model_and_settings(self) -> None:
        """When ``model`` is not provided, ``gpt-4o-mini`` is used."""
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "ok"}}]},
            )

        transport = httpx.MockTransport(handler)
        settings = LLMSettings(api_key="k", base_url="https://api.example.com")
        client = HttpLLMClient(settings=settings, transport=transport)

        await client.complete("hi")

        body = json.loads(captured[0].content)
        assert body["model"] == "gpt-4o-mini"


# ---------------------------------------------------------------------------
# HttpLLMClient: error handling
# ---------------------------------------------------------------------------


class TestHttpClientErrorHandling:
    @pytest.mark.asyncio
    async def test_http_error_propagates(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
            return httpx.Response(500, json={"error": "boom"})

        transport = httpx.MockTransport(handler)
        client = HttpLLMClient(
            settings=LLMSettings(api_key="k", base_url="https://api.example.com"),
            transport=transport,
        )

        with pytest.raises(httpx.HTTPStatusError):
            await client.complete("hi")

    @pytest.mark.asyncio
    async def test_malformed_response_raises(self) -> None:
        """Missing ``choices[0].message.content`` → the client surfaces
        a clear error rather than returning ``None``."""

        def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
            return httpx.Response(200, json={"choices": []})

        transport = httpx.MockTransport(handler)
        client = HttpLLMClient(
            settings=LLMSettings(api_key="k", base_url="https://api.example.com"),
            transport=transport,
        )

        with pytest.raises((KeyError, IndexError, ValueError)):
            await client.complete("hi")

    @pytest.mark.asyncio
    async def test_uses_settings_base_url(self) -> None:
        """The base URL is taken from settings, not from a hard-coded
        default, so an OpenAI-compatible alternative deployment
        works."""
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "ok"}}]},
            )

        transport = httpx.MockTransport(handler)
        client = HttpLLMClient(
            settings=LLMSettings(
                api_key="k",
                base_url="https://my-private-llm.example.org",
            ),
            transport=transport,
        )

        await client.complete("hi")

        assert captured[0].url.host == "my-private-llm.example.org"


# ---------------------------------------------------------------------------
# LLMSettings
# ---------------------------------------------------------------------------


class TestLLMSettings:
    def test_defaults(self) -> None:
        s = LLMSettings(api_key="k", base_url="https://api.example.com")
        assert s.model == "gpt-4o-mini"

    def test_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APP_LLM_API_KEY", "env-key")
        monkeypatch.setenv("APP_LLM_BASE_URL", "https://env.example.com")
        monkeypatch.setenv("APP_LLM_MODEL", "gpt-4o")

        s = LLMSettings.from_env()

        assert s.api_key == "env-key"
        assert s.base_url == "https://env.example.com"
        assert s.model == "gpt-4o"

    def test_from_env_defaults_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("APP_LLM_API_KEY", raising=False)
        monkeypatch.delenv("APP_LLM_BASE_URL", raising=False)
        monkeypatch.delenv("APP_LLM_MODEL", raising=False)

        s = LLMSettings.from_env()

        # API key defaults to empty string so unconfigured deployments
        # produce a clear failure at request time rather than at
        # settings construction.
        assert s.api_key == ""
        assert s.base_url == "https://api.openai.com"
        assert s.model == "gpt-4o-mini"


# ---------------------------------------------------------------------------
# Protocol compatibility
# ---------------------------------------------------------------------------


class TestProtocol:
    def test_in_memory_client_satisfies_protocol(self) -> None:
        """Both clients must satisfy the LLMClient Protocol surface."""
        from job_apply.features.scoring.llm import LLMClient

        client: LLMClient = InMemoryLLMClient(responses={"a": "b"})
        # Duck-typed: presence of ``complete`` is enough. The Protocol
        # check is structural.
        assert hasattr(client, "complete")

    def test_http_client_satisfies_protocol(self) -> None:
        from job_apply.features.scoring.llm import LLMClient

        def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
            return httpx.Response(200, json={"choices": [{"message": {"content": "x"}}]})

        client: LLMClient = HttpLLMClient(
            settings=LLMSettings(api_key="k", base_url="https://api.example.com"),
            transport=httpx.MockTransport(handler),
        )
        assert hasattr(client, "complete")


# ---------------------------------------------------------------------------
# Helper to silence unused-import warnings on Mapping/Callable imports
# ---------------------------------------------------------------------------

_ = (Mapping, Callable)
