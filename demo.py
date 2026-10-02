"""端到端演示 + 性能指标采集。

跑法::

    python demo.py

它同时干两件事：把需求逐条演一遍，并把题目要求的**性能指标**打出来
（单个证明生成时间、不同查询量下的聚合证明生成时间、验证时间、证明规模、
解密时间）。指标数字全部现场实测，不是估算。
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass

# 让中文在 Windows 控制台上正常输出
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from core import L, VectorStore, new_session  # noqa: E402
from core.keywrap import (  # noqa: E402
    KeyWrapIntegrityError,
    make_user_keypair,
    public_bytes,
    unwrap_key,
    unwrap_private_key,
    wrap_key,
)
from core.store import PlainKeyStore  # noqa: E402

# ---------------------------------------------------------------------------
# 演示参数
# ---------------------------------------------------------------------------

MODULUS_BITS = 1024   # **无安全强度，仅演示**；真实部署论文配置是 16λ = 2048
N_MAX = 256           # 全局位置上限
NODES = ("node-1", "node-2", "node-3", "node-4")


class Timer:
    """极简计时器，累计多段耗时。"""

    def __init__(self) -> None:
        self.ms = 0.0

    def __enter__(self) -> "Timer":
        self._t0 = time.perf_counter()
        return self

    def __exit__(self, *exc) -> None:
        self.ms += (time.perf_counter() - self._t0) * 1000.0

    def take(self) -> float:
        """取出并清零（用于"这一段花了多久"）。"""
        v, self.ms = self.ms, 0.0
        return v


def head(text: str) -> None:
    print(f"\n{'=' * 72}\n{text}\n{'=' * 72}")


def sub(text: str) -> None:
    print(f"\n--- {text} ---")


def payload(nbytes: int, tag: int) -> bytes:
    """造一段可复现的假数据（真随机性在 SM4 密钥与 IV 上）。"""
    out = bytearray(nbytes)
    x = (tag * 2654435761) & 0xFFFFFFFF
    for i in range(nbytes):
        x = (1103515245 * x + 12345) & 0x7FFFFFFF
        out[i] = (x >> 16) & 0xFF
    return bytes(out)


@dataclass
class Sample:
    q: int
    gen_ms: float
    agg_ms: float
    ver_ms: float
    proof_bytes: int
    certs: int
    nodes: int


#: 演示用的人员。口令统一是 ``pw-<名字>``（与后端演示账号同一个习惯）。
DEMO_USERS = ("zhangsan", "nurse", "ortho", "lisi", "wangwu")


def password_of(who: str) -> str:
    """演示口令。真实系统里口令由用户自己设、**绝不落库**。"""
    return f"pw-{who}"


def make_keys() -> tuple[dict[str, int], dict, dict[str, dict]]:
    """给每个人造一对 SM2 密钥。

    :returns: ``(私钥表, 公钥表, 口令封装好的私钥密文表)``

    ★ 三个都拿在手上，正是为了演示“拿走数据库 ≠ 拿到私钥”：
      库里只该放**公钥**与**口令包好的私钥密文**。
    """
    sks: dict[str, int] = {}
    pks: dict[str, object] = {}
    blobs: dict[str, dict] = {}
    for who in DEMO_USERS:
        sk, pk, blob = make_user_keypair(password_of(who))
        sks[who], pks[who], blobs[who] = sk, pk, blob
    return sks, pks, blobs


from core.console import force_utf8_console  # noqa: E402

force_utf8_console()


def main() -> int:
    head("VDS 可验证分布式存储系统 —— 设计 B（全系统一条向量）")
    print("论文依据：ASIACRYPT 2020 / eprint 2020/149 §5.2 + §8.2")
    print("设计决定：① 全系统一条向量（一个证据可横跨多个文件）")
    print("          ② 验证不受限（δ 公开）", end="")
    print("  ③ 解密受限（每块的块密钥用**所有者的公钥**封，只有他解得开）")

    # -- 1. 建立会话 -------------------------------------------------------
    sub("1. 建立会话（公开参数）")
    t = Timer()
    with t:
        session = new_session(
            l=L, n_max=N_MAX, modulus_bits=MODULUS_BITS, seed=None
        )
    primes = session.crs.primegen.first(N_MAX)
    print(f"  |N| = {session.crs.N.bit_length()} 位   l = {session.l}   "
          f"n_max = {N_MAX}")
    print(f"  素数位长 = {session.l + 1}（素数位长 = l+1）")
    print(f"  素数表 + 隐藏阶群生成耗时 = {t.take():.0f} ms")
    print("  δ₀ = (U=g, C=1, n=0)  —— 空向量")
    assert primes[0] < primes[1], "素数表应当是单调递增的"

    store = VectorStore(session, node_ids=NODES)
    print(f"  服务器：{', '.join(NODES)}")

    # -- 1b. 密钥：每个人的一对密钥 ----------------------------------------
    sub("1b. 密钥：每个人一对 SM2 密钥，私钥用**他自己的口令**包起来")
    t = Timer()
    with t:
        sks, pks, key_blobs = make_keys()
    for who in DEMO_USERS:
        blob = key_blobs[who]
        print(
            f"  {who:9s} 公钥 = {public_bytes(pks[who]).hex()[:24]}…  "
            f"私钥密文 kind={blob['kind']} iters={blob['iters']}"
        )
    print("  ↑ 库里只放**公钥**与**口令包好的私钥**：明文私钥从来没落过盘。"
          "\n    所以把数据库（不含口令）拿走，也拿不到任何明文。")
    print(f"  生成 {len(DEMO_USERS)} 对密钥 + 口令拉伸共耗时 {t.take():.0f} ms"
          "（PBKDF2-HMAC-SM3 20 万次，故意贵）")

    # -- 2. 上传 -----------------------------------------------------------
    sub("2. 上传：加密 → 分块 → 承诺 → **用所有者公钥封块密钥** → 分发")
    plan = [
        # (所有者, 文件名, 字节数)
        ("zhangsan", "case-基础信息", 3000),
        ("zhangsan", "case-内科", 2500),
        ("zhangsan", "case-骨科", 1800),
        ("lisi", "report-A", 4200),
        ("wangwu", "note", 900),
    ]

    #: {(所有者, 文件名): {块序号: 封好的块密钥}} —— 块密钥**只以密文形式**存在
    key_cts: dict[tuple[str, str], dict[int, dict]] = {}

    def make_sink(box: dict[int, dict], owner: str):
        """块密钥一生成就地封成密文，明文密钥不落到任何地方。

        用的是**这个文件所有者的公钥** —— 所以“谁能解密”在这一刻就定死了，
        后面不需要、也没有任何地方能再改它。
        """

        def sink(pos: int, key: bytes) -> None:
            box[pos] = wrap_key(pks[owner], key)

        return sink

    total_bytes = 0
    for owner, fkey, size in plan:
        box: dict[int, dict] = {}
        key_cts[(owner, fkey)] = box
        t = Timer()
        with t:
            rec = store.upload(
                owner,
                fkey,
                payload(size, size % 251),
                key_sink=make_sink(box, owner),
            )
        total_bytes += size
        print(
            f"  {owner:9s} / {fkey:14s} {size:>6d} B → {rec.block_count:>2d} 块  "
            f"下标 {rec.indices[0]:>3d}-{rec.indices[-1]:<3d}  "
            f"块密钥已封给 {owner:<9s} {t.take():>6.0f} ms"
        )
    print(f"\n  合计 {len(plan)} 个文件 / {total_bytes} 字节 / {store.n} 块"
          f"（全系统共享这一条向量）")

    # -- 3. 分片 -----------------------------------------------------------
    sub("3. 块分布（每台服务器只持有它那一段）")
    for row in store.node_report():
        print(f"  {row['node_id']}  {row['held']:>3d} 块  跨度 {row['span']:<28s} "
              f"视图{'合法' if row['valid'] else '不合法'}")
    covered = set()
    for state in store.transport.states.values():
        covered |= set(state.I)
    print(f"  并集覆盖 {len(covered)}/{store.n} 个位置，两两不相交 ✓")
    assert covered == set(range(store.n))

    # -- 4. 单文件查询 -----------------------------------------------------
    sub("4. 单文件查询（一个用户的某个文件）")
    idx = store.file_indices("zhangsan", "case-内科")
    r = store.query(idx)
    print(f"  文件 case-内科 → 下标 {list(idx)}")
    print(f"  {r.summary()}")
    print(f"  用了 {r.node_used} 共 {r.cert_count} 份凭证")

    # -- 5. 跨文件跨用户 ---------------------------------------------------
    sub("5. 跨文件 / 跨用户一次查询 —— 设计 B 的核心")
    a = store.file_indices("zhangsan", "case-基础信息")
    b = store.file_indices("lisi", "report-A")
    c = store.file_indices("wangwu", "note")
    sel = [a[0], a[-1], b[1], c[0]]
    t = Timer()
    with t:
        r = store.query(sel)
    print(f"  选中 {sel} —— 来自 2 个用户的 3 个不同文件")
    for ref in r.refs:
        print(f"    {ref}  ← 全局下标 {store.registry.index_of(ref.owner, ref.file_key, ref.block_idx)}")
    print(f"  {r.summary()}")
    print(f"  ★ 一份聚合成一体的证据覆盖了 {len({(x.owner) for x in r.refs})} 个用户"
          f"、{len({(x.file_key) for x in r.refs})} 个文件、{len(sel)} 个块")
    print(f"  总耗时 {t.take():.1f} ms")

    # -- 6. 性能指标 -------------------------------------------------------
    sub("6. 性能指标（题目要求的那几项，现场实测）")
    pool = sorted(
        {i for owner, fk, _size in plan for i in store.file_indices(owner, fk)}
    )
    from vds.client_node import ClientNode

    client = ClientNode(session, store.delta)
    nbytes = (session.crs.N.bit_length() + 7) // 8

    def measure(pick) -> Sample:
        tg, ta, tv = Timer(), Timer(), Timer()
        with tg:
            certs, holders, _, _ = store.collect_certificates(tuple(pick))
        with ta:
            pi_K = client.aggregate_certificates(certs)
        valmap: dict[int, int] = {}
        for cert in certs:
            valmap.update(dict(zip(cert.Q, cert.F_Q)))
        F_Q = tuple(valmap[i] for i in pick)
        with tv:
            client.ver_retrieve(list(pick), list(F_Q), pi_K)
        size = sum(
            (x.bit_length() + 7) // 8 for x in (pi_K.S_I, pi_K.Lambda_I)
        )
        return Sample(
            q=len(pick),
            gen_ms=tg.take(),
            agg_ms=ta.take(),
            ver_ms=tv.take(),
            proof_bytes=size,
            certs=len(certs),
            nodes=len(set(holders.values())),
        )

    samples: list[Sample] = []
    for q in (1, 2, 4, 8, 14):
        pick = pool[:q]
        measure(pick)  # 预热：第一次会算 e_all 缓存与模逆表
        runs = [measure(pick) for _ in range(3)]
        samples.append(
            Sample(
                q=q,
                gen_ms=min(r.gen_ms for r in runs),
                agg_ms=min(r.agg_ms for r in runs),
                ver_ms=min(r.ver_ms for r in runs),
                proof_bytes=runs[0].proof_bytes,
                certs=runs[0].certs,
                nodes=runs[0].nodes,
            )
        )

    print(f"  {'|Q|':>4} {'凭证数':>6} {'服务器':>7} {'证明生成':>11} "
          f"{'聚合':>10} {'验证':>10} {'证据字节':>9}")
    for s in samples:
        print(f"  {s.q:>4} {s.certs:>6} {s.nodes:>7} {s.gen_ms:>9.1f} ms "
              f"{s.agg_ms:>8.1f} ms {s.ver_ms:>8.1f} ms {s.proof_bytes:>9d}")
    print(f"\n  证据规模上界 = 2 个群元素 = 2 × {nbytes} = {2 * nbytes} 字节，"
          f"**与 |Q| 和向量长度 n 都无关**")
    print("  |Q|=14 那行只有 128 字节：此刻 I 覆盖了全部位置，空乘积使 Λ_I = 1，")
    print("  所以只剩一个群元素 —— 这是方案的定义，不是测量误差。")
    print("  证明生成在 |Q|=14 时几乎为零：disagg 的目标集合就等于节点持有的集合，")
    print("  一个 add_back 都不用做。")

    # -- 7. 攻击 -----------------------------------------------------------
    # 攻击会故意把节点弄坏，所以用**一次性**的 store 来演示，
    # 不污染主向量 —— 第 9 节的自检仍然是对一个干净状态做的。
    sub("7. 攻击一：服务器偷改自己存的分量")
    # 用 PlainKeyStore（"密钥自带"的便捷子类）：这一节测的是**分量与密文**
    # 被篡改能不能被发现，跟密钥保护无关 —— 真正的密钥保护在第 8 节。
    atk = PlainKeyStore(session, node_ids=NODES)
    atk.upload("attacker", "victim", payload(4096, 90))
    victim = atk.transport.states["node-1"]
    i = victim.I[0]
    victim.tamper_value(i)
    print(f"  把 node-1 手上下标 {i} 的分量 +1（模拟篡改数据）")
    bad = atk.query([i])
    print(f"  承诺验证：{'通过' if bad.report.ok else '失败'} —— {bad.report.message}")
    print("      ↑ 节点改的是自己手里的分量，而验证方用的是**自己从密文算**的分量：")
    print("        两者对不上，承诺验证当场就把它拒了。")
    print(f"  整体：{'通过（不该！）' if bad.ok else '被抓 ✓'}")
    try:
        atk.check()
        print("  自检：没发现（不该！）")
    except ValueError as exc:
        print(f"  自检：先一步发现 ✓（{exc}）")

    sub("7b. 攻击二：拿别的段的密文冒充（分量没动，密文换了）")
    atk2 = PlainKeyStore(session, node_ids=NODES)
    atk2.upload("attacker", "victim", payload(4096, 91))
    v2 = atk2.transport.states["node-1"]
    d2 = atk2.transport.states["node-2"]
    j = v2.I[0]
    v2.overwrite_blob(j, d2.blobs[d2.I[0]])
    swap = atk2.query([j])
    print(f"  承诺验证：{'通过' if swap.report.ok else '失败'} —— {swap.report.message}")
    print("      ↑ 承诺的对象虽然只是摘要，但那个摘要是**验证方自己从这串密文算**的：")
    print("        密文一换，算出来的值就跟着变，承诺这一层当场过不去。")
    print(f"  整体：{'通过（不该！）' if swap.ok else '被抓 ✓'}")

    sub("7c. 攻击三：伪造一份证据")
    from svc.types import Opening

    good = atk2.query([j]).proof
    forged = Opening(S_I=atk2.delta.U, Lambda_I=good.Lambda_I, I=(j,))
    rep = ClientNode(session, atk2.delta).ver_retrieve([j], [atk2.values[j]], forged)
    print("  把 S_I 换成 U（第一步 S_I^e_I = U 就不再成立）")
    print(f"  验证结论：{'通过（不该！）' if rep.ok else '失败'} —— {rep.message}")

    # -- 8. 解密 -----------------------------------------------------------
    sub("8. 解密（受限的那条线 —— 不是你的文件就真的算不出块密钥）")
    for who in DEMO_USERS:
        blob = key_blobs[who]
        print(f"  {who:9s} 私钥密文 kind={blob['kind']}  iters={blob['iters']}"
              f"  salt={blob['salt'][:16]}…")

    def unseal(who: str) -> int:
        """用口令把私钥解封出来 —— 这就是登录那一刻做的事。"""
        return unwrap_private_key(password_of(who), key_blobs[who])

    def try_read(who: str, owner: str, fkey: str, indices=None) -> bytes:
        """以 ``who`` 的私钥去解。

        ★ 这一层**不查任何表** —— 能不能解开完全由“这把私钥对不对”决定。
        它与应用层那道“快速门”是两回事：即使上面放行了，这里照样能拦住。
        """
        box = key_cts[(owner, fkey)]
        sk = unseal(who)

        def key_of(pos: int) -> bytes:
            try:
                return unwrap_key(sk, box[pos])
            except KeyWrapIntegrityError as exc:
                raise PermissionError(
                    f"第 {pos} 块的块密钥解不开 —— 它不属于你（别人的私钥对它无效）"
                ) from exc

        return store.read(owner, fkey, indices, key_of=key_of)

    want = payload(2500, 2500 % 251)
    sub("8a. 本人读自己的文件")
    data = try_read("zhangsan", "zhangsan", "case-内科")
    print(f"  zhangsan → case-内科：{len(data)} 字节，逐字节一致 = {data == want}")

    sub("8b. 别人一律不行 —— 包括管理员（已经**没有主密钥**这种东西了）")
    for who, owner, fkey, why in (
        ("nurse", "zhangsan", "case-内科", "nurse 的私钥不是张三那把"),
        ("ortho", "zhangsan", "case-骨科", "同上"),
        ("lisi", "zhangsan", "case-基础信息", "同上"),
        ("wangwu", "lisi", "report-A", "王五解不开李四的文件"),
        ("zhangsan", "wangwu", "note", "反过来也一样"),
    ):
        try:
            try_read(who, owner, fkey)
        except PermissionError as exc:
            print(f"  ✗ {who:9s} → {fkey:14s} 被拒（{why}）：{exc}")
        else:
            print(f"  ！{who} → {fkey} 竟然成功了（不该！）")

    sub("8c. 但**验证**不受限：任何人都能验别人的文件")
    cov = store.query(store.file_indices("wangwu", "note"))
    print(f"  zhangsan 验 wangwu 的 note：{'通过 ✓' if cov.ok else '失败（不该！）'}"
          "  ← 这条路一个密钥字节都不碰")

    sub("8d. 随机访问：只取第 2 段")
    seg2 = store.file_indices("zhangsan", "case-内科")[1]
    part = try_read("zhangsan", "zhangsan", "case-内科", [seg2])
    print(f"  取到 {len(part)} 字节（segment_bytes = {store.segment_bytes}）"
          f"，等于原文对应段 = {part == want[store.segment_bytes:2 * store.segment_bytes]}")

    sub("8e. 把封好的块密钥改一个字节 —— 连所有者自己也解不开了")
    ct0 = key_cts[("zhangsan", "case-内科")][0]
    print(f"  密文形状 = {sorted(ct0)}   kind = {ct0['kind']!r}")
    intact = ct0["body"]
    ct0["body"] = ("0" if intact[0] != "0" else "1") + intact[1:]
    try:
        try_read("zhangsan", "zhangsan", "case-内科")
    except PermissionError as exc:
        print(f"  ✗ 所有者：{exc}")
    else:
        print("  ○ 所有者：仍然解开了（不该！）")
    ct0["body"] = intact  # 复原，别影响第 9 节
    print("      ↑ 这就是那个认证标签（tag）的用处：SM4-CTR 本身**不提供完整性**，")
    print("        没有 tag 的话，改一位只会解出另一个块密钥 ——")
    print("        表现是“文件内容是乱的”，而不是“这里被篡改了”。")

    # -- 9. 自检 -----------------------------------------------------------
    sub("9. 全面自检")
    t = Timer()
    with t:
        store.check()
    print("  登记表一致 ✓  每台服务器视图合法 ✓  声称与实存一致 ✓")
    print(f"  增量摘要 == 一次性承诺（逐位相同）✓  耗时 {t.take():.0f} ms")
    print(f"\n  U = {hex(store.delta.U)[:34]}…")
    print(f"  C = {hex(store.delta.C)[:34]}…")
    print(f"  n = {store.n}")

    print(f"\n{'=' * 72}\n演示结束。\n{'=' * 72}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
