"""对渲染服务数据根做一次固定、只读的观察。

观察边界参考服务自身的备份语义：先把 SQLite 复制成一份固定快照，之后只打开
这份快照（immutable 只读），再按快照里的引用去核对内容寻址字节。抓取过程中
来源库继续变化也不会污染本轮观察——续做始终回到同一快照文件。

本模块不导入 facetproof，直接面对稳定的表结构与内容寻址目录布局。
"""
from pathlib import Path
import os
import sqlite3

from .canonical import digest, is_hex64
from .errors import ArchiveError, require

PLATE_CHANNELS = ("color.rgb", "depth.f32", "object.u32",
                  "color.png", "depth.png", "object.png")
PREVIEW_CHANNELS = ("color.png", "depth.png", "object.png")
SCHEMA_TABLES = ("objects", "exposures", "plates", "decisions")


def resolve_source_root(source_root):
    root = Path(source_root).resolve()
    require(root.is_dir() and not root.is_symlink(), "source_root",
            "来源数据根必须是真实存在的目录")
    db_path = root / "facetproof.sqlite3"
    blob_root = root / "plates"
    require(db_path.is_file(), "source_root", "来源数据根缺少 facetproof.sqlite3")
    require(blob_root.is_dir(), "source_root", "来源数据根缺少 plates 内容目录")
    return root, db_path, blob_root


def source_instance(root):
    """数据根实例标识：优先采用服务维护模块写入的属主标记。"""
    marker = root / ".facet-maintenance-owner"
    if marker.is_file() and not marker.is_symlink():
        try:
            value = marker.read_text().strip()
            if len(value) == 32 and all(c in "0123456789abcdef" for c in value):
                return value
        except OSError:
            pass
    # 没有属主标记时退化为路径的稳定摘要，仍能在同一机器上区分不同数据根。
    return digest({"facet_source_path": str(root)})[:32]


def capture_snapshot(source_root, snapshot_path):
    """把活动 SQLite 一致性备份到 snapshot_path。已存在则要求复用，绝不重抓。"""
    root, db_path, _ = resolve_source_root(source_root)
    snapshot_path = Path(snapshot_path)
    if snapshot_path.exists():
        require(snapshot_path.is_file() and not snapshot_path.is_symlink(),
                "snapshot", "既有快照路径不是普通文件")
        return snapshot_path
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    fd, scratch = _named_temp(snapshot_path.parent)
    try:
        with os.fdopen(fd, "wb"):
            pass
        source = sqlite3.connect(str(db_path))
        try:
            target = sqlite3.connect(str(scratch))
            try:
                source.backup(target)
                ok = target.execute("PRAGMA integrity_check").fetchone()[0]
                require(ok == "ok", "snapshot", "来源数据库完整性检查失败")
            finally:
                target.close()
        finally:
            source.close()
        os.replace(scratch, snapshot_path)
    finally:
        if os.path.exists(scratch):
            os.unlink(scratch)
    os.chmod(snapshot_path, 0o444)
    return snapshot_path


def _named_temp(directory):
    import tempfile
    return tempfile.mkstemp(prefix=".snapshot-", suffix=".sqlite3", dir=str(directory))


def open_snapshot(snapshot_path):
    """immutable=1 打开固定快照：不写 WAL/SHM，也看不到快照之后的新事务。"""
    snapshot_path = Path(snapshot_path).resolve()
    require(snapshot_path.is_file(), "snapshot", "固定快照不存在，无法续做")
    conn = sqlite3.connect(snapshot_path.as_uri() + "?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    present = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    missing = [name for name in SCHEMA_TABLES if name not in present]
    require(not missing, "snapshot_schema", f"快照缺少表: {', '.join(missing)}")
    require(conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok",
            "snapshot", "固定快照完整性检查失败")
    return conn


def _loads(raw, what):
    import json
    try:
        return json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ArchiveError("snapshot_json", f"{what} 负载不是合法 JSON") from exc


def _part_slug(assembly_id):
    return "p_" + digest({"assembly": assembly_id})[:12]


def _lens_slug(lens_hash):
    require(is_hex64(lens_hash), "snapshot", "镜头摘要必须是 sha256")
    return "l_" + lens_hash[:12]


def scan_snapshot(conn):
    """把一次固定快照整理成来源观察。所有目录身份均来自稳定 URN，而非图片摘要。"""
    objects_conn = conn
    objects_cache = {}

    def get_object(object_id):
        if object_id not in objects_cache:
            row = objects_conn.execute(
                "SELECT id,kind,state,revision,payload,created,updated FROM objects WHERE id=?",
                (object_id,)).fetchone()
            require(row is not None, "snapshot", f"快照缺少对象 {object_id}")
            objects_cache[object_id] = {
                "id": row["id"], "kind": row["kind"], "state": row["state"],
                "revision": row["revision"], "payload": _loads(row["payload"], "object"),
                "created": row["created"], "updated": row["updated"]}
        return objects_cache[object_id]

    decisions_by_plate = {}
    for row in conn.execute("SELECT id,plate_id,state,payload,created FROM decisions ORDER BY created,id"):
        decisions_by_plate.setdefault(row["plate_id"], []).append({
            "decision_id": row["id"], "state": row["state"],
            "payload": _loads(row["payload"], "decision"), "created": row["created"]})

    exposures = {}
    for row in conn.execute(
            "SELECT id,seal_id,state,request,dedup_key,attempt,created,updated FROM exposures ORDER BY created,id"):
        exposures[row["id"]] = {
            "exposure_id": row["id"], "seal_id": row["seal_id"],
            "state": row["state"], "request": _loads(row["request"], "exposure"),
            "dedup_key": row["dedup_key"], "attempt": row["attempt"],
            "created": row["created"], "updated": row["updated"]}

    plates = []
    blob_sources = {}
    for row in conn.execute("SELECT id,exposure_id,state,payload,created FROM plates ORDER BY created,id"):
        payload = _loads(row["payload"], "plate")
        require(payload.get("format") == "facet-plate-1", "snapshot",
                f"图版 {row['id']} 格式不受支持")
        passes = payload.get("passes")
        require(isinstance(passes, dict) and set(passes) == set(PLATE_CHANNELS),
                "snapshot", f"图版 {row['id']} 通道闭包不完整")
        exposure = exposures.get(row["exposure_id"])
        require(exposure is not None, "snapshot",
                f"图版 {row['id']} 找不到对应曝光")
        seal = get_object(payload["seal_id"])
        seal_payload = seal["payload"]
        assembly = get_object(seal_payload["assembly_id"])
        assembly_payload = assembly["payload"]
        closure = seal_payload.get("closure", [])
        placements = []
        for item in closure:
            placement = item["placement"]
            mesh_payload = item["mesh"]
            pigment_payload = item["pigment"]
            placements.append({
                "key": placement["key"], "visible": placement["visible"],
                "mesh_id": placement["mesh_id"], "mesh_name": mesh_payload.get("name"),
                "pigment_id": placement["pigment_id"],
                "pigment_name": pigment_payload.get("name"),
                "mesh_revision": item["mesh_revision"],
                "pigment_revision": item["pigment_revision"]})
        lens = seal_payload["lens"]
        record = {
            "urn": f"urn:facet:plate:{row['id']}",
            "plate_id": row["id"],
            "exposure_urn": f"urn:facet:exposure:{row['exposure_id']}",
            "exposure": exposure,
            "state": row["state"],
            "created": row["created"],
            "seal": {
                "urn": f"urn:facet:seal:{seal['id']}", "seal_id": seal["id"],
                "content_hash": seal_payload["content_hash"],
                "assembly_id": seal_payload["assembly_id"],
                "assembly_name": assembly_payload.get("name"),
                "part_slug": _part_slug(seal_payload["assembly_id"]),
                "lens": lens, "lens_hash": payload["lens_hash"],
                "lens_slug": _lens_slug(payload["lens_hash"]),
                "placements": placements},
            "request": payload["request"],
            "width": payload["width"], "height": payload["height"],
            "objects": payload["objects"],
            "statistics": payload["statistics"],
            "visual_hash": payload["visual_hash"],
            "seal_hash": payload["seal_hash"],
            "passes": {},
            "review_facts": decisions_by_plate.get(row["id"], [])}
        for name in PLATE_CHANNELS:
            descriptor = passes[name]
            require(is_hex64(descriptor.get("sha256")) and
                    isinstance(descriptor.get("size"), int),
                    "snapshot", f"图版 {row['id']} 通道 {name} 引用非法")
            record["passes"][name] = dict(descriptor)
            source = {"plate_urn": record["urn"], "plate_id": row["id"], "channel": name}
            blob_sources.setdefault(descriptor["sha256"], {
                "sha256": descriptor["sha256"], "size": descriptor["size"],
                "sources": []})
            blob_sources[descriptor["sha256"]]["sources"].append(source)
        plates.append(record)

    for ref in blob_sources.values():
        ref["sources"].sort(key=lambda item: (item["plate_id"], item["channel"]))

    groups = _build_groups(plates)
    observation = {
        "format": "facet-archive-observation-1",
        "plate_count": len(plates),
        "plates": plates,
        "blob_closure": [blob_sources[key] for key in sorted(blob_sources)],
        "groups": groups,
        "event_sequence": conn.execute("SELECT COALESCE(MAX(seq),0) FROM events").fetchone()[0]}
    observation["observation_hash"] = digest(
        {k: v for k, v in observation.items() if k != "observation_hash"})
    return observation


def _build_groups(plates):
    """按部件（装配）与镜头建立稳定分组，显示名与目录 slug 分离。"""
    parts, lenses = {}, {}
    for record in plates:
        seal = record["seal"]
        part = parts.setdefault(seal["part_slug"], {
            "part_slug": seal["part_slug"], "assembly_id": seal["assembly_id"],
            "name": seal["assembly_name"], "plate_urns": []})
        part["plate_urns"].append(record["urn"])
        lens = lenses.setdefault(seal["lens_slug"], {
            "lens_slug": seal["lens_slug"], "lens_hash": seal["lens_hash"],
            "projection": seal["lens"]["projection"],
            "eye": seal["lens"]["eye"], "target": seal["lens"]["target"],
            "fov": seal["lens"].get("fov"), "span": seal["lens"].get("span"),
            "plate_urns": []})
        lens["plate_urns"].append(record["urn"])
    for group in (*parts.values(), *lenses.values()):
        group["plate_urns"].sort()
    return {"parts": sorted(parts.values(), key=lambda g: g["part_slug"]),
            "lenses": sorted(lenses.values(), key=lambda g: g["lens_slug"])}


def expected_blob_path(blob_root, checksum):
    return Path(blob_root) / checksum[:2] / checksum
