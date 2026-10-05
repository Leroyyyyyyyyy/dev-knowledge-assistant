from app.retrieval.chunker import chunk_generic, chunk_python


def lines_of(text: str, chunk: dict) -> list[str]:
    return text.split("\n")[chunk["start_line"] - 1 : chunk["end_line"]]


def test_python_chunk_ranges_match_their_content():
    text = "\n\nimport os\n\n\ndef first():\n    return 1\n\n\ndef second():\n    return 2\n"
    chunks = chunk_python(text, "m.py", "r")
    ranges = []
    for chunk in chunks:
        ranges.append((chunk["start_line"], chunk["end_line"]))
    # Leading blank lines and the trailing newline are not part of any range.
    assert ranges == [(3, 3), (6, 7), (10, 11)]
    for chunk in chunks:
        assert "\n".join(lines_of(text, chunk)).strip() == chunk["content"]


def test_generic_chunk_never_points_past_the_file():
    text = "line one\nline two\n"
    [chunk] = chunk_generic(text, "a.md", "r")
    assert (chunk["start_line"], chunk["end_line"]) == (1, 2)
