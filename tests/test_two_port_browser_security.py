import json
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import pytest
from playwright.async_api import async_playwright
from server import app


class AttackerHandler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self):
        # Attacker page serves forms and fetch scripts targeting Victim server
        target_origin = self.server.target_origin  # type: ignore
        html = f"""<!doctype html>
<html>
<body>
    <h1>Attacker Page (Origin A)</h1>
    <!-- Form POST without custom headers -->
    <form id="form-attack" action="{target_origin}/api/reset" method="POST">
        <button id="btn-form-attack" type="submit">Submit Form Attack</button>
    </form>
    <script>
        async function runFetchAttackNoHeaders() {{
            try {{
                const r = await fetch('{target_origin}/api/reset', {{
                    method: 'POST',
                    body: JSON.stringify({{attack: true}})
                }});
                window.fetchNoHeadersResult = {{status: r.status, ok: r.ok}};
            }} catch(e) {{
                window.fetchNoHeadersResult = {{error: e.toString()}};
            }}
        }}

        async function runFetchAttackWithCustomHeader() {{
            try {{
                const r = await fetch('{target_origin}/api/reset', {{
                    method: 'POST',
                    headers: {{'X-QA-Request': '1'}},
                    body: JSON.stringify({{attack: true}})
                }});
                window.fetchWithHeaderResult = {{status: r.status, ok: r.ok}};
            }} catch(e) {{
                window.fetchWithHeaderResult = {{error: e.toString()}};
            }}
        }}
    </script>
</body>
</html>""".encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(html)))
        self.end_headers()
        self.wfile.write(html)


class LegitimateHandler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self):
        # Legitimate page on Origin B making same-origin fetch to B
        html = """<!doctype html>
<html>
<body>
    <h1>Legitimate Page (Origin B)</h1>
    <script>
        async function runLegitimatePost() {
            try {
                const r = await fetch('/api/reset', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({legit: true})
                });
                window.legitResult = {status: r.status, ok: r.ok};
            } catch(e) {
                window.legitResult = {error: e.toString()};
            }
        }
    </script>
</body>
</html>""".encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(html)))
        self.end_headers()
        self.wfile.write(html)


@pytest.mark.asyncio
async def test_two_port_browser_isolation():
    from werkzeug.serving import make_server
    from app.models.database import init_db, get_db_connection
    init_db()
    conn = get_db_connection()
    project = conn.execute("INSERT INTO projects(name,target_url,environment,tester_name) VALUES('CSRF sentinel','http://127.0.0.1','Test','QA')").lastrowid
    conn.commit(); conn.close()
    def sentinel_exists():
        conn = get_db_connection()
        try:
            return conn.execute('SELECT name FROM projects WHERE id=?', (project,)).fetchone() is not None
        finally:
            conn.close()
    
    # 1. Start Victim server (QA Tester Flask app) on Port B
    app.config["TESTING"] = True
    victim_server = make_server("127.0.0.1", 0, app)
    victim_port = victim_server.port
    victim_origin = f"http://127.0.0.1:{victim_port}"
    t_victim = threading.Thread(target=victim_server.serve_forever, daemon=True)
    t_victim.start()

    # 2. Start Attacker server on Port A
    attacker_server = HTTPServer(("127.0.0.1", 0), AttackerHandler)
    attacker_server.target_origin = victim_origin  # type: ignore
    attacker_port = attacker_server.server_port
    attacker_origin = f"http://127.0.0.1:{attacker_port}"
    t_attacker = threading.Thread(target=attacker_server.serve_forever, daemon=True)
    t_attacker.start()

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(chromium_sandbox=True, )
            page = await browser.new_page()

            # A. Test cross-origin Form POST from Origin A to Origin B
            await page.goto(attacker_origin)
            # Submit form attack
            await page.click("#btn-form-attack")
            await page.wait_for_timeout(300)
            # Form submission should be blocked with 403 Forbidden
            content = await page.content()
            assert "403" in content or "Cross-origin" in content or "prohibited" in content
            assert sentinel_exists(), 'Cross-origin form changed the database'

            # B. Test cross-origin fetch without custom headers from Origin A to Origin B
            await page.goto(attacker_origin)
            await page.evaluate("runFetchAttackNoHeaders()")
            await page.wait_for_timeout(500)
            res_no_headers = await page.evaluate("window.fetchNoHeadersResult")
            # Either network preflight/CORS error or 403 response
            assert res_no_headers.get("status") == 403 or "error" in res_no_headers
            assert sentinel_exists(), 'Cross-origin fetch changed the database'

            # C. Test cross-origin fetch WITH X-QA-Request header from Origin A to Origin B
            # Browser MUST trigger CORS preflight; server provides NO CORS headers -> blocked!
            await page.evaluate("runFetchAttackWithCustomHeader()")
            await page.wait_for_timeout(500)
            res_with_header = await page.evaluate("window.fetchWithHeaderResult")
            # Must fail with browser network/CORS error (cannot bypass CORS from browser JS)
            assert "error" in res_with_header or res_with_header.get("status") == 403
            assert sentinel_exists(), 'Custom header bypassed origin policy'

            # D. Test legitimate same-origin POST on Origin B
            # Load legitimate page directly on Origin B
            await page.goto(f"{victim_origin}/dashboard")
            # Execute same-origin fetch
            legit_res = await page.evaluate(
                """async () => {
                    const r = await fetch('/api/reset', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'}
                    });
                    return {status: r.status, ok: r.ok};
                }"""
            )
            assert legit_res.get("status") == 200 and legit_res.get("ok") is True
            assert not sentinel_exists(), 'Authorized reset did not reach the real handler'

            await browser.close()
    finally:
        attacker_server.shutdown()
        attacker_server.server_close()
        victim_server.shutdown()
        victim_server.server_close()
        t_attacker.join(timeout=5); t_victim.join(timeout=5)
        assert not t_attacker.is_alive() and not t_victim.is_alive()

