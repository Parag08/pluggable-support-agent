"""LLM provider plugins: selection by LLM_PROVIDER, and the Anthropic adapter's translation.

No network: the Anthropic adapter is given a stand-in client.
"""

from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from app.adapters.llm import build_llm_client
from app.adapters.llm.anthropic_messages import AnthropicLLMClient, to_messages_api
from app.adapters.llm.openai_compatible import OpenAICompatibleLLMClient
from app.agent.llm import LLMError


class CustomPlugin:
    provider = "custom"

    def __init__(self, settings):
        self.settings = settings


def with_llm(settings, **kw):
    return settings.model_copy(update=kw)


def test_built_in_and_plugin_providers_are_selected_by_name(settings):
    s = with_llm(settings, llm_provider="openai_compatible", llm_api_key=SecretStr("k"), llm_base_url="http://x/v1")
    assert isinstance(build_llm_client(s), OpenAICompatibleLLMClient)

    s = with_llm(settings, llm_provider="anthropic", llm_api_key=SecretStr("k"), llm_base_url="")
    assert isinstance(build_llm_client(s), AnthropicLLMClient)

    plugin = build_llm_client(with_llm(settings, llm_provider="tests.test_llm_providers:CustomPlugin"))
    assert isinstance(plugin, CustomPlugin) and plugin.provider == "custom"


def test_unknown_provider_is_rejected(settings):
    with pytest.raises(RuntimeError, match="Unknown LLM_PROVIDER"):
        build_llm_client(with_llm(settings, llm_provider="nonsense"))


def test_openai_compatible_requires_a_key(settings):
    with pytest.raises(RuntimeError, match="LLM_API_KEY"):
        build_llm_client(with_llm(settings, llm_provider="openai_compatible", llm_api_key=SecretStr("")))


def test_messages_are_translated_to_the_messages_api():
    system, messages = to_messages_api([
        {"role": "system", "content": "be strict"},
        {"role": "user", "content": [
            {"type": "text", "text": "what is this?"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ]},
    ])
    assert system == "be strict"
    assert messages == [{"role": "user", "content": [
        {"type": "text", "text": "what is this?"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}},
    ]}]


class FakeMessages:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response, error, []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.response


def _response(stop_reason="end_turn"):
    usage = SimpleNamespace(input_tokens=100, cache_read_input_tokens=50, cache_creation_input_tokens=0,
                            output_tokens=20)
    content = [SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text='{"reply": "hi"}')]
    return SimpleNamespace(content=content, stop_reason=stop_reason, model="claude-opus-5-5", usage=usage,
                           model_dump=lambda: {})


def _client(settings, messages, effort="low"):
    s = with_llm(settings, llm_provider="anthropic", llm_effort=effort, llm_json_mode="json_schema")
    return AnthropicLLMClient(s, client=SimpleNamespace(messages=messages))


def _complete(client):
    return client.complete(model="claude-opus-5-5", messages=[{"role": "system", "content": "sys"},
                                                               {"role": "user", "content": "hello"}],
                           max_tokens=4000, temperature=0.3, json_schema={"type": "object"}, schema_name="x")


def test_anthropic_request_and_result(settings):
    messages = FakeMessages(_response())
    result = _complete(_client(settings, messages))

    call = messages.calls[0]
    assert call["output_config"] == {"format": {"type": "json_schema", "schema": {"type": "object"}}, "effort": "low"}
    assert call["system"][0]["text"] == "sys" and "temperature" not in call
    assert result.content == '{"reply": "hi"}' and result.finish_reason == "stop"
    assert result.input_tokens == 150 and result.output_tokens == 20


@pytest.mark.parametrize("stop_reason, normalised", [("max_tokens", "length"), ("refusal", "content_filter")])
def test_anthropic_stop_reasons_are_normalised(settings, stop_reason, normalised):
    assert _complete(_client(settings, FakeMessages(_response(stop_reason)))).finish_reason == normalised


def test_anthropic_api_errors_become_llm_errors(settings):
    import anthropic

    error = anthropic.APIConnectionError(request=SimpleNamespace(method="POST", url="https://x"))
    with pytest.raises(LLMError):
        _complete(_client(settings, FakeMessages(error=error)))
