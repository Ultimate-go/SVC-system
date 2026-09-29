"""块大小顾问 —— “这个文件该切多大一块”。

为什么单独一个模块、而且把数字写死在里面
----------------------------------------
它不属于算法层（不参与承诺/验证，改动它不会动摇任何安全性），
而是**应用层的一个取舍**：

* 块越大 ⇒ 块数越少 ⇒ 每块的固定开销摊得越薄 ⇒ **上传与验证都更快**，
  而且**占用的全局位置更少**（``n_max`` 是稀缺资源）；
* 块越大 ⇒ **改一块**的粒度越粗（改一块会换掉整个块的密文与块密钥）。

★ 结论**不是推理出来的，是实测出来的**。下表来自
``scripts/bench_blocksize.py``（本机，|N|=512 位、l=256、4 台节点、单进程；
量于 2026-09-26，当时每块还要做一次 **ABE 策略封装** —— 现在封装已换成
更便宜的 ECIES，所以下表是**上界**，沿用它只会把估算得偏保守）：

===========  ======  ==========  ==============
块大小(字节)    块数   上传(ms)     全量验证(ms)
===========  ======  ==========  ==============
256             128      2838.8          1477.2
512              64      1027.9           640.5
1024             32       486.9           279.5
2048             16       227.1           125.0
4096              8       106.1            54.5
8192              4        49.1            24.4
===========  ======  ==========  ==============

**趋势是单调的**（块越大越省），没有旧仓库那种 U 形曲线 ——
因为那条曲线属于“块小到每块固定开销压不住”的区间
（旧系统 16 字节一块）。本项目的块从 64 字节起、每块还要单独封装一次
块密钥，所以“块数”才是代价的主项。

所以这里的规则很简单、而且有据可依：**在允许范围内挑块数最少的那种切法**，
并把“为什么”连同估算一起说给用户听。想要更细的改块粒度就显式选另一档。
"""

from __future__ import annotations

import math
from typing import Sequence

__all__ = [
    "MEASURED_PER_BLOCK_MS",
    "MEASURED_SCOPE",
    "MEASURED_SOURCE",
    "recommend_plan",
]

#: 每块的固定代价（毫秒）—— **实测**，用来估算别的切法要花多久。
#:
#: ★★ **它只含“协调者本地那几步”，不含把密文推给各存储节点的那一段。**
#:
#: 量的时候跑的是**单进程**模式（``LocalTransport``，节点是同进程对象），
#: 所以“分发到节点”几乎不花时间。真实部署是**跨进程**的，那一步走 HTTP ——
#: 实测（4 台真节点、本机、2026-09-27）：一次 7 块的上传后端合计 2662 ms，
#: 其中 **2351 ms** 是它（88%）。
#: 也就是说这几个系数在跨进程部署下会把总耗时**少估两个数量级**。
#:
#: 所以它只适合用来**比较不同切法**（同样是本地的几步，相对关系仍然成立），
#: 不能当成“上传要多久”。界面必须把这句话和数字一起摆出来
#: （见 :data:`MEASURED_SCOPE`）。
#:
#: 来源：``scripts/bench_blocksize.py``（2026-09-26，本机，|N|=512 位、l=256、
#: 4 台节点、单进程；当时每块还含一次 ABE 策略封装 —— 现在换成 ECIES，
#: 更便宜，所以这些系数是**上界**）。
#: 取值口径：**4096 字节那一档的每块代价**（上传 106.1/8 = 13.26 ms、
#: 全量验证 54.5/8 = 6.81 ms）。挑这一档是因为它已经落到曲线的平坦段
#: （4096 档 13.26/6.81 与 8192 档 12.28/6.10 只差不到 8%），
#: 再往上加块只会让基准更小、估得更乐观，所以这里**宁可略偏保守**。
#: 不取 256 档那种小块的每块代价：那里每块固定开销还没摊开（22.2 ms/块），
#: 拿它当基准会把大块估慢一倍以上。
MEASURED_PER_BLOCK_MS: dict[str, float] = {"upload": 13.3, "verify": 6.8}

#: 上面那两个数从哪来的（界面上要标出来，不能把估算说成实测）。
MEASURED_SOURCE = (
    "scripts/bench_blocksize.py 实测（本机，2026-09-26；单进程模式，只含本地封装与承诺）"
)

#: 这批系数的**口径**。界面必须原样带出来 ——
#: 一份“只算了本地几步”的估算，被摆成“预计上传约 X ms”就是谎报。
MEASURED_SCOPE = (
    "只含协调者本地几步（切块 / SM3 / SM4 / ECIES 封装 / 承诺）；"
    "不含把密文推给各存储节点的分发（跨进程部署下分发才是大头）"
)

#: 候选档位：从最小块开始按 2 的幂往上翻，最后补上上限本身。
#: 用 2 的幂是让界面上那几个候选看起来是“一档一档”的，而不是一堆怪数字。
def _ladder(min_bytes: int, max_bytes: int) -> list[int]:
    out: list[int] = []
    seg = max(1, int(min_bytes))
    while seg < max_bytes:
        out.append(seg)
        seg *= 2
    out.append(int(max_bytes))
    # 去重并保序（min == max、或 2 的幂正好撞上上限时会重复）
    seen: set[int] = set()
    uniq: list[int] = []
    for x in out:
        if x not in seen:
            seen.add(x)
            uniq.append(x)
    return uniq


def _blocks(size: int, seg: int) -> int:
    """切成几块 —— 与 :func:`core.crypto.split_segments` 同一条口径（向上取整）。"""
    return int(math.ceil(size / seg)) if seg > 0 else 0


def recommend_plan(
    size: int,
    *,
    min_bytes: int,
    max_bytes: int,
    n_max: int,
    used: int = 0,
    prefer: str = "fewest_blocks",
    ladder: Sequence[int] | None = None,
) -> dict:
    """给一个文件大小，推荐一种切法。

    :param size: 文件字节数（**明文**）。
    :param min_bytes: 允许的最小块（部署配置 ``segment_bytes_min``）。
    :param max_bytes: 允许的最大块（部署配置 ``segment_bytes_max``）。
    :param n_max: 全局位置上限。
    :param used: 已经用掉多少位置（``/api/status`` 里的 ``blocks``）。
    :param prefer: ``"fewest_blocks"``（默认：块数最少 = 最快、最省位置）
        或 ``"finer_updates"``（改块粒度更细 = 块更小）。

    :returns: 一个 dict，**无论能不能推荐都会说清为什么**：

        * ``ok=True`` 时给 ``segment_bytes`` / ``blocks`` / ``why``；
        * ``ok=False`` 时给 ``reason``（例如“就算按最大块切也要 N 块，
          而只剩 M 个位置”）—— 这两种都带上 ``alternatives`` 明细。

    :raises ValueError: 参数本身不合法（块大小范围颠倒、``n_max`` 非正…）。
        那是**调用方的错**，不该悄悄给一个凑合的建议。
    """
    if size <= 0:
        raise ValueError(f"文件大小必须为正，收到 {size}")
    if min_bytes <= 0 or max_bytes <= 0:
        raise ValueError(f"块大小范围必须为正，收到 {min_bytes}..{max_bytes}")
    if min_bytes > max_bytes:
        raise ValueError(f"最小块 {min_bytes} 比最大块 {max_bytes} 还大，配置错了")
    if n_max <= 0:
        raise ValueError(f"n_max 必须为正，收到 {n_max}")
    if used < 0:
        raise ValueError(f"已用位置不能为负，收到 {used}")
    if prefer not in ("fewest_blocks", "finer_updates"):
        raise ValueError(f"未知的 prefer={prefer!r}（只认 fewest_blocks / finer_updates）")

    remaining = int(n_max) - int(used)
    cands = list(ladder) if ladder is not None else _ladder(min_bytes, max_bytes)

    rows: list[dict] = []
    for seg in cands:
        blocks = _blocks(size, seg)
        allowed = blocks <= remaining
        rows.append(
            {
                "segment_bytes": int(seg),
                "blocks": int(blocks),
                # ★ 一律标“估算”：这是按实测的**每块**代价推出来的，不是现量的
                "est_upload_ms": round(blocks * MEASURED_PER_BLOCK_MS["upload"], 1),
                "est_verify_ms": round(blocks * MEASURED_PER_BLOCK_MS["verify"], 1),
                "allowed": bool(allowed),
                "note": ""
                if allowed
                else f"要 {blocks} 块，但只剩 {remaining} 个位置",
            }
        )

    feasible = [r for r in rows if r["allowed"]]
    base = {
        "size": int(size),
        "remaining": remaining,
        "n_max": int(n_max),
        "used": int(used),
        "min_bytes": int(min_bytes),
        "max_bytes": int(max_bytes),
        "prefer": prefer,
        "alternatives": rows,
        "measured_per_block_ms": dict(MEASURED_PER_BLOCK_MS),
        "measured_source": MEASURED_SOURCE,
        "measured_scope": MEASURED_SCOPE,
    }
    if not feasible:
        biggest = max(cands)
        need = _blocks(size, biggest)
        return {
            **base,
            "ok": False,
            "segment_bytes": None,
            "blocks": None,
            "reason": (
                f"放不下：就算按最大块 {biggest} 字节切，也要 {need} 块，"
                f"而全局只剩 {remaining} 个位置（n_max = {n_max}，已用 {used}）。"
                f"办法有三条：删掉一些不再需要的文件腾位置、调大部署的 n_max"
                f"（那要重新 Bootstrap 公开参数）、或者把块上限调大。"
            ),
        }

    if prefer == "finer_updates":
        chosen = feasible[0]  # 允许范围内**最小**的块 ⇒ 改块粒度最细
        why = (
            f"粒度更细的一档：在允许范围内挑最小的可行块 "
            f"{chosen['segment_bytes']} 字节，切 {chosen['blocks']} 块。"
            f"代价是块数多、上传与验证更慢（本地那几步估算 {chosen['est_upload_ms']} ms / "
            f"{chosen['est_verify_ms']} ms），也更占全局位置。"
        )
    else:
        chosen = feasible[-1]  # 允许范围内**最大**的块 ⇒ 块数最少
        why = (
            f"块数最少的一档：块越大，每块的固定开销摊得越薄。"
            f"{chosen['segment_bytes']} 字节一块，共 {chosen['blocks']} 块，"
            f"本地那几步估算上传 {chosen['est_upload_ms']} ms、一次全量验证 "
            f"{chosen['est_verify_ms']} ms（每块约 "
            f"{MEASURED_PER_BLOCK_MS['upload']} ms / "
            f"{MEASURED_PER_BLOCK_MS['verify']} ms，实测）。"
            f"这几个数不含把密文推给各存储节点的分发，所以只适合比较切法；"
            f"上传到底要多久，看上传完成后那条阶段耗时条。"
            f"块大只影响改一块的粒度，不影响验证结果，证据大小也与块数无关。"
        )

    return {
        **base,
        "ok": True,
        "segment_bytes": int(chosen["segment_bytes"]),
        "blocks": int(chosen["blocks"]),
        "est_upload_ms": chosen["est_upload_ms"],
        "est_verify_ms": chosen["est_verify_ms"],
        "why": why,
    }
