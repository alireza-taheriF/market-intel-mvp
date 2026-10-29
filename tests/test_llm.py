"""LLM client: OpenAI response handling and error mapping (fake SDK client, no network)."""
from types import SimpleNamespace

import pytest

from market_intel.ai.llm import LLMClient
from market_intel.core.config import Settings
from market_intel.core.errors import InvalidResponseError, LLMUnavailableError


def _response(content, finish_reason="stop", refusal=None):
    msg = SimpleNamespace(content=content, refusal=refusal)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason=finish_reason)])


class FakeOpenAI:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response, error, []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.response


def client_with(fake):
    llm = LLMClient(Settings(llm_provider_setting="openai"))
    llm._client = fake
    return llm


def test_openai_json_answer_is_parsed_and_requested_as_json():
    fake = FakeOpenAI(_response('{"markets": []}'))
    assert client_with(fake).complete_json("Return JSON") == {"markets": []}
    assert fake.calls[0]["response_format"] == {"type": "json_object"}
    assert fake.calls[0]["messages"][0]["role"] == "system"


@pytest.mark.parametrize("resp", [_response("", "length"), _response(None, "content_filter"),
                                  _response("", refusal="I can't help"), SimpleNamespace(choices=[])])
def test_openai_truncated_refused_or_empty_answers_are_rejected(resp):
    with pytest.raises(InvalidResponseError):
        client_with(FakeOpenAI(resp)).complete_json("x")


def test_openai_sdk_errors_become_typed_errors():
    import openai
    with pytest.raises(LLMUnavailableError):
        client_with(FakeOpenAI(error=openai.OpenAIError("Missing credentials"))).complete_json("x")
