"""★ 自动切块阶梯的验收 —— 小文件小块、大文件大块，且块数被压在目标以内。

为什么单独验它：这个函数决定**每一份新文件切多少块**，而块数同时决定

* 上传/验证的代价（实测 ~54 ms/块）；
* "改一块"到底动了多少数据（块越大，改一块越像重传整份文件）。

它没有一个"正确答案"，只有一组**必须成立的性质**：

1. **块数不失控** —— 任何大小都不超过 ``target_blocks``（除非撞上允许的
   最大块，那时如实取最大）；
2. **小文件用小块** —— 十几 KB 的文件必须落在 1 KB 档（"还不如原来 1 KB"
   那条反馈就是冲它来的）；
3. **单调** —— 文件更大时，块大小**不会变小**（否则会出现"文件越大反而
   切得越细"这种说不清的档位）；
4. **只在阶梯上取值** —— 返回的每一档都必须是配置里的某一档（或兜底值），
   不许算出个野生数。

★ 前端 `frontend/src/utils/split.js` 有一份**同口径**实现（只用于上传前的
  "本次切法"预览）。`frontend/tests/split.test.js` 钉的是**同一张表** ——
  两边任何一处的档位改了、另一边没跟上，都会有一侧变红。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.config import (  # noqa: E402
    SEGMENT_LADDER,
    SEGMENT_TARGET_BLOCKS,
    Settings,
    auto_segment_bytes,
)

FAILURES: list[str] = []
KB = 1024


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  PASS  " if cond else "  FAIL  ") + name
          + (("   :: " + extra) if extra else ""))
    if not cond:
        FAILURES.append(name)


def section(t: str) -> None:
    print()
    print("=" * 72)
    print(t)
    print("=" * 72)


st = Settings()
LO, HI, TARGET = st.segment_bytes_min, st.segment_bytes_max, st.segment_target_blocks


# ===========================================================================
section("1. 阶梯本身：形状与目标")

check("阶梯是升序的一串正整数",
      all(isinstance(x, int) and x > 0 for x in SEGMENT_LADDER)
      and list(SEGMENT_LADDER) == sorted(SEGMENT_LADDER),
      str(SEGMENT_LADDER))
check("阶梯最小档 = 1 KB（小文件要的就是它）", SEGMENT_LADDER[0] == 1024)
check("阶梯最大档 ≤ 允许的最大块",
      SEGMENT_LADDER[-1] <= st.segment_bytes_max,
      f"{SEGMENT_LADDER[-1]} vs {st.segment_bytes_max}")
check("目标块数是正整数且不夸张（≤ 256）", 0 < TARGET <= 256, str(TARGET))


# ===========================================================================
section("2. 逐档具体值：小文件小块、大文件大块")

CASES = [
    # (文件大小 KB, 期望每块字节, 一句话)
    (1, 1024, "1 KB 的文件：1 KB 一块"),
    (8, 1024, "8 KB：1 KB 一块"),
    (13, 1024, "★ 十几 KB：1 KB 一块（反馈里点名的那档）"),
    (64, 1024, "64 KB：1 KB 一块"),
    (128, 1024, "128 KB：刚好 128 块，仍是 1 KB"),
    (129, 4096, "过了 128 块就上一档"),
    (512, 4096, "512 KB：4 KB 一块（128 块）"),
    (513, 16384, "513 KB 上一档"),
    (1024, 16384, "1 MB：16 KB 一块（64 块 ≈ 3.5 秒）"),
    (2048, 16384, "2 MB：16 KB 一块（128 块）"),
    (2100, 65536, "2.1 MB 上一档"),
    (8 * 1024, 65536, "8 MB：64 KB 一块（128 块 ≈ 7 秒）"),
    (8193, 262144, "刚过 8 MB 上一档"),
    (32 * 1024, 262144, "32 MB：256 KB 一块（128 块）"),
]
for kb, want, why in CASES:
    got = auto_segment_bytes(kb * KB, lo=LO, hi=HI)
    check(f"{kb:>5} KB → {want:>7} 字节   （{why}）", got == want, f"实得 {got}")


# ===========================================================================
section("3. 性质：块数不失控 / 单调 / 只在阶梯上取值")

bad_count: list[str] = []
wild: list[str] = []
prev_seg = 0
prev_kb = 0
mono_bad: list[str] = []
for kb in list(range(1, 300)) + list(range(300, 4096, 7)) + [8192, 16384, 32768, 65536]:
    n = kb * KB
    seg = auto_segment_bytes(n, lo=LO, hi=HI)
    blocks = -(-n // seg)
    if blocks > TARGET and seg != HI:
        bad_count.append(f"{kb}KB→{blocks}块")
    if seg not in SEGMENT_LADDER and seg != HI:
        wild.append(f"{kb}KB→{seg}")
    if seg < prev_seg:
        mono_bad.append(f"{prev_kb}KB:{prev_seg} → {kb}KB:{seg}")
    prev_seg, prev_kb = seg, kb

check(f"扫 {len(range(1, 300)) + len(range(300, 4096, 7)) + 4} 个大小：块数都不超过 {TARGET}"
      "（除兜底那档）", not bad_count, ", ".join(bad_count[:4]))
check("返回值只在阶梯上（或允许的最大块）", not wild, ", ".join(wild[:4]))
check("单调：文件更大时块大小不会变小", not mono_bad, ", ".join(mono_bad[:4]))


# ===========================================================================
section("4. 边界与兜底")

check("空文件（0 字节）不炸", auto_segment_bytes(0, lo=LO, hi=HI) == 1024,
      str(auto_segment_bytes(0, lo=LO, hi=HI)))
check("1 字节的文件：1 KB 一块（一块装得下就行，块大小是上限）",
      auto_segment_bytes(1, lo=LO, hi=HI) == 1024)
check("★ 超大文件（64 MB）兜底到允许的最大块，块数仍然收敛",
      auto_segment_bytes(64 * 1024 * KB, lo=LO, hi=HI) == HI,
      f"{auto_segment_bytes(64 * 1024 * KB, lo=LO, hi=HI)}")
check("★ 兜底之后依然块数可控（≤ 128）",
      -(-64 * 1024 * KB // HI) <= TARGET,
      f"{-(-64 * 1024 * KB // HI)} 块")
check("允许范围小时不吃亏：lo 抬高到 4 KB 时最小档就是 4 KB",
      auto_segment_bytes(13 * KB, lo=4096, hi=HI) == 4096,
      str(auto_segment_bytes(13 * KB, lo=4096, hi=HI)))
check("阶梯为空时兜底到 max（不抛异常、不死循环）",
      auto_segment_bytes(5 * KB, ladder=(), lo=LO, hi=HI) == HI)


# ===========================================================================
section("5. 与前端预览表口径一致（这张表两边都要对得上）")

#: ★ 与 `frontend/tests/split.test.js` 里那张表**逐行相同**。
PIN = [(1, 1024), (13, 1024), (128, 1024), (129, 4096), (512, 4096),
       (1024, 16384), (2048, 16384), (8 * 1024, 65536), (32 * 1024, 262144)]
for kb, want in PIN:
    got = auto_segment_bytes(kb * KB, lo=LO, hi=HI)
    check(f"钉死 {kb} KB → {want}", got == want, f"实得 {got}")


print()
print("=" * 72)
if FAILURES:
    print(f"结果：{len(FAILURES)} 条未通过 ✗")
    for f in FAILURES:
        print("   - " + f)
else:
    print("结果：ALL PASS —— 小文件 1 KB 起、大文件逐档上升，块数始终受控")
print("=" * 72)
sys.exit(1 if FAILURES else 0)
