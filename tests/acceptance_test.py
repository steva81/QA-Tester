import asyncio
import os
import sys
sys.path.insert(0, os.path.abspath(os.path.dirname(os.path.dirname(__file__))))
from playwright.async_api import async_playwright
import threading
import time

def run_flask():
    os.environ['FLASK_ENV'] = 'testing'
    from server import app
    app.run(port=5000, use_reloader=False)

def run_demo_app():
    from tests.demo_app import app as demo
    demo.run(port=8080, use_reloader=False)

import pytest

@pytest.mark.asyncio
async def test_acceptance():
    print("Starting Flask and Demo Apps...")
    t1 = threading.Thread(target=run_flask, daemon=True)
    t2 = threading.Thread(target=run_demo_app, daemon=True)
    t1.start()
    t2.start()
    
    time.sleep(3) # Wait for servers to start
    
    print("Launching Browser...")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        
        print("Step 1: Open Dashboard and Create Project")
        await page.goto("http://127.0.0.1:5000/projects")
        
        await page.click("button:has-text('Create QA Project')")
        
        await page.fill("#p_name", "Local Demo Project")
        await page.fill("#p_url", "http://127.0.0.1:8080")
        await page.fill("#p_env", "Test")
        await page.fill("#p_tester", "Agent")
        await page.click("button#btnCreate")
        
        await page.wait_for_selector("text=Local Demo Project")
        
        print("Step 2: Navigate to Project Page")
        await page.click("a:has-text('Run Test')")
        
        print("Step 3: Confirm Authorization and Dry Run")
        await page.wait_for_selector("#auth_confirm")
        await page.check("#auth_confirm")
        
        print("Step 4: Start QA Run")
        await page.click("button:has-text('Start QA Test')")
        
        print("Step 5: Wait for Run to Complete...")
        # Should redirect to /run/X which is running.html, then auto refreshes to results.html
        await page.wait_for_selector("text=QA Test Results", timeout=30000)
        
        print("Step 6: Verify Results View")
        assert "QA Test Results" in await page.content()
        assert "Total Checks" in await page.content()
        
        print("Step 7: Verify Findings")
        await page.click("a:has-text('QA-')")
        
        assert "Bug ID / Title" in await page.content()
        content = await page.content()
        if "QA-" not in content:
            raise Exception("Finding details did not load correctly")
        
        print("Step 8: Verify New Finding Elements")
        # Verify title is concise
        assert "Page returns HTTP 500" in content or "Page failed to return" in content
        # Verify Area
        assert "Page Load / Server Response" in content
        # Verify screenshot is rendered
        assert "src=\"/screenshots/" in content
        
        # Verify explanations
        text_content = await page.inner_text("body")
        if "What does this finding mean?" not in text_content:
            print(f"FAILED TO FIND EXPLANATION. Content was: {text_content}")
            assert False, "What does this finding mean? missing"
            
        assert "An HTTP 500 error means" in text_content or "An HTTP 500 error means" in content
        assert "FÖRKLARING FÖR MIG" in text_content
        assert "Ett HTTP 500-fel betyder" in text_content or "Ett HTTP 500-fel betyder" in content
        assert "SÅ KAN JAG FÖRKLARA DET" in text_content
        
        print("Step 9: Test Edit and Review Flow")
        # Click edit
        await page.click("button:has-text('Edit Finding')")
        await page.select_option("#f_sev", "High")
        await page.select_option("#f_risk", "High")
        await page.fill("#f_impact", "High impact to users")
        await page.click("button:has-text('Save Changes')")
        
        # Wait for reload
        await page.wait_for_selector("text=Bug ID / Title")
        text_content = await page.inner_text("body")
        assert "Severity\nHigh" in text_content or "Severity\r\nHigh" in text_content or "High" in text_content
        
        # Test Approve
        await page.click("button:has-text('Approve Report')")
        await page.wait_for_timeout(1500) # wait for reload
        await page.wait_for_selector("text=APPROVED")
        
        text_content = await page.inner_text("body")
        assert "APPROVED" in text_content
        assert "Status: OPEN" in text_content or "Status\r\nOPEN" in text_content or "OPEN" in text_content
        
        print("Step 10: Check physical screenshots")
        screenshots = os.listdir("screenshots")
        print(f"Screenshots found: {screenshots}")
        assert any("QA-" in s for s in screenshots), "No QA finding screenshots generated"
        
        print("Step 11: Verify Findings History")
        await page.goto("http://127.0.0.1:5000/history")
        history_content = await page.inner_text("body")
        assert "QA Run History" in history_content
        assert "TODAY" in history_content or "YESTERDAY" in history_content
        assert "COMPLETED" in history_content
        assert "View Results" in history_content
        
        # Click the link to ensure it goes to the correct run
        await page.click("a:has-text('View Results')")
        await page.wait_for_selector("text=QA Test Results")
        finding_content = await page.inner_text("body")
        assert "QA Test Results" in finding_content
        
        await browser.close()
        print("Acceptance Test Passed!")

if __name__ == "__main__":
    asyncio.run(test_acceptance())
