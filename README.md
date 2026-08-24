# adbtool

macOS 下通过 ADB 连接安卓手机的综合工具箱。

双栏界面：左侧手机存储，右侧本地磁盘，支持双向传输与目录操作，同时集成投屏、应用管理、刷机等常用功能。

## 功能

### 文件管理
- 双栏文件浏览：手机（`/sdcard`）与本地磁盘
- 上传 / 下载文件（后台线程，不卡界面）
- 手机目录操作：新建文件夹、删除、剪切/复制/粘贴、移动
- 双击进入文件夹、后退、刷新
- 设备下拉切换、自动识别存储根目录
- 右键菜单：在手机上打开文件
- 列表 / 宫格视图切换，拖拽传输

### 投屏与录制
- scrcpy 投屏（可自动安装）
- 手机截屏，保存到桌面
- 手机屏幕录制

### 应用管理
- 查看手机已安装应用（第三方 / 系统应用）
- 卸载、启动、停止、清除应用数据
- 安装 APK 文件

### 系统工具
- 无线 ADB 配对连接
- 系统开关：Wi-Fi、蓝牙、充电常亮、动画缩放
- Logcat 实时日志查看
- 重启设备（系统 / Recovery / Fastboot / FastbootD）

### Fastboot 刷机
- 自动检测 fastboot 设备
- 单分区镜像刷写（`boot` / `recovery` / `vbmeta` / `super` 等）
- 完整包刷入：解析并刷写包内所有镜像，A/B 设备自动刷双槽并 `set_active`
- 支持普通分区镜像包与 `flash-all` 脚本，自动识别分区、去重、附加 `super`/`cust` 等
- 支持 `payload.bin` 全量包（`update_engine` 格式）自动解包后刷写
- 支持 `.img.zst` 压缩镜像，自动用 `zstd` 解压（未安装时提示 `brew install zstd`）
- 保留数据 / 清除数据两种刷写模式
- 刷机进度可视化

## 环境要求

- macOS
- Python 3.9+
- Android SDK platform-tools（`adb` 在 `PATH` 中，或软件自动下载）
- 手机开启 USB 调试（无线或 USB 连接均可）

## 安装

```bash
python3 -m venv venv
venv/bin/pip install -e .
```

## 运行

```bash
make run
# 或
venv/bin/python -m adbtool.main
```

## 测试

```bash
make test
```

## 项目结构

```
src/adbtool/
  adb_client.py      ADB 命令封装（设备管理、文件操作、截屏、系统控制）
  fastboot_client.py  Fastboot 命令封装（刷机、getvar、erase）
  payload_dumper.py   payload.bin 解包（下载并校验 payload-dumper-go）
  file_panel.py       可复用的文件面板（本地/手机通用，列表/宫格视图）
  tools.py            工具对话框（配对、应用管理、logcat、录屏、系统开关、刷机）
  main_window.py      主窗口 + 后台传输线程
  env_check.py        ADB 环境检测与自动下载
  main.py             程序入口
tests/                单元测试
scripts/              辅助脚本（生成 .app/.dmg）
```

## 打包（生成 .dmg）

需要本机已装好依赖（venv 内已有 PyQt6、py2app）：

```sh
make dmg        # 或 scripts/make_app.sh
```

流程：`py2app` 把 Python + PyQt6 + 代码打成自包含的 `dist/ADB Tool.app`，
再用 `hdiutil` 生成 `dist/ADB Tool-<版本>.dmg`。双击 dmg，把图标拖到
Applications 即可安装。首次运行时如系统提示「无法验证开发者」，右键打开或
在「系统设置 → 隐私与安全性」里允许。

> 说明：adb/fastboot 不随包内置，运行时会自动下载到 `~/Library/Android/sdk`
> （或读取已安装的 platform-tools）；`scrcpy`、`magiskboot`、`ffmpeg` 等按需
> 命令仍需在系统里另行安装。

### 架构说明（Intel / Apple Silicon）

- 打包脚本会把**可执行文件和 Python 运行时**合成 universal（x86_64 + arm64）
  双切片，但 **PyQt6 的 Qt 运行库取决于构建机器的架构**，不会跨架构合成。
- 因此最终 dmg 的可用性由**构建机器**决定：
  - 在 **Intel Mac** 上 `make dmg`：产物在 Intel 上原生运行；在 Apple Silicon
    （M 系列）上需安装 **Rosetta 2** 转译运行，不是原生 arm64。
  - 在 **Apple Silicon Mac** 上 `make dmg`：产物在 M 系列上原生运行；在
    Intel 上无法运行。
- 要一份**同时在 Intel 与 M 系列原生运行**的 dmg，需分别在两台机器上构建，再用
  `lipo` 合并双切片（一般工具类 app 不必如此，M 系列装 Rosetta 2 即可）。

> 检测构建机架构：`uname -m`（x86_64 = Intel / Rosetta，arm64 = Apple Silicon）。

## 说明

- 文件删除不可恢复，请谨慎操作。
- 手机文件管理器操作走 `adb shell`（toybox `ls`），兼容 MIUI/HyperOS 等常见 ROM。
- 刷机操作有风险，请确认设备型号与刷机包匹配后再操作。
