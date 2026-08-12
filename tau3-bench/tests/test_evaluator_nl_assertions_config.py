import argparse
import json

import pytest

from tau2.cli import add_run_args
from tau2.config import DEFAULT_LLM_NL_ASSERTIONS, DEFAULT_LLM_NL_ASSERTIONS_ARGS
from tau2.data_model.message import AssistantMessage, UserMessage
from tau2.evaluator import evaluator_nl_assertions
from tau2.evaluator.evaluator_nl_assertions import (
    NLAssertionsEvaluator,
    set_nl_assertion_llm_config,
)


@pytest.fixture(autouse=True)
def reset_nl_assertion_llm_config():
    yield
    set_nl_assertion_llm_config(
        DEFAULT_LLM_NL_ASSERTIONS,
        DEFAULT_LLM_NL_ASSERTIONS_ARGS,
    )


def test_cli_parses_nl_assertion_llm_config():
    parser = argparse.ArgumentParser()
    add_run_args(parser)

    args = parser.parse_args(
        [
            "--domain",
            "retail",
            "--nl-assertion-llm",
            "openai/custom-judge",
            "--nl-assertion-llm-args",
            '{"temperature":0,"extra_body":{"enable_thinking":false}}',
        ]
    )

    assert args.nl_assertion_llm == "openai/custom-judge"
    assert args.nl_assertion_llm_args == {
        "temperature": 0,
        "extra_body": {"enable_thinking": False},
    }


def test_evaluator_passes_config_to_generate(monkeypatch):
    captured = {}

    def fake_generate(*, model, messages, call_name, **kwargs):
        captured.update(
            model=model,
            messages=messages,
            call_name=call_name,
            kwargs=kwargs,
        )
        return AssistantMessage(
            role="assistant",
            content=json.dumps(
                {
                    "results": [
                        {
                            "expectedOutcome": "Agent greets the user",
                            "reasoning": "The agent said hello.",
                            "metExpectation": True,
                        }
                    ]
                }
            ),
        )

    monkeypatch.setattr(evaluator_nl_assertions, "generate", fake_generate)
    llm_args = {
        "temperature": 0.0,
        "api_base": "https://example.test/v1",
        "extra_body": {"enable_thinking": False},
    }
    set_nl_assertion_llm_config("openai/custom-judge", llm_args)

    checks = NLAssertionsEvaluator.evaluate_nl_assertions(
        [UserMessage(role="user", content="Hello")],
        ["Agent greets the user"],
    )

    assert captured["model"] == "openai/custom-judge"
    assert captured["call_name"] == "nl_assertions_eval"
    assert captured["kwargs"] == llm_args
    assert len(captured["messages"]) == 2
    assert len(checks) == 1
    assert checks[0].met is True


def test_config_is_copied_before_use(monkeypatch):
    captured = {}

    def fake_generate(*, model, messages, call_name, **kwargs):
        captured.update(model=model, kwargs=kwargs)
        return AssistantMessage(role="assistant", content='{"results":[]}')

    monkeypatch.setattr(evaluator_nl_assertions, "generate", fake_generate)
    llm_args = {"temperature": 0.0}
    set_nl_assertion_llm_config("openai/custom-judge", llm_args)
    llm_args["temperature"] = 1.0

    NLAssertionsEvaluator.evaluate_nl_assertions(
        [UserMessage(role="user", content="Hello")],
        ["Agent greets the user"],
    )

    assert captured["kwargs"] == {"temperature": 0.0}


@pytest.mark.parametrize("model", ["", None])
def test_config_rejects_empty_model(model):
    with pytest.raises(ValueError, match="must not be empty"):
        set_nl_assertion_llm_config(model, {})


def test_config_rejects_non_dict_args():
    with pytest.raises(TypeError, match="must be a dictionary"):
        set_nl_assertion_llm_config("openai/custom-judge", [])
