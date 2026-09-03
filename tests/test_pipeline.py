from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from publishing_qa.pipeline import (
    GENERATED_FILES,
    BuildFailed,
    build_release,
    check_workspace,
    verify_release,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_ROOT = PROJECT_ROOT / "sample"


class PipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name) / "sample"
        shutil.copytree(SAMPLE_ROOT, self.workspace, ignore=shutil.ignore_patterns("output"))

    def _json(self, name: str) -> dict:
        path = self.workspace / "input" / name
        return json.loads(path.read_text(encoding="utf-8"))

    def _write_json(self, name: str, value: dict) -> None:
        path = self.workspace / "input" / name
        path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")

    def test_clean_sample_passes_all_quality_gates(self) -> None:
        report = check_workspace(self.workspace)
        self.assertEqual("pass", report["summary"]["status"])
        self.assertEqual(0, report["summary"]["errors"])
        self.assertGreaterEqual(report["summary"]["passed"], 40)

    def test_build_creates_complete_verifiable_release(self) -> None:
        output = self.workspace / "output"
        manifest = build_release(self.workspace, output)
        self.assertEqual("tracepress-harbor-notes-0.1.0", manifest["release"]["release_id"])
        self.assertEqual(set(GENERATED_FILES), {item.name for item in output.iterdir()})
        self.assertEqual("pass", verify_release(output)["summary"]["status"])

        page = (output / "sample-publication.html").read_text(encoding="utf-8")
        self.assertIn('lang="en"', page)
        self.assertIn('class="skip"', page)
        self.assertIn('<main id="content">', page)
        self.assertIn('aria-label="Scrollable claim traceability table"', page)
        self.assertIn('id="trace-C-001"', page)

    def test_build_is_byte_for_byte_deterministic(self) -> None:
        first = Path(self.temp.name) / "first"
        second = Path(self.temp.name) / "second"
        build_release(self.workspace, first)
        build_release(self.workspace, second)
        for name in GENERATED_FILES:
            self.assertEqual((first / name).read_bytes(), (second / name).read_bytes(), name)

    def test_unknown_evidence_source_blocks_build(self) -> None:
        claims = self._json("claims.json")
        claims["claims"][0]["evidence"][0]["source_id"] = "SRC-999"
        claims["claims"][0]["verification"]["source_id"] = "SRC-999"
        self._write_json("claims.json", claims)
        report = check_workspace(self.workspace)
        self.assertEqual("fail", report["summary"]["status"])
        failures = {item["code"] for item in report["checks"] if item["status"] == "fail"}
        self.assertIn("CLAIM.EVIDENCE", failures)
        self.assertIn("CLAIM.REPRODUCIBLE", failures)
        with self.assertRaises(BuildFailed):
            build_release(self.workspace, self.workspace / "output")

    def test_unregistered_remote_link_blocks_build_without_network(self) -> None:
        manuscript = self.workspace / "input" / "manuscript.md"
        manuscript.write_text(
            manuscript.read_text(encoding="utf-8") + "\n[Unregistered](https://unregistered.invalid/resource)\n",
            encoding="utf-8",
        )
        report = check_workspace(self.workspace)
        failures = [item for item in report["checks"] if item["status"] == "fail"]
        self.assertTrue(any(item["code"] == "LINK.OFFLINE_REGISTERED" for item in failures))

    def test_unresolved_editorial_token_blocks_build(self) -> None:
        manuscript = self.workspace / "input" / "manuscript.md"
        manuscript.write_text(manuscript.read_text(encoding="utf-8") + "\nTODO confirm this.\n", encoding="utf-8")
        report = check_workspace(self.workspace)
        failure_codes = {item["code"] for item in report["checks"] if item["status"] == "fail"}
        self.assertIn("MANUSCRIPT.NO_EDITORIAL_TOKENS", failure_codes)

    def test_source_path_escape_is_rejected(self) -> None:
        sources = self._json("sources.json")
        sources["records"][0]["path"] = "../../outside.csv"
        self._write_json("sources.json", sources)
        report = check_workspace(self.workspace)
        failure_codes = {item["code"] for item in report["checks"] if item["status"] == "fail"}
        self.assertIn("SOURCE.PATH_CONTAINED", failure_codes)

    def test_renderer_escapes_untrusted_manuscript_html(self) -> None:
        manuscript = self.workspace / "input" / "manuscript.md"
        manuscript.write_text(
            manuscript.read_text(encoding="utf-8") + "\n<script>alert('synthetic')</script>\n",
            encoding="utf-8",
        )
        output = self.workspace / "output"
        build_release(self.workspace, output)
        page = (output / "sample-publication.html").read_text(encoding="utf-8")
        self.assertNotIn("<script>alert", page)
        self.assertIn("&lt;script&gt;alert", page)

    def test_tampering_is_detected(self) -> None:
        output = self.workspace / "output"
        build_release(self.workspace, output)
        publication = output / "sample-publication.html"
        publication.write_text(publication.read_text(encoding="utf-8") + "\nchanged\n", encoding="utf-8")
        report = verify_release(output)
        self.assertEqual("fail", report["summary"]["status"])
        failures = [item for item in report["checks"] if item["status"] == "fail"]
        self.assertTrue(any(item["code"] == "VERIFY.DIGEST" for item in failures))

    def test_removed_checksum_entry_is_detected(self) -> None:
        output = self.workspace / "output"
        build_release(self.workspace, output)
        checksum = output / "CHECKSUMS.sha256"
        lines = checksum.read_text(encoding="ascii").splitlines()
        checksum.write_text("\n".join(lines[1:]) + "\n", encoding="ascii")
        report = verify_release(output)
        failure_codes = {item["code"] for item in report["checks"] if item["status"] == "fail"}
        self.assertIn("VERIFY.COMPLETE_SET", failure_codes)


if __name__ == "__main__":
    unittest.main()
