"""资料库编排：一轮采集的完整事务与跨轮差异、根入口维护。"""
from pathlib import Path

from . import browse
from . import differ
from . import harvester
from . import seal as seal_mod


def archive_round(archive_root, source_root, round_id=None,
                  stop_after=None, fail_checksum=None, render_browse=True):
    """执行一轮观察并封存；成功后自动与上一轮生成差异记录与根入口。"""
    result = harvester.run_harvest(
        archive_root, source_root, round_id=round_id,
        stop_after=stop_after, fail_checksum=fail_checksum,
        render_browse=render_browse)
    archive_root = Path(archive_root).resolve()
    ledger = seal_mod.load_ledger(archive_root)
    diff_document = None
    if len(ledger["rounds"]) >= 2:
        prev_entry = ledger["rounds"][-2]
        curr_entry = ledger["rounds"][-1]
        previous = seal_mod.load_manifest(
            archive_root / "rounds" / prev_entry["round_id"])
        current = seal_mod.load_manifest(
            archive_root / "rounds" / curr_entry["round_id"])
        diff_document, _ = differ.write_diff(archive_root, previous, current)
        result["diff_hash"] = diff_document["diff_hash"]
        result["diff_counts"] = diff_document["counts"]
    browse.render_archive_root(archive_root, ledger, ledger["rounds"])
    result["ledger_head"] = ledger["head"]
    return result


def status(archive_root):
    archive_root = Path(archive_root).resolve()
    ledger = seal_mod.load_ledger(archive_root)
    staging = []
    for round_id in harvester.list_staging(archive_root):
        journal = harvester.read_journal(
            archive_root / "staging" / round_id)
        staging.append({
            "round_id": round_id, "state": journal["state"],
            "total_blobs": journal["total_blobs"],
            "copied_blobs": journal["copied_blobs"],
            "attempts": journal["attempts"],
            "resumable": journal["state"] == "staging"})
    return {"archive_root": str(archive_root),
            "sealed_rounds": ledger["rounds"],
            "ledger_head": ledger["head"], "staging": staging}
