import json
from pathlib import Path

import pytest

from brakesmith.core import BrakeSmithError
from brakesmith.plans import load_plan, load_state, write_plan, write_state


def test_plan_rejects_edits(tmp_path: Path):
    plan = tmp_path / "plan.json"
    write_plan(plan, {"items": [], "setting": "safe"})
    payload = json.loads(plan.read_text())
    payload["setting"] = "edited"
    plan.write_text(json.dumps(payload))
    with pytest.raises(BrakeSmithError, match="edited or corrupted"):
        load_plan(plan)


def test_state_must_match_plan(tmp_path: Path):
    state_path = tmp_path / "state.json"
    state = {"plan_digest": "one", "items": {"movie": {"status": "completed"}}}
    write_state(state_path, state)
    assert load_state(state_path, "one") == state
    with pytest.raises(BrakeSmithError, match="does not match"):
        load_state(state_path, "two")
