"""测试夹具：仅把仓库根目录加入 sys.path。

entity_mapping / mapping_config / catalog 均为纯 Python 模块，
不依赖 Home Assistant，可直接导入测试。
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
