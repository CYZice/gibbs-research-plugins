"""Browser checks against an isolated local workspace, not user artifacts."""
import os
import socket
import subprocess
import sys
import time

import httpx
import pytest
from playwright.sync_api import expect, sync_playwright

pytestmark = pytest.mark.skipif(not os.environ.get("GIBBS_TEST_UI"), reason="Set GIBBS_TEST_UI=1 with installed Chromium or Edge")


@pytest.fixture(scope="module")
def ui_service(tmp_path_factory):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = dict(os.environ, GIBBS_WORKSPACE=str(tmp_path_factory.mktemp("preview-ui")))
    env.pop("GIBBS_TOOLS_TOKEN", None)
    process = subprocess.Popen([sys.executable, "-m", "gibbs_research.cli", "--port", str(port)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                if httpx.get(url + "/health").status_code == 200:
                    break
            except httpx.ConnectError:
                pass
            assert process.poll() is None, "Preview test service exited"
            time.sleep(.1)
        else:
            pytest.fail("Preview test service did not start")
        yield url
    finally:
        process.terminate()
        process.wait(timeout=10)


@pytest.fixture
def page(ui_service):
    with sync_playwright() as pw:
        channel = os.environ.get("GIBBS_BROWSER_CHANNEL")
        browser = pw.chromium.launch(headless=True, **({"channel": channel} if channel else {}))
        page = browser.new_page(viewport={"width": 1440, "height": 900}, reduced_motion="reduce")
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        yield page
        browser.close()
        assert errors == []


def wait_plot(page):
    page.wait_for_function('document.querySelector("#preview > img")?.naturalWidth > 0')
    expect(page.locator("#save")).to_be_enabled()


def test_plot_icons_revision_feedback_and_mobile(page, ui_service):
    page.goto(ui_service)
    expect(page.get_by_role("button", name="生成预览", exact=True)).to_be_enabled()
    assert page.locator("header").count() == 0
    assert page.locator("#status").inner_text() == ""
    page.locator("#editor").fill("{")
    page.get_by_role("button", name="生成预览", exact=True).click()
    expect(page.locator("#status")).to_have_class("error")
    page.get_by_role("button", name="新建图表").click()
    page.get_by_role("button", name="生成预览", exact=True).click()
    wait_plot(page)
    assert not page.locator("#source-editor").evaluate("el => el.open")
    page.locator("#font-size").fill("18")
    page.locator("#legend").select_option("upper right")
    page.get_by_role("button", name="保存并重画", exact=True).click()
    expect(page.locator("#history")).to_have_value("2")
    wait_plot(page)
    page.get_by_role("button", name="对比上一版").click()
    expect(page.locator(".comparison img")).to_have_count(2)
    expect(page.locator("#compare")).to_have_attribute("aria-pressed", "true")
    page.get_by_role("button", name="对比上一版").click()
    wait_plot(page)
    page.locator("#history").select_option("1")
    expect(page.locator("#font-size")).to_have_value("12")
    expect(page.locator("#compare")).to_be_hidden()
    page.locator("#history").select_option("2")
    expect(page.locator("#font-size")).to_have_value("18")
    page.locator("#download summary").click()
    expect(page.get_by_role("link", name="SVG", exact=True)).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.get_by_role("link", name="SVG", exact=True)).to_be_hidden()
    page.locator("#feedback").fill("保留数据，图例放右上角")
    page.get_by_role("button", name="交给 Agent 修改").click()
    expect(page.locator("#status")).to_have_text("已提交，回到 Agent 继续修改。")
    feedback = httpx.get(ui_service + "/api/feedback").json()
    assert any(f["text"] == "保留数据，图例放右上角" for f in feedback)
    page.emulate_media(color_scheme="dark")
    assert page.locator("body").evaluate("el => getComputedStyle(el).color") == "rgb(249, 250, 251)"
    page.emulate_media(color_scheme="light")
    page.set_viewport_size({"width": 390, "height": 844})
    assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
    assert page.locator("aside").bounding_box()["height"] < 70
    page.get_by_role("button", name="新建图表").click()
    expect(page.locator("#compare")).to_be_hidden()
    expect(page.locator("#history")).to_be_hidden()
    expect(page.locator("#download")).to_be_hidden()
    expect(page.get_by_role("button", name="交给 Agent 修改")).to_be_disabled()


def test_report_uses_same_shell_and_keeps_editing(page, ui_service):
    material = {"title": "UI synthetic report", "results": [{"label": "Sample", "metrics": [{"name": "response", "label": "Response", "value": 2.5}]}]}
    draft = {"abstract": "Synthetic UI sample.", "background": "Test the report editor.", "methods": [{"title": "Input", "prose": "Supplied synthetic result."}], "results_by_system": [{"title": "Sample", "prose": "Response 2.5."}], "comparison": "Single series.", "discussion": "Synthetic interface test.", "conclusions": ["Preview works."], "recommendations": ["Review input."]}
    response = httpx.post(ui_service + "/api/reports", json={"material": material, "draft": draft})
    assert response.status_code == 200, response.text
    item = response.json()
    page.goto(ui_service + f"/?artifact={item['artifact_id']}")
    expect(page.frame_locator("iframe").locator("h1")).to_have_text(material["title"])
    expect(page.locator("#plot-controls")).to_be_hidden()
    expect(page.locator("#compare")).to_be_hidden()
    page.locator("#source-editor summary").click()
    data = page.locator("#editor").input_value().replace("Synthetic UI sample.", "Edited synthetic sample.")
    page.locator("#editor").fill(data)
    page.get_by_role("button", name="保存报告", exact=True).click()
    expect(page.locator("#history")).to_have_value("2")
    expect(page.frame_locator("iframe").locator("body")).to_contain_text("Edited synthetic sample.")
    page.locator("#download summary").click()
    assert [text.strip() for text in page.locator("#exports a").all_text_contents()] == ["HTML", "MD", "JSON", "PDF"]
