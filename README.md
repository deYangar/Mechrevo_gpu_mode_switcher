# dgpu_detective — 独显占用侦探

识别**到底是哪个进程在占用独立显卡**，导致独显无法休眠/关闭。

灵感与计数器方案借鉴自 [shanselman/gputaskman](https://github.com/shanselman/gputaskman)
（参考仓库快照在 `reference/gputaskman-src/`，核心是它的 `pdh.py` + `dxgi.py`）。

## 用法

```powershell
python dgpu_detective.py                # 抓当前独显持有进程清单
python dgpu_detective.py --list-gpus    # 列出所有显卡适配器（含独显判定）
python dgpu_detective.py --all          # 核显等非独显适配器也一并报告
python dgpu_detective.py --watch        # 持续监视独显，新持有进程冒头即时报告
python dgpu_detective.py --watch --interval 2
python dgpu_detective.py --json         # 一次性 JSON 快照
python dgpu_detective.py --eco          # 核显省电一键切换：列出占用→确认关闭→移交 gpu_switch
python dgpu_detective.py --eco --gpu-switch <路径>   # 指定 gpu_switch.py 位置
```

仅 Python 标准库（ctypes 调 `pdh.dll` + `dxgi.dll`），无需管理员权限。

## 原理

数据来自 **WDDM 图形内核的性能计数器**——任务管理器「性能 → GPU」背后同一套数据，
D3D / Vulkan / OpenGL / CUDA 全都过这一层（GeForce WDDM 模式下 nvidia-smi 每进程
显存常报 N/A，这套计数器反而全都有）。

三层链路：

1. **DXGI 枚举**（`CreateDXGIFactory1 → EnumAdapters1`）拿到每块卡的 LUID、厂商、
   独立显存 → 识别哪块是独显（NVIDIA 即独显；其余按独立显存 ≥512MB 判）。
2. **PDH 计数器**（`PdhAddEnglishCounterW`，中文系统不受计数器名本地化影响）：
   - `\GPU Process Memory(*)\Local Usage` — 进程驻留 VRAM 字节（用 Local 而非
     Dedicated：后者把共享 surface 重复记账，dwm.exe 能虚读到 15GB+）
   - `\GPU Process Memory(*)\Non Local Usage` — 溅到系统 RAM 的字节
   - `\GPU Process Memory(*)\Total Committed` — 提交总量
   - `\GPU Engine(*)\Utilization Percentage` — 每进程每引擎利用率
3. **实例名自带映射**：`pid_1234_luid_0x..._phys_0_eng_0_engtype_3D`，
   过滤 LUID=独显 → 直接得到独显上的全部持有进程。

关键点：**进程只要在独显上创建了 GPU 上下文，即使利用率 0%、显存 0，
引擎计数器实例也会出现**——这正是抓「导致独显关不掉的隐藏持有者」的依据。

## 输出解读

- 「仅上下文(0%)」= 进程挂着 GPU 上下文但没在干活——独显关不掉往往是这类
- 引擎利用率是 rate 计数器，一次性模式自动垫 0.5s 采样间隔
- `<拒绝访问 pid=N>` = 受保护进程（驱动/反作弊等），普通权限查不到名字
- pid 4 = System 内核进程

## 核显省电一键切换（--eco）

配合 `gpu_mode_switch` 项目的 `gpu_switch.py`（官方同款终态序列 CLI）使用，把
「找出占用 → 关软件 → 切换」串成一条命令：

1. **检测**：WDDM 口径抓独显全部持有进程（比 nvidia-smi 全，含 0 显存纯上下文
   持有者），自动过滤已退出的陈旧计数器实例
2. **分类提示**：用户软件列为「可关闭候选」；系统/服务组件（dwm / explorer /
   nvcontainer 等）只列出不动——杀了会被系统拉回，且实测不阻碍 PnP 禁用
3. **关闭**：确认后按进程名 `taskkill /F /T /IM` 强杀——**全部同名实例与各自的
   进程树一起结束**（覆盖同名子进程场景）；taskkill 结果如实显示
4. **复查**：重新采样；同名新 pid 标「被守护/自启拉回」，其余残留如实报告
5. **切换**：确认后调 `gpu_switch.py on`，完成 PnP 禁用 + json/注册表/GCU 状态同步

`--eco` 开头即提权（UAC 一次）：HwMonitor/GamePP 等硬件监控多以管理员运行，
不提权 taskkill 直接被拒；提权后 gpu_switch 也直接执行，不再二次弹窗。
每一步都有确认环节，随时可取消。

## 独显关不掉的排查思路

1. 跑一次本工具，看持有清单。常见元凶：
   - **硬件监控软件**（HWMonitor、GamePP、AIDA64 等）轮询 GPU 传感器会强制独显常驻
   - **NVIDIA Overlay / nvcontainer**（GeForce Experience/APP 的覆盖层与容器服务）
   - 浏览器/客户端软件的 GPU 加速渲染
2. 逐个退出清单里的可疑进程，观察独显能否歇机（可开 `--watch` 盯着）
3. 进程全清了独显仍不休眠 → 非进程因素：
   - 显示器直连独显（混合屏/高刷屏常把输出焊在独显上，核显带不动）
   - 驱动面板的电源管理设置（NVIDIA 控制面板「首选图形处理器」）
   - `nvidia-smi -q` 查看当前电源状态与持有原因

## 文件

| 文件 | 说明 |
|---|---|
| `dgpu_detective.py` | 主脚本（单文件，纯标准库） |
| `reference/gputaskman-src/` | 参考仓库快照（上游 MIT 许可） |
