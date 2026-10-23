import pytest

from work_assistant.costs import (BATCH_DISCOUNT, actual_cost, approx_cost_per_call, count_tokens, estimate_cost,
                            output_tokens_for)
from work_assistant.reviewer.catalog import get_model
from factories import add_run

OPUS, SONNET, FABLE = "claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1"


def test_batch_discount():
    assert BATCH_DISCOUNT == 0.5


def test_actual_cost_exact():
    usage = {
        "input_tokens": 10000,
        "output_tokens": 3000,
        "cache_read_input_tokens": 7000,
        "cache_creation_input_tokens": 0,
    }
    assert actual_cost(usage, OPUS) == pytest.approx(0.5 * (10000 * 4 + 3000 * 20 + 7000 * 0.2) / 1e6)
    assert actual_cost(usage, OPUS) == pytest.approx(0.0507)
    assert actual_cost(usage, FABLE) == pytest.approx(0.5 * (10000 * 10 + 3000 * 50 + 7000 * 0.25) / 1e6)


def test_actual_cost_per_model():
    # No cache reads: their price is not a fixed multiple of Opus's (Fable 0.25, Sonnet 0.20).
    usage = {"input_tokens": 10000, "output_tokens": 3000, "cache_creation_input_tokens": 2000}
    opus = actual_cost(usage, OPUS)
    assert actual_cost(usage, FABLE) == pytest.approx(2.5 * opus)
    assert actual_cost(usage, SONNET) == pytest.approx(0.5 * opus)


def test_actual_cost_unknown_model_raises():
    with pytest.raises(KeyError):
        actual_cost({"input_tokens": 1}, "claude-haiku-5-5")


def test_actual_cost_missing_and_none_count_as_zero():
    assert actual_cost({}, OPUS) == 0.0
    usage = {"input_tokens": 10000, "output_tokens": None, "cache_creation_input_tokens": 1000}
    assert actual_cost(usage, OPUS) == pytest.approx(0.5 * (10000 * 4 + 1000 * 8) / 1e6)


def test_actual_cost_is_not_rounded():
    usage = {"input_tokens": 1}
    assert actual_cost(usage, OPUS) == pytest.approx(0.5 * 4 / 1e6)
    assert actual_cost(usage, OPUS) != 0.0


def test_estimate_exact():
    # system 10000 tok x 8 (cache write) + 10000 x 0.2 (one cache read) = 82000
    # contacts (10000 + 10000 + 1600 image) tok x 4 = 86400; output 2 x 6000 x 20 = 240000
    # 408400 per MTok -> 0.5 x 0.4084 = 0.2042 -> 0.20
    assert estimate_cost(35000, [35000, 35000], [0, 1], OPUS, 6000) == 0.20


def test_estimate_uses_the_model_rates_and_output_tokens():
    # Sonnet: system 10000 x 4 + 10000 x 0.2 = 42000; contacts 21600 x 2 = 43200; output 2 x 10000 x 10
    # = 200000; 285200 per MTok -> 0.5 x 0.2852 = 0.1426 -> 0.14
    assert estimate_cost(35000, [35000, 35000], [0, 1], SONNET, 10000) == 0.14
    # Fable: 10000 x 20 + 10000 x 0.25 = 202500; 21600 x 10 = 216000; 2 x 10000 x 50 = 1000000
    # 1418500 per MTok -> 0.70925 -> 0.71
    assert estimate_cost(35000, [35000, 35000], [0, 1], FABLE, 10000) == 0.71


def test_estimate_empty_week():
    assert estimate_cost(35000, [], [], OPUS, 6000) == 0.0


def test_count_tokens_rounds_up():
    assert count_tokens(0) == 0
    assert count_tokens(1) == 1  # ceil(1 / 3.5)
    assert count_tokens(7) == 2  # ceil(7 / 3.5) exactly
    assert count_tokens(8) == 3  # ceil(8 / 3.5)
    assert count_tokens(35000) == 10000


def test_estimate_single_contact_no_cache_read():
    # system 10000 tok x 8 + contact 10000 tok x 4 + output 6000 x 20 = 240000 per MTok -> 0.5 x 0.24 = 0.12
    assert estimate_cost(35000, [35000], [0], OPUS, 6000) == 0.12


def test_estimate_covers_the_measured_runs():
    """The owner's first two real runs: 17 requests each, estimated $0.84 and $0.86 with 4000 output
    tokens assumed, billed $1.1437 and $1.1626. Opus's default assumption must have covered what they cost."""
    opus = get_model(OPUS)
    raised = (opus.default_output_tokens - 4000) * opus.rates["output"] * BATCH_DISCOUNT / 1e6 * 17
    for old_estimate, actual in ((0.84, 1.1437), (0.86, 1.1626)):
        assert old_estimate - 0.005 + raised >= actual  # the old estimate was rounded to cents


# -- the learned output tokens per call ------------------------------------------------------------


def _reviews(conn, model, tokens, status="ok", effort="medium", cost=None):
    run_id = add_run(conn)
    for i, count in enumerate(tokens):
        ticket_id = run_id * 1000 + i
        conn.execute(
            "INSERT INTO contacts (ticket_id, channel, call_started_at, agent_email, agent_name, attribution_ok, "
            "snapshot, fetched_at) VALUES (?, 'voice', '2026-10-05T10:00:00Z', 'a@x.com', 'A', 1, '{}', 'x')",
            (ticket_id,))
        conn.execute(
            "INSERT INTO reviews (run_id, ticket_id, rubric_version_id, status, is_current, created_at, model, "
            "effort, output_tokens, cost_usd) VALUES (?, ?, 1, ?, 0, 'x', ?, ?, ?, ?)",
            (run_id, ticket_id, status, model, effort, count, cost))
    conn.commit()


def test_output_tokens_for(conn):
    _reviews(conn, SONNET, [8000] * 9)
    assert output_tokens_for(conn, SONNET, "medium") == 10000          # under 10 reviews: the default
    _reviews(conn, SONNET, [3000] * 30, status="error")                # errors never count
    assert output_tokens_for(conn, SONNET, "medium") == 10000
    _reviews(conn, SONNET, [8000])
    assert output_tokens_for(conn, SONNET, "medium") == 10000          # 10 averaging 8000: 8000 x 1.25
    _reviews(conn, OPUS, [3000, 5000] * 5)
    assert output_tokens_for(conn, OPUS, "medium") == 5000             # 10 averaging 4000: 4000 x 1.25
    assert output_tokens_for(conn, FABLE, "medium") == 10000           # none: the default


def test_output_tokens_for_rounds_up_and_ignores_unmeasured(conn):
    _reviews(conn, OPUS, [1001] + [1000] * 9)
    assert output_tokens_for(conn, OPUS, "medium") == 1251             # 1000.1 x 1.25 = 1250.125, rounded up
    _reviews(conn, FABLE, [None] * 10 + [9000] * 9)                    # code-only and older reviews have no count
    assert output_tokens_for(conn, FABLE, "medium") == 10000


def test_output_tokens_for_is_learned_per_effort(conn):
    """Medium-effort output must never set the assumption for max effort, which writes more."""
    _reviews(conn, OPUS, [2000] * 10, effort="medium")
    assert output_tokens_for(conn, OPUS, "medium") == 2500
    assert output_tokens_for(conn, OPUS, "max") == 6000                # nothing measured at max yet
    _reviews(conn, OPUS, [8000] * 10, effort="max")
    assert output_tokens_for(conn, OPUS, "max") == 10000
    assert output_tokens_for(conn, OPUS, "medium") == 2500
    _reviews(conn, OPUS, [4000] * 10, effort=None)                     # a run from before effort was recorded
    assert output_tokens_for(conn, OPUS, None) == 5000
    assert output_tokens_for(conn, OPUS, "medium") == 2500


# -- the approximate cost per call shown beside the Model choice -----------------------------------


def test_approx_cost_per_call_scaled_until_measured(conn):
    """About $0.068 a call on Opus at medium, scaled by each model's output price, to the cent."""
    assert approx_cost_per_call(conn, OPUS, "medium") == (0.07, False)
    assert approx_cost_per_call(conn, SONNET, "medium") == (0.03, False)
    assert approx_cost_per_call(conn, FABLE, "medium") == (0.17, False)


def test_approx_cost_per_call_measured_at_the_effort(conn):
    _reviews(conn, SONNET, [3000] * 9, cost=0.05)
    assert approx_cost_per_call(conn, SONNET, "medium") == (0.03, False)      # 9 is not enough
    _reviews(conn, SONNET, [None] * 5, cost=0.0)                             # code-only reviews cost nothing
    _reviews(conn, SONNET, [3000] * 5, status="error", cost=0.01)           # errors never count
    _reviews(conn, SONNET, [3000], cost=0.04)
    assert approx_cost_per_call(conn, SONNET, "medium") == (0.05, True)      # (9 x 0.05 + 0.04) / 10 = 0.049
    assert approx_cost_per_call(conn, SONNET, "max") == (0.03, False)        # measured at medium only
    assert approx_cost_per_call(conn, OPUS, "medium") == (0.07, False)
