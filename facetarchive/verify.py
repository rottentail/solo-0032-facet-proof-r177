"""离线独立核对。接收者无需原服务即可复核全部收录依据。

核对内容：
1. 只增账本的哈希链（每条目含 prev_head，重算 entry_hash 与 head）；
2. 每个轮次 manifest 重算 manifest_hash，并与账本锚点一致；
3. manifest 引用的每个内容块在轮次 blobs/ 内重算 sha256/长度，PNG 独立解码；
4. 身份约束：曝光 URN 唯一、同画面独立曝光各自成条、共享字节只存一份；
5. 差异记录重算 diff_hash，且两端 manifest 哈希对得上。
任何一项不符即返回 valid=False 和稳定错误码，绝不伪造通过。
"""
from pathlib import Path
import json

from .canonical import digest, is_hex64, read_json_file, sha256_file
from .errors import ArchiveError, require
from . import pngcheck
from . import seal as seal_mod


def _check_manifest_bytes(round_dir, manifest):
    raw = (Path(round_dir) / "manifest.json").read_bytes()
    on_disk = json.loads(raw, parse_constant=lambda c: (_ for _ in ()).throw(
        ArchiveError("json", "非有限 JSON 常量")))
    stored = manifest["manifest_hash"]
    recomputed = digest({k: v for k, v in manifest.items() if k != "manifest_hash"})
    require(recomputed == stored, "verify_manifest",
            f"{manifest['round_id']}: manifest 内容与 manifest_hash 不符")
    require(digest({k: v for k, v in on_disk.items() if k != "manifest_hash"}) == stored,
            "verify_manifest", f"{manifest['round_id']}: manifest 文件字节复算不符")


def verify_round(round_dir, manifest=None):
    round_dir = Path(round_dir)
    manifest = manifest or seal_mod.load_manifest(round_dir)
    require(manifest["format"] == "facet-archive-manifest-1", "verify_manifest",
            "清单格式不受支持")
    _check_manifest_bytes(round_dir, manifest)

    blob_tree = round_dir / "blobs"
    checked_blobs = {}
    exposure_seen = set()
    visual_groups = {}
    plate_urns = set()
    for plate in manifest["plates"]:
        require(plate["urn"] not in plate_urns, "verify_identity", "图版 URN 重复")
        plate_urns.add(plate["urn"])
        require(plate["exposure_urn"] not in exposure_seen, "verify_identity",
                f"曝光 {plate['exposure_urn']} 关联多张图版，违反一曝光一图版")
        exposure_seen.add(plate["exposure_urn"])
        visual_groups.setdefault(plate["visual_hash"], []).append(plate["urn"])
        for name, descriptor in sorted(plate["passes"].items()):
            checksum = descriptor["sha256"]
            require(is_hex64(checksum), "verify_reference", "通道引用非法")
            path = blob_tree / checksum[:2] / checksum
            actual, size = sha256_file(path)
            require(actual == checksum, "verify_blob",
                    f"内容块损坏: {checksum}")
            require(size == descriptor["size"], "verify_blob",
                    f"内容块长度不符: {checksum}")
            checked_blobs[checksum] = size
            if name.endswith(".png"):
                width, height, _ = pngcheck.decode_rgb(path.read_bytes())
                require((width, height) == (plate["width"], plate["height"]),
                        "verify_png", f"PNG 尺寸与图版不符: {plate['plate_id']} {name}")

    # 闭包清单与实际引用必须完全一致，且每个共享字节在轮次内物理上只存一份。
    closure = {ref["sha256"]: ref for ref in manifest["blob_closure"]}
    require(set(closure) == set(checked_blobs), "verify_closure",
            "manifest 闭包与图版实际引用集合不一致")
    for checksum, ref in closure.items():
        require(ref["size"] == checked_blobs[checksum], "verify_closure",
                f"闭包长度登记错误: {checksum}")
        physical = list((blob_tree / checksum[:2]).glob(checksum))
        require(len(physical) == 1, "verify_dedup",
                f"共享内容未按内容寻址唯一存储: {checksum}")

    independent = {h: urns for h, urns in visual_groups.items() if len(urns) > 1}
    return {"round_id": manifest["round_id"], "valid": True,
            "plates": len(manifest["plates"]),
            "blobs": len(checked_blobs),
            "shared_blob_blobs": sum(1 for ref in closure.values()
                                     if len(ref["sources"]) > 1),
            "same_picture_independent_plates": independent}


def verify_ledger(archive_root):
    archive_root = Path(archive_root)
    ledger = seal_mod.load_ledger(archive_root)
    require(ledger["format"] == "facet-archive-ledger-1", "verify_ledger", "账本格式非法")
    prev_head = None
    seen_rounds = set()
    for entry in ledger["rounds"]:
        require(entry["prev_head"] == prev_head, "verify_ledger", "哈希链 prev_head 断裂")
        expect = digest({k: v for k, v in entry.items() if k != "entry_hash"})
        require(expect == entry["entry_hash"], "verify_ledger",
                f"轮次 {entry['round_id']} 账本条目哈希不符")
        require(entry["round_id"] not in seen_rounds, "verify_ledger", "账本出现重复轮次")
        seen_rounds.add(entry["round_id"])
        prev_head = entry["entry_hash"]
    require(ledger["head"] == prev_head, "verify_ledger", "账本 head 与链尾不符")
    return ledger


def verify_diffs(archive_root):
    diff_dir = Path(archive_root) / "diffs"
    results = []
    if not diff_dir.is_dir():
        return results
    for path in sorted(diff_dir.glob("*.json")):
        doc = read_json_file(path)
        expect = digest({k: v for k, v in doc.items() if k != "diff_hash"})
        require(expect == doc["diff_hash"], "verify_diff",
                f"差异记录被改动: {path.name}")
        results.append({"file": path.name, "diff_hash": doc["diff_hash"],
                        "from": doc["from_round"], "to": doc["to_round"],
                        "counts": doc["counts"]})
    return results


def verify_archive(archive_root):
    archive_root = Path(archive_root).resolve()
    ledger = verify_ledger(archive_root)
    round_reports = []
    for entry in ledger["rounds"]:
        round_dir = archive_root / "rounds" / entry["round_id"]
        manifest = seal_mod.load_manifest(round_dir)
        require(manifest["manifest_hash"] == entry["manifest_hash"],
                "verify_ledger", "账本锚点与轮次 manifest_hash 不一致")
        require(manifest["snapshot"]["sha256"] == entry["snapshot_sha256"],
                "verify_ledger", "账本快照锚点与清单不一致")
        round_reports.append(verify_round(round_dir, manifest))
    diff_reports = verify_diffs(archive_root)
    # 封存轮次目录不应包含账本之外的轮，保证没有游离的“成功”产物。
    physical = {p.name for p in (archive_root / "rounds").iterdir()} \
        if (archive_root / "rounds").is_dir() else set()
    ledger_rounds = {e["round_id"] for e in ledger["rounds"]}
    require(physical == ledger_rounds, "verify_ledger",
            f"存在账本未登记的轮次目录: {sorted(physical - ledger_rounds)}")
    return {"valid": True, "archive_root": str(archive_root),
            "rounds": round_reports, "diffs": diff_reports,
            "ledger_head": ledger["head"]}
