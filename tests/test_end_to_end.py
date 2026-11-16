import os
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from conftest import running


def test_complete_browser_customer_to_order_and_visible_receipts(database, tmp_path):
    from iew.__main__ import seed
    from iew.api import WorkbenchServer
    from iew.delivery import Delivery
    from iew.simulator import Simulator, SimulatorServer
    from iew.store import Store

    store, simulator = Store(database[0]), Simulator(database[1])
    store.initialize()
    simulator.initialize()
    seed(store, simulator, tmp_path, "test password only")
    token = "isolated-test-service-token"
    with running(SimulatorServer(("127.0.0.1", 0), simulator, token)) as destination:
        delivery = Delivery(store, database[2], destination, token)
        delivery.initialize()
        with running(WorkbenchServer(("127.0.0.1", 0), store, delivery)) as url, sync_playwright() as engines:
            browser = engines.chromium.launch(channel="chrome")
            page = browser.new_page(viewport={"width": 1280, "height": 850}, accept_downloads=True)
            failures = []
            page.on("pageerror", lambda error: failures.append(str(error)))
            page.goto(url)

            def login(user):
                page.get_by_label("User name").fill(user)
                page.get_by_label("Password").fill("test password only")
                page.get_by_label("Password").press("Enter")
                expect(page.get_by_role("heading", name="Failure queue")).to_be_focused()

            def draft(kind, key, evidence):
                login("analyst")
                page.get_by_role("button", name="Import", exact=True).click()
                folder = tmp_path / kind
                page.get_by_label("Failed entities CSV").set_input_files(folder / (kind + "s.csv"))
                page.get_by_label("Error manifest").set_input_files(folder / "errors.json")
                page.get_by_role("button", name="Validate bundle").click()
                expect(page.get_by_role("heading", name="Committed · 1 case")).to_be_visible()
                page.get_by_role("button", name="Failure queue", exact=True).click()
                page.locator(".case-list button").filter(has_text=key).click()
                page.get_by_role("button", name="Claim case", exact=True).click()
                expect(page.get_by_text("Claim saved.")).to_be_visible()
                page.get_by_role("button", name="Correction", exact=True).click()
                if kind == "order":
                    page.get_by_role("group", name="Order line 1").get_by_label("Item reference").fill("ITEM-1")
                page.get_by_label("Reason", exact=True).fill("Verified against an approved reference")
                page.get_by_label(evidence, exact=True).check()
                page.get_by_role("button", name="Validate and submit preview").click()
                expect(page.get_by_text("Preview submitted for peer approval.")).to_be_visible()
                page.get_by_role("button", name="Sign out").click()

            def deliver(key, before):
                login("approver")
                page.get_by_role("button", name="Approval inbox", exact=True).click()
                expect(page.get_by_role("heading", name="Approval inbox")).to_be_focused()
                page.locator(".case-list button").filter(has_text=key).click()
                page.get_by_role("button", name="Approve revision", exact=True).click()
                dialog = page.get_by_role("dialog")
                expect(dialog.get_by_role("button", name="Cancel")).to_be_focused()
                dialog.press("Escape")
                expect(page.get_by_role("button", name="Approve revision", exact=True)).to_be_focused()
                page.get_by_role("button", name="Approve revision", exact=True).click()
                dialog.get_by_role("button", name="Approve", exact=True).click()
                expect(page.get_by_text("Approval recorded.")).to_be_visible()
                assert simulator.effect_count() == before
                page.locator(".tabs").get_by_role("button", name="Delivery", exact=True).click()
                send = page.get_by_role("button", name="Send approved revision")
                expect(send).to_be_disabled()
                page.get_by_role("button", name="Reconcile evidence").click()
                expect(send).to_be_enabled()
                send.click()
                dialog.get_by_role("button", name="Send", exact=True).click()
                expect(page.get_by_text("Delivery queued.")).to_be_visible()
                assert delivery.tick()
                page.get_by_role("button", name="Refresh case").click()
                expect(page.get_by_text("Confirmed receipt")).to_be_visible()
                assert simulator.effect_count() == before + 1

            draft("customer", "C100", "steward-C100")
            deliver("C100", 0)
            page.get_by_role("button", name="Sign out").click()
            draft("order", "O200", "ITEM-1")
            deliver("O200", 1)
            with simulator.connect() as db:
                result = db.execute("SELECT payload FROM sim_effect WHERE identity->>'kind'='order'").fetchone()
            assert result is not None
            assert [(line["line_no"], line["quantity"], line["item_reference"]) for line in result["payload"]["lines"]] == [
                (1, "2", "ITEM-1"), (2, "3", "ITEM-1")
            ]
            output = os.getenv("IEW_BROWSER_OUTPUT")
            if output:
                page.screenshot(path=Path(output) / "confirmed-order.png", full_page=True)
            assert failures == []
            browser.close()
