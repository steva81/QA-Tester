import os
import pytest
import asyncio
import threading
from app.qa.engine import SmartSmokeQA
from tests.demo_app import app as demo_app

# Fixture to run the demo app in the background during tests
@pytest.fixture(scope="module")
def run_demo_app():
    import werkzeug.serving
    server = werkzeug.serving.make_server('127.0.0.1', 8080, demo_app)
    thread = threading.Thread(target=server.serve_forever)
    thread.daemon = True
    thread.start()
    yield server
    server.shutdown()
    thread.join(timeout=2)

@pytest.mark.asyncio
async def test_smart_smoke_engine(run_demo_app):
    # Ensure screenshots folder exists
    os.makedirs("screenshots", exist_ok=True)
    
    target_url = "http://127.0.0.1:8080"
    engine = SmartSmokeQA(target_url, project_id=1, run_id=1, max_pages=10, max_depth=2)
    
    results = await engine.run()
    
    assert len(results) > 0, "No results returned"
    
    # Check for PASS
    passes = [r for r in results if r["status"] == "PASS"]
    assert len(passes) > 0, "Expected at least one PASS"
    
    # Check for FAIL (the /fail route)
    fails = [r for r in results if r["status"] == "FAIL"]
    assert len(fails) > 0, "Expected at least one FAIL"
    assert "HTTP 500" in fails[0]["actual_result"]
    assert fails[0]["screenshot"] is not None
    assert os.path.exists(fails[0]["screenshot"]), "Screenshot file was not created physically"
    
    # Check for OBSERVATION (the /js-error route)
    obs = [r for r in results if r["status"] == "OBSERVATION"]
    assert len(obs) > 0, "Expected at least one OBSERVATION"
    assert "Console errors" in obs[0]["actual_result"]
    
    # Cleanup screenshot
    if fails[0]["screenshot"] and os.path.exists(fails[0]["screenshot"]):
        os.remove(fails[0]["screenshot"])
