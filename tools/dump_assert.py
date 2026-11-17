r"""dump 断言核对：跑 DGPU_TEST_DUMP 后核对产物（迁移方案 §五-3 验收项）。

用法：
  python tools/dump_assert.py [dump文件路径]
默认读 test_artifacts\gui_ctk_dump.txt；先跑：
  DGPU_TEST_OFFSCREEN=1 DGPU_TEST_DUMP=test_artifacts/gui_ctk_dump.txt python dgpu_gui.py
退出码 0=全部断言过。
"""

import os
import re
import sys

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT = os.path.join(PROJ, "test_artifacts", "gui_ctk_dump.txt")

EXPECTED_COLS = ["显卡", "PID", "进程", "显存(MB)", "共享(MB)", "提交(MB)",
                 "引擎活动", "NV 模块"]
EXPECTED_BUTTONS = ["独显直连", "混合模式", "核显省电"]


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError as e:
        print(f"FAIL  dump 文件读不了: {e}")
        return 1

    results = []

    def check(name, ok, note=""):
        results.append(ok)
        print(f"{'PASS' if ok else 'FAIL'}  {name}  {note}")

    # 1) 8 列列头
    head = lines[0].split("\t") if lines else []
    check("8 列列头", head == EXPECTED_COLS, str(head))

    # 2) 行数据 + tags
    row_lines = [l for l in lines[1:] if l.startswith("[")]
    tags_seen = set()
    for l in row_lines:
        m = re.match(r"\[([a-z,]*)\]", l)
        if m:
            tags_seen.update(t for t in m.group(1).split(",") if t)
    check("行数据存在", len(row_lines) >= 1, f"{len(row_lines)} 行")
    for t in ("new", "igpu", "nvdrv", "prot"):
        # prot 通常必现（System 等系统组件）；new/igpu 依赖运行态（igpu 默认
        # 不含核显行、new 依赖新进程出现）——可现即可，prot/nvdrv 场景性
        if t in ("prot", "nvdrv"):
            check(f"tag {t} 出现", t in tags_seen, sorted(tags_seen))
        else:
            print(f"INFO  tag {t}: {'现' if t in tags_seen else '未现（依赖运行态）'}")

    # 3) nv_rows
    nv = [l for l in lines if l.startswith("nv_rows:")]
    ok_nv = bool(nv) and len(nv[0]) > len("nv_rows: ") + 2
    check("nv_rows 存在", ok_nv, nv[0][:80] if nv else "")

    # 4) 明细条几何
    det = next((l for l in lines if l.startswith("detail:")), "")
    m = re.search(r"w=(\d+).*wraplength=(\d+).*tree_w=(\d+)", det)
    check("detail w>=600", bool(m) and int(m.group(1)) >= 600, det)
    check("wraplength 动态生效", bool(m) and int(m.group(2)) >= 200, det)

    # 5) gcu_info 五元组
    gi = next((l for l in lines if l.startswith("gcu_info:")), "")
    m = re.search(r"\((True|False|None), 'gen\d+', '[^']*',", gi)
    check("gcu_info 五元组", bool(m), gi[:90])

    # 6) 三按钮序 + 按态启停
    btns = next((l for l in lines if l.startswith("buttons:")), "")
    got = re.findall(r"(独显直连|混合模式|核显省电)=(normal|disabled)", btns)
    order_ok = [b[0] for b in got] == EXPECTED_BUTTONS
    check("三按钮序", order_ok, str([f"{b}={s}" for b, s in got]))
    states = dict(got)
    # 至少一个 disabled 才证明「按态启停」在工作（混合态下混合模式禁用）
    check("按态启停（存在 disabled）", "disabled" in states.values(), str(states))
    check("cget('state') API 正常", all(s in ("normal", "disabled") for s in states.values()))

    # 7) 徽章/三凭证文本
    badge = next((l for l in lines if l.startswith("badge:")), "")
    check("徽章文本非空", "badge: '" in badge and len(badge) > len("badge: ''"), badge[:60])
    ev = next((l for l in lines if "evidence:" in l), "")
    check("三凭证含内屏/PnP/DXGI", all(k in ev for k in ("内屏", "PnP", "DXGI")), ev[:80])

    # 8) 状态栏两行几何
    sb = next((l for l in lines if l.startswith("statusbar:")), "")
    m = re.search(r"badge_y=(\d+) ev_y=(\d+) ev_w=(\d+)", sb)
    check("两行布局 badge_y<ev_y", bool(m) and int(m.group(1)) < int(m.group(2)), sb)
    check("ev_w>=800", bool(m) and int(m.group(3)) >= 800, sb)

    n_ok = sum(results)
    print(f"--- {n_ok}/{len(results)} PASS ---")
    return 0 if n_ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
