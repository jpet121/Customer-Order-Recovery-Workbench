import base64
import os
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright, expect

from conftest import CUSTOMER, bundle


@pytest.mark.parametrize("channel", ["chrome", "msedge"])
def test_keyboard_sign_in_import_select_evidence_and_download(application, tmp_path, channel):
    url, _ = application
    with sync_playwright() as engines:
        browser = engines.chromium.launch(channel=channel)
        page = browser.new_page(viewport={"width": 1280, "height": 850}, accept_downloads=True)
        page.goto(url)
        page.get_by_label("User name").fill("analyst")
        page.get_by_label("Password").fill("test password only")
        page.get_by_label("Password").press("Enter")
        expect(page.get_by_role("heading", name="Failure queue")).to_be_focused()
        page.get_by_role("button", name="Import").click()
        page.get_by_label("Failed entities CSV").set_input_files(
            {"name":"customers.csv","mimeType":"text/csv","buffer": CUSTOMER})
        page.get_by_label("Error manifest").set_input_files(
            {"name":"errors.json","mimeType":"application/json","buffer":base64.b64decode(bundle(CUSTOMER)["manifest"])})
        page.get_by_role("button", name="Validate bundle").click()
        expect(page.get_by_text("Committed · 1 case")).to_be_visible()
        with page.expect_download() as event:
            page.get_by_role("link", name="Download original CSV").click()
        assert event.value.path().read_bytes() == CUSTOMER
        page.get_by_role("button", name="Failure queue").click()
        page.get_by_role("button", name="C100 Customer").click()
        expect(page.get_by_role("heading", name="C100", exact=True)).to_be_focused()
        page.get_by_role("button", name="Claim case").click()
        expect(page.get_by_text("Claim saved.")).to_be_visible()
        page.get_by_role("button", name="Correction", exact=True).click()
        expect(page.get_by_label("Customer reference")).to_have_attribute("placeholder", "REF-100")
        page.get_by_label("steward-C100").check()
        expect(page.get_by_label("steward-C100")).to_be_checked()
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        destination = os.getenv("IEW_BROWSER_OUTPUT")
        if destination:
            path = Path(destination)
            path.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=path / f"{channel}-390.png", full_page=True)
            page.set_viewport_size({"width": 1280, "height": 850})
            page.screenshot(path=path / f"{channel}-1280.png", full_page=True)
            (path / f"{channel}.txt").write_text(browser.version + "\n")
        browser.close()
