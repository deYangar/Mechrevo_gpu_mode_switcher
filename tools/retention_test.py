r"""日志保留策略单测（引擎 cleanup_logs + GUI settings 读写与档位表）。

C1：超龄 .txt 删（含大写 .TXT）、新文件与超龄非 txt 不动
C2：logs 目录不存在 → (0, 0) 不炸
C3：被占用文件计入失败数、不中断、其余照删（Windows 句柄占用）
C4：settings.json 原子写回读、损坏/非法值回落默认档、档位表完整性

用法：python tools/retention_test.py；全过退出码 0。
"""

import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dgpu_detective as eng  # noqa: E402
import dgpu_gui as gui  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))


def _with_temp_base(fn):
    """app_base_dir 指到临时目录跑 fn(base)，跑完还原（eng 与 gui 共用引用）。"""
    saved = eng.app_base_dir
    with tempfile.TemporaryDirectory() as d:
        eng.app_base_dir = lambda: d
        try:
            fn(d)
        finally:
            eng.app_base_dir = saved


def _touch(d, name, mtime):
    p = os.path.join(d, name)
    with open(p, "w", encoding="utf-8") as f:
        f.write("x")
    os.utime(p, (mtime, mtime))
    return p


OLD = 40 * 86400  # 明显超 30 天档


def c1(base):
    now = time.time()
    logs = os.path.join(base, "logs")
    os.makedirs(logs)
    old_txt = _touch(logs, "eco_old.txt", now - OLD)
    old_up = _touch(logs, "direct_old.TXT", now - OLD)
    new_txt = _touch(logs, "eco_new.txt", now - 86400)  # 1 天前，7 天档内
    old_md = _touch(logs, "readme.md", now - OLD)
    removed, failed = eng.cleanup_logs(7)
    check("C1 超龄删/新留/非txt不动", removed == 2 and failed == 0
          and not os.path.exists(old_txt) and not os.path.exists(old_up)
          and os.path.exists(new_txt) and os.path.exists(old_md),
          f"removed={removed} failed={failed}")


def c2(base):
    removed, failed = eng.cleanup_logs(3)
    check("C2 目录不存在不炸", removed == 0 and failed == 0,
          f"removed={removed} failed={failed}")


def c3(base):
    now = time.time()
    logs = os.path.join(base, "logs")
    os.makedirs(logs)
    victim = _touch(logs, "eco_locked.txt", now - OLD)
    other = _touch(logs, "hybrid_old.txt", now - OLD)
    with open(victim, "r"):  # Windows：无 share-delete 句柄，remove 报错
        removed, failed = eng.cleanup_logs(3)
        check("C3 占用计失败不中断", removed == 1 and failed == 1
              and os.path.exists(victim) and not os.path.exists(other),
              f"removed={removed} failed={failed}")


def c4(base):
    gui._save_log_retention("3d")
    p = gui._settings_path()
    with open(p, encoding="utf-8") as f:
        raw = json.load(f)
    check("C4-1 原子写+回读", raw == {"log_retention": "3d"}
          and gui._load_log_retention() == "3d"
          and not os.path.exists(p + ".tmp"))
    gui._save_log_retention("forever")  # 二次写走 os.replace 覆盖路径
    check("C4-2 二次写覆盖", gui._load_log_retention() == "forever")
    with open(p, "w", encoding="utf-8") as f:
        f.write("{垃圾不是json")
    check("C4-3 损坏回落默认", gui._load_log_retention()
          == gui.LOG_RETENTION_DEFAULT)
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"log_retention": "xx非法"}, f)
    check("C4-4 非法值回落默认", gui._load_log_retention()
          == gui.LOG_RETENTION_DEFAULT)
    keys = [k for k, _l, _d in gui.LOG_RETENTION]
    days = gui.LOG_RETENTION_DAYS
    check("C4-5 档位表五档三态", len(keys) == len(set(keys)) == 5
          and None in days.values() and -1 in days.values()
          and all(isinstance(v, int) and v > 0
                  for v in days.values() if v is not None and v != -1)
          and gui.LOG_RETENTION_DEFAULT in days
          and set(days) == set(gui.LOG_RETENTION_LABEL))


def main() -> int:
    for fn in (c1, c2, c3, c4):
        _with_temp_base(fn)
    print(f"\n{'=' * 40}\n{'全部通过' if all(results) else '存在失败'}："
          f"{sum(results)}/{len(results)}")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
