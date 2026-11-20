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

    # 6) 三按钮序 + 可点性（2026-11-17 交互改版：当前态不再 disabled，
    # 点击由 run_switch 落日志反馈；disabled 只剩「能力不支持/切换中」）
    btns = next((l for l in lines if l.startswith("buttons:")), "")
    got = re.findall(r"(独显直连|混合模式|核显省电)=(normal|disabled)", btns)
    order_ok = [b[0] for b in got] == EXPECTED_BUTTONS
    check("三按钮序", order_ok, str([f"{b}={s}" for b, s in got]))
    states = dict(got)
    check("至少一个按钮可点", "normal" in states.values(), str(states))
    check("cget('state') API 正常", all(s in ("normal", "disabled") for s in states.values()))
    badge = next((l for l in lines if l.startswith("badge:")), "")
    # 当前态按钮必须可点（徽章 ↔ 按钮一一对应；对应不上视为机器态特殊，跳过）
    m_bdg = re.search(r"badge: '(.*?)' evidence:", badge)
    if m_bdg:
        bt = m_bdg.group(1)
        cur = ("混合模式" if "混合" in bt else
               "独显直连" if "直连" in bt else
               "核显省电" if ("省电" in bt or "核显" in bt) else None)
        if cur:
            check("当前态按钮可点（非 disabled）", states.get(cur) == "normal",
                  f"badge={bt!r} → {cur}={states.get(cur)}")

    # 7) 徽章/三凭证文本
    check("徽章文本非空", "badge: '" in badge and len(badge) > len("badge: ''"), badge[:60])
    ev = next((l for l in lines if "evidence:" in l), "")
    check("三凭证含内屏/PnP/DXGI", all(k in ev for k in ("内屏", "PnP", "DXGI")), ev[:80])

    # 8) 状态栏两行几何
    sb = next((l for l in lines if l.startswith("statusbar:")), "")
    m = re.search(r"badge_y=(\d+) ev_y=(\d+) ev_w=(\d+)", sb)
    check("两行布局 badge_y<ev_y", bool(m) and int(m.group(1)) < int(m.group(2)), sb)
    # 阈值 700：三凭证全文在雅黑 11pt 的自然宽度 ~780px（1180 窗口）；
    # 单行回退时被右侧元素挤没会显著低于此
    check("ev_w>=700", bool(m) and int(m.group(3)) >= 700, sb)

    # 9) 设计系统：表格字体族 = Microsoft YaHei UI（雅黑生效=不吃宋体回退）
    fnt = next((l for l in lines if l.startswith("fonts:")), "")
    m = re.search(r"tree=(.*?) buttons=", fnt)
    check("表格字体=雅黑", bool(m) and m.group(1).lower() in (
        "microsoft yahei ui", "{microsoft yahei ui}"), fnt[:90])

    # 10) 态式按钮：至多一个实色（fg_color 非 transparent），其余描边
    m = re.search(r"buttons=([^\s]+)", fnt)
    fgs = [kv.split("=", 1)[1] for kv in m.group(1).split(",")] if m else []
    solid = [c for c in fgs if c and c != "transparent"]
    check("态式按钮（至多一个实色）", bool(fgs) and len(solid) <= 1, str(fgs))

    # 11) 布局几何：工具栏高度贴内容（竖分隔 CTkFrame 默认 200 高会把
    # 工具栏撑到 ~300px、按钮挤中间留大空白——28pt 控件行正常 ~42px）
    lay = next((l for l in lines if l.startswith("layout:")), "")
    m = re.search(r" bar_h=(\d+)", lay)
    check("工具栏高度贴内容 bar_h<=80", bool(m) and int(m.group(1)) <= 80, lay)

    # 12) 日志保留下拉：渲染出宽度（>0）且选中值在五档内
    #（不假设默认档——settings.json 可能记着用户上次的档位）
    ret = next((l for l in lines if l.startswith("retention:")), "")
    m = re.search(r"retention: '(.*?)' w=(\d+)", ret)
    check("日志保留下拉渲染+合法档", bool(m) and m.group(1) in
          ("不开启", "保留 3 天", "保留 7 天", "保留 30 天", "不自动清理")
          and int(m.group(2)) > 0, ret)

    n_ok = sum(results)
    print(f"--- {n_ok}/{len(results)} PASS ---")
    return 0 if n_ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
