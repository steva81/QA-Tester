"""Bounded, fail-closed proof of standard GraphQL POST query semantics.

This proves the selected operation type, not arbitrary server resolver purity.
No request body, variables, credentials or parse errors are retained or logged.
"""
import json
from email.message import Message
from urllib.parse import urlsplit

from graphql import get_operation_ast, parse
from graphql.language import OperationType, ParallelVisitor, Visitor, visit
from graphql.validation import (
    ExecutableDefinitionsRule, KnownFragmentNamesRule, LoneAnonymousOperationRule,
    NoFragmentCyclesRule, UniqueArgumentNamesRule, UniqueFragmentNamesRule,
    UniqueInputFieldNamesRule, UniqueOperationNamesRule, UniqueVariableNamesRule,
)
from graphql.validation.validation_context import ASTValidationContext
from app.qa.crawl_policy import link_reason

MAX_BODY_BYTES = 256 * 1024
MAX_TOKENS = 10000
MAX_BATCH = 20
MAX_CLIENT_METADATA_CHARS = 128
RULES = (ExecutableDefinitionsRule, KnownFragmentNamesRule, LoneAnonymousOperationRule,
         NoFragmentCyclesRule, UniqueArgumentNamesRule, UniqueFragmentNamesRule,
         UniqueInputFieldNamesRule, UniqueOperationNamesRule, UniqueVariableNamesRule)


def _invalid(*_args):
    raise ValueError('Unsupported or ambiguous request')


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _invalid()
        result[key] = value
    return result


class _StandardDirectives(Visitor):
    def enter_directive(self, node, *_args):
        # Custom directives can alter execution; no schema contract is available.
        if node.name.value not in ('skip', 'include'):
            _invalid()


def _client_metadata_extensions(value):
    """Accept only bounded descriptive client identity, never transport controls."""
    if not isinstance(value, dict):
        return False
    if not value:
        return True
    if set(value) != {'clientLibrary'}:
        return False
    client = value['clientLibrary']
    if not isinstance(client, dict) or set(client) != {'name', 'version'}:
        return False
    # Printable ASCII without whitespace: conservative identifiers, not free text.
    return all(isinstance(item, str) and 1 <= len(item) <= MAX_CLIENT_METADATA_CHARS
               and all('!' <= char <= '~' for char in item)
               for item in client.values())


def _query(envelope):
    if not isinstance(envelope, dict) or set(envelope) - {'query', 'operationName', 'variables', 'extensions'}:
        return False
    document = envelope.get('query')
    name = envelope.get('operationName')
    if not isinstance(document, str) or not document.strip():
        return False
    if name is not None and (not isinstance(name, str) or not name):
        return False
    if envelope.get('variables') is not None and not isinstance(envelope['variables'], dict):
        return False
    # APQ / proxy controls can override the document and remain denied. Only
    # strictly shaped descriptive client metadata receives an exception.
    if 'extensions' in envelope and not _client_metadata_extensions(envelope['extensions']):
        return False
    ast = parse(document, no_location=True, max_tokens=MAX_TOKENS)
    context = ASTValidationContext(ast, _invalid)
    visit(ast, ParallelVisitor([rule(context) for rule in RULES] + [_StandardDirectives()]))
    selected = get_operation_ast(ast, name)
    return selected is not None and selected.operation == OperationType.QUERY


def is_readonly_graphql_post(request, origin):
    """Only conventional same-origin GraphQL JSON transport qualifies.

    A terminal /graphql path is a transport constraint, never an allowlist:
    every envelope must independently prove its selected query via the AST.
    URL parameters, encodings, unsupported extensions and missing frame metadata deny.
    """
    try:
        if request.method.upper() != 'POST':
            return False
        # Unknown ownership must not gain a POST exception.
        if request.frame.page is None:
            return False
        url = urlsplit(request.url)
        if (url.path.rstrip('/').rsplit('/', 1)[-1].lower() != 'graphql'
                or url.query or url.fragment or link_reason(request.url, origin)):
            return False
        headers = request.headers
        if any(key in headers for key in ('content-encoding', 'x-http-method-override', 'x-method-override', 'x-http-method')):
            return False
        content_type = Message()
        content_type['content-type'] = headers.get('content-type', '')
        if content_type.get_content_type() != 'application/json':
            return False
        params = content_type.get_params()[1:]
        if len(params) > 1 or any(k != 'charset' or v.lower() not in ('utf-8', 'utf8') for k, v in params):
            return False
        body = request.post_data_buffer
        if not isinstance(body, bytes) or not body or len(body) > MAX_BODY_BYTES:
            return False
        payload = json.loads(body.decode('utf-8'), object_pairs_hook=_unique_object, parse_constant=_invalid)
        batch = payload if isinstance(payload, list) else [payload]
        return 0 < len(batch) <= MAX_BATCH and all(_query(item) for item in batch)
    except Exception:
        # Missing request metadata, invalid JSON/AST, resource limits and parser
        # failures all preserve the pre-existing POST denial.
        return False
