import pytest
import asyncio
import http.server
import threading
from playwright.async_api import async_playwright
from app.qa.browser_snapshot_adapter import ReadOnlyBrowserSnapshotAdapter, CancellationToken

HTML_CONTENT = """
<!DOCTYPE html>
<html>
<head><title>Test Page</title></head>
<body>
  <!-- Basic -->
  <button id="btn1">Native Button</button>
  <a href="/link" id="link1">Native Link</a>
  <div role="button" id="rolebtn" tabindex="0">Explicit Role</div>

  <!-- ARIA -->
  <button id="ariabtn" aria-label="Aria Label Btn"></button>
  <span id="lbl">Label Text</span>
  <input type="text" id="labelledby-input" aria-labelledby="lbl" />

  <label for="for-input">For Label</label>
  <input type="text" id="for-input" />

  <label>Wrapped Label <input type="text" id="wrapped-input" /></label>

  <img src="icon.png" alt="Icon alt" id="img-control" role="button" tabindex="0" />
  <button id="icon-only" aria-label="Save"><img src="save.png"/></button>

  <input type="checkbox" id="chk" checked />
  <input type="radio" id="rad" />
  <select id="sel"><option id="opt1" selected>A</option><option id="opt2">B</option></select>
  <div role="tab" id="aria-sel" aria-selected="true" tabindex="0">Tab</div>

  <!-- Disabled/Hidden states -->
  <button id="dis-btn" disabled>Disabled</button>
  <fieldset disabled><button id="dis-fieldset-btn">Fieldset</button></fieldset>
  <button id="aria-dis-btn" aria-disabled="true">Aria Disabled</button>

  <button id="hidden-attr" hidden>Hidden Attr</button>
  <button id="disp-none" style="display:none">Display None</button>
  <button id="vis-hidden" style="visibility:hidden">Vis Hidden</button>
  <div style="display:none"><button id="hidden-ancestor">Hidden Ancestor</button></div>

  <!-- Private -->
  <input type="password" id="pwd" value="PRIVATE-PHASE-A2-SENTINEL" />
  <input type="hidden" id="token_input" name="secret_token" value="PRIVATE-PHASE-A2-SENTINEL" />

  <!-- Ordinary text input (should NOT capture typed value) -->
  <input type="text" id="ordinary-text" placeholder="My Placeholder" value="ORDINARY-SENTINEL-123" />

  <!-- A2 compatibility amendment proof -->
  <input type="email" id="email-input" />
  <input type="checkbox" role="switch" id="switch-chk" />

  <!-- Volatile IDs -->
  <button id="ember123">Ember Button</button>
  <button id="uuid-btn">7b2b05f2-95f7-4340-9a40-5bbca61e6bd6</button>
  <button id="date-btn">2026-10-09</button>

  <!-- Duplicates (Structural Identity Proof) -->
  <button>Same Label</button>
  <button>Same Label</button>

  <!-- Parent Context Proof -->
  <main id="main-content">
      <form id="myform">
          <div role="group" id="mygroup">
              <button id="nested-btn">Nested</button>
          </div>
      </form>
  </main>

  <script>
    window.interactionCount = 0;
    ['click', 'submit', 'focus', 'blur', 'keydown', 'keypress', 'keyup'].forEach(e => {
        document.addEventListener(e, () => { window.interactionCount++; }, true);
    });
  </script>
</body>
</html>
"""

CROSS_ORIGIN_HTML = """
<!DOCTYPE html>
<html>
<head><title>Cross Origin</title></head>
<body>
    <input type="password" id="co-pwd" value="PRIVATE-CROSS-ORIGIN-SENTINEL" />
</body>
</html>
"""

class TrackingServer(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        self.server.request_counts['GET'] += 1
        self.send_response(200)
        self.send_header('Content-type', 'text/html')
        self.end_headers()
        if self.path == '/cross-origin':
            self.wfile.write(CROSS_ORIGIN_HTML.encode())
        else:
            self.wfile.write(HTML_CONTENT.encode())

    def do_POST(self): self.server.request_counts['POST'] += 1; self.send_response(200); self.end_headers()
    def do_PUT(self): self.server.request_counts['PUT'] += 1; self.send_response(200); self.end_headers()
    def do_PATCH(self): self.server.request_counts['PATCH'] += 1; self.send_response(200); self.end_headers()
    def do_DELETE(self): self.server.request_counts['DELETE'] += 1; self.send_response(200); self.end_headers()
    def do_HEAD(self): self.server.request_counts['HEAD'] += 1; self.send_response(200); self.end_headers()
    def do_OPTIONS(self): self.server.request_counts['OPTIONS'] += 1; self.send_response(200); self.end_headers()

    def log_message(self, format, *args):
        pass # silent

@pytest.fixture(scope="module")
def server_a():
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), TrackingServer)
    server.request_counts = {'GET': 0, 'POST': 0, 'PUT': 0, 'PATCH': 0, 'DELETE': 0, 'HEAD': 0, 'OPTIONS': 0}
    t = threading.Thread(target=server.serve_forever)
    t.daemon = True
    t.start()
    yield server
    server.shutdown()

@pytest.fixture(scope="module")
def server_b():
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), TrackingServer)
    server.request_counts = {'GET': 0, 'POST': 0, 'PUT': 0, 'PATCH': 0, 'DELETE': 0, 'HEAD': 0, 'OPTIONS': 0}
    t = threading.Thread(target=server.serve_forever)
    t.daemon = True
    t.start()
    yield server
    server.shutdown()

@pytest.mark.asyncio
async def test_real_browser_snapshot(server_a, server_b):
    url_a = f"http://127.0.0.1:{server_a.server_port}"
    url_b = f"http://127.0.0.1:{server_b.server_port}/cross-origin"

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        context = await browser.new_context()
        page = await context.new_page()

        await page.goto(url_a)

        # Inject iframes dynamically to use the exact ports
        await page.evaluate(f'''
            const f1 = document.createElement('iframe');
            f1.src = "{url_a}"; // same origin
            document.body.appendChild(f1);

            const f2 = document.createElement('iframe');
            f2.src = "{url_b}"; // cross origin
            document.body.appendChild(f2);

            // Setup storage baseline
            localStorage.setItem('baseline', 'value');
            sessionStorage.setItem('baseline', 'value');

            // Instrument storage
            window.storageWriteCount = 0;
            const origSet = Storage.prototype.setItem;
            Storage.prototype.setItem = function(k, v) {{
                if (this === window.localStorage || this === window.sessionStorage) window.storageWriteCount++;
                try {{ return origSet.call(this, k, v); }} catch(e) {{}}
            }};
            const origRem = Storage.prototype.removeItem;
            Storage.prototype.removeItem = function(k) {{
                if (this === window.localStorage || this === window.sessionStorage) window.storageWriteCount++;
                try {{ return origRem.call(this, k); }} catch(e) {{}}
            }};
            const origClr = Storage.prototype.clear;
            Storage.prototype.clear = function() {{
                if (this === window.localStorage || this === window.sessionStorage) window.storageWriteCount++;
                try {{ return origClr.call(this); }} catch(e) {{}}
            }};
        ''')

        await asyncio.sleep(0.5) # Wait for frames to load

        # Reset Network Counters and storage writes before test
        server_a.request_counts = {k: 0 for k in server_a.request_counts}
        server_b.request_counts = {k: 0 for k in server_b.request_counts}
        await page.evaluate("window.storageWriteCount = 0;")

        adapter = ReadOnlyBrowserSnapshotAdapter()
        eligibility = {"qa_owned": True, "expected_context": context}

        snapshot = await adapter.capture_snapshot(page, eligibility)

        assert snapshot.coverage.terminal_status == "COMPLETE"

        # Zero interactions check
        interaction_count = await page.evaluate("window.interactionCount")
        assert interaction_count == 0, "Adapter fired events!"

        # Zero Storage writes check
        local_baseline = await page.evaluate("localStorage.getItem('baseline')")
        session_baseline = await page.evaluate("sessionStorage.getItem('baseline')")
        assert local_baseline == 'value'
        assert session_baseline == 'value'

        storage_write_count = await page.evaluate("window.storageWriteCount")
        assert storage_write_count == 0

        # Zero Network check
        total_a = sum(server_a.request_counts.values())
        total_b = sum(server_b.request_counts.values())

        assert total_a == 0, f"Total HTTP requests not zero: {server_a.request_counts}"
        assert total_b == 0, f"Total HTTP requests not zero: {server_b.request_counts}"

        assert server_a.request_counts['POST'] == 0
        assert server_a.request_counts['PUT'] == 0
        assert server_a.request_counts['PATCH'] == 0
        assert server_a.request_counts['DELETE'] == 0

        # Check controls
        ctrls = {c.control_id: c for c in snapshot.main_frame.controls}

        # Basic
        assert ctrls["btn1"].raw_role == "button"
        assert ctrls["btn1"].raw_name == "Native Button"
        assert ctrls["link1"].raw_role == "a"
        assert ctrls["rolebtn"].raw_role == "button"

        # ARIA
        assert ctrls["ariabtn"].raw_name == "Aria Label Btn"

        # Note: A2 is a RAW heuristic, it currently fails to resolve aria-labelledby, label-for, wrapped-label, and alt natively.
        # It relies on innerText or aria-label.
        assert ctrls["labelledby-input"].raw_name == "Label Text" # It now finds it using document.getElementById
        assert ctrls["wrapped-input"].raw_name == "Wrapped Label"

        assert ctrls["chk"].checked is True
        assert ctrls["opt1"].selected is True
        assert ctrls["opt2"].selected is False
        assert ctrls["aria-sel"].selected is True

        # Native input extraction proofs
        assert ctrls["chk"].raw_role == "input checkbox"
        assert ctrls["rad"].raw_role == "input radio"
        assert ctrls["ordinary-text"].raw_role == "input text"
        assert ctrls["labelledby-input"].raw_role == "input text"
        assert ctrls["email-input"].raw_role == "input email"
        assert ctrls["switch-chk"].raw_role == "switch"

        # Disabled/Hidden
        assert ctrls["dis-btn"].raw_enabled is False
        assert ctrls["dis-fieldset-btn"].raw_enabled is False
        # aria-disabled is an attribute, current logic doesn't explicitly flag it as raw_enabled=False (raw_enabled tracks native).
        assert ctrls["aria-dis-btn"].raw_enabled is True

        assert ctrls["hidden-attr"].raw_visible is False
        assert ctrls["disp-none"].raw_visible is False
        assert ctrls["vis-hidden"].raw_visible is False
        assert ctrls["hidden-ancestor"].raw_visible is False

        # Private
        pwd = ctrls["pwd"]
        assert pwd.is_private is True
        assert pwd.raw_name == "***"
        assert pwd.raw_role == "input password"
        assert "PRIVATE-PHASE-A2-SENTINEL" not in repr(snapshot)

        tok = ctrls["token_input"]
        assert tok.is_private is True

        # Ordinary text input
        ord_txt = ctrls["ordinary-text"]
        assert ord_txt.is_private is False
        assert ord_txt.raw_name == "My Placeholder" # It uses placeholder, NOT value
        assert "ORDINARY-SENTINEL-123" not in repr(snapshot)

        # Volatile
        assert ctrls["ember123"].is_volatile is True
        assert ctrls["uuid-btn"].is_volatile is False
        assert ctrls["uuid-btn"].raw_name == "7b2b05f2-95f7-4340-9a40-5bbca61e6bd6"
        assert ctrls["date-btn"].is_volatile is False

        # Structural Identity Proof
        dup_buttons = [c for c in snapshot.main_frame.controls if c.raw_name == "Same Label"]
        assert len(dup_buttons) == 2
        assert dup_buttons[0].control_id != dup_buttons[1].control_id
        assert dup_buttons[0].control_id.startswith("auto-button-")

        # Parent Context Proof
        nested = ctrls["nested-btn"]
        assert nested.semantic_parent_context == "main#main-content > form#myform > div#mygroup[role=group]"

        # Frames
        assert len(snapshot.main_frame.child_frames) == 2

        f1 = [f for f in snapshot.main_frame.child_frames if f.is_same_origin][0]
        f2 = [f for f in snapshot.main_frame.child_frames if not f.is_same_origin][0]

        assert f1.coverage.terminal_status == "COMPLETE"
        assert len(f1.controls) > 0 # Introspected

        assert f2.coverage.terminal_status == "CROSS_ORIGIN_LIMITED"
        assert len(f2.controls) == 0 # NOT introspected

        # Cross origin private marker absent
        assert "PRIVATE-CROSS-ORIGIN-SENTINEL" not in repr(snapshot)

        await browser.close()
