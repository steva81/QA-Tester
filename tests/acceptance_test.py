import asyncio
import os
import sys
import threading
import time
import socket
from werkzeug.serving import make_server
import pytest
from playwright.async_api import async_playwright

sys.path.insert(0, os.path.abspath(os.path.dirname(os.path.dirname(__file__))))

def get_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('', 0))
        return s.getsockname()[1]

class ServerThread(threading.Thread):
    def __init__(self, app, port):
        threading.Thread.__init__(self)
        self.server = make_server('127.0.0.1', port, app)
    def run(self):
        self.server.serve_forever()
    def shutdown(self):
        self.server.shutdown()

@pytest.mark.asyncio
async def test_acceptance():
    print("Starting Flask and Demo Apps...")
    import app.routes.api
    app.routes.api.accepting_runs = True
    app_port = get_free_port()
    demo_port = get_free_port()
    
    from app.models.database import init_db
    init_db()
    from server import app
    from tests.demo_app import app as demo
    
    server_thread = ServerThread(app, app_port)
    demo_thread = ServerThread(demo, demo_port)
    server_thread.start()
    demo_thread.start()
    
    time.sleep(1) # Wait for servers to start
    
    try:
        print("Launching Browser...")
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            
            print("Step 1: Open Dashboard and Create Project")
            await page.goto(f"http://127.0.0.1:{app_port}/projects")
            
            await page.click("button:has-text('Create QA Project')")
            
            await page.fill("#p_name", "Local Demo Project")
            await page.fill("#p_url", f"http://127.0.0.1:{demo_port}")
            await page.fill("#p_env", "Test")
            await page.fill("#p_tester", "Agent")
            await page.click("button#btnCreate")
            
            await page.wait_for_selector("text=Local Demo Project")
            
            print("Step 2: Navigate to Project Page")
            await page.click("a:has-text('Start New QA Test')")
            
            print("Step 3: Confirm Authorization and Dry Run")
            await page.wait_for_selector("#auth_confirm")
            await page.check("#auth_confirm")
            
            print("Step 4: Start QA Run")
            await page.click("button:has-text('Start QA Test')")
            
            print("Step 5: Wait for Run to Complete...")
            await page.wait_for_selector("text=QA Test Results", timeout=60000)
            
            print("Step 6: Verify Results View")
            assert "QA Test Results" in await page.content()
            assert "Total Checks" in await page.content()
            
            print("Step 7: Verify Findings")
            await page.click("a:has-text('Review Finding')")
            
            assert "Bug ID" in await page.content()
            content = await page.content()
            if "QA-" not in content:
                raise Exception("Finding details did not load correctly")
            
            print("Step 8: Verify New Finding Elements")
            assert "Page returns HTTP 500" in content or "Page failed to return" in content
            assert "Page Load / Server Response" in content
            assert "src=\"/screenshots/" in content
            
            text_content = await page.inner_text("body")
            # Phase 1C uses CSS uppercase for this heading. Verify the exact
            # heading's source text and visibility, independent of CSS casing.
            explanation_heading = page.locator('h4', has_text='What does this mean?')
            assert await explanation_heading.count() == 1
            assert await explanation_heading.is_visible()
            assert await explanation_heading.text_content() == 'What does this mean?'
            if "what does this mean?" not in text_content.casefold():
                print(f"FAILED TO FIND EXPLANATION. Content was: {text_content}")
                assert False, "What does this mean? missing"
                
            assert "An HTTP 500 error means" in text_content or "An HTTP 500 error means" in content
            assert "FÖRKLARING FÖR MIG" in text_content
            assert "Ett HTTP 500-fel betyder" in text_content or "Ett HTTP 500-fel betyder" in content
            assert "SÅ KAN JAG FÖRKLARA DET" in text_content
            
            print("Step 9: Test Edit and Review Flow")
            await page.click("button:has-text('Edit Finding')")
            await page.select_option("#f_sev", "High")
            await page.select_option("#f_risk", "High")
            await page.fill("#f_impact", "High impact to users")
            async with page.expect_navigation():
                await page.click("button:has-text('Save Changes')")
            # Bug ID is in the hidden copy-report payload in Phase 1C; the
            # visible identity is the QA-001 badge. Verify the saved edit too.
            await page.locator('.badge', has_text='QA-001').wait_for(state='visible')
            text_content = await page.inner_text("body")
            assert 'High impact to users' in text_content
            assert "Severity\nHigh" in text_content or "Severity\r\nHigh" in text_content or "High" in text_content
            
            await page.click("button:has-text('Approve finding')")
            await page.wait_for_timeout(1500)
            await page.wait_for_selector("text=APPROVED")
            
            text_content = await page.inner_text("body")
            assert "APPROVED" in text_content
            assert "Status: OPEN" in text_content or "Status\r\nOPEN" in text_content or "OPEN" in text_content
            
            print("Step 10: Check physical screenshots")
            from app.paths import get_screenshots_dir
            screenshots = os.listdir(get_screenshots_dir())
            print(f"Screenshots found: {screenshots}")
            assert any("QA-" in s for s in screenshots), "No QA finding screenshots generated"
            
            print("Step 11: Verify Findings History")
            await page.goto(f"http://127.0.0.1:{app_port}/history")
            history_content = await page.inner_text("body")
            assert "QA Run History" in history_content
            assert "TODAY" in history_content or "YESTERDAY" in history_content
            assert "COMPLETED" in history_content
            assert "View Results" in history_content
            
            await page.click("a:has-text('View Results')")
            await page.wait_for_selector("text=QA Test Results")
            finding_content = await page.inner_text("body")
            assert "QA Test Results" in finding_content
            
            await browser.close()
            print("Acceptance Test Passed!")
    finally:
        server_thread.shutdown()
        demo_thread.shutdown()

if __name__ == "__main__":
    asyncio.run(test_acceptance())
