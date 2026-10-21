from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path, PurePosixPath


class UnsafeStoragePath(ValueError):
    pass


class ManagedStorage:
    """Resolve opaque relative keys without allowing access outside one data root."""

    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve(strict=False)
        if self.root == Path(self.root.anchor):
            raise UnsafeStoragePath("managed storage root cannot be a filesystem root")

    def ensure_exists(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def resolve_key(self, key: str) -> Path:
        pure = self._validated_key(key)
        candidate = self.root.joinpath(*pure.parts).resolve(strict=False)
        try:
            common = Path(os.path.commonpath((str(self.root), str(candidate))))
        except ValueError as exc:
            raise UnsafeStoragePath("storage key resolves outside managed root") from exc
        if common != self.root:
            raise UnsafeStoragePath("storage key resolves outside managed root")
        return candidate

    @staticmethod
    def _validated_key(key: str) -> PurePosixPath:
        if not key or "\\" in key:
            raise UnsafeStoragePath("storage key must be a non-empty POSIX relative path")
        pure = PurePosixPath(key)
        if pure.is_absolute() or any(part in ("", ".", "..") for part in pure.parts):
            raise UnsafeStoragePath("storage key must not be absolute or contain traversal")
        return pure

    def write_bytes_atomic(self, key: str, data: bytes) -> Path:
        destination = self.resolve_key(key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".receipt-", suffix=".tmp", dir=destination.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        return destination

    def read_bytes(self, key: str) -> bytes:
        path = self.resolve_key(key)
        if path.is_symlink() or not path.is_file():
            raise UnsafeStoragePath("managed receipt file is unavailable or unsafe")
        return path.read_bytes()

    def delete_regular_file(self, key: str) -> int:
        """Delete one managed regular file without following symlinks.

        Returns the on-disk byte size observed immediately before deletion.
        Missing files raise ``FileNotFoundError``; unsafe or non-regular targets
        raise ``UnsafeStoragePath``.
        """
        pure = self._validated_key(key)
        candidate = self.root.joinpath(*pure.parts)
        current = self.root
        for part in pure.parts[:-1]:
            current = current / part
            if current.is_symlink():
                raise UnsafeStoragePath("managed receipt parent cannot be a symlink")
        if candidate.is_symlink():
            raise UnsafeStoragePath("managed receipt target cannot be a symlink")
        metadata = candidate.stat(follow_symlinks=False)
        if not stat.S_ISREG(metadata.st_mode):
            raise UnsafeStoragePath("managed receipt target is not a regular file")
        candidate.unlink()
        return metadata.st_size
