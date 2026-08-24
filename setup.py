"""py2app 打包配置：把 adbtool 打包成自包含的 macOS .app。

用法：python setup.py py2app   （输出到 dist/ADB Tool.app）
完整 .dmg 打包请运行 scripts/make_app.sh。
"""

import os
import sys

# 让 py2app 的模块分析器能找到 src/ 下的 adbtool 包
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from setuptools import setup

APP = ["app_entry.py"]

OPTIONS = {
    "argv_emulation": False,
    "iconfile": "assets/adbtool.icns",
    "packages": ["adbtool"],
    "includes": ["PyQt6.QtCore", "PyQt6.QtGui", "PyQt6.QtWidgets"],
    "excludes": [
        "PyQt6.QtQml",
        "PyQt6.QtQuick",
        "PyQt6.QtQuick3D",
        "PyQt6.QtQuickWidgets",
        "PyQt6.Qt3DCore",
        "PyQt6.Qt3DRender",
        "PyQt6.QtWebEngineCore",
        "PyQt6.QtWebEngineWidgets",
        "PyQt6.QtWebChannel",
        "PyQt6.QtMultimedia",
        "PyQt6.QtMultimediaWidgets",
        "PyQt6.QtNetwork",
        "PyQt6.QtSvg",
        "PyQt6.QtSvgWidgets",
        "PyQt6.QtOpenGL",
        "PyQt6.QtOpenGLWidgets",
        "PyQt6.QtXml",
        "PyQt6.QtSql",
        "PyQt6.QtTest",
        "PyQt6.QtDBus",
        "PyQt6.Qsci",
        "PyQt6.QtPdf",
        "PyQt6.QtPdfWidgets",
        "PyQt6.QtDesigner",
        "PyQt6.QtHelp",
    ],
    "plist": {
        "CFBundleName": "ADB Tool",
        "CFBundleDisplayName": "ADB Tool",
        "CFBundleIdentifier": "com.adbtool.app",
        "CFBundleExecutable": "adbtool",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": "0.1.0",
        "CFBundleVersion": "1",
        "CFBundleIconFile": "adbtool",
        "LSMinimumSystemVersion": "10.15",
        "NSHighResolutionCapable": True,
        "NSPrincipalClass": "NSApplication",
    },
}

setup(
    app=APP,
    name="ADB Tool",
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
