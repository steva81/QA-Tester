"""Scoped Authorized Mutation & State-Changing Workflow Support.

Provides bounded, operation-scoped execution of authorized profile state changes:
1. Deny-by-default network barrier with single-use, time-bounded Operation Permits.
2. Durable SQLite ownership journal recording pre-action intent and post-action confirmed remote identity.
3. Strict ownership verification: rejects local registry ID collisions, substring matching, or cross-boundary claims.
4. Genuine upload lifecycle: targets verified attachment workflows (strictly preserving avatars and existing files),
   requires confirmed remote deletion, and tracks local file cleanup separately from remote cleanup.
5. Workflow-scoped UI actions: validates target section containers, rejects global fallback clicks, and excludes sensitive controls.
6. Responsive viewports (Desktop 1366x768 and Mobile 390x844).
7. Honest reporting: fail-closed ambiguity, preserved recovery details on failure or interruption.
"""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, asdict, field
from email.parser import BytesParser
from email import policy as email_policy
import hashlib
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import time
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Sequence, Set, Tuple
import urllib.parse
import uuid

from app.models.database import get_db_connection
from app.qa.browser_snapshot_adapter import CancellationToken
from app.utils import get_canonical_origin

try:
    from graphql import (
        parse as gql_parse,
        DocumentNode,
        OperationDefinitionNode,
        FragmentDefinitionNode,
        FieldNode,
        VariableNode,
        ArgumentNode,
        DirectiveNode,
        StringValueNode,
        IntValueNode,
        FloatValueNode,
        BooleanValueNode,
        EnumValueNode,
        ObjectValueNode,
        ListValueNode,
        InlineFragmentNode,
        FragmentSpreadNode,
    )
    GRAPHQL_AVAILABLE = True
except ImportError:
    GRAPHQL_AVAILABLE = False


class AuthenticatedIdentityDiscovery:
    """Discovers and verifies authenticated session identity from target page and session."""

    @classmethod
    async def discover_session_identity(cls, page: Any, intended_origin: str) -> Optional[str]:
        """Inspects active page to authoritatively discover authenticated session identity.

        Enforces:
        1. Current page URL must match intended_origin.
        2. Page must not be in a login / auth redirect state.
        3. Extracts identity ONLY from explicit authenticated-session contracts:
           - meta tags: <meta name="authenticated-actor-id">, <meta name="authenticated-user">,
             <meta name="session-actor">, <meta name="session-user-id">
           - explicit data attributes on session elements:
             [data-authenticated-actor], [data-authenticated-user], [data-session-user-id], [data-session-actor],
             #auth-session[data-user-id], #auth-session[data-actor-id], #auth-session[data-account-id]
           - JS session objects: window.__SESSION_ACTOR__, window.__AUTH_USER__, window.__CURRENT_USER__, window.__SESSION_USER__
           - authenticated profile session indicator: #auth-session, .auth-session, #authenticated-session
        4. Rejects generic data-user-id or data-account-id on viewed subjects, cards, or root body.
        5. Detects conflicting evidence: if distinct conflicting identities exist across session markers, returns None.
        """
        try:
            cur_url = getattr(page, "url", "") or ""
            if not cur_url:
                return None
            cur_origin = get_canonical_origin(cur_url)
            if cur_origin != intended_origin:
                return None

            cur_lower = cur_url.lower()
            if any(p in cur_lower for p in ("/login", "/signin", "/auth", "redirect=")):
                return None

            discovered = await page.evaluate("""() => {
                const candidates = new Set();

                // 1. Explicit session meta tags only (no generic 'user-id' or 'account-id')
                for (const name of ['authenticated-actor-id', 'authenticated-user', 'session-actor', 'session-user-id']) {
                    const el = document.querySelector(`meta[name='${name}']`) || document.querySelector(`meta[property='${name}']`);
                    if (el && el.content && el.content.trim()) {
                        candidates.add(el.content.trim());
                    }
                }

                // 2. Explicit session attributes on auth elements or containers
                for (const attr of ['data-authenticated-actor', 'data-authenticated-user', 'data-session-user-id', 'data-session-actor']) {
                    const elements = document.querySelectorAll(`[${attr}]`);
                    elements.forEach(el => {
                        const val = el.getAttribute(attr);
                        if (val && val.trim()) candidates.add(val.trim());
                    });
                }
                const dedicatedSession = document.querySelector('#auth-session, #authenticated-session, .session-identity');
                if (dedicatedSession) {
                    for (const attr of ['data-user-id', 'data-actor-id', 'data-account-id', 'data-authenticated-actor', 'data-authenticated-user']) {
                        const val = dedicatedSession.getAttribute(attr);
                        if (val && val.trim()) candidates.add(val.trim());
                    }
                }

                // 3. Window session globals
                for (const key of ['__SESSION_ACTOR__', '__AUTH_USER__', '__CURRENT_USER__', '__SESSION_USER__']) {
                    if (window[key]) {
                        const u = window[key];
                        if (typeof u === 'string' && u.trim()) candidates.add(u.trim());
                        if (typeof u === 'object' && u !== null) {
                            const candidate = u.email || u.id || u.username || u.userId || u.accountId || u.actorId;
                            if (candidate && String(candidate).trim()) candidates.add(String(candidate).trim());
                        }
                    }
                }

                // 4. Authenticated profile session text indicator
                if (dedicatedSession && dedicatedSession.textContent) {
                    const text = dedicatedSession.textContent.trim();
                    const textLower = text.toLowerCase();
                    if (text && !textLower.includes('anonymous') && !textLower.includes('login') && !textLower.includes('guest') && !textLower.includes('sign in')) {
                        candidates.add(text);
                    }
                }

                const list = Array.from(candidates);
                return list;
            }""")

            if not discovered or not isinstance(discovered, list):
                return None

            # Filter out empty or whitespace-only candidates
            cleaned = []
            for c in discovered:
                if isinstance(c, str) and c.strip():
                    cleaned.append(c.strip())

            if not cleaned:
                return None

            # Deduplicate case-insensitively
            unique = list({c.lower(): c for c in cleaned}.values())
            if len(unique) > 1:
                # Conflicting evidence: distinct identities present simultaneously!
                return None

            return unique[0]
        except Exception:
            return None


class OperationType:
    CREATE_ENTITY = "CREATE_ENTITY"
    EDIT_ENTITY = "EDIT_ENTITY"
    UPLOAD_ATTACHMENT = "UPLOAD_ATTACHMENT"
    DELETE_ENTITY = "DELETE_ENTITY"
    DELETE_ATTACHMENT = "DELETE_ATTACHMENT"


@dataclass
class TypedRequestContract:
    """Typed request contract defining exact parameters permitted for a write operation."""
    operation_type: str
    allowed_origin: str
    allowed_methods: Tuple[str, ...]
    endpoint_pattern: Optional[str] = None
    resource_id: Optional[str] = None
    run_tag: Optional[str] = None
    account_context: Optional[str] = None
    verified_account: Optional[str] = None
    entity_type: Optional[str] = None
    project_id: Optional[int] = None
    designated_identity_field: Optional[str] = None
    approved_file_path: Optional[str] = None
    approved_file_name: Optional[str] = None
    approved_file_hash: Optional[str] = None
    approved_file_size: Optional[int] = None
    controlled_dispatch_token: Optional[str] = None
    parent_resource_id: Optional[str] = None
    upload_destination: Optional[str] = None
    allowed_parts: Optional[Tuple[str, ...]] = None
    permitted_fields: Optional[Tuple[str, ...]] = None
    graphql_operation_name: Optional[str] = None
    graphql_mutation_field: Optional[str] = None
    graphql_resource_key: Optional[str] = "id"


@dataclass
class OperationPermit:
    """Single-use, time-bounded permission token authorizing a specific state-changing write."""
    permit_id: str
    operation_type: str
    workflow_name: str
    allowed_origin: str
    allowed_methods: Tuple[str, ...]
    endpoint_pattern: Optional[str] = None
    resource_id: Optional[str] = None
    run_tag: Optional[str] = None
    account_context: Optional[str] = None
    verified_account: Optional[str] = None
    entity_type: Optional[str] = None
    project_id: Optional[int] = None
    designated_identity_field: Optional[str] = None
    approved_file_path: Optional[str] = None
    approved_file_name: Optional[str] = None
    approved_file_hash: Optional[str] = None
    approved_file_size: Optional[int] = None
    controlled_dispatch_token: Optional[str] = None
    parent_resource_id: Optional[str] = None
    upload_destination: Optional[str] = None
    allowed_parts: Optional[Tuple[str, ...]] = None
    permitted_fields: Optional[Tuple[str, ...]] = None
    graphql_operation_name: Optional[str] = None
    graphql_mutation_field: Optional[str] = None
    graphql_resource_key: Optional[str] = "id"
    contract: Optional[TypedRequestContract] = None
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0.0
    max_uses: int = 1
    use_count: int = 0
    consumed: bool = False
    recorded_responses: List[Dict[str, Any]] = field(default_factory=list)

    def __post_init__(self):
        if self.approved_file_path and os.path.exists(self.approved_file_path):
            try:
                with open(self.approved_file_path, "rb") as f:
                    f_bytes = f.read()
                if not self.approved_file_hash:
                    self.approved_file_hash = hashlib.sha256(f_bytes).hexdigest()
                if self.approved_file_size is None:
                    self.approved_file_size = len(f_bytes)
            except Exception:
                pass

        if self.contract is None:
            self.contract = TypedRequestContract(
                operation_type=self.operation_type,
                allowed_origin=self.allowed_origin,
                allowed_methods=self.allowed_methods,
                endpoint_pattern=self.endpoint_pattern,
                resource_id=self.resource_id,
                run_tag=self.run_tag,
                account_context=self.account_context,
                verified_account=self.verified_account,
                entity_type=self.entity_type,
                project_id=self.project_id,
                designated_identity_field=self.designated_identity_field,
                approved_file_path=self.approved_file_path,
                approved_file_name=self.approved_file_name,
                approved_file_hash=self.approved_file_hash,
                approved_file_size=self.approved_file_size,
                controlled_dispatch_token=self.controlled_dispatch_token,
                parent_resource_id=self.parent_resource_id,
                upload_destination=self.upload_destination,
                allowed_parts=self.allowed_parts,
                permitted_fields=self.permitted_fields,
                graphql_operation_name=self.graphql_operation_name,
                graphql_mutation_field=self.graphql_mutation_field,
                graphql_resource_key=self.graphql_resource_key,
            )

    def is_active(self) -> bool:
        if self.consumed or self.use_count >= self.max_uses:
            return False
        if self.expires_at > 0 and time.time() > self.expires_at:
            return False
        return True

    def is_valid(self, method: str, origin: str) -> bool:
        if not self.is_active():
            return False
        if origin != self.allowed_origin:
            return False
        if method.upper() not in (m.upper() for m in self.allowed_methods):
            return False
        return True

    def matches_request(
        self,
        method: str,
        url: str,
        post_data: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
        account_identity: Optional[str] = None,
        account_context: Optional[str] = None,
    ) -> Tuple[bool, str]:
        """Strictly validates whether a specific HTTP request matches this permit's typed contract."""
        headers = {k.lower(): v for k, v in (headers or {}).items()}
        if not self.is_active():
            return False, "Permit is consumed, exhausted, or expired"

        # 1. Origin match
        req_origin = get_canonical_origin(url)
        if req_origin != self.allowed_origin:
            return False, f"Origin mismatch: {req_origin} != {self.allowed_origin}"

        # 2. HTTP Method match
        if method.upper() not in (m.upper() for m in self.allowed_methods):
            return False, f"Method {method} not allowed; permit requires {self.allowed_methods}"

        parsed_url = urllib.parse.urlsplit(url)
        path = parsed_url.path

        # 3. Endpoint check (exact match or explicit pattern)
        is_graphql = "graphql" in path.lower() or (headers and "application/graphql" in headers.get("content-type", "").lower())
        if self.endpoint_pattern:
            pattern = self.endpoint_pattern
            matched = False
            if pattern.startswith("^") or pattern.endswith("$") or "\\" in pattern or "[" in pattern or "(?:" in pattern:
                matched = bool(re.search(pattern, path))
            elif "*" in pattern:
                regex_pat = f"^{re.escape(pattern).replace(r'\*', '.*')}$"
                matched = bool(re.search(regex_pat, path))
            else:
                matched = (path == pattern)
            if not matched:
                return False, f"Endpoint {path} does not match allowed pattern '{pattern}'"
        elif not getattr(self, "upload_destination", None) and not is_graphql:
            return False, "Permit lacks mandatory endpoint_pattern constraint; unconstrained write endpoints are prohibited"

        # 4. Mandatory verified account and identity scope check
        target_acct = self.verified_account or self.account_context
        if target_acct:
            req_acct = account_identity or account_context
            if not req_acct:
                return False, f"Missing request account identity: permit requires authenticated account '{target_acct}'"

            if req_acct != target_acct:
                return False, f"Account mismatch: request account '{req_acct}' != permit account '{target_acct}'"

            if headers:
                for hdr_key in ("x-account-context", "x-account-id", "x-user-id"):
                    val = headers.get(hdr_key) or headers.get(hdr_key.lower())
                    if val and val != target_acct:
                        return False, f"Header {hdr_key} '{val}' conflicts with permit account context '{target_acct}'"
        elif headers:
            for hdr_key in ("x-account-context", "x-account-id", "x-user-id"):
                val = headers.get(hdr_key) or headers.get(hdr_key.lower())
                if val:
                    return False, f"Header {hdr_key} '{val}' present on permit without account context"

        # 5. GraphQL deep validation using standard GraphQL AST parser
        is_graphql = "graphql" in path.lower() or (headers and "application/graphql" in headers.get("content-type", "").lower())
        if isinstance(post_data, bytes):
            try:
                post_data_str = post_data.decode("utf-8", "ignore")
            except Exception:
                post_data_str = ""
        else:
            post_data_str = post_data or ""

        if is_graphql or (post_data_str and ('"query"' in post_data_str or '"mutation"' in post_data_str)):
            return self._validate_graphql_payload(post_data_str)

        # 6. REST resource_id validation & conflicting target identifier rejection
        if self.resource_id:
            rid = self.resource_id
            path_segments = [seg for seg in path.split("/") if seg]

            # Detect target resource ID in path segments
            path_target_id = None
            for i, seg in enumerate(path_segments):
                if seg in ("experience", "experiences", "attachment", "attachments", "items") and i + 1 < len(path_segments):
                    candidate = path_segments[i + 1]
                    if candidate not in ("upload", "create", "new", "list"):
                        path_target_id = candidate
                        break
            if path_target_id is None and path_segments:
                last_seg = path_segments[-1]
                if any(last_seg.startswith(p) for p in ("exp-", "att-", "item-")) or (last_seg.isdigit() and len(path_segments) > 2):
                    path_target_id = last_seg

            if path_target_id is not None and path_target_id != rid:
                return False, f"Conflicting target resource ID in path '{path_target_id}' != permit resource ID '{rid}'"

            # Detect conflicting target resource IDs in query parameters
            query_params = urllib.parse.parse_qs(parsed_url.query)
            for q_key in ("id", "resource_id", "experience_id", "attachment_id", "entity_id"):
                for val in query_params.get(q_key, []):
                    if val != rid:
                        return False, f"Conflicting target resource ID in query parameter '{q_key}={val}' != permit resource ID '{rid}'"

            # Detect conflicting target resource IDs in payload body
            in_body_ids = []
            if post_data_str:
                try:
                    body_json = json.loads(post_data_str)
                    if isinstance(body_json, dict):
                        for id_field in ("id", "resource_id", "experience_id", "attachment_id", "entity_id"):
                            if id_field in body_json:
                                val = str(body_json[id_field])
                                in_body_ids.append((id_field, val))
                except Exception:
                    try:
                        form_params = urllib.parse.parse_qs(post_data_str)
                        for id_field in ("id", "resource_id", "experience_id", "attachment_id", "entity_id"):
                            for val in form_params.get(id_field, []):
                                in_body_ids.append((id_field, str(val)))
                    except Exception:
                        pass

            for fld_name, fld_val in in_body_ids:
                if fld_val != rid:
                    return False, f"Conflicting target resource ID in payload field '{fld_name}={fld_val}' != permit resource ID '{rid}'"

            in_path = (path_target_id == rid) or (rid in path_segments)
            in_query = (rid in query_params.get("id", [])) or (rid in query_params.get("resource_id", []))
            in_body = any(v == rid for _, v in in_body_ids)

            if not (in_path or in_query or in_body):
                return False, f"Target resource ID '{rid}' not found in endpoint path, query, or structured payload ID field"

        # 7. Upload binding enforcement
        ctype = ""
        if headers:
            for k, v in headers.items():
                if k.lower() == "content-type":
                    ctype = v.lower()
                    break

        if self.operation_type == OperationType.UPLOAD_ATTACHMENT:
            # Check for prohibited avatar/photo upload paths or fields
            if any(bad in path.lower() for bad in ("/avatar", "/photo", "/profile-pic", "/profile_pic")):
                return False, "Avatar and user photo uploads are strictly prohibited"

            # Destination and parent resource validation
            query_params = urllib.parse.parse_qs(parsed_url.query)
            path_segments = [seg for seg in path.split("/") if seg]
            if any(key not in ("parent_id", "experience_id", "parent_resource_id") for key in query_params):
                return False, "Unapproved upload query field"
            if query_params and not self.parent_resource_id:
                return False, "Upload parent query requires an explicit approved parent"

            if self.upload_destination:
                dest = self.upload_destination.strip()
                if path != dest:
                    return False, f"Upload target path '{path}' does not match approved upload destination '{dest}'"

            if self.parent_resource_id:
                parent_id = self.parent_resource_id
                parent_in_path = (parent_id in path_segments)
                parent_in_query = (parent_id in query_params.get("parent_id", [])) or (parent_id in query_params.get("experience_id", [])) or (parent_id in query_params.get("parent_resource_id", []))
                parent_in_headers = False
                if headers:
                    for hk in ("x-parent-resource-id", "x-experience-id", "x-parent-id"):
                        if headers.get(hk) and headers[hk] != parent_id:
                            return False, "Conflicting parent resource ID in upload headers"
                        if headers.get(hk) == parent_id or headers.get(hk.lower()) == parent_id:
                            parent_in_headers = True

                # Conflicting parent check: if another exp-* or parent_id is specified in path, query, or headers
                for seg in path_segments:
                    if seg.startswith("exp-") and seg != parent_id:
                        return False, f"Conflicting parent resource ID in upload path '{seg}' != permit parent '{parent_id}'"
                for qk in ("parent_id", "experience_id", "parent_resource_id"):
                    for val in query_params.get(qk, []):
                        if val != parent_id:
                            return False, f"Conflicting parent resource ID in query parameter '{qk}={val}' != permit parent '{parent_id}'"

                if not (parent_in_path or parent_in_query or parent_in_headers):
                    return False, f"Approved parent resource ID '{parent_id}' not found in upload path, query, or headers"

            raw_bytes = post_data if isinstance(post_data, bytes) else post_data_str.encode("utf-8", "ignore") if post_data_str else b""

            if not raw_bytes:
                return False, "Opaque uploads are unsupported: actual file content must be inspectable; tokens cannot authorize missing bytes."
            try:
                content_type = headers.get("content-type", "")
                if "\r" in content_type or "\n" in content_type:
                    return False, "Invalid multipart content-type header"
                message = BytesParser(policy=email_policy.default).parsebytes(
                    b"Content-Type: " + content_type.encode("ascii") + b"\r\nMIME-Version: 1.0\r\n\r\n" + raw_bytes
                )
                if message.get_content_type() != "multipart/form-data" or not message.is_multipart():
                    return False, "Upload requires inspectable multipart file parts"
                parts = list(message.iter_parts())
                if any(part.defects for part in message.walk()) or message.preamble or (message.epilogue or "").strip():
                    return False, "Malformed multipart file parts or trailing content"
                files = [p for p in parts if p.get_filename() is not None]
                if len(files) != 1:
                    return False, "Upload contains extra or missing files; only single approved file is permitted"
                for part in parts:
                    if part.is_multipart() or part.get_content_disposition() != "form-data" or part.get("Content-Transfer-Encoding"):
                        return False, "Unsupported multipart part encoding or disposition"
                    name = part.get_param("name", header="content-disposition")
                    if not name or len(part.get_all("Content-Disposition", [])) != 1:
                        return False, "Ambiguous multipart part header"
                    if part is not files[0]:
                        # No unbound routing/account fields may accompany the file.
                        if name not in ("parent_id", "experience_id", "parent_resource_id") or not self.parent_resource_id:
                            return False, "Unapproved extra multipart field"
                        if part.get_payload(decode=True).decode("utf-8") != self.parent_resource_id:
                            return False, "Conflicting parent resource ID in multipart body"
                    elif self.allowed_parts and name not in self.allowed_parts:
                        return False, "Unapproved multipart file field"
                if files[0].get_filename() != self.approved_file_name:
                    return False, "Uploaded filename does not match approved filename"
                file_bytes = files[0].get_payload(decode=True)
                if not file_bytes:
                    # Chromium reports disk-file uploads with an empty part (bytes omitted).
                    return False, "Opaque uploads are unsupported: file part bytes were omitted and cannot be verified."
                if not self.approved_file_size or not self.approved_file_hash:
                    return False, "Upload requires an exact nonzero approved size and digest"
                if hashlib.sha256(file_bytes).hexdigest() != self.approved_file_hash:
                    return False, "Uploaded content does not match approved digest"
                if len(file_bytes) != self.approved_file_size:
                    return False, "Uploaded file size does not match approved size"
            except (ValueError, TypeError, UnicodeError):
                return False, "Malformed multipart file parts"

        # 8. REST run_tag and designated identity field validation (non-upload mutations)
        elif self.run_tag and method.upper() not in ("DELETE", "GET", "HEAD"):
            designated_fields = (
                (self.designated_identity_field,) if self.designated_identity_field
                else (("company", "title") if self.entity_type == "profile_experience"
                      else (("filename", "name") if self.entity_type == "profile_attachment"
                            else ("company", "title", "name", "filename")))
            )

            parsed_payload = None
            is_json = False
            is_form = False

            if post_data_str:
                try:
                    parsed_payload = json.loads(post_data_str)
                    if isinstance(parsed_payload, dict):
                        is_json = True
                except Exception:
                    try:
                        parsed_payload = urllib.parse.parse_qs(post_data_str, strict_parsing=True)
                        if parsed_payload:
                            is_form = True
                    except Exception:
                        parsed_payload = None

            if not (is_json or is_form):
                return False, "Unsupported payload encoding: write requests must be valid JSON or urlencoded form data"

            rtag = self.run_tag
            tag_found_in_designated_field = False

            if is_json and isinstance(parsed_payload, dict):
                for fld in designated_fields:
                    val = parsed_payload.get(fld)
                    if isinstance(val, str) and val.startswith(rtag):
                        tag_found_in_designated_field = True
                        break
            elif is_form and isinstance(parsed_payload, dict):
                for fld in designated_fields:
                    vals = parsed_payload.get(fld, [])
                    if any(isinstance(v, str) and v.startswith(rtag) for v in vals):
                        tag_found_in_designated_field = True
                        break

            if not tag_found_in_designated_field:
                return False, f"Required run tag '{rtag}' missing from designated identity field(s) {designated_fields} (must start with exact generated prefix; description/comments/substrings rejected)"

        # 9. REST permitted fields check
        if self.permitted_fields and post_data_str:
            try:
                body_json = json.loads(post_data_str)
                if isinstance(body_json, dict):
                    unknown = [k for k in body_json.keys() if k not in self.permitted_fields]
                    if unknown:
                        return False, f"Payload contains unpermitted fields: {unknown}"
            except Exception:
                pass

        return True, "Request strictly matches permit contract"

    def _validate_graphql_payload(self, post_data_str: str) -> Tuple[bool, str]:
        """Deep GraphQL payload validation using standard GraphQL AST parser."""
        if not GRAPHQL_AVAILABLE:
            return False, "GraphQL parser library is not available in environment"

        if not post_data_str:
            return False, "GraphQL request body is empty"

        try:
            payload = json.loads(post_data_str)
        except Exception:
            return False, "GraphQL request is not valid JSON"

        if isinstance(payload, list):
            return False, "Batched GraphQL operations are strictly prohibited"

        if not isinstance(payload, dict):
            return False, "GraphQL payload must be a JSON object"

        query_str = payload.get("query", "")
        if not isinstance(query_str, str) or not query_str.strip():
            return False, "GraphQL query string is missing or empty"

        op_name = payload.get("operationName")
        variables = payload.get("variables", {})
        if not isinstance(variables, dict):
            variables = {}

        # 1. Parse document into GraphQL AST
        try:
            doc = gql_parse(query_str)
        except Exception as e:
            return False, f"Malformed GraphQL document syntax: {e}"

        # 2. Extract operations and fragments
        operations = [d for d in doc.definitions if isinstance(d, OperationDefinitionNode)]
        fragments = {d.name.value: d for d in doc.definitions if isinstance(d, FragmentDefinitionNode)}

        # Reject multiple operations unless unambiguous
        if len(operations) > 1:
            if not self.graphql_operation_name and not op_name:
                return False, f"Document contains multiple operations ({len(operations)}) without specified operationName"
            target_op_name = self.graphql_operation_name or op_name
            matching_ops = [op for op in operations if op.name and op.name.value == target_op_name]
            if len(matching_ops) != 1:
                return False, f"Document contains multiple operations and exactly one matching '{target_op_name}' was not found"
            op_def = matching_ops[0]
        elif len(operations) == 1:
            op_def = operations[0]
            if self.graphql_operation_name and op_def.name and op_def.name.value != self.graphql_operation_name:
                return False, f"GraphQL operation name '{op_def.name.value}' != expected '{self.graphql_operation_name}'"
        else:
            return False, "No executable GraphQL operations found in document"

        # 3. Enforce operation is a MUTATION
        if op_def.operation.value != "mutation":
            return False, f"GraphQL operation is '{op_def.operation.value}', but permit requires 'mutation'"

        # 4. Resolve top-level selections (resolving fragment spreads if present)
        def resolve_selections(selections):
            resolved = []
            for sel in selections:
                if isinstance(sel, FieldNode):
                    resolved.append(sel)
                elif isinstance(sel, FragmentSpreadNode):
                    fname = sel.name.value
                    if fname in fragments:
                        sub, err = resolve_selections(fragments[fname].selection_set.selections)
                        if err:
                            return None, err
                        resolved.extend(sub)
                    else:
                        return None, f"Unknown fragment spread '{fname}'"
                elif isinstance(sel, InlineFragmentNode):
                    sub, err = resolve_selections(sel.selection_set.selections)
                    if err:
                        return None, err
                    resolved.extend(sub)
                else:
                    return None, f"Unsupported selection node type '{type(sel).__name__}'"
            return resolved, None

        root_fields, err = resolve_selections(op_def.selection_set.selections)
        if err:
            return False, err

        # Exactly one root mutation field allowed
        if len(root_fields) != 1:
            return False, f"GraphQL mutation must contain only a single mutation field; found {len(root_fields)}"

        root_field = root_fields[0]
        actual_mutation_name = root_field.name.value

        if self.graphql_mutation_field and actual_mutation_name != self.graphql_mutation_field:
            return False, f"GraphQL root mutation field '{actual_mutation_name}' != expected '{self.graphql_mutation_field}'"

        # Check prohibited operations
        for bad_op in (
            "deleteaccount", "deleteuser", "updatepassword", "changepassword", "resetpassword",
            "revokekey", "revokekeys", "billing", "terminateaccount",
        ):
            if bad_op in actual_mutation_name.lower():
                return False, f"Prohibited operation '{bad_op}' detected in GraphQL mutation"

        # Helper to resolve argument values from AST nodes and variable dictionary
        def resolve_arg_value(val_node):
            if isinstance(val_node, (StringValueNode, IntValueNode, FloatValueNode, BooleanValueNode, EnumValueNode)):
                return str(val_node.value)
            elif isinstance(val_node, VariableNode):
                vname = val_node.name.value
                return str(variables.get(vname, "")) if vname in variables else None
            elif isinstance(val_node, ObjectValueNode):
                res_dict = {}
                for f in val_node.fields:
                    res_dict[f.name.value] = resolve_arg_value(f.value)
                return res_dict
            elif isinstance(val_node, ListValueNode):
                return [resolve_arg_value(v) for v in val_node.values]
            return None

        # 5. Validate resource_id strictly bound to the mutation's actual executed arguments
        if self.resource_id:
            rid = self.resource_id
            key = self.graphql_resource_key or "id"
            target_val = None

            # Direct arguments on the root field (directives are separate and ignored)
            for arg in root_field.arguments:
                if arg.name.value == key:
                    target_val = resolve_arg_value(arg.value)
                    break
                elif arg.name.value == "input":
                    if isinstance(arg.value, VariableNode):
                        input_var = variables.get(arg.value.name.value)
                        if isinstance(input_var, dict):
                            target_val = str(input_var.get(key, "")) if key in input_var else None
                    elif isinstance(arg.value, ObjectValueNode):
                        for f in arg.value.fields:
                            if f.name.value == key:
                                target_val = resolve_arg_value(f.value)
                                break

            if target_val is None:
                return False, f"Target argument '{key}' not found on mutation field '{actual_mutation_name}'"

            if str(target_val) != rid:
                return False, f"Mutation argument '{key}' resolved to '{target_val}', does not match target resource ID '{rid}'"

        # 6. Validate run_tag strictly bound to the mutation's actual executed arguments
        if self.run_tag:
            rtag = self.run_tag
            tag_found = False
            designated_arg_keys = ("company", "title", "name", "filename")

            for arg in root_field.arguments:
                val = resolve_arg_value(arg.value)
                if arg.name.value in designated_arg_keys and isinstance(val, str) and val.startswith(rtag):
                    tag_found = True
                    break
                elif arg.name.value == "input" and isinstance(val, dict):
                    for sub_k, sub_v in val.items():
                        if sub_k in designated_arg_keys and isinstance(sub_v, str) and sub_v.startswith(rtag):
                            tag_found = True
                            break

            if not tag_found:
                return False, f"Required run tag '{rtag}' not found in designated mutation argument ({designated_arg_keys})"

        return True, "GraphQL mutation strictly matches permit contract"



@dataclass
class ScopedOwnershipRecord:
    """Persistent journal entry tracking test entity lifecycle and confirmed remote identity."""
    id: Optional[int]
    run_id: int
    project_id: int
    target_url: str
    entity_type: str  # "profile_experience", "profile_attachment"
    entity_id: str
    entity_name: str
    status: str  # "INTENT_RECORDED", "CONFIRMED_CREATED", "CONFIRMED_EDITED", "CONFIRMED_DELETED", "CLEANUP_FAILED", "AMBIGUOUS_UNCONFIRMED"
    details_json: str
    account_context: Optional[str] = None
    intent_id: Optional[str] = None
    run_prefix: Optional[str] = None
    remote_entity_id: Optional[str] = None
    remote_status: Optional[str] = None
    local_cleaned: bool = False
    remote_cleaned: bool = False
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class ScopedEntityOwnershipRegistry:
    """Durable SQLite journal tracking ownership and confirmed remote identities."""

    @staticmethod
    def generate_run_prefix(run_id: int) -> str:
        """Generate uniquely identifiable prefix for run-owned test records."""
        uid = uuid.uuid4().hex[:6].upper()
        return f"[QA-TEST-{run_id}-{uid}]"

    @classmethod
    def record_creation_intent(
        cls,
        run_id: int,
        project_id: int,
        target_url: str,
        entity_type: str,
        run_prefix: str,
        proposed_name: str,
        account_context: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> ScopedOwnershipRecord:
        """Record intent before an operation to ensure recovery state if interrupted."""
        intent_id = uuid.uuid4().hex[:8]
        details_dict = details or {}
        details_dict["intent_id"] = intent_id
        details_dict["run_prefix"] = run_prefix
        details_str = json.dumps(details_dict)

        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO mutation_ownership_records
                (run_id, project_id, target_url, account_context, entity_type, intent_id, run_prefix,
                 entity_id, remote_entity_id, entity_name, status, remote_status, local_cleaned, remote_cleaned,
                 details_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, 'INTENT_RECORDED', 'PENDING_CONFIRMATION', 0, 0, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (run_id, project_id, target_url, account_context, entity_type, intent_id, run_prefix,
                 intent_id, proposed_name, details_str),
            )
            rec_id = cursor.lastrowid
            conn.commit()
            return ScopedOwnershipRecord(
                id=rec_id,
                run_id=run_id,
                project_id=project_id,
                target_url=target_url,
                account_context=account_context,
                entity_type=entity_type,
                intent_id=intent_id,
                run_prefix=run_prefix,
                entity_id=intent_id,
                remote_entity_id=None,
                entity_name=proposed_name,
                status="INTENT_RECORDED",
                remote_status="PENDING_CONFIRMATION",
                local_cleaned=False,
                remote_cleaned=False,
                details_json=details_str,
            )
        finally:
            conn.close()

    @classmethod
    def confirm_creation(
        cls,
        record_id: int,
        remote_entity_id: str,
        confirmed_name: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Confirm server/DOM creation with confirmed remote identity."""
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            conn.execute("BEGIN IMMEDIATE")
            existing = cursor.execute("SELECT details_json FROM mutation_ownership_records WHERE id = ?", (record_id,)).fetchone()
            curr_details = json.loads(existing["details_json"] if existing and existing["details_json"] else "{}")
            if details:
                curr_details.update(details)
            curr_details["confirmed_at"] = time.time()
            cursor.execute(
                """
                UPDATE mutation_ownership_records
                SET status = 'CREATED',
                    remote_status = 'CONFIRMED_EXISTS',
                    entity_id = ?,
                    remote_entity_id = ?,
                    entity_name = ?,
                    details_json = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (remote_entity_id, remote_entity_id, confirmed_name, json.dumps(curr_details), record_id),
            )
            conn.commit()
        finally:
            conn.close()

    @classmethod
    def mark_ambiguous_creation(
        cls,
        record_id: int,
        reason: str,
        remote_entity_id: Optional[str] = None,
    ) -> None:
        """Record ambiguity when creation outcome or remote identity cannot be verified."""
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            existing = cursor.execute("SELECT details_json, remote_entity_id FROM mutation_ownership_records WHERE id = ?", (record_id,)).fetchone()
            curr_details = json.loads(existing["details_json"] if existing and existing["details_json"] else "{}")
            curr_details["ambiguity_reason"] = reason
            eff_remote_id = remote_entity_id or (existing["remote_entity_id"] if existing else None)
            cursor.execute(
                """
                UPDATE mutation_ownership_records
                SET status = 'AMBIGUOUS_UNCONFIRMED',
                    remote_status = 'AMBIGUOUS',
                    remote_entity_id = ?,
                    entity_id = COALESCE(?, entity_id),
                    details_json = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (eff_remote_id, eff_remote_id, json.dumps(curr_details), record_id),
            )
            conn.commit()
        finally:
            conn.close()

    @classmethod
    def confirm_edit(cls, record_id: int, updated_name: str, details: Optional[Dict[str, Any]] = None) -> None:
        """Update entity ownership record upon confirmed modification."""
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            existing = cursor.execute("SELECT details_json FROM mutation_ownership_records WHERE id = ?", (record_id,)).fetchone()
            curr_details = json.loads(existing["details_json"] if existing and existing["details_json"] else "{}")
            if details:
                curr_details.update(details)
            curr_details["edited_at"] = time.time()
            cursor.execute(
                """
                UPDATE mutation_ownership_records
                SET status = 'EDITED',
                    remote_status = 'CONFIRMED_EXISTS',
                    entity_name = ?,
                    details_json = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (updated_name, json.dumps(curr_details), record_id),
            )
            conn.commit()
        finally:
            conn.close()

    @classmethod
    def mark_not_dispatched(cls, record_id: int) -> None:
        """Close an unused upload intent without claiming remote creation/deletion."""
        conn = get_db_connection()
        try:
            conn.execute("""UPDATE mutation_ownership_records
                SET status='NOT_DISPATCHED', remote_status='NOT_CREATED', updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND status='INTENT_RECORDED' AND remote_entity_id IS NULL""", (record_id,))
            conn.commit()
        finally:
            conn.close()

    @classmethod
    def confirm_remote_deletion(cls, record_id: int) -> None:
        """Confirm verified remote deletion and update status."""
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE mutation_ownership_records
                SET status = 'DELETED',
                    remote_status = 'CONFIRMED_DELETED',
                    remote_cleaned = 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (record_id,),
            )
            conn.commit()
        finally:
            conn.close()

    @classmethod
    def mark_cleanup_failed(cls, record_id: int, error_details: str) -> None:
        """Record cleanup failure with persistent recovery details."""
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            existing = cursor.execute("SELECT details_json FROM mutation_ownership_records WHERE id = ?", (record_id,)).fetchone()
            curr_details = json.loads(existing["details_json"] if existing and existing["details_json"] else "{}")
            curr_details["cleanup_error"] = error_details
            cursor.execute(
                """
                UPDATE mutation_ownership_records
                SET status = 'CLEANUP_FAILED',
                    remote_status = 'CLEANUP_FAILED',
                    details_json = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (json.dumps(curr_details), record_id),
            )
            conn.commit()
        finally:
            conn.close()

    @classmethod
    def mark_local_cleaned(cls, record_id: int) -> None:
        """Mark local temporary file as deleted."""
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE mutation_ownership_records
                SET local_cleaned = 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (record_id,),
            )
            conn.commit()
        finally:
            conn.close()

    # Legacy-compatible register_entity wrapper
    @classmethod
    def register_entity(
        cls,
        run_id: int,
        project_id: int,
        target_url: str,
        entity_type: str,
        entity_id: str,
        entity_name: str,
        details: Optional[Dict[str, Any]] = None,
        account_context: Optional[str] = None,
    ) -> ScopedOwnershipRecord:
        """Register entity with confirmed remote identity (legacy-compatible)."""
        prefix = cls.generate_run_prefix(run_id) if not entity_name.startswith("[QA-TEST-") else entity_name.split()[0]
        rec = cls.record_creation_intent(
            run_id=run_id,
            project_id=project_id,
            target_url=target_url,
            entity_type=entity_type,
            run_prefix=prefix,
            proposed_name=entity_name,
            account_context=account_context,
            details=details,
        )
        cls.confirm_creation(rec.id, remote_entity_id=entity_id, confirmed_name=entity_name, details=details)
        rec.entity_id = entity_id
        rec.remote_entity_id = entity_id
        rec.status = "CREATED"
        return rec

    @classmethod
    def update_status(cls, record_id: int, status: str) -> None:
        """Update entity status (legacy-compatible)."""
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            rem_cleaned = 1 if status == "DELETED" else 0
            cursor.execute(
                """
                UPDATE mutation_ownership_records
                SET status = ?,
                    remote_cleaned = CASE WHEN ? = 'DELETED' THEN 1 ELSE remote_cleaned END,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (status, status, record_id),
            )
            conn.commit()
        finally:
            conn.close()

    @classmethod
    def mark_record_resolved_manual(
        cls,
        record_id: int,
        note: str = "Manually verified / cleaned by tester",
        project_id: Optional[int] = None,
        *,
        archive_project_key: Optional[str] = None
    ) -> bool:
        """Mark a pending record as manually resolved/cleaned by the tester.
        
        Strict safety:
        - Never sets remote_cleaned = 1 (distinguishes manual tester resolution from automated server deletion proof).
        - Sets status = 'RESOLVED_MANUAL' and remote_status = 'MANUALLY_RESOLVED'.
        - Sets server_deletion_confirmed = False in details_json.
        - Idempotent and immutable: refuses to overwrite already deleted or resolved records.
        - Supports keyword-only archive_project_key for historical decoupled records.
        """
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            conn.execute("BEGIN IMMEDIATE")
            existing = cursor.execute(
                "SELECT id, project_id, run_id, status, remote_status, remote_cleaned, details_json FROM mutation_ownership_records WHERE id = ?",
                (record_id,),
            ).fetchone()
            if not existing:
                return False

            raw_details = existing["details_json"] if existing["details_json"] else "{}"
            try:
                curr_details = json.loads(raw_details)
                if not isinstance(curr_details, dict):
                    return False
            except Exception:
                return False

            if archive_project_key is not None:
                # Historical route: active project_id cannot be used as authorization simultaneously
                if project_id is not None:
                    return False
                # Must be a historical record
                if existing["project_id"] > 0 and existing["run_id"] > 0:
                    return False
                # Determine expected archive_project_key
                hd = curr_details.get("historical_detachment")
                if isinstance(hd, dict) and hd.get("archive_project_key"):
                    expected_key = hd["archive_project_key"]
                else:
                    expected_key = f"legacy-record:{existing['id']}"
                if archive_project_key != expected_key:
                    return False
            else:
                # Active route: requires positive project_id matching record
                if project_id is None or type(project_id) is not int or project_id <= 0:
                    return False
                if existing["project_id"] != project_id or existing["project_id"] <= 0 or existing["run_id"] <= 0:
                    return False

            if existing["remote_cleaned"] or existing["status"] in ("NOT_CREATED", "NOT_DISPATCHED"):
                return False
            if existing["status"] in ("DELETED", "RESOLVED_MANUAL") or existing["remote_status"] in ("CONFIRMED_DELETED", "MANUALLY_RESOLVED"):
                return False

            curr_details["previous_status"] = existing["status"]
            curr_details["previous_remote_status"] = existing["remote_status"]
            curr_details["resolved_manual"] = True
            curr_details["resolved_manual_note"] = note
            curr_details["resolved_manual_at"] = datetime.now(timezone.utc).isoformat()
            curr_details["server_deletion_confirmed"] = False

            cursor.execute(
                """
                UPDATE mutation_ownership_records
                SET status = 'RESOLVED_MANUAL',
                    remote_status = 'MANUALLY_RESOLVED',
                    remote_cleaned = 0,
                    details_json = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND status NOT IN ('DELETED', 'RESOLVED_MANUAL') AND (remote_status IS NULL OR remote_status NOT IN ('CONFIRMED_DELETED', 'MANUALLY_RESOLVED'))
                """,
                (json.dumps(curr_details), record_id),
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()

    @classmethod
    def get_pending_records_for_display(
        cls,
        project_id: Optional[int] = None,
        run_id: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Fetch human-readable pending records needing tester attention."""
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            query = """
                SELECT id, run_id, project_id, target_url, account_context, entity_type, intent_id, run_prefix,
                       entity_id, remote_entity_id, entity_name, status, remote_status, local_cleaned, remote_cleaned,
                       details_json, created_at, updated_at
                FROM mutation_ownership_records
                WHERE status NOT IN ('DELETED', 'RESOLVED_MANUAL', 'NOT_DISPATCHED', 'NOT_CREATED')
                  AND (remote_status IS NULL OR remote_status NOT IN ('CONFIRMED_DELETED', 'MANUALLY_RESOLVED'))
                  AND remote_cleaned = 0
                  AND project_id > 0
                  AND run_id > 0
            """
            params: List[Any] = []
            if project_id is not None:
                query += " AND project_id = ?"
                params.append(project_id)
            if run_id is not None:
                query += " AND run_id = ?"
                params.append(run_id)
            query += " ORDER BY id DESC"
            rows = cursor.execute(query, tuple(params)).fetchall()
            results = []
            for row in rows:
                item = dict(row)
                if item.get("details_json"):
                    try:
                        item["details"] = json.loads(item["details_json"])
                    except Exception:
                        item["details"] = {}
                else:
                    item["details"] = {}
                results.append(item)
            return results
        finally:
            conn.close()

    @classmethod
    def get_records_for_run(cls, run_id: int) -> List[ScopedOwnershipRecord]:
        """Fetch all ownership records for a specific run from SQLite."""
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            rows = cursor.execute(
                """
                SELECT id, run_id, project_id, target_url, account_context, entity_type, intent_id, run_prefix,
                       entity_id, remote_entity_id, entity_name, status, remote_status, local_cleaned, remote_cleaned,
                       details_json, created_at, updated_at
                FROM mutation_ownership_records
                WHERE run_id = ?
                ORDER BY id ASC
                """,
                (run_id,),
            ).fetchall()
            return [
                ScopedOwnershipRecord(
                    id=row["id"],
                    run_id=row["run_id"],
                    project_id=row["project_id"],
                    target_url=row["target_url"],
                    account_context=row["account_context"],
                    entity_type=row["entity_type"],
                    intent_id=row["intent_id"],
                    run_prefix=row["run_prefix"],
                    entity_id=row["entity_id"],
                    remote_entity_id=row["remote_entity_id"],
                    entity_name=row["entity_name"],
                    status=row["status"],
                    remote_status=row["remote_status"],
                    local_cleaned=bool(row["local_cleaned"]),
                    remote_cleaned=bool(row["remote_cleaned"]),
                    details_json=row["details_json"],
                    created_at=str(row["created_at"]),
                    updated_at=str(row["updated_at"]),
                )
                for row in rows
            ]
        finally:
            conn.close()

    @classmethod
    def get_pending_or_uncleaned_records(
        cls,
        project_id: Optional[int] = None,
        account_context: Optional[str] = None,
    ) -> List[ScopedOwnershipRecord]:
        """Fetch pending, uncleaned, or ambiguous records needing recovery reconciliation."""
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            query = """
                SELECT id, run_id, project_id, target_url, account_context, entity_type, intent_id, run_prefix,
                       entity_id, remote_entity_id, entity_name, status, remote_status, local_cleaned, remote_cleaned,
                       details_json, created_at, updated_at
                FROM mutation_ownership_records
                WHERE (remote_cleaned = 0 OR status IN ('AMBIGUOUS_UNCONFIRMED', 'CLEANUP_FAILED', 'CREATED', 'EDITED')
                       OR remote_status IN ('PENDING_CONFIRMATION', 'AMBIGUOUS', 'CONFIRMED_EXISTS'))
                  AND status NOT IN ('NOT_DISPATCHED', 'RESOLVED_MANUAL', 'DELETED')
                  AND (remote_status IS NULL OR remote_status NOT IN ('CONFIRMED_DELETED', 'MANUALLY_RESOLVED'))
                  AND project_id > 0
                  AND run_id > 0
            """
            params: List[Any] = []
            if project_id is not None:
                query += " AND project_id = ?"
                params.append(project_id)
            if account_context:
                query += " AND account_context = ?"
                params.append(account_context)
            query += " ORDER BY id ASC"
            rows = cursor.execute(query, tuple(params)).fetchall()
            return [
                ScopedOwnershipRecord(
                    id=row["id"],
                    run_id=row["run_id"],
                    project_id=row["project_id"],
                    target_url=row["target_url"],
                    account_context=row["account_context"],
                    entity_type=row["entity_type"],
                    intent_id=row["intent_id"],
                    run_prefix=row["run_prefix"],
                    entity_id=row["entity_id"],
                    remote_entity_id=row["remote_entity_id"],
                    entity_name=row["entity_name"],
                    status=row["status"],
                    remote_status=row["remote_status"],
                    local_cleaned=bool(row["local_cleaned"]),
                    remote_cleaned=bool(row["remote_cleaned"]),
                    details_json=row["details_json"],
                    created_at=str(row["created_at"]),
                    updated_at=str(row["updated_at"]),
                )
                for row in rows
            ]
        finally:
            conn.close()

    @classmethod
    def get_historical_records(
        cls,
        state: str = "pending",
        limit: int = 100,
        before_id: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Fetch detached/historical records with project_id <= 0 OR run_id <= 0.
        
        state: 'pending' (unresolved only) or 'all'
        limit: bounded between 1 and 500
        before_id: optional positive integer for cursor pagination
        """
        if state not in ("pending", "all"):
            raise ValueError(f"Invalid state: {state}")
        limit = max(1, min(limit, 500))

        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            query = """
                SELECT id, run_id, project_id, target_url, account_context, entity_type, intent_id, run_prefix,
                       entity_id, remote_entity_id, entity_name, status, remote_status, local_cleaned, remote_cleaned,
                       details_json, created_at, updated_at
                FROM mutation_ownership_records
                WHERE (project_id <= 0 OR run_id <= 0)
            """
            params: List[Any] = []
            if state == "pending":
                query += """
                  AND status NOT IN ('DELETED', 'RESOLVED_MANUAL', 'NOT_DISPATCHED', 'NOT_CREATED')
                  AND (remote_status IS NULL OR remote_status NOT IN ('CONFIRMED_DELETED', 'MANUALLY_RESOLVED'))
                  AND remote_cleaned = 0
                """
            if before_id is not None and type(before_id) is int and before_id > 0:
                query += " AND id < ?"
                params.append(before_id)
            query += f" ORDER BY id DESC LIMIT {limit}"

            rows = cursor.execute(query, tuple(params)).fetchall()
            results = []
            for row in rows:
                item = dict(row)
                item["record_id"] = item["id"]
                raw_details = item.get("details_json") or "{}"
                try:
                    d = json.loads(raw_details)
                    if not isinstance(d, dict):
                        d = {}
                        item["metadata_corrupt"] = True
                    else:
                        item["metadata_corrupt"] = False
                except Exception:
                    d = {}
                    item["metadata_corrupt"] = True

                item["details"] = d
                hd = d.get("historical_detachment")
                if isinstance(hd, dict) and hd.get("archive_project_key"):
                    item["archive_project_key"] = hd["archive_project_key"]
                    item["archive_batch_key"] = hd.get("archive_batch_key")
                    item["original_project_id"] = hd.get("original_project_id")
                    item["original_project_name"] = hd.get("original_project_name") or "Unknown"
                    item["original_run_id"] = hd.get("original_run_id") or item["run_id"]
                    item["is_legacy"] = False
                else:
                    item["archive_project_key"] = f"legacy-record:{item['id']}"
                    item["archive_batch_key"] = None
                    item["original_project_id"] = None
                    item["original_project_name"] = "Unknown (Legacy)"
                    item["original_run_id"] = item["run_id"]
                    item["is_legacy"] = True

                results.append(item)
            return results
        finally:
            conn.close()

    @classmethod
    def is_owned_by_run(
        cls,
        run_id: int,
        entity_id: Optional[str] = None,
        entity_name: Optional[str] = None,
        project_id: Optional[int] = None,
        target_url: Optional[str] = None,
        account_context: Optional[str] = None,
        remote_entity_id: Optional[str] = None,
        entity_type: Optional[str] = None,
        require_destructive_authority: bool = False,
    ) -> bool:
        """Strict verification: check if entity is genuinely owned by this specific run.
        
        Strict Invariants:
        1. When require_destructive_authority is True:
           - Name matching ALONE is strictly insufficient to authorize destructive actions.
           - Confirmed remote identity (remote_entity_id or entity_id) is MANDATORY.
           - Matching project_id and canonical target_origin are MANDATORY.
           - account_context is verified against the record.
           - Lifecycle state must be actively confirmed (CREATED, EDITED, CONFIRMED_CREATED, CONFIRMED_EDITED).
             Entities in INTENT_RECORDED, AMBIGUOUS, DELETED, or CLEANUP_FAILED cannot be deleted again.
        2. Distinguishes local SQLite autoincrement ID from remote entity ID via provenance:
           A remote server ID of "1" is valid if and only if r.remote_entity_id == "1".
           We NEVER check r.id against target_eid, and NEVER reject legitimate remote IDs that
           coincidentally equal local integer row IDs.
        3. NEVER permits substring matching (bidirectional 'in' is strictly forbidden).
        4. If entity_name is provided, it MUST contain the unique run tag [QA-TEST-<run_id>- and match exactly.
        5. Fail closed: returns False on any ambiguity, missing identity, or missing context.
        """
        target_eid = remote_entity_id or entity_id

        # Invariant 1: Destructive authority requires confirmed remote identity and mandatory context
        if require_destructive_authority:
            if type(run_id) is not int or run_id <= 0:
                return False
            if project_id is None or type(project_id) is not int or project_id <= 0:
                return False
            if not target_eid:
                # Name alone can NEVER authorize destructive actions
                return False
            if project_id is None:
                return False
            if not target_url:
                return False

        if not target_eid and not entity_name:
            return False

        records = cls.get_records_for_run(run_id)
        if not records:
            return False

        target_origin = get_canonical_origin(target_url) if target_url else None

        matched_records = []
        for r in records:
            # Filter by project_id boundary
            if project_id is not None and r.project_id != project_id:
                continue
            # Filter by target origin boundary
            if target_origin and get_canonical_origin(r.target_url) != target_origin:
                continue
            # Filter by account context boundary
            if account_context:
                if r.account_context and r.account_context != account_context:
                    continue
            elif require_destructive_authority and r.account_context:
                # Context required if record has account context
                continue

            # Filter by entity_type if specified
            if entity_type and r.entity_type != entity_type:
                continue

            # Check lifecycle state
            if require_destructive_authority:
                # Must be an active confirmed entity that has NOT already been deleted or failed
                if r.status not in ("CONFIRMED_CREATED", "CONFIRMED_EDITED", "CREATED", "EDITED"):
                    continue
                if r.remote_status not in ("CONFIRMED_EXISTS", None):
                    continue
            else:
                if r.status not in ("CONFIRMED_CREATED", "CONFIRMED_EDITED", "CREATED", "EDITED", "DELETED", "CONFIRMED_DELETED"):
                    continue

            id_matches = False
            name_matches = False

            if target_eid:
                # Provenance verification: check confirmed remote identity recorded from server
                if r.remote_entity_id and r.remote_entity_id == target_eid:
                    id_matches = True
                elif r.entity_id and r.entity_id == target_eid and r.remote_status == "CONFIRMED_EXISTS":
                    id_matches = True

            if entity_name:
                # Require unique run prefix in name to prevent matching pre-existing records
                run_tag_fragment = f"[QA-TEST-{run_id}-"
                if run_tag_fragment in entity_name:
                    # Exact string match only - NEVER substring
                    if r.entity_name == entity_name:
                        name_matches = True
                    elif r.run_prefix and entity_name.startswith(r.run_prefix) and entity_name == r.entity_name:
                        name_matches = True

            if require_destructive_authority:
                # Both confirmed ID AND name tag match are required if name is provided,
                # but ID match is strictly required regardless!
                if id_matches:
                    if entity_name:
                        if name_matches:
                            matched_records.append(r)
                    else:
                        matched_records.append(r)
            else:
                if target_eid and entity_name:
                    if id_matches and name_matches:
                        matched_records.append(r)
                elif target_eid and id_matches:
                    matched_records.append(r)
                elif entity_name and name_matches:
                    matched_records.append(r)

        # Ambiguity check: must resolve to exactly one confirmed record
        return len(matched_records) == 1

    @staticmethod
    def generate_harmless_file(
        scratch_dir: str, run_id: int, file_type: str = "txt"
    ) -> Tuple[str, str]:
        """Create an identifiable harmless test file for upload testing.
        
        Returns (filepath, filename).
        """
        os.makedirs(scratch_dir, exist_ok=True)
        uid = uuid.uuid4().hex[:6].upper()
        if file_type == "png":
            filename = f"qa_test_upload_run{run_id}_{uid}.png"
            filepath = os.path.join(scratch_dir, filename)
            # 10x10 green PNG image binary
            png_bytes = (
                b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\n\x00\x00\x00\n\x08\x02\x00\x00\x00\x02"
                b"01a\x00\x00\x00\x1bIDATx\x9cc\xfc\xcf\x80\x01\x18\x18\x18\x80\xa2\x02\r3\x18\x00\x00"
                b"\x82\x00\x01\x19\xce\xa80\x00\x00\x00\x00IEND\xaeB`\x82"
            )
            with open(filepath, "wb") as f:
                f.write(png_bytes)
            return filepath, filename
        else:
            filename = f"qa_test_upload_run{run_id}_{uid}.txt"
            filepath = os.path.join(scratch_dir, filename)
            content = f"QA-Tester harmless verification upload [QA-TEST-{run_id}-{uid}]\nGenerated for authorized profile upload testing.\n"
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(content)
            return filepath, filename


# Alias for concise import compatibility
ScopedMutationRegistry = ScopedEntityOwnershipRegistry


class ScopedMutationNetworkBarrier:
    """Network boundary barrier enforcing operation-scoped write permissions.
    
    Invariants:
    1. Safe reads (GET, HEAD, OPTIONS) to approved target origin are permitted.
    2. All state-changing requests (POST, PUT, PATCH, DELETE) are DENIED BY DEFAULT.
    3. State changes require an active, unexpired, operation-scoped OperationPermit.
    4. Prohibited operations (account destruction, password change, billing, messaging)
       are blocked unconditionally, regardless of method or origin.
    5. External cross-origin requests are blocked.
    6. GraphQL mutations are inspected: operation name and payload must match active permit.
    """

    DANGEROUS_ENDPOINTS = (
        "delete_account",
        "delete-account",
        "account/delete",
        "account_delete",
        "remove_account",
        "remove-account",
        "account/remove",
        "purge_account",
        "destroy_account",
        "account/destroy",
        "user/delete",
        "profile/delete",
        "logout",
        "signout",
        "sign-out",
        "log-out",
        "change_password",
        "change-password",
        "reset_password",
        "reset-password",
        "revoke_keys",
        "revoke-keys",
    )

    PROHIBITED_PAYLOAD_OPERATIONS = (
        "deleteaccount",
        "deleteuser",
        "updatepassword",
        "changepassword",
        "resetpassword",
        "revokekey",
        "revokekeys",
        "inviteuser",
        "sendmessage",
        "charge",
        "billing",
        "transfer",
        "terminateaccount",
    )

    def __init__(self, target_origin: str, account_context: Optional[str] = None,
                 is_cancelled: Optional[Callable[[], bool]] = None) -> None:
        self.target_origin = get_canonical_origin(target_origin)
        self.is_cancelled = is_cancelled or (lambda: False)
        self.account_context = account_context
        self.verified_session_identity: Optional[str] = None
        self.blocked_requests: List[Dict[str, str]] = []
        self.active_permit: Optional[OperationPermit] = None
        self.response_history: List[Dict[str, Any]] = []
        self._lock = asyncio.Lock()

    def set_verified_session_identity(self, account: Optional[str]) -> None:
        """Install verified session identity from authentic DOM/window credentials.
        
        Invalidates any active permits if account changes or session identity is lost.
        """
        if self.verified_session_identity != account:
            if self.active_permit:
                self.active_permit.consumed = True
                self.active_permit = None
            self.verified_session_identity = account

    def acquire_permit(
        self,
        operation_type: str,
        workflow_name: str,
        allowed_methods: Tuple[str, ...],
        endpoint_pattern: Optional[str] = None,
        resource_id: Optional[str] = None,
        run_tag: Optional[str] = None,
        account_context: Optional[str] = None,
        verified_account: Optional[str] = None,
        entity_type: Optional[str] = None,
        project_id: Optional[int] = None,
        designated_identity_field: Optional[str] = None,
        approved_file_path: Optional[str] = None,
        approved_file_name: Optional[str] = None,
        approved_file_hash: Optional[str] = None,
        approved_file_size: Optional[int] = None,
        controlled_dispatch_token: Optional[str] = None,
        parent_resource_id: Optional[str] = None,
        upload_destination: Optional[str] = None,
        allowed_parts: Optional[Tuple[str, ...]] = None,
        permitted_fields: Optional[Tuple[str, ...]] = None,
        graphql_operation_name: Optional[str] = None,
        graphql_mutation_field: Optional[str] = None,
        graphql_resource_key: Optional[str] = "id",
        timeout_seconds: float = 10.0,
        max_uses: int = 1,
    ) -> OperationPermit:
        """Issue a short-lived, single-use permit for a verified operation."""
        eff_acct = account_context or self.account_context
        eff_verified = verified_account or self.verified_session_identity
        if not endpoint_pattern and workflow_name:
            if "experience" in workflow_name.lower():
                endpoint_pattern = r"^/api/(?:profile/)?experience(?:s)?(?:/.*)?$"
            elif "attachment" in workflow_name.lower():
                endpoint_pattern = r"^/api/(?:profile/)?attachment(?:s)?(?:/.*)?$"

        permit = OperationPermit(
            permit_id=uuid.uuid4().hex[:8],
            operation_type=operation_type,
            workflow_name=workflow_name,
            allowed_origin=self.target_origin,
            allowed_methods=allowed_methods,
            endpoint_pattern=endpoint_pattern,
            resource_id=resource_id,
            run_tag=run_tag,
            account_context=eff_acct,
            verified_account=eff_verified,
            entity_type=entity_type,
            project_id=project_id,
            designated_identity_field=designated_identity_field,
            approved_file_path=approved_file_path,
            approved_file_name=approved_file_name,
            approved_file_hash=approved_file_hash,
            approved_file_size=approved_file_size,
            controlled_dispatch_token=controlled_dispatch_token,
            parent_resource_id=parent_resource_id,
            upload_destination=upload_destination,
            allowed_parts=allowed_parts,
            permitted_fields=permitted_fields,
            graphql_operation_name=graphql_operation_name,
            graphql_mutation_field=graphql_mutation_field,
            graphql_resource_key=graphql_resource_key,
            created_at=time.time(),
            expires_at=time.time() + timeout_seconds,
            max_uses=max_uses,
        )
        self.active_permit = permit
        return permit

    def release_permit(self, permit: Optional[OperationPermit] = None) -> None:
        """Revoke active permit."""
        if permit is None or self.active_permit is permit:
            if self.active_permit:
                self.active_permit.consumed = True
            self.active_permit = None

    @asynccontextmanager
    async def permit_scope(
        self,
        operation_type: str,
        workflow_name: str,
        allowed_methods: Tuple[str, ...],
        endpoint_pattern: Optional[str] = None,
        resource_id: Optional[str] = None,
        run_tag: Optional[str] = None,
        account_context: Optional[str] = None,
        verified_account: Optional[str] = None,
        entity_type: Optional[str] = None,
        project_id: Optional[int] = None,
        designated_identity_field: Optional[str] = None,
        approved_file_path: Optional[str] = None,
        approved_file_name: Optional[str] = None,
        approved_file_hash: Optional[str] = None,
        approved_file_size: Optional[int] = None,
        controlled_dispatch_token: Optional[str] = None,
        parent_resource_id: Optional[str] = None,
        upload_destination: Optional[str] = None,
        allowed_parts: Optional[Tuple[str, ...]] = None,
        permitted_fields: Optional[Tuple[str, ...]] = None,
        graphql_operation_name: Optional[str] = None,
        graphql_mutation_field: Optional[str] = None,
        graphql_resource_key: Optional[str] = "id",
        timeout_seconds: float = 10.0,
        max_uses: int = 1,
    ) -> AsyncIterator[OperationPermit]:
        """Context manager scoping network write authorization to a single action."""
        permit = self.acquire_permit(
            operation_type=operation_type,
            workflow_name=workflow_name,
            allowed_methods=allowed_methods,
            endpoint_pattern=endpoint_pattern,
            resource_id=resource_id,
            run_tag=run_tag,
            account_context=account_context,
            verified_account=verified_account,
            entity_type=entity_type,
            project_id=project_id,
            designated_identity_field=designated_identity_field,
            approved_file_path=approved_file_path,
            approved_file_name=approved_file_name,
            approved_file_hash=approved_file_hash,
            approved_file_size=approved_file_size,
            controlled_dispatch_token=controlled_dispatch_token,
            parent_resource_id=parent_resource_id,
            upload_destination=upload_destination,
            allowed_parts=allowed_parts,
            permitted_fields=permitted_fields,
            graphql_operation_name=graphql_operation_name,
            graphql_mutation_field=graphql_mutation_field,
            graphql_resource_key=graphql_resource_key,
            timeout_seconds=timeout_seconds,
            max_uses=max_uses,
        )
        try:
            yield permit
        finally:
            self.release_permit(permit)

    async def install(self, page_or_context: Any) -> None:
        """Attach routing rules and response listener to Playwright page or context."""
        self._installed_target = page_or_context
        await page_or_context.route("**/*", self._handle_route)
        try:
            page_or_context.on("response", self._handle_response)
        except Exception:
            pass

    def _handle_response(self, response: Any) -> None:
        """Capture authoritative response evidence for active writes."""
        try:
            url = response.url
            status = response.status
            resp_info = {
                "url": url,
                "status": status,
                "ok": response.ok,
                "timestamp": time.time(),
            }
            self.response_history.append(resp_info)
            if self.active_permit:
                self.active_permit.recorded_responses.append(resp_info)
        except Exception:
            pass

    async def _handle_route(self, route: Any) -> None:
        req = route.request
        url = req.url
        method = req.method.upper()
        req_origin = get_canonical_origin(url)
        path_lower = urllib.parse.urlsplit(url).path.lower()

        # 1. Unconditional dangerous endpoint check (blocked regardless of method or permit)
        if any(d in path_lower for d in self.DANGEROUS_ENDPOINTS):
            self.blocked_requests.append({
                "url": url,
                "method": method,
                "reason": f"Prohibited destructive endpoint matching '{path_lower}'",
            })
            await route.abort("blockedbyclient")
            return

        # 2. Origin boundary: block all external domains
        if req_origin != self.target_origin:
            self.blocked_requests.append({
                "url": url,
                "method": method,
                "reason": f"External origin {req_origin} disallowed; boundary requires {self.target_origin}",
            })
            await route.abort("blockedbyclient")
            return

        # 3. Safe read methods (GET, HEAD, OPTIONS) permitted on target origin
        if method in ("GET", "HEAD", "OPTIONS"):
            await route.continue_()
            return

        guard = getattr(self, 'browser_upload_guard', None)
        if (guard and guard.active and self.active_permit
                and hasattr(route, 'fallback')):
            await route.continue_()
            return

        # 4. State-changing methods (POST, PUT, PATCH, DELETE) -> DENY BY DEFAULT
        if self.is_cancelled():
            self.blocked_requests.append({"url": url, "method": method, "reason": "Run cancelled before write dispatch"})
            await route.abort("blockedbyclient")
            return
        permit = self.active_permit
        if not permit or not permit.is_valid(method, req_origin):
            reason = (
                f"Unauthorized state change: {method} {url} without valid OperationPermit "
                f"(active_permit={permit.operation_type if permit else None}, valid={permit.is_valid(method, req_origin) if permit else False})"
            )
            self.blocked_requests.append({"url": url, "method": method, "reason": reason})
            await route.abort("blockedbyclient")
            return

        # Dynamic authenticated session verification at write dispatch
        # Find active page from request frame or installed target
        active_page = None
        frame = getattr(req, "frame", None)
        if frame and hasattr(frame, "page"):
            active_page = frame.page
        elif hasattr(self, "_installed_target") and self._installed_target is not None:
            if hasattr(self._installed_target, "evaluate"):
                active_page = self._installed_target
            elif hasattr(self._installed_target, "pages"):
                pages = getattr(self._installed_target, "pages", [])
                if pages:
                    active_page = pages[0]

        active_actor = None
        if active_page is not None and hasattr(active_page, "evaluate"):
            try:
                active_actor = await AuthenticatedIdentityDiscovery.discover_session_identity(active_page, self.target_origin)
            except Exception:
                active_actor = None

        # If permit requires a verified account, verify dynamic page session matches
        if permit.verified_account:
            if active_actor != permit.verified_account:
                # Dynamic account switch detected between discovery and write dispatch!
                self.set_verified_session_identity(active_actor)
                reason = f"Account mismatch at write dispatch: active page actor '{active_actor}' != permit verified actor '{permit.verified_account}'"
                self.blocked_requests.append({"url": url, "method": method, "reason": reason})
                await route.abort("blockedbyclient")
                return
            else:
                self.verified_session_identity = active_actor
        elif active_actor:
            self.verified_session_identity = active_actor

        # 5. Inspect post_data for prohibited operation semantics
        post_data = req.post_data_buffer or req.post_data or ""
        post_data_lower = post_data.decode("utf-8", "ignore").lower() if isinstance(post_data, bytes) else post_data.lower()

        if any(bad_op in post_data_lower for bad_op in self.PROHIBITED_PAYLOAD_OPERATIONS):
            self.blocked_requests.append({
                "url": url,
                "method": method,
                "reason": "Prohibited operation detected in payload body",
            })
            await route.abort("blockedbyclient")
            return

        # 6. Strict typed request contract validation
        # An unrelated request while a permit is active MUST BE BLOCKED without consuming the permit!
        # Do NOT fall back to permissive unverified cached account_context!
        matches, reason = permit.matches_request(
            method=method,
            url=url,
            post_data=post_data,
            headers=req.headers,
            account_identity=active_actor or self.verified_session_identity,
        )
        if not matches:
            self.blocked_requests.append({
                "url": url,
                "method": method,
                "reason": f"Active permit {permit.operation_type} rejected mismatched request: {reason}",
            })
            await route.abort("blockedbyclient")
            return

        # 7. Consume permit use and allow matching request
        if self.is_cancelled():
            self.blocked_requests.append({"url": url, "method": method, "reason": "Run cancelled during identity verification"})
            await route.abort("blockedbyclient")
            return
        permit.use_count += 1
        if permit.use_count >= permit.max_uses:
            permit.consumed = True
        await route.continue_()



class ProfileWorkflowScope:
    """Discovers and scopes UI actions strictly to validated workflow containers.
    
    Prevents global fallback selectors from clicking unintended controls
    (e.g. 'Delete Account', 'Save Security Settings', 'Add Payment').
    """

    SENSITIVE_KEYWORDS = (
        "account",
        "security",
        "password",
        "billing",
        "payment",
        "invite",
        "revoke",
        "danger",
        "delete account",
    )

    @classmethod
    def is_safe_control(cls, text: str = "", aria_label: str = "", control_id: str = "") -> bool:
        """Verify that control text, label, and ID do not match sensitive/prohibited domains."""
        combined = f"{text} {aria_label} {control_id}".lower()
        return not any(kw in combined for kw in cls.SENSITIVE_KEYWORDS)

    @classmethod
    async def discover_experience_container(cls, page: Any) -> Optional[Any]:
        """Find unambiguous workflow container representing profile experience/work history."""
        # 1. Targeted container IDs
        for cid in ("#section-experience-management", "#experience-section", "#work-history", "#profile-experience"):
            loc = page.locator(cid).first
            if await loc.count() > 0 and await loc.is_visible():
                return loc

        # 2. Containers with verified headings
        headings = page.locator("h2, h3, h4, [role='heading']")
        count = await headings.count()
        for i in range(count):
            h = headings.nth(i)
            txt = (await h.text_content() or "").strip()
            txt_lower = txt.lower()
            if any(term in txt_lower for term in ("experience", "work history", "employment")):
                if not any(bad in txt_lower for bad in ("security", "account", "billing")):
                    container = h.locator("xpath=..")
                    if await container.count() > 0:
                        return container

        return None

    @classmethod
    async def discover_attachment_container(cls, page: Any) -> Optional[Any]:
        """Find unambiguous workflow container representing profile certificates/attachments.
        
        Strictly rejects avatar or profile picture forms.
        """
        # Look for explicit attachment sections
        for sel in (
            "#attachment-section",
            "#section-certificates",
            "section:has(h4:has-text('Certificate'))",
            "div:has(h4:has-text('Certificate'))",
            "section:has(h4:has-text('Document'))",
            "div:has(h4:has-text('Document'))",
            "section:has(h4:has-text('Attachment'))",
            "div:has(h4:has-text('Attachment'))",
        ):
            loc = page.locator(sel).first
            if await loc.count() > 0 and await loc.is_visible():
                # Confirm it does not contain avatar keywords
                inner_text = (await loc.text_content() or "").lower()
                if not any(bad in inner_text for bad in ("avatar", "profile picture", "profile image", "photo")):
                    return loc

        # Check inline within experience container if present
        exp_c = await cls.discover_experience_container(page)
        if exp_c:
            inline_att = exp_c.locator("div:has(input[type='file'])").first
            if await inline_att.count() > 0 and await inline_att.is_visible():
                return inline_att

        return None

    @classmethod
    async def is_safe_attachment_input(cls, input_locator: Any) -> bool:
        """Verify file input is for attachments/documents, NOT an avatar or user profile photo."""
        inp_id = (await input_locator.get_attribute("id") or "").lower()
        inp_name = (await input_locator.get_attribute("name") or "").lower()
        inp_class = (await input_locator.get_attribute("class") or "").lower()
        inp_accept = (await input_locator.get_attribute("accept") or "").lower()

        combined = f"{inp_id} {inp_name} {inp_class} {inp_accept}"
        if any(bad in combined for bad in ("avatar", "profile-pic", "user-photo", "photo", "header-img", "banner")):
            return False
        return True


@dataclass
class ScopedProfileWorkflowResult:
    """Result record for an executed profile workflow."""
    workflow_type: str  # "validation_rejection", "create", "edit", "upload", "delete", "cleanup"
    verdict: str  # "PASS", "FAIL", "LIMITED_COVERAGE"
    entity_id: Optional[str]
    entity_name: Optional[str]
    action_type: str
    details: str
    screenshot_path: Optional[str] = None
    cleanup_status: str = "NOT_APPLICABLE"  # "CLEANED_UP", "PENDING", "FAILED", "NOT_APPLICABLE"
    is_customer_defect: bool = False
    defect_category: Optional[str] = None
    viewport_name: str = "Desktop"


UNCERTAIN_SERVER_STATUSES = {401, 403, 408, 429, 502, 503, 504}


class AutonomousProfileWorkflowRunner:
    """Autonomous executor for authorized profile state-changing workflows.
    
    Invariants:
    1. Scopes every UI action to a verified workflow container.
    2. Writes require an OperationPermit from ScopedMutationNetworkBarrier.
    3. Manages durable ownership journal: records intent before action, confirms identity afterward.
    4. Genuine upload lifecycle: verifies remote attachment creation, executes remote deletion,
       verifies remote absence, and separates local file removal from remote cleanup.
    5. Item-specific edit and delete with run-tag ownership boundary verification.
    """

    def __init__(
        self,
        registry: Optional[ScopedEntityOwnershipRegistry] = None,
        barrier: Optional[ScopedMutationNetworkBarrier] = None,
        scratch_dir: Optional[str] = None,
    ) -> None:
        self.registry = registry or ScopedEntityOwnershipRegistry()
        self.barrier = barrier
        from app.paths import get_data_dir
        self.scratch_dir = scratch_dir or os.path.join(get_data_dir(), "scratch")
        os.makedirs(self.scratch_dir, exist_ok=True)

    @asynccontextmanager
    async def _permit(
        self,
        operation_type: str,
        workflow_name: str,
        allowed_methods: Tuple[str, ...],
        endpoint_pattern: Optional[str] = None,
        resource_id: Optional[str] = None,
        run_tag: Optional[str] = None,
        account_context: Optional[str] = None,
        verified_account: Optional[str] = None,
        entity_type: Optional[str] = None,
        project_id: Optional[int] = None,
        designated_identity_field: Optional[str] = None,
        approved_file_path: Optional[str] = None,
        approved_file_name: Optional[str] = None,
        approved_file_hash: Optional[str] = None,
        approved_file_size: Optional[int] = None,
        controlled_dispatch_token: Optional[str] = None,
        parent_resource_id: Optional[str] = None,
        upload_destination: Optional[str] = None,
        allowed_parts: Optional[Tuple[str, ...]] = None,
        permitted_fields: Optional[Tuple[str, ...]] = None,
        graphql_operation_name: Optional[str] = None,
        graphql_mutation_field: Optional[str] = None,
        graphql_resource_key: Optional[str] = "id",
        timeout_seconds: float = 10.0,
    ) -> AsyncIterator[Optional[OperationPermit]]:
        """Scoping helper that issues permit on barrier if installed."""
        if self.barrier:
            async with self.barrier.permit_scope(
                operation_type=operation_type,
                workflow_name=workflow_name,
                allowed_methods=allowed_methods,
                endpoint_pattern=endpoint_pattern,
                resource_id=resource_id,
                run_tag=run_tag,
                account_context=account_context,
                verified_account=verified_account,
                entity_type=entity_type,
                project_id=project_id,
                designated_identity_field=designated_identity_field,
                approved_file_path=approved_file_path,
                approved_file_name=approved_file_name,
                approved_file_hash=approved_file_hash,
                approved_file_size=approved_file_size,
                controlled_dispatch_token=controlled_dispatch_token,
                parent_resource_id=parent_resource_id,
                upload_destination=upload_destination,
                allowed_parts=allowed_parts,
                permitted_fields=permitted_fields,
                graphql_operation_name=graphql_operation_name,
                graphql_mutation_field=graphql_mutation_field,
                graphql_resource_key=graphql_resource_key,
                timeout_seconds=timeout_seconds,
            ) as p:
                yield p
        else:
            yield None

    async def _verify_authenticated_scope_readback(
        self,
        page: Any,
        expected_scope_selector: str = "#section-experience-management, #profile-experience-container, [data-section='experience']",
        required_element_text: Optional[str] = None,
        forbidden_element_text: Optional[str] = None,
        target_entity_id: Optional[str] = None,
        expected_account: Optional[str] = None,
        intended_origin: Optional[str] = None,
    ) -> Tuple[str, str, Dict[str, Any]]:
        """
        Executes an authoritative reload and verifies:
        1. Reload completes without network or unhandled browser exceptions.
        2. Intended origin contract: page remains on intended origin post-reload.
        3. Page did NOT redirect to login, signin, or auth pages.
        4. Page is not displaying an HTTP 4xx/5xx or application error banner.
        5. Target section/container exists and is rendered (handling collapsible disclosures if needed).
        6. Container is visible and root/child loading states (aria-busy, spinners) have resolved.
        7. Completeness contract: heading or arbitrary text alone does NOT prove deletion.
           Must have explicit empty-state indicator OR authoritative non-empty item list without active pagination/filtering.
        8. Presence requires element to exist AND be visible in DOM.
        9. Authenticated session identity persists post-reload without degradation.

        Returns:
            status: "CONFIRMED_SUCCESS", "CONFIRMED_FAILURE", or "UNCERTAIN_READBACK"
            reason: Explanation string
            diagnostics: Details dict for logging/recovery
        """
        # 1. Authoritative page reload
        try:
            resp = await page.reload(wait_until="domcontentloaded", timeout=10000)
            if resp and resp.status >= 400:
                return "UNCERTAIN_READBACK", f"Page reload returned HTTP error status {resp.status}", {"http_status": resp.status}
            await page.wait_for_timeout(400)
        except Exception as e:
            return "UNCERTAIN_READBACK", f"Page reload failed due to exception: {e}", {"exception": str(e)}

        # 2. Check intended origin contract
        target_origin = intended_origin or (self.barrier.target_origin if self.barrier else None)
        if target_origin:
            try:
                cur_origin = get_canonical_origin(page.url)
                if cur_origin != get_canonical_origin(target_origin):
                    return "UNCERTAIN_READBACK", f"Origin mismatch post-reload: page is on origin '{cur_origin}', expected '{target_origin}'", {"origin": cur_origin, "intended_origin": target_origin}
            except Exception as e:
                return "UNCERTAIN_READBACK", f"Origin inspection failed: {e}", {}

        # 3. Check for authentication redirect
        try:
            cur_url = (page.url or "").lower()
            if any(auth_path in cur_url for auth_path in ("/login", "/signin", "/auth", "redirect=", "login?")):
                return "UNCERTAIN_READBACK", f"Page redirected to login/authentication URL: {page.url}", {"url": page.url}
        except Exception as e:
            return "UNCERTAIN_READBACK", f"Failed inspecting current URL: {e}", {}

        # 4. Check for application error views or HTTP error text
        try:
            body_text = await page.evaluate("() => document.body ? document.body.innerText : ''")
            for err_text in (
                "500 Internal Server Error",
                "502 Bad Gateway",
                "503 Service Unavailable",
                "504 Gateway Timeout",
                "Application Error",
                "Unhandled Exception",
                "Page Not Found",
            ):
                if err_text.lower() in body_text.lower():
                    return "UNCERTAIN_READBACK", f"Error view detected after reload: '{err_text}'", {"error_text": err_text}
        except Exception as e:
            return "UNCERTAIN_READBACK", f"Failed inspecting page text: {e}", {}

        # 5. Verify authenticated session identity post-reload if expected_account provided
        if expected_account:
            try:
                cur_origin = get_canonical_origin(page.url)
                post_acc = await AuthenticatedIdentityDiscovery.discover_session_identity(page, cur_origin)
                if not post_acc or post_acc != expected_account:
                    return "UNCERTAIN_READBACK", f"Session identity unverified or changed post-reload (expected '{expected_account}', got '{post_acc}')", {"discovered": post_acc, "expected": expected_account}
            except Exception as e:
                return "UNCERTAIN_READBACK", f"Session identity check failed post-reload: {e}", {}

        # 6. Check target scope container readiness
        try:
            scope_loc = page.locator(expected_scope_selector).first
            if await scope_loc.count() == 0:
                # Attempt to expand section if collapsed or tab switched
                tab_btn = page.locator("button:has-text('Experience'), a:has-text('Experience'), [data-tab='experience']").first
                if await tab_btn.count() > 0 and await tab_btn.is_visible():
                    await tab_btn.click()
                    await page.wait_for_timeout(400)
                scope_loc = page.locator(expected_scope_selector).first

            if await scope_loc.count() == 0:
                return "UNCERTAIN_READBACK", f"Scope container '{expected_scope_selector}' not found in DOM after reload", {}

            # Scope container must be visible in the DOM
            if not await scope_loc.is_visible():
                return "UNCERTAIN_READBACK", f"Scope container '{expected_scope_selector}' is present in DOM but not visible after reload", {}

            # Wait briefly if root container itself has aria-busy='true'
            is_busy = await scope_loc.get_attribute("aria-busy")
            if is_busy and is_busy.strip().lower() == "true":
                try:
                    await page.wait_for_function(
                        "sel => { const el = document.querySelector(sel); return !el || el.getAttribute('aria-busy') !== 'true'; }",
                        arg=expected_scope_selector,
                        timeout=3000,
                    )
                except Exception:
                    return "UNCERTAIN_READBACK", f"Scope container '{expected_scope_selector}' root remained aria-busy='true' after reload", {}

            # Wait briefly if there is an active spinner or loading indicator
            spinner = scope_loc.locator(".spinner, .loading, [aria-busy='true'], .loading-indicator").first
            if await spinner.count() > 0 and await spinner.is_visible():
                try:
                    await spinner.wait_for(state="hidden", timeout=3000)
                except Exception:
                    return "UNCERTAIN_READBACK", "Scope container loading indicator did not resolve", {}
        except Exception as e:
            return "UNCERTAIN_READBACK", f"Scope container inspection failed: {e}", {}

        # 7. Authoritative presence / absence check inside verified scope
        try:
            if forbidden_element_text:
                # 1. Filtered or paginated container rejection:
                # When a scope container shows pagination or active filters (e.g. 'Page 1 of 3'),
                # absence on the current view does not prove account-level absence.
                has_pagination = False
                pag_loc = scope_loc.locator(".pagination, .pagination-info, [data-pagination], .page-numbers, .paginate, [aria-label*='pagination' i]")
                if await pag_loc.count() > 0 and await pag_loc.first.is_visible():
                    has_pagination = True
                else:
                    scope_text = await scope_loc.inner_text() or ""
                    lower_text = scope_text.lower()
                    if ("page " in lower_text and " of " in lower_text) or ("showing " in lower_text and " of " in lower_text) or ("filtered from" in lower_text):
                        has_pagination = True

                if has_pagination:
                    return "UNCERTAIN_READBACK", f"Scope container '{expected_scope_selector}' indicates a paginated or filtered view; cannot confirm complete absence", {"paginated": True}

                if await scope_loc.get_attribute("data-qa-inventory-complete") != "true":
                    return "UNCERTAIN_READBACK", "Missing explicit inventory completeness contract; rendered items or generic empty-state text cannot prove deletion", {}

                # 2. Authoritative completeness contract:
                # To confirm absence, the container MUST either:
                # a) Contain an explicit empty-state indicator, OR
                # b) Contain an authoritative list element with confirmed non-zero items (none matching deleted item)
                empty_loc = scope_loc.locator(".empty-state, [data-empty-state], [data-empty='true'], .no-items, .no-data, [role='status']:has-text('No')")
                has_empty_indicator = await empty_loc.count() > 0 and await empty_loc.first.is_visible()
                if not has_empty_indicator:
                    scope_text = await scope_loc.inner_text() or ""
                    lower_text = scope_text.lower()
                    for phrase in ("no experience", "no experiences", "no items", "no records", "no attachments", "nothing to display", "list is empty", "no entries"):
                        if phrase in lower_text:
                            has_empty_indicator = True
                            break

                child_items = scope_loc.locator(".exp-item, .attachment-item, [data-id], .list-item, tr.data-row, li.item-row")
                child_count = await child_items.count()

                if child_count == 0 and not has_empty_indicator:
                    return "UNCERTAIN_READBACK", f"Scope container '{expected_scope_selector}' contains no items and lacks explicit empty-state confirmation; heading or placeholder alone cannot confirm deletion", {"child_count": 0, "empty_indicator": False}

                f_count = await scope_loc.get_by_text(forbidden_element_text).count()
                if target_entity_id:
                    tid_str = str(target_entity_id)
                    f_count += await scope_loc.locator(
                        f"[id={json.dumps(tid_str)}], [data-id={json.dumps(tid_str)}], [data-attachment-id={json.dumps(tid_str)}]"
                    ).count()
                if f_count == 0:
                    return "CONFIRMED_SUCCESS", f"Confirmed '{forbidden_element_text}' absent from {expected_scope_selector}", {"completeness_contract": "empty_state" if has_empty_indicator else "rendered_list", "item_count": child_count}
                else:
                    return "CONFIRMED_FAILURE", f"Item '{forbidden_element_text}' still present ({f_count} matches) in {expected_scope_selector}", {"count": f_count}

            if required_element_text:
                r_loc = scope_loc.get_by_text(required_element_text).first
                if await r_loc.count() > 0 and await r_loc.is_visible():
                    return "CONFIRMED_SUCCESS", f"Confirmed '{required_element_text}' present and visible in {expected_scope_selector}", {}
                elif await r_loc.count() > 0:
                    return "CONFIRMED_FAILURE", f"Item '{required_element_text}' present in DOM but hidden/invisible in {expected_scope_selector}", {}
                else:
                    return "CONFIRMED_FAILURE", f"Item '{required_element_text}' missing from {expected_scope_selector}", {}
        except Exception as e:
            return "UNCERTAIN_READBACK", f"Presence/absence verification failed: {e}", {}

        return "CONFIRMED_SUCCESS", "Scope verified ready", {}

    async def run_authorized_profile_suite(
        self,
        page: Any,
        target_url: str,
        run_id: int,
        project_id: int,
        viewport_name: str = "Desktop",
        account_context: Optional[str] = None,
        cancellation_token: Optional[CancellationToken] = None,
        screenshot_fn: Optional[Callable[[Any, str, str], Any]] = None,
    ) -> List[ScopedProfileWorkflowResult]:
        """Execute complete authorized profile workflow suite against active page."""
        results: List[ScopedProfileWorkflowResult] = []
        run_prefix = self.registry.generate_run_prefix(run_id)

        # Step 0: Authoritative identity discovery & verification
        canon_origin = get_canonical_origin(target_url or page.url)
        discovered_account = await AuthenticatedIdentityDiscovery.discover_session_identity(page, canon_origin)

        if not discovered_account:
            return [
                ScopedProfileWorkflowResult(
                    workflow_type="session_identity",
                    verdict="LIMITED_COVERAGE",
                    entity_id=None,
                    entity_name="session_identity",
                    action_type="verify_session_identity",
                    details="Zero writes dispatched: Authenticated session identity could not be verified on profile page. Synthetic identity fallback is prohibited in production.",
                    viewport_name=viewport_name,
                )
            ]

        if account_context and discovered_account != account_context:
            return [
                ScopedProfileWorkflowResult(
                    workflow_type="session_identity",
                    verdict="LIMITED_COVERAGE",
                    entity_id=None,
                    entity_name="session_identity",
                    action_type="verify_session_identity",
                    details=f"Zero writes dispatched: Discovered session identity '{discovered_account}' conflicts with requested account '{account_context}'.",
                    viewport_name=viewport_name,
                )
            ]

        effective_account = discovered_account
        if self.barrier:
            self.barrier.set_verified_session_identity(effective_account)

        # Step 0.5: Cold-restart & pending recovery reconciliation
        recov_res = await self.reconcile_and_recover_pending_entities(
            page=page,
            project_id=project_id,
            target_url=target_url,
            account_context=effective_account,
            screenshot_fn=screenshot_fn,
        )
        results.extend(recov_res)

        pending = self.registry.get_pending_or_uncleaned_records(
            project_id=project_id, account_context=effective_account,
        )
        if any(get_canonical_origin(r.target_url) == canon_origin for r in pending):
            results.append(ScopedProfileWorkflowResult(
                workflow_type="cleanup", verdict="LIMITED_COVERAGE",
                entity_id=None, entity_name="pending_recovery",
                action_type="block_pending_recovery",
                details="Prior cleanup remains uncertain; new mutations are blocked until authoritative recovery completes.",
                cleanup_status="PENDING", is_customer_defect=False,
                viewport_name=viewport_name,
            ))
            return results

        # 1. Negative Validation Rejection Test
        if cancellation_token and cancellation_token.is_cancelled():
            return results
        val_res = await self._exercise_validation_rejection(
            page, target_url, run_id, project_id, viewport_name, cancellation_token, screenshot_fn
        )
        if val_res:
            results.append(val_res)

        # 2. Test Material Creation Test
        if cancellation_token and cancellation_token.is_cancelled():
            return results
        created_rec, create_res = await self._exercise_create_workflow(
            page, target_url, run_id, project_id, run_prefix, viewport_name, effective_account, cancellation_token, screenshot_fn
        )
        if create_res:
            results.append(create_res)
            if create_res.verdict != "PASS":
                return results

        # 3. Test Material Modification / Edit Test
        if created_rec and (not cancellation_token or not cancellation_token.is_cancelled()):
            edit_res = await self._exercise_edit_workflow(
                page, target_url, run_id, project_id, created_rec, viewport_name, effective_account, cancellation_token, screenshot_fn
            )
            if edit_res:
                results.append(edit_res)
                if edit_res.verdict != "PASS":
                    return results

        # 4. Genuine File Upload Test (with remote deletion)
        if cancellation_token and cancellation_token.is_cancelled():
            cleanup_res = await self._cleanup_run_entities(
                page, run_id, project_id, viewport_name, target_url, effective_account, screenshot_fn
            )
            results.extend(cleanup_res)
            return results

        upload_res = await self._exercise_upload_workflow(
            page, target_url, run_id, project_id, run_prefix, viewport_name, effective_account, cancellation_token, screenshot_fn
        )
        if upload_res:
            results.append(upload_res)
            if upload_res.cleanup_status == "PENDING":
                cleanup_res = await self._cleanup_run_entities(
                    page=page,
                    run_id=run_id,
                    project_id=project_id,
                    viewport_name=viewport_name,
                    target_url=target_url,
                    account_context=effective_account,
                    screenshot_fn=screenshot_fn,
                )
                results.extend(cleanup_res)
                return results

        # 5. Ownership-Verified Deletion of Created Test Entity
        if created_rec:
            delete_res = await self._exercise_delete_workflow(
                page=page,
                run_id=run_id,
                project_id=project_id,
                record=created_rec,
                viewport_name=viewport_name,
                target_url=target_url,
                account_context=effective_account,
                cancellation_token=cancellation_token,
                screenshot_fn=screenshot_fn,
            )
            if delete_res:
                results.append(delete_res)

        # 6. Final Sweep & Recovery Cleanup Verification
        cleanup_res = await self._cleanup_run_entities(
            page=page,
            run_id=run_id,
            project_id=project_id,
            viewport_name=viewport_name,
            target_url=target_url,
            account_context=effective_account,
            screenshot_fn=screenshot_fn,
        )
        results.extend(cleanup_res)

        return results

    async def reconcile_and_recover_pending_entities(
        self,
        page: Any,
        project_id: int,
        target_url: str,
        account_context: Optional[str] = None,
        run_id: Optional[int] = None,
        screenshot_fn: Optional[Callable[[Any, str, str], Any]] = None,
    ) -> List[ScopedProfileWorkflowResult]:
        """Reconcile and recover pending or ambiguous entities across cold restarts / interrupted runs.
        
        Guarantees:
        1. Verifies authenticated session identity on target page. If unverified or conflicting with
           record account_context, cross-account cleanup is strictly forbidden.
        2. Reconciles against live server/DOM state:
           - If entity is absent from live page DOM, confirms remote deletion without creating duplicates.
           - If entity is present on live page DOM and owned by verified test prefix, issues authorized
             permit to delete and confirms absence upon reload.
        3. Updates SQLite journal status to CONFIRMED_DELETED / CLEANED_UP.
        """
        results: List[ScopedProfileWorkflowResult] = []
        if type(project_id) is not int or project_id <= 0:
            return results
        if run_id is not None and (type(run_id) is not int or run_id <= 0):
            return results
        canon_origin = get_canonical_origin(target_url or (page.url if hasattr(page, "url") else ""))
        discovered_account = await AuthenticatedIdentityDiscovery.discover_session_identity(page, canon_origin)
        if not discovered_account:
            return results

        if account_context and discovered_account != account_context:
            return results

        effective_account = discovered_account
        if self.barrier:
            self.barrier.set_verified_session_identity(effective_account)

        unreconciled = self.registry.get_pending_or_uncleaned_records(
            project_id=project_id,
            account_context=effective_account,
        )
        if run_id is not None:
            unreconciled = [r for r in unreconciled if r.run_id == run_id]

        for r in unreconciled:
            try:
                if r.account_context and r.account_context != effective_account:
                    continue
                if get_canonical_origin(r.target_url) != canon_origin:
                    continue

                if not r.local_cleaned and r.entity_type == "profile_attachment":
                    try:
                        details = json.loads(r.details_json or "{}")
                        lpath = details.get("local_path")
                        if lpath and os.path.exists(lpath):
                            os.remove(lpath)
                        self.registry.mark_local_cleaned(r.id)
                        r.local_cleaned = True
                    except Exception:
                        pass

                details_obj = {}
                if r.details_json:
                    try:
                        parsed = json.loads(r.details_json)
                        if isinstance(parsed, dict):
                            details_obj = parsed
                    except Exception:
                        details_obj = {}

                if not r.remote_entity_id or not details_obj.get("confirmed_at"):
                    results.append(ScopedProfileWorkflowResult(
                        workflow_type="cleanup",
                        verdict="LIMITED_COVERAGE",
                        entity_id=r.remote_entity_id,
                        entity_name=r.entity_name,
                        action_type="reconcile_pending_cleanup",
                        details=(
                            f"Pending intent '{r.entity_name}' lacks confirmed remote ID or "
                            "confirmed creation provenance; automatic recovery blocked."
                        ),
                        cleanup_status="PENDING",
                        is_customer_defect=False,
                    ))
                    continue

                scope_selector = (
                    "#section-attachment-management, [data-section='attachments'], #section-experience-management"
                    if r.entity_type == "profile_attachment" else
                    "#section-experience-management, #profile-experience-container, [data-section='experience']"
                )
                readback_status, reason, _ = await self._verify_authenticated_scope_readback(
                    page=page, expected_scope_selector=scope_selector,
                    forbidden_element_text=r.entity_name, target_entity_id=r.remote_entity_id,
                    expected_account=effective_account, intended_origin=canon_origin,
                )
                if readback_status == "UNCERTAIN_READBACK":
                    results.append(ScopedProfileWorkflowResult(
                        workflow_type="cleanup", verdict="LIMITED_COVERAGE",
                        entity_id=r.remote_entity_id, entity_name=r.entity_name,
                        action_type="reconcile_pending_cleanup", details=reason,
                        cleanup_status="PENDING", is_customer_defect=False,
                    ))
                    continue

                if readback_status == "CONFIRMED_SUCCESS":
                    self.registry.confirm_remote_deletion(r.id)
                    r.remote_cleaned = True
                    r.status = "DELETED"
                    r.remote_status = "CONFIRMED_DELETED"
                    results.append(
                        ScopedProfileWorkflowResult(
                            workflow_type="cleanup",
                            verdict="PASS",
                            entity_id=r.remote_entity_id,
                            entity_name=r.entity_name,
                            action_type="reconcile_pending_cleanup",
                            details=f"Reconciled pending entity '{r.entity_name}': confirmed absent from server state and marked deleted in journal.",
                            cleanup_status="CLEANED_UP",
                            is_customer_defect=False,
                        )
                    )
                else:
                    item = page.locator(
                        f"[id={json.dumps(str(r.remote_entity_id))}], [data-id={json.dumps(str(r.remote_entity_id))}], [data-attachment-id={json.dumps(str(r.remote_entity_id))}]"
                    )
                    if await item.count() != 1 or await item.get_by_text(r.entity_name, exact=True).count() != 1:
                        # A recycled ID, renamed resource, or non-unique match is not safe to delete.
                        results.append(
                            ScopedProfileWorkflowResult(
                                workflow_type="cleanup",
                                verdict="LIMITED_COVERAGE",
                                entity_id=r.remote_entity_id,
                                entity_name=r.entity_name,
                                action_type="reconcile_pending_cleanup",
                                details=f"Pending entity '{r.entity_name}' (ID: {r.remote_entity_id}) failed uniqueness or exact name match in DOM ({await item.count()} items, {await item.get_by_text(r.entity_name, exact=True).count()} text matches); cannot safely delete.",
                                cleanup_status="PENDING",
                                is_customer_defect=False,
                            )
                        )
                        continue

                    del_btn = None
                    if r.entity_type == "profile_attachment":
                        del_btn = item.locator(f"[data-delete-attachment={json.dumps(str(r.remote_entity_id))}], button[data-filename={json.dumps(str(r.entity_name))}], button:has-text('Delete'), button:has-text('Remove')").first
                        op_type = OperationType.DELETE_ATTACHMENT
                        endpoint_pattern = rf"^/api/(?:profile/)?attachments/{re.escape(str(r.remote_entity_id))}/?$"
                    else:
                        del_btn = item.locator(
                            f"[id={json.dumps(f'btn-delete-{r.remote_entity_id}')}], button[data-id={json.dumps(str(r.remote_entity_id))}][data-action='delete'], button[data-delete={json.dumps(str(r.remote_entity_id))}], button:has-text('Delete')"
                        ).first
                        op_type = OperationType.DELETE_ENTITY
                        endpoint_pattern = rf"^/api/(?:profile/)?experience/{re.escape(str(r.remote_entity_id))}/?$"

                    if not del_btn or not hasattr(del_btn, "count") or await del_btn.count() == 0 or not await del_btn.is_visible():
                        results.append(
                            ScopedProfileWorkflowResult(
                                workflow_type="cleanup",
                                verdict="LIMITED_COVERAGE",
                                entity_id=r.remote_entity_id,
                                entity_name=r.entity_name,
                                action_type="reconcile_pending_cleanup",
                                details=f"Delete control for pending entity '{r.entity_name}' (ID: {r.remote_entity_id}) not found or not visible in DOM; left as PENDING.",
                                cleanup_status="PENDING",
                                is_customer_defect=False,
                            )
                        )
                        continue

                    cleanup_succeeded = False
                    uncleaned_reason = "Unknown recovery failure"
                    try:
                        async with self._permit(
                            operation_type=op_type,
                            workflow_name=r.entity_type,
                            allowed_methods=("DELETE", "POST"),
                            endpoint_pattern=endpoint_pattern,
                            resource_id=r.remote_entity_id,
                            account_context=effective_account,
                            verified_account=effective_account,
                            entity_type=r.entity_type,
                            project_id=project_id,
                        ):
                            await del_btn.click()
                            await page.wait_for_timeout(300)
                            confirm_modal_btn = page.locator("dialog[open] #btn-confirm-delete, dialog[open] #confirm-delete-btn, dialog[open] [data-confirm='true'], dialog[open] .btn-confirm-delete").first
                            if await confirm_modal_btn.count() > 0 and await confirm_modal_btn.is_visible():
                                await confirm_modal_btn.click()
                                await page.wait_for_timeout(300)

                        readback_status, reason, diag = await self._verify_authenticated_scope_readback(
                            page=page,
                            expected_scope_selector=scope_selector,
                            forbidden_element_text=r.entity_name,
                            target_entity_id=r.remote_entity_id,
                            expected_account=effective_account,
                            intended_origin=canon_origin,
                        )
                        if readback_status == "CONFIRMED_SUCCESS":
                            self.registry.confirm_remote_deletion(r.id)
                            r.remote_cleaned = True
                            r.status = "DELETED"
                            r.remote_status = "CONFIRMED_DELETED"
                            results.append(
                                ScopedProfileWorkflowResult(
                                    workflow_type="cleanup",
                                    verdict="PASS",
                                    entity_id=r.remote_entity_id,
                                    entity_name=r.entity_name,
                                    action_type="recover_and_delete_pending",
                                    details=f"Recovered and deleted pending entity '{r.entity_name}' (ID: {r.remote_entity_id}), confirmed absence in DOM upon reload.",
                                    cleanup_status="CLEANED_UP",
                                    is_customer_defect=False,
                                )
                            )
                            cleanup_succeeded = True
                        else:
                            uncleaned_reason = f"Readback returned {readback_status}: {reason}"
                    except Exception as ex:
                        uncleaned_reason = f"Exception during recovery deletion: {ex}"

                    if not cleanup_succeeded:
                        results.append(
                            ScopedProfileWorkflowResult(
                                workflow_type="cleanup",
                                verdict="LIMITED_COVERAGE",
                                entity_id=r.remote_entity_id,
                                entity_name=r.entity_name,
                                action_type="reconcile_pending_cleanup",
                                details=f"Pending entity '{r.entity_name}' (ID: {r.remote_entity_id}) remains uncleaned: {uncleaned_reason}. Left registered as PENDING for future recovery.",
                                cleanup_status="PENDING",
                                is_customer_defect=False,
                            )
                        )
            except Exception as err:
                results.append(
                    ScopedProfileWorkflowResult(
                        workflow_type=getattr(r, "entity_type", "cleanup") or "cleanup",
                        verdict="LIMITED_COVERAGE",
                        entity_id=getattr(r, "remote_entity_id", None),
                        entity_name=getattr(r, "entity_name", "unknown"),
                        action_type="reconcile_pending_cleanup",
                        details=f"Recovery failed with exception for '{getattr(r, 'entity_name', 'unknown')}': {err}. Preserved as PENDING.",
                        cleanup_status="PENDING",
                        is_customer_defect=False,
                    )
                )

        return results

    async def _exercise_validation_rejection(
        self,
        page: Any,
        target_url: str,
        run_id: int,
        project_id: int,
        viewport_name: str,
        cancellation_token: Optional[CancellationToken],
        screenshot_fn: Optional[Callable[[Any, str, str], Any]],
    ) -> Optional[ScopedProfileWorkflowResult]:
        """Verify that submitting empty/invalid data is rejected without unintended writes."""
        try:
            container = await ProfileWorkflowScope.discover_experience_container(page)
            if not container:
                return ScopedProfileWorkflowResult(
                    workflow_type="validation_rejection",
                    verdict="LIMITED_COVERAGE",
                    entity_id=None,
                    entity_name=None,
                    action_type="submit_invalid_form",
                    details="No verified profile experience container found on active page.",
                    viewport_name=viewport_name,
                )

            # Look for Add trigger strictly inside the verified container
            btn_add = container.locator("#btn-add-experience, button:has-text('Add Experience'), button:has-text('Add')").first
            if await btn_add.count() == 0 or not await btn_add.is_visible():
                return None

            await btn_add.click()
            await page.wait_for_timeout(300)

            # Find dialog or form opened for adding experience
            dialog = page.locator("dialog[open], #dialog-add-experience[open], form#form-add-experience").first
            form_ctx = dialog if await dialog.count() > 0 and await dialog.is_visible() else container

            btn_submit = form_ctx.locator("#btn-submit-experience, button[type='submit']:has-text('Save'), button:has-text('Save Experience')").first
            if await btn_submit.count() == 0:
                # Close if possible
                btn_cancel = form_ctx.locator("#btn-cancel-add-experience, button:has-text('Cancel')").first
                if await btn_cancel.count() > 0 and await btn_cancel.is_visible():
                    await btn_cancel.click()
                return None

            # Clear required inputs
            company_input = form_ctx.locator("#exp-company, input[name='company'], input[placeholder*='Company']").first
            if await company_input.count() > 0 and await company_input.is_visible():
                await company_input.fill("")

            # Note: No permit is issued. State change must be rejected client-side or blocked.
            await btn_submit.click()
            await page.wait_for_timeout(300)

            feedback = form_ctx.locator("#exp-feedback, .error, .invalid-feedback, [role='alert']").first
            feedback_text = (await feedback.text_content() or "") if await feedback.count() > 0 and await feedback.is_visible() else ""
            is_rejected = bool(feedback_text and any(k in feedback_text.lower() for k in ("require", "error", "fill", "invalid")))

            shot_path = None
            if screenshot_fn:
                shot_path = await screenshot_fn(page, f"val_rejection_{viewport_name}", "VAL-001")

            # Dismiss dialog
            btn_cancel = form_ctx.locator("#btn-cancel-add-experience, button:has-text('Cancel')").first
            if await btn_cancel.count() > 0 and await btn_cancel.is_visible():
                await btn_cancel.click()
                await page.wait_for_timeout(200)
            else:
                try:
                    await page.keyboard.press("Escape")
                    await page.wait_for_timeout(200)
                except Exception:
                    pass

            if is_rejected:
                return ScopedProfileWorkflowResult(
                    workflow_type="validation_rejection",
                    verdict="PASS",
                    entity_id=None,
                    entity_name=None,
                    action_type="submit_invalid_form",
                    details=f"Form submission with empty required fields was rejected with validation error: '{feedback_text.strip()}'. Zero unintended records created.",
                    screenshot_path=shot_path,
                    cleanup_status="NOT_APPLICABLE",
                    viewport_name=viewport_name,
                )
            else:
                # Check HTML5 validation constraint
                is_invalid = await page.evaluate("() => { const el = document.querySelector('#exp-company') || document.querySelector('input[required]'); return el ? !el.checkValidity() : false; }")
                if is_invalid:
                    return ScopedProfileWorkflowResult(
                        workflow_type="validation_rejection",
                        verdict="PASS",
                        entity_id=None,
                        entity_name=None,
                        action_type="submit_invalid_form",
                        details="Client-side required constraint prevented invalid form submission. Zero unintended records created.",
                        screenshot_path=shot_path,
                        cleanup_status="NOT_APPLICABLE",
                        viewport_name=viewport_name,
                    )
                return ScopedProfileWorkflowResult(
                    workflow_type="validation_rejection",
                    verdict="LIMITED_COVERAGE",
                    entity_id=None,
                    entity_name=None,
                    action_type="submit_invalid_form",
                    details="Validation rejection feedback was not observed in DOM.",
                    viewport_name=viewport_name,
                )
        except Exception as e:
            return ScopedProfileWorkflowResult(
                workflow_type="validation_rejection",
                verdict="LIMITED_COVERAGE",
                entity_id=None,
                entity_name=None,
                action_type="submit_invalid_form",
                details=f"Validation rejection observation inconclusive: {e}",
                viewport_name=viewport_name,
            )

    async def _exercise_create_workflow(
        self,
        page: Any,
        target_url: str,
        run_id: int,
        project_id: int,
        run_prefix: str,
        viewport_name: str = "Desktop",
        account_context: Optional[str] = None,
        cancellation_token: Optional[CancellationToken] = None,
        screenshot_fn: Optional[Callable[[Any, str, str], Any]] = None,
    ) -> Tuple[Optional[ScopedOwnershipRecord], Optional[ScopedProfileWorkflowResult]]:
        """Autonomously create a test profile item with unique run-scoped tag under permit."""
        test_company = f"{run_prefix} Nordic Quality AB"
        test_role = "Senior Automation Tester"
        test_desc = "Automated test item created by QA-Tester verification suite."

        # Step 1: Record intent in SQLite journal BEFORE initiating action
        intent_record = self.registry.record_creation_intent(
            run_id=run_id,
            project_id=project_id,
            target_url=target_url,
            entity_type="profile_experience",
            run_prefix=run_prefix,
            proposed_name=test_company,
            account_context=account_context,
            details={"role": test_role, "desc": test_desc},
        )

        try:
            container = await ProfileWorkflowScope.discover_experience_container(page)
            if not container:
                self.registry.mark_ambiguous_creation(intent_record.id, "No verified experience workflow container found")
                return None, ScopedProfileWorkflowResult(
                    workflow_type="create",
                    verdict="LIMITED_COVERAGE",
                    entity_id=None,
                    entity_name=test_company,
                    action_type="create_profile_item",
                    details="No verified profile experience container found on active page.",
                    viewport_name=viewport_name,
                )

            # Check if add button exists in container
            btn_add = container.locator("#btn-add-experience, button:has-text('Add Experience'), button:has-text('Add')").first
            if await btn_add.count() > 0 and await btn_add.is_visible():
                await btn_add.click()
                await page.wait_for_timeout(300)

            dialog = page.locator("dialog[open], #dialog-add-experience[open], form#form-add-experience").first
            form_ctx = dialog if await dialog.count() > 0 and await dialog.is_visible() else container

            company_input = form_ctx.locator("#exp-company, input[name='company'], input[placeholder*='Company']").first
            role_input = form_ctx.locator("#exp-role, input[name='role'], input[placeholder*='Role'], input[placeholder*='Title']").first
            desc_input = form_ctx.locator("#exp-desc, textarea[name='desc'], textarea[name='description'], input[placeholder*='Description']").first

            if await company_input.count() == 0:
                self.registry.mark_ambiguous_creation(intent_record.id, "Creation inputs not found in form context")
                return None, ScopedProfileWorkflowResult(
                    workflow_type="create",
                    verdict="LIMITED_COVERAGE",
                    entity_id=None,
                    entity_name=test_company,
                    action_type="create_profile_item",
                    details="Profile creation inputs not found in form context.",
                    viewport_name=viewport_name,
                )

            await company_input.fill(test_company)
            if await role_input.count() > 0:
                await role_input.fill(test_role)
            if await desc_input.count() > 0:
                await desc_input.fill(test_desc)

            btn_submit = form_ctx.locator("#btn-submit-experience, button[type='submit']:has-text('Save'), button:has-text('Save Experience')").first
            if await btn_submit.count() == 0:
                self.registry.mark_ambiguous_creation(intent_record.id, "Submit button not found in form context")
                return None, ScopedProfileWorkflowResult(
                    workflow_type="create",
                    verdict="LIMITED_COVERAGE",
                    entity_id=None,
                    entity_name=test_company,
                    action_type="create_profile_item",
                    details="Submit button not found in form context.",
                    viewport_name=viewport_name,
                )

            # Step 2: Acquire Operation Permit and submit
            async with self._permit(
                operation_type=OperationType.CREATE_ENTITY,
                workflow_name="profile_experience",
                allowed_methods=("POST", "PUT"),
                endpoint_pattern=r"^/api/(?:profile/)?experience/?$",
                run_tag=run_prefix,
                account_context=account_context,
                verified_account=account_context,
                entity_type="profile_experience",
                project_id=project_id,
                designated_identity_field="company",
                permitted_fields=("company", "role", "desc", "description", "start_date", "end_date"),
            ) as create_permit:
                await btn_submit.click()
                await page.wait_for_timeout(500)

            # Check server response evidence under permit
            server_failed = False
            error_status = 0
            if create_permit and create_permit.recorded_responses:
                for resp in create_permit.recorded_responses:
                    if resp.get("status", 200) >= 400:
                        server_failed = True
                        error_status = resp.get("status")
                        break

            if server_failed:
                is_uncertain = (error_status in UNCERTAIN_SERVER_STATUSES or error_status == 0)
                # An error response plus transient DOM text is not a creation receipt.
                # Preserve the intent, without promoting a guessed ID to deletion authority.
                disc_remote_id = None
                self.registry.mark_ambiguous_creation(
                    intent_record.id,
                    f"Server returned {'uncertain ' if is_uncertain else ''}HTTP {error_status} during creation",
                    remote_entity_id=disc_remote_id,
                )
                return (intent_record if disc_remote_id else None), ScopedProfileWorkflowResult(
                    workflow_type="create",
                    verdict="LIMITED_COVERAGE" if is_uncertain else "FAIL",
                    entity_id=disc_remote_id,
                    entity_name=test_company,
                    action_type="create_profile_item",
                    details=(
                        f"Form submitted for '{test_company}', but server returned uncertain status HTTP {error_status} (transient gateway/availability/timeout issue)."
                        if is_uncertain else
                        (
                            f"Server returned HTTP {error_status} during creation, but entity '{test_company}' appeared in DOM (Remote ID: {disc_remote_id}). Entity registered for recovery cleanup."
                            if disc_remote_id else
                            f"Form submitted for '{test_company}', but server rejected creation with HTTP {error_status}."
                        )
                    ),
                    cleanup_status="PENDING",
                    is_customer_defect=not is_uncertain,
                    defect_category=None if is_uncertain else "PERSISTENCE_CONFIRMATION_FAILED",
                    viewport_name=viewport_name,
                )

            # Step 3: Establish confirmed remote identity from DOM
            created_item = page.get_by_text(test_company).last
            is_present = await created_item.count() > 0

            remote_id = None
            if is_present:
                remote_id = await page.evaluate(
                    """company => {
                        const all = Array.from(document.querySelectorAll('*')).reverse();
                        for (const el of all) {
                            if (el.children.length === 0 && el.textContent && el.textContent.includes(company)) {
                                const container = el.closest('[data-id]') || el.closest('.exp-item') || el.closest('[id^="exp-item"]') || el.closest('li, tr, article, .item, .card');
                                if (container) {
                                    return container.getAttribute('data-id') || container.getAttribute('id');
                                }
                            }
                        }
                        return null;
                    }""",
                    test_company,
                )

            if not is_present or not remote_id:
                # Identity cannot be confirmed unambiguously
                self.registry.mark_ambiguous_creation(intent_record.id, "DOM element or remote ID was not confirmed after submit")
                is_defect = (not is_present)
                return None, ScopedProfileWorkflowResult(
                    workflow_type="create",
                    verdict="FAIL" if is_defect else "LIMITED_COVERAGE",
                    entity_id=None,
                    entity_name=test_company,
                    action_type="create_profile_item",
                    details=(
                        f"Form submitted for '{test_company}', but item appearance was not detected in DOM."
                        if is_defect else
                        f"Form submitted for '{test_company}' and rendered in DOM, but container lacks a distinguishable remote ID contract attribute."
                    ),
                    cleanup_status="PENDING",
                    is_customer_defect=is_defect,
                    defect_category="PERSISTENCE_CONFIRMATION_FAILED" if is_defect else None,
                    viewport_name=viewport_name,
                )

            # Step 4: Confirm creation in SQLite journal with remote ID
            self.registry.confirm_creation(
                record_id=intent_record.id,
                remote_entity_id=remote_id,
                confirmed_name=test_company,
                details={"role": test_role, "desc": test_desc},
            )
            intent_record.remote_entity_id = remote_id
            intent_record.entity_id = remote_id
            intent_record.entity_name = test_company
            intent_record.status = "CONFIRMED_CREATED"

            canon_origin = get_canonical_origin(target_url or (page.url if hasattr(page, "url") else ""))
            # Step 5: Authoritative authenticated reload & read-back verification
            readback_status, reason, diag = await self._verify_authenticated_scope_readback(
                page=page,
                expected_scope_selector="#section-experience-management, #profile-experience-container, [data-section='experience']",
                required_element_text=test_company,
                target_entity_id=remote_id,
                expected_account=account_context,
                intended_origin=canon_origin,
            )

            if readback_status == "CONFIRMED_SUCCESS":
                shot_path = None
                if screenshot_fn:
                    shot_path = await screenshot_fn(page, f"create_success_{viewport_name}", "MUT-001")

                return intent_record, ScopedProfileWorkflowResult(
                    workflow_type="create",
                    verdict="PASS",
                    entity_id=remote_id,
                    entity_name=test_company,
                    action_type="create_profile_item",
                    details=f"Successfully created run-owned test entity '{test_company}' (Remote ID: {remote_id}), confirmed in DOM and verified upon authenticated page reload.",
                    screenshot_path=shot_path,
                    cleanup_status="PENDING",
                    viewport_name=viewport_name,
                )
            elif readback_status == "CONFIRMED_FAILURE":
                self.registry.mark_ambiguous_creation(intent_record.id, f"Item missing after reload: {reason}")
                return None, ScopedProfileWorkflowResult(
                    workflow_type="create",
                    verdict="FAIL",
                    entity_id=remote_id,
                    entity_name=test_company,
                    action_type="create_profile_item",
                    details=f"Test entity '{test_company}' appeared in DOM initially, but was not present upon authenticated page reload ({reason}).",
                    cleanup_status="PENDING",
                    is_customer_defect=True,
                    defect_category="PERSISTENCE_CONFIRMATION_FAILED",
                    viewport_name=viewport_name,
                )
            else:  # UNCERTAIN_READBACK (failed reload, login redirect, error view, unrendered container)
                self.registry.mark_ambiguous_creation(intent_record.id, f"Read-back uncertainty: {reason}")
                return intent_record, ScopedProfileWorkflowResult(
                    workflow_type="create",
                    verdict="LIMITED_COVERAGE",
                    entity_id=remote_id,
                    entity_name=test_company,
                    action_type="create_profile_item",
                    details=f"Test entity creation completed, but post-creation read-back was uncertain ({reason}). Entity remains registered for cleanup.",
                    cleanup_status="PENDING",
                    is_customer_defect=False,
                    viewport_name=viewport_name,
                )
        except Exception as e:
            self.registry.mark_ambiguous_creation(intent_record.id, f"Exception during creation: {e}")
            return None, ScopedProfileWorkflowResult(
                workflow_type="create",
                verdict="LIMITED_COVERAGE" if any(w in str(e).lower() for w in ("reload", "timeout", "navigation")) else "FAIL",
                entity_id=None,
                entity_name=test_company,
                action_type="create_profile_item",
                details=f"Test entity creation encountered exception: {e}",
                cleanup_status="PENDING",
                is_customer_defect=False,
                viewport_name=viewport_name,
            )

    async def _exercise_edit_workflow(
        self,
        page: Any,
        target_url: str,
        run_id: int,
        project_id: int,
        record: ScopedOwnershipRecord,
        viewport_name: str = "Desktop",
        account_context: Optional[str] = None,
        cancellation_token: Optional[CancellationToken] = None,
        screenshot_fn: Optional[Callable[[Any, str, str], Any]] = None,
    ) -> Optional[ScopedProfileWorkflowResult]:
        """Autonomously edit the verified run-owned test item under permit."""
        # Safety gate: verify strict run ownership and destructive authority
        if not self.registry.is_owned_by_run(
            run_id=run_id,
            project_id=project_id,
            target_url=target_url,
            remote_entity_id=record.remote_entity_id,
            entity_name=record.entity_name,
            account_context=account_context,
            entity_type=record.entity_type,
            require_destructive_authority=True,
        ):
            return ScopedProfileWorkflowResult(
                workflow_type="edit",
                verdict="FAIL",
                entity_id=record.remote_entity_id,
                entity_name=record.entity_name,
                action_type="edit_profile_item",
                details=f"Destructive authority check failed: Entity '{record.entity_name}' cannot be modified by run {run_id}.",
                viewport_name=viewport_name,
            )

        canon_origin = get_canonical_origin(target_url or (page.url if hasattr(page, "url") else ""))
        updated_name = f"{record.entity_name} (Updated)"
        try:
            # Locate specific container in DOM
            container = None
            if record.remote_entity_id:
                c_loc = page.locator(f"[id={json.dumps(str(record.remote_entity_id))}], [data-id={json.dumps(str(record.remote_entity_id))}]").first
                if await c_loc.count() > 0 and await c_loc.is_visible():
                    container = c_loc

            if not container:
                c_loc = page.locator(".exp-item, [data-id]").filter(has_text=record.entity_name).first
                if await c_loc.count() > 0:
                    container = c_loc

            if not container:
                return ScopedProfileWorkflowResult(
                    workflow_type="edit",
                    verdict="LIMITED_COVERAGE",
                    entity_id=record.remote_entity_id,
                    entity_name=record.entity_name,
                    action_type="edit_profile_item",
                    details=f"Container for test entity '{record.entity_name}' not found in DOM.",
                    viewport_name=viewport_name,
                )

            # Item-specific edit button strictly scoped to the container
            btn_edit = container.locator("#btn-edit-experience, button:has-text('Edit'), a:has-text('Edit')").first
            if await btn_edit.count() == 0 or not await btn_edit.is_visible():
                return ScopedProfileWorkflowResult(
                    workflow_type="edit",
                    verdict="LIMITED_COVERAGE",
                    entity_id=record.remote_entity_id,
                    entity_name=record.entity_name,
                    action_type="edit_profile_item",
                    details="Item-specific edit control not found within entity container.",
                    viewport_name=viewport_name,
                )

            await btn_edit.click()
            await page.wait_for_timeout(300)

            # Edit dialog
            edit_dialog = page.locator("dialog[open], #dialog-edit-experience[open], form#form-edit-experience").first
            edit_input = edit_dialog.locator("input:not([type='hidden']), #edit-exp-company").first
            btn_save = edit_dialog.locator("#btn-save-experience, button:has-text('Save Changes'), button[type='submit']:has-text('Save')").first

            if await edit_input.count() == 0 or await btn_save.count() == 0:
                return ScopedProfileWorkflowResult(
                    workflow_type="edit",
                    verdict="LIMITED_COVERAGE",
                    entity_id=record.remote_entity_id,
                    entity_name=record.entity_name,
                    action_type="edit_profile_item",
                    details="Edit input or save control not found in edit dialog.",
                    viewport_name=viewport_name,
                )

            await edit_input.fill(updated_name)

            # Acquire permit for edit with exact resource contract
            async with self._permit(
                operation_type=OperationType.EDIT_ENTITY,
                workflow_name="profile_experience",
                allowed_methods=("POST", "PUT", "PATCH"),
                endpoint_pattern=rf"^/api/(?:profile/)?experience/{re.escape(record.remote_entity_id)}/?$",
                resource_id=record.remote_entity_id,
                run_tag=record.run_prefix,
                account_context=account_context,
                verified_account=account_context,
                entity_type=record.entity_type,
                project_id=project_id,
                designated_identity_field="company",
                permitted_fields=("id", "company", "role", "desc", "description", "start_date", "end_date"),
            ) as edit_permit:
                await btn_save.click()
                await page.wait_for_timeout(500)

            # Check server response evidence under permit
            server_failed = False
            error_status = 0
            if edit_permit and edit_permit.recorded_responses:
                for resp in edit_permit.recorded_responses:
                    if resp.get("status", 200) >= 400:
                        server_failed = True
                        error_status = resp.get("status")
                        break

            if server_failed:
                is_uncertain = (error_status in UNCERTAIN_SERVER_STATUSES or error_status == 0)
                if is_uncertain:
                    return ScopedProfileWorkflowResult(
                        workflow_type="edit",
                        verdict="LIMITED_COVERAGE",
                        entity_id=record.remote_entity_id,
                        entity_name=record.entity_name,
                        action_type="edit_profile_item",
                        details=f"Edit saved for '{record.entity_name}', but server returned uncertain status HTTP {error_status} (transient gateway/availability/timeout issue).",
                        cleanup_status="PENDING",
                        is_customer_defect=False,
                        defect_category=None,
                        viewport_name=viewport_name,
                    )

                # Authoritative readback check: did server persist updated name despite HTTP 500 (crash-after-commit)?
                readback_status, reason, _ = await self._verify_authenticated_scope_readback(
                    page=page,
                    expected_scope_selector="#section-experience-management, #profile-experience-container, [data-section='experience']",
                    required_element_text=updated_name,
                    target_entity_id=record.remote_entity_id,
                    expected_account=account_context,
                    intended_origin=canon_origin,
                )
                if readback_status == "CONFIRMED_SUCCESS":
                    self.registry.confirm_edit(record.id, updated_name)
                    record.entity_name = updated_name
                    return ScopedProfileWorkflowResult(
                        workflow_type="edit",
                        verdict="FAIL",
                        entity_id=record.remote_entity_id,
                        entity_name=updated_name,
                        action_type="edit_profile_item",
                        details=f"Server returned HTTP {error_status} on edit, but entity was confirmed updated to '{updated_name}' in server storage upon reload.",
                        cleanup_status="PENDING",
                        is_customer_defect=True,
                        defect_category="SERVER_ERROR_ON_EDIT",
                        viewport_name=viewport_name,
                    )
                elif readback_status == "CONFIRMED_FAILURE":
                    return ScopedProfileWorkflowResult(
                        workflow_type="edit",
                        verdict="FAIL",
                        entity_id=record.remote_entity_id,
                        entity_name=record.entity_name,
                        action_type="edit_profile_item",
                        details=f"Edit saved, but server rejected update with HTTP {error_status}.",
                        cleanup_status="PENDING",
                        is_customer_defect=True,
                        defect_category="PERSISTENCE_UPDATE_FAILED",
                        viewport_name=viewport_name,
                    )
                else:  # UNCERTAIN_READBACK
                    return ScopedProfileWorkflowResult(
                        workflow_type="edit",
                        verdict="LIMITED_COVERAGE",
                        entity_id=record.remote_entity_id,
                        entity_name=record.entity_name,
                        action_type="edit_profile_item",
                        details=f"Edit saved with server HTTP {error_status}, but read-back was uncertain ({reason}). Entity remains registered for recovery cleanup.",
                        cleanup_status="PENDING",
                        is_customer_defect=False,
                        viewport_name=viewport_name,
                    )

            # Verify DOM updated
            is_updated = await page.evaluate("text => document.body.textContent.includes(text)", updated_name)

            # Authoritative authenticated reload & read-back
            readback_status, reason, diag = await self._verify_authenticated_scope_readback(
                page=page,
                expected_scope_selector="#section-experience-management, #profile-experience-container, [data-section='experience']",
                required_element_text=updated_name,
                target_entity_id=record.remote_entity_id,
                expected_account=account_context,
                intended_origin=canon_origin,
            )

            shot_path = None
            if screenshot_fn:
                shot_path = await screenshot_fn(page, f"edit_success_{viewport_name}", "MUT-002")

            if readback_status == "CONFIRMED_SUCCESS":
                self.registry.confirm_edit(record.id, updated_name)
                record.entity_name = updated_name
                return ScopedProfileWorkflowResult(
                    workflow_type="edit",
                    verdict="PASS",
                    entity_id=record.remote_entity_id,
                    entity_name=updated_name,
                    action_type="edit_profile_item",
                    details=f"Successfully edited test entity to '{updated_name}', confirmed DOM reflection and verified upon authenticated page reload.",
                    screenshot_path=shot_path,
                    cleanup_status="PENDING",
                    viewport_name=viewport_name,
                )
            elif readback_status == "CONFIRMED_FAILURE":
                return ScopedProfileWorkflowResult(
                    workflow_type="edit",
                    verdict="FAIL",
                    entity_id=record.remote_entity_id,
                    entity_name=record.entity_name,
                    action_type="edit_profile_item",
                    details=f"Edit saved, but updated text '{updated_name}' was not reflected upon authenticated reload: {reason}",
                    screenshot_path=shot_path,
                    cleanup_status="PENDING",
                    is_customer_defect=True,
                    defect_category="PERSISTENCE_UPDATE_FAILED",
                    viewport_name=viewport_name,
                )
            else:  # UNCERTAIN_READBACK
                return ScopedProfileWorkflowResult(
                    workflow_type="edit",
                    verdict="LIMITED_COVERAGE",
                    entity_id=record.remote_entity_id,
                    entity_name=record.entity_name,
                    action_type="edit_profile_item",
                    details=f"Edit action executed, but post-edit read-back was uncertain ({reason}).",
                    screenshot_path=shot_path,
                    cleanup_status="PENDING",
                    is_customer_defect=False,
                    viewport_name=viewport_name,
                )
        except Exception as e:
            return ScopedProfileWorkflowResult(
                workflow_type="edit",
                verdict="LIMITED_COVERAGE" if any(w in str(e).lower() for w in ("reload", "timeout", "navigation")) else "FAIL",
                entity_id=record.remote_entity_id,
                entity_name=record.entity_name,
                action_type="edit_profile_item",
                details=f"Edit workflow failed due to exception: {e}",
                viewport_name=viewport_name,
                is_customer_defect=False,
            )

    async def _exercise_upload_workflow(
        self,
        page: Any,
        target_url: str,
        run_id: int,
        project_id: int,
        run_prefix: str,
        viewport_name: str,
        account_context: Optional[str],
        cancellation_token: Optional[CancellationToken],
        screenshot_fn: Optional[Callable[[Any, str, str], Any]],
    ) -> Optional[ScopedProfileWorkflowResult]:
        """Autonomously upload harmless test file and execute verified remote deletion."""
        file_path, file_name = self.registry.generate_harmless_file(self.scratch_dir, run_id, "txt")

        # Step 1: Record intent in SQLite journal
        intent_record = self.registry.record_creation_intent(
            run_id=run_id,
            project_id=project_id,
            target_url=target_url,
            entity_type="profile_attachment",
            run_prefix=run_prefix,
            proposed_name=file_name,
            account_context=account_context,
            details={"local_path": file_path, "filename": file_name},
        )

        try:
            if cancellation_token and cancellation_token.is_cancelled():
                self.registry.mark_not_dispatched(intent_record.id)
                return ScopedProfileWorkflowResult(
                    workflow_type="upload",
                    verdict="LIMITED_COVERAGE",
                    entity_id=None,
                    entity_name=file_name,
                    action_type="upload_harmless_file",
                    details="Upload workflow cancelled before file selection or dispatch.",
                    cleanup_status="NOT_APPLICABLE",
                    is_customer_defect=False,
                    viewport_name=viewport_name,
                )

            # Step 2: Discover verified attachment container (strictly avoiding avatars/user photos)
            container = await ProfileWorkflowScope.discover_attachment_container(page)
            if not container:
                self.registry.mark_ambiguous_creation(intent_record.id, "No verified attachment container found")
                return ScopedProfileWorkflowResult(
                    workflow_type="upload",
                    verdict="LIMITED_COVERAGE",
                    entity_id=None,
                    entity_name=file_name,
                    action_type="upload_harmless_file",
                    details="No verified isolated attachment container found on active page (avatars preserved).",
                    viewport_name=viewport_name,
                )

            file_input = container.locator("#input-experience-file, #attachment-file-input, input[type='file']").first
            if await file_input.count() == 0 or not await ProfileWorkflowScope.is_safe_attachment_input(file_input):
                self.registry.mark_ambiguous_creation(intent_record.id, "Safe file input not found in container")
                return ScopedProfileWorkflowResult(
                    workflow_type="upload",
                    verdict="LIMITED_COVERAGE",
                    entity_id=None,
                    entity_name=file_name,
                    action_type="upload_harmless_file",
                    details="Safe attachment file input not found in container.",
                    viewport_name=viewport_name,
                )

            # Compute SHA-256 digest and file size for upload binding contract from the
            # exact bytes handed to the browser.
            with open(file_path, "rb") as f:
                file_bytes = f.read()
            file_size = len(file_bytes)
            approved_file_hash = hashlib.sha256(file_bytes).hexdigest()
            # Declarative target contract: the exact upload path. Without it the CDP body
            # verification is not used and the upload stays blocked as opaque.
            upload_destination = (await file_input.get_attribute("data-qa-upload-destination")
                                  or await container.get_attribute("data-qa-upload-destination") or "").strip()
            if not upload_destination:
                try:
                    form_action = await file_input.evaluate(
                        "el => el.form ? el.form.getAttribute('action') : null"
                    )
                    upload_destination = discover_attachment_form_destination(form_action)
                except Exception:
                    upload_destination = None
            if (not upload_destination
                    or not re.fullmatch(r"/[A-Za-z0-9._~/-]*", upload_destination)
                    or "//" in upload_destination
                    or "/.." in upload_destination):
                upload_destination = None

            if upload_destination is None:
                self.registry.mark_not_dispatched(intent_record.id)
                return ScopedProfileWorkflowResult(
                    workflow_type="upload",
                    verdict="LIMITED_COVERAGE",
                    entity_id=None,
                    entity_name=file_name,
                    action_type="upload_harmless_file",
                    details="Unsupported upload destination: no approved attachment target.",
                    cleanup_status="NOT_APPLICABLE",
                    is_customer_defect=False,
                    viewport_name=viewport_name,
                )

            await file_input.set_input_files({
                "name": file_name,
                "mimeType": "text/plain",
                "buffer": file_bytes,
            })
            await page.wait_for_timeout(300)

            btn_upload = container.locator("#btn-upload-file, button:has-text('Upload')").first

            eff_acct = account_context or (self.barrier.verified_session_identity if self.barrier else None)
            if not eff_acct:
                eff_acct = await AuthenticatedIdentityDiscovery.discover_session_identity(page, get_canonical_origin(target_url or getattr(page, "url", "")))
            if eff_acct and self.barrier:
                self.barrier.set_verified_session_identity(eff_acct)

            eff_endpoint_pattern = (
                rf"^{re.escape(upload_destination)}/?$"
                if upload_destination
                else r"^/api/(?:profile/)?attachments(?:/upload)?/?$"
            )

            # Step 3: Issue upload under UPLOAD_ATTACHMENT permit with exact file contract
            async with self._permit(
                operation_type=OperationType.UPLOAD_ATTACHMENT,
                workflow_name="profile_attachment",
                allowed_methods=("POST", "PUT"),
                endpoint_pattern=eff_endpoint_pattern,
                run_tag=run_prefix,
                account_context=eff_acct,
                verified_account=eff_acct,
                entity_type="profile_attachment",
                project_id=project_id,
                approved_file_path=file_path,
                approved_file_name=file_name,
                approved_file_hash=approved_file_hash,
                approved_file_size=file_size,
                upload_destination=upload_destination,
                designated_identity_field="name",
            ) as upload_permit:
                if cancellation_token and cancellation_token.is_cancelled():
                    self.registry.mark_not_dispatched(intent_record.id)
                    return ScopedProfileWorkflowResult(
                        workflow_type="upload",
                        verdict="LIMITED_COVERAGE",
                        entity_id=None,
                        entity_name=file_name,
                        action_type="upload_harmless_file",
                        details="Upload workflow cancelled before dispatch.",
                        cleanup_status="NOT_APPLICABLE",
                        is_customer_defect=False,
                        viewport_name=viewport_name,
                    )
                if await btn_upload.count() > 0 and await btn_upload.is_visible():
                    await btn_upload.click()
                await page.wait_for_timeout(500)

            # Check server response evidence
            server_failed = False
            error_status = 0
            if upload_permit and upload_permit.recorded_responses:
                for resp in upload_permit.recorded_responses:
                    if resp.get("status", 200) >= 400:
                        server_failed = True
                        error_status = resp.get("status")
                        break

            if server_failed:
                is_uncertain = (error_status in UNCERTAIN_SERVER_STATUSES or error_status == 0)
                # Do not adopt an ID from transient DOM after a failed upload response.
                disc_att_id = None

                self.registry.mark_ambiguous_creation(
                    intent_record.id,
                    f"Server returned {'uncertain ' if is_uncertain else ''}HTTP {error_status} during upload",
                    remote_entity_id=disc_att_id,
                )
                return ScopedProfileWorkflowResult(
                    workflow_type="upload",
                    verdict="LIMITED_COVERAGE" if is_uncertain else "FAIL",
                    entity_id=disc_att_id or file_name,
                    entity_name=file_name,
                    action_type="upload_harmless_file",
                    details=(
                        f"File '{file_name}' attached, but server returned uncertain status HTTP {error_status} (transient gateway/availability/timeout issue)."
                        if is_uncertain else
                        (
                            f"Server returned HTTP {error_status} during upload, but file '{file_name}' was associated in DOM (Attachment ID: {disc_att_id})."
                            if disc_att_id else
                            f"File '{file_name}' attached, but server rejected upload with HTTP {error_status}."
                        )
                    ),
                    cleanup_status="PENDING",
                    is_customer_defect=not is_uncertain,
                    defect_category=None if is_uncertain else "UPLOAD_ASSOCIATION_FAILED",
                    viewport_name=viewport_name,
                )

            if upload_permit and upload_permit.use_count == 0:
                # Barrier rejected the upload (e.g. opaque streamed request without inspectable payload).
                # This is an engine safety boundary, not a customer application defect.
                self.registry.mark_not_dispatched(intent_record.id)
                if os.path.exists(file_path):
                    try:
                        os.remove(file_path)
                    except Exception:
                        pass
                return ScopedProfileWorkflowResult(
                    workflow_type="upload",
                    verdict="LIMITED_COVERAGE",
                    entity_id=file_name,
                    entity_name=file_name,
                    action_type="upload_harmless_file",
                    details=f"Upload of '{file_name}' was blocked by network barrier (opaque/streamed browser uploads are unsupported without inspectable content digest).",
                    cleanup_status="NOT_APPLICABLE",
                    is_customer_defect=False,
                    viewport_name=viewport_name,
                )

            # Step 4: Authoritative read-back after reload. Optimistic client feedback is not
            # creation evidence; the server-rendered attachment scope must list the file.
            attachment_scope = "#section-experience-management, #section-attachment-management, [data-section='attachments']"
            feedback_text = ""
            canon_origin = get_canonical_origin(target_url or getattr(page, "url", ""))
            create_status, create_reason, _ = await self._verify_authenticated_scope_readback(
                page=page,
                expected_scope_selector=attachment_scope,
                required_element_text=file_name,
                expected_account=eff_acct,
                intended_origin=canon_origin,
            )
            ids = []
            if create_status == "CONFIRMED_SUCCESS":
                ids = await page.evaluate(
                    """([sel, fname]) => {
                        const scope = document.querySelector(sel);
                        if (!scope) return [];
                        return Array.from(scope.querySelectorAll('[data-attachment-id]'))
                            .filter(el => (el.textContent || '').includes(fname))
                            .map(el => el.getAttribute('data-attachment-id'));
                    }""",
                    [attachment_scope, file_name],
                )
            if create_status != "CONFIRMED_SUCCESS" or len(set(ids)) != 1 or not ids[0]:
                reason = create_reason if create_status != "CONFIRMED_SUCCESS" else f"expected exactly one server-rendered attachment ID, found {ids}"
                self.registry.mark_ambiguous_creation(intent_record.id, f"Upload creation read-back not confirmed: {reason}")
                confirmed_missing = create_status == "CONFIRMED_FAILURE"
                return ScopedProfileWorkflowResult(
                    workflow_type="upload",
                    verdict="FAIL" if confirmed_missing else "LIMITED_COVERAGE",
                    entity_id=None,
                    entity_name=file_name,
                    action_type="upload_harmless_file",
                    details=f"Verified upload of '{file_name}' was dispatched, but creation read-back was not confirmed: {reason}",
                    cleanup_status="PENDING",
                    is_customer_defect=confirmed_missing,
                    defect_category="UPLOAD_ASSOCIATION_FAILED" if confirmed_missing else None,
                    viewport_name=viewport_name,
                )
            remote_att_id = ids[0]

            # Confirm creation in journal: remote_cleaned=0, local_cleaned=0
            self.registry.confirm_creation(
                record_id=intent_record.id,
                remote_entity_id=remote_att_id,
                confirmed_name=file_name,
                details={"local_path": file_path, "feedback": feedback_text},
            )
            intent_record.remote_entity_id = remote_att_id
            intent_record.entity_id = remote_att_id
            intent_record.status = "CONFIRMED_CREATED"

            shot_path = None
            if screenshot_fn:
                shot_path = await screenshot_fn(page, f"upload_success_{viewport_name}", "MUT-003")

            if cancellation_token and cancellation_token.is_cancelled():
                return ScopedProfileWorkflowResult(
                    workflow_type="upload",
                    verdict="LIMITED_COVERAGE",
                    entity_id=remote_att_id,
                    entity_name=file_name,
                    action_type="upload_harmless_file",
                    details=f"Uploaded harmless test file '{file_name}' (ID: {remote_att_id}), but run was cancelled before remote deletion. Entity preserved for recovery cleanup.",
                    screenshot_path=shot_path,
                    cleanup_status="PENDING",
                    is_customer_defect=False,
                    viewport_name=viewport_name,
                )

            # Step 5: Execute GENUINE REMOTE DELETION of uploaded attachment
            remote_deleted = False
            del_server_failed = False
            del_error_status = 0
            del_readback_reason = ""
            readback_status = None
            is_uncertain_status = False

            item_loc = container.locator(
                f"[id={json.dumps(str(remote_att_id))}], [data-id={json.dumps(str(remote_att_id))}], [data-attachment-id={json.dumps(str(remote_att_id))}]"
            ).first
            del_btn = None
            if await item_loc.count() > 0:
                btn_candidate = item_loc.locator(
                    f"[data-delete-attachment={json.dumps(str(remote_att_id))}], button[data-filename={json.dumps(str(file_name))}], button:has-text('Delete'), button:has-text('Remove')"
                ).first
                if await btn_candidate.count() > 0 and await btn_candidate.is_visible():
                    del_btn = btn_candidate

            if del_btn is None:
                btn_fallback = container.locator(
                    f"[data-delete-attachment={json.dumps(str(remote_att_id))}], button[data-filename={json.dumps(str(file_name))}]"
                ).first
                if await btn_fallback.count() > 0 and await btn_fallback.is_visible():
                    del_btn = btn_fallback

            if del_btn is None or await del_btn.count() == 0 or not await del_btn.is_visible():
                # Step 6: Local temporary scratch file cleanup (tracked separately)
                local_cleaned = False
                try:
                    if os.path.exists(file_path):
                        os.remove(file_path)
                    self.registry.mark_local_cleaned(intent_record.id)
                    intent_record.local_cleaned = True
                    local_cleaned = True
                except Exception:
                    pass

                return ScopedProfileWorkflowResult(
                    workflow_type="upload",
                    verdict="LIMITED_COVERAGE",
                    entity_id=remote_att_id,
                    entity_name=file_name,
                    action_type="upload_harmless_file",
                    details=(
                        f"Uploaded harmless test file '{file_name}' ({file_size} bytes, sha256 {approved_file_hash}) "
                        f"to {upload_destination}; creation read back as '{remote_att_id}'. Remote UI does not provide an "
                        f"actionable attachment delete control. Entity remains registered for recovery cleanup."
                    ),
                    screenshot_path=shot_path,
                    cleanup_status="PENDING",
                    is_customer_defect=False,
                    defect_category=None,
                    viewport_name=viewport_name,
                )

            if cancellation_token and cancellation_token.is_cancelled():
                return ScopedProfileWorkflowResult(
                    workflow_type="upload",
                    verdict="LIMITED_COVERAGE",
                    entity_id=remote_att_id,
                    entity_name=file_name,
                    action_type="upload_harmless_file",
                    details=f"Uploaded harmless test file '{file_name}' (ID: {remote_att_id}), but run was cancelled before delete dispatch.",
                    screenshot_path=shot_path,
                    cleanup_status="PENDING",
                    is_customer_defect=False,
                    viewport_name=viewport_name,
                )
            async with self._permit(
                operation_type=OperationType.DELETE_ATTACHMENT,
                workflow_name="profile_attachment",
                allowed_methods=("DELETE", "POST"),
                endpoint_pattern=rf"^/api/(?:profile/)?attachments/{re.escape(remote_att_id)}/?$",
                resource_id=remote_att_id,
                account_context=eff_acct,
                verified_account=eff_acct,
                entity_type="profile_attachment",
                project_id=project_id,
            ) as del_att_permit:
                await del_btn.click()
                await page.wait_for_timeout(500)

            # Check response evidence
            if del_att_permit and del_att_permit.recorded_responses:
                for resp in del_att_permit.recorded_responses:
                    if resp.get("status", 200) >= 400:
                        del_server_failed = True
                        del_error_status = resp.get("status", 500)
                        break

            # Confirm remote absence in current DOM
            is_still_in_dom = await page.evaluate("fname => document.body.textContent.includes(fname)", file_name)

            # Authoritative reload read-back verification for attachment deletion
            readback_status, reason, diag = await self._verify_authenticated_scope_readback(
                page=page,
                expected_scope_selector="#section-experience-management, #section-attachment-management, [data-section='attachments']",
                forbidden_element_text=file_name,
                target_entity_id=remote_att_id,
                expected_account=eff_acct,
                intended_origin=canon_origin,
            )
            del_readback_reason = reason

            is_uncertain_status = del_server_failed and (del_error_status in UNCERTAIN_SERVER_STATUSES or del_error_status == 0)
            if is_uncertain_status:
                self.registry.mark_ambiguous_creation(
                    intent_record.id,
                    f"Server returned uncertain HTTP {del_error_status} during attachment deletion",
                )
            elif readback_status == "CONFIRMED_SUCCESS":
                self.registry.confirm_remote_deletion(intent_record.id)
                intent_record.remote_cleaned = True
                remote_deleted = True
            elif readback_status == "CONFIRMED_FAILURE":
                err_msg = f"Server rejected attachment deletion with HTTP {del_error_status}" if del_server_failed else f"Attachment remains present after delete action: {reason}"
                self.registry.mark_cleanup_failed(intent_record.id, err_msg)
            else:  # UNCERTAIN_READBACK
                self.registry.mark_ambiguous_creation(
                    intent_record.id,
                    f"Attachment deletion read-back uncertain{' with HTTP ' + str(del_error_status) if del_server_failed else ''}: {reason}",
                )

            # Step 6: Local temporary scratch file cleanup (tracked separately)
            local_cleaned = False
            try:
                if os.path.exists(file_path):
                    os.remove(file_path)
                self.registry.mark_local_cleaned(intent_record.id)
                intent_record.local_cleaned = True
                local_cleaned = True
            except Exception:
                pass

            # Cleanup status requires genuine remote cleanup
            if remote_deleted and local_cleaned:
                cleanup_status = "CLEANED_UP"
            elif remote_deleted and not local_cleaned:
                cleanup_status = "PENDING"
            else:
                cleanup_status = "PENDING"  # Remote upload remains on server

            # Case 1: Uncertain status code (e.g. 502, 503, 504, 408, 429, 401, 403, 0)
            if is_uncertain_status:
                return ScopedProfileWorkflowResult(
                    workflow_type="upload",
                    verdict="LIMITED_COVERAGE",
                    entity_id=remote_att_id,
                    entity_name=file_name,
                    action_type="upload_harmless_file",
                    details=(
                        f"Uploaded harmless test file '{file_name}' ({file_size} bytes, sha256 {approved_file_hash}) "
                        f"to {upload_destination}; creation read back as '{remote_att_id}', but remote deletion returned "
                        f"uncertain status HTTP {del_error_status}."
                    ),
                    screenshot_path=shot_path,
                    cleanup_status="PENDING",
                    is_customer_defect=False,
                    defect_category=None,
                    viewport_name=viewport_name,
                )

            # Case 2: Readback was uncertain (even under HTTP 500) -> NEVER reported as confirmed customer defect!
            if readback_status == "UNCERTAIN_READBACK" and (del_server_failed or del_btn is not None):
                return ScopedProfileWorkflowResult(
                    workflow_type="upload",
                    verdict="LIMITED_COVERAGE",
                    entity_id=remote_att_id,
                    entity_name=file_name,
                    action_type="upload_harmless_file",
                    details=(
                        f"Uploaded harmless test file '{file_name}' ({file_size} bytes, sha256 {approved_file_hash}) "
                        f"to {upload_destination}; creation read back as '{remote_att_id}'. Post-deletion read-back was uncertain "
                        f"({del_readback_reason}). Entity remains registered for recovery cleanup."
                    ),
                    screenshot_path=shot_path,
                    cleanup_status="PENDING",
                    is_customer_defect=False,
                    defect_category=None,
                    viewport_name=viewport_name,
                )

            # Case 3: Server returned error (e.g. HTTP 500) but reload readback proves the attachment was actually deleted!
            if del_server_failed and remote_deleted:
                return ScopedProfileWorkflowResult(
                    workflow_type="upload",
                    verdict="FAIL",
                    entity_id=remote_att_id,
                    entity_name=file_name,
                    action_type="upload_harmless_file",
                    details=(
                        f"Uploaded harmless test file '{file_name}' ({file_size} bytes, sha256 {approved_file_hash}) "
                        f"to {upload_destination}; creation read back as '{remote_att_id}'. Server returned HTTP {del_error_status} "
                        f"on delete, but attachment was confirmed removed from server storage upon reload."
                    ),
                    screenshot_path=shot_path,
                    cleanup_status=cleanup_status,
                    is_customer_defect=True,
                    defect_category="SERVER_ERROR_ON_DELETE",
                    viewport_name=viewport_name,
                )

            # Case 4: Confirmed server defect: server rejected delete or item confirmed still present
            if del_server_failed or (del_btn is not None and not remote_deleted and readback_status == "CONFIRMED_FAILURE"):
                return ScopedProfileWorkflowResult(
                    workflow_type="upload",
                    verdict="FAIL",
                    entity_id=remote_att_id,
                    entity_name=file_name,
                    action_type="upload_harmless_file",
                    details=(
                        f"Uploaded harmless test file '{file_name}' ({file_size} bytes, sha256 {approved_file_hash}) "
                        f"to {upload_destination}; creation read back as '{remote_att_id}', but remote deletion failed: "
                        f"{'Server returned HTTP ' + str(del_error_status) if del_server_failed else del_readback_reason}."
                    ),
                    screenshot_path=shot_path,
                    cleanup_status="PENDING",
                    is_customer_defect=True,
                    defect_category="DELETION_EXECUTION_FAILED",
                    viewport_name=viewport_name,
                )

            return ScopedProfileWorkflowResult(
                workflow_type="upload",
                # Creation was read back with an exact ID; only verified deletion completes it.
                verdict="PASS" if remote_deleted else "LIMITED_COVERAGE",
                entity_id=remote_att_id,
                entity_name=file_name,
                action_type="upload_harmless_file",
                details=(
                    f"Uploaded harmless test file '{file_name}' ({file_size} bytes, sha256 {approved_file_hash}) "
                    f"to {upload_destination}; creation read back as '{remote_att_id}'. "
                    f"Remote cleanup: {'CONFIRMED_DELETED' if remote_deleted else 'PENDING_REMOTE_CLEANUP'}; "
                    f"Local file: {'REMOVED' if local_cleaned else 'RETAINED'}."
                ),
                screenshot_path=shot_path,
                cleanup_status=cleanup_status,
                viewport_name=viewport_name,
            )
        except Exception as e:
            if intent_record.remote_entity_id:
                self.registry.mark_cleanup_failed(intent_record.id, f"Upload workflow exception: {e}")
            else:
                self.registry.mark_ambiguous_creation(intent_record.id, f"Upload workflow exception: {e}")
            return ScopedProfileWorkflowResult(
                workflow_type="upload",
                verdict="LIMITED_COVERAGE" if any(w in str(e).lower() for w in ("reload", "timeout", "navigation", "cancel")) else "FAIL",
                entity_id=intent_record.remote_entity_id or file_name,
                entity_name=file_name,
                action_type="upload_harmless_file",
                details=f"File upload failed due to exception: {e}",
                cleanup_status="PENDING",
                is_customer_defect=False,
                viewport_name=viewport_name,
            )
        finally:
            # Local disposal is independent of remote outcome, including early returns.
            try:
                if os.path.exists(file_path):
                    os.remove(file_path)
                self.registry.mark_local_cleaned(intent_record.id)
            except OSError:
                pass

    async def _exercise_delete_workflow(
        self,
        page: Any,
        run_id: int,
        project_id: int,
        record: ScopedOwnershipRecord,
        viewport_name: str = "Desktop",
        target_url: Optional[str] = None,
        account_context: Optional[str] = None,
        cancellation_token: Optional[CancellationToken] = None,
        screenshot_fn: Optional[Callable[[Any, str, str], Any]] = None,
    ) -> Optional[ScopedProfileWorkflowResult]:
        """Perform verified deletion of run-owned test entity under permit."""
        target_url = target_url or record.target_url
        canon_origin = get_canonical_origin(target_url or (page.url if hasattr(page, "url") else ""))
        # Safety gate: verify strict run ownership and destructive authority
        if not self.registry.is_owned_by_run(
            run_id=run_id,
            project_id=project_id,
            target_url=target_url,
            remote_entity_id=record.remote_entity_id,
            entity_name=record.entity_name,
            account_context=account_context,
            entity_type=record.entity_type,
            require_destructive_authority=True,
        ):
            return ScopedProfileWorkflowResult(
                workflow_type="delete",
                verdict="FAIL",
                entity_id=record.remote_entity_id,
                entity_name=record.entity_name,
                action_type="delete_test_item",
                details=f"Ownership boundary violation (destructive authority): Entity '{record.entity_name}' is not authorized for deletion by run {run_id}.",
                cleanup_status="FAILED",
                viewport_name=viewport_name,
            )

        try:
            container = None
            if record.remote_entity_id:
                c_loc = page.locator(f"[id={json.dumps(str(record.remote_entity_id))}], [data-id={json.dumps(str(record.remote_entity_id))}]").first
                if await c_loc.count() > 0 and await c_loc.is_visible():
                    container = c_loc

            if not container:
                c_loc = page.locator(".exp-item, [data-id]").filter(has_text=record.entity_name).first
                if await c_loc.count() > 0:
                    container = c_loc

            if not container or await container.count() == 0:
                return ScopedProfileWorkflowResult(
                    workflow_type="delete",
                    verdict="LIMITED_COVERAGE",
                    entity_id=record.remote_entity_id,
                    entity_name=record.entity_name,
                    action_type="delete_test_item",
                    details=f"Test entity '{record.entity_name}' was not found in page DOM for deletion.",
                    cleanup_status="PENDING",
                    viewport_name=viewport_name,
                )

            # Item-specific delete control strictly scoped to the verified entity container
            btn_del = container.locator(
                f"[id={json.dumps(f'btn-delete-{record.remote_entity_id}')}], button[data-delete={json.dumps(str(record.remote_entity_id))}], button:has-text('Delete'), button:has-text('Remove')"
            ).first

            if await btn_del.count() == 0 or not await btn_del.is_visible():
                return ScopedProfileWorkflowResult(
                    workflow_type="delete",
                    verdict="LIMITED_COVERAGE",
                    entity_id=record.remote_entity_id,
                    entity_name=record.entity_name,
                    action_type="delete_test_item",
                    details=f"Delete control for test entity '{record.entity_name}' was not found or visible within verified container.",
                    cleanup_status="PENDING",
                    viewport_name=viewport_name,
                )

            # Acquire permit for delete with exact resource contract
            async with self._permit(
                operation_type=OperationType.DELETE_ENTITY,
                workflow_name="profile_experience",
                allowed_methods=("DELETE", "POST"),
                endpoint_pattern=rf"^/api/(?:profile/)?experience/{re.escape(record.remote_entity_id)}/?$",
                resource_id=record.remote_entity_id,
                account_context=account_context,
                verified_account=account_context,
                entity_type=record.entity_type,
                project_id=project_id,
            ) as delete_permit:
                await btn_del.click()
                await page.wait_for_timeout(300)

                # Confirm modal if present
                btn_confirm = page.locator(
                    "#btn-confirm-delete, dialog[open] button:has-text('Delete Permanently'), dialog[open] button:has-text('Confirm'), dialog[open] button:has-text('Yes')"
                ).first
                if await btn_confirm.count() > 0 and await btn_confirm.is_visible():
                    await btn_confirm.click()
                    await page.wait_for_timeout(500)

            # Check 1: Authoritative network response evidence under delete_permit
            server_failed = False
            error_status = 0
            if delete_permit and delete_permit.recorded_responses:
                for resp in delete_permit.recorded_responses:
                    if resp.get("status", 200) >= 400:
                        server_failed = True
                        error_status = resp.get("status")
                        break

            # Check 2: DOM absence in current page
            dom_absent = (await page.get_by_text(record.entity_name).count() == 0)

            # If server explicitly failed, verify actual persistence via authenticated reload read-back!
            if server_failed:
                is_uncertain = (error_status in UNCERTAIN_SERVER_STATUSES or error_status == 0)
                if is_uncertain:
                    self.registry.mark_ambiguous_creation(record.id, f"Server returned uncertain HTTP {error_status} during deletion")
                    return ScopedProfileWorkflowResult(
                        workflow_type="delete",
                        verdict="LIMITED_COVERAGE",
                        entity_id=record.remote_entity_id,
                        entity_name=record.entity_name,
                        action_type="delete_test_item",
                        details=f"Delete action executed, but server returned uncertain status HTTP {error_status}. Entity remains registered for recovery cleanup.",
                        cleanup_status="PENDING",
                        is_customer_defect=False,
                        viewport_name=viewport_name,
                    )

                # Server returned HTTP 500 (or non-transient error).
                # Do NOT guess whether optimistic DOM removal was an illusion!
                # Verify actual persistence on server via authenticated reload read-back!
                readback_status, reason, _ = await self._verify_authenticated_scope_readback(
                    page=page,
                    expected_scope_selector="#section-experience-management, #profile-experience-container, [data-section='experience']",
                    forbidden_element_text=record.entity_name,
                    target_entity_id=record.remote_entity_id,
                    expected_account=account_context,
                    intended_origin=canon_origin,
                )
                if readback_status == "CONFIRMED_SUCCESS":
                    self.registry.confirm_remote_deletion(record.id)
                    record.remote_cleaned = True
                    record.status = "CONFIRMED_DELETED"
                    return ScopedProfileWorkflowResult(
                        workflow_type="delete",
                        verdict="FAIL",
                        entity_id=record.remote_entity_id,
                        entity_name=record.entity_name,
                        action_type="delete_test_item",
                        details=f"Server returned HTTP {error_status} on delete, but entity '{record.entity_name}' was confirmed deleted from server storage upon reload.",
                        cleanup_status="CLEANED_UP",
                        is_customer_defect=True,
                        defect_category="SERVER_ERROR_ON_DELETE",
                        viewport_name=viewport_name,
                    )
                elif readback_status == "CONFIRMED_FAILURE":
                    self.registry.mark_cleanup_failed(record.id, f"Server rejected deletion with HTTP {error_status}")
                    return ScopedProfileWorkflowResult(
                        workflow_type="delete",
                        verdict="FAIL",
                        entity_id=record.remote_entity_id,
                        entity_name=record.entity_name,
                        action_type="delete_test_item",
                        details=f"Delete action executed and removed element from DOM optimistically, but server rejected deletion with HTTP {error_status} and item reappeared upon authenticated reload ({reason}).",
                        cleanup_status="FAILED",
                        is_customer_defect=True,
                        defect_category="OPTIMISTIC_UI_SERVER_REJECTION",
                        viewport_name=viewport_name,
                    )
                else:  # UNCERTAIN_READBACK
                    self.registry.mark_ambiguous_creation(record.id, f"Server returned HTTP {error_status} and read-back was uncertain: {reason}")
                    return ScopedProfileWorkflowResult(
                        workflow_type="delete",
                        verdict="LIMITED_COVERAGE",
                        entity_id=record.remote_entity_id,
                        entity_name=record.entity_name,
                        action_type="delete_test_item",
                        details=f"Delete action executed with server HTTP {error_status}, but post-deletion read-back was uncertain ({reason}). Entity remains registered for recovery cleanup.",
                        cleanup_status="PENDING",
                        is_customer_defect=False,
                        viewport_name=viewport_name,
                    )

            # Check 3: Authoritative authenticated reload & read-back verification
            readback_status, reason, diag = await self._verify_authenticated_scope_readback(
                page=page,
                expected_scope_selector="#section-experience-management, #profile-experience-container, [data-section='experience']",
                forbidden_element_text=record.entity_name,
                target_entity_id=record.remote_entity_id,
                expected_account=account_context,
                intended_origin=canon_origin,
            )

            shot_path = None
            if screenshot_fn:
                shot_path = await screenshot_fn(page, f"delete_success_{viewport_name}", "MUT-004")

            if readback_status == "CONFIRMED_SUCCESS" and dom_absent:
                self.registry.confirm_remote_deletion(record.id)
                record.remote_cleaned = True
                record.status = "CONFIRMED_DELETED"
                return ScopedProfileWorkflowResult(
                    workflow_type="delete",
                    verdict="PASS",
                    entity_id=record.remote_entity_id,
                    entity_name=record.entity_name,
                    action_type="delete_test_item",
                    details=f"Successfully deleted run-owned test entity '{record.entity_name}' (ID: {record.remote_entity_id}), confirmed absence in DOM and verified upon authenticated page reload.",
                    screenshot_path=shot_path,
                    cleanup_status="CLEANED_UP",
                    viewport_name=viewport_name,
                )
            elif readback_status == "CONFIRMED_FAILURE":
                self.registry.mark_cleanup_failed(record.id, f"Entity reappeared in DOM after reload: {reason}")
                return ScopedProfileWorkflowResult(
                    workflow_type="delete",
                    verdict="FAIL",
                    entity_id=record.remote_entity_id,
                    entity_name=record.entity_name,
                    action_type="delete_test_item",
                    details=f"Delete action removed item from DOM, but item '{record.entity_name}' reappeared after page reload ({reason}).",
                    screenshot_path=shot_path,
                    cleanup_status="FAILED",
                    is_customer_defect=True,
                    defect_category="DELETION_PERSISTENCE_FAILED",
                    viewport_name=viewport_name,
                )
            else:  # UNCERTAIN_READBACK (failed reload, login redirect, error page, unrendered section)
                self.registry.mark_ambiguous_creation(record.id, f"Post-deletion read-back was uncertain: {reason}")
                return ScopedProfileWorkflowResult(
                    workflow_type="delete",
                    verdict="LIMITED_COVERAGE",
                    entity_id=record.remote_entity_id,
                    entity_name=record.entity_name,
                    action_type="delete_test_item",
                    details=f"Delete action executed, but post-deletion read-back was uncertain ({reason}). Entity remains registered for recovery cleanup.",
                    screenshot_path=shot_path,
                    cleanup_status="PENDING",
                    is_customer_defect=False,
                    viewport_name=viewport_name,
                )
        except Exception as e:
            is_transient = any(w in str(e).lower() for w in ("reload", "timeout", "navigation", "closed", "reset", "abort"))
            if is_transient:
                self.registry.mark_ambiguous_creation(record.id, f"Delete workflow transient error: {e}")
                return ScopedProfileWorkflowResult(
                    workflow_type="delete",
                    verdict="LIMITED_COVERAGE",
                    entity_id=record.remote_entity_id,
                    entity_name=record.entity_name,
                    action_type="delete_test_item",
                    details=f"Delete workflow encountered transient failure ({e}); entity preserved for recovery.",
                    cleanup_status="PENDING",
                    is_customer_defect=False,
                    viewport_name=viewport_name,
                )
            self.registry.mark_cleanup_failed(record.id, f"Delete workflow exception: {e}")
            return ScopedProfileWorkflowResult(
                workflow_type="delete",
                verdict="FAIL",
                entity_id=record.remote_entity_id,
                entity_name=record.entity_name,
                action_type="delete_test_item",
                details=f"Deletion failed due to exception: {e}",
                cleanup_status="FAILED",
                is_customer_defect=False,
                viewport_name=viewport_name,
            )

    async def _cleanup_run_entities(
        self,
        page: Any,
        run_id: int,
        project_id: int,
        viewport_name: str = "Desktop",
        target_url: Optional[str] = None,
        account_context: Optional[str] = None,
        screenshot_fn: Optional[Callable[[Any, str, str], Any]] = None,
    ) -> List[ScopedProfileWorkflowResult]:
        """Sweep remaining uncleaned entities owned by this run and verify complete cleanup."""
        results: List[ScopedProfileWorkflowResult] = []

        # Enforce authenticated identity verification before performing remote cleanup sweep
        canon_origin = get_canonical_origin(target_url or page.url)
        discovered_account = await AuthenticatedIdentityDiscovery.discover_session_identity(page, canon_origin)
        if not discovered_account:
            # Identity not verified; abort cleanup sweep to avoid touching another user's content
            return results
        if account_context and discovered_account != account_context:
            # Discovered account does not match expected target account; abort sweep
            return results

        effective_account = discovered_account
        if self.barrier:
            self.barrier.set_verified_session_identity(effective_account)

        records = self.registry.get_records_for_run(run_id)
        uncleaned = [r for r in records if (not r.remote_cleaned and r.status in ("CONFIRMED_CREATED", "CONFIRMED_EDITED", "CLEANUP_FAILED", "CREATED", "EDITED", "AMBIGUOUS_UNCONFIRMED")) or (not r.local_cleaned and r.entity_type == "profile_attachment")]

        for r in uncleaned:
            if (r.project_id != project_id or r.account_context != effective_account
                    or get_canonical_origin(r.target_url) != canon_origin):
                continue
            if r.entity_type == "profile_attachment":
                # Local cleanup
                if not r.local_cleaned:
                    try:
                        details = json.loads(r.details_json or "{}")
                        lpath = details.get("local_path")
                        if lpath and os.path.exists(lpath):
                            os.remove(lpath)
                        self.registry.mark_local_cleaned(r.id)
                        r.local_cleaned = True
                    except Exception:
                        pass

                # Never let the legacy cleanup path bypass recovery's identity proof.
                if not r.remote_cleaned:
                    try:
                        confirmed = bool(r.remote_entity_id and json.loads(r.details_json or "{}").get("confirmed_at"))
                        item = page.locator(
                            f"[data-attachment-id={json.dumps(str(r.remote_entity_id))}], [data-id={json.dumps(str(r.remote_entity_id))}]"
                        )
                        confirmed = confirmed and await item.count() == 1 and await item.get_by_text(r.entity_name, exact=True).count() == 1
                    except Exception:
                        confirmed = False
                    if not confirmed:
                        results.append(ScopedProfileWorkflowResult(
                            workflow_type="cleanup", verdict="LIMITED_COVERAGE",
                            entity_id=r.remote_entity_id, entity_name=r.entity_name,
                            action_type="reconcile_pending_cleanup",
                            details="Attachment lacks confirmed creation provenance or unique exact identity; cleanup blocked.",
                            cleanup_status="PENDING", is_customer_defect=False))
                        continue
                    del_btn = item.locator(
                        f"[data-delete-attachment={json.dumps(str(r.remote_entity_id))}], button[data-filename={json.dumps(str(r.entity_name))}], [id={json.dumps(f'btn-delete-{r.remote_entity_id}')}]"
                    ).first
                    if await del_btn.count() > 0 and await del_btn.is_visible():
                        try:
                            async with self._permit(
                                operation_type=OperationType.DELETE_ATTACHMENT,
                                workflow_name="profile_attachment",
                                allowed_methods=("DELETE", "POST"),
                                endpoint_pattern=rf"^/api/(?:profile/)?attachments/{re.escape(r.remote_entity_id)}/?$",
                                resource_id=r.remote_entity_id,
                                account_context=effective_account,
                                verified_account=effective_account,
                                entity_type="profile_attachment",
                                project_id=project_id,
                            ):
                                await del_btn.click()
                                await page.wait_for_timeout(300)
                            readback_status, reason, diag = await self._verify_authenticated_scope_readback(
                                page=page,
                                expected_scope_selector="#section-experience-management, #section-attachment-management, [data-section='attachments']",
                                forbidden_element_text=r.entity_name,
                                target_entity_id=r.remote_entity_id,
                                expected_account=effective_account,
                                intended_origin=canon_origin,
                            )
                            if readback_status == "CONFIRMED_SUCCESS":
                                self.registry.confirm_remote_deletion(r.id)
                                r.remote_cleaned = True
                        except Exception:
                            pass
                continue

            # Experience entries: require destructive authority before deleting
            if self.registry.is_owned_by_run(
                run_id=run_id,
                project_id=project_id,
                target_url=target_url or r.target_url,
                remote_entity_id=r.remote_entity_id,
                entity_name=r.entity_name,
                account_context=effective_account,
                entity_type=r.entity_type,
                require_destructive_authority=True,
            ):
                del_res = await self._exercise_delete_workflow(
                    page=page,
                    run_id=run_id,
                    project_id=project_id,
                    record=r,
                    viewport_name=viewport_name,
                    target_url=target_url or r.target_url,
                    account_context=effective_account,
                    cancellation_token=None,
                    screenshot_fn=screenshot_fn,
                )
                if del_res:
                    results.append(del_res)

        return results


def adapt_scoped_mutation_result_to_test_record(
    result: ScopedProfileWorkflowResult,
    run_id: int,
    target_url: str,
) -> Dict[str, Any]:
    """Convert ScopedProfileWorkflowResult to canonical test record dictionary."""
    clean_vp = "Mobile" if "mobile" in (result.viewport_name or "").lower() else "Desktop"
    status_map = {
        "PASS": "PASS",
        "FAIL": "FAIL",
        "LIMITED_COVERAGE": "LIMITED COVERAGE",
    }
    status = status_map.get(result.verdict, "LIMITED COVERAGE")
    if status == "FAIL" and not result.is_customer_defect:
        status = "LIMITED COVERAGE"
    expected_desc = {
        "create": "Authorized test entity is created and confirmed visible in page DOM.",
        "edit": "Run-owned test entity is modified and updated content is confirmed in page DOM.",
        "upload": "Harmless test file is uploaded and remote deletion is confirmed.",
        "validation_rejection": "Invalid form submission is rejected without creating unintended state.",
        "delete": "Run-owned test entity is deleted and confirmed completely absent from DOM.",
        "cleanup": "All run-owned test entities are cleanly removed from environment.",
        "boundary_rejection": "Production network barrier strictly rejects unauthorized and prohibited state changes.",
    }.get(result.workflow_type, "Authorized state-changing workflow executes within approved scope.")

    stable_id = f"MUT-{result.workflow_type.upper()}-{uuid.uuid4().hex[:6].upper()}" if status == "FAIL" else None

    return {
        "run_id": int(run_id),
        "url": target_url,
        "final_url": target_url,
        "viewport": clean_vp,
        "status": status,
        "expected_result": expected_desc,
        "actual_result": result.details,
        "screenshot_path": result.screenshot_path,
        "screenshot": result.screenshot_path,
        "stable_id": stable_id,
        "finding_id": None,
        "cleanup_status": result.cleanup_status,
        "entity_id": result.entity_id,
        "entity_name": result.entity_name,
        "independent_safety": True,
        "is_mutation_record": True,
    }

def discover_attachment_form_destination(action):
    """Discover only supported attachment collection endpoints, never arbitrary writes."""
    if not isinstance(action, str):
        return None
    action = action.strip()
    return action if re.fullmatch(
        r"/api/(?:profile/)?attachments(?:/upload)?", action
    ) else None

