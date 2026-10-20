import json

import pytest
from fastapi import HTTPException

from app.knowledge.chunking import MAX_CHARS, chunk_markdown, chunk_plain, split_text
from app.knowledge.gitrepo import validate_page_path
from app.knowledge.ingest.code import extract_symbols, supported
from app.knowledge.ingest.files import parse_upload
from app.knowledge.ingest.repos import project_name, validate_repo_url
from app.knowledge.ingest.transcripts import parse_transcript


def test_markdown_chunks_keep_heading_path():
    chunks = chunk_markdown("intro\n# A\ntext a\n## B\ntext b\n# C\ntext c")
    assert [(c.content.splitlines()[0], c.meta.get("headings")) for c in chunks] == [
        ("intro", None),
        ("# A", ["A"]),
        ("## B", ["A", "B"]),
        ("# C", ["C"]),
    ]


def test_long_text_is_split_with_bounded_chunks():
    text = "\n\n".join(["word " * 200] * 10) + "\n\n" + "x" * (MAX_CHARS * 2)
    parts = split_text(text)
    assert len(parts) > 3
    assert all(len(p) <= MAX_CHARS for p in parts)
    assert chunk_plain("") == []


def test_parse_upload_types():
    assert parse_upload("notes.md", b"# T\nbody")[0].meta == {"headings": ["T"]}
    assert parse_upload("log.txt", b"line")[0].content == "line"
    with pytest.raises(HTTPException):
        parse_upload("x.docx", b"")


PY = b"""
import os

@decorator
def top(a, b):
    return a + b

class Small:
    def m(self):
        pass

class Big:
""" + b"".join(f"    def method_{i}(self):\n        return {'x' * 60!r}\n\n".encode() for i in range(60))

GO = b"""package main

type Server struct{}

func Start() error { return nil }

func (s *Server) Stop() {}
"""


def test_extract_python_symbols():
    syms = extract_symbols("mod.py", PY)
    names = [s.name for s in syms]
    assert names[:3] == ["top", "Small", "Big"]
    assert "Big.method_0" in names and "Big.method_59" in names
    top = syms[0]
    assert top.kind == "function" and top.start_line == 4 and "@decorator" in top.code


def test_extract_go_symbols():
    syms = extract_symbols("main.go", GO)
    assert [(s.name, s.kind) for s in syms] == [("Server", "type"), ("Start", "function"), ("Stop", "method")]
    assert supported("a.go") and not supported("a.rs")
    assert extract_symbols("a.rs", b"fn main(){}") == []


@pytest.mark.parametrize("path", ["a.md", "runbooks/x-y_z.1.md", "deep/er/page.md"])
def test_valid_page_paths(path):
    assert validate_page_path(path) == path


@pytest.mark.parametrize("path", ["../x.md", "/x.md", "x.txt", ".git/x.md", "a/.hidden.md", "a//b.md", "sp ace.md"])
def test_invalid_page_paths(path):
    with pytest.raises(HTTPException):
        validate_page_path(path)


def test_repo_url_validation():
    assert validate_repo_url(" https://gitlab.example.com/team/repo.git ") == "https://gitlab.example.com/team/repo.git"
    for bad in ["http://gitlab/x.git", "file:///etc", "ext::sh -c id", "https://user:pw@gitlab/x.git", "https://"]:
        with pytest.raises(HTTPException):
            validate_repo_url(bad)
    assert project_name("https://gerrit.example.com/a/data/collector") == "collector"
    assert project_name("https://gitlab.example.com/team/pipeline.git/") == "pipeline"


def _line(type_, content, **extra):
    return json.dumps(
        {
            "type": type_,
            "sessionId": "s-1",
            "timestamp": "2026-10-01T10:00:00Z",
            "cwd": "/repo",
            "gitBranch": "main",
            "message": {"role": type_, "content": content},
            **extra,
        }
    )


def test_parse_claude_code_transcript():
    data = "\n".join(
        [
            json.dumps({"type": "summary", "summary": "ignored"}),
            _line("user", "Fix the flaky Jenkins test"),
            _line("user", "meta", isMeta=True),
            _line(
                "assistant",
                [
                    {"type": "thinking", "thinking": "hidden"},
                    {"type": "text", "text": "Looking at it."},
                    {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "pytest -x"}},
                ],
            ),
            _line("user", [{"type": "tool_result", "tool_use_id": "t1", "content": "1 failed"}]),
            _line("assistant", "sidechain", isSidechain=True),
            "not json",
        ]
    ).encode()
    parsed = parse_transcript(data)
    assert parsed.session_id == "s-1" and parsed.cwd == "/repo" and parsed.branch == "main"
    assert parsed.title == "Fix the flaky Jenkins test"
    assert [(m.role, m.content) for m in parsed.messages] == [
        ("user", "Fix the flaky Jenkins test"),
        ("assistant", "Looking at it."),
    ]
    assert parsed.messages[1].tools[0].name == "Bash"
    assert parsed.messages[1].tools[0].args == {"command": "pytest -x"}


def test_parse_rejects_non_transcripts():
    with pytest.raises(HTTPException):
        parse_transcript(b'{"hello": "world"}\n')
