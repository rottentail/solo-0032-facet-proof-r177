"""Facet Proof 离线图版资料库命令行。

子命令：
  harvest   对来源数据根做一轮可续做观察并封存
  status    查看已封存轮次与 staging 续做状态
  verify    离线独立核对资料库（无需原服务）
  diffs     列出差异记录
"""
import argparse
import json
import sys

from . import archive as archive_mod
from . import verify as verify_mod
from .canonical import read_json_file
from .errors import ArchiveError


def _print(value):
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _cmd_harvest(args):
    result = archive_mod.archive_round(
        args.archive, args.source, round_id=args.round_id,
        stop_after=args.stop_after, fail_checksum=args.fail_checksum,
        render_browse=not args.no_browse)
    _print(result)
    return 0


def _cmd_status(args):
    _print(archive_mod.status(args.archive))
    return 0


def _cmd_verify(args):
    report = verify_mod.verify_archive(args.archive)
    _print(report)
    return 0


def _cmd_diffs(args):
    from pathlib import Path
    diff_dir = Path(args.archive) / "diffs"
    docs = []
    if diff_dir.is_dir():
        for path in sorted(diff_dir.glob("*.json")):
            docs.append(read_json_file(path))
    _print({"diffs": docs})
    return 0


def build_parser():
    parser = argparse.ArgumentParser(prog="python3 -m facetarchive",
                                     description="Facet Proof 离线图版资料库")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("harvest", help="执行一轮可续做观察并封存")
    p.add_argument("--archive", required=True, help="资料库根目录")
    p.add_argument("--source", required=True, help="渲染服务数据根（facetproof 根）")
    p.add_argument("--round-id", help="显式轮次号（续做同一轮时使用）")
    p.add_argument("--stop-after", type=int,
                   help="（演示/测试）抓取 N 个新内容块后暂停，保留 staging")
    p.add_argument("--fail-checksum",
                   help="（演示/测试）命中该内容块时注入一次失败，保留 staging")
    p.add_argument("--no-browse", action="store_true", help="只封存清单，不渲染浏览目录")
    p.set_defaults(func=_cmd_harvest)

    p = sub.add_parser("status", help="查看封存轮次与可续做 staging")
    p.add_argument("--archive", required=True)
    p.set_defaults(func=_cmd_status)

    p = sub.add_parser("verify", help="离线独立核对资料库")
    p.add_argument("--archive", required=True)
    p.set_defaults(func=_cmd_verify)

    p = sub.add_parser("diffs", help="列出差异记录")
    p.add_argument("--archive", required=True)
    p.set_defaults(func=_cmd_diffs)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ArchiveError as exc:
        print(json.dumps(exc.as_dict(), ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
