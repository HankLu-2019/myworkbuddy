"""Workspace uploads (design 04 §4 `PUT /v1/workspaces/{job_id}/content`): read the caller's tar, refuse anything
that could land outside /workspace or isn't plain content, and write a clean tar for Docker to extract.

Refused: absolute paths, `..` components, symlinks or hard links whose target is absolute or leaves the tree,
devices, FIFOs, and archives over the size or member limits. Kept members get uid/gid 1000, no setuid/setgid/
sticky bits, and normalised relative names.
"""

import io
import posixpath
import tarfile
from typing import BinaryIO

MAX_MEMBERS = 200_000
MAX_BYTES = 2 * 1024**3  # uncompressed content


class UnsafeArchive(Exception):
    def __init__(self, member: str, reason: str) -> None:
        super().__init__(f"{member}: {reason}")
        self.member = member
        self.reason = reason


def _clean_name(name: str) -> str:
    if name.startswith("/") or name.startswith("\\"):
        raise UnsafeArchive(name, "absolute path")
    parts = [p for p in name.replace("\\", "/").split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise UnsafeArchive(name, "path leaves the workspace")
    if not parts:
        return "."
    return "/".join(parts)


def _inside(base: str, target: str) -> bool:
    joined = posixpath.normpath(posixpath.join(posixpath.dirname(base), target))
    return not (joined == ".." or joined.startswith("../") or joined.startswith("/"))


def sanitize(source: BinaryIO, out: BinaryIO, *, max_bytes: int = MAX_BYTES) -> int:
    """Copy the safe members of `source` (a tar stream, optionally gzip) to `out`; returns the member count."""
    total = 0
    count = 0
    with tarfile.open(fileobj=source, mode="r|*") as src, tarfile.open(fileobj=out, mode="w|") as dst:
        for member in src:
            count += 1
            if count > MAX_MEMBERS:
                raise UnsafeArchive(member.name, "too many entries")
            name = _clean_name(member.name)
            if name == ".":
                continue
            if member.isdev() or member.isfifo():
                raise UnsafeArchive(member.name, "device or FIFO")
            if member.issym() or member.islnk():
                target = member.linkname
                if target.startswith("/"):
                    raise UnsafeArchive(member.name, "link to an absolute path")
                check = target if member.issym() else _clean_name(target)
                if member.issym() and not _inside(name, check):
                    raise UnsafeArchive(member.name, "link leaves the workspace")
                if member.islnk():
                    target = check
            elif not (member.isfile() or member.isdir()):
                raise UnsafeArchive(member.name, "unsupported entry type")
            clean = tarfile.TarInfo(name)
            clean.type = member.type
            clean.mode = member.mode & 0o777
            clean.mtime = member.mtime
            clean.uid = clean.gid = 1000
            clean.uname = clean.gname = "agent"
            if member.issym() or member.islnk():
                clean.linkname = target
            if member.isfile():
                total += member.size
                if total > max_bytes:
                    raise UnsafeArchive(member.name, "archive is too large")
                clean.size = member.size
                data = src.extractfile(member)
                dst.addfile(clean, data)
            else:
                dst.addfile(clean)
    return count


def sanitize_bytes(data: bytes, **kw) -> bytes:
    out = io.BytesIO()
    sanitize(io.BytesIO(data), out, **kw)
    return out.getvalue()
