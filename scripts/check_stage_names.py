"""自检：**界面上的步骤 key** 与**后端真实的阶段埋点名**是否仍然一一对得上。

为什么需要它
------------
界面上那些「过程弹框」（上传 / 验证 / 聚合 / 分解 …）靠 **步骤 key 与
后端 ``with stage("…")`` 的名字逐字相等** 来取耗时（见 ``core/timing.py``）。
对不上的后果是**静默的**：那一步不显示毫秒，看着像"这一步不需要时间"，
而不是像"名字写错了"。改一处埋点名却漏改前端，只有人肉点开弹框才发现。

所以这里把两边都抓出来对一遍，任何一边多出来都在跑的时候报出来。

三件事
------
1. 后端阶段名 → 前端步骤 key：**每个 key 都必须有对应的阶段**；
2. 反过来：后端有阶段但界面上没有对应步骤的，列出来（属于正常情况 ——
   像 ``分片规划``、``待推送密文打包`` 这类细分步不单独做成一步），只报不判错；
3. 扫一遍**已经废弃的旧名**是否还有残留（含注释与文档外的任何代码）。

退出码：0 = 没有对不上的 key；1 = 有。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: 后端埋点所在的包（定义处）。
BACKEND_DIRS = ["backend", "core", "vds", "node_service", "svc"]

#: 前端步骤定义所在文件（``key`` 要与后端阶段名对齐的地方）。
FRONTEND_FILES = [
    "frontend/src/views/files/list/index.vue",
    "frontend/src/views/evidence/query/index.vue",
    "frontend/src/views/evidence/aggregate/index.vue",
]

#: 曾经的埋点名。这些词**不该**再出现在可执行代码里（历史文档里有它们，不动）。
LEGACY = [
    "切块（split_segments）", "逐块加密（SM4）", "算向量分量（SM3）", "承诺（push_update）",
    "分片规划（_plan）", "打包待推密文", "分发到节点", "探测各节点（report）",
    "承诺验证（VerRetrieve）", "承诺验证（合并向量）", "合并位置集 P", "聚合凭证",
    "按主副本拆挑战", "聚合份额", "验证证明", "封装块密钥（ECIES）", "解封块密钥（ECIES）",
    "落库（上传文件登记）", "落库（新块与块登记）", "落库（新密文与块登记）",
    "落库（删除登记）", "落库（删除上传文件）", "格式（PBKDF2-HMAC-SM3）",
    "重新封装私钥（PBKDF2-HMAC-SM3）", "解封私钥（PBKDF2-HMAC-SM3）",
    "拆出子集证据（svc.disagg）", "验证拆出来的证据（VerRetrieve）",
    "合并两份证据（svc.agg）", "验证合并出来的证据（VerRetrieve）",
    "对整份文件做一次 commit",
]

problems: list[str] = []

#: 文档里的示例占位名（不是真埋点），不参与统计。
PLACEHOLDERS = {"名字", "…", "..."}

#: 本文件自己带着一份旧名清单（它就是用来抓旧名的），别自报家门。
SELF = Path(__file__).name


def code_files():
    """要扫的代码文件（不含文档、构建产物）。"""
    out = []
    for d in BACKEND_DIRS:
        out += list((ROOT / d).rglob("*.py"))
    out += list((ROOT / "frontend" / "src").rglob("*.js"))
    out += list((ROOT / "frontend" / "src").rglob("*.vue"))
    out += list((ROOT / "frontend" / "tests").rglob("*.js"))
    out += list((ROOT / "scripts").glob("*.py"))
    return sorted(p for p in out if p.is_file())


# ---------------------------------------------------------------------------
# 1. 后端阶段名
# ---------------------------------------------------------------------------
stage_re = re.compile(r'stage\(\s*"([^"]+)"')
stages: dict[str, list[str]] = {}
for p in code_files():
    if p.suffix != ".py":
        continue
    for m in stage_re.finditer(p.read_text(encoding="utf-8")):
        if m.group(1) in PLACEHOLDERS:
            continue
        stages.setdefault(m.group(1), []).append(p.relative_to(ROOT).as_posix())

print("=" * 72)
print(f"后端阶段名 {len(stages)} 个（去重后）")
print("=" * 72)
for name in sorted(stages):
    print(f"  {name}")
print()

# ---------------------------------------------------------------------------
# 2. 前端步骤 key
# ---------------------------------------------------------------------------
key_re = re.compile(r"^\s*key:\s*'([^']+)'", re.M)
keys: dict[str, str] = {}
for rel in FRONTEND_FILES:
    text = (ROOT / rel).read_text(encoding="utf-8")
    for m in key_re.finditer(text):
        keys[m.group(1)] = rel

print("=" * 72)
print(f"界面步骤 key {len(keys)} 个")
print("=" * 72)
for k in sorted(keys):
    ok = k in stages
    print(f"  {'OK  ' if ok else 'FAIL'}  {k}   ({keys[k]})")
    if not ok:
        problems.append(f"步骤 key 没有对应的后端阶段：{k}（{keys[k]}）")
print()

unused = sorted(set(stages) - set(keys))
print("后端有、界面上没做成独立步骤的阶段（正常，仅列出）：")
for n in unused:
    print(f"  ·  {n}")
print()

# ---------------------------------------------------------------------------
# 3. 旧名残留
# ---------------------------------------------------------------------------
print("=" * 72)
print("旧埋点名残留检查（代码里应为 0）")
print("=" * 72)
legacy_hits = 0
for p in code_files():
    if p.name == SELF:
        continue
    text = p.read_text(encoding="utf-8")
    for old in LEGACY:
        n = text.count(old)
        if n:
            legacy_hits += n
            rel = p.relative_to(ROOT).as_posix()
            print(f"  FAIL  {rel}: {old} ×{n}")
            problems.append(f"旧埋点名残留：{old}（{rel}）")
print(f"  合计 {legacy_hits} 处")
print()

print("=" * 72)
if problems:
    print(f"结果：{len(problems)} 项未通过")
    for x in problems:
        print("  - " + x)
    sys.exit(1)
print("结果：全部通过 —— 步骤 key 与埋点名一一对得上，旧名无残留")
print("=" * 72)
