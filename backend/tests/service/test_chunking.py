from app.chunking import CHUNKER_VERSION, chunk_docling


def _fixture():
    return {
        "pages": [
            {"page": 2, "elements": [
                {"type": "heading", "level": 1, "text": "Policy"},
                {"type": "paragraph", "text": "Alpha beta gamma delta."},
                {"type": "table", "text": "| Key | Value |\n| --- | --- |\n| A | B |"},
            ]},
        ]
    }


def test_docling_chunks_are_stable_and_citable():
    left = chunk_docling(7, "v3", _fixture(), max_chars=128)
    right = chunk_docling(7, "v3", _fixture(), max_chars=128)
    assert [c.chunk_id for c in left] == [c.chunk_id for c in right]
    assert left[0].section_path == ("Policy",)
    assert left[0].page_start == 2
    assert CHUNKER_VERSION


def test_long_text_has_bounded_overlap_and_plain_text():
    text = " ".join(f"word{i}" for i in range(80))
    chunks = chunk_docling(1, 1, [{"page": 1, "blocks": [{"type": "paragraph", "text": text}]}], max_chars=128, overlap_chars=20)
    assert len(chunks) > 1
    assert all(len(chunk.text) <= 128 for chunk in chunks)
    assert chunks[0].text[-10:].split()[-1] in chunks[1].text
