from __future__ import annotations

import pytest

from scripts.compose_runtime_acceptance import assert_completed, lease_from, task_id_from


def test_task_id_extraction_requires_a_nonempty_task_id():
    assert task_id_from({"task": {"id": "task-123"}}) == "task-123"
    with pytest.raises(AssertionError):
        task_id_from({"task": {"id": ""}})
    with pytest.raises(AssertionError):
        task_id_from({"task": None})


def test_lease_extraction_validates_connector_lease_fields():
    assert lease_from({"lease": {"lease_token": "x" * 24, "lease_version": 2}})["lease_version"] == 2
    with pytest.raises(AssertionError):
        lease_from({"lease": {"lease_token": "short"}})


def test_completed_assertion_rejects_nonterminal_task():
    assert_completed({"task": {"status": {"state": "completed"}}})
    with pytest.raises(AssertionError):
        assert_completed({"task": {"status": {"state": "working"}}})
