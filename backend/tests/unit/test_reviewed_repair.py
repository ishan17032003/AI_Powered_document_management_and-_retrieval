from pathlib import Path

import pytest

from app.services.reviewed_repair import RepairError, build_plan


def test_dry_run_is_explicit_and_hashes_targets(tmp_path: Path) -> None:
    root = tmp_path / "storage"
    root.mkdir()
    target = root / "quarantine" / "a.bin"
    target.parent.mkdir()
    target.write_bytes(b"payload")
    plan = build_plan(storage=root, targets=["quarantine/a.bin"], backup_id="b-1", actor="admin", dry_run=True)
    assert plan["mutation"] == "none"
    assert plan["targets"][0]["target"] == "quarantine/a.bin"
    assert len(plan["targets"][0]["sha256"]) == 64


def test_repair_rejects_traversal_and_broad_targets(tmp_path: Path) -> None:
    root = tmp_path / "storage"
    root.mkdir()
    (root / "a").write_text("x")
    with pytest.raises(RepairError):
        build_plan(storage=root, targets=["../a"], backup_id="b", actor="admin", dry_run=True)
    with pytest.raises(RepairError):
        build_plan(storage=root, targets=["."], backup_id="b", actor="admin", dry_run=True)


def test_repair_requires_explicit_inputs(tmp_path: Path) -> None:
    with pytest.raises(RepairError):
        build_plan(storage=tmp_path, targets=[], backup_id="", actor="", dry_run=True)
