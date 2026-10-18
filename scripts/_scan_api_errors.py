"""审计：后端有哪些报错是**发给客户端的**（只读，不改东西）。

用法::

    python scripts/_scan_api_errors.py

它把 ``backend/`` 下每个 ``raise HTTPException(...)`` 与
``return JSONResponse(status_code=...)`` 列出来，格式::

    文件:行  状态码  消息表达式（用 ast.unparse 还原）

消息表达式用还原而不是"取字符串常量"——因为很多消息是 f-string，
只有还原出来才知道它里面嵌了几个变量（那决定了前端能不能只靠 code 重建这句话）。
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILES = sorted((ROOT / "backend").rglob("*.py"))


def _status_of(call: ast.Call) -> str:
    """从 ``HTTPException`` 的第一个位置参数里取出状态码的写法。"""
    if call.args:
        return ast.unparse(call.args[0])
    return "?"


def scan(path: Path) -> int:
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    rows: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
            fn = node.exc.func
            name = getattr(fn, "id", None) or getattr(fn, "attr", "")
            if name != "HTTPException":
                continue
            msg = ", ".join(ast.unparse(a) for a in node.exc.args[1:])
            rows.append(f"  L{node.lineno}  [{_status_of(node.exc)}]  {msg}")
        elif isinstance(node, ast.Call):
            fn = node.func
            name = getattr(fn, "id", None) or getattr(fn, "attr", "")
            if name != "JSONResponse":
                continue
            kw = {k.arg: k.value for k in node.keywords}
            if "status_code" not in kw:
                continue
            code = ast.unparse(kw["status_code"])
            if code in ("200", "201", "202", "204"):
                continue
            body = ast.unparse(kw["content"]) if "content" in kw else ""
            rows.append(f"  L{node.lineno}  [{code}]  {body}")
    if not rows:
        return 0
    print(f"\n=== {path.relative_to(ROOT).as_posix()} ({len(rows)}) ===")
    for r in sorted(rows, key=lambda s: int(s.split("L")[1].split(" ")[0])):
        print(r)
    return len(rows)


def main() -> int:
    total = sum(scan(p) for p in FILES if "__pycache__" not in p.parts)
    print(f"\n共 {total} 处。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
