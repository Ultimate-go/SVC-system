"""审计：后端代码里"会送到前端去的静态文案"有哪些（只读，不改任何东西）。

用法::

    python scripts/_scan_backend_text.py            # 全量
    python scripts/_scan_backend_text.py advisor    # 只看文件名含 advisor 的

判定方式（AST，不靠正则，所以不会把注释和 docstring 算进来）：

* 只收 ``ast.Constant`` 且是 ``str``；
* 排除 docstring（模块/类/函数体的第一句纯字符串表达式）；
* 保留含中日韩字符、且去掉首尾空白后长度 >= 12 的；
* 记录它出现在哪：函数名、以及如果是 dict 的值就带上 key、如果是 list 元素就标 list。

输出按 **文件名** 分组，逐条给出 ``行号 / 函数 / 位置 / 文本``，便于人工判断
"这句是不是自始至终不变"。
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCAN_DIRS = ["backend", "core", "svc", "vds", "node_service"]
CJK = re.compile(r"[\u4e00-\u9fff]")
MIN_LEN = 12


def _docstrings(tree: ast.AST) -> set[int]:
    """收集所有 docstring 节点的 id（模块/类/函数体的首句纯字符串）。"""
    out: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if not body:
                continue
            first = body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                if isinstance(first.value.value, str):
                    out.add(id(first.value))
    return out


class Walker(ast.NodeVisitor):
    def __init__(self) -> None:
        self.stack: list[str] = []
        self.hits: list[tuple[int, str, str, str]] = []

    @property
    def where(self) -> str:
        return ".".join(self.stack) if self.stack else "<module>"

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def visit_Dict(self, node: ast.Dict) -> None:
        for k, v in zip(node.keys, node.values):
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                key = k.value if isinstance(k, ast.Constant) and isinstance(k.value, str) else "<?key>"
                self._consider(v, f"dict[{key}]")
            elif isinstance(k, ast.Constant) and isinstance(k.value, str):
                # key 本身也是文案（例如 "中文名": val）
                self._consider(k, "dict-key")
        self.generic_visit(node)

    def visit_List(self, node: ast.List) -> None:
        for i, el in enumerate(node.elts):
            if isinstance(el, ast.Constant) and isinstance(el.value, str):
                self._consider(el, f"list[{i}]")
        self.generic_visit(node)

    def visit_Return(self, node: ast.Return) -> None:
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            self._consider(node.value, "return")
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    self._consider(node.value, f"assign {t.id}")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        # 形如 HTTPException(400, "……") / audit(..., detail="……")
        for a in node.args:
            if isinstance(a, ast.Constant) and isinstance(a.value, str):
                fname = getattr(node.func, "id", None) or getattr(node.func, "attr", "<call>")
                self._consider(a, f"call {fname}()")
        for kw in node.keywords:
            if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                fname = getattr(node.func, "id", None) or getattr(node.func, "attr", "<call>")
                self._consider(kw.value, f"call {fname}({kw.arg}=)")
        self.generic_visit(node)

    def _consider(self, node: ast.Constant, where: str) -> None:
        s = node.value
        if not isinstance(s, str):
            return
        if id(node) in self.docs:
            return
        text = s.strip()
        if len(text) < MIN_LEN or not CJK.search(text):
            return
        # f-string 才会带 {}，普通常量里出现 {} 一般是模板，仍收
        self.hits.append((node.lineno, self.where, where, " ".join(text.split())))


def scan(path: Path, key: str) -> None:
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    w = Walker()
    w.docs = _docstrings(tree)  # type: ignore[attr-defined]
    w.visit(tree)
    if not w.hits:
        return
    rel = path.relative_to(ROOT).as_posix()
    print(f"\n=== {rel} ({len(w.hits)}) ===")
    for line, fn, where, text in sorted(w.hits):
        print(f"  L{line}  [{fn}] {where}: {text}")


def main() -> int:
    key = sys.argv[1] if len(sys.argv) > 1 else ""
    n_files = n_hits = 0
    for d in SCAN_DIRS:
        base = ROOT / d
        if not base.exists():
            continue
        for p in sorted(base.rglob("*.py")):
            if "__pycache__" in p.parts:
                continue
            if key and key.lower() not in p.name.lower():
                continue
            n_files += 1
            scan(p, key)
    print(f"\n扫了 {n_files} 个文件。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
