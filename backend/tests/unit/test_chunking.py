from app.chunking import SourceSegment, chunk_text


def test_long_text_is_bounded_and_deterministic() -> None:
    text = "alpha " * 1_000
    first = chunk_text(7, 3, text, max_chars=256, overlap_chars=24)
    second = chunk_text(7, 3, text, max_chars=256, overlap_chars=24)
    assert first == second
    assert first
    assert all(len(item.text) <= 256 for item in first)
    assert all(item.source_start < item.source_end for item in first)


def test_multilingual_and_table_lineage_is_preserved() -> None:
    text = "शीर्षक\nबकाया राशि ₹10,000\nالمبلغ 10000"
    segments = [
        SourceSegment(
            text="शीर्षक\nबकाया राशि ₹10,000",
            start=0,
            end=22,
            page_start=2,
            page_end=2,
            section_path=("वित्त", "तालिका 1"),
            chunk_type="table",
        ),
        SourceSegment(
            text="المبلغ 10000",
            start=23,
            end=len(text),
            page_start=3,
            page_end=3,
            section_path=("Summary",),
            chunk_type="prose",
        ),
    ]
    chunks = chunk_text(9, "v2", text, max_chars=128, overlap_chars=0, source_segments=segments)
    assert [item.page_start for item in chunks] == [2, 3]
    assert chunks[0].chunk_type == "table"
    assert chunks[0].section_path == ("वित्त", "तालिका 1")
    assert "المبلغ" in chunks[1].text
