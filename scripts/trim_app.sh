#!/bin/zsh
# py2app 构建后裁剪，只保留 QtWidgets 栈用到的部分，显著减小 .app 体积。
set -e

APP="${1:?用法: trim_app.sh <ADB Tool.app>}"
PYQT="$APP/Contents/Resources/lib/python3.9/PyQt6"
QT6="$PYQT/Qt6"

echo "==> 裁剪 Qt6 冗余内容"

# 1) QML / Scintilla / 翻译，全部用不到
rm -rf "$QT6/qml" "$QT6/qsci" "$QT6/translations" "$QT6/resources"

# 2) lib 框架白名单（QtWidgets 栈及其依赖）
KEEP_LIB=(QtCore QtGui QtWidgets QtDBus QtPrintSupport QtOpenGL QtOpenGLWidgets QtConcurrent QtSvg QtSvgWidgets)
for d in "$QT6"/lib/Qt*.framework; do
  name="$(basename "$d")"
  name="${name%.framework}"
  keep=0
  for k in "${KEEP_LIB[@]}"; do
    [ "$k" = "$name" ] && keep=1
  done
  if [ "$keep" = "0" ]; then
    rm -rf "$d"
  fi
done

# 3) 插件白名单
KEEP_PLUGIN=(platforms imageformats styles printsupport)
for d in "$QT6"/plugins/*; do
  name="$(basename "$d")"
  keep=0
  for k in "${KEEP_PLUGIN[@]}"; do
    [ "$k" = "$name" ] && keep=1
  done
  if [ "$keep" = "0" ]; then
    rm -rf "$d"
  fi
done

# 4) 未用到的 PyQt6 .so 模块（保留 Widgets 栈 + sip）
KEEP_SO=(QtCore QtGui QtWidgets QtDBus QtPrintSupport sip)
for so in "$PYQT"/*.so; do
  base="$(basename "$so")"
  mod="${base%.abi3.so}"
  mod="${mod%.so}"
  keep=0
  for k in "${KEEP_SO[@]}"; do
    [ "$k" = "$mod" ] && keep=1
  done
  case "$mod" in sip*) keep=1 ;; esac   # PyQt6.sip 的 .so 名为 sip.cpython-39-darwin
  if [ "$keep" = "0" ]; then
    rm -f "$so"
  fi
done

# 5) 裁剪后重新 ad-hoc 签名
codesign --force --deep --sign - "$APP" >/dev/null 2>&1 || true

echo "==> 裁剪完成"
