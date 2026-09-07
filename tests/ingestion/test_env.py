"""`SEC_USER_AGENT` is a hard requirement, not a nice-to-have.

SEC and Wikipedia both 403 the default urllib User-Agent, so an unset value has to fail
where it is read rather than surface later as an opaque HTTP error from a worker thread.
"""

import pytest

from src.utils import env


def test_the_user_agent_is_returned_when_it_is_set(monkeypatch):
    monkeypatch.setattr(env, "load_env", lambda: None)
    monkeypatch.setenv("SEC_USER_AGENT", "AURUM-Project you@example.com")

    assert env.get_sec_user_agent() == "AURUM-Project you@example.com"


@pytest.mark.parametrize("value", [None, ""])
def test_a_missing_user_agent_raises_and_says_what_to_set(monkeypatch, value):
    monkeypatch.setattr(env, "load_env", lambda: None)
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    if value is not None:
        monkeypatch.setenv("SEC_USER_AGENT", value)

    with pytest.raises(ValueError, match="SEC_USER_AGENT is not set"):
        env.get_sec_user_agent()
