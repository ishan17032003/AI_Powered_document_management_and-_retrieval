import json
from pathlib import Path

from scripts.validate_visual_bom import validate


def test_visual_bom_is_reviewable_and_has_no_approved_unhashed_models():
    path = Path(__file__).resolve().parents[2] / "artifacts" / "visual_model_bom.json"
    digest = validate(path)
    assert len(digest) == 64
    payload = json.loads(path.read_text())
    assert payload["models"] == []
    assert payload["release_policy"]["remote_code"] is False
