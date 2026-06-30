from concurrent.futures import ThreadPoolExecutor
from splice.storage import archive_to_file


def test_archive_to_file_concurrent(tmp_path):
    p = tmp_path / "hist.jsonl"

    def write_one(i):
        archive_to_file(str(p), [{"path": f"/a{i}", "value": i}])

    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(write_one, range(50)))

    lines = p.read_text().strip().split("\n")
    assert len(lines) == 50
