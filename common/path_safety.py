"""跨模块共用的文件系统路径对象检查。"""

from __future__ import annotations

import os
import stat
from pathlib import Path


def is_link_or_reparse_point(path: Path) -> bool:
    """同时识别 POSIX 符号链接和 Windows 重解析点。"""

    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse_flag)


def has_link_or_reparse_component(path: Path) -> bool:
    """检查绝对路径中任一已有组件是否为链接或重解析点。"""

    lexical = Path(os.path.abspath(os.fspath(path)))
    current = Path(lexical.anchor)
    if is_link_or_reparse_point(current):
        return True
    for part in lexical.parts[1:]:
        current = current / part
        if is_link_or_reparse_point(current):
            return True
    return False
