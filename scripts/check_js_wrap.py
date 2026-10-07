"""用 Python 侧解**浏览器（JS）封的**私钥密文 —— 跨实现互通的另一半。

为什么需要它（安全审计 I8）
--------------------------
改口令那条路是"**浏览器重封 → 服务端登记**"（默认模型下服务端不持有私钥）。
如果浏览器封出来的东西服务端解不开，用户**再也登不进去** ——
而且是"新口令过不了、旧口令解不开"的双向锁死。

`frontend/tests/crypto-vectors.json` 里的 ``user_keys`` 只覆盖了
「Python 封 → JS 解」这一个方向。本脚本反过来跑一遍：
读前端测试落盘的 ``logs/js-wrapped.json``，用
:func:`core.keywrap.unwrap_private_key` 去解。

用法
----
::

    npm --prefix frontend test          # 产出 logs/js-wrapped.json
    python -X utf8 scripts/check_js_wrap.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.keywrap import (  # noqa: E402
    KeyWrapIntegrityError,
    public_bytes,
    unwrap_private_key,
)
from core.sm2 import public_key_of  # noqa: E402

SRC = ROOT / "logs" / "js-wrapped.json"


def main() -> int:
    if not SRC.exists():
        print(f"[跳过] 还没有 {SRC} —— 先跑 `npm --prefix frontend test`")
        return 0

    data = json.loads(SRC.read_text(encoding="utf-8"))
    password = data["password"]
    expect = int(data["sk"])
    blob = data["blob"]

    got = unwrap_private_key(password, blob)
    same_sk = got == expect
    print(f"[1] Python 解得开 JS 封的密文：{same_sk}")

    same_pk = public_bytes(public_key_of(got)) == public_bytes(public_key_of(expect))
    print(f"[2] 解出来的私钥对应同一把公钥：{same_pk}")

    try:
        unwrap_private_key("wrong-pass", blob)
    except KeyWrapIntegrityError:
        print("[3] 口令错被拒绝：True")
        bad_ok = True
    else:
        print("[3] 口令错也被解开了 —— 不对！")
        bad_ok = False

    all_ok = same_sk and same_pk and bad_ok
    print(f"\n{'[ok]' if all_ok else '[失败]'} JS 封 / Python 解 互通：{all_ok}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
