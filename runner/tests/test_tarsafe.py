"""Workspace uploads refuse traversal, absolute or escaping links and devices (design 04 §4, §9 test 4)."""

import io
import tarfile

import pytest

from mwb_runner.tarsafe import UnsafeArchive, sanitize_bytes


def tar(*members: tuple[str, str, str | bytes]) -> bytes:
    """members: (kind, name, data-or-linkname); kind is file, dir, sym, hard, dev, fifo."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        for kind, name, data in members:
            info = tarfile.TarInfo(name)
            if kind == "file":
                raw = data.encode() if isinstance(data, str) else data
                info.size, info.mode, info.uid = len(raw), 0o4755, 0
                t.addfile(info, io.BytesIO(raw))
                continue
            info.type = {"dir": tarfile.DIRTYPE, "sym": tarfile.SYMTYPE, "hard": tarfile.LNKTYPE,
                         "dev": tarfile.CHRTYPE, "fifo": tarfile.FIFOTYPE}[kind]  # fmt: skip
            if kind in ("sym", "hard"):
                info.linkname = str(data)
            t.addfile(info)
    return buf.getvalue()


def members(data: bytes) -> list[tarfile.TarInfo]:
    with tarfile.open(fileobj=io.BytesIO(data)) as t:
        return t.getmembers()


def test_clean_members_are_kept_owned_by_the_agent() -> None:
    out = members(sanitize_bytes(tar(("dir", "./app", ""), ("file", "./app/main.py", "print(1)"),
                                     ("sym", "app/link.py", "main.py"), ("sym", "docs/up", "../app/main.py"),
                                     ("hard", "app/copy.py", "app/main.py"))))  # fmt: skip
    assert [m.name for m in out] == ["app", "app/main.py", "app/link.py", "docs/up", "app/copy.py"]
    main = out[1]
    assert (main.uid, main.gid, main.mode) == (1000, 1000, 0o755)  # the setuid bit is dropped
    assert out[2].linkname == "main.py" and out[4].linkname == "app/main.py"


@pytest.mark.parametrize(
    ("member", "reason"),
    [
        (("file", "../evil.sh", "x"), "path leaves the workspace"),
        (("file", "a/../../evil.sh", "x"), "path leaves the workspace"),
        (("file", "/etc/passwd", "x"), "absolute path"),
        (("sym", "secret", "/etc/shadow"), "link to an absolute path"),
        (("sym", "a/up", "../../outside"), "link leaves the workspace"),
        (("hard", "h", "/etc/passwd"), "link to an absolute path"),
        (("hard", "h", "../x"), "path leaves the workspace"),
        (("dev", "null", ""), "device or FIFO"),
        (("fifo", "pipe", ""), "device or FIFO"),
    ],
)
def test_unsafe_members_are_refused(member: tuple, reason: str) -> None:
    with pytest.raises(UnsafeArchive) as e:
        sanitize_bytes(tar(("file", "ok.txt", "fine"), member))
    assert e.value.reason == reason


def test_size_limit() -> None:
    with pytest.raises(UnsafeArchive, match="too large"):
        sanitize_bytes(tar(("file", "big", b"x" * 2000)), max_bytes=1000)


def test_gzip_is_accepted() -> None:
    import gzip

    assert [m.name for m in members(sanitize_bytes(gzip.compress(tar(("file", "a.txt", "a")))))] == ["a.txt"]
