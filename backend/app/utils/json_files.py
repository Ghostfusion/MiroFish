"""
JSON 状态文件的原子写入与容错读取

状态文件（project.json / state.json / run_state.json / 报告 meta 等）会被
后台线程与请求线程并发读写。直接 open(path, 'w') 会先截断文件再写出，
并发读者可能读到半个 JSON 并抛 JSONDecodeError。这里统一走
“临时文件 + os.replace”，让读者看到的永远是完整的旧文件或完整的新文件；
Windows 上 os.replace 需要短暂的独占窗口，因此读写两侧都对
PermissionError 做有限重试。
"""

import json
import os
import tempfile
import time
from typing import Any

_JSON_INDENT = 2
# Windows 上 os.replace 在目标文件被其他句柄打开时会抛 PermissionError，
# 需要短暂重试等待读者关闭文件
_REPLACE_ATTEMPTS = 50
_REPLACE_DELAY_SECONDS = 0.02


def _replace_with_retry(temp_path: str, path: str) -> None:
    """Replace ``path`` with ``temp_path``, retrying Windows sharing violations."""

    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(temp_path, path)
            return
        except PermissionError:
            if attempt == _REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(_REPLACE_DELAY_SECONDS)


def write_json_atomic(path: str, data: Any, *, indent: int | None = _JSON_INDENT) -> None:
    """
    原子地写入 JSON 文件

    Args:
        path: 目标文件路径
        data: 可 JSON 序列化的数据
        indent: 缩进，None 表示紧凑输出
    """
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)

    fd, temp_path = tempfile.mkstemp(
        prefix=f".{os.path.basename(path)}.",
        suffix=".tmp",
        dir=directory,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=indent)
            handle.flush()
            os.fsync(handle.fileno())
        _replace_with_retry(temp_path, path)
    except BaseException:
        try:
            os.remove(temp_path)
        except OSError:
            pass
        raise


def read_json(path: str) -> Any:
    """
    读取 JSON 文件，对 Windows 上的瞬时共享冲突做有限重试

    与 write_json_atomic 配对使用。JSONDecodeError 不重试：配合原子写入，
    解析失败说明文件本身有问题，而不是并发导致。

    Raises:
        FileNotFoundError: 文件不存在
        json.JSONDecodeError: 内容不是合法 JSON
    """
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except PermissionError:
            if attempt == _REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(_REPLACE_DELAY_SECONDS)

    raise AssertionError("unreachable")