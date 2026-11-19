import subprocess
import sys
import os
import pytest
import pytest

def test_no_forbidden_dependencies_in_subprocess():
    script = f"""
import sys
import os
sys.path.insert(0, r'{os.path.dirname(os.path.dirname(os.path.abspath(__file__)))}')
import app.qa.semantic_model
import app.qa.interaction_policy
import app.qa.state_fingerprint
import app.qa.candidate_intent_graph

forbidden_substrings = ['playwright', 'flask', 'sqlalchemy', 'app.models']
leaked = []
for mod_name in sys.modules.keys():
    for f in forbidden_substrings:
        if f in mod_name:
            leaked.append(mod_name)
if leaked:
    print("FORBIDDEN: " + ",".join(leaked))
    sys.exit(1)
print("CLEAN")
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, f"Subprocess failed: {result.stderr}\n{result.stdout}"
    assert "CLEAN" in result.stdout

def test_no_network_calls(monkeypatch):
    import socket

    def mock_socket(*args, **kwargs):
        raise RuntimeError("Network calls are forbidden in Phase A1 pure core")

    monkeypatch.setattr(socket, "socket", mock_socket)

    from app.qa.semantic_model import SemanticControl, SemanticState
    from app.qa.state_fingerprint import CanonicalStateFingerprint

    c = SemanticControl("c1", "button", "Click", True, True, None, None, None, False)
    s = SemanticState("http://a.com", (c,))
    CanonicalStateFingerprint.generate(s)
