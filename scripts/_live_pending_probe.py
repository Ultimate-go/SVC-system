"""★ 「待补推现场跨重启」的**真链路**探针（配合"人工重启一次后端"用）。

用法::

    python scripts/_live_pending_probe.py setup     # 造一次真失败：掉线 1 台 + 上传
    —— 然后**重启后端**——
    python scripts/_live_pending_probe.py verify    # 看现场活没活下来、补推能不能收敛

为什么不用一个脚本跑完：中间那一步是**真的把后端进程重启**（那正是要验的事），
脚本没法在自己进程里做。所以拆成两半，中间由终端去重启。

``setup`` 会把这次上传的明文与原文件名记在 ``scripts/_live_pending_state.json``
（用完即删），``verify`` 拿它逐位比对 —— 「补推之后还解得开」这条才算真的验过。

.. note::

   这是**真链路**：跨进程模式、真 HTTP 节点、真 SQLite。所以它能验到
   ``scripts/verify_pending_restart.py``（进程内、假传输层）验不到的那一层。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

BASE = "http://127.0.0.1:8000"
PWD = "vds12345"
STATE = ROOT / "scripts" / "_live_pending_state.json"
SEG = 512
BLOCKS = 4

c = httpx.Client(base_url=BASE, timeout=180.0, trust_env=False)


def login(user: str, *, server_key: bool = False) -> dict:
    body = {"username": user, "password": PWD}
    if server_key:
        body["server_key"] = True
    r = c.post("/api/auth/login", json=body)
    if r.status_code != 200:
        raise SystemExit(f"登录 {user} 失败：{r.status_code} {r.text[:200]}")
    return {"Authorization": f"Bearer {r.json()['token']}"}


def say(t: str) -> None:
    print()
    print("=" * 72)
    print(t)
    print("=" * 72)


def setup() -> int:
    say("① 造一次真失败：让 node-1 掉线，然后上传")
    h_admin = login("admin")
    h_user = login("zhangsan")

    r = c.post("/api/admin/fault-drill", headers=h_admin,
               json={"action": "knock_out", "nodes": ["node-1"]})
    print(f"  演练 knock_out node-1 → {r.status_code} "
          f"{json.dumps(r.json(), ensure_ascii=False)[:160]}")

    key = f"live-pending-{int(time.time())}"
    data = bytes((i * 11) % 251 for i in range(SEG * BLOCKS))
    r = c.post("/api/files", headers=h_user,
               files={"file": (f"{key}.bin", data, "application/octet-stream")},
               data={"file_key": key, "segment_bytes": str(SEG)})
    print(f"  上传 {key}（{BLOCKS} 块）→ **{r.status_code}** "
          f"{json.dumps(r.json(), ensure_ascii=False)[:220]}")

    r = c.get("/api/nodes/pending", headers=h_user)
    pend = r.json()
    print(f"  /api/nodes/pending → {json.dumps(pend, ensure_ascii=False)[:300]}")

    ok = (
        r.status_code == 200
        and (pend.get("pending") or {}).get("nodes") == ["node-1"]
    )
    STATE.write_text(
        json.dumps({"file_key": key, "hex": data.hex()}, ensure_ascii=False),
        encoding="utf-8",
    )
    print()
    print(f"  [{'PASS' if ok else 'FAIL'}] 写如约失败、且盘上留下了现场")
    print(f"  状态已记到 {STATE.name}，请**重启后端**，再跑 verify")
    return 0 if ok else 1


def verify() -> int:
    say("② 重启之后（这一句能跑起来，本身就说明没有拒绝启动）")
    h_user = login("zhangsan")
    h_admin = login("admin")          # ★ 补推是管理员动作（普通用户会 403）
    h_own = login("zhangsan", server_key=True)
    st = json.loads(STATE.read_text(encoding="utf-8"))
    key, want = st["file_key"], bytes.fromhex(st["hex"])
    bad: list[str] = []

    def check(name: str, cond: bool, extra: str = "") -> None:
        print(("  PASS  " if cond else "  FAIL  ") + name
              + (("   :: " + extra) if extra else ""))
        if not cond:
            bad.append(name)

    r = c.get("/api/nodes/pending", headers=h_user)
    pend = r.json()
    print(f"  /api/nodes/pending → {json.dumps(pend, ensure_ascii=False)[:400]}")
    check("★★ 重启后能登录（没有以「存储节点与协调者不同步」拒绝启动）", True)
    note = pend.get("site_note") or ""
    live = pend.get("pending")
    # ★ 两条都算通过：现场还在（等着人工点「补推」），或者**后端一起来就自己
    #   补推收敛了**（自动补推那一轮成功了）。后者不是"没验到"——它恰好说明
    #   「恢复现场 → 自动补推 → 落库」整条路在真链路上走通了。
    check("★ 现场是从盘上恢复的（或已被自动补推收敛）",
          pend.get("restored") is True or "已恢复" in note or live is None,
          note)
    if live is not None:
        check("★ 待补推还在，且点名 node-1",
              (live or {}).get("nodes") == ["node-1"], str(live))
    else:
        print("        （后端一起来就自己补推收敛了："
              f"auto = {json.dumps(pend.get('auto'), ensure_ascii=False)}）")

    r = c.get("/api/nodes", headers=h_user)
    rep = r.json()
    rep = rep.get("nodes") if isinstance(rep, dict) else rep
    check("★ 4 台节点视图全合法", all(x.get("valid") for x in rep),
          str([x.get("valid") for x in rep]))

    say("③ 点一次「补推」")
    r = c.post("/api/nodes/retry-push", headers=h_admin)
    out = r.json()
    print(f"  retry-push → {json.dumps(out, ensure_ascii=False)[:260]}")
    if out.get("op"):
        check("★★ 补推收敛（ok=true）", out.get("ok") is True)
        check("★★ 落库那一步也补上了", out.get("persisted") is True)
        check("★ 现场清空", out.get("pending") is None)
    else:
        print("        （没有待补推的更新 —— 已经自动收敛过了）")

    r = c.get("/api/nodes/pending", headers=h_user)
    check("★ 再查一次：现场确实没了", r.json().get("pending") is None)
    check("★ 盘上那行也抹掉了（restored 回落到 false）",
          r.json().get("restored") is False)

    r = c.get("/api/files", headers=h_user)
    files = r.json()
    files = files.get("files") if isinstance(files, dict) else files
    hit = next((f for f in files if f.get("file_key") == key), None)
    check("★★ 补推之后文件出现在列表里", hit is not None, str(hit)[:120])
    if hit is None:
        print("结果：有未通过项 ✗")
        return 1
    check("★ 块数与上传时一致", hit.get("block_count") == BLOCKS,
          str(hit.get("block_count")))

    r = c.post(f"/api/files/{hit['id']}/decrypt", headers=h_own, json={})
    check("★★ 解出来逐位相同", r.status_code == 200
          and bytes.fromhex(r.json().get("data_hex", "")) == want,
          f"{r.status_code} 实得 {len(r.json().get('data_hex', '')) // 2} 字节 / 应为 {len(want)} 字节")

    print()
    print("=" * 72)
    print("结果：" + ("★ 真链路通过 —— 现场跨重启活下来了，补推之后逐位可解"
                      if not bad else f"{len(bad)} 条未通过 ✗"))
    for b in bad:
        print("   - " + b)
    print("=" * 72)
    return 1 if bad else 0


def cleanup() -> int:
    """删掉本探针造过的测试文件（``live-pending-*``），并报告当前状态。"""
    h_user = login("zhangsan")
    h_admin = login("admin")
    r = c.get("/api/files", headers=h_user)
    files = r.json()
    files = files.get("files") if isinstance(files, dict) else files
    for f in files:
        if str(f.get("file_key", "")).startswith("live-pending-"):
            d = c.delete(f"/api/files/{f['id']}", headers=h_user)
            print(f"  删除 {f['file_key']} → {d.status_code}")
    files = c.get("/api/files", headers=h_user).json()
    files = files.get("files") if isinstance(files, dict) else files
    print(f"  剩余文件：{[f['file_key'] for f in files]}")
    print(f"  待补推：{c.get('/api/nodes/pending', headers=h_user).json().get('pending')}")
    print(f"  节点：{[(x['node_id'], x.get('unreachable', False)) for x in c.get('/api/nodes', headers=h_user).json()]}")
    if STATE.exists():
        STATE.unlink()
        print(f"  已删 {STATE.name}")
    return 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "setup":
        sys.exit(setup())
    if cmd == "verify":
        sys.exit(verify())
    if cmd == "cleanup":
        sys.exit(cleanup())
    print(__doc__)
    sys.exit(2)
