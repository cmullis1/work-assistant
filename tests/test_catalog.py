import pytest

from work_assistant.reviewer.catalog import DEFAULT_MODEL, MODELS, get_model


def test_catalog_values():
    assert list(MODELS) == ["claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1"]
    assert DEFAULT_MODEL == "claude-opus-5-5"
    assert {m.id: m.label for m in MODELS.values()} == {
        "claude-opus-5-5": "Claude Opus 5.5",
        "claude-sonnet-5-5": "Claude Sonnet 5.5",
        "claude-fable-5-1": "Claude Fable 5.1",
    }
    keys = ("input", "output", "cache_read", "cache_write_1h")
    assert {m.id: tuple(m.rates[k] for k in keys) for m in MODELS.values()} == {
        "claude-opus-5-5": (4.00, 20.00, 0.20, 8.00),
        "claude-sonnet-5-5": (2.00, 10.00, 0.20, 4.00),
        "claude-fable-5-1": (10.00, 50.00, 0.25, 20.00),
    }
    assert all(set(m.rates) == set(keys) for m in MODELS.values())
    assert {m.id: m.default_output_tokens for m in MODELS.values()} == {
        "claude-opus-5-5": 6000, "claude-sonnet-5-5": 10000, "claude-fable-5-1": 10000,
    }
    assert {m.id: m.max_tokens for m in MODELS.values()} == {
        "claude-opus-5-5": 16000, "claude-sonnet-5-5": 16000, "claude-fable-5-1": 32000,
    }
    assert all(m.id == model_id for model_id, m in MODELS.items())


def test_get_model():
    assert get_model("claude-sonnet-5-5") is MODELS["claude-sonnet-5-5"]
    with pytest.raises(KeyError):
        get_model("claude-haiku-5-5")
