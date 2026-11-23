from copy import deepcopy

from scripts.workbuddy_desktop_evidence import REQUIRED_EVENTS, validate_evidence


def valid_evidence():
    return {
        "schema_version": "1.0",
        "evidence_type": "workbuddy_desktop_connection",
        "client": {"product": "WorkBuddy Desktop", "version": "4.23.0"},
        "connector": {"id": "workbuddy-personal"},
        "hub": {"origin": "https://hub.example.com", "environment": "staging"},
        "session": {
            "id": "session-001",
            "started_at": "2026-11-23T10:00:00+08:00",
            "completed_at": "2026-11-23T10:02:00+08:00",
        },
        "events": [
            {
                "name": name,
                "status": "passed",
                "observed_at": f"2026-11-23T10:00:{index:02d}+08:00",
                "source": "hub_receipt" if name.endswith(("accepted", "online")) else "desktop_ui",
                "evidence_ref": f"receipt:{index}",
            }
            for index, name in enumerate(REQUIRED_EVENTS, start=1)
        ],
        "attestation": {"method": "operator_attestation", "confirmed_by_operator": True},
    }


def test_valid_operator_attested_manifest_passes():
    assert validate_evidence(valid_evidence()) == []


def test_non_object_root_fails():
    assert validate_evidence([])


def test_secret_fields_are_rejected_without_echoing_values():
    data = valid_evidence()
    data["connector"]["token"] = "never-print-this-secret"
    errors = validate_evidence(data)
    assert any("疑似凭据" in error for error in errors)
    assert all("never-print-this-secret" not in error for error in errors)


def test_unknown_fields_fail_closed():
    data = valid_evidence()
    data["extra"] = "unexpected"
    assert any("不支持" in error for error in validate_evidence(data))


def test_remote_http_is_rejected():
    data = valid_evidence()
    data["hub"]["origin"] = "http://hub.example.com"
    assert validate_evidence(data)


def test_loopback_http_requires_explicit_opt_in():
    data = valid_evidence()
    data["hub"]["origin"] = "http://127.0.0.1:8000"
    assert validate_evidence(data)
    assert validate_evidence(data, allow_loopback_http=True) == []


def test_hub_url_with_userinfo_query_or_path_is_rejected():
    for url in (
        "https://user:pass@hub.example.com",
        "https://hub.example.com?token=abc",
        "https://hub.example.com/api",
    ):
        data = valid_evidence()
        data["hub"]["origin"] = url
        assert validate_evidence(data)


def test_events_must_be_complete_and_ordered():
    data = valid_evidence()
    data["events"].reverse()
    assert validate_evidence(data)


def test_failed_event_is_rejected():
    data = valid_evidence()
    data["events"][2]["status"] = "failed"
    assert validate_evidence(data)


def test_timezone_is_required_and_completion_cannot_precede_start():
    data = valid_evidence()
    data["session"]["started_at"] = "2026-11-23T10:00:00"
    assert validate_evidence(data)
    data = valid_evidence()
    data["session"]["completed_at"] = "2026-11-23T09:59:00+08:00"
    assert validate_evidence(data)


def test_attestation_must_be_explicit():
    data = valid_evidence()
    data["attestation"]["confirmed_by_operator"] = False
    assert validate_evidence(data)


def test_no_task_claim_or_execution_is_part_of_connection_proof():
    data = valid_evidence()
    data["events"].append({
        "name": "task_claimed", "status": "passed", "observed_at": "2026-11-23T10:01:00+08:00",
        "source": "hub_receipt", "evidence_ref": "receipt:task",
    })
    assert validate_evidence(data)


def test_event_timestamps_must_follow_declared_event_order():
    data = valid_evidence()
    data["events"][1]["observed_at"] = "2026-11-23T09:59:59+08:00"
    assert any("单调递增" in error for error in validate_evidence(data))


def test_event_timestamps_must_fall_within_session_window():
    data = valid_evidence()
    data["events"][0]["observed_at"] = "2026-11-23T09:59:59+08:00"
    assert any("session 时间范围" in error for error in validate_evidence(data))

    data = valid_evidence()
    data["events"][-1]["observed_at"] = "2026-11-23T10:02:01+08:00"
    assert any("session 时间范围" in error for error in validate_evidence(data))


def test_equal_adjacent_event_timestamps_are_allowed():
    data = valid_evidence()
    data["events"][1]["observed_at"] = data["events"][0]["observed_at"]
    assert validate_evidence(data) == []



def test_session_window_endpoints_are_inclusive():
    data = valid_evidence()
    data["events"][0]["observed_at"] = data["session"]["started_at"]
    data["events"][-1]["observed_at"] = data["session"]["completed_at"]
    assert validate_evidence(data) == []


def test_mixed_timezone_offsets_are_compared_as_absolute_instants():
    data = valid_evidence()
    # 02:00:01Z and 10:00:01+08:00 represent the same absolute instant.
    data["events"][1]["observed_at"] = "2026-11-23T02:00:01Z"
    data["events"][2]["observed_at"] = "2026-11-23T10:00:02+08:00"
    assert validate_evidence(data) == []


def test_all_equal_event_timestamps_are_allowed_within_session():
    data = valid_evidence()
    same = data["session"]["started_at"]
    for event in data["events"]:
        event["observed_at"] = same
    assert validate_evidence(data) == []


def test_invalid_session_window_reports_root_cause_without_cascading_event_errors():
    data = valid_evidence()
    data["session"]["completed_at"] = "2026-11-23T09:59:00+08:00"
    errors = validate_evidence(data)
    assert errors == ["session completed_at 不能早于 started_at"]
