# adbpush

macOS 下通过 ADB 连接安卓手机的可视化文件管理器。

双栏界面：左侧手机存储，右侧本地磁盘，支持双向传输与目录操作。

## 功能

- 双栏文件浏览：手机（`/sdcard`）与本地磁盘
- 上传 / 下载文件（后台线程，不卡界面）
- 手机目录操作：新建文件夹、删除、剪切/复制/粘贴、移动
- 双击进入文件夹、后退、刷新
- 设备下拉切换、自动识别存储根目录
- 右键菜单：在手机上打开文件

## 环境要求

- macOS
- Python 3.9+
- Android SDK platform-tools（`adb` 在 `PATH` 中）
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
venv/bin/python -m adbpush.main
```

## 测试

```bash
make test
```

## 项目结构

```
src/adbpush/
  adb_client.py   ADB 命令封装（列表、push/pull、mkdir、rm、mv、cp）
  file_panel.py   可复用的文件面板（本地/手机通用）
  main_window.py  主窗口 + 后台传输线程
  main.py         程序入口
tests/            单元测试
```

## 说明

- 文件删除不可恢复，请谨慎操作。
- 手机文件管理器操作走 `adb shell`（toybox `ls`），兼容 MIUI/HyperOS 等常见 ROM。