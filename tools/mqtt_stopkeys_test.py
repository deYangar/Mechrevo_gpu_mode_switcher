r"""gcu_mqtt_command stop_keys 与 kill_by_name 128 的代码层单测（假 broker，不碰真 GCU）。

用例：
1) stop_keys 命中 → 判据码一到立即返回（远小于 listen_secs，2026-11-13 F1 优化）
2) 不传 stop_keys → 等满 listen_secs（旧行为不变）
3) kill_by_name 对不存在进程名 → taskkill rc=128，按已结束算成功（F4）

用法：python tools/mqtt_stopkeys_test.py；全过退出码 0。
"""

import json
import os
import socket
import struct
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dgpu_detective as eng  # noqa: E402


def _read_packet(conn):
    head = conn.recv(1)
    if not head:
        return None
    ptype = head[0] >> 4
    mult, length = 1, 0
    while True:
        b = conn.recv(1)
        if not b:
            return None
        length += (b[0] & 0x7F) * mult
        mult *= 128
        if not b[0] & 0x80:
            break
    body = b""
    while len(body) < length:
        chunk = conn.recv(length - len(body))
        if not chunk:
            return None
        body += chunk
    return ptype, body


def start_fake_broker(payloads_after_publish: list[str]):
    """单连接假 broker：CONNACK → 收 2 个 SUBSCRIBE → 收 PUBLISH(控制命令) 后
    依次回发 Setting/Status PUBLISH，等客户端断开。返回 (port, state)。"""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    state = {"published": None, "closed": None, "error": None}

    def run():
        try:
            conn, _ = srv.accept()
            conn.settimeout(10)
            assert _read_packet(conn)[0] == 1  # CONNECT
            conn.sendall(bytes([0x20, 0x02, 0x00, 0x00]))  # CONNACK rc=0
            _read_packet(conn)  # SUBSCRIBE ×2（客户端不等 SUBACK）
            _read_packet(conn)
            ptype, _body = _read_packet(conn)  # PUBLISH Setting/Control
            assert ptype == 3
            state["published"] = time.monotonic()
            for payload in payloads_after_publish:
                topic = "Setting/Status"
                out = struct.pack(">H", len(topic)) + topic.encode() + payload.encode()
                conn.sendall(bytes([0x30, len(out)]) + out)
            while _read_packet(conn) is not None:
                pass  # 等客户端断开（剩余长度假设单字节：payload 均小于 128 字节）
            state["closed"] = time.monotonic()
            conn.close()
        except Exception as e:  # noqa: BLE001
            state["error"] = repr(e)
        finally:
            srv.close()

    threading.Thread(target=run, daemon=True).start()
    return port, state


def main() -> int:
    results = []

    def check(name, ok, note=""):
        results.append(ok)
        print(f"{'PASS' if ok else 'FAIL'}  {name}  {note}", flush=True)

    # 1) stop_keys 命中 → 秒回
    port, state = start_fake_broker(
        [json.dumps({"CheckDGpuStatusforIGpuOnlyOnSuccess": 1})])
    eng.GCU_MQTT_PORT = port
    t0 = time.monotonic()
    st, events = eng.gcu_mqtt_command(
        eng.GCU_ECO_ON_CMD, listen_secs=8.0,
        stop_keys=("CheckDGpuStatusforIGpuOnlyOnSuccess",))
    dt = time.monotonic() - t0
    check("stop_keys 命中即返回", st == "ok" and dt < 3.0,
          f"耗时 {dt:.2f}s（listen 8s），回报 {len(events)} 条，broker {state['error']}")

    # 2) 不传 stop_keys → 等满（旧行为）
    port2, state2 = start_fake_broker(
        [json.dumps({"CheckDGpuStatusforIGpuOnlyOnSuccess": 2})])
    eng.GCU_MQTT_PORT = port2
    t0 = time.monotonic()
    st2, events2 = eng.gcu_mqtt_command(eng.GCU_ECO_ON_CMD, listen_secs=4.0)
    dt2 = time.monotonic() - t0
    check("不传 stop_keys 等满 listen", st2 == "ok" and dt2 >= 3.8,
          f"耗时 {dt2:.2f}s（listen 4s），broker {state2['error']}")

    # 3) kill_by_name 对不存在的进程名 → rc=128 按成功
    ok3, msg3 = eng.kill_by_name("definitely_not_running_xyz.exe")
    check("taskkill 128 按已结束", ok3 and msg3 == "", f"ok={ok3} msg={msg3!r}")

    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
