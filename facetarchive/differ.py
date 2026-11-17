"""相邻观察轮次之间的差异记录。

比较键始终是图版/曝光的稳定 URN，绝不用图片摘要当身份——因此“同画面的独立
曝光”会被显式标成 added_independent_exposure，而不会被误判为“无变化”合并掉。
差异文件本身内容寻址、只增写入 rounds 之外的 diffs/，并在账本锚点中登记。
"""
from pathlib import Path

from .canonical import atomic_write, canonical_bytes, digest
from .errors import require
from . import browse


def _index(manifest):
    return {p["urn"]: p for p in manifest["plates"]}


def _exposure_index(manifest):
    # 同一曝光 URN 至多一张图版（由采集器约束）；这里再防一次重复。
    result = {}
    for plate in manifest["plates"]:
        require(plate["exposure_urn"] not in result, "identity",
                "清单内同一曝光出现多张图版")
        result[plate["exposure_urn"]] = plate
    return result


def diff_manifests(previous, current):
    old, new = _index(previous), _index(current)
    old_exp, new_exp = _exposure_index(previous), _exposure_index(current)
    old_visual = {}
    for plate in old.values():
        old_visual.setdefault(plate["visual_hash"], []).append(plate["urn"])

    entries = []
    for urn in sorted(new):
        plate = new[urn]
        if urn not in old:
            # 关键判定：新 URN，但画面摘要在旧轮已存在 => 独立曝光，不得并条。
            same_picture = sorted(old_visual.get(plate["visual_hash"], []))
            if same_picture:
                entries.append({
                    "kind": "added_independent_exposure", "plate_urn": urn,
                    "plate_id": plate["plate_id"],
                    "exposure_urn": plate["exposure_urn"],
                    "visual_hash": plate["visual_hash"],
                    "same_picture_existing_urns": same_picture,
                    "detail": "相同画面的独立曝光：URN 不同，按独立图版收录，不按摘要合并"})
            else:
                entries.append({
                    "kind": "added", "plate_urn": urn, "plate_id": plate["plate_id"],
                    "exposure_urn": plate["exposure_urn"],
                    "visual_hash": plate["visual_hash"]})
            continue
        before = old[urn]
        changes = []
        if before["visual_hash"] != plate["visual_hash"]:
            changes.append({"field": "visual_hash",
                            "before": before["visual_hash"], "after": plate["visual_hash"]})
        before_blob = {k: v["sha256"] for k, v in before["passes"].items()}
        after_blob = {k: v["sha256"] for k, v in plate["passes"].items()}
        for channel in sorted(set(before_blob) | set(after_blob)):
            if before_blob.get(channel) != after_blob.get(channel):
                changes.append({"field": f"pass.{channel}",
                                "before": before_blob.get(channel),
                                "after": after_blob.get(channel)})
        if before["state"] != plate["state"]:
            changes.append({"field": "state", "before": before["state"],
                            "after": plate["state"]})
            if plate["state"] == "quarantined":
                entries.append({"kind": "quarantined", "plate_urn": urn,
                                "plate_id": plate["plate_id"], "changes": changes})
                continue
            if plate["state"] == "purged":
                entries.append({"kind": "purged", "plate_urn": urn,
                                "plate_id": plate["plate_id"],
                                "detail": "逻辑清退；内容寻址字节仍保留在闭包内",
                                "changes": changes})
                continue
        before_review = _review_signature(before)
        after_review = _review_signature(plate)
        if before_review != after_review:
            changes.append({"field": "review_facts",
                            "before": before_review, "after": after_review})
        if changes:
            entries.append({"kind": "changed", "plate_urn": urn,
                            "plate_id": plate["plate_id"], "changes": changes})

    for urn in sorted(old):
        if urn not in new:
            entries.append({"kind": "removed_from_catalog", "plate_urn": urn,
                            "plate_id": old[urn]["plate_id"],
                            "detail": "该图版不再出现在新快照的可收录集合中"})

    counts = {}
    for entry in entries:
        counts[entry["kind"]] = counts.get(entry["kind"], 0) + 1
    return entries, counts


def _review_signature(plate):
    return [{
        "decision_id": f["decision_id"], "state": f["state"],
        "visual_hash": f["payload"].get("visual_hash")}
        for f in plate["review_facts"]]


def write_diff(archive_root, previous, current):
    entries, counts = diff_manifests(previous, current)
    document = {
        "format": "facet-archive-diff-1",
        "from_round": previous["round_id"], "to_round": current["round_id"],
        "from_manifest_hash": previous["manifest_hash"],
        "to_manifest_hash": current["manifest_hash"],
        "identity_rule": "urn: 以图版/曝光 URN 为身份；相同画面摘要不构成合并依据",
        "counts": counts, "entries": entries}
    document["diff_hash"] = digest(
        {k: v for k, v in document.items() if k != "diff_hash"})
    diff_dir = Path(archive_root) / "diffs"
    diff_dir.mkdir(parents=True, exist_ok=True)
    name = f"{previous['round_id']}__to__{current['round_id']}.json"
    path = diff_dir / name
    require(not path.exists(), "diff_exists", "差异记录已存在，拒绝覆盖")
    atomic_write(path, canonical_bytes(document))
    os_chmod_readonly(path)
    _write_diff_html(diff_dir / (name[:-5] + ".html"), document)
    return document, path


def os_chmod_readonly(path):
    import os
    try:
        os.chmod(path, 0o444)
    except OSError:
        pass


def _write_diff_html(path, document):
    rows = []
    badge = {
        "added": "ok", "added_independent_exposure": "ok",
        "quarantined": "warn", "purged": "warn",
        "removed_from_catalog": "warn", "changed": ""}
    for entry in document["entries"]:
        detail = entry.get("detail", "")
        if entry["kind"] == "changed":
            detail = "；".join(
                f"{c['field']}: {str(c['before'])[:16]}… → {str(c['after'])[:16]}…"
                for c in entry["changes"])
        rows.append(
            f"<tr><td><span class='badge {badge.get(entry['kind'],'')}'>"
            f"{entry['kind']}</span></td>"
            f"<td><code>{entry.get('plate_id','')}</code></td>"
            f"<td class='hash'>{entry.get('plate_urn','')}</td>"
            f"<td>{detail}</td></tr>")
    html = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>差异 {document['from_round']} → {document['to_round']}</title>
<style>{browse.STYLE}</style></head><body>
<header><h1>观察轮次差异记录</h1>
<div class="meta">{document['from_round']} → {document['to_round']}</div></header><main>
<div class="section"><h2>身份规则</h2><p>{document['identity_rule']}</p>
<p>diff_sha256 <span class='hash'>{document['diff_hash']}</span></p></div>
<div class="section"><h2>变化计数</h2><table><tr><th>类型</th><th>数量</th></tr>
{''.join(f"<tr><td>{k}</td><td>{v}</td></tr>" for k,v in sorted(document['counts'].items()))}
</table></div>
<div class="section"><h2>明细（{len(document['entries'])} 条）</h2>
<table><tr><th>类型</th><th>图版</th><th>URN</th><th>说明</th></tr>
{''.join(rows) or '<tr><td colspan=4>无差异</td></tr>'}</table></div>
</main></body></html>"""
    path.write_text(html, encoding="utf-8")
    os_chmod_readonly(path)
