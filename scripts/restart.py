r"""重启器 —— 管理员界面上那个「立刻重启」按钮背后真正干活的东西。

为什么要**单独一个脚本**，而不是在后端进程里就地做完
----------------------------------------------------
因为停机与重启这两件事**不能由同一个进程干**：

后端自己也是要被停掉的那几样之一（节点 / 后端 / 前端），而
``taskkill /F`` 打完自己那一发之后，后面"把它们重新拉起来"就没机会跑了。
所以分工是这样的：

* 后端（``POST /api/admin/deploy/restart``）只负责**拉起本脚本**
  （分离进程，独立控制台），然后把 HTTP 响应原样返回；
* 真正的停机、按新台数启动，全在这里做。

★★ 它自己是后端进程的**子进程**，这一条有个直接后果：停后端时
**不能用** ``taskkill /T``（"连同子进程"）—— 那会把本脚本自己也杀掉，
于是表现成"点了立刻重启，然后什么都没发生"。
所以 :func:`~scripts.start_all.stop_everything` 对后端那一发传
``backend_tree=False``。节点与前端照旧用 ``/T``，它们底下确实有孩子要收，
而且它们都不是本脚本的祖先。

跑法::

    python scripts/restart.py                # 按部署配置的台数重启
    python scripts/restart.py --nodes 6      # 强制按 6 台重启（并写回配置）
    python scripts/restart.py --no-browser   # 不自动开浏览器

★★ 它还负责「让位」这件事：动手停之前先立一个**交接标记**
（``logs/restart.pending``，见 :data:`~scripts.start_all.RESTART_MARKER`）。
旧启动器（上一次 ``一键启动.cmd`` 那个窗口里的 ``start_all.py``）盯着自己的
子进程，后端一死它就会**按端口扫一遍**收尾 —— 而本脚本马上要在同一个区间
（9101..9164）起新节点，会被它一并收走，表现就是「重启后新的起来了又没了」。
所以：立标记 → 旧启动器看到就主动让位（退出码 0，它那个窗口自己关掉）→
本脚本**确认它真退了**（等不到就强制收掉，不带 ``/T``）→ 才起新的一套。

它**不碰数据**：不清库、不清节点目录、不改账号。重启前后的区别只有
"按几台跑"和"进程换了一批"，块、承诺、证据、文件全部原样 ——
这也正是"改台数不需要重传文件"那句话的由来。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PY = sys.executable

#: 从"被拉起来"到"开始停东西"之间等多久。
#:
#: ★ 不是随便等的：后端的 ``POST /restart`` 要先把 HTTP 响应写回去，
#:   浏览器才不会看到一个被掐断的连接（那种失败看起来像"按钮点坏了"）。
#:   2 秒足够一个本地响应走完，也不会让人等得难受。
HANDOFF_WAIT = 2.0

#: 停完之后、起之前等多久。
#:
#: ★ 必须留这一段：Windows 释放监听端口不是同步的，紧接着启动会碰到
#:   "端口被占用"（而那个报错看起来像代码 bug，不像"没停干净"）。
#:   与 ``start_all.ensure_nodes`` 里那句 ``time.sleep(1.0)`` 同理。
PORT_RELEASE_WAIT = 1.5

#: 等旧启动器（``start_all.py``）退场最多多久。
#:
#: ★★ 为什么非要等（这是"重启没成功"的真正原因之一）：
#:   旧启动器盯着自己的子进程，后端一死它就会去**按端口扫**一套收尾
#:   （9101..9164）。而本脚本马上要在同一个区间起新节点 ——
#:   那一下正好把它们收走。交接标记（见
#:   :data:`~scripts.start_all.RESTART_MARKER`）能让它主动让位，
#:   但它要等下一次轮询（0.5 s）才看得到，所以这里必须确认真退完了。
#:
#: ★ 上限给 **20 秒** 是有实测依据的：启动器从"端口就绪"到**真正进入监控循环**
#:   还要约 10 秒（它还在把 banner 那一屏打完）—— 实测在它刚起来就杀后端，
#:   它 11.5 秒才反应。正常场景（用户隔着几分钟点的按钮）它是秒级反应，
#:   这个上限只是为"刚起完就重启"那种边角情形留余量。
LAUNCHER_EXIT_WAIT = 20.0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="重启 VDS 演示环境（按部署配置的台数）")
    ap.add_argument(
        "--nodes",
        type=int,
        default=None,
        help="强制按几台重启。不传就用部署配置里的台数（管理员界面改的那个）。",
    )
    ap.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    ap.add_argument(
        "--wait",
        type=float,
        default=HANDOFF_WAIT,
        help=f"动手之前先等几秒，让后端的响应回去（默认 {HANDOFF_WAIT}）",
    )
    args = ap.parse_args(argv)

    # 复用启动器里那套：它已经处理了控制台编码、端口扫描、终止进程树。
    import scripts.start_all as starter

    starter._force_utf8_console()
    plain_say = starter.say

    # ★★ 本脚本跑在**另一个控制台**里（后端用 ``CREATE_NEW_CONSOLE`` 拉起来的）。
    #   那个窗口一旦一闪而过（或者被关掉），就没任何东西可看了 ——
    #   而“重启没成功”恰恰是最需要线索的时候。所以同时写一份
    #   ``logs/restart.log``（每次重启重写一份，不带历史）。
    #   （实测踩到过：重启器自己在停机那一刻被误杀，而屏幕上什么也没留下，
    #    只能靠猜 —— 这一行就是为那种时候写的。）
    try:
        starter.LOG_DIR.mkdir(parents=True, exist_ok=True)
        restart_log = (starter.LOG_DIR / "restart.log").open(
            "w", encoding="utf-8", errors="replace"
        )
    except OSError:  # pragma: no cover - 日志目录建不出来时不该把重启也拖死
        restart_log = None

    def say(msg: str = "") -> None:
        plain_say(msg)
        if restart_log is not None:
            try:
                restart_log.write(str(msg) + "\n")
                restart_log.flush()
            except (OSError, ValueError):  # pragma: no cover
                pass

    say("=" * 62)
    say("  VDS 重启（管理员界面 → 「立刻重启」）")
    say("=" * 62)

    # ★★ 先立「交接标记」，而且必须在停任何东西**之前**（见
    #   ``starter.RESTART_MARKER``）：后端的进程一死，旧启动器就会去扫
    #   9101..9164 收尾 —— 而我们要在那个区间上起新节点，会被它一并收走。
    #   它看到这个标记就会主动让位（退出码 0，它那个窗口自己关掉）。
    marker = starter.RESTART_MARKER
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(f"restart by pid {os.getpid()}\n", encoding="utf-8")
    say(f"  已立交接标记 {marker.relative_to(ROOT)}（旧启动器看到它会主动让位）")

    if args.wait > 0:
        say(f"\n  {args.wait:.1f} 秒后开始停机（先把这次的响应送回去）…")
        time.sleep(args.wait)

    # ★★ 顺序：先停**别人**，最后停后端 —— 而且后端那一发不带 ``/T``。
    #   本脚本挂在后端下面（见文件头），带 ``/T`` 就是自杀。
    #   ★ 而"哪几个端口上是我的祖先"不能靠端口号猜：改端口时配置里写的已经是
    #     **新**端口，而正在跑的后端还在**旧**端口上。所以把"我自己这棵树可能在
    #     听的端口"显式交给它（见 ``start_all.my_tree_ports``）—— 那几个一律不 /T。
    #     （这条不做的话，「改端口 → 立刻重启」会在停在旧后端那一刻把自己杀掉：
    #      旧的那套停了，新的那套再也起不来。）
    say("\n[1/3] 停掉现有的节点 / 后端 / 前端")
    starter.stop_everything(
        quiet=False, backend_tree=False, self_ports=starter.my_tree_ports()
    )

    # 托管进程（``run_nodes.py``）不在任何端口上听，所以上面那一遍扫不到它；
    # 留着它会和新起的那个抢 9101.. —— 按命令行收掉（带 ``/T``：它底下就是那几台节点）。
    sup = starter.kill_by_cmdline("run_nodes.py", tree=True)
    if sup:
        say(f"  收掉旧的节点托管进程（PID {', '.join(map(str, sup))}）")

    # ★ 等端口真的放掉。
    # ★★ 这里用 **socket 探活**（``alive``，微秒级）而不是 ``listening_map()``：
    #   后者要起一个 PowerShell（本机实测 2.0 秒），放进轮询里就是"卡死"。
    #   同一个坑在 start_all.listening_map 的注释里写过（那是"一次问完全部"
    #   的修法），而轮询场景正确的做法是根本不问进程表。
    # ★ 端口清单交给 ``starter.stop_port_list()``：它把配置里那一套、
    #   ``stop_ports``（历史上用过的全部端口）、以及默认那一段都算进去了 ——
    #   端口可以每台节点都不一样，所以这里**没有"一段区间"可以扫**。
    ports = starter.stop_port_list()
    deadline = time.time() + PORT_RELEASE_WAIT * 10
    busy = [p for p in ports if starter.alive(p, timeout=0.2)]
    while busy and time.time() < deadline:
        time.sleep(PORT_RELEASE_WAIT)
        busy = [p for p in ports if starter.alive(p, timeout=0.2)]
    if busy:
        say(f"  ⚠ 这些端口还占着（{busy}）—— 紧接着的启动可能会失败，")
        say("    真卡住就先跑一次 停止.cmd，再手动双击 一键启动.cmd。")
    else:
        say("  端口都放掉了")

    # ★★ 再等旧启动器**真的退场**，然后才允许起新的。
    #   它就算看到了交接标记，也要等下一次轮询（0.5 s）才会退；
    #   而它只要还在，收尾那一步就会按端口扫一遍 —— 正好把新节点收走。
    #
    #   ★★ 轮询必须用 ``pid_alive``（微秒级，一次 OpenProcess）：
    #   写成 ``find_by_cmdline`` 的话，每轮都要起一个 PowerShell（1.2 秒），
    #   而循环每 0.3 秒转一圈 —— 实测白等了 **76 秒**，
    #   表现就是窗口停在 ``[1/3]`` 不动。所以：**进程表只查一次拿 PID**，
    #   之后一直问"这个 PID 还在不在"。
    #   等不到就强制收掉（**不带 ``/T``**：本脚本挂在后端下面，
    #   而后端是启动器的子进程，带 ``/T`` 会连自己一起杀）。
    olds = starter.find_by_cmdline("start_all.py", exclude={os.getpid()})
    deadline = time.time() + LAUNCHER_EXIT_WAIT
    while olds and time.time() < deadline:
        olds = [p for p in olds if starter.pid_alive(p)]
        if olds:
            time.sleep(0.3)
    if olds:
        say(f"  旧启动器没自己退（PID {', '.join(map(str, olds))}）—— 强制收掉（不带 /T）")
        for pid in olds:
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
        time.sleep(PORT_RELEASE_WAIT)
    else:
        say("  没有还在跑的旧启动器（或者它已经让位了）")

    # 交接完毕：把标记撤掉。新的一套起来之后就不该再有“正在重启”这回事了。
    marker.unlink(missing_ok=True)

    say("\n[2/3] 按部署配置的台数重新启动")
    cmd = [PY, str(ROOT / "scripts" / "start_all.py")]
    if args.nodes is not None:
        cmd += ["--nodes", str(args.nodes)]
    if args.no_browser:
        cmd.append("--no-browser")

    # ★ 把后端注入给本进程的那三个变量**清掉**再交给启动器：
    #   ``VDS_NODE_URLS`` / ``VDS_NODE_IDS`` 带着的是**上一次**的节点名单，
    #   而启动器自己会按新台数重新设一遍。不清的话，
    #   "改了台数却还是按老台数起"这种故障会以"配置没生效"的样子出现 ——
    #   而配置其实生效了，只是被环境变量盖住了。
    env = os.environ.copy()
    # ★ 端口那几个变量一并清掉：它们描述的是**上一个**进程（后端注入给本脚本的
    #   ``VDS_BACKEND_PORT`` / ``VDS_FRONTEND_PORT``），留着会被启动器当真。
    #   启动器自己会按新配置重新设一遍。
    for var in (
        "VDS_NODE_URLS",
        "VDS_NODE_IDS",
        "VDS_NODE_TOKEN",
        "VDS_BACKEND_PORT",
        "VDS_FRONTEND_PORT",
    ):
        env.pop(var, None)
    say(f"  {' '.join(cmd[1:])}")

    say("\n[3/3] 交棒给一键启动（这个窗口从此就是它的控制台）")
    say("     要停就按 Ctrl+C，或者跑一次 停止.cmd。\n")
    # 前台跑（阻塞）：本脚本活着的这段时间就是这个演示环境的控制台。
    # 用 run 而不是 Popen + 退出，是因为"启动器需要一个活着的终端"——
    # 它要盯着子进程、掉了要打印日志尾巴。
    return subprocess.run(cmd, cwd=str(ROOT), env=env).returncode


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
