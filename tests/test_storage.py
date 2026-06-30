from splice.storage import archive_to_file


def test_archive_to_file(tmp_path):
    p = tmp_path / "hist.jsonl"
    archive_to_file(str(p), [{"path": "/a", "value": 1}])
    data = p.read_text().strip()
    assert "\"path\"" in data
