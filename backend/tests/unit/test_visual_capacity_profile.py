from pathlib import Path


def test_visual_capacity_profile_is_explicitly_baseline_and_gated() -> None:
    evidence = Path("docs/evidence/WP13-A-visual-capacity-profile.md").read_text()
    assert "development baseline" in evidence
    assert "TBD" in evidence
    assert "GPU/VRAM" in evidence
    assert "query-mode mix" in evidence
