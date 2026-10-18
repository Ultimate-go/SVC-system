"""比对「后端用的错误码」与「前端那张表」，缺一条就报错。

用法::

    python scripts/check_error_codes.py

为什么要它：错误码分散在两处 ——

* 后端 ``raise ApiError(状态码, "码", …)``（``backend/`` 下各路由器）；
* 前端 ``frontend/src/data/errors.js`` 的 ``ERROR_TEXT`` 表。

两边一旦对不上，表现是**界面弹后端那句原文**（`api/client.js` 的回退分支），
不报错、不崩，只是"改了前端却没生效"—— 这种静默失配查起来很费时间。
所以这里用脚本钉住：后端有、前端缺 → 报错；前端有、后端没在用 → 也报错
（说明是搬走之后忘了删，或者码拼错了）。
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
ERRORS_JS = ROOT / "frontend" / "src" / "data" / "errors.js"

#: 码的形状：``区域.事项``（小写字母、数字、下划线）。
CODE_RE = re.compile(r"'([a-z][a-z0-9_]*\.[a-z0-9_]+)'\s*:")


def backend_codes() -> dict[str, list[str]]:
    """扫出所有 ``ApiError(...)`` 里的码 —— 码必须是**字面量**第二个参数。"""
    out: dict[str, list[str]] = {}
    for path in sorted(BACKEND.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = getattr(fn, "id", None) or getattr(fn, "attr", "")
            if name != "ApiError" or len(node.args) < 3:
                continue
            code = node.args[1]
            if not (isinstance(code, ast.Constant) and isinstance(code.value, str)):
                out.setdefault("<非字面量>", []).append(
                    f"{path.relative_to(ROOT).as_posix()}:{node.lineno}"
                )
                continue
            out.setdefault(code.value, []).append(
                f"{path.relative_to(ROOT).as_posix()}:{node.lineno}"
            )
    return out


def frontend_codes() -> set[str]:
    text = ERRORS_JS.read_text(encoding="utf-8")
    return set(CODE_RE.findall(text))


def main() -> int:
    back = backend_codes()
    front = frontend_codes()
    bad = 0

    problems = sorted(back.pop("<非字面量>", []))
    if problems:
        print("✗ 有 ApiError 的码不是字面量（脚本无法比对）：")
        for p in problems:
            print(f"    {p}")
        bad += len(problems)

    missing = sorted(set(back) - front)
    if missing:
        print("✗ 后端用了、前端 errors.js 里没有的码（界面会退回后端原文）：")
        for code in missing:
            print(f"    {code:34s} {back[code][0]}")
        bad += len(missing)

    extra = sorted(front - set(back))
    if extra:
        print("✗ 前端 errors.js 里有、后端没用到的码（搬走后忘了删？码拼错？）：")
        for code in extra:
            print(f"    {code}")
        bad += len(extra)

    if bad:
        print(f"\n共 {bad} 处不一致。")
        return 1
    print(f"全部一致：后端 {len(back)} 个码，前端 {len(front)} 条。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
