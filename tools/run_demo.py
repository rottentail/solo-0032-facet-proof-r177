#!/usr/bin/env python3
"""端到端演示：构建可离线核对的 Facet Proof 图版资料库。

流程（全部使用临时工作区，可重复运行）：
1. 调用真实渲染服务生成多部件/多镜头/多曝光素材（含同画面独立曝光）；
2. 第一轮采集：先在抓取中途暂停，证明中断不产生成功标记且可续做；
3. 续做时注入一次内容块失败，再次续做成功并封存；
4. 变更来源库（新增曝光、隔离/清退、再加同画面独立曝光）；
5. 第二轮采集封存，自动生成差异记录与根入口；
6. 离线核对整个资料库，并演示篡改可被检出。

建议以 `python3 tools/run_demo.py --workspace ./demo-workspace` 运行。
"""
import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYTHON = sys.executable


def run(description, cmd, expect_zero=True):
    print(f"\n$ {description}\n  {' '.join(cmd)}")
    completed = subprocess.run(cmd, cwd=str(ROOT), text=True,
                               capture_output=True)
    if completed.stdout:
        print(completed.stdout.strip())
    if completed.stderr:
        print("STDERR:", completed.stderr.strip(), file=sys.stderr)
    if expect_zero:
        assert completed.returncode == 0, f"步骤失败: {description}"
    return completed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", default=None,
                        help="工作区目录；默认临时目录")
    args = parser.parse_args()
    if args.workspace:
        workspace = Path(args.workspace).resolve()
        if workspace.exists():
            shutil.rmtree(workspace)
        workspace.mkdir(parents=True)
        cleanup = False
    else:
        import tempfile
        workspace = Path(tempfile.mkdtemp(prefix="facet-archive-demo-"))
        cleanup = True
    source = workspace / "facet-source"
    archive = workspace / "offline-archive"

    try:
        run("生成真实渲染素材（含同画面独立曝光与共享通道）",
            [PYTHON, "tools/generate_materials.py", "--source", str(source)])

        # 第一轮：抓取若干块后主动暂停，验证可续做且没有成功标记。
        paused = run("第一轮采集：中途暂停（应返回 paused，退出码 2）",
                     [PYTHON, "-m", "facetarchive", "harvest",
                      "--archive", str(archive), "--source", str(source),
                      "--round-id", "round_demo_0001", "--stop-after", "2"],
                     expect_zero=False)
        assert paused.returncode == 2 and "paused" in paused.stderr
        status1 = run("查看 staging 续做状态",
                      [PYTHON, "-m", "facetarchive", "status",
                       "--archive", str(archive)])
        assert '"state": "staging"' in status1.stdout
        assert not (archive / "rounds").exists() or not any(
            (archive / "rounds").iterdir()), "中断后不应出现封存轮次"

        # 续做：注入一次失败，再续做成功。
        journal = json.loads((archive / "staging" / "round_demo_0001" /
                              "journal.json").read_text("utf-8"))
        remaining = [ref["sha256"] for ref in
                     json.loads((archive / "staging" / "round_demo_0001" /
                                 "observation.json").read_text("utf-8"))["blob_closure"]
                     if ref["sha256"] not in set(journal["copied"])]
        assert remaining, "应当还有未抓取内容块"
        failed = run("续做并注入一次内容块失败（退出码 2，轮次保持 staging）",
                     [PYTHON, "-m", "facetarchive", "harvest",
                      "--archive", str(archive), "--source", str(source),
                      "--round-id", "round_demo_0001",
                      "--fail-checksum", remaining[0]], expect_zero=False)
        assert failed.returncode == 2 and "injected_failure" in failed.stderr
        run("再次续做：完成闭包并封存第一轮",
            [PYTHON, "-m", "facetarchive", "harvest",
             "--archive", str(archive), "--source", str(source),
             "--round-id", "round_demo_0001"])

        # 来源库在两轮之间发生变化。
        run("变更来源库：新增曝光、隔离/清退、再加同画面独立曝光",
            [PYTHON, "tools/mutate_source.py", "--source", str(source)])

        run("第二轮采集封存（自动生成差异记录）",
            [PYTHON, "-m", "facetarchive", "harvest",
             "--archive", str(archive), "--source", str(source),
             "--round-id", "round_demo_0002"])

        run("查看资料库状态（两轮封存 + 差异）",
            [PYTHON, "-m", "facetarchive", "status",
             "--archive", str(archive)])
        run("查看差异记录",
            [PYTHON, "-m", "facetarchive", "diffs",
             "--archive", str(archive)])
        verified = run("离线独立核对（无需原服务）",
                       [PYTHON, "-m", "facetarchive", "verify",
                        "--archive", str(archive)])
        report = json.loads(verified.stdout)
        assert report["valid"] and len(report["rounds"]) == 2

        # 篡改检测：改动某个只读内容块的副本，核对必须失败。
        _demonstrate_tamper(archive)

        print("\n演示完成。可浏览入口：")
        print(f"  资料库总入口: {archive / 'index.html'}")
        print(f"  第一轮目录:   {archive / 'rounds' / 'round_demo_0001' / 'browse' / 'index.html'}")
        print(f"  第二轮目录:   {archive / 'rounds' / 'round_demo_0002' / 'browse' / 'index.html'}")
        print(f"  只增账本:     {archive / 'ledger.json'}")
        print(f"  差异记录:     {archive / 'diffs'}")
    finally:
        if cleanup:
            shutil.rmtree(workspace, ignore_errors=True)


def _demonstrate_tamper(archive):
    import os
    ledger = json.loads((archive / "ledger.json").read_text("utf-8"))
    round_id = ledger["rounds"][0]["round_id"]
    manifest = json.loads((archive / "rounds" / round_id /
                           "manifest.json").read_text("utf-8"))
    target_checksum = manifest["blob_closure"][0]["sha256"]
    target = archive / "rounds" / round_id / "blobs" / target_checksum[:2] / target_checksum
    os.chmod(target, 0o600)
    raw = bytearray(target.read_bytes())
    raw[0] ^= 0xFF
    target.write_bytes(bytes(raw))
    tampered = run("篡改一个内容块后核对（应失败，退出码 2）",
                   [PYTHON, "-m", "facetarchive", "verify",
                    "--archive", str(archive)], expect_zero=False)
    assert tampered.returncode == 2 and "verify_blob" in tampered.stderr
    print("\n[已证明] 篡改被离线核对稳定检出；演示结束后该副本仅存在于本演示工作区。")


if __name__ == "__main__":
    main()
