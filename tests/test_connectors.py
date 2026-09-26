import pytest

from jobagent.connectors import backoff, parse_json


def test_parse_json_handles_think_blocks_and_fences():
    assert parse_json('<think>hmm</think>```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json('Sure! {"a": {"b": 2}} done') == {"a": {"b": 2}}


def test_parse_json_rejects_non_objects():
    with pytest.raises(ValueError):
        parse_json("no json here")
    with pytest.raises(ValueError):
        parse_json("[1, 2]")


def test_backoff_grows_and_caps():
    assert backoff(1) == 30
    assert backoff(2) == 60
    assert backoff(50) == 1800
