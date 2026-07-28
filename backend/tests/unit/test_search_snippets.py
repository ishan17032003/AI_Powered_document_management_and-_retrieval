from app.services.search_service import _match_ranges, _plain_text_snippet


def test_search_snippet_is_plain_text_and_unescapes_entities() -> None:
    assert _plain_text_snippet("before <mark>matched &amp; safe</mark> after") == (
        "before matched & safe after"
    )


def test_search_snippet_drops_provider_tags() -> None:
    assert _plain_text_snippet("<script>alert(1)</script>visible") == "alert(1)visible"


def test_match_ranges_are_plain_text_offsets_and_case_insensitive() -> None:
    snippet = _plain_text_snippet("before <mark>Matched</mark> &amp; safe")
    assert snippet == "before Matched & safe"
    assert _match_ranges(snippet, "matched safe") == [
        {"start": 7, "end": 14},
        {"start": 17, "end": 21},
    ]


def test_match_ranges_are_bounded_and_merge_overlaps() -> None:
    assert _match_ranges("aaaa", "aa", limit=1) == [{"start": 0, "end": 2}]
