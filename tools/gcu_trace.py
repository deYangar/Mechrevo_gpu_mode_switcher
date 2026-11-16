# -*- coding: utf-8 -*-
"""GCU 切换动作全程嗅探：MQTT 总线 + NV 组件进程 + 显示模式，三路时间线。

纯被动观测，不发任何切换指令（除了 MQTT 保活 PINGREQ）。
用法: python tools/gcu_trace.py [时长秒，默认 120] [--no-mqtt]
  --no-mqtt：跳过 MQTT 线——与 dgpu_detective --eco 同跑时必加（两者都用
  UWPClient_4 席位，互踢会砍掉编排的判据码回报）；判据码时刻看编排自己的日志。
输出: stdout + logs/gcu_trace_时间戳.txt；Ctrl+C 或到时停止后自动拉
System 事件日志 7036（服务状态变化）补第四路时间线（本机基本不记，仅兜底）。
"""
import json
import socket
import struct
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dgpu_detective as eng  # noqa: E402  复用 MQTT 连接层与模式判读

HOST, PORT = "127.0.0.1", 13688
CLIENT_ID = "UWPClient_4"  # 与主脚本 eco 切换同席位（实测 4 平时空闲；3 常被占）

# NV 自家组件名单（PROTECTED_PROCESSES 的 NV 子集 + 常见伴生）
NV_NAMES = {
    "nvcontainer.exe", "nvdisplay.container.exe", "nvidia overlay.exe",
    "nvidia share.exe", "nvidia web helper.exe",
}

T0 = time.monotonic()


def ts() -> str:
    """统一时间戳：[开机以来秒.毫秒] 挂钟时刻。"""
    return f"[+{time.monotonic() - T0:7.3f}s] {datetime.now().strftime('%H:%M:%S.%f')[:-3]}"


class Trace:
    def __init__(self, path: Path):
        self.f = open(path, "w", encoding="utf-8", buffering=1)
        self.lock = threading.Lock()

    def log(self, line: str) -> None:
        with self.lock:
            print(line, flush=True)
            self.f.write(line + "\n")

    def close(self) -> None:
        self.f.close()


def mqtt_sniff(trace: Trace, stop: threading.Event) -> None:
    """订阅 # 全量 topic 嗅探；被拒则退回已知 topic 清单。"""
    try:
        sock = socket.create_connection((HOST, PORT), timeout=5)
    except OSError as e:
        trace.log(f"{ts()} [mqtt] X 连不上 {HOST}:{PORT}（{e}）——GCU 后台没在跑？")
        return
    try:
        sock.settimeout(1.0)
        sock.sendall(eng._mqtt_packet(
            eng._mqtt_str("MQTT") + bytes([4, 0xC2]) + struct.pack(">H", 30) +
            eng._mqtt_str(CLIENT_ID) + eng._mqtt_str(eng.GCU_MQTT_USER) +
            eng._mqtt_str(eng.GCU_MQTT_PASS), 1))
        while True:
            p = eng._mqtt_read_packet(sock)
            if p is None:
                trace.log(f"{ts()} [mqtt] X broker 在 CONNACK 前断开")
                return
            if p[0] == 2:
                rc = p[2][1] if len(p[2]) > 1 else -1
                if rc != 0:
                    trace.log(f"{ts()} [mqtt] X CONNACK 拒绝 rc={rc}（2=clientId 席位被占）")
                    return
                break
        # 先订 #（通配全量）；SUBACK 码 0x80 = 被拒 → 退回已知清单
        topics = ["#"]
        sock.sendall(eng._mqtt_packet(
            struct.pack(">H", 1) + eng._mqtt_str("#") + bytes([0]), 8, flags=2))
        granted = None
        while granted is None:
            p = eng._mqtt_read_packet(sock)
            if p is None:
                trace.log(f"{ts()} [mqtt] X 等 SUBACK 时断开")
                return
            if p[0] == 9:  # SUBACK
                granted = p[2][2] if len(p[2]) > 2 else 0
        if granted == 0x80:
            topics = ["Setting/Status", "GPUDevice/Status", "Setting/Control"]
            for i, t in enumerate(topics, start=2):
                sock.sendall(eng._mqtt_packet(
                    struct.pack(">H", i) + eng._mqtt_str(t) + bytes([0]), 8, flags=2))
            trace.log(f"{ts()} [mqtt] # 订阅被拒（ACL），退回已知 topic 清单")
        trace.log(f"{ts()} [mqtt] ✓ 已订阅: {topics}，开始嗅探（只收不发）")
        last_ping = time.monotonic()
        while not stop.is_set():
            if time.monotonic() - last_ping > 20:
                sock.sendall(eng._mqtt_packet(b"", 12))
                last_ping = time.monotonic()
            try:
                p = eng._mqtt_read_packet(sock)
            except socket.timeout:
                continue
            if p is None:
                trace.log(f"{ts()} [mqtt] 连接被 broker 断开")
                return
            if p[0] == 3:  # PUBLISH
                topic, payload = eng._mqtt_parse_publish(p[1], p[2])
                trace.log(f"{ts()} [mqtt] {topic}: {payload[:500]}")
    finally:
        sock.close()


def nv_snapshot(trace: Trace, stop: threading.Event) -> None:
    """每 2s 快照 NV 名单进程，diff 打退出/新增时刻。"""
    prev = None
    while not stop.is_set():
        try:
            out = subprocess.run(
                ["tasklist", "/fo", "csv", "/nh"],
                capture_output=True, timeout=8).stdout.decode("gbk", "replace")
        except (OSError, subprocess.TimeoutExpired) as e:
            trace.log(f"{ts()} [nv] tasklist 失败: {e}")
            out = ""
        cur = {}
        for line in out.splitlines():
            parts = line.split('","')
            if len(parts) >= 2:
                name = parts[0].strip('"').lower()
                if name in NV_NAMES:
                    cur.setdefault(name, set()).add(parts[1].strip('"'))
        if prev is not None and cur != prev:
            for name in sorted(set(prev) - set(cur)):
                for pid in sorted(prev[name]):
                    trace.log(f"{ts()} [nv ] 退出 {name} pid={pid}")
            for name in sorted(set(cur) - set(prev)):
                for pid in sorted(cur[name]):
                    trace.log(f"{ts()} [nv ] 新增 {name} pid={pid}")
            for name in sorted(set(cur) & set(prev)):
                gone = prev[name] - cur[name]
                new = cur[name] - prev[name]
                for pid in sorted(gone):
                    trace.log(f"{ts()} [nv ] 退出 {name} pid={pid}")
                for pid in sorted(new):
                    trace.log(f"{ts()} [nv ] 新增 {name} pid={pid}")
        if prev is None:
            for name in sorted(cur):
                trace.log(f"{ts()} [nv ] 在场 {name} pid={','.join(sorted(cur[name]))}")
        prev = cur
        stop.wait(2.0)


def mode_snapshot(trace: Trace, stop: threading.Event) -> None:
    """每 3s 读显示模式，变化时刻打点。"""
    prev = None
    while not stop.is_set():
        try:
            res = eng.read_display_mode_state()
            cur = res.mode
        except Exception as e:  # 模式判读失败不中断嗅探
            trace.log(f"{ts()} [mode] 读模式失败: {e!r}")
            cur = None
        if cur != prev and cur is not None:
            trace.log(f"{ts()} [mode] → {cur}（{eng.MODE_LABELS.get(cur, cur)}）")
            prev = cur
        stop.wait(3.0)


def dump_service_events(trace: Trace, minutes: int = 10) -> None:
    """拉 System 日志 7036（服务状态变化），过滤 NV/GCU 相关。"""
    since = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    from datetime import timedelta
    since = (datetime.now() - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S")
    try:
        out = subprocess.run(
            ["wevtutil", "qe", "System",
             "/q:*[System[(EventID=7036) and TimeCreated[timediff(@SystemTime) <= "
             f"{minutes * 60000}]]]",
             "/rd:true", "/c:200", "/f:text"],
            capture_output=True, timeout=15).stdout.decode("gbk", "replace")
    except (OSError, subprocess.TimeoutExpired) as e:
        trace.log(f"[svc ] X 拉事件日志失败: {e}")
        return
    trace.log(f"\n===== System 日志 7036（近 {minutes} 分钟，NV/GCU 相关）=====")
    keep = ("nvcontainer", "nvdisplay", "nvidia", "gcu", "g-sen", "mechrevo")
    block, hits = [], 0
    for line in out.splitlines() + [""]:
        block.append(line)
        if not line.strip():  # 一条事件结束
            text = "\n".join(block)
            low = text.lower()
            if any(k in low for k in keep) and "停止" in text or \
               any(k in low for k in keep) and ("已停止" in text or "正在启动" in text or "正在停止" in text):
                trace.log(text.rstrip())
                hits += 1
            block = []
    if not hits:
        trace.log("（无 NV/GCU 相关服务状态变化）")


def main() -> int:
    argv = [a for a in sys.argv[1:] if a != "--no-mqtt"]
    no_mqtt = "--no-mqtt" in sys.argv[1:]
    duration = float(argv[0]) if argv else 120.0
    logdir = Path(__file__).resolve().parents[1] / "logs"
    logdir.mkdir(exist_ok=True)
    path = logdir / f"gcu_trace_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    trace = Trace(path)
    trace.log(f"===== GCU 切换动作嗅探 {datetime.now():%Y-%m-%d %H:%M:%S} "
              f"（时长 {duration:.0f}s，被动观测{'+ 无 MQTT' if no_mqtt else ''}）=====")
    trace.log("现在去控制中心点「核显省电」，全程会被记录。Ctrl+C 提前结束。\n")
    stop = threading.Event()
    probes = (nv_snapshot, mode_snapshot) if no_mqtt else \
             (mqtt_sniff, nv_snapshot, mode_snapshot)
    threads = [threading.Thread(target=fn, args=(trace, stop), daemon=True)
               for fn in probes]
    for t in threads:
        t.start()
    try:
        time.sleep(duration)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=3)
        dump_service_events(trace)
        trace.log(f"\n===== 嗅探结束 {datetime.now():%H:%M:%S}，日志: {path} =====")
        trace.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
