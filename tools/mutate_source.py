#!/usr/bin/env python3
"""对既有来源数据根做一轮“观察时点之后”的变更，用于第二轮收录。

变更内容：
1. 在斜翼部件上新增一次曝光（新增图版）；
2. 隔离一张已接受图版，使其既有接受审阅失效，再标记清退（字节仍保留）；
3. 再加一次与既有图版同画面的独立曝光，验证不按摘要并条。

资料库必须把这些变化反映为“新一轮观察 + 差异记录”，而不是改写旧轮次。
建议以 `python3 tools/mutate_source.py ...` 运行。
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


def main():
    parser = argparse.ArgumentParser(description="在第二轮观察前变更来源库")
    parser.add_argument("--source", required=True)
    parser.add_argument("--quarantine-key", default="wing_persp_96")
    args = parser.parse_args()
    source_root = Path(args.source).resolve()
    material = json.loads((source_root / "material-manifest.json").read_text("utf-8"))
    app = Application(str(source_root))

    by_key = {item["key"]: item for item in material["plates"]}
    seal_a = next(p for p in material["parts"] if "斜翼" in p["name"])["seal_id"]

    # 1) 新增一张不同尺寸的曝光。
    exp_new = call(app, "POST", "seals/" + seal_a + "/exposures",
                   {"key": "wing_persp_48_round2", "width": 48, "height": 48})
    rendered_new = call(app, "POST", "worker/step")
    assert rendered_new["state"] == "succeeded", rendered_new
    new_plate = rendered_new["plate"]

    # 2) 同画面独立曝光（与已存在的 wing_persp_64 同参数、新键）。
    call(app, "POST", "seals/" + seal_a + "/exposures",
         {"key": "wing_persp_64_round2_dup", "width": 64, "height": 64})
    rendered_dup = call(app, "POST", "worker/step")
    assert rendered_dup["state"] == "succeeded", rendered_dup
    dup_plate = rendered_dup["plate"]

    # 3) 隔离并清退指定图版；隔离会使其已接受审阅失效。
    target_id = by_key[args.quarantine_key]["plate_id"]
    quarantined = call(app, "POST", "plates/" + target_id + "/quarantine",
                       {"reason": "第二轮观察前撤回：更换标注规范"})
    purged = call(app, "POST", "plates/" + target_id + "/purge")

    report = {
        "added_plate": new_plate["id"],
        "added_exposure": exp_new["id"],
        "same_picture_independent_plate": dup_plate["id"],
        "quarantined_and_purged_plate": target_id,
        "quarantined_state": quarantined["state"],
        "purged_state": purged["state"]}
    out = source_root / "mutation-round2.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
