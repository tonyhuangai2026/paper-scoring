from __future__ import annotations

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Lock, Thread
import time
import unittest
from xml.etree import ElementTree
from zipfile import ZipFile

import pymupdf

from paper_scoring.cli import write_assignment
from paper_scoring.judge import RUBRIC, InvalidResult, check_evidence, classify
from paper_scoring.materials import ScoringError, Settings, load_manifest
from paper_scoring.reports import details


IDS = ["017", "020", "025", *(f"{n:03d}" for n in range(27, 44))]
ROOT = Path(__file__).resolve().parents[1]
QUOTE = "Evidence in the synthetic paper."


def judgment(source: str, theory: bool = False, empirical: bool = True, unknown: bool = False) -> dict:
    def evidence():
        return [{"source": source, "start": 1, "end": 1, "quote": QUOTE}]

    def step(result):
        return {"result": result, "reason": "已核对本步证据。", "evidence": evidence()}

    def variable(role, name, economic):
        return {"role": role, "name": name, "economic": economic, "reason": "变量属性理由。", "evidence": evidence()}

    if theory and not empirical:
        variables = []
    elif empirical and not unknown:
        variables = [variable("independent", "Price", True), variable("dependent", "Output", False)]
    else:
        variables = [variable("central", "Price", True)]
    return {"status": "ok", "abstract": {"found": not unknown, "reason": "已核对摘要。", "evidence": evidence()},
            "theory": None if unknown else {"contribution": step(theory), "derivations": step(True) if theory else None},
            "empirical": None if unknown else {"real_data": step(empirical), "estimated_model": step(True) if empirical else None},
            "field": {"reason": "按正文的变量判断。", "variables": variables}}



class RuleTests(unittest.TestCase):
    def test_combinations_and_short_circuits(self):
        cases = [(False, False, "描述性统计和综述", "3.3"), (True, False, "理论", "3.1"),
                 (False, True, "实证", "3.2"), (True, True, "理论+实证", "3.2")]
        for theory, empirical, label, path in cases:
            with self.subTest(label=label):
                self.assertEqual(classify(judgment("md", theory, empirical), "md"), (label, "核心经管", path))
        unknown = judgment("pdf", unknown=True)
        self.assertEqual(classify(unknown, "pdf"), ("无法判断", "核心经管", "3.3"))
        with self.assertRaises(InvalidResult):
            classify(unknown, "md")
        broad = judgment("md")
        broad["field"]["variables"][0]["economic"] = False
        self.assertEqual(classify(broad, "md")[1], "广义经管")
        bad = judgment("md")
        bad["theory"]["derivations"] = False
        with self.assertRaises(InvalidResult):
            classify(bad, "md")

    def test_each_executed_step_and_variable_requires_its_own_evidence(self):
        for parent, child in (("theory", "contribution"), ("theory", "derivations"),
                              ("empirical", "real_data"), ("empirical", "estimated_model")):
            with self.subTest(step=child):
                result = judgment("md", theory=True, empirical=True)
                result[parent][child]["evidence"] = []
                with self.assertRaises(InvalidResult):
                    classify(result, "md")
        result = judgment("md")
        result["field"]["variables"][1]["evidence"] = []
        with self.assertRaises(InvalidResult):
            classify(result, "md")

    def test_report_lists_executed_steps_and_skip_reasons(self):
        for result, pdf, expected in (
            (judgment("md", theory=True, empirical=False), False, ("1.3 正文是否有", "2.2 跳过：未使用真实数据")),
            (judgment("md"), False, ("1.3 跳过：摘要未明确提出理论贡献", "2.2 是否明确建模并估计出系数")),
            (judgment("pdf", unknown=True), True, ("1.2 跳过", "1.3 跳过", "2.1 跳过", "2.2 跳过")),
        ):
            mode = "pdf" if pdf else "md"
            _, label_two, path = classify(result, mode)
            report = details({"judgment": result, "field_path": path, "label_two": label_two,
                              "modes": [mode], "pdf_pages": 1, "image_pages": []})
            report_text = "\n".join(line for _, lines in report for line in lines)
            for phrase in expected:
                self.assertIn(phrase, report_text)

    def test_evidence_must_match_location_and_page_scope(self):
        good = judgment("md")
        check_evidence(good, "md", [QUOTE], [], [])
        with self.assertRaises(InvalidResult):
            check_evidence(good, "md", ["Other text", QUOTE], [], [])
        image = judgment("pdf_image")
        with self.assertRaises(InvalidResult):
            check_evidence(image, "pdf_image", [], [QUOTE], [])
        check_evidence(image, "pdf_image", [], [QUOTE], [1])


def create_materials(root: Path) -> Path:
    for folder in ("pdf", "md"):
        (root / folder).mkdir()
    for number in IDS:
        file_name = f"{number}-Sample paper {number}"
        marker = "## Abstract. " if number != "020" else ""
        (root / "md" / f"{file_name}.md").write_text(marker + QUOTE + "\nUnique fixture " + number + "\n", encoding="utf-8")
        with pymupdf.open() as doc:
            page = doc.new_page()
            page.insert_text((72, 96), QUOTE + "\nUnique fixture " + number)
            doc.save(root / "pdf" / f"{file_name}.pdf")
    manifest = root / "papers.toml"
    manifest.write_text('name = "fixture"\npdf_dir = "pdf"\nmd_dir = "md"\nids = ' + json.dumps(IDS) + "\n", encoding="utf-8")
    return manifest


@contextmanager
def fake_gateway():
    lock = Lock()
    state = {"requests": [], "errors": [], "fail_images": True, "active": 0, "peak": 0, "md_017": 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send_json(self, value, status=200):
            response = json.dumps(value, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def do_POST(self):
            with lock:
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
            try:
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                content = request["messages"][1]["content"]
                mode = "pdf_image" if isinstance(content, list) else "pdf" if "<PDF page=" in content else "md"
                text = content[0]["text"] if mode == "pdf_image" else content
                paper = re.search(r"当前论文编号 (\d+)", text)[1]
                with lock:
                    state["requests"].append((paper, mode))
                    if len(request["messages"]) != 2 or re.findall(r"Unique fixture (\d+)", text) != [paper]:
                        state["errors"].append("request scope")
                    if self.path != "/v1/chat/completions" or request["model"] != "fixture-model":
                        state["errors"].append("route/model")
                    if ([m["role"] for m in request["messages"]] != ["system", "user"]
                            or request["messages"][0]["content"] != RUBRIC.strip()
                            or request.get("response_format") != {"type": "json_object"}
                            or request.get("thinking") != {"type": "disabled"}
                            or request.get("reasoning_effort") != "none"
                            or request.get("temperature") != 0 or request.get("max_tokens") != 8192
                            or any(key in request for key in ("tools", "store", "previous_response_id", "conversation"))):
                        state["errors"].append("agent request contract")
                    if mode == "pdf_image" and not any(
                        part["type"] == "image_url" and part["image_url"]["url"].startswith("data:image/png;base64,")
                        for part in content
                    ):
                        state["errors"].append("image content")
                    if paper == "017" and mode == "md":
                        state["md_017"] += 1
                    use_theory = state["md_017"] <= 5
                time.sleep(.018)
                if paper in {"020", "025"} and mode == "md":
                    result = {"status": "needs_pdf", "reason": "请核查 PDF。", "pages": []}
                elif paper == "020":
                    result = judgment("pdf", unknown=True)
                    result["abstract"]["evidence"] = [{"ref": "P1.1"}]
                    result["field"]["variables"][0]["evidence"] = [{"ref": "P1.1"}]
                elif paper == "025" and mode == "pdf":
                    result = {"status": "needs_pdf", "reason": "请核查页图。", "pages": [1]}
                elif paper == "025" and state["fail_images"]:
                    return self.send_json({"ignored": "secret-like error; do not log"}, 422)
                else:
                    result = judgment(mode, theory=paper == "017" and use_theory)
                answer = json.dumps(result, ensure_ascii=False)
                if mode == "pdf_image":
                    answer = "```json\n" + answer + "\n```"
                self.send_json({"id": "fixture-response", "created": 1, "object": "chat.completion", "model": "fixture-model",
                                "choices": [{"index": 0, "message": {"role": "assistant", "content": answer}, "finish_reason": "stop"}]})
            except (AssertionError, KeyError, ValueError, TypeError) as error:
                with lock:
                    state["errors"].append(type(error).__name__)
                self.send_json({"error": "bad fixture request"}, 422)
            finally:
                with lock:
                    state["active"] -= 1

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class IntegrationTests(unittest.TestCase):
    def test_missing_material_can_be_restored_without_mixing_ready_inputs(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            manifest_file = create_materials(root)
            original_pdf = root / "pdf" / "025-Sample paper 025.pdf"
            removed = original_pdf.read_bytes()
            original_pdf.unlink()
            settings = Settings("http://127.0.0.1/v1", "fixture-model", "fixture-key")
            first = load_manifest(manifest_file)
            self.assertTrue(first.papers[2].issue)
            run_id = write_assignment(root / "run", first, first.papers, settings, False)
            original_pdf.write_bytes(removed)
            restored = load_manifest(manifest_file)
            self.assertFalse(restored.papers[2].issue)
            self.assertEqual(write_assignment(root / "run", restored, restored.papers, settings, False), run_id)
            with restored.papers[0].md.open("a", encoding="utf-8") as changed:
                changed.write("Changed prior evidence.")
            edited = load_manifest(manifest_file)
            with self.assertRaises(ScoringError):
                write_assignment(root / "run", edited, edited.papers, settings, False)

    def test_ten_workers_partial_resume_and_reports(self):
        with TemporaryDirectory() as temp, fake_gateway() as (base_url, state):
            root = Path(temp)
            manifest = create_materials(root)
            output = root / "runs" / "fixture"
            env_file = root / ".env"
            env_file.write_text(f"PAPER_SCORING_BASE_URL={base_url}\nPAPER_SCORING_MODEL=fixture-model\nPAPER_SCORING_API_KEY=fixture-key\n", encoding="utf-8")
            env = {k: v for k, v in os.environ.items() if not k.startswith("PAPER_SCORING_")}
            cmd = [sys.executable, "-m", "paper_scoring", "run", "--manifest", str(manifest),
                   "--env-file", str(env_file), "--run-dir", str(output)]

            def run(expected_status: int):
                result = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True, timeout=100)
                self.assertEqual(result.returncode, expected_status, result.stdout + result.stderr)

            run(2)
            final = output / "04_汇总结果"
            receipt = final / "验收报告.md"
            self.assertIn("190 / 200", receipt.read_text())
            self.assertIn("实测子 Agent 最大并发：3", receipt.read_text())
            self.assertNotIn("secret-like", receipt.read_text())
            self.assertFalse(list(final.glob("*.xlsx")))
            self.assertEqual(state["peak"], 3)
            self.assertFalse(state["errors"])
            self.assertEqual([request for request in state["requests"] if request[0] == "020"], [("020", "pdf")] * 10)

            state["fail_images"] = False
            request_count = len(state["requests"])
            run(0)
            self.assertEqual(set(state["requests"][request_count:]), {("025", "md"), ("025", "pdf"), ("025", "pdf_image")})
            self.assertEqual(len(state["requests"]) - request_count, 30)
            self.assertIn("200 / 200", receipt.read_text())
            self.assertIn("子 Agent 框架：Pydantic AI", receipt.read_text())
            self.assertIn("标签一：19 / 20", receipt.read_text())
            self.assertIn("标签组合：19 / 20", receipt.read_text())
            self.assertIn("恢复", receipt.read_text())
            self.assertEqual(len(list((final / "PDF-每组详细判定").glob("*.pdf"))), 10)
            self.assertEqual(len(list((final / "MD-每组详细判定").glob("*.md"))), 10)
            with pymupdf.open(final / "PDF-每组详细判定" / "agent_01-组01.pdf") as pdf:
                self.assertGreaterEqual(len(pdf), 21)
                self.assertIn("原始序号", pdf[0].get_text())
                self.assertIn("理论判断及依据", pdf[1].get_text())
            review = (final / "不一致论文复核.md").read_text()
            self.assertIn("存在分歧", review)
            self.assertIn("存在无法判断结果", review)
            self.assertNotIn("## 025", review)
            excel = next(final.glob("*.xlsx"))
            self.verify_excel(excel)

            before = excel.stat().st_mtime_ns
            request_count = len(state["requests"])
            run(0)
            self.assertEqual(request_count, len(state["requests"]))
            self.assertEqual(before, excel.stat().st_mtime_ns)
            self.assertFalse(state["errors"])

    def verify_excel(self, path: Path):
        ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        with ZipFile(path) as archive:
            raw = ElementTree.fromstring(archive.read("xl/worksheets/sheet1.xml"))
            cells = {c.get("r"): c for c in raw.findall(".//m:c", ns)}
            self.assertEqual(cells["E4"].findtext("m:v", namespaces=ns), "实证 5 票；理论+实证 5 票")
            self.assertEqual(cells["F4"].findtext("m:v", namespaces=ns), "0.5")
            self.assertEqual(cells["I4"].findtext("m:v", namespaces=ns), "众数并列")
            self.assertEqual(cells["F5"].findtext("m:v", namespaces=ns), "1.0")
            self.assertEqual(cells["G5"].findtext("m:v", namespaces=ns), "是")
            self.assertEqual(cells["H5"].findtext("m:v", namespaces=ns), "是")
            self.assertIsNone(cells["J5"].find("m:v", ns))
            self.assertIn("COUNTIFS", cells["K4"].findtext("m:f", namespaces=ns))
            self.assertEqual(raw.find(".//m:pane", ns).get("topLeftCell"), "C4")
            detail = ElementTree.fromstring(archive.read("xl/worksheets/sheet3.xml"))
            self.assertEqual(len(detail.findall(".//m:row", ns)), 203)
            self.assertEqual(len(detail.findall(".//m:hyperlink", ns)), 400)
            self.assertIn(b'TargetMode="External"', archive.read("xl/worksheets/_rels/sheet3.xml.rels"))


if __name__ == "__main__":
    unittest.main()
