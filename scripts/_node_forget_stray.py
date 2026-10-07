"""维护：清掉节点上**协调者不认识的段**（一次没推完的写留下的死重量）。

为什么可以清
------------
``offset`` 是"哪一份向量"的标识，**只有协调者有权分配**。节点上出现一个
协调者登记表里没有的 offset，只可能来自一次**没推完的写**：
协调者已经回滚了自己的账（登记表里没有它），而已经收到的那几台不会回滚。

那段数据**在系统里无法被任何正常路径碰到**：
协调者没有它的分量值、没有它的块密钥、也没有它的登记行 ——
检索、验证、解密、删除**都不会**走到它。留着只会占空间，
并且让启动守卫以为"节点跑在前面"而**拒绝启动**。

所以这里做的是"把节点拉回协调者的真相"，**不是**删一份有用的文件。

用法（**必须先停掉所有节点进程**，否则内存里的状态会把它写回去）：

    python scripts\\start_all.py --stop
    python scripts\\_node_forget_stray.py            # 只看，不动
    python scripts\\_node_forget_stray.py --apply     # 真删
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APPLY = "--apply" in sys.argv


def known_offsets() -> set[int]:
    """协调者登记的段起始 —— 以 ``file_deltas`` 为准（它就是路由真正用的那张表）。"""
    con = sqlite3.connect(f"file:{ROOT / 'vds.db'}?mode=ro", uri=True)
    try:
        return {int(r[0]) for r in con.execute("SELECT offset FROM file_deltas")}
    finally:
        con.close()


def main() -> int:
    known = known_offsets()
    print(f"协调者登记的段 offset = {sorted(known)}")
    total = 0
    for nd in sorted((ROOT / "nodes").iterdir()):
        db = nd / "node.db"
        if not db.exists():
            continue
        con = sqlite3.connect(db)
        try:
            rows = list(con.execute("SELECT offset, delta_n FROM state ORDER BY offset"))
            stray = [(int(o), int(n)) for o, n in rows if int(o) not in known]
            print(f"\n--- {nd.name} ---")
            print(f"   共 {len(rows)} 段；其中协调者不认识的：{stray or '（无）'}")
            if not stray:
                continue
            for off, n in stray:
                blobs = con.execute(
                    "SELECT COUNT(*) FROM blobs WHERE offset = ?", (off,)
                ).fetchone()[0]
                print(f"     offset={off} n={n} 密文 {blobs} 块" + ("  → 删除" if APPLY else "  （预览）"))
            if APPLY:
                for off, _ in stray:
                    con.execute("DELETE FROM blobs WHERE offset = ?", (off,))
                    con.execute("DELETE FROM state WHERE offset = ?", (off,))
                con.commit()
                print(f"   已删 {len(stray)} 段")
            total += len(stray)
        finally:
            con.close()
    print()
    if APPLY:
        print(f"完成：共清掉 {total} 段死重量。协调者认得的段一个字节没动。")
    else:
        print(f"预览：有 {total} 段会被清掉。加 --apply 才真删。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
