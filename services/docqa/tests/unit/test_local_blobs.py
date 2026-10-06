from pathlib import Path

import pytest

from docqa.adapters.local_blobs import LocalBlobStore


def test_put_get_exists_list_delete(tmp_path: Path) -> None:
    store = LocalBlobStore(tmp_path)
    store.put("parsed/a.json", b"{}", "application/json")
    store.put("raw/x/b.pdf", b"%PDF", "application/pdf")
    (tmp_path / "raw" / ".DS_Store").write_bytes(b"")
    assert store.get("raw/x/b.pdf") == b"%PDF"
    assert store.exists("parsed/a.json")
    assert not store.exists("parsed/missing.json")
    assert store.list_keys("raw/") == ["raw/x/b.pdf"]  # hidden files ignored
    assert store.list_keys("nothing/") == []
    store.delete("parsed/a.json")
    store.delete("parsed/a.json")  # deleting twice is fine
    assert not store.exists("parsed/a.json")


@pytest.mark.parametrize("key", ["../outside.txt", "raw/../../outside.txt", "/etc/passwd"])
def test_keys_cannot_escape_the_root(tmp_path: Path, key: str) -> None:
    store = LocalBlobStore(tmp_path / "data")
    with pytest.raises(ValueError, match="outside the data directory"):
        store.get(key)
