"""BlobStore over a local directory, for local mode: documents never leave the laptop."""

from datetime import UTC, datetime
from pathlib import Path

from docqa.domain.documents import BlobInfo


class LocalBlobStore:
    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    def _path(self, key: str) -> Path:
        path = (self._root / key).resolve()
        if not path.is_relative_to(self._root):  # keys like "../x" must not escape the root
            raise ValueError(f"key outside the data directory: {key}")
        return path

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def put(self, key: str, data: bytes, content_type: str) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def list_keys(self, prefix: str) -> list[str]:
        base = self._path(prefix.rstrip("/") or ".")
        if not base.is_dir():
            return []
        return sorted(
            p.relative_to(self._root).as_posix()
            for p in base.rglob("*")
            if p.is_file() and not p.name.startswith(".")
        )

    def list_objects(self, prefix: str) -> list[BlobInfo]:
        objects = []
        for key in self.list_keys(prefix):
            stat = self._path(key).stat()
            modified = datetime.fromtimestamp(stat.st_mtime, UTC)
            objects.append(BlobInfo(key=key, size=stat.st_size, modified=modified))
        return objects
