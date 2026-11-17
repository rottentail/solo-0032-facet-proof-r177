#!/usr/bin/env python3
"""用真实 Facet Proof 渲染服务生成离线资料库的来源素材。

产出一个服务数据根，包含两个部件（装配）、两类镜头，以及多组曝光：
- 同一次冻结输入下的不同曝光（不同幂等键、不同尺寸）；
- 刻意构造的“同画面独立曝光”：参数完全相同、键不同，渲染字节逐字节一致，
  用于证明资料库按 URN 收录、绝不按图片摘要并条；
- 同部件多曝光天然共享冻结闭包，且同画面曝光在内容寻址层共享通道字节。

脚本只负责“制造来源”，不参与收录；资料库逻辑全部在 facetarchive 包内。
建议以 `python3 tools/generate_materials.py ...` 运行。
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from facetproof.application import Application  # noqa: E402
from facetproof.routing import route_request, PREFIX  # noqa: E402


def call(app, method, route, data=None):
    return route_request(app, method, PREFIX + route, data)


def make_mesh(app, name, vertices, faces, pigment_name, color, two_sided=True):
    mesh = call(app, "POST", "meshes",
                {"name": name, "vertices": vertices, "faces": faces})
    pigment = call(app, "POST", "pigments",
                   {"name": pigment_name, "color": color, "two_sided": two_sided})
    return mesh, pigment


def wing_geometry():
    vertices = [[-1, -1, 0], [1, -1, 0], [1, 1, 0], [-1, 1, 0], [0, 0, 1.2]]
    faces = [[0, 1, 4], [1, 2, 4], [2, 3, 4], [3, 0, 4], [0, 3, 2], [0, 2, 1]]
    return vertices, faces


def block_geometry():
    v = [[-0.8, -0.8, -0.8], [0.8, -0.8, -0.8], [0.8, 0.8, -0.8], [-0.8, 0.8, -0.8],
         [-0.8, -0.8, 0.8], [0.8, -0.8, 0.8], [0.8, 0.8, 0.8], [-0.8, 0.8, 0.8]]
    faces = [[0, 1, 2], [0, 2, 3], [4, 6, 5], [4, 7, 6],
             [0, 4, 5], [0, 5, 1], [2, 6, 7], [2, 7, 3],
             [1, 5, 6], [1, 6, 2], [0, 3, 7], [0, 7, 4]]
    return v, faces


def build_part(app, name, lens, geometry, pigment):
    mesh, pigment = make_mesh(app, geometry["mesh_name"], geometry["vertices"],
                              geometry["faces"], pigment["name"], pigment["color"])
    assembly = call(app, "POST", "assemblies", {"name": name, "lens": lens})
    call(app, "PUT", "assemblies/" + assembly["id"],
         {"revision": 1, "placements": [
             {"key": geometry["placement"], "mesh_id": mesh["id"],
              "pigment_id": pigment["id"], "transform": geometry["transform"]}]})
    seal = call(app, "POST", "assemblies/" + assembly["id"] + "/seal", {"revision": 2})
    return assembly, seal


def submit_and_render(app, seal_id, key, width, height, samples=1):
    exposure = call(app, "POST", "seals/" + seal_id + "/exposures",
                    {"key": key, "width": width, "height": height, "samples": samples})
    result = call(app, "POST", "worker/step")
    assert result["state"] == "succeeded", result
    plate = result["plate"]
    return exposure, plate


def main():
    parser = argparse.ArgumentParser(description="生成 Facet Proof 来源素材")
    parser.add_argument("--source", required=True, help="输出的服务数据根（空目录或不存在）")
    args = parser.parse_args()
    source_root = Path(args.source).resolve()
    if source_root.exists():
        assert not any(source_root.iterdir()), "来源目录必须为空，避免覆盖既有数据"
    source_root.mkdir(parents=True, exist_ok=True)

    app = Application(str(source_root))
    wing = {"mesh_name": "斜翼网格", "placement": "wing",
            "vertices": wing_geometry()[0], "faces": wing_geometry()[1],
            "transform": {"euler": [0, 20, 0]}}
    block = {"mesh_name": "盒体网格", "placement": "block",
             "vertices": block_geometry()[0], "faces": block_geometry()[1],
             "transform": {"euler": [10, 0, 8]}}

    # 两个部件，分别使用透视与正交镜头，形成不同镜头分组。
    assembly_a, seal_a = build_part(
        app, "斜翼结构说明图",
        {"eye": [3, 2, 4], "projection": "perspective", "fov": 45},
        wing, {"name": "釉青", "color": [0.12, 0.62, 0.61]})
    assembly_b, seal_b = build_part(
        app, "盒体剖切示意",
        {"eye": [2.5, 2, 3], "projection": "orthographic", "span": 4},
        block, {"name": "赭红", "color": [0.72, 0.31, 0.2]})

    created = {"parts": [], "plates": [], "same_picture_pairs": []}
    plans = [
        (seal_a, "wing_persp_64", 64, 64),
        (seal_a, "wing_persp_96", 96, 96),
        # 同画面独立曝光：与 wing_persp_64 参数一致、键不同，字节应完全相同。
        (seal_a, "wing_persp_64_duplicate", 64, 64),
        (seal_b, "block_ortho_64", 64, 64),
        (seal_b, "block_ortho_80", 80, 80),
        # 第二个部件也放一对同画面独立曝光。
        (seal_b, "block_ortho_64_duplicate", 64, 64),
    ]
    plates_by_key = {}
    for seal, key, width, height in plans:
        exposure, plate = submit_and_render(app, seal["id"], key, width, height)
        plates_by_key[key] = (exposure, plate)
        created["plates"].append({
            "key": key, "exposure_id": exposure["id"], "plate_id": plate["id"],
            "seal_id": seal["id"], "width": width, "height": height,
            "visual_hash": plate["payload"]["visual_hash"]})

    for first, second in (("wing_persp_64", "wing_persp_64_duplicate"),
                          ("block_ortho_64", "block_ortho_64_duplicate")):
        p1, p2 = plates_by_key[first][1], plates_by_key[second][1]
        assert p1["payload"]["visual_hash"] == p2["payload"]["visual_hash"]
        assert p1["id"] != p2["id"]
        created["same_picture_pairs"].append(
            {"keys": [first, second], "visual_hash": p1["payload"]["visual_hash"],
             "distinct_plate_ids": [p1["id"], p2["id"]]})

    # 给部分图版建立审阅事实（含一个接受、一个拒绝）。
    accepted = plates_by_key["wing_persp_64"][1]
    proof = call(app, "POST", "plates/" + accepted["id"] + "/proofs", {"note": "轮廓与通道一致"})
    call(app, "POST", "proofs/" + proof["id"] + "/decide",
         {"state": "accepted", "note": "审阅通过"})
    rejected_plate = plates_by_key["block_ortho_80"][1]
    proof2 = call(app, "POST", "plates/" + rejected_plate["id"] + "/proofs", {"note": "剖切边界复核"})
    call(app, "POST", "proofs/" + proof2["id"] + "/decide",
         {"state": "rejected", "note": "需补剖切标注"})
    # 隔离目标（默认 wing_persp_96）也先建立一个已接受审阅，
    # 以便第二轮隔离时演示“隔离使已接受审阅失效（invalidated）”。
    quarantine_target = plates_by_key["wing_persp_96"][1]
    proof3 = call(app, "POST", "plates/" + quarantine_target["id"] + "/proofs",
                  {"note": "隔离前的接受依据"})
    call(app, "POST", "proofs/" + proof3["id"] + "/decide",
         {"state": "accepted", "note": "首轮接受"})
    created["reviews"] = [
        {"plate_id": accepted["id"], "state": "accepted"},
        {"plate_id": rejected_plate["id"], "state": "rejected"},
        {"plate_id": quarantine_target["id"], "state": "accepted"}]
    created["parts"] = [
        {"assembly_id": assembly_a["id"], "seal_id": seal_a["id"], "name": assembly_a["payload"]["name"]},
        {"assembly_id": assembly_b["id"], "seal_id": seal_b["id"], "name": assembly_b["payload"]["name"]}]

    out = source_root / "material-manifest.json"
    out.write_text(json.dumps(created, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"source_root": str(source_root),
                      "plates": len(created["plates"]),
                      "same_picture_pairs": len(created["same_picture_pairs"])},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
