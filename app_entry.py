"""py2app 入口脚本：把 src 加入路径后启动 adbtool。"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "src"))

from adbtool.main import main

main()
