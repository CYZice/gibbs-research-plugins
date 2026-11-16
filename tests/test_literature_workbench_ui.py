"""Real browser, synthetic API fixtures. No access to a user's literature database."""
import io
import os
from pathlib import Path
import socket
import subprocess
import time
from urllib.parse import urlsplit

import httpx
import pytest
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.figure import Figure
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(not os.environ.get("GIBBS_TEST_LIBRARY_UI"), reason="Set GIBBS_TEST_LIBRARY_UI=1; install frontend dependencies and a browser")


@pytest.fixture(scope="module")
def literature_ui():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    frontend = ROOT / "services/literature-kb/frontend"
    process = subprocess.Popen(["node", str(frontend / "node_modules/vite/bin/vite.js"), "--host", "127.0.0.1", "--port", str(port), "--strictPort"], cwd=frontend, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                if httpx.get(url).status_code == 200:
                    break
            except httpx.ConnectError:
                pass
            assert process.poll() is None, "Vite exited"
            time.sleep(.1)
        else:
            pytest.fail("Vite did not start")
        yield url
    finally:
        process.terminate()
        process.wait(timeout=10)


@pytest.fixture(scope="module")
def pdf_bytes():
    buffer = io.BytesIO()
    with PdfPages(buffer) as document:
        for page in (1, 2):
            figure = Figure(figsize=(6.3, 8.8))
            figure.text(.10, .92, "Electrolyte design for stable batteries", fontsize=16, weight="bold")
            figure.text(.10, .87, "SYNTHETIC UI FIXTURE / NOT RESEARCH DATA", fontsize=9, color="#61666b")
            figure.text(.10, .81, "Results and discussion" if page == 2 else "Methods and characterization", fontsize=13)
            figure.text(.10, .76, "The formulation was evaluated under controlled test conditions.\nThe selected evidence is located on this page.", fontsize=10, linespacing=1.8)
            axis = figure.subplots()
            axis.set_position([.15, .30, .72, .34])
            axis.plot([0, 20, 40, 60, 80, 100], [100, 98, 96, 95, 94, 93], color="#4176e6", label="Sample A")
            axis.plot([0, 20, 40, 60, 80, 100], [100, 95, 91, 88, 84, 80], color="#9ca3af", label="Reference")
            axis.set(xlabel="Cycle number", ylabel="Capacity retention (%)")
            axis.spines[["top", "right"]].set_visible(False)
            axis.legend(frameon=False)
            figure.text(.10, .20, "Figure 1. Synthetic illustration for interface verification.", fontsize=10)
            figure.text(.48, .06, str(page), fontsize=10)
            document.savefig(figure)
    return buffer.getvalue()


@pytest.fixture
def browser_page(literature_ui, pdf_bytes):
    paper = {"paper_id": "p1", "title": "Stable electrolyte interfaces for lithium-metal batteries", "year": 2025, "venue": "Synthetic Research", "doi": "10.0000/ui-fixture", "authors": [{"name": "Demo Team"}], "abstract": "Synthetic sample for workbench interaction testing.", "processing_status": {"parse": "ready", "embedding": "ready"}, "capabilities": {"ai_ready": True, "can_search_evidence": True}, "asset_counts": {"pdf": 1, "figure": 1}}
    papers = [paper, {**paper, "paper_id": "p2", "title": "Interfacial stability under controlled cycling", "year": 2026}, {**paper, "paper_id": "p3", "title": "Evidence-guided materials discovery", "year": 2024}]
    requests = []
    def route_api(route):
        path = urlsplit(route.request.url).path
        body = route.request.post_data_json if "application/json" in route.request.headers.get("content-type", "") else None
        requests.append((path, body))
        result = {}
        if path == "/auth/session": result = {"authenticated": True}
        elif path.endswith("/system/health"): result = {"status": "healthy", "components": []}
        elif path.endswith("/library/overview"): result = {"papers": 3, "searchable": 3, "ai_ready": 3}
        elif path.endswith("/discovery/sources"): result = {"items": [{"source": "openalex", "role": "search_and_resolve", "configured": True}]}
        elif path.endswith("/papers/search"): result = {"items": papers, "pagination": {"total": 3}}
        elif path.endswith("/literature/search"): result = {"items": [{"identity_key": "remote_1", "metadata": {"title": "Remote synthetic paper", "year": 2026}, "availability": "remote_only", "remote": {"source": "openalex", "external_id": "W1", "downloadable": True}, "remote_options": [{"source": "openalex", "external_id": "W1", "downloadable": True}]}]}
        elif path.endswith("/literature/imports"): result = {"batch_id": "import_1", "status": "succeeded", "items": [{"paper_id": "p4"}]}
        elif path == "/api/v1/papers" and route.request.method == "POST": result = {"task": {"task_id": "upload_task", "status": "succeeded", "operation": "ingest"}, "batch_id": "batch_1", "item_id": "item_1"}
        elif path.endswith("/tasks/upload_task"): result = {"task_id": "upload_task", "status": "succeeded", "progress": 1}
        elif path.endswith("/evidence/search"):
            result = {"items": [{"evidence_id": "ev2", "paper": papers[1], "content": {"snippet": "界面稳定性随配方与循环条件变化；测试条件与结果应结合原文核对。"}, "location": {"page": 2, "section_title": "Results and discussion"}, "score": .928, "assets": [{"asset_id": "figure2"}]}]}
        elif path.endswith("/evidence/ev2/content"): result = {"evidence_id": "ev2", "paper": papers[1], "content": {"text": "CONTEXT: controlled cycling conditions and interfacial stability.", "evidence_char_start": 20, "evidence_char_end": 40}, "location": {"page": 2, "section_title": "Results and discussion"}}
        elif path.endswith("/evidence/ev2/source"): result = {"citation": {"evidence_id": "ev2", "paper": papers[1], "quote": "界面稳定性随配方与循环条件变化。", "location": {"page": 2, "bbox": [.08, .20, .92, .30], "coordinate_space": "normalized"}}, "pdf_asset_id": "pdf2"}
        elif path.endswith("/evidence/packs"): result = {"run_id": "pack1", "status": "queued"}
        elif path.endswith("/citations/ground"): result = {"run_id": "ground1", "status": "queued"}
        elif path.endswith("/workflows/pack1"): result = {"run_id": "pack1", "status": "succeeded", "result": {"claims": [{"claim_id": "c1", "claim": "Test claim", "evidence": [{"evidence_id": "ev2", "verdict": "supported", "confidence": .92, "quote": "Verified synthetic claim."}]}]}}
        elif path.endswith("/workflows/ground1"): result = {"run_id": "ground1", "status": "succeeded", "result": {"grounded_answer": "Grounded synthetic draft [1]."}}
        elif "/papers/p" in path:
            selected = next(p for p in papers if p["paper_id"] == path.split("/")[4])
            n = selected["paper_id"][-1]
            if path.endswith("/content"):
                more = "offset=0" in route.request.url
                result = {"paper": selected, "content": {"text": "FIRST CONTENT WINDOW\n" if more else "SECOND CONTENT WINDOW", "more": more, "next_offset": 6000 if more else None}}
            elif path.endswith("/assets"): result = {"items": [{"asset_id": f"pdf{n}", "asset_type": "pdf"}, {"asset_id": f"figure{n}", "asset_type": "figure", "mime_type": "image/svg+xml", "caption": "Cycling performance", "location": {"page": 2}}]}
            else: result = selected
        elif path.endswith("/download"):
            if "/pdf" in path:
                route.fulfill(status=200, body=pdf_bytes, headers={"Content-Type": "application/pdf", "Access-Control-Allow-Origin": route.request.headers.get("origin", literature_ui), "Access-Control-Allow-Credentials": "true"})
            else:
                route.fulfill(status=200, body='<svg xmlns="http://www.w3.org/2000/svg" width="400" height="220"><rect width="400" height="220" fill="#f5f6f7"/><path d="M30 180 110 130 190 90 280 70 360 40" stroke="#4176e6" fill="none" stroke-width="3"/></svg>', headers={"Content-Type": "image/svg+xml", "Access-Control-Allow-Origin": "*"})
            return
        elif "/assets/figure" in path: result = {"asset_id": path.rsplit("/", 1)[1], "asset_type": "figure", "mime_type": "image/svg+xml"}
        else:
            route.fulfill(status=404, json={"detail": f"Unmocked API: {path}"}, headers={"Access-Control-Allow-Origin": "*"})
            return
        route.fulfill(status=200, json=result, headers={"Access-Control-Allow-Origin": route.request.headers.get("origin", literature_ui), "Access-Control-Allow-Credentials": "true"})
    with sync_playwright() as pw:
        channel = os.environ.get("GIBBS_BROWSER_CHANNEL")
        browser = pw.chromium.launch(headless=True, **({"channel": channel} if channel else {}))
        page = browser.new_page(viewport={"width": 1600, "height": 1000}, reduced_motion="reduce")
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route("**/api/v1/**", route_api)
        page.route("**/auth/**", route_api)
        page.goto(literature_ui)
        yield page, requests
        browser.close()
        assert errors == []


def rendered_pdf(page):
    page.wait_for_function('document.querySelector(".pdf-scroll canvas")?.style.visibility === "visible"')
    expect(page.locator(".pdf-scroll [role=alert]")).to_have_count(0)


def screenshot(page, name):
    if directory := os.environ.get("GIBBS_UI_SCREENSHOTS"):
        target = Path(directory)
        target.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(target / name), full_page=True)


def test_three_pane_reading_and_evidence(browser_page):
    page, requests = browser_page
    expect(page.locator(".paper-row")).to_have_count(3)
    for selector in (".workbench-library", ".workbench-document", ".workbench-evidence"):
        expect(page.locator(selector)).to_be_visible()
    page.locator(".paper-main").first.click()
    rendered_pdf(page)
    page.get_by_role("button", name="PDF 下一页").click()
    expect(page.get_by_role("spinbutton", name="PDF 页码")).to_have_value("2")
    rendered_pdf(page)
    page.get_by_role("button", name="放大", exact=True).click()
    expect(page.get_by_role("button", name="适合宽度")).to_have_text("125%")
    rendered_pdf(page)
    page.get_by_role("button", name="适合宽度").click()
    page.get_by_role("button", name="正文", exact=True).click()
    expect(page.locator(".reading-text")).to_contain_text("FIRST CONTENT WINDOW")
    page.get_by_role("button", name="继续阅读").click()
    expect(page.locator(".reading-text")).to_contain_text("SECOND CONTENT WINDOW")
    page.get_by_role("button", name="图表", exact=True).click()
    page.get_by_role("button", name="Cycling performance").click()
    expect(page.locator(".document-body img")).to_be_visible()
    page.get_by_label("检索问题").fill("电解液界面稳定性")
    page.get_by_role("button", name="检索证据", exact=True).click()
    expect(page.locator(".hit")).to_have_count(1)
    page.get_by_role("button", name="阅读正文", exact=True).first.click()
    expect(page.locator(".document-heading")).to_contain_text("Interfacial stability")
    expect(page.locator(".reading-text")).to_contain_text("CONTEXT: controlled cycling")
    page.get_by_role("button", name="定位原文", exact=True).click()
    rendered_pdf(page)
    expect(page.get_by_role("spinbutton", name="PDF 页码")).to_have_value("2")
    expect(page.locator(".paper-row.selected")).to_contain_text("Interfacial stability")
    page.get_by_label("检索范围").select_option("selected")
    page.get_by_role("button", name="检索证据", exact=True).click()
    expect(page.locator(".hit")).to_have_count(1)
    assert [body for path, body in requests if path.endswith("/evidence/search")][-1]["scope"] == {"paper_ids": ["p2"]}
    assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
    screenshot(page, "literature-workbench-light.png")
    page.emulate_media(color_scheme="dark")
    screenshot(page, "literature-workbench-dark.png")
    assert page.locator("body").evaluate("el => getComputedStyle(el).backgroundColor") == "rgb(21, 21, 23)"
    page.emulate_media(color_scheme="light")
    page.get_by_role("button", name="论断核验", exact=True).click()
    page.get_by_label("待核验论断").fill("Test claim")
    page.get_by_role("button", name="核验论断", exact=True).click()
    expect(page.locator(".pack-list")).to_contain_text("Verified synthetic claim")
    page.get_by_role("button", name="引用校验", exact=True).click()
    page.get_by_label("待校验草稿").fill("Synthetic draft")
    page.get_by_role("button", name="校验引用", exact=True).click()
    expect(page.locator(".ground-list")).to_contain_text("Grounded synthetic draft")
    page.get_by_role("button", name="证据", exact=True).click()
    page.set_viewport_size({"width": 1000, "height": 800})
    expect(page.locator(".workbench-library")).to_be_visible()
    page.get_by_role("button", name="证据", exact=True).first.click()
    expect(page.locator(".workbench-evidence")).to_be_visible()
    page.set_viewport_size({"width": 390, "height": 844})
    screenshot(page, "literature-workbench-mobile-evidence.png")
    page.get_by_role("button", name="原文", exact=True).click()
    rendered_pdf(page)
    screenshot(page, "literature-workbench-mobile.png")
    assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
    assert page.locator(".workbench-document").bounding_box()["height"] > 600


def test_catalog_filters_remote_import_and_upload(browser_page, pdf_bytes):
    page, requests = browser_page
    page.get_by_label("起始年").fill("2020")
    page.get_by_placeholder("搜索标题、摘要或 DOI").fill("interfaces")
    with page.expect_response("**/papers/search"):
        page.get_by_role("button", name="搜索", exact=True).click()
    assert [body for path, body in requests if path.endswith("/papers/search")][-1]["filters"]["year"]["gte"] == 2020
    with page.expect_response("**/papers/search"):
        page.get_by_role("button", name="重置筛选").click()
    assert [body for path, body in requests if path.endswith("/papers/search")][-1]["filters"] == {}
    page.get_by_label("文献来源").select_option("openalex")
    page.get_by_placeholder("搜索标题、摘要或 DOI").fill("materials")
    page.get_by_role("button", name="搜索", exact=True).click()
    expect(page.locator(".paper-list")).to_contain_text("Remote synthetic paper")
    page.get_by_role("button", name="Import →").click()
    expect(page.get_by_role("status")).to_contain_text("论文已导入")
    page.get_by_role("button", name="上传 PDF", exact=True).click()
    page.locator('input[type=file]').set_input_files({"name": "synthetic.pdf", "mimeType": "application/pdf", "buffer": pdf_bytes})
    page.get_by_role("button", name="Upload and process").click()
    expect(page.locator(".upload-modal")).to_have_count(0)
    assert any(path == "/api/v1/papers" for path, _ in requests)


def test_late_evidence_does_not_replace_new_paper(browser_page):
    page, _ = browser_page
    page.locator(".paper-main").first.click()
    rendered_pdf(page)
    page.get_by_label("检索问题").fill("interfacial stability")
    page.get_by_role("button", name="检索证据", exact=True).click()
    expect(page.locator(".hit")).to_have_count(1)
    page.evaluate("""() => {
      const original = window.fetch;
      window.fetch = async (...args) => {
        if (String(args[0]).includes('/evidence/ev2/content')) {
          await new Promise(resolve => { window.releaseEvidence = resolve; });
        }
        return original(...args);
      };
    }""")
    page.get_by_role("button", name="阅读正文", exact=True).first.click()
    page.wait_for_function("typeof window.releaseEvidence === 'function'")
    page.locator(".paper-main").nth(2).click()
    rendered_pdf(page)
    with page.expect_response("**/evidence/ev2/content?*"):
        page.evaluate("window.releaseEvidence()")
    # The visible selection is retained when an earlier evidence request completes.
    expect(page.locator(".document-heading")).to_contain_text("Evidence-guided materials discovery")
    expect(page.locator(".paper-row.selected")).to_contain_text("Evidence-guided materials discovery")
    expect(page.get_by_role("button", name="证据正文", exact=True)).to_have_count(0)
    page.get_by_label("选择 Evidence-guided materials discovery").check()
    page.get_by_label("检索范围").select_option("custom")
    page.get_by_role("button", name="检索证据", exact=True).click()
    expect(page.locator(".hit")).to_have_count(1)
    # A global resource hit must open its own paper, even without opening its context first.
    page.locator(".hit").get_by_role("button", name="查看资源").click()
    expect(page.locator(".document-heading")).to_contain_text("Interfacial stability")
    expect(page.locator(".document-body img")).to_be_visible()
