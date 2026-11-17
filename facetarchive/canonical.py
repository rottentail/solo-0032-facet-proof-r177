"""规范化 JSON、摘要与原子写入。

canonical 的序列化规则（ensure_ascii=False、sort_keys、紧凑分隔、禁止 NaN）
与渲染服务保持一致，但代码独立，避免接收者依赖服务私有实现。
"""
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

from .errors import require

HEX64 = re.compile(r"[a-f0-9]{64}\Z")
HEX32 = re.compile(r"[a-f0-9]{32}\Z")
ROUND_ID = re.compile(r"round_[0-9]{8}t[0-9]{6}_[a-f0-9]{8}\Z")


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def canonical_bytes(value):
    return canonical(value).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def digest_bytes(raw):
    require(isinstance(raw, (bytes, bytearray)), "type", "Expected bytes")
    return hashlib.sha256(bytes(raw)).hexdigest()


def sha256_file(path, limit=None):
    """流式计算文件摘要；可选字节上限，超限即失败，不把大文件读进内存。"""
    hasher = hashlib.sha256()
    total = 0
    with open(path, "rb") as stream:
        while True:
            block = stream.read(1024 * 1024)
            if not block:
                break
            total += len(block)
            if limit is not None and total > limit:
                raise _io_error("file_too_large", "File exceeds allowed size")
            hasher.update(block)
    return hasher.hexdigest(), total


def is_hex64(value):
    return isinstance(value, str) and bool(HEX64.fullmatch(value))


def is_round_id(value):
    return isinstance(value, str) and bool(ROUND_ID.fullmatch(value))


def atomic_write(path, raw):
    """临时文件 + fsync + 原子改名，保证中断不会留下半截正式文件。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, scratch = tempfile.mkstemp(prefix=".writing-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(scratch, path)
        _sync_dir(path.parent)
    finally:
        if os.path.exists(scratch):
            os.unlink(scratch)
    return path


def _sync_dir(path):
    try:
        fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def read_json_file(path):
    try:
        with open(path, "rb") as stream:
            raw = stream.read(64 * 1024 * 1024)
    except OSError as exc:
        raise _io_error("io", f"Cannot read {path}") from exc
    return parse_canonical_json(raw)


def parse_canonical_json(raw):
    try:
        return json.loads(raw, parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError, TypeError) as exc:
        from .errors import ArchiveError
        raise ArchiveError("json", "Expected finite, well-formed JSON") from exc


def _reject_constant(value):
    from .errors import ArchiveError
    raise ArchiveError("json", f"Non-finite JSON constant is not allowed: {value}")


def _io_error(code, message):
    from .errors import ArchiveError
    return ArchiveError(code, message)
