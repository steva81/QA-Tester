import os
import pytest
import asyncio
import threading
import socket
from app.qa.engine import SmartSmokeQA
from tests.demo_app import app as demo_app

def get_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('', 0))
        return s.getsockname()[1]

# Fixture to run the demo app in the background during tests
@pytest.fixture(scope="module")
def run_demo_app():
    port = get_free_port()
    import werkzeug.serving
    server = werkzeug.serving.make_server('127.0.0.1', port, demo_app)
    thread = threading.Thread(target=server.serve_forever)
    thread.daemon = True
    thread.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()
    thread.join(timeout=2)

@pytest.mark.asyncio
async def test_smart_smoke_engine(run_demo_app):
    target_url = run_demo_app
    engine = SmartSmokeQA(target_url, project_id=1, run_id=1, max_pages=30, max_depth=3)
    
    results = await engine.run()
    assert len(results) > 0, "No results returned"
    
    # Check /pass
    passes = [r for r in results if r["url"].endswith("/pass") and r["status"] == "PASS"]
    if len(passes) == 0:
        print("RESULTS:", results)
    assert len(passes) > 0, f"Expected /pass to PASS. Results: {results}"
    
    # Check /server-error
    fails_500 = [r for r in results if r["url"].endswith("/server-error") or r["url"].endswith("/fail")]
    assert any(r["status"] == "FAIL" and "HTTP 500" in r["actual_result"] for r in fails_500)
    
    # Check /missing
    fails_404 = [r for r in results if r["url"].endswith("/missing")]
    assert any(r["status"] == "FAIL" and "HTTP 404" in r["actual_result"] for r in fails_404)
    
    # Check /js-error
    js_errors = [r for r in results if r["url"].endswith("/js-error")]
    assert any(r["status"] == "OBSERVATION" and "Console" in r["actual_result"] for r in js_errors)
    
    # Check /broken-image
    broken_image = [r for r in results if r["url"].endswith("/broken-image")]
    assert any(r["status"] == "OBSERVATION" for r in broken_image)
    
    # Check /broken-script
    broken_script = [r for r in results if r["url"].endswith("/broken-script")]
    assert any(r["status"] == "OBSERVATION" for r in broken_script)
    
    # Check /overflow (Mobile should FAIL, Desktop should PASS)
    overflow = [r for r in results if r["url"].endswith("/overflow")]
    assert any(r["status"] == "FAIL" and r["viewport"].startswith("Mobile") for r in overflow)
    assert any(r["status"] == "PASS" and r["viewport"].startswith("Desktop") for r in overflow)
    
    # Check /auth-protected
    auth = [r for r in results if r["url"].endswith("/auth-protected")]
    assert any(r["status"] == "AUTH REQUIRED" for r in auth)
    
    # Check /redirect
    redir = [r for r in results if r["url"].endswith("/redirect")]
    assert any(r["status"] == "REDIRECTED" for r in redir)
    
    # Check /duplicate-id
    dup = [r for r in results if r["url"].endswith("/duplicate-id")]
    assert any(r["status"] == "OBSERVATION" and "Duplicate DOM ID" in r["actual_result"] for r in dup)
    
    # Check /no-title
    no_title = [r for r in results if r["url"].endswith("/no-title")]
    assert any(r["status"] == "OBSERVATION" and "Missing or empty document title" in r["actual_result"] for r in no_title)
    
    # Check /missing-label
    missing_lbl = [r for r in results if r["url"].endswith("/missing-label")]
    assert any(r["status"] == "OBSERVATION" and "Missing form label" in r["actual_result"] for r in missing_lbl)
    
    # Verify Requested vs Final URL on redirect
    assert any("/pass" in str(r.get("final_url", "")) and r["url"].endswith("/redirect") for r in redir)
    
    # Verify auth redirect does not PASS
    assert not any(r["status"] == "PASS" for r in auth)
    
    # Cleanup screenshot
    fails = [r for r in results if r["status"] == "FAIL"]
    if len(fails) > 0 and fails[0]["screenshot"] and os.path.exists(fails[0]["screenshot"]):
        try:
            os.remove(fails[0]["screenshot"])
        except Exception:
            pass

@pytest.mark.asyncio
async def test_engine_modes(run_demo_app):
    target_url = run_demo_app + "/pass"
    
    # Test current mode
    engine_current = SmartSmokeQA(target_url, project_id=1, run_id=2, mode="current")
    res_current = await engine_current.run()
    # Should only run 2 checks (Desktop + Mobile for 1 page)
    assert len(res_current) == 2
    
    # Test collected mode
    engine_coll = SmartSmokeQA(target_url, project_id=1, run_id=3, mode="collected", collected_urls=[target_url, run_demo_app + "/js-error"])
    res_coll = await engine_coll.run()
    assert len(res_coll) == 4 # 2 pages * 2 viewports
