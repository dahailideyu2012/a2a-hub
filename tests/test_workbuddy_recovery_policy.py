"""T92-T98 bounded WorkBuddy connection recovery policy tests."""
from __future__ import annotations
import math
import pytest
from a2a_hub.workbuddy_runtime import recovery_advice

def test_online_connection_needs_no_recovery():
    result = recovery_advice({"connection_state": "online", "reconnect_attempt": 2}, now=100)
    assert result["action"] == "none"
    assert result["retry_allowed"] is False
    assert result["retry_after_seconds"] == 0

def test_degraded_connection_recommends_probe_without_reconnect():
    result = recovery_advice({"connection_state": "degraded"}, now=100)
    assert result["action"] == "heartbeat_probe"
    assert result["retry_allowed"] is False

@pytest.mark.parametrize("error", ["authentication_failed", "protocol_mismatch"])
def test_fatal_errors_require_operator_intervention(error):
    result = recovery_advice({"connection_state": "offline", "last_error_code": error}, now=100)
    assert result["action"] == "operator_intervention"
    assert result["retry_allowed"] is False
    assert result["reason"] == error

def test_offline_recovery_uses_bounded_deterministic_backoff():
    first = recovery_advice({"connection_state": "offline", "reconnect_attempt": 0}, now=100)
    second = recovery_advice({"connection_state": "offline", "reconnect_attempt": 1}, now=100)
    capped = recovery_advice({"connection_state": "offline", "reconnect_attempt": 20}, now=100, max_attempts=30, base_seconds=2, max_seconds=10)
    assert first["retry_after_seconds"] == 2 and first["retry_at"] == 102
    assert second["retry_after_seconds"] == 4
    assert capped["retry_after_seconds"] == 10
    assert first["automatic_actions_enabled"] is False

def test_retry_budget_exhaustion_stops_retries():
    result = recovery_advice({"connection_state": "offline", "reconnect_attempt": 8}, now=100, max_attempts=8)
    assert result["action"] == "operator_intervention"
    assert result["retry_allowed"] is False
    assert result["attempts_remaining"] == 0

@pytest.mark.parametrize("snapshot", [None, [], {"connection_state": "unknown"}, {"connection_state": "offline", "reconnect_attempt": True}, {"connection_state": "offline", "reconnect_attempt": -1}, {"connection_state": "offline", "last_error_code": "raw secret"}])
def test_invalid_recovery_inputs_fail_closed(snapshot):
    with pytest.raises(ValueError):
        recovery_advice(snapshot, now=100)

def test_non_finite_time_and_invalid_retry_policy_fail_closed():
    with pytest.raises(ValueError, match="finite"):
        recovery_advice({"connection_state": "offline"}, now=math.inf)
    with pytest.raises(ValueError):
        recovery_advice({"connection_state": "offline"}, now=100, max_attempts=0)
    with pytest.raises(ValueError):
        recovery_advice({"connection_state": "offline"}, now=100, base_seconds=-1)
