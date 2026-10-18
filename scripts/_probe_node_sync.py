"""临时探针：**截断 / 删文件 / 替换**之后，节点侧的向量登记（``state.delta_n``）跟不跟得上。

为什么要有它
------------
启动守卫（``StoreManager._verify_nodes_at_startup``）比的是"每份文件的
``offset -> n``"：一边取协调者的 ``file_deltas``，一边取各节点 ``describe()``。
两边不等就**拒绝启动**。这个脚本就是去实测：哪一种写操作会让两边岔开。

只读节点库（``nodes/node-*/node.db``，跟节点的 ``describe()`` 同源），
不改协调者库；对协调者的改动一律走**真实 HTTP 接口**（真后端 + 真节点）。

用法（服务要起着）：python scripts/_probe_node_sync.py
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:8000"
PWD = "vds12345"
RUN = time.strftime("%H%M%S")

# ★ trust_env=False：本机环境里有 HTTP_PROXY，httpx 会把 127.0.0.1 也往代理上送，
#   结果是"目标计算机积极拒绝"。测试脚本走的是本机回环，必须绕开代理。
c = httpx.Client(base_url=BASE, timeout=60.0, trust_env=False)


def login(username: str) -> str:
    r = c.post("/api/auth/login", json={"username": username, "password": PWD})
    r.raise_for_status()
    return r.json()["token"]


def auth(tok: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {tok}"}


def upload(tok: str, key: str, n_blocks: int, block: int = 256) -> dict:
    data = bytes((i * 7 + 3) % 256 for i in range(n_blocks * block))
    r = c.post(
        "/api/files",
        headers=auth(tok),
        files={"file": (f"{key}.bin", data)},
        data={"file_key": key, "segment_bytes": str(block)},
    )
    r.raise_for_status()
    return r.json()


def patch(tok: str, fid: int, body: dict) -> dict:
    r = c.patch(f"/api/files/{fid}", headers=auth(tok), json=body)
    print(f"    PATCH {body} -> {r.status_code}")
    if r.status_code >= 400:
        print("      detail:", r.json().get("detail"))
    return r.json()


def nodes() -> dict[str, dict[int, int]]:
    """各节点自己记的 ``offset -> delta_n``（与 ``describe()`` 同源）。"""
    out: dict[str, dict[int, int]] = {}
    for nd in sorted((ROOT / "nodes").iterdir()):
        db = nd / "node.db"
        if not db.exists():
            continue
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            out[nd.name] = {
                int(o): int(n) for o, n in con.execute("SELECT offset, delta_n FROM state")
            }
        finally:
            con.close()
    return out


def coord() -> dict[int, int]:
    """协调者登记的 ``offset -> n``（``file_deltas``，就是路由真正用的那张表）。"""
    con = sqlite3.connect(f"file:{ROOT / 'vds.db'}?mode=ro", uri=True)
    try:
        return {int(o): int(n) for o, n in con.execute("SELECT offset, delta_n FROM file_deltas")}
    finally:
        con.close()


def step(title: str) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


def compare(label: str) -> None:
    want = coord()
    got = nodes()
    first = next(iter(got.values()), {})
    diff = {k: (first.get(k), want.get(k)) for k in sorted(set(first) | set(want)) if first.get(k) != want.get(k)}
    print(f"  协调者: {sorted(want.items())}")
    for nid, d in got.items():
        print(f"  {nid}: {sorted(d.items())}" + ("   <== 与其他节点不同" if d != first else ""))
    print(f"  结论：{'一致 ✓' if not diff else f'不一致 ✗  {diff} （值 = (节点, 协调者)）'}")
    assert not diff, f"{label}: 节点与协调者岔开了"


def main() -> int:
    tok = login("admin")

    step("① 基线：先传一份 5 块的文件")
    f1 = upload(tok, f"probe-sync-A-{RUN}", 5)
    print(f"  上传 {f1['file_key']}  id={f1['id']}  {f1['block_count']} 块  "
          f"位置段 {f1['indices'][0]}-{f1['indices'][-1]}")
    compare("上传后")

    step("② 替换第 0 块（n 不该变）")
    patch(tok, f1["id"], {"op": "modify", "block_idx": 0, "data_b64": "cmVwbGFjZWQ="})
    compare("替换后")

    step("③ 追加 1 块（n 5 -> 6）")
    patch(tok, f1["id"], {"op": "append", "data_b64": "YXBwZW5kZWQ="})
    compare("追加后")

    step("④ 截断末尾 1 块（n 6 -> 5）★ 重点")
    patch(tok, f1["id"], {"op": "truncate", "drop_blocks": 1})
    compare("截断后")

    step("⑤ 传一份新的 2 块文件，再整份删掉 ★ 重点")
    f2 = upload(tok, f"probe-sync-B-{RUN}", 2)
    print(f"  上传 {f2['file_key']}  id={f2['id']}")
    compare("新文件上传后")
    r = c.delete(f"/api/files/{f2['id']}", headers=auth(tok))
    print(f"    DELETE -> {r.status_code}")
    compare("删文件后")

    step("⑥ 再截断一次（看是否累积）")
    patch(tok, f1["id"], {"op": "truncate", "drop_blocks": 1})
    compare("第二次截断后")

    step("⑦ ★ 关键：截断**不是最后一份**的文件（它后面压着别人后写的块）")
    f3 = upload(tok, f"probe-sync-C-{RUN}", 2)
    print(f"  后写的 {f3['file_key']}  id={f3['id']}  位置段 {f3['indices'][0]}-{f3['indices'][-1]}")
    r = c.patch(
        f"/api/files/{f1['id']}", headers=auth(tok), json={"op": "truncate", "drop_blocks": 1}
    )
    print(f"    截断 {f1['file_key']}（写在前面、后面压着别人）-> {r.status_code}")
    print("      detail:", r.json().get("detail"))
    print(
        "  结论："
        + (
            "允许 —— “只有最后写进向量的那份文件才删得动尾巴”这条**旧约束已经不存在** ✓"
            if r.status_code == 200
            else "被拒 —— 旧约束仍在（要改的是界面文案，不是这里）"
        )
    )
    compare("截断非末位文件后")

    print()
    print("全部一致 —— 写操作本身没有把节点登记拉偏。")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as exc:
        print(f"\n!! 发现不一致：{exc}")
        print(json.dumps({"hint": "节点侧 state.delta_n 没跟上"}, ensure_ascii=False))
        raise SystemExit(1)
