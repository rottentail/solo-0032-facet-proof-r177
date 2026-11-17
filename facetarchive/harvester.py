"""可续做的观察轮次采集器。

状态机：staging（固定快照 -> 抓取核对内容闭包）-> sealed（生成不可变清单与
浏览目录 -> 追加只增账本）。中断后再次运行只会打开同一 staging 的同一快照，
不会重新抓另一个 SQL 时点；只有全部字节核对通过才能封存，中断永远不产生成功
标记。浏览页只在封存阶段生成，因此不存在跨轮拼接的半成品页面。
"""
from datetime import datetime, timezone
from pathlib import Path
import fcntl
import os

from . import source as source_mod
from .canonical import (atomic_write, canonical_bytes, read_json_file,
                        sha256_file)
from .errors import ArchiveError, require
from . import pngcheck

CHUNK = 1024 * 1024
MAX_RAW_BYTES = 512 * 512 * 4
MAX_PNG_BYTES = 8 * 1024 * 1024


class RoundInProgress:
    def __init__(self, archive_root, round_id):
        self.archive_root = Path(archive_root).resolve()
        self.round_id = round_id
        self.dir = self.archive_root / "staging" / round_id
        self.lock_path = self.dir / ".lock"
        self.journal_path = self.dir / "journal.json"
        self.snapshot_path = self.dir / "source.sqlite3"
        self.blob_dir = self.dir / "blobs"
        self._lock_fd = None

    def lock(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise ArchiveError("round_busy",
                               f"轮次 {self.round_id} 正被另一个进程处理") from exc
        self._lock_fd = fd
        return self

    def unlock(self):
        if self._lock_fd is not None:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            os.close(self._lock_fd)
            self._lock_fd = None

    def __enter__(self):
        return self.lock()

    def __exit__(self, *exc):
        self.unlock()


def new_round_id(now=None):
    now = datetime.now(timezone.utc) if now is None else now
    stamp = now.strftime("%Y%m%dt%H%M%S")
    suffix = os.urandom(4).hex()
    return f"round_{stamp}_{suffix}"


def list_staging(archive_root):
    staging = Path(archive_root) / "staging"
    if not staging.is_dir():
        return []
    result = []
    for child in sorted(staging.iterdir()):
        if child.is_dir() and (child / "journal.json").is_file():
            result.append(child.name)
    return result


def _write_journal(round_dir, journal):
    atomic_write(Path(round_dir) / "journal.json", canonical_bytes(journal))


def read_journal(round_dir):
    round_dir = Path(round_dir)
    journal = round_dir if round_dir.name == "journal.json" else round_dir / "journal.json"
    return read_json_file(journal)


def begin_round(archive_root, source_root, round_id=None):
    """创建或打开一个 staging 轮次；续做时复用既有快照与已抓取字节。"""
    archive_root = Path(archive_root).resolve()
    archive_root.mkdir(parents=True, exist_ok=True)
    round_id = round_id or new_round_id()
    progress = RoundInProgress(archive_root, round_id)
    progress.lock()
    try:
        root, _, blob_root = source_mod.resolve_source_root(source_root)
        if progress.journal_path.is_file():
            journal = read_journal(progress.journal_path)
            require(journal["state"] == "staging", "round_state",
                    "轮次已封存，不能在原目录续做；如需反映来源变化请开启新轮次")
            require(journal["source_root"] == str(root), "round_origin",
                    "续做不能更换来源数据根")
            return progress, journal, root, blob_root
        journal = {
            "format": "facet-archive-journal-1",
            "round_id": round_id, "state": "staging",
            "source_root": str(root),
            "source_instance": source_mod.source_instance(root),
            "created": datetime.now(timezone.utc).isoformat(),
            "snapshot": None, "observation": None,
            "total_blobs": 0, "copied_blobs": 0,
            "copied": [], "failures": [], "attempts": 0}
        _write_journal(progress.dir, journal)
        return progress, journal, root, blob_root
    except BaseException:
        progress.unlock()
        raise


def fix_snapshot(progress, journal, root):
    """阶段一：固定 SQL 快照。快照一经写定，本轮永远只读这一份。"""
    if journal["snapshot"] is None:
        source_mod.capture_snapshot(root, progress.snapshot_path)
        checksum, size = sha256_file(progress.snapshot_path)
        journal["snapshot"] = {"sha256": checksum, "size": size,
                               "path": "source.sqlite3"}
        _write_journal(progress.dir, journal)
    else:
        checksum, size = sha256_file(progress.snapshot_path)
        require(checksum == journal["snapshot"]["sha256"]
                and size == journal["snapshot"]["size"],
                "snapshot", "固定快照在续做前被改动，拒绝继续")
    return journal


def observe(progress, journal):
    """阶段二：从固定快照提取来源观察（图版、来源、审阅事实、分组）。"""
    if journal["observation"] is None:
        conn = source_mod.open_snapshot(progress.snapshot_path)
        try:
            observation = source_mod.scan_snapshot(conn)
        finally:
            conn.close()
        journal["observation"] = {
            "observation_hash": observation["observation_hash"],
            "plate_count": observation["plate_count"],
            "event_sequence": observation["event_sequence"]}
        journal["total_blobs"] = len(observation["blob_closure"])
        atomic_write(progress.dir / "observation.json", canonical_bytes(observation))
        _write_journal(progress.dir, journal)
    return journal


def _load_observation(progress):
    return read_json_file(progress.dir / "observation.json")


def _staging_blob_path(progress, checksum):
    return progress.blob_dir / checksum[:2] / checksum


def _channel_limit(name):
    return MAX_PNG_BYTES if name.endswith(".png") else MAX_RAW_BYTES


def copy_and_check(progress, journal, source_blob_root, stop_after=None,
                   fail_checksum=None):
    """阶段三：逐项抓取并核对内容闭包，逐项落 journal，可随时中断续做。

    stop_after / fail_checksum 仅用于演示与测试中断恢复；正常采集不传。
    """
    observation = _load_observation(progress)
    copied = set(journal["copied"])
    processed = 0
    journal["attempts"] += 1
    _write_journal(progress.dir, journal)
    for ref in observation["blob_closure"]:
        checksum, size = ref["sha256"], ref["size"]
        if checksum in copied:
            continue
        if fail_checksum == checksum:
            journal["failures"] = [f for f in journal["failures"]
                                   if f.get("sha256") != checksum]
            journal["failures"].append({"sha256": checksum, "code": "injected_failure",
                                        "message": "演示用注入失败，可重试"})
            _write_journal(progress.dir, journal)
            raise ArchiveError("injected_failure", "注入的抓取失败，轮次保持 staging")
        source_path = source_mod.expected_blob_path(source_blob_root, checksum)
        destination = _staging_blob_path(progress, checksum)
        _stream_copy(source_path, destination, checksum, size)
        copied.add(checksum)
        journal["copied"] = sorted(copied)
        journal["copied_blobs"] = len(copied)
        journal["failures"] = [f for f in journal["failures"]
                               if f.get("sha256") != checksum]
        _write_journal(progress.dir, journal)
        processed += 1
        if stop_after is not None and processed >= stop_after:
            raise ArchiveError("paused",
                               f"已处理 {processed} 个新内容块后按计划暂停，可续做")
    require(journal["copied_blobs"] == journal["total_blobs"], "closure",
            "内容闭包不完整，拒绝封存")
    journal["failures"] = []
    _write_journal(progress.dir, journal)
    return journal


def _stream_copy(source_path, destination, checksum, expected_size):
    require(source_path.is_file() and not source_path.is_symlink(),
            "blob_missing", f"来源内容缺失: {checksum}")
    import hashlib
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, scratch = _scratch(destination.parent)
    hasher = hashlib.sha256()
    length = 0
    try:
        with os.fdopen(fd, "wb") as out, open(source_path, "rb") as src:
            before = os.fstat(src.fileno())
            while True:
                block = src.read(CHUNK)
                if not block:
                    break
                length += len(block)
                hasher.update(block)
                out.write(block)
            after = os.fstat(src.fileno())
        require((before.st_size, before.st_mtime_ns, before.st_ctime_ns) ==
                (after.st_size, after.st_mtime_ns, after.st_ctime_ns),
                "blob_changed", "来源内容在抓取期间发生变化")
        require(hasher.hexdigest() == checksum, "blob_corrupt",
                f"来源内容摘要不匹配: {checksum}")
        require(length == expected_size, "blob_size",
                f"来源内容长度不匹配: {checksum}")
        os.replace(scratch, destination)
    finally:
        if os.path.exists(scratch):
            os.unlink(scratch)
    os.chmod(destination, 0o444)


def _scratch(directory):
    import tempfile
    return tempfile.mkstemp(prefix=".copy-", dir=str(directory))


def validate_closure(progress, journal):
    """封存前独立复核：重新解码每个引用，校验尺寸、PNG 结构与共享关系。"""
    observation = _load_observation(progress)
    by_checksum = {ref["sha256"]: ref for ref in observation["blob_closure"]}
    require(len(by_checksum) == journal["total_blobs"], "closure", "闭包索引重复")
    identity_rows = {}
    for plate in observation["plates"]:
        for name, descriptor in plate["passes"].items():
            checksum = descriptor["sha256"]
            path = _staging_blob_path(progress, checksum)
            actual, size = sha256_file(path, _channel_limit(name))
            require(actual == checksum and size == descriptor["size"],
                    "blob_corrupt", f"封存复核失败: {plate['plate_id']} {name}")
            if name.endswith(".png"):
                width, height, _ = pngcheck.decode_rgb(path.read_bytes())
                require((width, height) == (plate["width"], plate["height"]),
                        "png_extent", f"PNG 尺寸与图版不符: {plate['plate_id']} {name}")
        # 身份约束：独立曝光各占一条；同摘要只允许在内容层共享，绝不并条。
        key = plate["exposure_urn"]
        require(key not in identity_rows, "identity", "曝光 URN 出现重复图版")
        identity_rows[key] = plate["urn"]
    _assert_no_digest_merge(observation)
    return observation


def _assert_no_digest_merge(observation):
    """同画面（visual_hash 相同）的独立曝光必须各自成条。"""
    seen = {}
    for plate in observation["plates"]:
        bucket = seen.setdefault(plate["visual_hash"], [])
        bucket.append(plate["urn"])
    duplicates = {k: v for k, v in seen.items() if len(v) > 1}
    # 这是允许且必须保留的情形：多条独立图版共享画面摘要，但 URN 不同。
    for urns in duplicates.values():
        require(len(set(urns)) == len(urns), "identity", "同画面图版 URN 冲突")


def seal_round(progress, journal, render_browse=True):
    """阶段四：发布不可变轮次（清单 + 自包含浏览目录），再追加只增账本。"""
    require(journal["state"] == "staging", "round_state", "轮次已封存")
    require(journal["copied_blobs"] == journal["total_blobs"] and journal["total_blobs"] > 0,
            "closure", "内容闭包未完成，不能封存")
    observation = validate_closure(progress, journal)
    from .seal import publish_round
    return publish_round(progress, journal, observation, render_browse)


def run_harvest(archive_root, source_root, round_id=None, stop_after=None,
                fail_checksum=None, render_browse=True):
    """一键采集：固定快照 -> 观察 -> 抓取核对 -> 封存。中断后重入即续做。"""
    progress, journal, root, blob_root = begin_round(archive_root, source_root, round_id)
    try:
        journal = fix_snapshot(progress, journal, root)
        journal = observe(progress, journal)
        journal = copy_and_check(progress, journal, blob_root,
                                 stop_after=stop_after, fail_checksum=fail_checksum)
        return seal_round(progress, journal, render_browse)
    finally:
        progress.unlock()
