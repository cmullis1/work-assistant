"""The models that can review calls, with their Message Batches list prices. Pure data, no network.

Rates are USD per million tokens before the Batch API discount. Cache writes cost 2x input (the
1-hour cache). `default_output_tokens` is the output assumed per call until a model has enough
reviews of its own to measure (`costs.output_tokens_for`); it errs high on purpose. `max_tokens` is the
output limit each request is sent with (`reviewer.batch.build_request`).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ReviewModel:
    id: str
    label: str
    rates: dict[str, float]
    default_output_tokens: int
    max_tokens: int


def _rates(input_: float, output: float, cache_read: float) -> dict[str, float]:
    return {"input": input_, "output": output, "cache_read": cache_read, "cache_write_1h": 2 * input_}


MODELS: dict[str, ReviewModel] = {
    model.id: model
    for model in (
        # The owner's first two real runs (17 calls each) cost about $0.068 a call against an estimate
        # of about $0.050 with 4000 output tokens assumed; 6000 puts both estimates above what they cost.
        ReviewModel("claude-opus-5-5", "Claude Opus 5.5", _rates(4.00, 20.00, 0.20), 6000, 16000),
        # Unmeasured here, and their thinking and effort levels differ from Opus: padded further.
        ReviewModel("claude-sonnet-5-5", "Claude Sonnet 5.5", _rates(2.00, 10.00, 0.20), 10000, 16000),
        # Fable always thinks, so at high effort 16000 would cut its output off more often.
        ReviewModel("claude-fable-5-1", "Claude Fable 5.1", _rates(10.00, 50.00, 0.25), 10000, 32000),
    )
}
DEFAULT_MODEL = "claude-opus-5-5"
FABLE_MODEL = "claude-fable-5-1"   # needs 30-day data retention at Anthropic (spec §5.1)


def get_model(model_id: str) -> ReviewModel:
    """The catalog entry for `model_id`; KeyError for a model not in the catalog."""
    return MODELS[model_id]
