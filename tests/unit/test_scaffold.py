from fastapi.testclient import TestClient

from prospectpilot.api.app import app
from prospectpilot.config import Settings
from prospectpilot.models import EmailSequence, strip_markers


def test_settings_defaults() -> None:
    s = Settings(_env_file=None)
    assert s.anthropic_large_model == "claude-sonnet-5-5"
    assert s.anthropic_small_model == "claude-haiku-4-5-20251001"
    assert s.price_for("claude-sonnet-5-5").output == 10.0
    assert s.price_for("qwen2.5").input == 0.0
    assert s.libpq_database_url.startswith("postgresql://")


def test_metrics_endpoint() -> None:
    with TestClient(app) as client:
        r = client.get("/metrics")
    assert r.status_code == 200
    assert "pp_llm_calls_total" in r.text


def test_strip_markers() -> None:
    assert strip_markers("Saw your launch [fact:12]. Nice [fact:3] !") == "Saw your launch. Nice!"


def test_sequence_requires_three_steps() -> None:
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        EmailSequence.model_validate({"emails": [{"step": 1, "subject": "a", "body": "b"}]})
    seq = EmailSequence.model_validate(
        {"emails": [{"step": s, "subject": "s", "body": f"b [fact:{s}]"} for s in (3, 1, 2)]}
    )
    assert [e.step for e in seq.emails] == [1, 2, 3]
    assert seq.fact_ids() == [1, 2, 3]
