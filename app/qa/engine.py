import asyncio
from playwright.async_api import async_playwright
import time
from urllib.parse import urlparse, urljoin
import os
import datetime

class SmartSmokeQA:
    def __init__(self, target_url, project_id, run_id, max_pages=25, max_depth=3):
        self.target_url = target_url
        self.project_id = project_id
        self.run_id = run_id
        self.max_pages = max_pages
        self.max_depth = max_depth
        
        parsed = urlparse(target_url)
        self.target_origin = f"{parsed.scheme}://{parsed.netloc}"
        
        self.visited_urls = set()
        self.queue = [(self.target_url, 0)]  # (url, depth)
        self.results = []
        self.is_stopped = False
        
        self.finding_counter = 1
        
        # Phase 1 Viewports
        self.viewports = [
            {'name': 'Desktop', 'width': 1366, 'height': 768},
            {'name': 'Mobile', 'width': 390, 'height': 844}
        ]

    def is_same_origin(self, url):
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        return origin == self.target_origin

    def normalize_url(self, url):
        parsed = urlparse(url)
        return parsed._replace(fragment="").geturl()

    def stop(self):
        self.is_stopped = True
        
    def get_next_finding_id(self):
        fid = f"QA-{self.finding_counter:03d}"
        self.finding_counter += 1
        return fid

    async def capture_screenshot(self, page, name, finding_id, vp_name):
        # Format: QA-001_Desktop_HTTP_500.png
        vp_safe = vp_name.split(' ')[0] # just 'Desktop' or 'Mobile'
        filename = f"{finding_id}_{vp_safe}_{name}.png"
        from app.paths import get_screenshots_dir
        filepath = os.path.join(get_screenshots_dir(), filename)
        await page.screenshot(path=filepath, full_page=True)
        return filepath

    def get_existing_finding_id(self, url, status, actual_result):
        for r in self.results:
            if r['url'] == url and r['status'] == status and r['actual_result'] == actual_result and r['finding_id']:
                return r['finding_id']
        return None

    async def run(self):
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            
            while self.queue and len(self.visited_urls) < self.max_pages and not self.is_stopped:
                current_url, depth = self.queue.pop(0)
                
                if current_url in self.visited_urls:
                    continue
                    
                self.visited_urls.add(current_url)
                
                for vp in self.viewports:
                    if self.is_stopped:
                        break
                        
                    context = await browser.new_context(
                        viewport={'width': vp['width'], 'height': vp['height']},
                        ignore_https_errors=True
                    )
                    page = await context.new_page()
                    
                    page_errors = []
                    page.on("pageerror", lambda err: page_errors.append(str(err)))
                    page.on("console", lambda msg: page_errors.append(msg.text) if msg.type == "error" else None)

                    screenshot_path = None
                    vp_name = f"{vp['name']} {vp['width']}x{vp['height']}"
                    
                    try:
                        response = await page.goto(current_url, timeout=30000, wait_until="networkidle")
                        
                        status = response.status if response else None
                        title = await page.title()
                        
                        if not status:
                            actual_res = "Failed to get response status"
                            fid = self.get_existing_finding_id(current_url, "FAIL", actual_res)
                            if not fid: fid = self.get_next_finding_id()
                            screenshot_path = await self.capture_screenshot(page, "No_Status", fid, vp_name)
                            self.results.append({
                                "url": current_url, "expected_result": "Page loads successfully",
                                "actual_result": actual_res, "status": "FAIL",
                                "screenshot": screenshot_path, "viewport": vp_name, "finding_id": fid,
                                "title": "Page failed to return a status", "area": "Page Load / Server Response"
                            })
                        elif status >= 400:
                            actual_res = f"Server returned HTTP {status}"
                            fid = self.get_existing_finding_id(current_url, "FAIL", actual_res)
                            if not fid: fid = self.get_next_finding_id()
                            screenshot_path = await self.capture_screenshot(page, f"HTTP_{status}", fid, vp_name)
                            self.results.append({
                                "url": current_url, "expected_result": "Page loads successfully (200 OK)",
                                "actual_result": actual_res, "status": "FAIL",
                                "screenshot": screenshot_path, "viewport": vp_name, "finding_id": fid,
                                "title": f"Page returns HTTP {status}", "area": "Page Load / Server Response"
                            })
                        else:
                            # Basic responsive check for horizontal scroll (mobile mostly)
                            overflow_detected = await page.evaluate('''() => {
                                return document.documentElement.scrollWidth > window.innerWidth;
                            }''')
                            
                            if overflow_detected and vp['name'] == 'Mobile':
                                actual_res = "Horizontal overflow detected on mobile"
                                fid = self.get_existing_finding_id(current_url, "FAIL", actual_res)
                                if not fid: fid = self.get_next_finding_id()
                                screenshot_path = await self.capture_screenshot(page, "Mobile_Overflow", fid, vp_name)
                                self.results.append({
                                    "url": current_url, "expected_result": "Page fits viewport without horizontal scrolling",
                                    "actual_result": actual_res, "status": "FAIL",
                                    "screenshot": screenshot_path, "viewport": vp_name, "finding_id": fid,
                                    "title": "Horizontal overflow detected", "area": "Responsive Layout"
                                })
                            elif page_errors:
                                actual_res = f"Console errors detected: {page_errors[0]}"
                                fid = self.get_existing_finding_id(current_url, "OBSERVATION", actual_res)
                                if not fid: fid = self.get_next_finding_id()
                                self.results.append({
                                    "url": current_url, "expected_result": "No JS errors",
                                    "actual_result": actual_res, "status": "OBSERVATION",
                                    "screenshot": None, "viewport": vp_name, "finding_id": fid,
                                    "title": "Uncaught JavaScript exception", "area": "JavaScript Execution"
                                })
                            else:
                                self.results.append({
                                    "url": current_url, "expected_result": "Page loads successfully",
                                    "actual_result": "Page loaded without errors", "status": "PASS",
                                    "screenshot": None, "viewport": vp_name, "finding_id": None
                                })
                            
                            # Only extract links on the desktop run to avoid double queuing
                            if depth < self.max_depth and vp['name'] == 'Desktop':
                                hrefs = await page.evaluate("""() => {
                                    return Array.from(document.querySelectorAll('a[href]')).map(a => a.href);
                                }""")
                                
                                for href in hrefs:
                                    full_url = urljoin(current_url, href)
                                    norm_url = self.normalize_url(full_url)
                                    
                                    if self.is_same_origin(norm_url) and norm_url not in self.visited_urls:
                                        self.queue.append((norm_url, depth + 1))
                                        
                    except Exception as e:
                        actual_res = f"Navigation failed: {str(e)}"
                        fid = self.get_existing_finding_id(current_url, "FAIL", actual_res)
                        if not fid: fid = self.get_next_finding_id()
                        screenshot_path = await self.capture_screenshot(page, "Navigation_Failed", fid, vp_name)
                        self.results.append({
                            "url": current_url, "expected_result": "Page loads successfully",
                            "actual_result": actual_res, "status": "FAIL",
                            "screenshot": screenshot_path, "viewport": vp_name, "finding_id": fid,
                            "title": "Page navigation failed", "area": "Page Load / Server Response"
                        })
                    finally:
                        await page.close()
                        await context.close()
                    
            await browser.close()
            return self.results
