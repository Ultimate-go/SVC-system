"""建库并灌入演示账号。

跑法::

    python scripts/seed.py                # 建库 + 建账号（库已存在则只补账号）
    python scripts/seed.py --reset        # 先删库重建（**会清掉所有文件**）
    python scripts/seed.py --demo         # 顺带传几个演示文件

.. important::

   ``--reset`` 只清**协调者**的库。各节点在 ``nodes/node-*/node.db`` 里还有自己的
   位置账，本脚本**不动**它们 —— 要整套重来（库与节点目录一起清）用
   ``python scripts/start_all.py --reset``。用 ``--reset`` 时若发现节点目录里
   还留着上一代的账，本脚本会明确告警（否则下一次启动会被启动闸拒掉）。

演示账号（口令统一写在 `DEMO_PASSWORD`）：

============  ==========
用户名          角色
============  ==========
``admin``     管理员
``zhangsan``  用户
``wangwu``    用户
============  ==========

★ 建账号时会**顺手给每个人生成一对 SM2 密钥**，并把私钥用他的登录口令
包起来存进 ``users.sk_wrapped``（见 ``manager.ensure_user_key``）。所以：

* 每个人上传文件时，块密钥是用**他自己的公钥**封的 —— 只有他能解密；
* **验证不受限**：任何人都能验证任何块（那条路径根本不碰密钥）。

``--demo`` 灌的都是一句话能说清的：``zhangsan`` 的病历本（4 份，
演示分块与"只有所有者能读"）+ ``wangwu`` 的私人笔记。

.. note::

   删掉 ABE 之后**没有"按属性分权"了**：一份文件要么所有者能解密、
   要么谁都解不开。以前那几个"内科护士只读内科那段"的演示文件随之取消 ——
   这不是"功能退化"，而是**换了访问控制模型**：现在是**公钥封装**，
   不是策略封装。想恢复共享，要另加一张"额外封装"表，而不是回到 ABE。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.config import Settings  # noqa: E402
from backend.db import Database  # noqa: E402
from backend.manager import StoreManager  # noqa: E402
from backend.models import UserRow  # noqa: E402
from backend.security import hash_password  # noqa: E402

DEMO_PASSWORD = "vds12345"

USERS: list[tuple[str, str, str]] = [
    ("admin", "admin", "管理员"),
    ("zhangsan", "user", "张三"),
    ("wangwu", "user", "王五"),
]

#: 演示文件：``(所有者, 文件名, 内容)``。
DEMO_FILES: list[tuple[str, str, bytes]] = [
    ("zhangsan", "病例本-基础信息", b"[basic] name=zhangsan sex=M age=41\n" * 40),
    ("zhangsan", "病例本-内科", b"[internal] dx=hypertension rx=amlodipine\n" * 60),
    ("zhangsan", "病例本-骨科", b"[ortho] fx=left-radius cast=6w\n" * 45),
    ("zhangsan", "病例本-多块", b"[part-A] " * 50 + b"[part-B] " * 45),
    ("wangwu", "会议纪要", b"[notes] 2026-09-25 review of VDS demo\n" * 30),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true", help="先删库重建（会清掉所有文件）")
    ap.add_argument("--demo", action="store_true", help="顺带传几个演示文件")
    ap.add_argument("--db", default=None, help="数据库路径（默认为项目根的 vds.db）")
    args = ap.parse_args()

    settings = Settings()
    if args.db:
        settings.db_path = Path(args.db)

    db = Database(settings.database_url())
    if args.reset:
        db.drop_all()
        print("[reset] 已删除全部表（协调者侧）")
        # ★ 只删协调者的表**不够**：各节点在 ``nodes/node-*/node.db`` 里也各有一份
        #   “位置账”（``state``：offset -> delta_n）。两代混在一起，下一次启动就会被
        #   启动闸拒掉 —— 它比的正是“每份文件的 offset -> n”
        #   （``StoreManager._verify_nodes_at_startup``）。★ 2026-10-16 实测踩到过：
        #   用 ``seed.py --reset`` 修不好，得整套重来。
        #
        #   这里**故意不去删**那些目录：本脚本可能正跑在“节点进程活着”的手动流程里
        #   （见 docs/复现指南.md），替用户删节点数据不是它的职责。只在真存在
        #   陈旧节点账目时把话说清楚，并给出唯一正确的补救命令。
        stale = sorted(
            d.name for d in (ROOT / "nodes").glob("node-*") if (d / "node.db").exists()
        )
        if stale and not args.db:
            print(
                f"[reset] ⚠ 节点目录 {', '.join(stale)} 里还有上一代的位置账（node.db）。\n"
                "        直接拿这套库去起后端，启动闸会因为“节点与协调者不同步”\n"
                "        而拒绝启动。整套重来（库与节点目录一起清）：\n"
                "            python scripts/start_all.py --reset\n"
                "        若确实只想重建协调者的库（例如跑调试），请同时停掉节点进程\n"
                "        并删掉 nodes/node-*，再拿这套库去启动。"
            )
    db.create_all()

    manager = StoreManager(settings, db)
    manager.bootstrap()
    print(f"[crs] |N| = {manager.session.crs.N.bit_length()} 位, "
          f"l = {manager.session.l}, n_max = {manager.session.n_max}")

    pwd_hash = hash_password(DEMO_PASSWORD)
    with db.session() as s:
        created, skipped = [], []
        for username, role, display in USERS:
            exists = s.query(UserRow).filter_by(username=username).one_or_none()
            if exists is not None:
                skipped.append(username)
                continue
            s.add(
                UserRow(
                    username=username,
                    pwd_hash=pwd_hash,
                    role=role,
                    display_name=display,
                )
            )
            created.append(username)
        s.commit()

    print(f"[users] 新建: {created or '（无）'}")
    print(f"[users] 已存在跳过: {skipped or '（无）'}")
    print(f"[users] 演示口令统一为: {DEMO_PASSWORD}")

    # ★ 给每个用户生成密钥对：**公钥入库，私钥用他的登录口令包好再入库**。
    #
    #   已经有的不会被动 —— 换一对新私钥会让他以前传的文件全部解不开，
    #   所以 ensure_user_key 对已有密钥对是**幂等**的。
    made = []
    for username, _role, _display in USERS:
        if manager.ensure_user_key(username, DEMO_PASSWORD):
            made.append(username)
    kw = manager.keywrap_status()
    print(f"[keys] 这次新生成了密钥对: {made or '（无，都已存在）'}")
    print("[keys] 方案: SM2-ECIES（封装块密钥）+ PBKDF2-HMAC-SM3（封装用户私钥）")
    print(f"[keys] 私钥存放: 口令派生密钥加密后入库（users.sk_wrapped，明文不落库）")
    print(f"[keys] 已有密钥对的账号: {kw['user_keys']}/{kw['users']}")

    if args.demo:
        for owner, file_key, payload in DEMO_FILES:
            try:
                row = manager.upload(owner, file_key, payload)
            except Exception as exc:  # noqa: BLE001 - 演示脚本，报错说清楚就行
                print(f"[demo] {owner}/{file_key} 跳过：{exc}")
                continue
            idx = manager._require().file_indices(owner, file_key)
            print(
                f"[demo] {owner}/{file_key}  {row.total_bytes} B → "
                f"{row.block_count} 块（全局位置 {idx[0]}-{idx[-1]}）"
            )
        print("[demo] 每块的块密钥都用**所有者的公钥**封装 —— 只有他能解密；验证不限")

    manager.check()
    st = manager.status()
    print(f"[check] 自检通过；全局位置总数 n = {st['delta']['n']}，"
          f"{st['files']} 个文件")
    print(f"[done] 数据库：{settings.db_path}")
    print("[next] 跑后端：python -m uvicorn backend.main:app")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
