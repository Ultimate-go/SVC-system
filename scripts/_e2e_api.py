"""接口层端到端（打真的 8000 端口）—— 上传 / 改块 / 追加 / 截断 / 检索 /
批量验证 / 证据池 / 存储证明 / 故障演练，一条一条走下来。

★ 为什么要有它（而不是只靠那几个 ``verify_*.py``）：
  那些脚本大多在**进程内**跑，而这次线上事故恰恰是"进程内全绿、跨进程全挂"。
  本脚本打的是**真的后端 + 真的 4 台节点进程**，走真的 HTTP 与真的 SQLite。

用法（服务要先起着）：python scripts/_e2e_api.py
"""

from __future__ import annotations

import base64
import json
import sys
import time

import httpx

BASE = "http://127.0.0.1:8000"
PWD = "vds12345"

# ★ 每轮都用**唯一**文件名：旧的一轮会把文件留在库里，重跑时同一个
#   file_key 会 409（而 FID 变成 None，后面所有断言跟着级联失败 ——
#   看起来像“全挂了”，其实只有第一条是原因）。
RUN = time.strftime("%H%M%S")
KEY_A = f"e2e-a-{RUN}"
KEY_B = f"e2e-b-{RUN}"

FAILURES: list[str] = []
OK = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global OK
    if cond:
        OK += 1
    else:
        FAILURES.append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name
          + (("   :: " + extra) if extra else ""))


def section(t: str) -> None:
    print()
    print("=" * 72)
    print(t)
    print("=" * 72)


def show(r: httpx.Response, limit: int = 200) -> str:
    body = r.text
    return f"{r.status_code} {body[:limit]}"


c = httpx.Client(base_url=BASE, timeout=180.0, trust_env=False)


def login(user: str) -> httpx.Headers:
    r = c.post("/api/auth/login", json={"username": user, "password": PWD})
    if r.status_code != 200:
        raise SystemExit(f"登录 {user} 失败：{show(r)}")
    tok = r.json()["token"]
    return {"Authorization": f"Bearer {tok}"}


section("0. 登录与状态")
H_ADMIN = login("admin")
H_USER = login("zhangsan")
# ★ 默认是“客户端解封”模型：后端手里没私钥，所以 /decrypt 一定 403
#   （那是**设计如此**，不是 bug）。要在这里做“解出原文”的逐位比对，
#   得用文档里那条对比路径：server_key=true（后端代管私钥）。
_r = c.post("/api/auth/login",
            json={"username": "zhangsan", "password": PWD, "server_key": True})
H_OWN = {"Authorization": f"Bearer {_r.json()['token']}"}
check("管理员登录成功", bool(H_ADMIN["Authorization"]))
check("普通用户登录成功", bool(H_USER["Authorization"]))
check("server_key 对比模型登录成功", bool(H_OWN["Authorization"]))

r = c.get("/api/status", headers=H_USER)
check("GET /api/status 200", r.status_code == 200, show(r, 120))
st = r.json()
print("        status keys:", ", ".join(sorted(st)[:14]))

r = c.get("/api/nodes", headers=H_USER)
check("GET /api/nodes 200", r.status_code == 200, show(r, 120))
nodes = r.json()
report = nodes.get("nodes") if isinstance(nodes, dict) else nodes
check("4 台节点都在", len(report) == 4, f"{len(report)} 台")
check("4 台节点视图都合法", all(x.get("valid") for x in report),
      str([x.get("valid") for x in report]))


section("1. ★ 上传（线上就是卡在这一步）")
DATA1 = bytes((i * 7) % 251 for i in range(3000))
r = c.post(
    "/api/files",
    headers=H_USER,
    files={"file": (f"{KEY_A}.bin", DATA1, "application/octet-stream")},
    data={"file_key": KEY_A, "segment_bytes": "512"},
)
check("★ POST /api/files 建成功（不再 503）", r.status_code in (200, 201), show(r, 400))
up = r.json()
FID = up.get("id")
BLOCKS_A = up.get("block_count")
print(f"        id={FID} 块数={BLOCKS_A} 段落={up.get('segments')}")
check("上传返回了块数", isinstance(BLOCKS_A, int) and BLOCKS_A > 0, str(BLOCKS_A))

r = c.get("/api/files", headers=H_USER)
check("GET /api/files 200", r.status_code == 200, show(r, 120))
files = r.json()
rows = files.get("files") if isinstance(files, dict) else files
check("文件列表里能看到它", any(x.get("id") == FID for x in rows),
      json.dumps(rows, ensure_ascii=False)[:200])

r = c.get(f"/api/files/{FID}", headers=H_USER)
check("GET /api/files/{id} 200", r.status_code == 200, show(r, 200))
detail = r.json()
check("详情带块数", bool(detail.get("block_count")), str(detail.get("block_count")))

r = c.post(
    "/api/files",
    headers=H_USER,
    files={"file": (f"{KEY_B}.bin", b"B" * 1500, "application/octet-stream")},
    data={"file_key": KEY_B, "segment_bytes": "512"},
)
check("第二份文件也传得上（各自一段位置）", r.status_code in (200, 201), show(r, 300))
FID2 = r.json().get("id")
check("★ 第二份文件拿到了**另一段**位置（段不重叠）",
      r.json().get("segments") != up.get("segments"),
      f"{up.get('segments')} vs {r.json().get('segments')}")


section("2. 检索 + 承诺验证（读回来自己算分量）")
r = c.post("/api/query", headers=H_USER, json={"indices": [0, 1]})
check("POST /api/query 200", r.status_code == 200, show(r, 300))
q = r.json().get("verify") or r.json()
check("★ 检索结果验证通过", bool(q.get("ok")), str(q)[:200])

r = c.post("/api/query/files", headers=H_USER,
           json={"targets": [["zhangsan", KEY_A]], "block_indices": [[0, 1]]})
check("POST /api/query/files 200", r.status_code == 200, show(r, 250))
if r.status_code == 200:
    _b = r.json()
    check("★ 按文件检索也验证通过",
          bool((_b.get("verify") or _b).get("ok")),
          json.dumps(_b, ensure_ascii=False)[:200])


section("3. 改块 / 追加 / 截断（三种更新都要推给每台节点）")
# 改块 0 → 'Z'*512；末尾追加 'Q'*512。于是期望的明文是：
#     "Z"*512 + DATA1[512:] + "Q"*512
EXPECT1 = b"Z" * 512 + DATA1[512:] + b"Q" * 512
r = c.patch(
    f"/api/files/{FID}",
    headers=H_USER,
    json={"op": "modify", "block_idx": 0,
          "data_b64": base64.b64encode(b"Z" * 512).decode()},
)
check("★ PATCH 改块 200", r.status_code == 200, show(r, 300))

r = c.patch(
    f"/api/files/{FID}",
    headers=H_USER,
    json={"op": "append", "data_b64": base64.b64encode(b"Q" * 512).decode()},
)
check("★ PATCH 追加 200", r.status_code == 200, show(r, 300))

r = c.patch(
    f"/api/files/{FID2}", headers=H_USER,
    json={"op": "truncate", "drop_blocks": 1},
)
check("★ PATCH 截断 200", r.status_code == 200, show(r, 300))

r = c.get("/api/nodes", headers=H_USER)
rep2 = r.json()
rep2 = rep2.get("nodes") if isinstance(rep2, dict) else rep2
check("★ 三种更新之后，4 台节点视图仍合法",
      all(x.get("valid") for x in rep2), str([x.get("valid") for x in rep2]))


section("4. 解密（所有者才行；默认模型是浏览器解封）")
r = c.post(f"/api/files/{FID}/decrypt", headers=H_USER, json={})
check("★ 默认（客户端解封）模型下后端拒绝解密 —— 它手里本来就没私钥",
      r.status_code == 403, show(r, 200))
r = c.post(f"/api/files/{FID}/decrypt", headers=H_OWN, json={})
check("所有者能解密（server_key 对比模型）", r.status_code == 200, show(r, 250))
if r.status_code == 200:
    got = bytes.fromhex(r.json().get("data_hex") or "")
    check("★ 解出来的字节与预期逐位相同", got == EXPECT1,
          f"{len(got)} vs {len(EXPECT1)}")
r = c.post(f"/api/files/{FID}/decrypt", headers=H_ADMIN, json={})
check("★ 别人（含管理员）解不开 —— 密码学那道门",
      r.status_code in (403, 400, 409), show(r, 200))


section("5. 批量验证 / 跨文件聚合 / 存储证明")
_q = c.post("/api/query", headers=H_USER, json={"indices": [0, 1]}).json()
card = {"indices": _q["indices"], "values": _q["values"], "proof": _q["proof"]}
r = c.post("/api/query/verify-batch", headers=H_USER,
           json={"items": [card]})
check("POST /api/query/verify-batch 200", r.status_code == 200, show(r, 250))
if r.status_code == 200:
    check("★ 批量验证（一次结论）通过", bool(r.json().get("ok")), show(r, 250))

r = c.post("/api/por", headers=H_USER, json={})
check("POST /api/por 200", r.status_code == 200, show(r, 250))
if r.status_code == 200:
    check("★ 存储证明通过（各节点确实还存着）",
          bool(r.json().get("ok")), json.dumps(r.json(), ensure_ascii=False)[:220])


section("6. ★ 故障演练必须**一个字节都不删**（上次就是把真数据删了）")

r = c.get("/api/admin/fault-drill", headers=H_ADMIN)
check("GET /api/admin/fault-drill 200", r.status_code == 200, show(r, 200))
base = r.json()
check("演练前没有故障节点", not base.get("down") and not base.get("destroyed"),
      json.dumps(base, ensure_ascii=False)[:200])
# ★ 基线要在**演练前这一刻**取（上面已经改块+追加过了，块数不再是上传时的 6）。
BLOCKS_PRE = c.get(f"/api/files/{FID}", headers=H_USER).json().get("block_count")

r = c.get("/api/nodes", headers=H_USER)
before = r.json()
before = before.get("nodes") if isinstance(before, dict) else before
held_before = {x["node_id"]: x.get("held") for x in before}

r = c.post("/api/admin/fault-drill", headers=H_ADMIN,
           json={"action": "knock_out", "nodes": ["node-1", "node-2"],
                 "mode": "destroyed"})
check("★ 标记 2 台损毁 200", r.status_code == 200, show(r, 300))
impact = r.json().get("impact") or {}
note = str(impact.get("note", ""))
print("        影响面：", note[:200])
check("★ 影响面说清「本演练不删数据」", "不删数据" in note, note[:200])
check("★ 影响面报出了会丢哪几块（预判）", int(impact.get("lost_count") or 0) > 0,
      str(impact.get("lost_count")))

r = c.post("/api/admin/fault-drill", headers=H_ADMIN,
           json={"action": "restore"})
check("★ 恢复 200", r.status_code == 200, show(r, 200))

r = c.get("/api/nodes", headers=H_USER)
after = r.json()
after = after.get("nodes") if isinstance(after, dict) else after
held_after = {x["node_id"]: x.get("held") for x in after}
check("★★ 演练前后各节点持有的块数**逐台相同**（真的没删）",
      held_before == held_after, f"前={held_before} 后={held_after}")
check("★ 恢复后视图仍合法", all(x.get("valid") for x in after),
      str([x.get("valid") for x in after]))

r = c.get(f"/api/files/{FID}", headers=H_USER)
check("★ 演练之后文件还在、块数不变",
      r.status_code == 200 and r.json().get("block_count") == BLOCKS_PRE,
      f"{r.json().get('block_count')} vs {BLOCKS_PRE}")
r = c.post(f"/api/files/{FID}/decrypt", headers=H_OWN, json={})
check("★★ 演练之后**还能解出原文**（数据真的没动）", r.status_code == 200,
      show(r, 200))
if r.status_code == 200:
    got = bytes.fromhex(r.json().get("data_hex") or "")
    check("★★ 演练后解出来的字节与演练前**逐位相同**", got == EXPECT1,
          f"{len(got)} vs {len(EXPECT1)}")


section("7. 管理员面：自检 / 部署 / 端口 / 审计")
r = c.post("/api/admin/check", headers=H_ADMIN)
check("POST /api/admin/check 200", r.status_code == 200, show(r, 250))
r = c.get("/api/admin/deploy", headers=H_ADMIN)
check("GET /api/admin/deploy 200", r.status_code == 200, show(r, 200))
r = c.get("/api/admin/ports", headers=H_ADMIN)
check("GET /api/admin/ports 200", r.status_code == 200, show(r, 200))
r = c.get("/api/admin/audit", headers=H_ADMIN)
check("GET /api/admin/audit 200", r.status_code == 200, show(r, 200))
r = c.get("/api/perf/summary", headers=H_ADMIN)
check("GET /api/perf/summary 200", r.status_code == 200, show(r, 150))


section("8. 删除（del 要走 /node/delete 那条路）")
r = c.delete(f"/api/files/{FID2}", headers=H_USER)
check("★ DELETE 文件 200", r.status_code == 200, show(r, 300))
r = c.get("/api/files", headers=H_USER)
rows = r.json()
rows = rows.get("files") if isinstance(rows, dict) else rows
check("删掉之后列表里没有它", all(x.get("id") != FID2 for x in rows))
r = c.get("/api/nodes", headers=H_USER)
rep4 = r.json()
rep4 = rep4.get("nodes") if isinstance(rep4, dict) else rep4
check("★ 删除之后各节点视图仍合法", all(x.get("valid") for x in rep4),
      str([x.get("valid") for x in rep4]))
r = c.post(f"/api/files/{FID}/decrypt", headers=H_OWN, json={})
check("剩下那份文件仍然解得开", r.status_code == 200, show(r, 150))
if r.status_code == 200:
    got = bytes.fromhex(r.json().get("data_hex") or "")
    check("★★ 删掉另一份之后，这份的字节仍然逐位相同", got == EXPECT1,
          f"{len(got)} vs {len(EXPECT1)}")

section("9. ★ 掉线 → 写失败（本次作废）→ 恢复 → 重做一次成功")

# 这一节把**线上那次报错描述的整套流程**真跑一遍。它盯三件事：
#  1. 掉线期间广播写必须**失败**（而不是把更新悄悄推给一台"已经没了"的机器）；
#  2. 失败之后这次写**整次作废**：登记表退回、账面不推进，文件块数一个字不变；
#     而 /api/status 等**每个页面都在轮询**的接口**不能 500**；
#  3. 修好那台之后**重做一次**，全绿。
KEY_C = f"e2e-c-{RUN}"
r = c.post(
    "/api/files",
    headers=H_USER,
    files={"file": (f"{KEY_C}.bin", b"C" * 2048, "application/octet-stream")},
    data={"file_key": KEY_C, "segment_bytes": "512"},
)
check("先传一份新文件（它占向量末尾，才删得动/加得上）",
      r.status_code in (200, 201), show(r, 200))
FID3 = r.json().get("id")
_b_pre = c.get(f"/api/files/{FID3}", headers=H_USER).json().get("block_count")

r = c.post("/api/admin/fault-drill", headers=H_ADMIN,
           json={"action": "knock_out", "nodes": ["node-1"], "mode": "down"})
check("★ 演练：把 node-1 标成掉线", r.status_code == 200, show(r, 200))

r = c.patch(
    f"/api/files/{FID3}", headers=H_USER,
    json={"op": "append", "data_b64": base64.b64encode(b"D" * 300).decode()},
)
check("★★ 掉线期间写**失败**（不再是「假成功」）", r.status_code == 503, show(r, 300))
check("★ 报错说清是「没跟上」", "没跟上" in r.text, show(r, 300))

# ★ 这次写应当**整次作废**：已分配的位置退回登记表，文件账目不动。
_b_post = c.get(f"/api/files/{FID3}", headers=H_USER).json().get("block_count")
check("★★ 失败的那次追加**没有落账**（块数与失败前逐个相同）",
      _b_post == _b_pre, f"{_b_post} vs {_b_pre}")
check("★★ 退回登记表之后还没有撞游标 —— 重传一次文件仍然可以",
      c.post("/api/files", headers=H_USER,
             files={"file": (f"e2e-probe-{RUN}.bin", b"P", "application/octet-stream")},
             data={"file_key": f"e2e-probe-{RUN}", "segment_bytes": "512"}
             ).status_code in (200, 201))

# ★★ 写失败之后，**每个页面都会轮询**的那些接口必须照常可用。
#    以前 /api/status 会在这里 500：``under_replicated`` 走到"登记表已分配、
#    但还没落账"的那个新下标，``replicas_of`` 直接 KeyError ——
#    于是整个界面全是「服务器内部错误」，恰恰在最该看清楚的时候。
for _path in ("/api/status", "/api/nodes", "/api/files",
              "/api/admin/deploy", "/api/perf/summary", "/api/query/files"):
    _r = c.get(_path, headers=H_ADMIN) if _path.startswith("/api/admin") or _path.startswith("/api/perf") \
        else c.get(_path, headers=H_USER)
    check(f"★★ 写失败之后 {_path} 不 500", _r.status_code < 500, show(_r, 160))

_r = c.post("/api/admin/check", headers=H_ADMIN)
check("★★ 写失败之后自检**仍然全绿**（账面没有留下脏数据）",
      _r.status_code == 200 and bool(_r.json().get("ok")), show(_r, 260))

r = c.post("/api/admin/fault-drill", headers=H_ADMIN, json={"action": "restore"})
check("★ 演练：恢复 node-1", r.status_code == 200, show(r, 200))

# ★ 恢复之后**重做一次**同一个追加 —— 这次应该成功。
r = c.patch(
    f"/api/files/{FID3}", headers=H_USER,
    json={"op": "append", "data_b64": base64.b64encode(b"D" * 300).decode()},
)
check("★★ 恢复之后重做同一次追加**成功**", r.status_code == 200, show(r, 300))
_b_done = c.get(f"/api/files/{FID3}", headers=H_USER).json().get("block_count")
check("★★ 重做之后块数确实长上去了", int(_b_done) > int(_b_pre),
      f"{_b_done} vs {_b_pre}")

_r = c.get("/api/nodes", headers=H_USER).json()
_r = _r.get("nodes") if isinstance(_r, dict) else _r
check("★★ 重做之后 4 台节点全部视图合法", all(x.get("valid") for x in _r),
      str([x.get("valid") for x in _r]))
check("★★ 重做之后不再有「连不上」的机器",
      not any(x.get("unreachable") for x in _r),
      str([x.get("unreachable") for x in _r]))
_r = c.post("/api/admin/check", headers=H_ADMIN)
check("★★ 重做之后自检全绿", _r.status_code == 200 and bool(_r.json().get("ok")),
      show(_r, 250))

_r = c.post(f"/api/files/{FID3}/decrypt", headers=H_OWN, json={})
check("★★ 走完整套故障流程之后，文件仍然解得开",
      _r.status_code == 200, show(_r, 200))
if _r.status_code == 200:
    _got = bytes.fromhex(_r.json().get("data_hex") or "")
    # ★ 期望值就是上传的 2048 字节 + 那次**重做成功**的追加。
    #   失败的那一次按约定根本没发生，所以它不在这份期望值里。
    _want = b"C" * 2048 + b"D" * 300
    check("★★ 解出来的字节与上传时逐位相同（演练 + 写失败都没伤到数据）",
          _got == _want, f"实得 {len(_got)} 字节 / 应为 {len(_want)} 字节")

_r = c.delete(f"/api/files/{FID3}", headers=H_USER)
check("收尾：把这一节的文件删掉", _r.status_code == 200, show(_r, 200))

c.close()

# ★ 收尾：把自己造的那份也删掉 —— 这是演示库，别留下一堆 e2e-* 文件。
with httpx.Client(base_url=BASE, timeout=180.0, trust_env=False) as _c:
    _c.delete(f"/api/files/{FID}", headers=H_USER)

print()
print("=" * 72)
if FAILURES:
    print(f"通过 {OK} 项，失败 {len(FAILURES)} 项：")
    for f in FAILURES:
        print("  - " + f)
    sys.exit(1)
print(f"全部通过 ✓ （{OK} 项）")
