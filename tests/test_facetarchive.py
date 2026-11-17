"""facetarchive 离线资料库自动化测试。

覆盖任务的关键不变量：
- 中断/注入失败可续做，且中断绝不产生成功封存；
- 一页内容只来自同一观察轮次，来源变更不污染旧轮次；
- 身份是 URN 而非图片摘要：同画面独立曝光不被并条，共享字节只存一份；
- 清单、账本哈希链、差异记录可离线独立复核，篡改必被检出；
- 隔离/清退保留内容寻址字节，并在差异中正确分类。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from facetarchive import archive as archive_mod  # noqa: E402
from facetarchive import differ, harvester, seal as seal_mod  # noqa: E402
from facetarchive import verify as verify_mod  # noqa: E402
from facetarchive.canonical import digest, read_json_file  # noqa: E402
from facetarchive.errors import ArchiveError  # noqa: E402

PYTHON = sys.executable
TOOLS = ROOT / "tools"


def run_tool(*args, expect_zero=True):
    proc = subprocess.run([PYTHON, *args], cwd=str(ROOT), text=True,
                          capture_output=True)
    if expect_zero:
        assert proc.returncode == 0, proc.stderr
    return proc


class ArchiveTestBase(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="facetarchive-test-"))
        self.source = self.work / "source"
        self.archive = self.work / "archive"
        run_tool(str(TOOLS / "generate_materials.py"),
                 "--source", str(self.source))
        self.material = json.loads(
            (self.source / "material-manifest.json").read_text("utf-8"))

    def tearDown(self):
        shutil.rmtree(self.work, ignore_errors=True)

    def harvest(self, round_id, **kwargs):
        return archive_mod.archive_round(
            str(self.archive), str(self.source), round_id=round_id, **kwargs)

    def manifest(self, round_id):
        return seal_mod.load_manifest(
            self.archive / "rounds" / round_id)


class TestGenerationAndIdentity(ArchiveTestBase):
    def test_materials_contain_same_picture_independent_exposures(self):
        self.assertEqual(len(self.material["same_picture_pairs"]), 2)
        for pair in self.material["same_picture_pairs"]:
            self.assertEqual(len(set(pair["distinct_plate_ids"])), 2)

    def test_same_picture_plates_keep_distinct_urn_and_shared_bytes(self):
        self.harvest("round_a")
        manifest = self.manifest("round_a")
        # 六张图版全部收录，没有因为画面相同而减少条数。
        self.assertEqual(len(manifest["plates"]), 6)
        urns = [p["urn"] for p in manifest["plates"]]
        exposures = [p["exposure_urn"] for p in manifest["plates"]]
        self.assertEqual(len(set(urns)), 6)
        self.assertEqual(len(set(exposures)), 6)
        # 同画面独立曝光：visual_hash 相同，但 URN/曝光 URN 不同。
        visual = {}
        for plate in manifest["plates"]:
            visual.setdefault(plate["visual_hash"], set()).add(plate["urn"])
        merged = [u for u in visual.values() if len(u) > 1]
        self.assertEqual(len(merged), 2)
        # 内容寻址：36 个通道引用去重后少于 36，且共享字节物理上只存一份。
        all_checksums = [d["sha256"] for p in manifest["plates"]
                         for d in p["passes"].values()]
        self.assertEqual(len(all_checksums), 36)
        self.assertLess(len(set(all_checksums)), 36)
        closure = {r["sha256"]: r for r in manifest["blob_closure"]}
        self.assertEqual(len(closure), len(set(all_checksums)))
        shared = [r for r in closure.values() if len(r["sources"]) > 1]
        self.assertTrue(shared)
        for checksum in closure:
            matches = list((self.archive / "rounds" / "round_a" /
                            "blobs" / checksum[:2]).glob(checksum))
            self.assertEqual(len(matches), 1)

    def test_review_and_source_facts_carried_into_manifest(self):
        self.harvest("round_a")
        manifest = self.manifest("round_a")
        states = {fact["state"] for p in manifest["plates"]
                  for fact in p["review_facts"]}
        self.assertIn("accepted", states)
        self.assertIn("rejected", states)
        for plate in manifest["plates"]:
            self.assertTrue(plate["seal"]["placements"])
            self.assertEqual(set(plate["passes"]),
                             {"color.rgb", "depth.f32", "object.u32",
                              "color.png", "depth.png", "object.png"})


class TestResumability(ArchiveTestBase):
    def test_interruption_leaves_no_sealed_round_and_resumes(self):
        with self.assertRaises(ArchiveError) as ctx:
            self.harvest("round_resume", stop_after=1)
        self.assertEqual(ctx.exception.code, "paused")
        # 中断后没有任何封存轮次，账本也不存在。
        self.assertFalse((self.archive / "rounds").exists()
                         and any((self.archive / "rounds").iterdir()))
        self.assertFalse((self.archive / "ledger.json").exists())
        journal = harvester.read_journal(
            self.archive / "staging" / "round_resume")
        self.assertEqual(journal["state"], "staging")
        self.assertLess(journal["copied_blobs"], journal["total_blobs"])
        # 续做沿用同一固定快照。
        snapshot_before = journal["snapshot"]["sha256"]
        result = self.harvest("round_resume")
        journal2 = harvester.read_journal(
            self.archive / "staging" / "round_resume")
        self.assertEqual(journal2["state"], "sealed")
        self.assertEqual(journal2["snapshot"]["sha256"], snapshot_before)
        self.assertEqual(result["plates"], 6)

    def test_injected_failure_is_retryable_and_never_fakes_success(self):
        with self.assertRaises(ArchiveError):
            self.harvest("round_fail", stop_after=2)
        observation = read_json_file(
            self.archive / "staging" / "round_fail" / "observation.json")
        remaining = [r["sha256"] for r in observation["blob_closure"]
                     if r["sha256"] not in set(
                         harvester.read_journal(
                             self.archive / "staging" / "round_fail")["copied"])]
        with self.assertRaises(ArchiveError) as ctx:
            self.harvest("round_fail", fail_checksum=remaining[0])
        self.assertEqual(ctx.exception.code, "injected_failure")
        self.assertFalse((self.archive / "ledger.json").exists())
        # 再次续做成功。
        result = self.harvest("round_fail")
        self.assertEqual(result["plates"], 6)
        verify_mod.verify_archive(str(self.archive))

    def test_resume_cannot_switch_source_root(self):
        with self.assertRaises(ArchiveError):
            self.harvest("round_switch", stop_after=1)
        other = self.work / "other-source"
        shutil.copytree(self.source, other)
        # 用另一来源根续做同一轮次，必须在打开 journal 时被来源归属拦截。
        with self.assertRaises(ArchiveError) as ctx:
            harvester.begin_round(
                str(self.archive), str(other), round_id="round_switch")
        self.assertEqual(ctx.exception.code, "round_origin")


class TestRoundIsolationAndDiffs(ArchiveTestBase):
    def _two_rounds(self):
        self.harvest("round_one")
        run_tool(str(TOOLS / "mutate_source.py"),
                 "--source", str(self.source))
        self.harvest("round_two")

    def test_old_round_immutable_after_source_changes(self):
        before = self.harvest("round_one")
        first_manifest = self.manifest("round_one")
        run_tool(str(TOOLS / "mutate_source.py"),
                 "--source", str(self.source))
        self.harvest("round_two")
        again = self.manifest("round_one")
        self.assertEqual(again["manifest_hash"], before["manifest_hash"])
        self.assertEqual(again, first_manifest)
        # 旧轮只读。
        plate_file = self.archive / "rounds" / "round_one" / "manifest.json"
        self.assertFalse(os.access(plate_file, os.W_OK))
        journal_file = self.archive / "staging" / "round_one" / "journal.json"
        self.assertFalse(os.access(journal_file, os.W_OK))

    def test_each_browse_page_belongs_to_one_round(self):
        self._two_rounds()
        for round_id in ("round_one", "round_two"):
            browse_dir = self.archive / "rounds" / round_id / "browse"
            manifest_hash = self.manifest(round_id)["manifest_hash"]
            for html in browse_dir.rglob("*.html"):
                text = html.read_text("utf-8")
                self.assertIn(round_id, text)
                self.assertIn(manifest_hash, text)
            # 媒体全部位于本轮目录内，无指向来源或其他轮次的链接。
            for media in browse_dir.rglob("*"):
                if media.is_file():
                    self.assertTrue(str(media).startswith(str(browse_dir)))

    def test_diff_classifies_independent_exposure_and_purge(self):
        self._two_rounds()
        diff_path = next((self.archive / "diffs").glob("*.json"))
        doc = read_json_file(diff_path)
        kinds = {e["kind"] for e in doc["entries"]}
        self.assertIn("added_independent_exposure", kinds)
        self.assertIn("purged", kinds)
        self.assertIn("added", kinds)
        indep = next(e for e in doc["entries"]
                     if e["kind"] == "added_independent_exposure")
        self.assertTrue(indep["same_picture_existing_urns"])
        # 差异记录自身哈希可复算。
        expect = digest({k: v for k, v in doc.items() if k != "diff_hash"})
        self.assertEqual(expect, doc["diff_hash"])

    def test_purge_retains_content_addressed_bytes(self):
        self._two_rounds()
        first = self.manifest("round_one")
        second = self.manifest("round_two")
        purged_id = json.loads(
            (self.source / "mutation-round2.json").read_text("utf-8")
        )["quarantined_and_purged_plate"]
        # 第二轮里该图版状态为 purged，但其通道字节仍在闭包内。
        plate = next(p for p in second["plates"] if p["plate_id"] == purged_id)
        self.assertEqual(plate["state"], "purged")
        blob_root = self.archive / "rounds" / "round_two" / "blobs"
        for descriptor in plate["passes"].values():
            self.assertTrue((blob_root / descriptor["sha256"][:2]
                             / descriptor["sha256"]).is_file())
        # 旧轮里它仍是 ready，证明审阅事实没有被跨轮改写。
        old_plate = next(p for p in first["plates"]
                         if p["plate_id"] == purged_id)
        self.assertEqual(old_plate["state"], "ready")
        # 隔离使首轮的“已接受”审阅在新快照中失效；旧轮仍保留当时的接受事实。
        self.assertTrue(any(f["state"] == "accepted"
                            for f in old_plate["review_facts"]))
        self.assertTrue(any(f["state"] == "invalidated"
                            for f in plate["review_facts"]))
        self.assertFalse(any(f["state"] == "accepted"
                             for f in plate["review_facts"]))

    def test_ledger_hash_chain_links_rounds(self):
        self._two_rounds()
        ledger = seal_mod.load_ledger(self.archive)
        self.assertEqual(len(ledger["rounds"]), 2)
        self.assertIsNone(ledger["rounds"][0]["prev_head"])
        self.assertEqual(ledger["rounds"][1]["prev_head"],
                         ledger["rounds"][0]["entry_hash"])
        self.assertEqual(ledger["head"], ledger["rounds"][1]["entry_hash"])


class TestVerification(ArchiveTestBase):
    def test_clean_archive_verifies(self):
        self.harvest("round_v")
        report = verify_mod.verify_archive(str(self.archive))
        self.assertTrue(report["valid"])

    def test_tampered_blob_is_detected(self):
        self.harvest("round_t")
        manifest = self.manifest("round_t")
        checksum = manifest["blob_closure"][0]["sha256"]
        target = (self.archive / "rounds" / "round_t" / "blobs"
                  / checksum[:2] / checksum)
        os.chmod(target, 0o600)
        raw = bytearray(target.read_bytes())
        raw[0] ^= 0x01
        target.write_bytes(bytes(raw))
        with self.assertRaises(ArchiveError) as ctx:
            verify_mod.verify_archive(str(self.archive))
        self.assertEqual(ctx.exception.code, "verify_blob")

    def test_ledger_tamper_is_detected(self):
        self._two = self.harvest("round_l")
        ledger_path = self.archive / "ledger.json"
        os.chmod(ledger_path, 0o600)
        ledger = json.loads(ledger_path.read_text("utf-8"))
        ledger["head"] = "0" * 64
        ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
        with self.assertRaises(ArchiveError) as ctx:
            verify_mod.verify_archive(str(self.archive))
        self.assertEqual(ctx.exception.code, "verify_ledger")

    def test_manifest_tamper_is_detected(self):
        self.harvest("round_m")
        manifest_path = (self.archive / "rounds" / "round_m" / "manifest.json")
        os.chmod(manifest_path, 0o600)
        manifest = json.loads(manifest_path.read_text("utf-8"))
        manifest["sealed"] = "1999-01-01T00:00:00+00:00"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False),
                                 encoding="utf-8")
        with self.assertRaises(ArchiveError) as ctx:
            verify_mod.verify_archive(str(self.archive))
        self.assertEqual(ctx.exception.code, "verify_manifest")

    def test_no_facetproof_import_for_offline_verify(self):
        self.harvest("round_offline")
        # 在一个禁止导入 facetproof 的子进程里完成核对，证明接收者无需服务。
        code = (
            "import sys; sys.modules['facetproof']=None; "
            "from facetarchive.verify import verify_archive; "
            f"r=verify_archive(r'{self.archive}'); "
            "assert r['valid']; print('offline-ok')")
        proc = subprocess.run([PYTHON, "-c", code], cwd=str(ROOT),
                              text=True, capture_output=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("offline-ok", proc.stdout)


class TestPngDecoder(unittest.TestCase):
    def test_roundtrip_decode_matches_encoded_pixels(self):
        from facetproof.png import encode_rgb
        from facetarchive.pngcheck import decode_rgb
        width = height = 12
        pixels = bytes((x * 7 + y * 13) % 256
                       for y in range(height) for x in range(width * 3))
        raw = encode_rgb(width, height, pixels)
        w, h, decoded = decode_rgb(raw)
        self.assertEqual((w, h), (width, height))
        self.assertEqual(decoded, pixels)

    def test_rejects_truncated_png(self):
        from facetarchive.pngcheck import decode_rgb
        with self.assertRaises(ArchiveError):
            decode_rgb(b"\x89PNG\r\n\x1a\n\x00\x00")


if __name__ == "__main__":
    unittest.main(verbosity=2)
