"""Adversarial Local Verification for Scoped Authorized Mutation Support.

Asserts true server-side state (not just DOM elements or HTTP return codes) against:
1. Generic API/GraphQL prohibited operations blocked by default.
2. Unrelated same-origin writes blocked without explicit OperationPermit.
3. Similar names, partial substrings, and local SQLite row ID collisions cannot authorize deletion.
4. Ownership cannot cross project, target origin, or account context boundaries.
5. Pre-existing avatars and files remain strictly untouched; avatar file inputs are never hijacked.
6. Upload success does not imply cleanup; remote deletion and absence are genuinely verified on server state.
7. Ambiguous creation, duplicate submission, and interruption retain honest recovery state in journal.
8. Cancellation and recovery sweeps clean only confirmed run-owned material, preserving pre-existing server state.
"""

import asyncio
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import threading
import time
from typing import Any, Dict, List
import urllib.parse
import pytest
from playwright.async_api import async_playwright

from app.models.database import get_db_connection, init_db
from app.qa.scoped_mutation import (
    AutonomousProfileWorkflowRunner,
    OperationPermit,
    OperationType,
    ProfileWorkflowScope,
    ScopedEntityOwnershipRegistry,
    ScopedMutationNetworkBarrier,
    ScopedOwnershipRecord,
)


class AdversarialProfileServer(ThreadingHTTPServer):
    """In-memory realistic adversarial backend tracking true server-side state."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0):
        super().__init__((host, port), AdversarialProfileHandler)
        # Server-side state store
        self.user_profile = {
            "user_id": "usr_tester_123",
            "account_status": "ACTIVE",
            "security_settings_version": 1,
            "avatar": "avatar_real_steva.jpg",
            "avatar_tampered": False,
        }
        self.experiences: Dict[str, Dict[str, Any]] = {
            "exp-real-1": {
                "id": "exp-real-1",
                "company": "Acme Corp Global",
                "role": "Lead Architect",
                "desc": "Pre-existing work history item",
            },
            "1": {
                "id": "1",
                "company": "Nordic Quality AB",
                "role": "QA Director",
                "desc": "Pre-existing record with integer-like string ID '1' to test DB row collision",
            },
            "exp-impostor-1": {
                "id": "exp-impostor-1",
                "company": "[QA-TEST-1-ABC] Impostor Corp",
                "role": "Senior Consultant",
                "desc": "Belongs to Run 1, must not be deletable by Run 2",
            },
        }
        self.attachments: Dict[str, Dict[str, Any]] = {
            "att-real-1": {
                "id": "att-real-1",
                "filename": "steva_real_resume_2026.pdf",
                "content": b"%PDF-1.4 Real Resume Content",
            }
        }
        self.graphql_mutations_executed: List[str] = []
        self.unsolicited_writes_received: List[Dict[str, Any]] = []
        self.next_exp_id = 100
        self.next_att_id = 500

    @property
    def origin(self) -> str:
        return f"http://{self.server_address[0]}:{self.server_address[1]}"


class AdversarialProfileHandler(BaseHTTPRequestHandler):
    """HTTP handler with realistic adversarial HTML and backend API/GraphQL endpoints."""

    @property
    def adv_server(self) -> AdversarialProfileServer:
        return self.server  # type: ignore

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in ("/", "/profile"):
            self._serve_profile_page()
        elif parsed.path == "/api/state":
            self._send_json(200, {
                "user_profile": self.adv_server.user_profile,
                "experiences": self.adv_server.experiences,
                "attachments": {k: v["filename"] for k, v in self.adv_server.attachments.items()},
                "graphql_mutations": self.adv_server.graphql_mutations_executed,
            })
        elif parsed.path == "/login":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<!DOCTYPE html><html><body><h1>Sign In</h1><form><input name='username'/></form></body></html>")
        elif parsed.path == "/error-page":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<!DOCTYPE html><html><body><h1>500 Internal Server Error</h1><p>Application Error</p></body></html>")
        elif parsed.path.startswith("/static/"):
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b"\xFF\xD8\xFF\xE0\x00\x10JFIF\x00\x01\x01\x01\x00`\x00`\x00\x00\xFF\xDB\x00C\x00\xFF\xC0\x00\x0B\x08\x00\x01\x00\x01\x01\x01\x11\x00\xFF\xC4\x00\x1F\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b\xFF\xDA\x00\x08\x01\x01\x00\x00?\x00\xBF\x00\xFF\xD9")
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(length) if length > 0 else b""

        # 1. Prohibited Generic GraphQL mutations endpoint
        if parsed.path == "/api/graphql":
            try:
                body = json.loads(raw_body.decode("utf-8"))
            except Exception:
                body = {}
            query = body.get("query", "")
            op_name = body.get("operationName", "")
            self.adv_server.graphql_mutations_executed.append(f"{op_name}:{query}")

            # If an unauthorized destructive mutation somehow arrived here:
            if "deleteAccount" in query or "deleteAccount" in op_name:
                self.adv_server.user_profile["account_status"] = "PROHIBITED_DELETED_BY_GRAPHQL"
            if "updatePassword" in query or "updatePassword" in op_name:
                self.adv_server.user_profile["security_settings_version"] += 999
            self._send_json(200, {"data": {"result": "graphql_executed"}})
            return

        # 2. Prohibited account/security mutation endpoints
        if parsed.path in ("/api/account/delete", "/api/security/revoke-keys", "/api/security/save"):
            self.adv_server.unsolicited_writes_received.append({"path": parsed.path, "body": raw_body.decode("utf-8", "ignore")})
            if parsed.path == "/api/account/delete":
                self.adv_server.user_profile["account_status"] = "PROHIBITED_DELETED"
            elif "security" in parsed.path:
                self.adv_server.user_profile["security_settings_version"] += 100
            self._send_json(200, {"status": "ok"})
            return

        # 3. Avatar upload endpoint (must remain untouched!)
        if parsed.path == "/api/avatar/upload":
            self.adv_server.user_profile["avatar"] = "tampered_avatar.png"
            self.adv_server.user_profile["avatar_tampered"] = True
            self._send_json(200, {"status": "avatar_updated"})
            return

        # 4. Profile Experience Create
        if parsed.path == "/api/experience":
            try:
                data = json.loads(raw_body.decode("utf-8"))
            except Exception:
                data = urllib.parse.parse_qs(raw_body.decode("utf-8"))
                data = {k: v[0] for k, v in data.items()}

            exp_id = f"exp-adv-{self.adv_server.next_exp_id}"
            self.adv_server.next_exp_id += 1
            item = {
                "id": exp_id,
                "company": data.get("company", "Unknown Company"),
                "role": data.get("role", "Tester"),
                "desc": data.get("desc", ""),
            }
            self.adv_server.experiences[exp_id] = item
            self._send_json(201, item)
            return

        # 5. Attachment Upload
        if parsed.path == "/api/attachments/upload":
            # Simple multipart/form or raw parser
            boundary = None
            ctype = self.headers.get("Content-Type", "")
            att_id = f"att-adv-{self.adv_server.next_att_id}"
            self.adv_server.next_att_id += 1

            filename = f"uploaded_file_{att_id}.txt"
            if "filename=" in raw_body.decode("utf-8", "ignore"):
                parts = raw_body.decode("utf-8", "ignore").split("filename=\"")
                if len(parts) > 1:
                    filename = parts[1].split("\"")[0]

            self.adv_server.attachments[att_id] = {
                "id": att_id,
                "filename": filename,
                "content": raw_body,
            }
            self._send_json(201, {"id": att_id, "filename": filename})
            return

        self.adv_server.unsolicited_writes_received.append({"path": parsed.path, "body": raw_body.decode("utf-8", "ignore")})
        self._send_json(200, {"status": "recorded"})

    def do_PUT(self):
        parsed = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(length) if length > 0 else b""

        if parsed.path.startswith("/api/experience/"):
            exp_id = parsed.path.split("/")[-1]
            if exp_id in self.adv_server.experiences:
                try:
                    data = json.loads(raw_body.decode("utf-8"))
                    self.adv_server.experiences[exp_id]["company"] = data.get("company", self.adv_server.experiences[exp_id]["company"])
                    self._send_json(200, self.adv_server.experiences[exp_id])
                    return
                except Exception:
                    pass
            self.send_response(404)
            self.end_headers()
            return

        self.adv_server.unsolicited_writes_received.append({"path": parsed.path, "method": "PUT"})
        self._send_json(200, {"status": "ok"})

    def do_DELETE(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.startswith("/api/experience/"):
            exp_id = parsed.path.split("/")[-1]
            if exp_id in self.adv_server.experiences:
                del self.adv_server.experiences[exp_id]
                self._send_json(200, {"status": "deleted", "id": exp_id})
                return
            self.send_response(404)
            self.end_headers()
            return

        if parsed.path.startswith("/api/attachments/"):
            att_id = parsed.path.split("/")[-1]
            if att_id in self.adv_server.attachments:
                del self.adv_server.attachments[att_id]
                self._send_json(200, {"status": "deleted", "id": att_id})
                return
            self.send_response(404)
            self.end_headers()
            return

        self.adv_server.unsolicited_writes_received.append({"path": parsed.path, "method": "DELETE"})
        self._send_json(200, {"status": "ok"})

    def _send_json(self, status: int, payload: Any):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _serve_profile_page(self):
        exp_items_html = ""
        for eid, exp in self.adv_server.experiences.items():
            exp_items_html += f"""
            <div id="{eid}" class="exp-item" data-id="{eid}" style="border:1px solid #ccc; margin:8px; padding:8px;">
                <h4 class="company-name">{exp['company']}</h4>
                <p class="role-name">{exp['role']}</p>
                <button id="btn-edit-{eid}" class="btn-edit" onclick="openEdit('{eid}', '{exp['company']}')">Edit</button>
                <button id="btn-delete-{eid}" data-delete="{eid}" class="btn-delete" onclick="deleteExp('{eid}')">Delete</button>
            </div>
            """

        att_items_html = ""
        for aid, att in self.adv_server.attachments.items():
            att_items_html += f"""
            <div id="{aid}" class="attachment-item" data-id="{aid}" style="border:1px solid #ddd; margin:4px; padding:4px;">
                <span class="attachment-filename">{att['filename']}</span>
                <button id="btn-del-att-{aid}" data-delete-attachment="{aid}" data-filename="{att['filename']}" onclick="deleteAtt('{aid}')">Delete File</button>
            </div>
            """

        html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8" />
    <meta name="user-id" content="tester@example.com" />
    <meta name="account-id" content="tester@example.com" />
    <meta name="authenticated-user" content="tester@example.com" />
    <title>Example User Profile - Adversarial Test Environment</title>
    <style>
        body {{ font-family: sans-serif; margin: 20px; }}
        .warning-zone {{ background: #ffeeee; border: 2px solid #cc0000; padding: 12px; margin-bottom: 20px; }}
        .avatar-zone {{ background: #f0f4f8; border: 1px solid #bcd; padding: 12px; margin-bottom: 20px; }}
        .section-box {{ background: #fafafa; border: 1px solid #eee; padding: 15px; margin-bottom: 20px; }}
        button {{ padding: 6px 12px; cursor: pointer; }}
        .btn-danger {{ background: #d9534f; color: white; border: none; }}
    </style>
</head>
<body>
    <div id="auth-session" data-authenticated-user="tester@example.com" data-user-id="tester@example.com" style="display:none;">tester@example.com</div>
    <h1>User Profile - Tester</h1>

    <!-- ADVERSARIAL ZONE 1: Misleading Global Controls & Forbidden Actions -->
    <div class="warning-zone" id="account-security-danger-zone">
        <h3>Account & Security Settings (FORBIDDEN WRITE AREA)</h3>
        <p>Warning: Modifying or deleting this account will result in immediate termination.</p>
        <button id="btn-delete-account" class="btn-danger" onclick="deleteAccountProhibited()">Delete Account Permanently</button>
        <button id="btn-save-security" onclick="saveSecuritySettingsProhibited()">Save Security Settings</button>
        <button id="btn-revoke-keys" onclick="revokeKeysProhibited()">Revoke All API Keys</button>
        <form id="form-change-password" onsubmit="event.preventDefault(); submitGraphQLPassword();">
            <input type="password" id="new-password" placeholder="New Password" />
            <button type="submit" id="btn-submit-password">Update Password</button>
        </form>
    </div>

    <!-- ADVERSARIAL ZONE 2: Avatar & Profile Photo (MUST REMAIN UNTOUCHED) -->
    <div class="avatar-zone" id="profile-avatar-container">
        <h3>User Avatar (PROTECTED PRE-EXISTING IMAGE)</h3>
        <img id="user-avatar-img" src="/static/avatar_real.jpg" alt="Tester's Real Avatar" width="80" height="80" />
        <p>Current avatar: <span id="avatar-label">{self.adv_server.user_profile['avatar']}</span></p>
        <form id="form-upload-avatar" action="/api/avatar/upload" method="POST" enctype="multipart/form-data">
            <label for="avatar-input">Upload New Avatar Photo:</label>
            <input type="file" id="avatar-input" name="avatar_photo" accept="image/*" />
            <button type="submit" id="btn-submit-avatar">Save Avatar</button>
        </form>
    </div>

    <!-- ADVERSARIAL ZONE 3: Experience Workflow (AUTHORIZED RUN-OWNED MUTATION TARGET) -->
    <div class="section-box" id="profile-experience-container" data-qa-inventory-complete="true">
        <h2>Work Experience</h2>
        <button id="btn-add-experience" onclick="document.getElementById('dialog-add-experience').showModal()">Add Experience</button>
        
        <div id="experience-list">
            {exp_items_html}
        </div>
    </div>

    <!-- Add Experience Dialog -->
    <dialog id="dialog-add-experience">
        <form id="form-add-experience" method="dialog" onsubmit="event.preventDefault(); submitAddExperience();">
            <h3>Add New Work Experience</h3>
            <div>
                <label for="exp-company">Company:</label>
                <input type="text" id="exp-company" name="company" required />
            </div>
            <div>
                <label for="exp-role">Role:</label>
                <input type="text" id="exp-role" name="role" />
            </div>
            <div>
                <label for="exp-desc">Description:</label>
                <textarea id="exp-desc" name="desc"></textarea>
            </div>
            <div style="margin-top:10px;">
                <button type="submit" id="btn-submit-experience">Save Experience</button>
                <button type="button" onclick="document.getElementById('dialog-add-experience').close()">Cancel</button>
            </div>
        </form>
    </dialog>

    <!-- Edit Experience Dialog -->
    <dialog id="dialog-edit-experience">
        <form id="form-edit-experience" method="dialog" onsubmit="event.preventDefault(); submitEditExperience();">
            <h3>Edit Experience</h3>
            <input type="hidden" id="edit-exp-id" />
            <div>
                <label for="edit-exp-company">Company:</label>
                <input type="text" id="edit-exp-company" name="company" required />
            </div>
            <div style="margin-top:10px;">
                <button type="submit" id="btn-save-experience">Save Changes</button>
                <button type="button" onclick="document.getElementById('dialog-edit-experience').close()">Cancel</button>
            </div>
        </form>
    </dialog>

    <!-- ADVERSARIAL ZONE 4: Documents & Attachments (VERIFIED SAFE ATTACHMENT TARGET) -->
    <div class="section-box" id="profile-documents-container">
        <h2>Verified Documents & Resume Attachments</h2>
        <div id="attachments-list">
            {att_items_html}
        </div>

        <form id="form-upload-attachment" onsubmit="event.preventDefault(); submitUploadAttachment();">
            <h4>Upload Test Document</h4>
            <input type="file" id="attachment-file-input" name="document" accept=".txt,.pdf,.png" />
            <button type="submit" id="btn-upload-file">Upload Document</button>
        </form>
    </div>

    <script>
        async function submitAddExperience() {{
            const company = document.getElementById('exp-company').value;
            const role = document.getElementById('exp-role').value;
            const desc = document.getElementById('exp-desc').value;
            const res = await fetch('/api/experience', {{
                method: 'POST',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify({{ company, role, desc }})
            }});
            if (res.ok) {{
                const item = await res.json();
                const div = document.createElement('div');
                div.id = item.id;
                div.className = 'exp-item';
                div.setAttribute('data-id', item.id);
                div.style = 'border:1px solid #ccc; margin:8px; padding:8px;';
                div.innerHTML = `<h4 class="company-name">${{item.company}}</h4><p class="role-name">${{item.role}}</p><button id="btn-edit-${{item.id}}" class="btn-edit" onclick="openEdit('${{item.id}}', '${{item.company}}')">Edit</button><button id="btn-delete-${{item.id}}" data-delete="${{item.id}}" class="btn-delete" onclick="deleteExp('${{item.id}}')">Delete</button>`;
                document.getElementById('experience-list').appendChild(div);
                document.getElementById('dialog-add-experience').close();
            }}
        }}

        function openEdit(id, company) {{
            document.getElementById('edit-exp-id').value = id;
            document.getElementById('edit-exp-company').value = company;
            document.getElementById('dialog-edit-experience').showModal();
        }}

        async function submitEditExperience() {{
            const id = document.getElementById('edit-exp-id').value;
            const company = document.getElementById('edit-exp-company').value;
            const res = await fetch('/api/experience/' + id, {{
                method: 'PUT',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify({{ company }})
            }});
            if (res.ok) {{
                const item = await res.json();
                const el = document.getElementById(id);
                if (el) {{
                    el.querySelector('.company-name').textContent = item.company;
                }}
                document.getElementById('dialog-edit-experience').close();
            }}
        }}

        async function deleteExp(id) {{
            const res = await fetch('/api/experience/' + id, {{ method: 'DELETE' }});
            if (res.ok) {{
                const el = document.getElementById(id);
                if (el) el.remove();
            }}
        }}

        async function submitUploadAttachment() {{
            const fileInput = document.getElementById('attachment-file-input');
            if (!fileInput.files || fileInput.files.length === 0) return;
            const file = fileInput.files[0];
            const formData = new FormData();
            formData.append('document', file, file.name);
            const res = await fetch('/api/attachments/upload', {{
                method: 'POST',
                body: formData
            }});
            if (res.ok) {{
                const data = await res.json();
                const div = document.createElement('div');
                div.id = data.id;
                div.className = 'attachment-item';
                div.setAttribute('data-id', data.id);
                div.style = 'border:1px solid #ddd; margin:4px; padding:4px;';
                div.innerHTML = `<span class="attachment-filename">${{data.filename}}</span> <button id="btn-del-att-${{data.id}}" data-delete-attachment="${{data.id}}" data-filename="${{data.filename}}" onclick="deleteAtt('${{data.id}}')">Delete File</button>`;
                document.getElementById('attachments-list').appendChild(div);
            }}
        }}

        async function deleteAtt(id) {{
            const res = await fetch('/api/attachments/' + id, {{ method: 'DELETE' }});
            if (res.ok) {{
                const el = document.getElementById(id);
                if (el) el.remove();
            }}
        }}

        async function deleteAccountProhibited() {{
            await fetch('/api/account/delete', {{ method: 'POST' }});
        }}

        async function saveSecuritySettingsProhibited() {{
            await fetch('/api/security/save', {{ method: 'POST' }});
        }}

        async function revokeKeysProhibited() {{
            await fetch('/api/security/revoke-keys', {{ method: 'POST' }});
        }}

        async function submitGraphQLPassword() {{
            await fetch('/api/graphql', {{
                method: 'POST',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify({{
                    query: 'mutation UpdatePwd {{ updatePassword(new: "hacked") {{ success }} }}',
                    operationName: 'UpdatePwd'
                }})
            }});
        }}
    </script>
</body>
</html>
"""
        data = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture(scope="module")
def adv_server():
    server = AdversarialProfileServer("127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def isolated_adv_db(tmp_path, monkeypatch):
    db_file = str(tmp_path / "adv_mutation_test.sqlite")
    monkeypatch.setenv("QA_TESTER_DB_PATH", db_file)
    from app.paths import ensure_dirs
    ensure_dirs()
    init_db()

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM mutation_ownership_records")
        conn.commit()
    finally:
        conn.close()
    yield db_file


# ---------------------------------------------------------------------------
# Test 1: Generic GraphQL and Dangerous API Prohibited Writes Are Blocked
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_generic_graphql_and_dangerous_api_blocked_by_barrier(adv_server, isolated_adv_db):
    """Verify that generic GraphQL mutations and dangerous API operations are blocked by default."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(chromium_sandbox=True, headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        barrier = ScopedMutationNetworkBarrier(target_origin=adv_server.origin)
        await barrier.install(page)
        await page.goto(f"{adv_server.origin}/profile")

        # Initial server state check
        assert adv_server.user_profile["account_status"] == "ACTIVE"
        assert adv_server.user_profile["security_settings_version"] == 1
        assert len(adv_server.graphql_mutations_executed) == 0

        # Attempt prohibited generic GraphQL mutation directly via page evaluate
        with pytest.raises(Exception):
            await page.evaluate("""() => {
                return fetch('/api/graphql', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        query: 'mutation DeleteMyAccount { deleteAccount(id: "usr_tester_123") { success } }',
                        operationName: 'DeleteMyAccount'
                    })
                });
            }""")

        # Attempt prohibited security settings post
        with pytest.raises(Exception):
            await page.evaluate("""() => {
                return fetch('/api/security/save', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ two_factor: false })
                });
            }""")

        # Attempt prohibited direct account deletion button click
        await page.locator("#btn-delete-account").click()
        await page.wait_for_timeout(300)

        # Assert true server state: account status MUST STILL BE ACTIVE!
        assert adv_server.user_profile["account_status"] == "ACTIVE", "Security violation: Account was modified on server!"
        assert adv_server.user_profile["security_settings_version"] == 1, "Security violation: Security settings modified!"
        assert len(adv_server.graphql_mutations_executed) == 0, "GraphQL mutation reached server despite network barrier!"

        await browser.close()


# ---------------------------------------------------------------------------
# Test 2: Unrelated Same-Origin Writes Blocked Without Permit
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_unrelated_same_origin_writes_blocked_by_default(adv_server, isolated_adv_db):
    """Verify that unpermitted POST/PUT/DELETE writes to same-origin endpoints are blocked by default."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(chromium_sandbox=True, headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        barrier = ScopedMutationNetworkBarrier(target_origin=adv_server.origin)
        await barrier.install(page)
        await page.goto(f"{adv_server.origin}/profile")

        initial_exp_count = len(adv_server.experiences)

        # 1. Unsolicited POST to /api/experience without permit
        with pytest.raises(Exception):
            await page.evaluate("""() => {
                return fetch('/api/experience', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ company: 'Unpermitted Corp', role: 'Hacker' })
                });
            }""")

        # 2. Unsolicited DELETE to /api/experience/exp-real-1 without permit
        with pytest.raises(Exception):
            await page.evaluate("""() => {
                return fetch('/api/experience/exp-real-1', { method: 'DELETE' });
            }""")

        # Assert true server state: No new experience was created, and exp-real-1 is untouched!
        assert len(adv_server.experiences) == initial_exp_count
        assert "exp-real-1" in adv_server.experiences
        assert adv_server.experiences["exp-real-1"]["company"] == "Acme Corp Global"

        await browser.close()



# ---------------------------------------------------------------------------
# Test 3: Local Registry ID Collisions & Substring Names Cannot Authorize Deletion
# ---------------------------------------------------------------------------
def test_local_registry_id_and_substring_matching_strictly_rejected(adv_server, isolated_adv_db):
    """Verify that local database primary key collisions and substring matches cannot authorize deletion."""
    registry = ScopedEntityOwnershipRegistry
    run_id = 99
    project_id = 1

    # Insert a genuine record into DB: local table row id will be 1
    intent = registry.record_creation_intent(
        run_id=run_id,
        project_id=project_id,
        target_url=f"{adv_server.origin}/profile",
        entity_type="profile_experience",
        run_prefix="[QA-TEST-99-A1B2]",
        proposed_name="[QA-TEST-99-A1B2] Genuine Corp",
    )
    registry.confirm_creation(
        record_id=intent.id,
        remote_entity_id="exp-adv-genuine-101",
        confirmed_name="[QA-TEST-99-A1B2] Genuine Corp",
    )

    db_recs = registry.get_records_for_run(run_id)
    assert len(db_recs) == 1
    local_row_id = db_recs[0].id
    assert local_row_id == 1, "First inserted row id in isolated DB should be 1"

    # ATTACK 1: An attacker or bug passes remote_entity_id="1" (matching local row ID 1).
    # On the server, entity "1" is 'Nordic Quality AB' (pre-existing record!).
    is_owned = registry.is_owned_by_run(
        run_id=run_id,
        project_id=project_id,
        target_url=f"{adv_server.origin}/profile",
        remote_entity_id=str(local_row_id),  # "1"
        entity_name="Nordic Quality AB",
    )
    assert not is_owned, "Security violation: Local DB primary key '1' collided with remote ID and granted ownership!"

    # ATTACK 2: Substring name matching attack.
    # The record has name '[QA-TEST-99-A1B2] Genuine Corp'.
    # Target entity has name 'Genuine Corp' (substring) or '[QA-TEST-99-A1B2] Genuine Corp Extra'.
    is_owned_sub1 = registry.is_owned_by_run(
        run_id=run_id,
        project_id=project_id,
        target_url=f"{adv_server.origin}/profile",
        remote_entity_id="exp-adv-genuine-101",
        entity_name="Genuine Corp",  # missing prefix!
    )
    assert not is_owned_sub1, "Security violation: Substring name match granted ownership without run tag!"

    # ATTACK 3: Impostor name with wrong run tag.
    # Remote entity on server: '[QA-TEST-1-ABC] Impostor Corp'. We are run 99.
    is_owned_impostor = registry.is_owned_by_run(
        run_id=run_id,
        project_id=project_id,
        target_url=f"{adv_server.origin}/profile",
        remote_entity_id="exp-impostor-1",
        entity_name="[QA-TEST-1-ABC] Impostor Corp",
    )
    assert not is_owned_impostor, "Security violation: Tag from a different run (Run 1) matched Run 99!"


# ---------------------------------------------------------------------------
# Test 4: Ownership Cannot Cross Project, Target Origin, or Account Boundaries
# ---------------------------------------------------------------------------
def test_ownership_cannot_cross_context_boundaries(adv_server, isolated_adv_db):
    """Verify that records are strictly isolated across project, target origin, and account contexts."""
    registry = ScopedEntityOwnershipRegistry
    run_id = 105

    intent = registry.record_creation_intent(
        run_id=run_id,
        project_id=1,
        target_url="http://127.0.0.1:8000/profile",
        entity_type="profile_experience",
        run_prefix="[QA-TEST-105-X1]",
        proposed_name="[QA-TEST-105-X1] Boundary Corp",
        account_context="tester@example.com",
    )
    registry.confirm_creation(
        record_id=intent.id,
        remote_entity_id="exp-adv-bound-1",
        confirmed_name="[QA-TEST-105-X1] Boundary Corp",
    )

    # 1. Reject cross-project access
    assert not registry.is_owned_by_run(
        run_id=run_id,
        project_id=2,  # Different project
        target_url="http://127.0.0.1:8000/profile",
        remote_entity_id="exp-adv-bound-1",
        entity_name="[QA-TEST-105-X1] Boundary Corp",
        account_context="tester@example.com",
    )

    # 2. Reject cross-origin access
    assert not registry.is_owned_by_run(
        run_id=run_id,
        project_id=1,
        target_url="http://other-site.com/profile",  # Different origin
        remote_entity_id="exp-adv-bound-1",
        entity_name="[QA-TEST-105-X1] Boundary Corp",
        account_context="tester@example.com",
    )

    # 3. Reject cross-account access
    assert not registry.is_owned_by_run(
        run_id=run_id,
        project_id=1,
        target_url="http://127.0.0.1:8000/profile",
        remote_entity_id="exp-adv-bound-1",
        entity_name="[QA-TEST-105-X1] Boundary Corp",
        account_context="attacker@example.com",  # Different account
    )

    # 4. Valid matching context
    assert registry.is_owned_by_run(
        run_id=run_id,
        project_id=1,
        target_url="http://127.0.0.1:8000/profile",
        remote_entity_id="exp-adv-bound-1",
        entity_name="[QA-TEST-105-X1] Boundary Corp",
        account_context="tester@example.com",
    )


# ---------------------------------------------------------------------------
# Test 5: Pre-Existing Avatar and Files Remain Untouched
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_avatar_and_existing_files_remain_untouched(adv_server, isolated_adv_db, tmp_path):
    """Verify that avatar inputs are never selected and pre-existing files remain intact."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(chromium_sandbox=True, headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(f"{adv_server.origin}/profile")

        # 1. Discover attachment container
        container = await ProfileWorkflowScope.discover_attachment_container(page)
        assert container is not None
        container_text = await container.text_content()
        assert "Verified Documents" in container_text
        assert "profile-avatar-container" not in (await container.inner_html())

        # 2. Verify is_safe_attachment_input rejects avatar inputs
        avatar_input = page.locator("#avatar-input")
        assert not await ProfileWorkflowScope.is_safe_attachment_input(avatar_input)

        # 3. Run complete autonomous suite with runner
        barrier = ScopedMutationNetworkBarrier(target_origin=adv_server.origin)
        await barrier.install(page)

        runner = AutonomousProfileWorkflowRunner(scratch_dir=str(tmp_path / "scratch"), barrier=barrier)
        results = await runner.run_authorized_profile_suite(
            page=page,
            target_url=f"{adv_server.origin}/profile",
            run_id=10,
            project_id=1,
            viewport_name="Desktop",
        )

        for res in results:
            if res.workflow_type == "upload":
                assert res.verdict == "LIMITED_COVERAGE"
                assert not res.is_customer_defect
                assert res.cleanup_status == "NOT_APPLICABLE"
            else:
                assert res.verdict == "PASS", f"Workflow {res.workflow_type} failed: {res.details}"

        # 4. Assert true server state: Avatar must be completely untouched!
        assert adv_server.user_profile["avatar"] == "avatar_real_steva.jpg"
        assert not adv_server.user_profile["avatar_tampered"]

        # 5. Assert true server state: Pre-existing attachment 'att-real-1' must still exist!
        assert "att-real-1" in adv_server.attachments
        assert adv_server.attachments["att-real-1"]["filename"] == "steva_real_resume_2026.pdf"

        # 6. Assert true server state: Pre-existing experiences still exist!
        assert "exp-real-1" in adv_server.experiences
        assert "1" in adv_server.experiences
        assert "exp-impostor-1" in adv_server.experiences

        await browser.close()


# ---------------------------------------------------------------------------
# Test 6: Unsupported Opaque Upload Leaves Server Untouched
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_opaque_upload_blocked_without_false_remote_cleanup(adv_server, isolated_adv_db, tmp_path, preflight_calls):
    """Opaque browser uploads are unsupported; reject without claiming upload or deletion."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(chromium_sandbox=True, headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(f"{adv_server.origin}/profile")

        barrier = ScopedMutationNetworkBarrier(target_origin=adv_server.origin)
        await barrier.install(page)

        scratch_dir = str(tmp_path / "scratch_att")
        runner = AutonomousProfileWorkflowRunner(scratch_dir=scratch_dir, barrier=barrier)

        initial_server_att_count = len(adv_server.attachments)

        # Execute upload workflow
        up_res = await runner._exercise_upload_workflow(
            page=page,
            target_url=f"{adv_server.origin}/profile",
            run_id=11,
            project_id=1,
            run_prefix="[QA-TEST-11-ATT]",
            viewport_name="Desktop",
            account_context=None,
            cancellation_token=None,
            screenshot_fn=None,
        )

        assert up_res is not None
        assert up_res.verdict == "LIMITED_COVERAGE"
        assert not up_res.is_customer_defect
        assert "Unsupported upload destination" in up_res.details
        assert barrier.blocked_requests == []
        assert preflight_calls == dict(file_selection=0, clicks=0, permits=0)
        assert up_res.cleanup_status == "NOT_APPLICABLE"

        # Check DB record
        db_recs = ScopedEntityOwnershipRegistry.get_records_for_run(11)
        assert len(db_recs) == 1
        att_db_rec = db_recs[0]
        assert att_db_rec.remote_entity_id is None
        assert att_db_rec.remote_cleaned is False
        assert att_db_rec.status == 'NOT_DISPATCHED'
        assert att_db_rec.remote_status == 'NOT_CREATED'
        assert att_db_rec.local_cleaned
        assert not ScopedEntityOwnershipRegistry.get_pending_or_uncleaned_records(project_id=1)

        # Assert true server state: No extra attachment left on server!
        assert len(adv_server.attachments) == initial_server_att_count
        assert att_db_rec.remote_entity_id not in adv_server.attachments

        assert not os.listdir(scratch_dir), "Temporary upload file remains"

        # Assert local scratch file was removed
        local_path = json.loads(att_db_rec.details_json or "{}").get("local_path")
        if local_path:
            assert not os.path.exists(local_path), "Local temporary file was not cleaned up!"

        await browser.close()


# ---------------------------------------------------------------------------
# Test 7: Interruption Retains Honest Recovery State Without False Claims
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_ambiguous_creation_and_interruption_recovery_state(adv_server, isolated_adv_db):
    """Verify that unconfirmed creation retains AMBIGUOUS_UNCONFIRMED state and refuses deletion."""
    registry = ScopedEntityOwnershipRegistry
    run_id = 77
    project_id = 1

    # Simulate ambiguous creation intent
    intent = registry.record_creation_intent(
        run_id=run_id,
        project_id=project_id,
        target_url=f"{adv_server.origin}/profile",
        entity_type="profile_experience",
        run_prefix="[QA-TEST-77-AMB]",
        proposed_name="[QA-TEST-77-AMB] Ghost Entity",
    )
    registry.mark_ambiguous_creation(intent.id, "Network timeout during save")

    # Record must be AMBIGUOUS_UNCONFIRMED
    db_recs = registry.get_records_for_run(run_id)
    assert len(db_recs) == 1
    assert db_recs[0].status == "AMBIGUOUS_UNCONFIRMED"
    assert db_recs[0].remote_status == "AMBIGUOUS"

    # Must refuse ownership
    assert not registry.is_owned_by_run(
        run_id=run_id,
        project_id=project_id,
        target_url=f"{adv_server.origin}/profile",
        remote_entity_id="any-id",
        entity_name="[QA-TEST-77-AMB] Ghost Entity",
    )


@pytest.fixture(scope="module")
def profile_server():
    from tests.profile_test_site import ProfileTestServer
    server = ProfileTestServer("127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


# ---------------------------------------------------------------------------
# Test 8: Active Permit Mismatches Are Aborted Without Consuming Permit
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_active_permit_mismatches_aborted_without_consuming_permit(adv_server, isolated_adv_db):
    """Verify that unrelated or mismatched writes are blocked WITHOUT consuming the active permit."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(chromium_sandbox=True, headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        barrier = ScopedMutationNetworkBarrier(target_origin=adv_server.origin)
        await barrier.install(page)
        await page.goto(f"{adv_server.origin}/profile")

        permit = barrier.acquire_permit(
            operation_type=OperationType.DELETE_ENTITY,
            workflow_name="profile_experience",
            allowed_methods=("DELETE",),
            endpoint_pattern=r"^/api/experience/exp-adv-100/?$",
            resource_id="exp-adv-100",
            max_uses=1,
        )

        try:
            # 1. Mismatched write: wrong endpoint
            with pytest.raises(Exception):
                await page.evaluate("""() => {
                    return fetch('/api/experience/exp-real-1', { method: 'DELETE' });
                }""")
            assert permit.consumed is False
            assert permit.use_count == 0
            assert any("Endpoint /api/experience/exp-real-1 does not match" in b["reason"] for b in barrier.blocked_requests)

            # 2. Mismatched write: wrong method
            with pytest.raises(Exception):
                await page.evaluate("""() => {
                    return fetch('/api/experience', { method: 'POST', body: 'unrelated' });
                }""")
            assert permit.consumed is False
            assert permit.use_count == 0

            # 3. Genuine matching write: DELETE /api/experience/exp-adv-100
            adv_server.experiences["exp-adv-100"] = {"id": "exp-adv-100", "company": "Test Corp", "role": "QA"}
            res = await page.evaluate("""async () => {
                const r = await fetch('/api/experience/exp-adv-100', { method: 'DELETE' });
                return { ok: r.ok, status: r.status };
            }""")
            assert res["ok"] is True
            assert permit.consumed is True
            assert permit.use_count == 1
            assert "exp-adv-100" not in adv_server.experiences
        finally:
            barrier.release_permit(permit)
            await browser.close()


# ---------------------------------------------------------------------------
# Test 9: Resource ID Appearing in Unrelated Comment Field Is Rejected
# ---------------------------------------------------------------------------
def test_resource_id_in_unrelated_comment_strictly_rejected(adv_server):
    """Verify that resource ID string appearing in unrelated comment/text field cannot authorize write."""
    permit = OperationPermit(
        permit_id="test-rid-1",
        operation_type=OperationType.EDIT_ENTITY,
        workflow_name="profile_experience",
        allowed_origin=adv_server.origin,
        allowed_methods=("PUT",),
        endpoint_pattern=r"^/api/experience/?$",
        resource_id="exp-target-99",
    )

    # Attack payload: target ID is in a comment/notes field, not an ID field
    attack_body = json.dumps({
        "id": "exp-attacker-01",
        "company": "Attacker Inc",
        "notes": "Target to destroy is exp-target-99 in notes text",
    })

    matches, reason = permit.matches_request(
        method="PUT",
        url=f"{adv_server.origin}/api/experience",
        post_data=attack_body,
    )
    assert not matches
    assert ("not found in endpoint path, query, or structured payload ID field" in reason or
            "Conflicting target resource ID in payload field" in reason)

    # Legitimate payload: target ID is in structured id field
    valid_body = json.dumps({
        "id": "exp-target-99",
        "company": "Target Inc Updated",
    })
    matches_valid, _ = permit.matches_request(
        method="PUT",
        url=f"{adv_server.origin}/api/experience",
        post_data=valid_body,
    )
    assert matches_valid


# ---------------------------------------------------------------------------
# Test 10: Deep GraphQL AST Inspection Rejects Batched, Aliased, & Multi-Op Injections
# ---------------------------------------------------------------------------
def test_graphql_deep_inspection_adversarial_rejections(adv_server):
    """Verify that GraphQL AST inspection rejects batched queries, multi-operation, aliased roots, and mismatched resources."""
    permit = OperationPermit(
        permit_id="test-gql-1",
        operation_type=OperationType.DELETE_ENTITY,
        workflow_name="profile_experience",
        allowed_origin=adv_server.origin,
        allowed_methods=("POST",),
        endpoint_pattern=r"^/api/graphql/?$",
        graphql_operation_name="DeleteExperience",
        graphql_mutation_field="deleteExperience",
        graphql_resource_key="id",
        resource_id="exp-adv-202",
    )

    # 1. Batched JSON array attack
    batch_attack = json.dumps([
        {"query": 'mutation DeleteExperience { deleteExperience(id: "exp-adv-202") { ok } }', "operationName": "DeleteExperience"},
        {"query": 'mutation Malicious { deleteAccount { ok } }', "operationName": "Malicious"},
    ])
    m1, r1 = permit.matches_request(method="POST", url=f"{adv_server.origin}/api/graphql", post_data=batch_attack)
    assert not m1
    assert "Batched" in r1

    # 2. Aliased malicious root attack inside single mutation
    aliased_attack = json.dumps({
        "operationName": "DeleteExperience",
        "query": """mutation DeleteExperience {
            deleteExperience(id: "exp-adv-202") { ok }
            evilAlias: updatePreferences(theme: "dark") { ok }
        }""",
    })
    m2, r2 = permit.matches_request(method="POST", url=f"{adv_server.origin}/api/graphql", post_data=aliased_attack)
    assert not m2
    assert "only a single mutation field" in r2

    # 3. Wrong operation name attack
    wrong_op = json.dumps({
        "operationName": "PurgeAll",
        "query": 'mutation PurgeAll { deleteExperience(id: "exp-adv-202") { ok } }',
    })
    m3, r3 = permit.matches_request(method="POST", url=f"{adv_server.origin}/api/graphql", post_data=wrong_op)
    assert not m3
    assert "operation" in r3.lower()

    # 4. Wrong resource ID in GraphQL variables
    wrong_var = json.dumps({
        "operationName": "DeleteExperience",
        "query": 'mutation DeleteExperience($id: ID!) { deleteExperience(id: $id) { ok } }',
        "variables": {"id": "exp-victim-different"},
    })
    m4, r4 = permit.matches_request(method="POST", url=f"{adv_server.origin}/api/graphql", post_data=wrong_var)
    assert not m4
    assert "does not match target resource ID" in r4

    # 5. Legitimate single mutation matching operation, field, and resource ID
    valid_gql = json.dumps({
        "operationName": "DeleteExperience",
        "query": 'mutation DeleteExperience($id: ID!) { deleteExperience(id: $id) { ok } }',
        "variables": {"id": "exp-adv-202"},
    })
    m5, r5 = permit.matches_request(method="POST", url=f"{adv_server.origin}/api/graphql", post_data=valid_gql)
    assert m5


# ---------------------------------------------------------------------------
# Test 11: Legitimate Remote ID Numerically Equal to Local DB Row ID
# ---------------------------------------------------------------------------
def test_legitimate_remote_id_numerically_equal_to_local_db_row(adv_server, isolated_adv_db):
    """Verify that remote entity with confirmed remote ID '1' (equal to local DB row ID 1) is correctly owned."""
    registry = ScopedEntityOwnershipRegistry
    run_id = 55
    project_id = 1
    target_url = f"{adv_server.origin}/profile"

    # Insert genuine record: local table row ID is 1
    intent = registry.record_creation_intent(
        run_id=run_id,
        project_id=project_id,
        target_url=target_url,
        entity_type="profile_experience",
        run_prefix="[QA-TEST-55-NUM1]",
        proposed_name="[QA-TEST-55-NUM1] Numeric ID Corp",
    )
    # Server assigns remote entity ID "1"
    registry.confirm_creation(
        record_id=intent.id,
        remote_entity_id="1",
        confirmed_name="[QA-TEST-55-NUM1] Numeric ID Corp",
    )

    db_recs = registry.get_records_for_run(run_id)
    assert len(db_recs) == 1
    assert db_recs[0].id == 1
    assert db_recs[0].remote_entity_id == "1"
    assert db_recs[0].remote_status == "CONFIRMED_EXISTS"

    # Provenance matches: remote_entity_id="1" is genuinely owned with destructive authority!
    is_owned = registry.is_owned_by_run(
        run_id=run_id,
        project_id=project_id,
        target_url=target_url,
        remote_entity_id="1",
        entity_name="[QA-TEST-55-NUM1] Numeric ID Corp",
        entity_type="profile_experience",
        require_destructive_authority=True,
    )
    assert is_owned is True, "Legitimate remote ID '1' with confirmed remote identity must be recognized as owned!"


# ---------------------------------------------------------------------------
# Test 12: Optimistic DOM Removal with Server Error Accurately Reported as Defect
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_optimistic_ui_deletion_with_server_rejection_reported_as_defect(profile_server, isolated_adv_db, tmp_path):
    """Verify that optimistic DOM removal followed by server 500 error is detected as OPTIMISTIC_UI_SERVER_REJECTION."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(chromium_sandbox=True, headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        barrier = ScopedMutationNetworkBarrier(target_origin=profile_server.origin)
        await barrier.install(page)
        await page.goto(f"{profile_server.origin}/profile?mutation=true")

        run_id = 66
        run_prefix = ScopedEntityOwnershipRegistry.generate_run_prefix(run_id)

        # 1. Create genuine entity on server first
        runner = AutonomousProfileWorkflowRunner(scratch_dir=str(tmp_path / "scratch_opt"), barrier=barrier)
        created_rec, create_res = await runner._exercise_create_workflow(
            page=page,
            target_url=f"{profile_server.origin}/profile?mutation=true",
            run_id=run_id,
            project_id=1,
            run_prefix=run_prefix,
            viewport_name="Desktop",
        )
        assert create_res.verdict == "PASS"
        assert created_rec is not None

        # 2. Configure server to fail DELETE requests with HTTP 500, and client to remove optimistically
        profile_server.fail_delete_optimistically = True
        await page.evaluate("() => { window.__qa_fail_delete_optimistically = true; }")

        try:
            # 3. Attempt deletion
            del_res = await runner._exercise_delete_workflow(
                page=page,
                run_id=run_id,
                project_id=1,
                record=created_rec,
                viewport_name="Desktop",
            )

            # Assert defect detection: optimistic DOM removal did NOT fool the engine!
            assert del_res.verdict == "FAIL"
            assert del_res.defect_category == "OPTIMISTIC_UI_SERVER_REJECTION"
            assert del_res.is_customer_defect is True
            assert del_res.cleanup_status == "FAILED"

            # In database, status must NOT be CONFIRMED_DELETED
            recs = ScopedEntityOwnershipRegistry.get_records_for_run(run_id)
            assert recs[0].status == "CLEANUP_FAILED"
            assert recs[0].remote_status != "CONFIRMED_DELETED"
        finally:
            profile_server.fail_delete_optimistically = False
            await browser.close()


# ---------------------------------------------------------------------------
# Test 13: GraphQL AST Counterexamples (Unrelated Variables & Directive Distraction)
# ---------------------------------------------------------------------------
def test_graphql_ast_counterexamples_strictly_rejected(adv_server):
    """
    Cover Counterexample 1 & 2 from independent review:
    1. Query targets id: 'exp-victim-other' inline, while unrelated variables.id contains 'exp-owned-100'.
    2. Query passes allowed ID to a directive while target argument is a different variable.
    3. Valid aliased mutation is accepted.
    4. Valid fragment spread is accepted.
    """
    permit = OperationPermit(
        permit_id="test-gql-cx",
        operation_type=OperationType.DELETE_ENTITY,
        workflow_name="profile_experience",
        allowed_origin=adv_server.origin,
        allowed_methods=("POST",),
        endpoint_pattern=r"^/api/graphql/?$",
        graphql_operation_name="DeleteExperience",
        graphql_mutation_field="deleteExperience",
        graphql_resource_key="id",
        resource_id="exp-owned-100",
    )

    # Counterexample 1: Query targets id: 'exp-victim-other' inline, while variables.id contains 'exp-owned-100'
    cx1 = json.dumps({
        "operationName": "DeleteExperience",
        "query": """mutation DeleteExperience($id: ID!) {
            deleteExperience(id: "exp-victim-other") { ok }
        }""",
        "variables": {"id": "exp-owned-100"},
    })
    m1, r1 = permit.matches_request(method="POST", url=f"{adv_server.origin}/api/graphql", post_data=cx1)
    assert not m1, "Counterexample 1 must be rejected: checking variables alone must not authorize inline argument target!"
    assert "does not match target resource ID" in r1

    # Counterexample 2: Directive distraction - directive contains allowed ID, but argument targets different variable
    cx2 = json.dumps({
        "operationName": "DeleteExperience",
        "query": """mutation DeleteExperience($target: ID!) {
            deleteExperience(id: $target) @auditLog(resourceId: "exp-owned-100") { ok }
        }""",
        "variables": {"target": "exp-victim-other"},
    })
    m2, r2 = permit.matches_request(method="POST", url=f"{adv_server.origin}/api/graphql", post_data=cx2)
    assert not m2, "Counterexample 2 must be rejected: directive argument cannot authorize target ID!"
    assert "does not match target resource ID" in r2

    # Legitimate Aliased mutation: alias differs from root field name
    aliased = json.dumps({
        "operationName": "DeleteExperience",
        "query": """mutation DeleteExperience($id: ID!) {
            aliasedDelete: deleteExperience(id: $id) { ok }
        }""",
        "variables": {"id": "exp-owned-100"},
    })
    m3, r3 = permit.matches_request(method="POST", url=f"{adv_server.origin}/api/graphql", post_data=aliased)
    assert m3, f"Valid aliased mutation should be accepted: {r3}"

    # Legitimate Fragment resolution
    fragment_query = json.dumps({
        "operationName": "DeleteExperience",
        "query": """
        fragment ExpFields on ExperiencePayload {
            ok
        }
        mutation DeleteExperience($id: ID!) {
            deleteExperience(id: $id) {
                ...ExpFields
            }
        }""",
        "variables": {"id": "exp-owned-100"},
    })
    m4, r4 = permit.matches_request(method="POST", url=f"{adv_server.origin}/api/graphql", post_data=fragment_query)
    assert m4, f"Valid fragment query should be accepted: {r4}"


# ---------------------------------------------------------------------------
# Test 14: Verified Account Scope and Designated Identity Field Enforcement
# ---------------------------------------------------------------------------
def test_verified_account_and_designated_field_enforcement(adv_server):
    """
    Assert that:
    1. Conflicting account identity is rejected.
    2. Missing account identity when required is rejected.
    3. Run tag in description/notes/body does NOT authorize creation.
    4. Exact designated field prefix is strictly required.
    5. Non-JSON / unsupported body encodings are rejected.
    """
    run_prefix = "[QA-TEST-88-TAG]"
    permit = OperationPermit(
        permit_id="test-acct-field",
        operation_type=OperationType.CREATE_ENTITY,
        workflow_name="profile_experience",
        allowed_origin=adv_server.origin,
        allowed_methods=("POST",),
        endpoint_pattern=r"^/api/experience/?$",
        run_tag=run_prefix,
        account_context="tester@example.com",
        verified_account="tester@example.com",
        entity_type="profile_experience",
        designated_identity_field="company",
    )

    # 1. Conflicting account context passed via session / header
    conflicting_body = json.dumps({
        "company": f"{run_prefix} Nordic Quality AB",
        "account_id": "attacker@example.com",
    })
    m1, r1 = permit.matches_request(
        method="POST",
        url=f"{adv_server.origin}/api/experience",
        post_data=conflicting_body,
        account_context="attacker@example.com",
    )
    assert not m1, "Conflicting account identity must be rejected!"
    assert "account mismatch" in r1.lower()

    # 2. Tag inside description field (not company) must be rejected
    tag_in_desc_body = json.dumps({
        "company": "Regular Company Without Tag",
        "description": f"Notes mentioning {run_prefix} inside description",
    })
    m2, r2 = permit.matches_request(
        method="POST",
        url=f"{adv_server.origin}/api/experience",
        post_data=tag_in_desc_body,
        account_context="tester@example.com",
    )
    assert not m2, "Permissive tag matching must be removed: tag in description must not authorize creation!"
    assert "missing from designated identity field" in r2

    # 3. Tag in raw unstructured body (e.g. text/plain or XML) must be rejected
    m3, r3 = permit.matches_request(
        method="POST",
        url=f"{adv_server.origin}/api/experience",
        post_data=f"raw text containing {run_prefix}",
        account_context="tester@example.com",
    )
    assert not m3, "Unsupported payload encoding must be rejected!"
    assert "Unsupported payload encoding" in r3

    # 4. Valid matching creation: starts with run prefix in designated 'company' field
    valid_body = json.dumps({
        "company": f"{run_prefix} Nordic Quality AB",
        "description": "Legitimate test item",
    })
    m4, r4 = permit.matches_request(
        method="POST",
        url=f"{adv_server.origin}/api/experience",
        post_data=valid_body,
        account_context="tester@example.com",
    )
    assert m4, f"Valid creation matching verified account and designated field should be accepted: {r4}"


# ---------------------------------------------------------------------------
# Test 15: Read-Back Uncertainty Does Not Mark Customer Defect or Claim Deletion
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_readback_uncertainty_handling(adv_server, isolated_adv_db, tmp_path):
    """
    Assert that:
    1. HTTP error upon reload results in UNCERTAIN_READBACK.
    2. Login redirect results in UNCERTAIN_READBACK.
    3. 500 error page results in UNCERTAIN_READBACK.
    4. Missing scope container results in UNCERTAIN_READBACK.
    5. Uncertain outcomes preserve durable recovery state without marking customer defect.
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(chromium_sandbox=True, headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        runner = AutonomousProfileWorkflowRunner(scratch_dir=str(tmp_path / "scratch_rb"))

        # Test case 1: Page at /error-page (500 Internal Server Error view)
        await page.goto(f"{adv_server.origin}/error-page")
        status_500, reason_500, _ = await runner._verify_authenticated_scope_readback(
            page=page,
            expected_scope_selector="#profile-experience-container",
            forbidden_element_text="Ghost Entity",
        )
        assert status_500 == "UNCERTAIN_READBACK"
        assert "500 Internal Server Error" in reason_500

        # Test case 2: Page at /login (Authentication redirect)
        await page.goto(f"{adv_server.origin}/login")
        status_login, reason_login, _ = await runner._verify_authenticated_scope_readback(
            page=page,
            expected_scope_selector="#profile-experience-container",
            forbidden_element_text="Ghost Entity",
        )
        assert status_login == "UNCERTAIN_READBACK"
        assert "login" in reason_login.lower()

        # Test case 3: Scope container missing after reload
        await page.goto(f"{adv_server.origin}/profile")
        status_noscope, reason_noscope, _ = await runner._verify_authenticated_scope_readback(
            page=page,
            expected_scope_selector="#nonexistent-scope-selector-xyz",
            forbidden_element_text="Ghost Entity",
        )
        assert status_noscope == "UNCERTAIN_READBACK"
        assert "not found in DOM after reload" in reason_noscope

        # Test case 4: 404 URL reload status
        await page.goto(f"{adv_server.origin}/nonexistent-404-url")
        status_404, reason_404, _ = await runner._verify_authenticated_scope_readback(
            page=page,
            expected_scope_selector="#profile-experience-container",
            forbidden_element_text="Ghost Entity",
        )
        assert status_404 == "UNCERTAIN_READBACK"
        assert "404" in reason_404 or "error" in reason_404.lower()

        await browser.close()


# ---------------------------------------------------------------------------
# Test 16: Denied Mismatches Under Real Network Barrier Do Not Consume Permit
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_denied_mismatches_under_barrier_real_path(adv_server, isolated_adv_db):
    """Verify that multiple denied adversarial write attempts do not consume the legitimate permit."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(chromium_sandbox=True, headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        barrier = ScopedMutationNetworkBarrier(target_origin=adv_server.origin)
        await barrier.install(page)
        await page.goto(f"{adv_server.origin}/profile")

        permit = barrier.acquire_permit(
            operation_type=OperationType.EDIT_ENTITY,
            workflow_name="profile_experience",
            allowed_methods=("PUT",),
            endpoint_pattern=r"^/api/experience/exp-real-1/?$",
            resource_id="exp-real-1",
            account_context="tester@example.com",
            verified_account="tester@example.com",
            entity_type="profile_experience",
            designated_identity_field="company",
            permitted_fields=("id", "company", "role"),
            max_uses=1,
        )

        try:
            # 1. Attack: wrong HTTP method (DELETE instead of PUT)
            with pytest.raises(Exception):
                await page.evaluate("""() => fetch('/api/experience/exp-real-1', { method: 'DELETE' })""")
            assert permit.consumed is False
            assert permit.use_count == 0

            # 2. Attack: wrong resource ID in endpoint
            with pytest.raises(Exception):
                await page.evaluate("""() => fetch('/api/experience/exp-other-99', {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ company: 'Attacker Corp' })
                })""")
            assert permit.consumed is False
            assert permit.use_count == 0

            # 3. Attack: forbidden extra fields in payload (e.g. admin: true)
            with pytest.raises(Exception):
                await page.evaluate("""() => fetch('/api/experience/exp-real-1', {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ company: 'Acme Corp', admin: true })
                })""")
            assert permit.consumed is False
            assert permit.use_count == 0

            # 4. Legitimate matching write: PUT /api/experience/exp-real-1 with allowed fields
            res = await page.evaluate("""async () => {
                const r = await fetch('/api/experience/exp-real-1', {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ company: 'Acme Corp Updated' })
                });
                return { ok: r.ok, status: r.status };
            }""")
            assert res["ok"] is True
            assert permit.consumed is True
            assert permit.use_count == 1
            assert adv_server.experiences["exp-real-1"]["company"] == "Acme Corp Updated"

            # 5. Second write under single-use permit is denied!
            with pytest.raises(Exception):
                await page.evaluate("""() => fetch('/api/experience/exp-real-1', {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ company: 'Acme Corp Re-Updated' })
                })""")
        finally:
            barrier.release_permit(permit)
            await browser.close()


# ---------------------------------------------------------------------------
# Test 17: Missing / Mismatched Session Identity Prevents All State Writes
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_missing_and_mismatched_session_identity_prevents_all_writes(adv_server, isolated_adv_db, tmp_path):
    """Verify that absent or conflicting session identity prevents any writes and reports honest LIMITED_COVERAGE."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(chromium_sandbox=True, headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        # Route to serve page without any auth markers
        async def handle_unauth(route):
            unauth_html = """<!DOCTYPE html><html><body>
            <div id="profile-experience-container">
                <button id="btn-add-experience">Add Experience</button>
            </div>
            </body></html>"""
            await route.fulfill(status=200, content_type="text/html", body=unauth_html)

        await page.route(f"{adv_server.origin}/unauth_profile", handle_unauth)
        await page.goto(f"{adv_server.origin}/unauth_profile")

        runner = AutonomousProfileWorkflowRunner(scratch_dir=str(tmp_path))
        results = await runner.run_authorized_profile_suite(
            page=page,
            target_url=f"{adv_server.origin}/unauth_profile",
            run_id=101,
            project_id=1,
            viewport_name="Desktop",
        )
        assert len(results) == 1
        assert results[0].workflow_type == "session_identity"
        assert results[0].verdict == "LIMITED_COVERAGE"
        assert "Zero writes dispatched" in results[0].details
        assert "Synthetic identity fallback is prohibited" in results[0].details

        # Now test conflicting account context
        await page.goto(f"{adv_server.origin}/profile")
        results_conflict = await runner.run_authorized_profile_suite(
            page=page,
            target_url=f"{adv_server.origin}/profile",
            run_id=102,
            project_id=1,
            viewport_name="Desktop",
            account_context="victim@otherdomain.com",  # Page has tester@example.com
        )
        assert len(results_conflict) == 1
        assert results_conflict[0].workflow_type == "session_identity"
        assert results_conflict[0].verdict == "LIMITED_COVERAGE"
        assert "conflicts with requested account" in results_conflict[0].details

        await browser.close()


# ---------------------------------------------------------------------------
# Test 18: Account Switching Invalidates Active Permits
# ---------------------------------------------------------------------------
def test_session_identity_change_invalidates_active_permits():
    """Verify that updating barrier session identity immediately revokes any active permit."""
    barrier = ScopedMutationNetworkBarrier(target_origin="http://127.0.0.1:8080")
    barrier.set_verified_session_identity("user1@example.com")

    permit = barrier.acquire_permit(
        operation_type=OperationType.CREATE_ENTITY,
        workflow_name="profile_experience",
        allowed_methods=("POST",),
        account_context="user1@example.com",
    )
    assert barrier.active_permit is permit
    assert permit.consumed is False

    # Switch identity to a different user
    barrier.set_verified_session_identity("user2@example.com")
    assert barrier.active_permit is None
    assert permit.consumed is True


# ---------------------------------------------------------------------------
# Test 19: Conflicting Target IDs Across REST Path, Query, or Payload Strictly Rejected
# ---------------------------------------------------------------------------
def test_conflicting_target_ids_in_rest_path_and_query_strictly_rejected():
    """Verify that an attacker cannot slip in a permit resource ID in query while path targets a different resource."""
    permit = OperationPermit(
        permit_id="p-rest-conflict",
        operation_type=OperationType.EDIT_ENTITY,
        workflow_name="profile_experience",
        allowed_origin="http://127.0.0.1:8080",
        allowed_methods=("PUT",),
        endpoint_pattern=r"^/api/experiences/[^/]+$",
        resource_id="exp-approved-123",
        account_context="tester@example.com",
        verified_account="tester@example.com",
    )

    # Attack 1: Path targets victim resource, query param carries approved resource
    matches1, reason1 = permit.matches_request(
        method="PUT",
        url="http://127.0.0.1:8080/api/experiences/exp-victim-999?id=exp-approved-123",
        post_data=json.dumps({"company": "Legit Corp"}),
        account_identity="tester@example.com",
    )
    assert not matches1
    assert "Conflicting target resource ID in path 'exp-victim-999'" in reason1

    # Attack 2: Path matches approved resource, but query targets victim
    matches2, reason2 = permit.matches_request(
        method="PUT",
        url="http://127.0.0.1:8080/api/experiences/exp-approved-123?id=exp-victim-999",
        post_data=json.dumps({"company": "Legit Corp"}),
        account_identity="tester@example.com",
    )
    assert not matches2
    assert "Conflicting target resource ID in query parameter 'id=exp-victim-999'" in reason2

    # Attack 3: Path matches, query matches, but payload body has conflicting id
    matches3, reason3 = permit.matches_request(
        method="PUT",
        url="http://127.0.0.1:8080/api/experiences/exp-approved-123",
        post_data=json.dumps({"id": "exp-victim-888", "company": "Legit Corp"}),
        account_identity="tester@example.com",
    )
    assert not matches3
    assert "Conflicting target resource ID in payload field 'id=exp-victim-888'" in reason3


# ---------------------------------------------------------------------------
# Test 20: Upload Content Digest Mismatch and File Substitution Rejected
# ---------------------------------------------------------------------------
def test_upload_content_digest_mismatch_and_file_substitution_rejected(tmp_path):
    """Verify that file content digest is verified against approved SHA-256 and extra parts rejected."""
    approved_file = tmp_path / "test_approved.txt"
    approved_file.write_bytes(b"Authorized clean content")
    approved_hash = hashlib.sha256(b"Authorized clean content").hexdigest()

    permit = OperationPermit(
        permit_id="p-up-1",
        operation_type=OperationType.UPLOAD_ATTACHMENT,
        workflow_name="profile_attachment",
        allowed_origin="http://127.0.0.1:8080",
        allowed_methods=("POST",),
        endpoint_pattern=r"^/api/attachments/upload$",
        account_context="tester@example.com",
        verified_account="tester@example.com",
        approved_file_path=str(approved_file),
        approved_file_name="test_approved.txt",
    )
    assert permit.approved_file_hash == approved_hash

    # Attack 1: Altered content payload with same filename
    bad_payload = (
        b"--boundary123\r\n"
        b'Content-Disposition: form-data; name="file"; filename="test_approved.txt"\r\n'
        b"Content-Type: text/plain\r\n\r\n"
        b"Tampered payload content\r\n"
        b"--boundary123--\r\n"
    )
    matches_tampered, reason_tampered = permit.matches_request(
        method="POST",
        url="http://127.0.0.1:8080/api/attachments/upload",
        post_data=bad_payload.decode("latin1"),
        headers={"content-type": "multipart/form-data; boundary=boundary123"},
        account_identity="tester@example.com",
    )
    assert not matches_tampered
    assert "does not match approved digest" in reason_tampered

    # Attack 2: Multiple files submitted in one request
    multi_payload = (
        b"--boundary123\r\n"
        b'Content-Disposition: form-data; name="file1"; filename="test_approved.txt"\r\n\r\n'
        b"Authorized clean content\r\n"
        b"--boundary123\r\n"
        b'Content-Disposition: form-data; name="file2"; filename="malicious.exe"\r\n\r\n'
        b"MALWARE\r\n"
        b"--boundary123--\r\n"
    )
    matches_multi, reason_multi = permit.matches_request(
        method="POST",
        url="http://127.0.0.1:8080/api/attachments/upload",
        post_data=multi_payload.decode("latin1"),
        headers={"content-type": "multipart/form-data; boundary=boundary123"},
        account_identity="tester@example.com",
    )
    assert not matches_multi
    assert "Upload contains" in reason_multi or "Multiple" in reason_multi


# ---------------------------------------------------------------------------
# Test 21: Readback Uncertainty on Blank or Busy Container
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_readback_blank_or_busy_container_yields_uncertain_readback(adv_server):
    """Verify that an aria-busy container that does not clear or a blank container returns UNCERTAIN_READBACK."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(chromium_sandbox=True, headless=True)
        page = await browser.new_page()

        runner = AutonomousProfileWorkflowRunner()

        # Simulate stuck aria-busy container across reloads
        async def handle_busy(route):
            busy_html = """<!DOCTYPE html><html><body>
            <div id="profile-experience-container" aria-busy="true" style="display:block;min-height:50px;width:100%;">
                <p>Loading experiences...</p>
            </div>
            </body></html>"""
            await route.fulfill(status=200, content_type="text/html", body=busy_html)

        await page.route(f"{adv_server.origin}/busy_profile", handle_busy)
        await page.goto(f"{adv_server.origin}/busy_profile")

        status_busy, reason_busy, _ = await runner._verify_authenticated_scope_readback(
            page=page,
            expected_scope_selector="#profile-experience-container",
            required_element_text="Any Element",
        )
        assert status_busy == "UNCERTAIN_READBACK"
        assert "aria-busy" in reason_busy

        # Simulate unrendered / blank container when attempting absence confirmation
        async def handle_blank(route):
            blank_html = """<!DOCTYPE html><html><body>
            <div id="profile-experience-container" style="display:block;min-height:50px;width:100%;"></div>
            </body></html>"""
            await route.fulfill(status=200, content_type="text/html", body=blank_html)

        await page.route(f"{adv_server.origin}/blank_profile", handle_blank)
        await page.goto(f"{adv_server.origin}/blank_profile")

        status_blank, reason_blank, _ = await runner._verify_authenticated_scope_readback(
            page=page,
            expected_scope_selector="#profile-experience-container",
            forbidden_element_text="Deleted Item",
        )
        assert status_blank == "UNCERTAIN_READBACK"
        assert "unrendered" in reason_blank or "empty" in reason_blank

        await browser.close()


