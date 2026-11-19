"""Fifth Review Failing Acceptance Tests for Scoped Mutation.

Demonstrates concrete defects identified in SCOPED_MUTATION_FIFTH_REVIEW_2026-10-21.md:
1. Incomplete read-back: Heading-only container, delayed content without spinner, paginated/filtered container, and wrong origin with same account marker.
2. Authenticated actor identity: Viewed subject's data-user-id promoted to actor, account switching between discovery and dispatch, and permissive cached fallbacks.
3. Actual upload dispatch: Opaque upload token truthiness without request binding, substituted file bytes, extra files, and wrong destination.
"""

import asyncio
import hashlib
import json
import pytest
from playwright.async_api import async_playwright

from app.qa.scoped_mutation import (
    AutonomousProfileWorkflowRunner,
    AuthenticatedIdentityDiscovery,
    OperationPermit,
    OperationType,
    ScopedMutationNetworkBarrier,
    ScopedProfileWorkflowResult,
)
import threading
from tests.test_scoped_mutation_adversarial import AdversarialProfileServer


@pytest.fixture(scope="module")
def adv_server():
    server = AdversarialProfileServer("127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


# ===========================================================================
# 1. Incomplete Read-Back Counterexamples
# ===========================================================================

@pytest.mark.asyncio
async def test_readback_heading_only_container_must_not_confirm_deletion():
    """Exact reproduction of review_readback_heading_probe.py:
    A visible container containing only heading text ('Experience') without items,
    and without an authoritative completeness / empty-state indicator,
    MUST NOT return CONFIRMED_SUCCESS. It must fail-safe to UNCERTAIN_READBACK.
    """
    runner = AutonomousProfileWorkflowRunner()

    class Loc:
        def __init__(self, count, text="Experience"):
            self.n = count
            self.t = text
        @property
        def first(self):
            return self
        async def count(self):
            return self.n
        async def is_visible(self):
            return True
        async def get_attribute(self, k):
            return None
        async def inner_text(self):
            return self.t
        def locator(self, s):
            # No matching child elements or items
            return Loc(0, "")

    class Page:
        url = "https://example.test/profile"
        async def reload(self, **kw):
            return type("Response", (), {"status": 200})()
        async def wait_for_timeout(self, n):
            pass
        async def evaluate(self, s):
            if "candidates" in s or "authenticated" in s or "document.querySelector" in s:
                return ["account-A"]
            return "Experience"
        def locator(self, s):
            return Loc(1, "Experience")

    # With current code, this returns CONFIRMED_SUCCESS (the defect!)
    status, reason, diag = await runner._verify_authenticated_scope_readback(
        page=Page(),
        expected_scope_selector="#section-experience-management",
        forbidden_element_text="owned-test",
        target_entity_id="owned-1",
        expected_account="account-A",
    )
    # ACCEPTANCE CRITERIA: A heading-only container MUST NOT confirm deletion!
    assert status == "UNCERTAIN_READBACK"
    assert "completeness" in reason.lower() or "empty-state" in reason.lower() or "unrendered" in reason.lower() or "heading" in reason.lower()


@pytest.mark.asyncio
async def test_readback_delayed_content_without_spinner_must_be_uncertain(adv_server):
    """When a container has no items and no authoritative empty-state marker
    (e.g. data is still loading asynchronously without an aria-busy spinner),
    readback must NOT declare absence as confirmed success.
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        # Serve a profile where experience section has heading and container, but items are still in-flight
        async def handle_delayed(route):
            html = """<!DOCTYPE html><html><head>
            <meta name="authenticated-actor-id" content="tester@example.com">
            </head><body>
            <div id="auth-session" data-authenticated-actor="tester@example.com">tester@example.com</div>
            <div id="section-experience-management">
                <h3>Experience</h3>
                <div id="experience-list"></div>
            </div>
            </body></html>"""
            await route.fulfill(status=200, content_type="text/html", body=html)

        await page.route(f"{adv_server.origin}/delayed_profile", handle_delayed)
        await page.goto(f"{adv_server.origin}/delayed_profile")

        runner = AutonomousProfileWorkflowRunner()
        status, reason, _ = await runner._verify_authenticated_scope_readback(
            page=page,
            expected_scope_selector="#section-experience-management",
            forbidden_element_text="[QA-TEST-100] Item",
            target_entity_id="exp-100",
            expected_account="tester@example.com",
            intended_origin=adv_server.origin,
        )
        assert status != "CONFIRMED_SUCCESS"
        assert status == "UNCERTAIN_READBACK"
        await browser.close()


@pytest.mark.asyncio
async def test_readback_filtered_or_paginated_container_must_be_uncertain(adv_server):
    """When a scope container shows pagination or active filters (e.g. 'Page 1 of 3'),
    the absence of an item on the current view does not prove account-level absence.
    Must return UNCERTAIN_READBACK without completeness evidence.
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        async def handle_paginated(route):
            html = """<!DOCTYPE html><html><head>
            <meta name="authenticated-actor-id" content="tester@example.com">
            </head><body>
            <div id="auth-session" data-authenticated-actor="tester@example.com">tester@example.com</div>
            <div id="section-experience-management">
                <h3>Experience</h3>
                <div class="pagination-info" data-pagination="true">Showing 1-10 of 25 entries (Page 1 of 3)</div>
                <div class="exp-item" id="exp-other-1"><h4>Other Corp</h4></div>
            </div>
            </body></html>"""
            await route.fulfill(status=200, content_type="text/html", body=html)

        await page.route(f"{adv_server.origin}/paginated_profile", handle_paginated)
        await page.goto(f"{adv_server.origin}/paginated_profile")

        runner = AutonomousProfileWorkflowRunner()
        status, reason, _ = await runner._verify_authenticated_scope_readback(
            page=page,
            expected_scope_selector="#section-experience-management",
            forbidden_element_text="[QA-TEST-100] Target",
            target_entity_id="exp-100",
            expected_account="tester@example.com",
            intended_origin=adv_server.origin,
        )
        assert status != "CONFIRMED_SUCCESS"
        assert status == "UNCERTAIN_READBACK"
        assert "paginat" in reason.lower() or "filter" in reason.lower() or "incomplete" in reason.lower()
        await browser.close()


@pytest.mark.asyncio
async def test_readback_different_origin_same_account_must_be_uncertain(adv_server):
    """If post-reload page is on a different origin than the bound intended_origin,
    even if it serves the same account identifier, readback must strictly reject with UNCERTAIN_READBACK.
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        # The page is on adv_server.origin, but intended_origin was bound to https://trusted.example.test
        await page.goto(f"{adv_server.origin}/profile")

        runner = AutonomousProfileWorkflowRunner()
        status, reason, _ = await runner._verify_authenticated_scope_readback(
            page=page,
            expected_scope_selector="#section-experience-management",
            forbidden_element_text="Ghost",
            expected_account="tester@example.com",
            intended_origin="https://trusted.example.test",  # Different origin!
        )
        assert status == "UNCERTAIN_READBACK"
        assert "origin" in reason.lower()
        await browser.close()


# ===========================================================================
# 2. Authenticated Actor Identity Counterexamples
# ===========================================================================

@pytest.mark.asyncio
async def test_viewed_subject_data_user_id_must_not_become_authenticated_actor(adv_server):
    """A viewed profile subject or user card carrying data-user-id or data-account-id
    must NOT be promoted to the logged-in actor identity.
    Only explicit authenticated actor contracts (e.g. data-authenticated-actor, window.__SESSION_ACTOR__)
    may establish actor identity.
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        # Page viewing another user's profile: subject is 'victim_other_user',
        # but there is NO logged-in session actor!
        async def handle_viewed_profile(route):
            html = """<!DOCTYPE html><html><head>
            <meta name="user-id" content="victim_other_user">
            <meta name="profile-subject-id" content="victim_other_user">
            </head><body>
            <div class="profile-header" data-user-id="victim_other_user" data-account-id="victim_other_user">
                <h1>Victim Other User Profile</h1>
            </div>
            </body></html>"""
            await route.fulfill(status=200, content_type="text/html", body=html)

        await page.route(f"{adv_server.origin}/other_profile", handle_viewed_profile)
        await page.goto(f"{adv_server.origin}/other_profile")

        # Discovering session identity must NOT return 'victim_other_user' as the actor!
        discovered = await AuthenticatedIdentityDiscovery.discover_session_identity(page, adv_server.origin)
        assert discovered is None, f"Defect reproduced: promoted viewed subject '{discovered}' to authenticated actor!"

        await browser.close()


@pytest.mark.asyncio
async def test_account_switching_between_discovery_and_dispatch_must_be_blocked_in_production(adv_server):
    """When account switching occurs on the active page between initial discovery and write dispatch,
    the production runner/barrier must detect the switch dynamically and block the write.
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        # Initially logged in as 'user_alice@example.com'
        async def handle_switchable(route):
            html = """<!DOCTYPE html><html><head>
            <meta name="authenticated-actor-id" content="user_alice@example.com">
            </head><body>
            <div id="auth-session" data-authenticated-actor="user_alice@example.com">user_alice@example.com</div>
            <div id="section-experience-management">
                <button id="btn-add-experience">Add</button>
            </div>
            </body></html>"""
            await route.fulfill(status=200, content_type="text/html", body=html)

        await page.route(f"{adv_server.origin}/switchable_profile", handle_switchable)
        await page.goto(f"{adv_server.origin}/switchable_profile")

        barrier = ScopedMutationNetworkBarrier(target_origin=adv_server.origin)
        await barrier.install(page)

        # Initial discovery establishes user_alice
        initial_actor = await AuthenticatedIdentityDiscovery.discover_session_identity(page, adv_server.origin)
        assert initial_actor == "user_alice@example.com"
        barrier.set_verified_session_identity(initial_actor)

        # Alice acquires a permit
        permit = barrier.acquire_permit(
            operation_type=OperationType.CREATE_ENTITY,
            workflow_name="profile_experience",
            allowed_methods=("POST",),
            endpoint_pattern=r"^/api/experience/?$",
            account_context="user_alice@example.com",
            verified_account="user_alice@example.com",
        )

        # Now simulate an account switch in the browser before the write is dispatched:
        # User logs in as user_bob!
        await page.evaluate("""() => {
            const meta = document.querySelector('meta[name="authenticated-actor-id"]');
            if (meta) meta.setAttribute('content', 'user_bob@example.com');
            const s = document.querySelector('#auth-session');
            if (s) {
                s.setAttribute('data-authenticated-actor', 'user_bob@example.com');
                s.textContent = 'user_bob@example.com';
            }
        }""")

        # Attempt to dispatch the write from the page under the permit issued to Alice:
        # The barrier must detect that active page session is now Bob, conflicting with Alice's permit!
        blocked = False
        try:
            res = await page.evaluate("""async (origin) => {
                const r = await fetch(origin + '/api/experience', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({company: 'Switched Account Corp'})
                });
                return { status: r.status, ok: r.ok };
            }""", adv_server.origin)
            if not res.get("ok"):
                blocked = True
        except Exception:
            blocked = True

        assert blocked is True, "Defect: write under old account permit succeeded after account switch!"
        await browser.close()


# ===========================================================================
# 3. Actual Upload Dispatch Counterexamples
# ===========================================================================

def test_unrelated_opaque_upload_rejected_while_permit_active():
    """An unrelated opaque upload without request token binding must NOT pass
    merely because a controlled_dispatch_token exists on the permit.
    """
    permit = OperationPermit(
        permit_id="p-up-opaque",
        operation_type=OperationType.UPLOAD_ATTACHMENT,
        workflow_name="profile_attachment",
        allowed_origin="http://127.0.0.1:8080",
        allowed_methods=("POST",),
        endpoint_pattern=r"^/api/attachments/upload$",
        account_context="tester@example.com",
        verified_account="tester@example.com",
        approved_file_name="verified_doc.pdf",
        controlled_dispatch_token="approved-token-12345",
    )

    # An attacker/unrelated request arrives without body (opaque streaming)
    # and without presenting the approved dispatch token:
    matches, reason = permit.matches_request(
        method="POST",
        url="http://127.0.0.1:8080/api/attachments/upload",
        post_data=None,  # Opaque / streaming request
        headers={"content-type": "application/octet-stream"},  # No token!
        account_identity="tester@example.com",
    )
    # ACCEPTANCE CRITERIA: An unbound opaque request MUST NOT pass!
    assert not matches, f"Defect reproduced: opaque upload without request token binding passed: {reason}"
    assert "token" in reason.lower() or "unsupported" in reason.lower() or "unbound" in reason.lower()


def test_upload_rejected_on_wrong_destination_or_parent():
    """Uploads targeting an unapproved destination path or wrong parent resource ID
    must be strictly rejected.
    """
    permit = OperationPermit(
        permit_id="p-up-parent",
        operation_type=OperationType.UPLOAD_ATTACHMENT,
        workflow_name="profile_attachment",
        allowed_origin="http://127.0.0.1:8080",
        allowed_methods=("POST",),
        endpoint_pattern=r"^/api/experiences/exp-123/attachments$",
        parent_resource_id="exp-123",
        account_context="tester@example.com",
        verified_account="tester@example.com",
        approved_file_name="test.txt",
    )

    # Request targets a different parent resource
    matches, reason = permit.matches_request(
        method="POST",
        url="http://127.0.0.1:8080/api/experiences/exp-VICTIM-999/attachments",
        post_data="dummy",
        account_identity="tester@example.com",
    )
    assert not matches
    assert "destination" in reason.lower() or "parent" in reason.lower() or "pattern" in reason.lower()


def test_upload_rejected_on_substituted_bytes():
    """Uploaded content whose bytes differ from approved_file_hash or approved_file_size
    must be rejected even if filename matches.
    """
    import hashlib
    approved_bytes = b"authentic certificate content"
    approved_hash = hashlib.sha256(approved_bytes).hexdigest()
    
    permit = OperationPermit(
        permit_id="p-up-tamper",
        operation_type=OperationType.UPLOAD_ATTACHMENT,
        workflow_name="profile_attachment",
        allowed_origin="http://127.0.0.1:8080",
        allowed_methods=("POST",),
        endpoint_pattern=r"^/api/attachments$",
        account_context="tester@example.com",
        verified_account="tester@example.com",
        approved_file_name="cert.pdf",
        approved_file_hash=approved_hash,
        approved_file_size=len(approved_bytes),
    )

    # Substituted malicious bytes
    tampered_bytes = b"MALICIOUS INJECTED PAYLOAD"
    multipart_body = (
        b"--boundary123\r\n"
        b'Content-Disposition: form-data; name="file"; filename="cert.pdf"\r\n'
        b"Content-Type: application/pdf\r\n\r\n"
        + tampered_bytes +
        b"\r\n--boundary123--\r\n"
    )

    matches, reason = permit.matches_request(
        method="POST",
        url="http://127.0.0.1:8080/api/attachments",
        post_data=multipart_body,
        headers={"content-type": "multipart/form-data; boundary=boundary123"},
        account_identity="tester@example.com",
    )
    assert not matches
    assert "hash" in reason.lower() or "digest" in reason.lower() or "size" in reason.lower()


def test_upload_rejected_on_extra_files_in_multipart():
    """Multipart upload containing extra files beyond the single approved file must be rejected."""
    permit = OperationPermit(
        permit_id="p-up-multi",
        operation_type=OperationType.UPLOAD_ATTACHMENT,
        workflow_name="profile_attachment",
        allowed_origin="http://127.0.0.1:8080",
        allowed_methods=("POST",),
        endpoint_pattern=r"^/api/attachments$",
        account_context="tester@example.com",
        verified_account="tester@example.com",
        approved_file_name="approved.pdf",
    )

    # Two files included in payload
    multipart_body = (
        b"--boundary123\r\n"
        b'Content-Disposition: form-data; name="file1"; filename="approved.pdf"\r\n'
        b"Content-Type: application/pdf\r\n\r\n"
        b"content1\r\n"
        b"--boundary123\r\n"
        b'Content-Disposition: form-data; name="file2"; filename="unapproved_extra.exe"\r\n'
        b"Content-Type: application/octet-stream\r\n\r\n"
        b"malware\r\n"
        b"--boundary123--\r\n"
    )

    matches, reason = permit.matches_request(
        method="POST",
        url="http://127.0.0.1:8080/api/attachments",
        post_data=multipart_body,
        headers={"content-type": "multipart/form-data; boundary=boundary123"},
        account_identity="tester@example.com",
    )
    assert not matches
    assert "multiple" in reason.lower() or "extra" in reason.lower() or "only single" in reason.lower()


def test_upload_rejected_when_filename_substring_only_without_parts():
    """If multipart post_data merely contains the filename substring as text
    without actual multipart file part headers (filename="..."), it must be rejected.
    """
    permit = OperationPermit(
        permit_id="p-up-substring",
        operation_type=OperationType.UPLOAD_ATTACHMENT,
        workflow_name="profile_attachment",
        allowed_origin="http://127.0.0.1:8080",
        allowed_methods=("POST",),
        endpoint_pattern=r"^/api/attachments$",
        account_context="tester@example.com",
        verified_account="tester@example.com",
        approved_file_name="approved.pdf",
    )

    # Post data with approved.pdf in a comment/text field, but not as an established file part
    raw_payload = b"field1=notes_mentioning_approved.pdf&field2=other"

    matches, reason = permit.matches_request(
        method="POST",
        url="http://127.0.0.1:8080/api/attachments",
        post_data=raw_payload,
        headers={"content-type": "multipart/form-data; boundary=boundary123"},
        account_identity="tester@example.com",
    )
    assert not matches, "Defect: substring match accepted payload without multipart file parts!"
    assert "part" in reason.lower() or "header" in reason.lower() or "filename=" in reason.lower()

