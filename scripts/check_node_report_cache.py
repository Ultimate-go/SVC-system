"""钉住两件「看着像上传变慢」的事：

1. ``node_report()`` 的**短 TTL 缓存**：同一屏内重复问不重复算，
   但**任何写操作之后必须当场失效**（否则块分布图会显示旧数据）。
2. ``GET /api/status?probe=0`` 与 ``GET /api/nodes?verify=0`` 这两个
   *轻量档* 仍然存在 —— 上传文件页 / 聚合页 / 可验证查询页靠它们
   绕开「每台 × 每份文件一遍 svc.verify」。

用法::

    python scripts/check_node_report_cache.py

背景：``node_report(verify=True)`` 会让每台节点对每份文件跑一遍
``check_local_view``（§|I| 级模幂），代价随文件数增长（实测 7 份文件
1.5~3 s）。它同时被 ``/api/status`` 与 ``/api/nodes`` 调用，而「上传文件」页
一次 ``load()`` 会**两个都打**，于是历史上表现成"上传完要等 3 秒"。

这个脚本不连数据库、不起服务：直接用 ``VectorStore.__new__`` 造一个只有
缓存相关字段的壳，配一个会计数的假传输层来做断言。
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.registry import SegmentRegistry  # noqa: E402
from core.store import NODE_REPORT_TTL_S, VectorStore  # noqa: E402


class CountingTransport:
    """假的传输层：只数 ``report()`` 被调了几次。"""

    node_ids = ("node-1",)

    def __init__(self) -> None:
        self.calls = 0

    def report(self, *, verify: bool = False) -> list[dict]:
        self.calls += 1
        return [
            {
                "node_id": "node-1",
                "vectors": [],
                "offsets": [],
                "held": 0,
                "valid": True,
                "proved": True if verify else None,
                "fresh": False,
            }
        ]


def bare_store(transport: CountingTransport) -> VectorStore:
    """只装 ``node_report`` 需要的那几个字段（跳过 CRS / 会话初始化）。"""
    store = VectorStore.__new__(VectorStore)
    store.transport = transport  # type: ignore[assignment]
    store.deltas = {}
    store.registry = SegmentRegistry()
    store._report_cache = {}
    return store


def check_ttl(bad: list[str]) -> None:
    t = CountingTransport()
    store = bare_store(t)
    first = store.node_report(verify=True)
    second = store.node_report(verify=True)
    if t.calls != 1:
        bad.append(f"TTL 缓存没生效：TTL={NODE_REPORT_TTL_S}s 内连问两次，传输层被调了 {t.calls} 次（应 1 次）")
    if first != second:
        bad.append("命中缓存时返回的内容与第一次不一致")

    # verify=False 是另一档，必须单独算（proved 字段不同，不能互相顶替）
    store.node_report(verify=False)
    if t.calls != 2:
        bad.append(f"verify 两档没有分开缓存：换 verify=False 后传输层调用 {t.calls} 次（应 2 次）")


def check_deepcopy(bad: list[str]) -> None:
    t = CountingTransport()
    store = bare_store(t)
    rows = store.node_report(verify=True)
    rows[0]["indices"] = [999]  # 调用方写脏返回值
    rows[0]["node_id"] = "被改过"
    again = store.node_report(verify=True)
    if again[0].get("node_id") == "被改过" or again[0].get("indices") == [999]:
        bad.append("缓存里的行被调用方改脏了（命中时没有深拷贝）")


def check_invalidate(bad: list[str]) -> None:
    t = CountingTransport()
    store = bare_store(t)
    store.node_report(verify=True)
    store.invalidate_node_report()
    store.node_report(verify=True)
    if t.calls != 2:
        bad.append(f"invalidate_node_report() 之后没有重算：传输层只被调了 {t.calls} 次（应 2 次）")


#: 写操作点 -> 该处必须调 ``invalidate_node_report()``。
WRITE_SITES = (
    ("apply_update(", "上传 / 替换推送"),
    ("apply_append(", "追加推送"),
    ("self.transport.drop(", "删除（先丢下标）"),
    ("apply_delete(", "删除推送"),
)


def check_write_sites(bad: list[str]) -> None:
    text = (ROOT / "core" / "store.py").read_text(encoding="utf-8")
    for needle, what in WRITE_SITES:
        idx = text.find(needle)
        if idx < 0:
            bad.append(f"core/store.py 里找不到写操作点 {needle}（{what}）—— 缓存失效清单该更新了")
            continue
        window = text[max(0, idx - 400) : idx]
        if "invalidate_node_report()" not in window:
            bad.append(f"{what}（{needle}）之前没有调 invalidate_node_report() —— 写完会看到旧现状")
    if "self.invalidate_node_report()" not in text.split("def set_crs")[1][:400]:
        bad.append("set_crs() 换了公开参数之后没让现状缓存失效")

    mgr = (ROOT / "backend" / "manager.py").read_text(encoding="utf-8")
    drill = mgr.split("def fault_drill")[1][:4000]
    if "invalidate_node_report()" not in drill:
        bad.append("fault_drill() 改了节点可达性却没让现状缓存失效 —— 演练出来的掉线会晚 1.5 s 才看得见")
    install = mgr.split("def _install_fault_drill")[1][:800]
    if "invalidate_node_report()" not in install:
        bad.append("_install_fault_drill() 换了传输层却没让现状缓存失效")


def check_routes(bad: list[str]) -> None:
    """``/api/status`` 有 probe、``/api/nodes`` 有 verify —— 轻量档的入口。"""
    path = ROOT / "backend" / "routers" / "system.py"
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    routes: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        args = {a.arg for a in node.args.args} | {a.arg for a in node.args.kwonlyargs}
        routes[node.name] = args
    if "probe" not in routes.get("status", set()):
        bad.append("/api/status 没有 probe 参数 —— 上传文件页又会退回到逐台探活")
    if "verify" not in routes.get("nodes", set()):
        bad.append("/api/nodes 没有 verify 参数 —— 上传文件页又会退回到每台跑一遍 svc.verify")
    if "mgr.status(probe=probe)" not in src:
        bad.append("/api/status 没把 probe 传给 mgr.status()")
    if "mgr.node_report(verify=verify)" not in src:
        bad.append("/api/nodes 没把 verify 传给 mgr.node_report()")


def check_frontend(bad: list[str]) -> None:
    """三个只要部署参数 / δ 的页面，必须走轻量档。"""
    want = {
        "frontend/src/views/files/list/index.vue": (
            ("systemApi.status({ probe: false })", "/api/status 仍逐台探活"),
            ("devicesApi.nodes({ verify: false })", "/api/nodes 仍做密码学自检"),
        ),
        "frontend/src/views/evidence/aggregate/index.vue": (
            ("systemApi.status({ probe: false })", "/api/status 仍逐台探活"),
        ),
        "frontend/src/views/evidence/query/index.vue": (
            ("systemApi.status({ probe: false })", "/api/status 仍逐台探活"),
        ),
    }
    for rel, checks in want.items():
        text = (ROOT / rel).read_text(encoding="utf-8")
        for needle, why in checks:
            if needle not in text:
                bad.append(f"{rel}：找不到 {needle} —— {why}")

    # 反过来的约束：设备页要看「视图校验通过」（proved），不能给它降级。
    dev = (ROOT / "frontend/src/views/devices/list/index.vue").read_text(encoding="utf-8")
    if re.search(r"devicesApi\.nodes\(\s*\{\s*verify:\s*false", dev):
        bad.append("设备页把 verify 降成了 false —— 那一列「视图校验」会永远显示「未验证」")


def main() -> int:
    bad: list[str] = []
    check_ttl(bad)
    check_deepcopy(bad)
    check_invalidate(bad)
    check_write_sites(bad)
    check_routes(bad)
    check_frontend(bad)

    if bad:
        print("✗ node_report 缓存 / 轻量档有问题：")
        for line in bad:
            print(f"    {line}")
        print(f"\n共 {len(bad)} 处。")
        return 1

    print(f"OK  node_report 短 TTL 缓存：{NODE_REPORT_TTL_S}s，写操作后当场失效")
    print("OK  /api/status?probe=0 与 /api/nodes?verify=0 两个轻量档都在用")
    print("OK  设备页仍走完整自检（proved）")
    print("\n结果：全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
