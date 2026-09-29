"""对**正在运行的后端**做端到端冒烟。

跑法::

    # 终端 1
    python -m uvicorn backend.main:app --port 8099
    # 终端 2
    python scripts/smoke.py                  # 默认打 127.0.0.1:8099
    python scripts/smoke.py --base http://127.0.0.1:8000
    python scripts/smoke.py --seed           # 先灌演示数据再冒烟

它顺着需求走一遍，并把每一步的结论打出来 —— 很适合答辩前自查，
也很适合当作"服务到底通不通"的一键验证。

**为什么用 Python 而不是 PowerShell**：PowerShell 5.1 把无 BOM 的 ``.ps1``
当 ANSI 读，脚本里的中文会被打散成语法错误；而且它对不带 charset 的
``application/json`` 按 Latin-1 解码，中文全变乱码。Python 两个问题都没有。
"""

from __future__ import annotations

import argparse
import base64
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

PASSWORD = "vds12345"


class Smoke:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        # ★ trust_env=False：同 node_service/client.py 里那段理由 —— 后端就在
        #   本机回环上，别让 Windows 的系统代理（httpx 在环境变量为空时会回落到
        #   urllib.getproxies()，那会读注册表里的 IE 代理设置）把它接管走。
        #   否则机器上开着系统代理而代理没在跑时，冒烟脚本会报“后端连不上”，
        #   而后端其实好好地听着。
        self.client = httpx.Client(base_url=self.base, timeout=60.0, trust_env=False)
        self.failures: list[str] = []

    # -- 小工具 -------------------------------------------------------------

    def step(self, text: str) -> None:
        print(f"\n=== {text} ===")

    def check(self, cond: bool, what: str) -> None:
        mark = "OK " if cond else "!! "
        print(f"  {mark}{what}")
        if not cond:
            self.failures.append(what)

    def login(self, username: str) -> dict[str, str]:
        try:
            r = self.client.post(
                "/api/auth/login", json={"username": username, "password": PASSWORD}
            )
            r.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # ★ 打错目标要**说人话**：原来这里会甩出一整段 httpx 堆栈
            #   （我拿节点端口 9101 试过：`Client error '404 Not Found' for
            #   url '.../api/auth/login'` + 六行调用栈），看的人得自己猜
            #   "我打错端口了"。退出码虽然是对的，但这不是给人看的报错。
            raise SystemExit(
                f"★ {self.base} 不是本系统的后端：/api/auth/login 回了 "
                f"{exc.response.status_code}。\n"
                f"  冒烟要打**后端**端口（默认 8000）；节点端口（9101-9104）"
                f"每个接口都要令牌、不会回这个路径。"
            ) from None
        return {"Authorization": f"Bearer {r.json()['token']}"}

    # -- 各步 ---------------------------------------------------------------

    def run(self) -> int:
        self.step("1. 无令牌访问应被拒")
        r = self.client.get("/api/status")
        self.check(r.status_code == 401, f"/api/status 无令牌 -> {r.status_code}")

        self.step("2. 登录（管理员 / 用户）")
        nurse = self.login("nurse")
        owner = self.login("zhangsan")
        admin = self.login("admin")
        me = self.client.get("/api/auth/me", headers=nurse).json()
        print(f"  nurse 有密钥对 = {me['has_key']}   公钥 = {me['pub_key'][:24]}…")
        self.check(
            "pwd_hash" not in me and "password" not in me, "返回体不含口令哈希"
        )
        self.check(
            "sk_wrapped" not in me and "attrs" not in me,
            "返回体不含私钥（连它的密文也不给）",
        )
        self.check(me["has_key"] is True, "演示账号都带着一对 SM2 密钥")

        self.step("3. 文件列表 —— 验证不受限：所有人都看得到全部文件")
        files = self.client.get("/api/files", headers=nurse).json()
        print(f"  nurse 看到 {len(files)} 个文件")
        for f in files:
            print(
                f"    #{f['id']}  {f['owner']:9s} {f['block_count']:>2d} 块  "
                f"下标 {f['first_index']}-{f['last_index']}  "
                f"我的={f['is_mine']}  可解密={f['can_decrypt']}"
            )
        self.check(len(files) > 0, "有文件可查")

        mine = [f for f in files if f["owner"] == "zhangsan" and f["block_count"] >= 2]
        other = [f for f in files if f["owner"] != "zhangsan"]
        if not mine:
            print("  （演示数据不足，先跑 python scripts/seed.py --demo）")
            return 1
        target = mine[0]

        self.step("4. 验证不受限：nurse 验别人文件的全部块")
        q = self.client.post(
            "/api/query", json={"indices": target["indices"]}, headers=nurse
        ).json()
        print(
            f"  ok={q['ok']}  凭证 {q['cert_count']} 份  服务器 {q['nodes_used']}\n"
            f"  证据规模 {q['proof']['size_bytes']} 字节  "
            f"验证 {q['verify']['ok']} {q['verify']['message']}"
        )
        self.check(q["ok"] is True, "验证通过")
        self.check(q["hash_layer_ok"] is True, "块哈希层自洽")
        self.check(len(q["values"]) == len(target["indices"]), "取回的分量个数正确")

        self.step("5. 解密受限：nurse 解别人文件 -> 期望 403")
        r = self.client.post(f"/api/files/{target['id']}/decrypt", json={}, headers=nurse)
        self.check(r.status_code == 403, f"状态码 {r.status_code}")
        if r.status_code == 403:
            print(f"  提示：{r.json()['detail']}")

        self.step("6. 被拒绝后仍然可以验证同一块（两条线独立）")
        q2 = self.client.post(
            "/api/query", json={"indices": target["indices"]}, headers=nurse
        )
        self.check(q2.status_code == 200 and q2.json()["ok"], "解密被拒不影响验证")

        self.step("7. 只有所有者能解密；而丢了会话私钥连他自己也解不开")
        d = self.client.post(f"/api/files/{target['id']}/decrypt", json={}, headers=owner)
        self.check(d.status_code == 200, f"所有者解密：{d.status_code}")
        if d.status_code == 200:
            text = bytes.fromhex(d.json()["data_hex"])[:48].decode("utf-8", "replace")
            print(f"  明文前 48 字符：{text!r}")

        # ★ 「退出登录」把**内存里那把私钥**丢掉 —— 令牌本身仍然有效
        #   （JWT 是无状态的），但没了私钥就解不开任何东西。
        out = self.client.post("/api/auth/logout", headers=owner)
        self.check(out.status_code == 200, "退出登录")
        r = self.client.post(f"/api/files/{target['id']}/decrypt", json={}, headers=owner)
        self.check(
            r.status_code == 403, f"丢了会话私钥之后连所有者也被拒：{r.status_code}"
        )
        if r.status_code == 403:
            self.check(
                "重新登录" in r.json()["detail"],
                "提示叫人重新登录（而不是甩一句看不懂的密码学错误）",
            )
        qv = self.client.post(
            "/api/query", json={"indices": target["indices"]}, headers=owner
        )
        self.check(qv.status_code == 200 and qv.json()["ok"], "丢私钥也不影响验证")

        owner = self.login("zhangsan")  # 重新登录，把会话私钥拿回来
        r2 = self.client.post(f"/api/files/{target['id']}/decrypt", json={}, headers=owner)
        self.check(r2.status_code == 200, "重新登录后又解得开了（口令解封了私钥）")

        self.step("8. 跨文件 / 跨用户一次查询 —— 设计 B 的核心")
        picks = [mine[0]]
        if other:
            picks.append(other[0])
        body = {"targets": [[f["owner"], f["file_key"]] for f in picks]}
        j = self.client.post("/api/query/files", json=body, headers=nurse).json()
        owners = {r["owner"] for r in j["refs"]}
        keys = {r["file_key"] for r in j["refs"]}
        print(
            f"  覆盖 {len(j['indices'])} 块 / {len(keys)} 个文件 / {len(owners)} 个用户\n"
            f"  聚合成 {j['cert_count']} 份凭证 -> **一份**证据 {j['proof']['size_bytes']} 字节"
        )
        self.check(j["ok"] is True, "跨文件验证通过")
        self.check(len(j["proof"]) == 4, "证据里只有 4 个字段（两个群元素 + 下标 + 规模）")

        self.step("9. 块分布与自检")
        nodes = self.client.get("/api/nodes", headers=nurse).json()
        for n in nodes:
            print(f"  {n['node_id']}  {n['held']:>3d} 块  跨度 {n['span']:<22s} 合法={n['valid']}")
        st = self.client.get("/api/status", headers=nurse).json()
        # 开了副本之后，各台持有块数之和是 **n × 副本数** ——
        # 而这正是"每块真的存了多份"的体现。所以断言必须连着副本数一起写，
        # 不能沿用单副本时代的 `== n`（那是过期的判据，会误报失败）。
        copies = st["replica_factor"]
        self.check(
            sum(n["held"] for n in nodes) == st["delta"]["n"] * copies,
            f"各节点持有块数之和 == 全局 n × 副本数（{st['delta']['n']} × {copies}）",
        )
        # 每个下标被持有的台数必须**正好**等于副本数：多了说明有残留副本没清，
        # 少了说明有块没存够。两种都是真问题。
        per_index: dict[int, int] = {}
        for n in nodes:
            for i in n["indices"]:
                per_index[i] = per_index.get(i, 0) + 1
        bad = sorted(i for i, c in per_index.items() if c != copies)
        self.check(not bad, f"每个下标都正好有 {copies} 份副本（异常的：{bad or '无'}）")
        self.check(all(n["valid"] for n in nodes), "每台服务器视图合法")
        chk = self.client.post("/api/admin/check", headers=admin)
        self.check(chk.status_code == 200, f"自检接口：{chk.json().get('message')}")

        self.step("10. 登记表反查")
        g0 = target["indices"][0]
        info = self.client.get(f"/api/registry/{g0}", headers=nurse).json()
        print(f"  下标 {g0} -> {info['owner']}/{info['file_key']}#{info['block_idx']} "
              f"存在 {info['holder']}")
        self.check(info["owner"] == target["owner"], "归属正确")

        self.step("11. 审计流水")
        rows = self.client.get("/api/admin/audit?limit=10", headers=admin).json()
        for a in rows[:8]:
            print(f"  {a['action']:<16s} {a['actor']:<10s} ok={str(a['ok']):<5s} {a['target']}")
        self.check(any(a["action"] == "decrypt_denied" for a in rows), "拒绝有留痕")

        self.step("12. 存储证明（PoR）")
        # 问的是**另一个**问题：不是"你给我的这几块对不对"，而是"你还存着吗"。
        # 挑战由服务端随机生成，各节点只答自己沾到的那部分，合并成一份再验一次。
        por = self.client.post("/api/por", json={"lambda_pos": 8}, headers=nurse)
        self.check(por.status_code == 200, f"PoR 接口：{por.status_code}")
        pj = por.json()
        print(
            f"  挑战 {len(pj['challenge'])} 个下标｜参与 {pj['queried_nodes']} 台"
            f"｜问了/答了 {pj['asked_total']}/{pj['answered_total']}"
            f"｜合并 {pj.get('aggregated_shares', '—')} 份份额"
            f"｜证明 {pj['proof_size_bytes']} 字节"
        )
        for sh in pj["shares"]:
            print(f"    {sh['node_id']}  问到 {sh['asked']}  答了 {sh['answered']}")
        self.check(pj["ok"] is True, f"PoR 通过：{pj['message']}")
        self.check(pj["Q"] == pj["challenge"], "判据 Q = r：挑战被收齐")
        # 只审计主副本那一份 —— 所以被问到的下标合起来正好是 r、且互不相交
        self.check(
            pj["asked_total"] == len(pj["challenge"]),
            "各台被问到的下标合起来正好是 r（按主副本归属拆开）",
        )
        # PoR 是在"不下载任何内容"的前提下做的，响应里不该有内容类字段
        self.check(
            not ({"blobs", "values", "data_hex"} & set(pj)),
            "PoR 汇报里不含密文/明文",
        )
        # 发起人不是所有者：与"验证不受限"一致
        self.check(pj["checked_by"] == "nurse", "非所有者也能发起审计")

        self.step("13. 块大小是逐文件的（只读断言）")
        # 为什么只读：块大小在**上传时**定、之后不再改（改块 / 追加都按
        # `rec.segment_bytes` 走）。往正在演示的库里塞新文件会永久改掉演示数据，
        # 所以这一步不传文件 —— 只对**现有**文件做必然成立的检查。
        #
        # ★ 判据为什么不是 `ceil(total_bytes / segment_bytes) == block_count`：
        #   那个等式**不是不变量**。改块允许把一块改短（只要不超过该文件的
        #   块大小），而块数不会跟着重算 —— 于是 total_bytes 不再等于
        #   "满块 × (k-1) + 尾块"。本机的演示库里 #1 与 #6 都已经改过块，
        #   这条等式当场就报错（本脚本第一版正是这么写的，被自己抓出来了）。
        #
        #   真正成立的是**上界**：每块的明文长度都 <= 这份文件的块大小，
        #   所以 sum(plain_len) = total_bytes <= block_count × segment_bytes。
        #   它照样能抓住"报出来的块大小不是这份文件自己的"：
        #   若把部署默认值 1024 当成 #6 的块大小，11687 <= 4 × 1024 立刻假。
        sizes = sorted({f["segment_bytes"] for f in files})
        print(f"  演示库里出现过的块大小：{sizes} 字节")
        for f in files:
            cap = f["block_count"] * f["segment_bytes"]
            self.check(
                f["segment_bytes"] > 0 and f["total_bytes"] <= cap,
                f"#{f['id']} {f['total_bytes']} B <= {f['block_count']} 块 × "
                f"{f['segment_bytes']} B = {cap} B（每块都装得下）",
            )
        det = self.client.get(f"/api/files/{target['id']}", headers=nurse).json()
        self.check(
            det["segment_bytes"] == target["segment_bytes"],
            "详情页与列表里的块大小一致（同一个字段，两处序列化）",
        )

        self.step("14. 批量验证：多份证据一次验（随机系数把 m 条方程合成一条）")
        # 取**两份不同**的证据（挑两个不同文件的首块，下标集合必然不同），
        # 一次验掉。设计 B 下它们本来就在同一条向量里，所以能这样合。
        q1 = self.client.post(
            "/api/query", json={"indices": [files[0]["indices"][0]]}, headers=nurse
        ).json()
        q2 = self.client.post(
            "/api/query", json={"indices": [files[1]["indices"][0]]}, headers=nurse
        ).json()

        def _card(q: dict) -> dict:
            return {
                "indices": q["indices"],
                "values": q["values"],
                "proof": {k: q["proof"][k] for k in ("S_I", "Lambda_I", "I")},
            }

        r = self.client.post(
            "/api/query/verify-batch",
            json={"items": [_card(q1), _card(q2)], "locate": True, "compare": True},
            headers=nurse,
        )
        self.check(r.status_code == 200, f"批量验证接口：{r.status_code}")
        bj = r.json()
        print(
            f"  {bj['n']} 份证据一次验｜两路都过：S={bj['ok_s']} Λ={bj['ok_lambda']}"
            f"\n  批量 {bj['ms_batch']} ms vs 逐份 {bj['ms_separate']} ms"
        )
        self.check(bj["ok"] is True, f"结论：{bj['message']}")
        self.check(bj["bad"] == [], "没有坏的那几份")
        # ★ 它比逐份验**慢**（规划 §13.6 有实测）—— 两个耗时都返回、都打出来，
        #   数字照实报。值钱的地方是"一次结论"这件事本身。
        self.check(
            bj["ms_separate"] is not None and bj["agree"] is True,
            "逐份耗时也一起返回，且两种验法结论一致",
        )

        self.step("15. 读路径缺块：允许部分结果时，结论只覆盖拿到的那些块")
        qp = self.client.post(
            "/api/query",
            json={"indices": list(files[0]["indices"][:2]), "allow_partial": True},
            headers=nurse,
        ).json()
        self.check(
            "missing" in qp and "partial" in qp, "响应里带着 missing / partial 两个字段"
        )
        self.check(
            qp["missing"] == [] and qp["partial"] is False,
            "块齐全时 missing 空、partial=False（开关只影响缺块的情形）",
        )
        self.check(qp["ok"] is True, "块齐全时结论照旧通过")

        self.step("16. 集群与待补推：健康状态下应当是空操作")
        pend = self.client.get("/api/nodes/pending", headers=nurse)
        self.check(pend.status_code == 200, "待补推对任何登录用户可见（δ 是公开的）")
        self.check(pend.json()["pending"] is None, "现在没有待补推的更新")
        self.check(
            self.client.post("/api/nodes/retry-push", headers=nurse).status_code == 403,
            "补推是写操作，普通用户被拒",
        )
        rp = self.client.post("/api/nodes/retry-push", headers=admin)
        self.check(rp.status_code == 200 and rp.json()["ok"] is True, "管理员补推")
        self.check(rp.json()["pending"] is None, "无事可补时也不改任何状态")

        self.step("17. 密钥模型：库里只有密文，私钥从不明文落盘")
        st_kw = self.client.get("/api/status", headers=nurse).json()
        self.check("abe" not in st_kw, "状态里已经没有 ABE 这一项了")
        kw = st_kw["keywrap"]
        print(f"  方案：{kw['scheme']}")
        print(f"  私钥：{kw['private_key_storage']}")
        self.check(
            kw["users"] >= 5 and kw["user_keys"] == kw["users"],
            f"{kw['user_keys']}/{kw['users']} 个账号都有密钥对",
        )
        self.check("口令" in kw["private_key_storage"], "状态里说清了私钥是口令封装的")
        self.check(kw["sessions_with_key"] >= 1, "内存里有本次登录解封出来的会话私钥")

        self.step("18. 管理员也解不开别人的文件（没有主密钥了）")
        adm = self.login("admin")
        r = self.client.post(f"/api/files/{target['id']}/decrypt", json={}, headers=adm)
        self.check(
            r.status_code == 403,
            f"管理员解别人的文件 -> {r.status_code}（以前靠 ABE 主密钥能解，现在没有这东西了）",
        )
        st_adm = self.client.get("/api/files", headers=adm).json()
        row = next(f for f in st_adm if f["id"] == target["id"])
        self.check(row["can_decrypt"] is False, "列表里那把锁与真实结果一致")
        self.check(row["is_mine"] is False, "并且明确标出这不是他的")

        # ★ 需求第 4 条：管理员**也改不了**别人的文件。
        #   ``PATCH`` 的三个变体（modify / append / truncate）归**同一条**判据管
        #   —— 改块要重新封装块密钥，这与上传是同一级别的权限。
        #   不钉这一条的话，四个变体里任何一个被人悄悄放开都不会有人察觉
        #   （而“能写、读不到、查不到、察觉不了”四层叠在一起才是真问题）。
        rp = self.client.patch(
            f"/api/files/{target['id']}",
            json={
                "op": "modify",
                "block_idx": 0,
                "data_b64": base64.b64encode(b"admin-should-not-write").decode(),
            },
            headers=adm,
        )
        self.check(
            rp.status_code == 403,
            f"管理员改别人的文件 -> {rp.status_code}（应当 403）",
        )
        self.check(
            "只有所有者" in rp.json().get("detail", ""),
            "拒绝理由说的是「只有所有者」，而不是“你的角色不够”",
        )

        print("\n" + "=" * 60)
        if self.failures:
            print(f"冒烟失败 {len(self.failures)} 项：")
            for f in self.failures:
                print(f"  - {f}")
            return 1
        self.step("19. 截断：**只能删向量末尾**这条前提被守住（不破坏演示数据）")
        # ★ 为什么这里只验“拒绝路径”：截断是**真的删数据**（不可撤销），
        #   而演示库是留着给人看的 —— 所以这一步只碰“该被拒绝”的情形，
        #   既验了新代码路径，又不留下任何改动。
        #   “真删”的正反面由 tests/test_truncate.py（15 条）与
        #   tests/test_truncate_nodes.py（2 条，4 台**真节点进程**）覆盖。
        #
        # ★ 用**文件所有者**的令牌：改文件**只**要所有者（需求第 4 条把
        #   “管理员”那一档去掉了），而权限检查在参数校验**之前** ——
        #   拿护士的令牌去试，四道拒绝里前两道会是 403，
        #   那就验不到“缺参数 / 不是末尾”这两条了（我第一版就是这么错的）。
        owner_h = self.login(target["owner"])
        miss = self.client.patch(
            f"/api/files/{target['id']}", json={"op": "truncate"}, headers=owner_h
        )
        self.check(
            miss.status_code == 400 and "drop_blocks" in miss.json().get("detail", ""),
            f"缺 drop_blocks 给 400 并说清要什么（拿到 {miss.status_code}）",
        )
        # 挑**全局下标最小**的那份文件：只要演示库里不止一份文件，
        # 它的最后一块就必然不是全局向量的最后一块（最后一块属于下标最大的那份），
        # 所以这次请求**一定会被拒** —— 不会真的删掉任何东西。
        front = min(files, key=lambda f: f["indices"][-1])
        tail_file = max(files, key=lambda f: f["indices"][-1])
        assert front["id"] != tail_file["id"], "演示库至少要两份文件这条才成立"
        r = self.client.patch(
            f"/api/files/{front['id']}",
            json={"op": "truncate", "drop_blocks": 1},
            headers=self.login(front["owner"]),
        )
        detail = r.json().get("detail", "")
        self.check(
            r.status_code == 400 and "末尾" in detail,
            f"删“不是向量末尾”的尾巴被拒，并点出卡住的下标（{r.status_code}）",
        )

        self.step("20. 块大小顾问 /api/plan：建议在允许范围内、放不下时说实话")
        st2 = self.client.get("/api/status", headers=nurse).json()
        plan = self.client.post("/api/plan", json={"size": 300_000}, headers=nurse).json()
        self.check(plan.get("ok") is True, "给了一份建议")
        self.check(
            st2["segment_bytes_min"] <= plan["segment_bytes"] <= st2["segment_bytes_max"],
            f"建议 {plan['segment_bytes']} B 落在允许范围 "
            f"{st2['segment_bytes_min']}–{st2['segment_bytes_max']} B 内",
        )
        self.check(
            plan["blocks"] == -(-300_000 // plan["segment_bytes"]),
            f"块数与文件大小自洽（{plan['blocks']} 块）",
        )
        fine = self.client.post(
            "/api/plan", json={"size": 300_000, "prefer": "finer_updates"}, headers=nurse
        ).json()
        self.check(
            fine["ok"] is True and fine["segment_bytes"] < plan["segment_bytes"],
            "“改块粒度细一点”那一档真的给更小的块",
        )
        huge = st2["segment_bytes_max"] * (st2["crs"]["n_max"] + 10)
        nope = self.client.post("/api/plan", json={"size": huge}, headers=nurse).json()
        self.check(
            nope.get("ok") is False and "放不下" in nope.get("reason", ""),
            "放不下时不许硬凑：ok=false 且说清数字",
        )

        # ★★ 收尾必须**看那份失败清单**：`check()` 一直有记（`self.failures`），
        #   但这里从前无视它 —— 结果就是「上面明明打着 !!，下面照样印
        #   冒烟全部通过、退出码还是 0」。验收工具一旦会说谎，比没有更糟：
        #   它会把"没验"伪装成"验过了"。（我自己就被它骗过一次：改了第 19 步
        #   用错账号拿到 403，两次 !! 夹在"全部通过"里，差点当成没事。）
        if self.failures:
            print(f"★ 冒烟**不通过**：{len(self.failures)} 项没过（上面带 !! 的那些）")
            for what in self.failures:
                print(f"    - {what}")
            return 1

        print("冒烟全部通过。")
        return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8099")
    ap.add_argument("--seed", action="store_true", help="先灌演示数据（--reset --demo）")
    args = ap.parse_args()

    if args.seed:
        subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "seed.py"), "--reset", "--demo"],
            check=True,
        )

    try:
        return Smoke(args.base).run()
    except httpx.ConnectError:
        print(f"连不上 {args.base} —— 先把后端起起来：")
        print("  python -m uvicorn backend.main:app --port 8099")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
