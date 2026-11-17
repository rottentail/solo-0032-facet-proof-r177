"""封存一个观察轮次并发布不可变产物。

产物全部位于 archive/rounds/<round_id>/：
- manifest.json 不可变收录清单（先算内容哈希，再落只读文件）；
- blobs/ 内容寻址闭包，相同字节在轮次内只有一份；
- browse/ 自包含离线浏览目录（图片以硬链接/复制落到轮次内，断网可看）。

封存成功后把轮次锚点追加到只增、带哈希链的 ledger.json。账本最后一次成功
追加即“封存完成”的唯一标记；此前任何中断都不会让半成品出现在 rounds/ 之外的
浏览入口。
"""
from datetime import datetime, timezone
from pathlib import Path
import os
import shutil

from .canonical import (atomic_write, canonical_bytes, digest,
                        read_json_file)
from .errors import require
from . import browse

LEDGER_NAME = "ledger.json"
ROUND_PREFIX = "round_"


def _ledger_path(archive_root):
    return Path(archive_root) / LEDGER_NAME


def load_ledger(archive_root):
    path = _ledger_path(archive_root)
    if not path.exists():
        return {"format": "facet-archive-ledger-1", "rounds": [], "head": None}
    return read_json_file(path)


def _materialize(target, source):
    """把字节落到轮次内：同文件系统优先硬链接（自包含且零额外占用），
    跨设备则复制。绝不使用符号链接，避免接收者缺少来源目录后断链。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.chmod(0o600) if not os.access(target, os.W_OK) else None
        target.unlink()
    try:
        os.link(source, target)
    except OSError:
        shutil.copyfile(source, target)
    os.chmod(target, 0o444)


def _build_manifest(journal, observation):
    plates = []
    for plate in observation["plates"]:
        plates.append({
            "urn": plate["urn"], "plate_id": plate["plate_id"],
            "exposure_urn": plate["exposure_urn"],
            "exposure": plate["exposure"], "state": plate["state"],
            "created": plate["created"], "seal": plate["seal"],
            "request": plate["request"], "width": plate["width"],
            "height": plate["height"], "objects": plate["objects"],
            "statistics": plate["statistics"], "visual_hash": plate["visual_hash"],
            "seal_hash": plate["seal_hash"], "passes": plate["passes"],
            "review_facts": plate["review_facts"]})
    plates.sort(key=lambda item: item["urn"])
    manifest = {
        "format": "facet-archive-manifest-1",
        "round_id": journal["round_id"],
        "source_root": journal["source_root"],
        "source_instance": journal["source_instance"],
        "created": journal["created"],
        "sealed": datetime.now(timezone.utc).isoformat(),
        "snapshot": journal["snapshot"],
        "observation_hash": journal["observation"]["observation_hash"],
        "event_sequence": journal["observation"]["event_sequence"],
        "groups": observation["groups"],
        "plates": plates,
        "blob_closure": observation["blob_closure"]}
    manifest["manifest_hash"] = digest(
        {k: v for k, v in manifest.items() if k != "manifest_hash"})
    return manifest


def publish_round(progress, journal, observation, render_browse=True):
    archive_root = progress.archive_root
    rounds_root = archive_root / "rounds"
    target_dir = rounds_root / progress.round_id
    require(not target_dir.exists(), "round_exists",
            "同名轮次目录已存在，拒绝覆盖不可变产物")
    staging_round = progress.dir
    manifest = _build_manifest(journal, observation)

    # 先在 staging 内组好完整轮次树，再整体原子改名到 rounds/。
    staged_tree = staging_round / "published"
    if staged_tree.exists():
        shutil.rmtree(staged_tree)
    (staged_tree / "blobs").mkdir(parents=True)
    for ref in observation["blob_closure"]:
        src = progress.blob_dir / ref["sha256"][:2] / ref["sha256"]
        dst = staged_tree / "blobs" / ref["sha256"][:2] / ref["sha256"]
        _materialize(dst, src)
    atomic_write(staged_tree / "manifest.json", canonical_bytes(manifest))
    if render_browse:
        browse.render_site(staged_tree / "browse", manifest,
                           staged_tree / "blobs")

    rounds_root.mkdir(parents=True, exist_ok=True)
    os.replace(staged_tree, target_dir)
    _make_readonly(target_dir)

    entry = _append_ledger(archive_root, manifest)
    journal["state"] = "sealed"
    journal["manifest_hash"] = manifest["manifest_hash"]
    journal["sealed"] = manifest["sealed"]
    atomic_write(staging_round / "journal.json", canonical_bytes(journal))
    _make_readonly(staging_round / "journal.json")
    return {"round_id": manifest["round_id"],
            "manifest_hash": manifest["manifest_hash"],
            "round_dir": str(target_dir), "ledger_entry": entry,
            "plates": len(manifest["plates"]),
            "blobs": len(manifest["blob_closure"])}


def _make_readonly(path):
    path = Path(path)
    if path.is_file():
        try:
            os.chmod(path, 0o444)
        except OSError:
            pass
        return
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                os.chmod(os.path.join(root, name), 0o444)
            except OSError:
                pass


def _append_ledger(archive_root, manifest):
    path = _ledger_path(archive_root)
    ledger = load_ledger(archive_root)
    require(ledger["format"] == "facet-archive-ledger-1", "ledger", "账本格式不受支持")
    require(all(item["round_id"] != manifest["round_id"] for item in ledger["rounds"]),
            "ledger", "轮次已在账本中，拒绝重复追加")
    prev_head = ledger["head"]
    entry = {
        "round_id": manifest["round_id"],
        "manifest_hash": manifest["manifest_hash"],
        "observation_hash": manifest["observation_hash"],
        "snapshot_sha256": manifest["snapshot"]["sha256"],
        "sealed": manifest["sealed"],
        "plate_count": len(manifest["plates"]),
        "blob_count": len(manifest["blob_closure"]),
        "prev_head": prev_head}
    entry["entry_hash"] = digest(
        {k: v for k, v in entry.items() if k != "entry_hash"})
    ledger["rounds"].append(entry)
    ledger["head"] = entry["entry_hash"]
    atomic_write(path, canonical_bytes(ledger))
    os.chmod(path, 0o444)
    return entry


def load_manifest(round_dir):
    return read_json_file(Path(round_dir) / "manifest.json")


def list_rounds(archive_root):
    ledger = load_ledger(archive_root)
    return ledger["rounds"]
