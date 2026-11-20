r"""3040 适配施工回归单测（方案 §四 R4/R5，mock 层验证，不真发令）。

R4：Gen40 判档三态化——raw/reg 双空 → three_mode=None + TRI 词表 + 「未定」行；
    有证据时 True/False 各归位；gen50 不打印档位行。
    附带凭据前缀分线（E1/E2）：公式、注册表分线、无线索兜底。
R5：eco lite 路由——gen30+igpu_only=False 改走 cmd_eco_lite；
    词表无 lite 时原拒绝分支保留；3e pnp_disabled 语义三分支。
R6：Gen40 能力位误报修正——Support=0 而状态字段活跃回报 → 实证置 True
    （2026-11-20 40 系三模真机：eco 按钮被误禁零反应零日志）；
    无状态字段时 False 维持（双模机 lite 路由依据不变）。

用法：python tools/adapter3040_test.py；全过退出码 0。
"""

import contextlib
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dgpu_detective as eng  # noqa: E402


def _capture(fn, *args, **kwargs) -> tuple[str, object]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        out = fn(*args, **kwargs)
    return buf.getvalue(), out


def _make_info(**kw):
    base = dict(connected=False, connect_note="离线（测试注入）", generation="gen30",
                gen_source="测试注入", vocab=set(eng.GEN30_ACTIONS), three_mode=None,
                igpu_only=False, dgpu_direct=True, igpu_source="测试注入",
                dgpu_source="测试注入", hot_swap_note="", is_mechrevo=True,
                identity={}, direct_status="", igpu_status="", raw={},
                credential_prefix="UWPClient", credential_source="测试注入")
    base.update(kw)
    return eng.GcuInfo(**base)


def _probe_env(gen="gen40", raw_events=(), reg=None):
    """给 probe_gcu 换测试环境：代际注入 + MQTT 事件注入 + 注册表能力位注入。"""
    saved = (eng.detect_dgpu_generation, eng.enumerate_adapters,
             eng.gcu_mqtt_command, eng.read_gcu_registry_caps,
             eng.read_machine_identity)

    def restore():
        (eng.detect_dgpu_generation, eng.enumerate_adapters, eng.gcu_mqtt_command,
         eng.read_gcu_registry_caps, eng.read_machine_identity) = saved

    eng.detect_dgpu_generation = lambda adapters=None: (gen, "测试注入")
    eng.enumerate_adapters = lambda: []
    eng.gcu_mqtt_command = lambda *a, **k: (("ok", list(raw_events))
                                            if raw_events else ("离线（测试）", []))
    eng.read_gcu_registry_caps = (lambda: dict(reg)) if reg is not None else (lambda: {})
    eng.read_machine_identity = lambda: {}
    return restore


class _FakeKey:
    def __init__(self, values):
        self._values = values

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_registry(scenario: dict):
    """patch winreg：仅 SOFTWARE\\OEM\\GamingCenter2 根返回 scenario，其余 OSError。"""
    saved = (eng.winreg.OpenKey, eng.winreg.QueryValueEx)

    def restore():
        eng.winreg.OpenKey, eng.winreg.QueryValueEx = saved

    def open_key(root, path, reserved, access):
        if path == r"SOFTWARE\OEM\GamingCenter2" and scenario:
            return _FakeKey(scenario)
        raise OSError(2, "系统找不到指定的文件。")

    def query_value(key, name):
        if name in key._values:
            return key._values[name], 4  # REG_DWORD
        raise OSError(2, "系统找不到指定的文件。")

    eng.winreg.OpenKey = open_key
    eng.winreg.QueryValueEx = query_value
    return restore


def _restore_eco_on(saved):
    (eng.read_display_mode_state, eng.enrich_with_gcu_status,
     eng.probe_gcu, eng.cmd_eco_lite) = saved


def main() -> int:
    results = []

    def check(name, ok, note=""):
        results.append(ok)
        print(f"{'PASS' if ok else 'FAIL'}  {name}  {note}", flush=True)

    # ---------------- R4：Gen40 判档三态 ----------------
    # R4-1 双空（在线无回报 + 注册表无线索）→ 未定，词表给 TRI 全集，打印「未定」行
    r = _probe_env("gen40", raw_events=(), reg={})
    try:
        info = eng.probe_gcu()
        out, _ = _capture(eng.print_gcu_info, info)
    finally:
        r()
    check("R4-1 双空→None+TRI 集+未定行",
          info.three_mode is None and info.vocab == set(eng.GEN40_TRI_ACTIONS)
          and "未定（在线无回报且注册表无线索，按三模词表尝试）" in out,
          f"three_mode={info.three_mode} vocab={len(info.vocab)} 条")

    # R4-2 在线回报 iGPUModeOnlySupport=1 → 三模档
    r = _probe_env("gen40",
                   raw_events=[("Setting/Status",
                                json.dumps({"iGPUModeOnlySupport": 1}))], reg={})
    try:
        info = eng.probe_gcu()
    finally:
        r()
    check("R4-2 在线=1→三模档", info.three_mode is True
          and info.vocab == set(eng.GEN40_TRI_ACTIONS),
          f"three_mode={info.three_mode}")

    # R4-3 注册表 0（在线离线）→ 双模档，词表收窄 DUAL
    r = _probe_env("gen40", raw_events=(), reg={"igpumodeonlysupport": 0})
    try:
        info = eng.probe_gcu()
    finally:
        r()
    check("R4-3 注册表=0→双模档+DUAL 集",
          info.three_mode is False and info.vocab == set(eng.GEN40_DUAL_ACTIONS),
          f"three_mode={info.three_mode} vocab={len(info.vocab)} 条")

    # R4-4 gen50：three_mode 恒 None，不打印档位行（未定行是 Gen40 专属）
    r = _probe_env("gen50", raw_events=(), reg={})
    try:
        info = eng.probe_gcu()
        out, _ = _capture(eng.print_gcu_info, info)
    finally:
        r()
    check("R4-4 gen50 无档位行", info.three_mode is None
          and "Gen40 档位" not in out and "未定" not in out,
          f"three_mode={info.three_mode}")

    # ---------------- R4 附带：凭据前缀分线（E1/E2） ----------------
    prefix, src = eng.gcu_credential_prefix()  # 本机真注册表
    check("前缀-本机注册表", prefix == "UWPClient", f"前缀={prefix} 依据={src}")
    slots = eng.gcu_mqtt_slots()
    check("槽位公式（E1，_4→_3→_2）",
          tuple(tuple(s) for s in slots)
          == tuple((f"{prefix}_{n}", f"{prefix}_User_{n}",
                    f"{prefix}_Pwd888881772688_{n}") for n in (4, 3, 2)),
          f"首槽 {slots[0][0]}，末槽 {slots[-1][2]}")

    cases = [({"BridgeType": 0}, "UWPClient"),
             ({"BridgeType": 1, "CustomizeTarget": 16}, "UWPIntelClient"),
             ({"BridgeType": 1, "CustomizeTarget": 25}, "UWPIntelClient"),
             ({"BridgeType": 1, "CustomizeTarget": 29}, "UWPADATAClient"),
             ({"BridgeType": 1, "CustomizeTarget": 7}, "UWPSTDClient"),
             ({"BridgeType": 1}, "UWPClient"),      # CustomizeTarget 缺失 → 默认族
             ({}, "UWPClient")]                     # 无线索 → 默认族
    for scen, want in cases:
        r = _fake_registry(scen)
        try:
            got, got_src = eng.gcu_credential_prefix()
        finally:
            r()
        check(f"分线 {scen or '无线索'} → {want}", got == want, f"依据={got_src}")

    # ---------------- R5：eco lite 路由（mock 层，不真发令） ----------------
    # R5-1 gen30 + igpu_only=False + 词表含 lite → 改走 cmd_eco_lite(on=True)
    info = _make_info()
    saved = (eng.read_display_mode_state, eng.enrich_with_gcu_status,
             eng.probe_gcu, eng.cmd_eco_lite)
    calls = []
    st_hybrid = eng.DisplayModeState(
        problems=[("PCI\\VEN_10DE&DEV_2C19&SUBSYS_60411D05&REV_A1\\4&1234", "online")],
        dxgi_names=["NVIDIA GeForce RTX 5080 Laptop GPU"])
    eng.read_display_mode_state = lambda: st_hybrid
    eng.enrich_with_gcu_status = lambda s, listen_secs=4.0: "ok"
    eng.probe_gcu = lambda *a, **k: info
    eng.cmd_eco_lite = lambda on: (calls.append(on), 7)[1]
    try:
        out, rc = _capture(eng.cmd_eco_on, None, [])
    finally:
        _restore_eco_on(saved)
    check("R5-1 无MUX档→改走lite", calls == [True] and rc == 7
          and "改走官方运行时省电（lite）" in out,
          f"lite 调用={calls} rc={rc}")

    # R5-2 词表无 lite 族 + igpu_only=False → 原拒绝分支保留
    info = _make_info(vocab={"DGPU_DIRECT_CONNECT_TOGGLE_ON",
                             "DGPU_DIRECT_CONNECT_TOGGLE_OFF"})
    saved = (eng.read_display_mode_state, eng.enrich_with_gcu_status,
             eng.probe_gcu, eng.cmd_eco_lite)
    eng.read_display_mode_state = lambda: st_hybrid
    eng.enrich_with_gcu_status = lambda s, listen_secs=4.0: "ok"
    eng.probe_gcu = lambda *a, **k: info
    eng.cmd_eco_lite = lambda on: 0
    try:
        out, rc = _capture(eng.cmd_eco_on, None, [])
    finally:
        _restore_eco_on(saved)
    check("R5-2 词表无lite→原拒绝", rc == 1
          and "X 本机不支持核显省电" in out and "lite" not in out,
          f"rc={rc}")

    # R5-3 3e：lite 机型 pnp_disabled → 「已在 lite 省电态」rc=0
    st_pnp = eng.DisplayModeState(
        problems=[("PCI\\VEN_10DE&DEV_2C19&SUBSYS_60411D05&REV_A1\\4&1234", "disabled")],
        dxgi_names=[])
    info = _make_info()
    saved = (eng.read_display_mode_state, eng.enrich_with_gcu_status,
             eng.probe_gcu, eng.cmd_eco_lite)
    eng.read_display_mode_state = lambda: st_pnp
    eng.probe_gcu = lambda *a, **k: info
    eng.cmd_eco_lite = lambda on: 0
    try:
        out, rc = _capture(eng.cmd_eco_on, None, [])
    finally:
        _restore_eco_on(saved)
    check("R5-3 lite机型pnp_disabled→已在lite态", rc == 0
          and "已在 lite 省电态" in out and "--eco lite off" in out,
          f"rc={rc}")

    # R5-4 3e：词表无 lite 机型 pnp_disabled → 原假切换文案 rc=1
    info = _make_info(vocab={"DGPU_DIRECT_CONNECT_TOGGLE_ON"})
    saved = (eng.read_display_mode_state, eng.enrich_with_gcu_status,
             eng.probe_gcu, eng.cmd_eco_lite)
    eng.read_display_mode_state = lambda: st_pnp
    eng.probe_gcu = lambda *a, **k: info
    eng.cmd_eco_lite = lambda on: 0
    try:
        out, rc = _capture(eng.cmd_eco_on, None, [])
    finally:
        _restore_eco_on(saved)
    check("R5-4 非lite机型→假切换文案", rc == 1
          and "PnP 软禁用残留" in out, f"rc={rc}")

    # R5-5 3e：50 系在线（igpu_only=True）pnp_disabled → 不误判 lite（保护三模语义）
    info = _make_info(generation="gen50", vocab=set(eng.GEN50_ACTIONS),
                      igpu_only=True, three_mode=None)
    saved = (eng.read_display_mode_state, eng.enrich_with_gcu_status,
             eng.probe_gcu, eng.cmd_eco_lite)
    eng.read_display_mode_state = lambda: st_pnp
    eng.probe_gcu = lambda *a, **k: info
    eng.cmd_eco_lite = lambda on: 0
    try:
        out, rc = _capture(eng.cmd_eco_on, None, [])
    finally:
        _restore_eco_on(saved)
    check("R5-5 50系在线不误判lite", rc == 1
          and "PnP 软禁用残留" in out and "已在 lite" not in out,
          f"rc={rc}")

    # ---------------- R6：Gen40 能力位误报修正（状态字段实证 > Support 位） ----------------
    # R6-1 40 系三模真机实锤（2026-11-20 反馈）：Support=0 而 IGPU 状态字段活跃
    #     回报 → 实证置 True（eco 按钮不再被误禁）；词表仍按判档走 TRI 全集
    r = _probe_env("gen40",
                   raw_events=[("Setting/Status", json.dumps({
                       "IGpuOnlyConnectionSwitch_Support": "0",
                       "IGpuOnlyConnectionSwitch_Status": "IGPU_ONLY_CONNECT_RB_OFF"}))],
                   reg={})
    try:
        info = eng.probe_gcu()
        out, _ = _capture(eng.print_gcu_info, info)
    finally:
        r()
    check("R6-1 Support=0+状态字段在场→True",
          info.igpu_only is True and "状态字段实证" in info.igpu_source
          and info.vocab == set(eng.GEN40_TRI_ACTIONS),
          f"igpu_only={info.igpu_only} 来源={info.igpu_source}")

    # R6-2 真双模机不受影响：Support=0 且无状态字段 → False 维持（lite 路由依据不变）
    r = _probe_env("gen40",
                   raw_events=[("Setting/Status", json.dumps({
                       "IGpuOnlyConnectionSwitch_Support": "0"}))],
                   reg={})
    try:
        info = eng.probe_gcu()
    finally:
        r()
    check("R6-2 Support=0+无状态字段→False维持",
          info.igpu_only is False, f"igpu_only={info.igpu_only}")

    # R6-3 直连对称：Direct Support=0 而直连状态字段在场 → dgpu_direct=True
    r = _probe_env("gen40",
                   raw_events=[("Setting/Status", json.dumps({
                       "DGpuDirectConnectionSwitch_Support": "0",
                       "DiscreteGpuDirectConnectionSwitch_Status":
                           "DGPU_DIRECT_CONNECT_TOGGLE_OFF"}))],
                   reg={})
    try:
        info = eng.probe_gcu()
    finally:
        r()
    check("R6-3 直连对称→True",
          info.dgpu_direct is True and "状态字段实证" in info.dgpu_source,
          f"dgpu_direct={info.dgpu_direct} 来源={info.dgpu_source}")

    print(f"\n{'=' * 40}\n{'全部通过' if all(results) else '存在失败'}："
          f"{sum(results)}/{len(results)}")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
