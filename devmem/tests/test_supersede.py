import pytest

from devmem.db import plan_supersede


def test_supersede_keeps_history_link_and_reason():
    old = {
        "id": 7,
        "status": "active",
        "kind": "decision",
        "project_id": 1,
        "task_id": 4,
        "refs": ["src/app.py"],
    }
    planned = plan_supersede(old, "Use the config constant.", "moved the number into config")
    assert planned["old_status"] == "superseded"
    assert planned["old_id"] == 7
    assert planned["kind"] == "decision"
    assert planned["project_id"] == 1
    assert planned["task_id"] == 4
    assert planned["content"] == "Use the config constant."
    assert planned["refs"] == [
        "src/app.py",
        {"supersedes": 7, "reason": "moved the number into config"},
    ]


def test_supersede_rejects_an_already_replaced_entry():
    old = {"id": 3, "status": "superseded", "kind": "fact", "project_id": 1, "refs": []}
    with pytest.raises(ValueError, match="already superseded"):
        plan_supersede(old, "replacement", "because")


def test_supersede_requires_content_and_reason():
    old = {"id": 3, "status": "active", "kind": "fact", "project_id": 1, "refs": []}
    with pytest.raises(ValueError):
        plan_supersede(old, "   ", "because")
    with pytest.raises(ValueError):
        plan_supersede(old, "replacement", "  ")
