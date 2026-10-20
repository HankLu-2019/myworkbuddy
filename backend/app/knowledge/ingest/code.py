from dataclasses import dataclass

import tree_sitter_go
import tree_sitter_python
from tree_sitter import Language, Node, Parser

from app.knowledge.chunking import MAX_CHARS

# Languages are module-level so they outlive the parsers that point at them.
_PY = Language(tree_sitter_python.language())
_GO = Language(tree_sitter_go.language())
_PARSERS = {".py": Parser(_PY), ".go": Parser(_GO)}
_PY_DEFS = {"function_definition", "class_definition", "decorated_definition"}
_GO_DEFS = {"function_declaration", "method_declaration", "type_declaration"}


@dataclass
class Symbol:
    name: str
    kind: str
    start_line: int
    end_line: int
    code: str


def supported(path: str) -> bool:
    return any(path.endswith(ext) for ext in _PARSERS)


def _name(node: Node) -> str:
    if node.type == "decorated_definition":
        inner = node.child_by_field_name("definition")
        return _name(inner) if inner else "?"
    if node.type == "type_declaration":
        spec = next((c for c in node.children if c.type == "type_spec"), None)
        return _name(spec) if spec else "?"
    n = node.child_by_field_name("name")
    return n.text.decode() if n and n.text else "?"


def _symbol(node: Node, prefix: str = "") -> Symbol:
    return Symbol(
        name=f"{prefix}{_name(node)}",
        kind=node.type.replace("_definition", "").replace("_declaration", "").replace("decorated", "function"),
        start_line=node.start_point.row + 1,
        end_line=node.end_point.row + 1,
        code=node.text.decode(errors="replace") if node.text else "",
    )


def _class_node(node: Node) -> Node | None:
    target = node.child_by_field_name("definition") if node.type == "decorated_definition" else node
    return target if target is not None and target.type == "class_definition" else None


def extract_symbols(path: str, source: bytes) -> list[Symbol]:
    parser = next((p for ext, p in _PARSERS.items() if path.endswith(ext)), None)
    if parser is None:
        return []
    # Keep a reference to the Tree: nodes point into its memory and segfault once it is freed.
    tree = parser.parse(source)
    root = tree.root_node
    defs = _PY_DEFS if path.endswith(".py") else _GO_DEFS
    out: list[Symbol] = []
    for node in root.children:
        if node.type not in defs:
            continue
        # Hold the class node while using its body: py-tree-sitter child nodes don't keep their parent alive.
        cls_node = _class_node(node)
        body = cls_node.child_by_field_name("body") if cls_node is not None else None
        # Large classes are split per method so each chunk stays within embedding limits.
        if body is not None and len(node.text or b"") > MAX_CHARS:
            cls = _name(node)
            out.append(
                Symbol(
                    cls,
                    "class",
                    node.start_point.row + 1,
                    node.end_point.row + 1,
                    (node.text or b"").decode(errors="replace")[:600],
                )
            )
            out += [_symbol(m, f"{cls}.") for m in body.children if m.type in _PY_DEFS]
        else:
            out.append(_symbol(node))
    return out
