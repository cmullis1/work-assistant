"""Cost estimate and actual cost for Message Batches, at each model's prices (`reviewer.catalog`).

Pure functions apart from `output_tokens_for` and `approx_cost_per_call`, which read past reviews. No network.
"""
from __future__ import annotations

import sqlite3

from work_assistant.reviewer.catalog import get_model

IMAGE_TOKENS = 1600
BATCH_DISCOUNT = 0.5
# A model's measured output tokens per call replace its default once it has this many ok reviews.
MEASURED_AFTER = 10
# What a call cost on average in the owner's first real runs (Opus 5.5 at medium effort), the base of
# the approximate cost per call shown for a model with too few reviews of its own.
OPUS_COST_PER_CALL = 0.068
OPUS_MODEL = "claude-opus-5-5"


def count_tokens(chars: int) -> int:
    """Estimate tokens for a text: ceil(chars / 3.5), computed in integers."""
    # ceil(chars / 3.5) == ceil(2 * chars / 7) == (2 * chars + 6) // 7 for chars >= 0.
    return (chars * 2 + 6) // 7


def actual_cost(usage: dict, model_id: str) -> float:
    """USD cost of one request from its usage counts. Missing or None counts are 0. Not rounded."""
    rates = get_model(model_id).rates

    def count(key: str) -> int:
        return usage.get(key) or 0

    per_mtok = (
        count("input_tokens") * rates["input"]
        + count("output_tokens") * rates["output"]
        + count("cache_read_input_tokens") * rates["cache_read"]
        + count("cache_creation_input_tokens") * rates["cache_write_1h"]
    )
    return BATCH_DISCOUNT * per_mtok / 1e6


def estimate_cost(system_chars: int, contact_chars: list[int], image_counts: list[int], model_id: str,
                  output_tokens: int) -> float:
    """Pre-run USD estimate for a batch: system prompt cached once, `output_tokens` assumed per contact."""
    if len(contact_chars) != len(image_counts):
        raise ValueError("contact_chars and image_counts must have the same length")
    if not contact_chars:
        return 0.0

    rates = get_model(model_id).rates
    system_tokens = count_tokens(system_chars)
    contacts = len(contact_chars)
    per_mtok = system_tokens * rates["cache_write_1h"]
    per_mtok += (contacts - 1) * system_tokens * rates["cache_read"]
    for chars, images in zip(contact_chars, image_counts, strict=True):
        per_mtok += (count_tokens(chars) + images * IMAGE_TOKENS) * rates["input"]
    per_mtok += contacts * output_tokens * rates["output"]
    return round(BATCH_DISCOUNT * per_mtok / 1e6, 2)


def output_tokens_for(conn: sqlite3.Connection, model_id: str, effort: str | None) -> int:
    """The output tokens per call an estimate assumes for this model at this effort.

    Its measured average at that effort x 1.25, rounded up, once it has 10 or more ok reviews at that
    effort with a recorded count (from any kind of run); otherwise the model's default. Measured per
    effort because higher effort writes more: an average learned at medium would put a max-effort
    estimate under what it costs. `effort` None matches reviews whose effort was not recorded. Code-only
    reviews and reviews stored before counts were recorded have none, so they never count.
    """
    measured, total = conn.execute(
        "SELECT COUNT(output_tokens), COALESCE(SUM(output_tokens), 0) FROM reviews "
        "WHERE status = 'ok' AND model = ? AND effort IS ? AND output_tokens IS NOT NULL",
        (model_id, effort),
    ).fetchone()
    if measured < MEASURED_AFTER:
        return get_model(model_id).default_output_tokens
    # The average padded 25% so estimates stay on the high side: ceil(total / measured x 5/4), in
    # integers so 8000 x 1.25 is exactly 10000.
    return -(-total * 5 // (measured * 4))


def approx_cost_per_call(conn: sqlite3.Connection, model_id: str, effort: str | None) -> tuple[float, bool]:
    """The rough USD cost of reviewing one call, to the cent, and whether it was measured.

    Measured: the average cost of the model's ok reviews at this effort, by the same rule as
    `output_tokens_for` (10 or more with recorded output tokens, so code-only reviews, which cost
    nothing, never count). Otherwise the about $0.068 a call measured on Opus 5.5 at medium, scaled by
    the model's output price.
    """
    measured, average = conn.execute(
        "SELECT COUNT(*), AVG(cost_usd) FROM reviews WHERE status = 'ok' AND model = ? AND effort IS ? "
        "AND output_tokens IS NOT NULL AND cost_usd IS NOT NULL",
        (model_id, effort),
    ).fetchone()
    if measured >= MEASURED_AFTER:
        return round(average, 2), True
    scale = get_model(model_id).rates["output"] / get_model(OPUS_MODEL).rates["output"]
    return round(OPUS_COST_PER_CALL * scale, 2), False
