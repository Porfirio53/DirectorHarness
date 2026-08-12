from litellm import ModelResponse

from tau2.utils import llm_utils


class CapturingLogger:
    def __init__(self):
        self.debug_messages = []
        self.error_messages = []

    def debug(self, message):
        self.debug_messages.append(str(message))

    def error(self, message):
        self.error_messages.append(str(message))


def test_unmapped_model_cost_is_debug_only(monkeypatch):
    capturing_logger = CapturingLogger()
    response = ModelResponse(model="qwen3.6-plus-2026-05-26", choices=[])

    def raise_unmapped_model(**kwargs):
        raise ValueError("This model isn't mapped yet")

    monkeypatch.setattr(llm_utils, "logger", capturing_logger)
    monkeypatch.setattr(llm_utils, "completion_cost", raise_unmapped_model)

    assert llm_utils.get_response_cost(response) == 0.0
    assert len(capturing_logger.debug_messages) == 1
    assert capturing_logger.error_messages == []


def test_unexpected_cost_error_remains_error(monkeypatch):
    capturing_logger = CapturingLogger()
    response = ModelResponse(model="custom-model", choices=[])

    def raise_unexpected_error(**kwargs):
        raise RuntimeError("unexpected pricing failure")

    monkeypatch.setattr(llm_utils, "logger", capturing_logger)
    monkeypatch.setattr(llm_utils, "completion_cost", raise_unexpected_error)

    assert llm_utils.get_response_cost(response) == 0.0
    assert capturing_logger.debug_messages == []
    assert capturing_logger.error_messages == ["unexpected pricing failure"]
