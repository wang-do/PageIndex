"""Agent tools: the cloud MCP tool contract, executed against a PageIndexClient.

Tool names and the surviving input-schema structure match the PageIndex
cloud MCP server — the local surface hides the documented cloud-only
parameters — so agent prompts port across the cloud MCP connection and
this in-process layer. Only the tools that exist in every mode are
registered (no folders, search_documents, or get_document_image), and the
guidance strings (tool descriptions) adapt to the local surface the same
way the agent instructions do — they never teach capabilities that only
exist on the cloud.

Tools never raise: every outcome, including errors, is returned as the
same JSON envelope the cloud emits ({"success": true, ...} /
{"error": ...}) — arguments outside a pruned local signature come back as
that envelope too, on the direct and the call_tool path alike, except
browse_documents' ``recursive``: call_tool honors it, because the flat
no-folders shape it asks for is trivially true here. One exception to
never-raise: a cloud 401/403 re-raises PageIndexAPIError.
"""
from __future__ import annotations

import copy
import difflib
import inspect
import json
import re
import threading
import time
import weakref
from typing import Any, Callable, Optional

from .errors import PageIndexAPIError

TOOL_RESPONSE_CHAR_LIMIT = 100_000
_CHAR_BUDGET = int(TOOL_RESPONSE_CHAR_LIMIT * 0.95)
STRUCTURE_FIRST_PAGE_THRESHOLD = 20

_MAX_REQUESTED_PAGES = 10_000
_SIMILAR_NAMES_LIMIT = 3
_TOOL_WAIT_TIMEOUT = 180.0  # "up to 3 minutes", per the wait_for_completion schema
_TOOL_WAIT_INTERVAL = 5.0

_DOC_NAME_DESCRIPTION = (
    'Copy the `name` field verbatim from a browse_documents() or '
    'search_documents() response (case-sensitive, include extension). '
    'Example: "Q3 Report.pdf". If the response shows two documents with the '
    'same name, pass `folder_id` alongside to disambiguate.'
)
_FOLDER_ID_DISAMBIGUATOR_DESCRIPTION = (
    'Disambiguator for same-name documents. Copy the `folder_id` from the '
    'intended browse/search result; use "root" for root-level documents, or '
    '"shared-with-me"/"following" for the read-only folders at the library '
    'root; omit if `doc_name` is unique. Copy any folder_id verbatim from a '
    'browse_documents()/get_folder_structure() response, never construct one.'
)
_WAIT_FOR_COMPLETION_DESCRIPTION = (
    "If true and document is processing, automatically wait up to 3 minutes "
    "until completed. Reduces repeated tool calls."
)

#: Tool names, descriptions, and parameter schemas, identical to the cloud
#: MCP server's tools/list.
TOOL_CONTRACT: dict[str, dict[str, Any]] = {
    "browse_documents": {
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
        "description": (
            "Primary document retrieval tool. After orienting with "
            "get_folder_structure() (when available), use this for all "
            "document-related questions. The bare call returns root-level "
            "sub-folders and documents; pass folder_id to drill into a "
            'sub-folder level by level. Use sort="relevance" + query for '
            "semantic ranking. Do NOT jump to search_documents() first — it "
            "is an escalation path, only after "
            'browse_documents(sort="relevance") has failed.'
        ),
        "schema": {
            "type": "object",
            "properties": {
                "folder_id": {
                    "type": "string",
                    "default": "root",
                    "description": (
                        'Folder scope (default "root"). Pass a specific folder '
                        'ID to scope into that folder, or "root" to reference '
                        "the library root. The read-only \"shared-with-me\" and "
                        '"following" folders live at the library root — pass '
                        "one of those ids to browse them. Copy any folder_id "
                        "verbatim from a browse/tree response, never construct "
                        "one. Combine with `recursive` to control breadth."
                    ),
                },
                "recursive": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Whether to include documents from descendant folders. "
                        "When false (default), returns the direct contents of "
                        "folder_id along with its sub-folders — prefer this for "
                        "level-by-level exploration so you retain folder "
                        "hierarchy context. When true, flattens all descendant "
                        "documents into one list and omits sub-folders — use "
                        "only when a non-recursive browse of the target folder "
                        "returned no relevant results and you need to widen the "
                        "scope, or the user explicitly requests a flat listing."
                    ),
                },
                "sort": {
                    "type": "string",
                    "enum": ["time", "relevance"],
                    "default": "time",
                    "description": (
                        'Sort order. "time" (default) sorts by upload date '
                        '(newest first); "relevance" orders documents by '
                        "semantic relevance to `query`. Relevance also works "
                        "inside the read-only shared folders — pass their "
                        "folder_id — but at the library root it ranks only "
                        "your own documents."
                    ),
                },
                "query": {
                    "type": "string",
                    "description": (
                        "Search query for relevance ranking. Required when "
                        'sort="relevance"; must be omitted when sort="time".'
                    ),
                },
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 9007199254740991,
                    "default": 0,
                    "description": (
                        "Zero-based pagination offset. Pass the value of "
                        "`next_offset` from the previous response to fetch the "
                        "next page."
                    ),
                },
                "limit": {
                    "type": "number",
                    "minimum": 1,
                    "maximum": 50,
                    "default": 50,
                    "description": (
                        "Number of documents to return per page (1-50, "
                        "default 50)"
                    ),
                },
            },
            "required": [],
        },
    },
    "get_document": {
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
        "description": (
            "Check a document's processing status and metadata. `status` is "
            'one of "pending", "queued", "processing", "completed", or '
            '"failed" — call this before `get_document_structure()` or '
            "`get_page_content()` to confirm the document is ready."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "doc_name": {
                    "type": "string",
                    "minLength": 1,
                    "description": _DOC_NAME_DESCRIPTION,
                },
                "folder_id": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "description": _FOLDER_ID_DISAMBIGUATOR_DESCRIPTION,
                },
                "wait_for_completion": {
                    "type": "boolean",
                    "default": False,
                    "description": _WAIT_FOR_COMPLETION_DESCRIPTION,
                },
            },
            "required": ["doc_name"],
        },
    },
    "get_document_structure": {
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
        "description": (
            "Extract a document's hierarchical outline (headers, sections, "
            f"page references). REQUIRED for documents over "
            f"{STRUCTURE_FIRST_PAGE_THRESHOLD} pages — call this first to "
            "locate relevant sections, then pass their page numbers to "
            "`get_page_content()`. The outline is progressive: without "
            "`node_id` you get the TOP level only (chapters); pass a "
            "`node_id` from a previous response to drill into that node's "
            "next level. Drill down level by level until you reach clauses, "
            "then call `get_page_content()`."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "doc_name": {
                    "type": "string",
                    "minLength": 1,
                    "description": _DOC_NAME_DESCRIPTION,
                },
                "node_id": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "description": (
                        "Optional node to drill into (copy `node_id` verbatim "
                        "from a previous response). Omit or null for the "
                        "document's top level."
                    ),
                },
                "folder_id": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "description": _FOLDER_ID_DISAMBIGUATOR_DESCRIPTION,
                },
                "part": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 9007199254740991,
                    "default": 1,
                    "description": (
                        "Part number for pagination (1-based, default 1). For "
                        "large outlines, increment until the response's "
                        "`pagination.has_more` becomes false."
                    ),
                },
                "wait_for_completion": {
                    "type": "boolean",
                    "default": False,
                    "description": _WAIT_FOR_COMPLETION_DESCRIPTION,
                },
            },
            "required": ["doc_name"],
        },
    },
    "get_page_content": {
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
        "description": (
            "Extract page content from a processed document. Use tight, "
            "targeted page ranges — never the whole document at once. For "
            f"documents over {STRUCTURE_FIRST_PAGE_THRESHOLD} pages, call "
            "`get_document_structure()` first to pick relevant sections. "
            "Embedded image paths in the response feed into "
            "`get_document_image()`."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "doc_name": {
                    "type": "string",
                    "minLength": 1,
                    "description": _DOC_NAME_DESCRIPTION,
                },
                "folder_id": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "description": _FOLDER_ID_DISAMBIGUATOR_DESCRIPTION,
                },
                "pages": {
                    "type": "string",
                    "minLength": 1,
                    "pattern": r"^(\d+(-\d+)?)(,\s*\d+(-\d+)?)*$",
                    "description": (
                        'Page specification: "5", "3,7,10", "5-10", or '
                        '"1-3,7,9-12"'
                    ),
                },
                "wait_for_completion": {
                    "type": "boolean",
                    "default": False,
                    "description": _WAIT_FOR_COMPLETION_DESCRIPTION,
                },
            },
            "required": ["doc_name", "pages"],
        },
    },
    "remove_document": {
        "annotations": {"readOnlyHint": False, "destructiveHint": True,
                        "idempotentHint": True, "openWorldHint": False},
        "description": (
            "Permanently delete documents and all associated data. Only invoke "
            "when the user explicitly names the documents AND confirms "
            "deletion. Returns `results` — one entry per requested document: "
            '`{ doc_name, status: "deleted" | "not_found" | "failed", '
            "error? }`. Inspect each entry for per-document failures. This "
            "action is irreversible."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "doc_names": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1},
                    "minItems": 1,
                    "maxItems": 10,
                    "description": (
                        "Array of document names to delete. Each name must be "
                        "copied verbatim from the `name` field of a "
                        "browse_documents() or search_documents() response "
                        "(case-sensitive, include extension). Example: "
                        '["Q3 Report.pdf", "draft.pdf"]. Max 10 per call.'
                    ),
                },
                "folder_id": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "description": _FOLDER_ID_DISAMBIGUATOR_DESCRIPTION,
                },
            },
            "required": ["doc_names"],
        },
    },
}

_READ_TOOLS = ("browse_documents", "get_document", "get_document_structure",
               "get_page_content")
_MANAGEMENT_TOOLS = ("remove_document",)


# ── response envelopes ──

_ToolResult = tuple[dict, bool]


def _success(data: dict[str, Any], next_steps: dict[str, Any]) -> tuple[dict, bool]:
    return {"success": True, **data, "next_steps": next_steps}, False


def _failure(error: str, details: Optional[dict[str, Any]],
             next_steps: dict[str, Any], error_code: Optional[str] = None,
             ) -> tuple[dict, bool]:
    payload: dict[str, Any] = {"error": error}
    if error_code:
        payload["errorCode"] = error_code
    if details:
        payload.update(details)
    payload["next_steps"] = next_steps
    return payload, True


def _dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


# ── document listing / name resolution ──

def _all_documents(client, stop_ids=None) -> list[dict[str, Any]]:
    """Every document the client can list, newest first (both modes list
    newest-first; paging preserves that order). With ``stop_ids``, paging
    stops early once every one of those ids has been seen — for callers
    that only need those entries; an id absent from the listing still
    costs a full sweep."""
    documents: list[dict[str, Any]] = []
    offset = 0
    remaining = {str(one_id) for one_id in stop_ids} if stop_ids else None
    while True:
        page = client.list_documents(limit=100, offset=offset)
        batch = page.get("documents") or []
        documents.extend(batch)
        if remaining is not None:
            remaining.difference_update(str(doc.get("id")) for doc in batch)
            if not remaining:
                return documents
        # Advance by what actually arrived — stepping by the requested
        # limit skips documents whenever a server caps its page size.
        offset += len(batch)
        total = page.get("total")
        # An empty page is the reliable terminator; `total` (absent or
        # None on some backends) only saves the final empty-page request.
        if not batch or (isinstance(total, int) and offset >= total):
            return documents


def _normalize_created_at(value: Any) -> str:
    """Emit the cloud tool format (ISO-8601 UTC with 'Z', millisecond
    precision) from either mode's createdAt string."""
    if not isinstance(value, str) or not value:
        return ""
    try:
        from datetime import datetime, timezone
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        parsed = parsed.astimezone(timezone.utc)
        return parsed.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    except ValueError:
        return value


def _flat_metadata(value: Any) -> Optional[dict[str, Any]]:
    """User-facing string|number|boolean metadata fields only, or None."""
    if not isinstance(value, dict):
        return None
    flat = {key: val for key, val in value.items()
            if isinstance(val, (str, int, float, bool))}
    return flat or None


def _scope_documents(documents: list[dict[str, Any]],
                     allowed_ids: Optional[frozenset]) -> list[dict[str, Any]]:
    if allowed_ids is None:
        return documents
    return [doc for doc in documents if doc.get("id") in allowed_ids]


def _resolve_document(
    client, doc_name: str,
    documents: Optional[list[dict[str, Any]]] = None,
    allowed_ids: Optional[frozenset] = None,
) -> "tuple[Optional[dict[str, Any]], Optional[_ToolResult]]":
    """Resolve doc_name to a list entry. Same-name duplicates resolve to the
    newest match. Returns (entry, None) or (None, error_payload_pair)."""
    if documents is None:
        documents = _all_documents(client, stop_ids=allowed_ids)
    documents = _scope_documents(documents, allowed_ids)
    matches = [doc for doc in documents if doc.get("name") == doc_name]
    if matches:
        return max(matches, key=lambda d: d.get("createdAt") or ""), None
    names = [str(doc.get("name")) for doc in documents if doc.get("name")]
    similar = difflib.get_close_matches(str(doc_name), names,
                                        n=_SIMILAR_NAMES_LIMIT, cutoff=0.5)
    message = (
        "Document not found. Did you mean: "
        + ", ".join(f'"{name}"' for name in similar) + "?"
        if similar else "Document not found or you do not have access to it"
    )
    return None, _failure(
        message,
        {"doc_name": doc_name, "similar_files": similar},
        {
            "summary": "The requested document does not exist or is not accessible",
            "options": [
                "Verify the document name is correct",
                "Use browse_documents() to see your recent documents",
                "Check if the document was deleted",
            ],
        },
        "NOT_FOUND",
    )


def _refetch_entry(client, doc_id: str) -> Optional[dict[str, Any]]:
    try:
        return client.get_document(doc_id)
    except PageIndexAPIError:
        return None


def _await_completion(client, entry: dict[str, Any], wait: bool) -> dict[str, Any]:
    """Re-poll a processing document for up to 3 minutes when wait is set.
    Local documents are stored already terminal, so the wait never engages
    there."""
    doc_id = entry.get("id")
    if not wait or not doc_id or entry.get("status") in ("completed", "failed"):
        return entry
    deadline = time.monotonic() + _TOOL_WAIT_TIMEOUT
    current = entry
    while time.monotonic() < deadline:
        time.sleep(_TOOL_WAIT_INTERVAL)
        refreshed = _refetch_entry(client, doc_id)
        if refreshed is None:
            continue  # transient refetch failure: poll on to the deadline
        if refreshed.get("metadata") is None:
            # Status refetches omit (or null out) custom metadata; keep the
            # listing's copy.
            refreshed["metadata"] = current.get("metadata")
        current = {**current, **refreshed}
        if current.get("status") in ("completed", "failed"):
            return current
    return current


def _not_ready_error(doc_name: str, status: Any, operation: str,
                     timed_out: bool) -> tuple[dict, bool]:
    if status == "failed":
        return _failure(
            f"Document processing failed. Current status: {status}",
            {"doc_name": doc_name},
            {
                "summary": "Document processing has failed",
                "options": [
                    "Index the document again with "
                    "PageIndexClient.submit_document()",
                    "Use browse_documents() to work with other documents",
                ],
            },
            "INVALID_INPUT",
        )
    if timed_out:
        return _failure(
            f"Document is still processing. Current status: {status}",
            {"doc_name": doc_name},
            {
                "summary": "Document processing timeout",
                "options": [
                    "Try again later when processing is complete",
                    "Check status with get_document()",
                ],
            },
            "INVALID_INPUT",
        )
    return _failure(
        f"Document is not ready for {operation}. Current status: {status}",
        {"doc_name": doc_name},
        {
            "summary": "Document is still processing",
            "options": [
                "Wait for document processing to complete",
                "Check status with browse_documents() or get_document()",
            ],
        },
        "INVALID_INPUT",
    )


def _folder_unsupported(param: str) -> tuple[dict, bool]:
    return _failure(
        f"Folders are not supported in local mode yet — omit {param}.",
        None,
        {
            "summary": "This local library does not have folders yet",
            "options": ["Retry the call without a folder_id",
                        "Use browse_documents() to list the library root",
                        "Folders are available on PageIndex cloud (PageIndexCloudClient with an API key)"],
        },
        "INVALID_INPUT",
    )


# ── page spec handling ──

class _PageSpecError(ValueError):
    """Shared page-spec rejection; ``code`` picks the caller's rendering."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# re.ASCII mirrors the ECMA regex semantics the contract's pattern carries
_PAGE_SPEC_RE = re.compile(
    TOOL_CONTRACT["get_page_content"]["schema"]["properties"]["pages"]["pattern"],
    re.ASCII)


def _expand_pages(pages) -> list[int]:
    """Expand '1-3,7' into sorted distinct pages — the one parser for the
    SDK surface and the tool layer, holding both to the contract's pages
    pattern. Raises _PageSpecError with code 'invalid', 'too_many', or
    'nonpositive'."""
    if not isinstance(pages, str):
        raise _PageSpecError("invalid",
                             f"Invalid page specification: {pages!r}")
    if not _PAGE_SPEC_RE.fullmatch(pages):
        raise _PageSpecError(
            "invalid", f"Invalid page specification '{pages}'")
    too_many = (f"Page specification '{pages}' spans more than "
                f"{_MAX_REQUESTED_PAGES} pages; request a narrower range")
    expanded: set[int] = set()
    for part in pages.split(","):
        part = part.strip()
        try:
            if "-" in part:
                start, end = (int(x) for x in part.split("-", 1))
                if start > end:
                    raise _PageSpecError(
                        "invalid",
                        f"Invalid range '{part}': start must be <= end")
            else:
                start = end = int(part)
        except _PageSpecError:
            raise
        except ValueError as exc:
            raise _PageSpecError(
                "invalid", f"Invalid page specification '{pages}'") from exc
        # Bound each part arithmetically before materializing it: a spec like
        # "1-1000000000" would otherwise expand to billions of integers
        # inside the caller's process. The cap is on distinct pages, so
        # overlapping parts (a parent section plus its children) don't
        # double-count.
        if end - start + 1 > _MAX_REQUESTED_PAGES:
            raise _PageSpecError("too_many", too_many)
        expanded.update(range(start, end + 1))
        if len(expanded) > _MAX_REQUESTED_PAGES:
            raise _PageSpecError("too_many", too_many)
    if min(expanded) < 1:
        raise _PageSpecError(
            "nonpositive",
            "Invalid page numbers. Page numbers must be positive integers")
    return sorted(expanded)


def _parse_page_spec(
    pages: str, doc_name: str,
) -> "tuple[Optional[list[int]], Optional[_ToolResult]]":
    """Expand '1-3,7' into a sorted, deduplicated page list, or an error."""
    try:
        return _expand_pages(pages), None
    except _PageSpecError as exc:
        if exc.code == "too_many":
            return None, _failure(
                f"Too many pages requested (over {_MAX_REQUESTED_PAGES})",
                {"doc_name": doc_name},
                {
                    "summary": "The page specification spans too many pages",
                    "options": [
                        "Request a narrower page range",
                        "The response holds only a few pages per call - page through with several smaller requests",
                    ],
                },
                "INVALID_INPUT",
            )
        if exc.code == "nonpositive":
            return None, _failure(
                "Invalid page numbers. Page numbers must be positive integers",
                {"doc_name": doc_name},
                {
                    "summary": "Invalid page numbers provided",
                    "options": [
                        "Page numbers must be positive integers (>= 1)",
                        "Check the page specification format",
                    ],
                },
                "INVALID_INPUT",
            )
        return None, _failure(
            "Invalid page specification format",
            {"doc_name": doc_name},
            {
                "summary": "Failed to parse the pages parameter",
                "options": [
                    'Use valid formats: "5", "3,7,10", "5-10", or "1-3,7,9-12"',
                    "Ensure page numbers are positive integers",
                ],
            },
            "INVALID_INPUT",
        )


def _format_page_spec(pages: list[int]) -> str:
    """Compress [1,2,3,5] into '1-3,5'."""
    if not pages:
        return ""
    ordered = sorted(set(pages))
    ranges = []
    start = prev = ordered[0]
    for page in ordered[1:]:
        if page == prev + 1:
            prev = page
            continue
        ranges.append(f"{start}" if start == prev else f"{start}-{prev}")
        start = prev = page
    ranges.append(f"{start}" if start == prev else f"{start}-{prev}")
    return ",".join(ranges)


# ── structure formatting / splitting ──

def _serialized_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False))




# ── tool implementations (client-backed; mode-blind) ──

def _browse_documents(client, folder_id: str = "root", recursive: bool = False,
                      sort: str = "time", query: Optional[str] = None,
                      offset: int = 0, limit: int = 50,
                      _allowed_ids: Optional[frozenset] = None) -> tuple[dict, bool]:
    if folder_id != "root":
        return _folder_unsupported("folder_id")
    if sort not in ("time", "relevance"):
        return _failure(
            'Invalid sort mode — only the default "time" sort is available '
            "in local mode.", None,
            {"summary": "Invalid sort mode",
             "options": ['Use sort="time" (newest first) or omit sort',
                         "Semantic ranking is available on PageIndex cloud (PageIndexCloudClient with an API key)"]},
            "INVALID_INPUT",
        )
    if sort == "relevance" or query:
        # Semantic ranking is a cloud capability; like folders, it is not
        # imitated here.
        return _failure(
            "Relevance ranking is not supported in local mode yet — use "
            "the default time sort.", None,
            {"summary": "This local library does not have semantic ranking yet",
             "options": ["Retry without sort/query and match the returned names and descriptions against the intent yourself",
                         "Page through the full library with `offset: next_offset`",
                         "Semantic ranking is available on PageIndex cloud (PageIndexCloudClient with an API key)"]},
            "INVALID_INPUT",
        )
    try:
        offset = max(int(offset), 0)
        limit = min(max(int(limit), 1), 50)
    except (TypeError, ValueError):
        return _failure("offset and limit must be numbers", None,
                        {"summary": "Invalid pagination parameters",
                         "options": ["Pass integer offset and limit values"]},
                        "INVALID_INPUT")

    if _allowed_ids is None:
        listing = client.list_documents(limit=limit, offset=offset)
        window = listing.get("documents") or []
        total = listing.get("total")
    else:
        scoped = _scope_documents(_all_documents(client, stop_ids=_allowed_ids),
                                  _allowed_ids)
        window, total = scoped[offset:offset + limit], len(scoped)
    window_end = offset + len(window)
    has_more = bool(window) and (window_end < total if isinstance(total, int)
                                 else len(window) == limit)
    next_offset = window_end if has_more else None

    page_has_processing = False
    page_has_failed = False
    items = []
    for doc in window:
        status = doc.get("status") or "unknown"
        if status == "failed":
            page_has_failed = True
        elif status != "completed":
            page_has_processing = True
        item = {
            "name": doc.get("name") or "Unknown Document",
            "description": doc.get("description") or "No description provided",
            "status": status,
            "created_at": _normalize_created_at(doc.get("createdAt")),
        }
        metadata = _flat_metadata(doc.get("metadata"))
        if metadata is not None:
            item["metadata"] = metadata
        items.append(item)

    data: dict[str, Any] = {
        "documents": items,
        "sort": sort,
        "next_offset": next_offset,
        "has_more": has_more,
    }
    if not recursive:
        data["folders"] = []

    if not items and offset == 0:
        next_steps = {
            "summary": "Nothing to show",
            "options": ["Nothing here. Index documents with "
                        "PageIndexClient.submit_document() to get started."],
            "auto_retry": "Index a document with "
                          "PageIndexClient.submit_document() to get started",
        }
        return _success(data, next_steps)

    options = []
    if items:
        options.append("Use get_document() with a document name to view details")
        options.append(
            "Results returned ≠ correct results. Verify these documents match "
            "the user's actual intent (topic, time period, document type) "
            "before proceeding."
            + (" If they do not match, page through the rest of the library."
               if has_more else "")
            + " Do NOT use general knowledge as a substitute."
        )
    if page_has_processing:
        options.append("Some documents on this page are still processing. "
                       "Use get_document() to check individual status.")
    if page_has_failed:
        options.append("Some documents on this page failed processing. "
                       "Use get_document() to see error details.")
    if has_more:
        options.append("Use browse_documents() with `offset: next_offset` to "
                       "load more documents")
    summary = (f"Showing {len(items)} document(s)"
               + (" (more available)" if has_more else "")
               if items else "Nothing to show")
    return _success(data, {"summary": summary, "options": options})


def _get_document(client, doc_name: str, folder_id: Optional[str] = None,
                  wait_for_completion: bool = False,
                  _allowed_ids: Optional[frozenset] = None) -> tuple[dict, bool]:
    if folder_id not in (None, "root"):
        return _folder_unsupported("folder_id")
    entry, error = _resolve_document(client, doc_name, allowed_ids=_allowed_ids)
    if error is not None:
        return error
    assert entry is not None
    entry = _await_completion(client, entry, wait_for_completion)

    status = entry.get("status") or "unknown"
    is_processing = status not in ("completed", "failed")
    is_ready = status == "completed"
    page_num = entry.get("pageNum") or 0
    name = entry.get("name") or "Unknown Document"

    suggestions: list[str] = []
    if is_processing:
        suggestions.append("Document is still processing. Processing status "
                           "can be checked later.")
    elif is_ready:
        suggestions.append("Document is ready for analysis.")
        if page_num > 0:
            if page_num <= 5:
                suggestions.extend([
                    f"This is a short document with {page_num} pages.",
                    f'First explore structure: get_document_structure(doc_name: "{name}")',
                    f'Then extract all content: get_page_content(doc_name: "{name}", pages: "1-{page_num}")',
                ])
            elif page_num <= STRUCTURE_FIRST_PAGE_THRESHOLD:
                suggestions.extend([
                    f"This document has {page_num} pages.",
                    f'First explore structure: get_document_structure(doc_name: "{name}")',
                    f'Then extract key pages: get_page_content(doc_name: "{name}", pages: "1,5,10")',
                ])
            else:
                suggestions.extend([
                    f"This is a large document with {page_num} pages.",
                    f'First explore structure: get_document_structure(doc_name: "{name}")',
                    f'Then target specific sections: get_page_content(doc_name: "{name}", pages: "1-3")',
                ])
    else:
        suggestions.append("Document processing failed. Index the document "
                           "again with PageIndexClient.submit_document().")

    data: dict[str, Any] = {
        "name": name,
        "description": entry.get("description") or "No description provided",
        "status": status,
        "created_at": _normalize_created_at(entry.get("createdAt")),
        "page_count": page_num or None,
        "folder_id": entry.get("folderId"),
    }
    metadata = _flat_metadata(entry.get("metadata"))
    if metadata is not None:
        data["metadata"] = metadata

    return _success(data, {
        "summary": ("Document is ready for analysis and querying." if is_ready
                    else "Document is still being processed." if is_processing
                    else "Document processing has failed."),
        "options": suggestions,
        **({"auto_retry": "Document processing status can be monitored periodically"}
           if is_processing else {}),
    })


def _find_node(node, node_id: str):
    """按 node_id 在树中定位节点（支持 list 与 dict 两种树根）。"""
    if isinstance(node, list):
        for item in node:
            found = _find_node(item, node_id)
            if found is not None:
                return found
        return None
    if isinstance(node, dict):
        if node.get("node_id") == node_id:
            return node
        return _find_node(node.get("nodes") or [], node_id)
    return None


def _get_document_structure(client, doc_name: str,
                            folder_id: Optional[str] = None, part: int = 1,
                            node_id: Optional[str] = None,
                            wait_for_completion: bool = False,
                            _allowed_ids: Optional[frozenset] = None) -> tuple[dict, bool]:
    if folder_id not in (None, "root"):
        return _folder_unsupported("folder_id")
    entry, error = _resolve_document(client, doc_name, allowed_ids=_allowed_ids)
    if error is not None:
        return error
    assert entry is not None
    waited = wait_for_completion and entry.get("status") not in ("completed", "failed")
    entry = _await_completion(client, entry, wait_for_completion)
    if entry.get("status") != "completed":
        return _not_ready_error(doc_name, entry.get("status"),
                                "structure retrieval",
                                waited and entry.get("status") != "failed")

    try:
        raw_tree = getattr(getattr(client, "_api", None), "raw_tree", None)
        tree = raw_tree(entry["id"]) if raw_tree is not None else None
        if tree is None:
            # Don't download text — the caller only needs the outline.
            tree = client.get_tree(entry["id"], node_summary=True,
                                   include_text=False).get("result")
    except PageIndexAPIError as exc:
        return _failure(
            f"Failed to retrieve document structure: {exc}",
            {"doc_name": doc_name},
            {
                "summary": "Failed to retrieve document structure due to an error",
                "options": [
                    "The document may not exist or is not accessible",
                    "Check if the document name is correct",
                    "Try again in a few moments",
                ],
            },
            "INTERNAL_ERROR",
        )
    if tree is None:
        return _failure(
            "Structure not available for this document",
            {"doc_name": doc_name},
            {
                "summary": "Structure not available for this document",
                "options": [
                    "The document may not have been processed correctly or structure extraction may have failed",
                    "Try processing the document again if possible",
                ],
            },
            "INTERNAL_ERROR",
        )

    # 渐进式下钻：node_id 未给定 → 返回第一层（顶层各章）；给定 → 返回该
    # 节点的下一层子节点列表。每层只带 title/页范围/has_children，不带
    # summary（条文导入模式下 summary 是条文全文，是工具结果体积的大头）。
    # tree.json 顶层是多根 list（附录 A/B/C + 各章各为一个根）。
    if node_id:
        target = _find_node(tree, node_id)
        if target is None:
            return _failure(
                f"node_id not found in document: {node_id}",
                {"doc_name": doc_name, "node_id": node_id},
                {"summary": "Unknown node_id",
                 "options": ["Copy node_id verbatim from a previous "
                             "get_document_structure response",
                             "Call without node_id to restart from the top level"]},
                "INVALID_INPUT",
            )
        children = target.get("nodes") or []
        header = {"doc_name": doc_name, "node_id": target.get("node_id"),
                  "title": target.get("title")}
    else:
        target = None
        children = tree if isinstance(tree, list) else [tree]
        header = {"doc_name": doc_name,
                  "title": "(document top level)",
                  "node_id": None}

    outline = []
    for child in children:
        entry = {
            "start_index": child.get("start_index"),
            "end_index": child.get("end_index"),
        }
        if child.get("clause_no"):
            entry["clause_no"] = child["clause_no"]
        has_children = bool(child.get("nodes"))
        entry["has_children"] = has_children
        if has_children:
            entry["node_id"] = child["node_id"]
        # summary 前置 title：每层节点都带语义（章/节是概要，条文是全文），
        # 模型在任意层都能判断内容相关性，支撑"宽泛问题到章节层泛答"。
        title = (child.get("title") or "").strip()
        summary = (child.get("summary") or "").strip()
        label = f"{title}：{summary}" if title and summary else (title or summary)
        if label:
            entry["summary"] = label
        outline.append(entry)

    payload = {
        **header,
        "child_count": len(outline),
        "children": outline,
    }
    next_steps = {
        "summary": (f"Level with {len(outline)} node(s). Nodes with "
                    f"has_children=true can be drilled into via node_id; "
                    f"leaves are clauses — use get_page_content() with their "
                    f"start_index~end_index page range."),
        "options": ["Use get_page_content() to read the clause text"],
    }
    return _success(payload, next_steps)


def _get_page_content(client, doc_name: str, pages: str,
                      folder_id: Optional[str] = None,
                      wait_for_completion: bool = False,
                      _allowed_ids: Optional[frozenset] = None) -> tuple[dict, bool]:
    if folder_id not in (None, "root"):
        return _folder_unsupported("folder_id")
    entry, error = _resolve_document(client, doc_name, allowed_ids=_allowed_ids)
    if error is not None:
        return error
    assert entry is not None
    waited = wait_for_completion and entry.get("status") not in ("completed", "failed")
    entry = _await_completion(client, entry, wait_for_completion)
    if entry.get("status") != "completed":
        return _not_ready_error(doc_name, entry.get("status"),
                                "page content retrieval",
                                waited and entry.get("status") != "failed")

    requested, error = _parse_page_spec(pages, doc_name)
    if error is not None:
        return error
    assert requested is not None

    try:
        page_data = client.get_ocr(entry["id"], format="page").get("result") or []
    except PageIndexAPIError as exc:
        return _failure(
            f"Failed to retrieve page content: {exc}",
            {"doc_name": doc_name},
            {
                "summary": "Unable to retrieve page content due to a service issue.",
                "options": [
                    "Verify the document name is correct using browse_documents()",
                    "Check if the document processing is complete with get_document()",
                    "Ensure the requested page numbers are valid",
                ],
                "auto_retry": "This may be a temporary issue - you can try "
                              "the request again",
            },
            "INTERNAL_ERROR",
        )

    by_index = {item["page_index"]: item for item in page_data
                if isinstance(item, dict)
                and isinstance(item.get("page_index"), int)}
    max_page = max(by_index, default=0)

    out_of_range = [page for page in requested if page > max_page]
    valid_pages = [page for page in requested if page <= max_page]
    if out_of_range and not valid_pages:
        return _failure(
            f"All requested pages are out of range. Document has {max_page} "
            f"pages, but you requested pages: {_format_page_spec(out_of_range)}",
            {
                "doc_name": doc_name,
                "max_pages": max_page,
                "requested_pages": _format_page_spec(out_of_range),
            },
            {
                "summary": "All requested pages are out of range for this document",
                "options": [
                    f"Request pages between 1 and {max_page}",
                    "Use get_document() to check document page count",
                ],
            },
            "INVALID_INPUT",
        )

    content = []
    included: list[int] = []
    remaining: list[int] = []
    budget = _CHAR_BUDGET
    for page in valid_pages:
        item = by_index.get(page)
        markdown = item.get("markdown") if item else None
        text = (markdown if isinstance(markdown, str)
                else f"Page {page} content not available")
        entry = {"page": page, "text": text}
        size = _serialized_size(entry) + 2  # +2: json ", " item separator
        if not included or budget - size >= 0:
            content.append(entry)
            included.append(page)
            budget -= size
        else:
            remaining.append(page)

    options = [
        "Use get_document_structure() to understand document organization",
        "Request additional pages as needed",
    ]
    if remaining:
        options.insert(0, f"For remaining pages, request: {_format_page_spec(remaining)}")
    if out_of_range:
        options.insert(0, f"Document has {max_page} pages total - request "
                          f"pages 1-{max_page}")
    if remaining or out_of_range:
        parts = [f"Retrieved {len(included)} of {len(requested)} "
                 "requested pages."]
        if remaining:
            parts.append(f"Pages {_format_page_spec(remaining)} were "
                         "omitted due to response size limits.")
        if out_of_range:
            parts.append(f"Pages {_format_page_spec(out_of_range)} "
                         "were out of range.")
        summary = " ".join(parts)
    else:
        summary = (f"Successfully retrieved content for {len(content)} "
                   f"page{'' if len(content) == 1 else 's'}.")
    return _success(
        {
            "doc_name": doc_name,
            "total_pages": max_page,
            "requested_pages": _format_page_spec(requested),
            "returned_pages": _format_page_spec(included),
            "content": content,
        },
        {"summary": summary, "options": options},
    )


def _remove_document(client, doc_names: list[str],
                     folder_id: Optional[str] = None,
                     _allowed_ids: Optional[frozenset] = None) -> tuple[dict, bool]:
    if folder_id not in (None, "root"):
        return _folder_unsupported("folder_id")
    if not isinstance(doc_names, list) or not doc_names:
        return _failure("At least one document name is required", None,
                        {"summary": "No document names provided",
                         "options": ["Pass doc_names as a non-empty array"]},
                        "INVALID_INPUT")
    # Validate every element before deleting anything: a rejection envelope
    # must mean nothing was destroyed.
    if not all(isinstance(name, str) and name.strip() for name in doc_names):
        return _failure(
            "doc_names must be an array of non-empty document name strings",
            None,
            {"summary": "Invalid document names",
             "options": ["Copy each name verbatim from a browse_documents() "
                         "response"]},
            "INVALID_INPUT")
    doc_names = list(dict.fromkeys(doc_names))
    if len(doc_names) > 10:
        return _failure("Maximum 10 documents can be deleted at once", None,
                        {"summary": "Too many documents in one call",
                         "options": ["Delete at most 10 documents per call"]},
                        "INVALID_INPUT")
    documents = _all_documents(client, stop_ids=_allowed_ids)
    results = []
    for doc_name in doc_names:
        entry, error = _resolve_document(client, doc_name, documents=documents,
                                         allowed_ids=_allowed_ids)
        if error is not None or entry is None:
            results.append({"doc_name": doc_name, "status": "not_found"})
            continue
        try:
            client.delete_document(entry["id"])
            results.append({"doc_name": doc_name, "status": "deleted"})
        except Exception as exc:
            # Any escape here (OSError, transport errors) would discard the
            # entries for documents already irreversibly deleted.
            results.append({"doc_name": doc_name, "status": "failed",
                            "error": str(exc)})
    deleted = sum(1 for item in results if item["status"] == "deleted")
    return _success(
        {"results": results},
        {
            "summary": f"Deleted {deleted} of {len(doc_names)} document(s).",
            "options": ["Use browse_documents() to review the remaining library"],
        },
    )


_IMPLEMENTATIONS: dict[str, Callable[..., tuple[dict, bool]]] = {
    "browse_documents": _browse_documents,
    "get_document": _get_document,
    "get_document_structure": _get_document_structure,
    "get_page_content": _get_page_content,
    "remove_document": _remove_document,
}


def tool_names(include_management: bool = False) -> tuple[str, ...]:
    return _READ_TOOLS + (_MANAGEMENT_TOOLS if include_management else ())


def _coerce_bool_args(schema: dict, kwargs: dict[str, Any]) -> None:
    """Models routinely send booleans as JSON strings ("false"); the bare
    truthiness tests downstream would read those as True. Runs on both
    dispatch paths — call_tool and the cloud bridge invoker."""
    properties = (schema or {}).get("properties", {})
    for key, spec in properties.items():
        value = kwargs.get(key)
        if spec.get("type") == "boolean" and isinstance(value, str):
            kwargs[key] = value.strip().lower() not in ("false", "no", "0", "")


def call_tool(client, name: str, arguments: dict[str, Any],
              doc_ids=None) -> tuple[str, bool]:
    """Run one contract tool; returns (envelope_json, is_error). Never raises
    for tool-level failures — unexpected exceptions become error envelopes.
    ``doc_ids`` restricts every document lookup to that allowlist (the local
    chat surfaces' doc_id scope)."""
    implementation = _IMPLEMENTATIONS.get(name)
    if implementation is None:
        payload, _ = _failure(
            f"Unknown tool: {name}",
            {"tool_name": name, "available_tools": list(_IMPLEMENTATIONS)},
            {"summary": "Tool not found",
             "options": [f"Available tools: {', '.join(_IMPLEMENTATIONS)}"]},
            "INVALID_INPUT",
        )
        return _dumps(payload), True
    if arguments is not None and not isinstance(arguments, dict):
        payload, is_error = _failure(
            f"Invalid arguments for {name}: expected a JSON object, got "
            f"{type(arguments).__name__}", None,
            {"summary": "Invalid tool arguments",
             "options": [f"Pass {name}() arguments as a JSON object of its "
                         "parameters"]},
            "INVALID_INPUT",
        )
        return _dumps(payload), is_error
    # Underscore-prefixed keys are the SDK's private channel (the scope
    # below), never model arguments. None ≡ omitted (the contract's
    # "omit if ..." semantics, same as the cloud bridge invoker).
    kwargs = {key: value for key, value in (arguments or {}).items()
              if not key.startswith("_") and value is not None}
    _coerce_bool_args(TOOL_CONTRACT.get(name, {}).get("schema", {}), kwargs)
    try:
        if doc_ids is not None:
            ids = [doc_ids] if isinstance(doc_ids, str) else doc_ids
            kwargs["_allowed_ids"] = frozenset(str(one_id) for one_id in ids)
        bound = inspect.signature(implementation).bind(client, **kwargs)
    except TypeError as exc:
        payload, is_error = _failure(
            f"Invalid arguments for {name}: {exc}", None,
            {"summary": "Invalid tool arguments",
             "options": [f"Check the {name}() parameter names and types"]},
            "INVALID_INPUT",
        )
        return _dumps(payload), is_error
    try:
        payload, is_error = implementation(*bound.args, **bound.kwargs)
    except Exception as exc:  # tool calls must never raise into the agent loop
        payload, is_error = _failure(
            f"{name} failed: {exc}", None,
            {"summary": "Unexpected error while running the tool",
             "options": ["Try the request again"],
             "auto_retry": "This is likely a temporary issue - you can try "
                           "the request again"},
            "INTERNAL_ERROR",
        )
    return _dumps(payload), is_error


# ── plain-function materialization (the `client.agent_tools()` surface) ──

def _tool_docstring(description: str, properties: dict[str, Any]) -> str:
    lines = [description, "", "Args:"]
    for param, spec in properties.items():
        lines.append(f"    {param}: {spec.get('description', '')}")
    return "\n".join(lines)


_LOCAL_HIDDEN_PARAMS: dict[str, tuple[str, ...]] = {
    "browse_documents": ("folder_id", "recursive", "sort", "query"),
    "get_document": ("folder_id",),
    "get_document_structure": ("folder_id",),
    "get_page_content": ("folder_id",),
    "remove_document": ("folder_id",),
}

_LOCAL_DOC_NAME_DESCRIPTION = (
    'Copy the `name` field verbatim from a browse_documents() response '
    '(case-sensitive, include extension). Example: "Q3 Report.pdf". '
    "Document names are unique in a local library."
)

_LOCAL_DESCRIPTIONS: dict[str, str] = {
    "browse_documents": (
        "Primary document retrieval tool — first choice for any "
        "document-related question. Lists your documents newest first with "
        "names and descriptions; match them against the user's intent and "
        "page through with `offset: next_offset` (limit up to 50) while "
        "`has_more` is true. "
        'Folder browsing and semantic ranking (sort="relevance") are not '
        "supported in local mode yet — they work on PageIndex cloud."
    ),
    # Drop the sentence naming the cloud-only image tool, whatever its
    # wording; the contract-refresh test pins that something was removed.
    "get_page_content": re.sub(
        r"\s*[^.]*`get_document_image\(\)`[^.]*\.", "",
        TOOL_CONTRACT["get_page_content"]["description"]),
}

_LOCAL_PARAM_DESCRIPTIONS: dict[tuple[str, str], str] = {
    ("get_document", "doc_name"): _LOCAL_DOC_NAME_DESCRIPTION,
    ("get_document_structure", "doc_name"): _LOCAL_DOC_NAME_DESCRIPTION,
    ("get_page_content", "doc_name"): _LOCAL_DOC_NAME_DESCRIPTION,
    ("remove_document", "doc_names"): (
        "Array of document names to delete. Each name must be copied "
        "verbatim from the `name` field of a browse_documents() response "
        '(case-sensitive, include extension). Example: ["Q3 Report.pdf", '
        '"draft.pdf"]. Max 10 per call.'
    ),
}


def _local_description(name: str) -> str:
    return _LOCAL_DESCRIPTIONS.get(name) or TOOL_CONTRACT[name]["description"]


def _local_schema(name: str) -> dict[str, Any]:
    schema = copy.deepcopy(TOOL_CONTRACT[name]["schema"])
    for param in _LOCAL_HIDDEN_PARAMS.get(name, ()):
        schema["properties"].pop(param, None)
    for (tool_name, param), text in _LOCAL_PARAM_DESCRIPTIONS.items():
        if tool_name == name and param in schema["properties"]:
            schema["properties"][param]["description"] = text
    return schema




_SCHEMA_TYPE_MAP = {"string": str, "integer": int, "number": float,
                    "boolean": bool, "array": list, "object": dict}


def _annotation_for(spec: dict) -> Any:
    schema_type = spec.get("type")
    if schema_type is None and isinstance(spec.get("anyOf"), list):
        # Nullable unions arrive as anyOf: [{type: string}, {type: null}].
        options = [option for option in spec["anyOf"]
                   if isinstance(option, dict) and option.get("type")]
        schema_type = [option["type"] for option in options]
        # `items` lives on the array option, not the union shell.
        spec = next((option for option in options
                     if option["type"] == "array"), spec)
    nullable = False
    if isinstance(schema_type, list):
        nullable = "null" in schema_type
        bases = [t for t in schema_type if t != "null"]
        schema_type = bases[0] if bases else None
    base = _SCHEMA_TYPE_MAP.get(schema_type or "", Any)
    if base is list:
        # Strict function calling rejects arrays whose item type was lost
        # in the annotation round-trip; parameterize when it is known.
        item_type = (spec["items"].get("type")
                     if isinstance(spec.get("items"), dict) else None)
        element = (_SCHEMA_TYPE_MAP.get(item_type)
                   if isinstance(item_type, str) else None)
        if element is not None:
            base = list[element]
    return Optional[base] if nullable else base


def _bridge_invoker(bridge, name: str, schema: dict,
                    ) -> "Callable[[dict], tuple[str, bool]]":
    """One cloud tool call proxied over MCP: string booleans are coerced
    (same as call_tool), None-valued arguments are dropped (None ≡ omitted,
    matching the contract's "omit if ..." semantics) and failures are
    contained in the error envelope — except 401/403, which re-raise.
    Returns (envelope_text, is_error), like call_tool."""
    def _invoke(arguments: dict[str, Any]) -> tuple[str, bool]:
        try:
            arguments = {key: value for key, value in arguments.items()
                         if value is not None}
            _coerce_bool_args(schema, arguments)
            return bridge.call_tool(name, arguments)
        except Exception as exc:
            if (isinstance(exc, PageIndexAPIError)
                    and exc.status_code in (401, 403)):
                raise
            payload, _ = _failure(
                f"{name} failed: {exc}", None,
                {"summary": "Unexpected error while running the tool",
                 "options": ["Try the request again"],
                 "auto_retry": "This is likely a temporary issue - you can "
                               "try the request again"},
                "INTERNAL_ERROR",
            )
            return _dumps(payload), True
    return _invoke


def _make_tool_function(name: str, description: str, schema: dict,
                        invoke: "Callable[[dict], tuple[str, bool]]",
                        ) -> Callable[..., str]:
    """One plain function for a tool: real signature and docstring from the
    schema, errors contained by the invoker; arguments the signature
    rejects come back as the guided envelope instead of raising."""
    import keyword

    properties: dict[str, Any] = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    _invoke = invoke

    params_usable = all(param.isidentifier() and not keyword.iskeyword(param)
                        and param != "_invoke"
                        for param in properties)
    if not params_usable:
        def inner(**kwargs: Any) -> str:
            return _invoke(kwargs)[0]
    else:
        ordered = ([p for p in properties if p in required]
                   + [p for p in properties if p not in required])
        rendered = ", ".join(
            p if p in required else f"{p}={properties[p].get('default')!r}"
            for p in ordered
        )
        args_literal = "{" + ", ".join(f"'{p}': {p}" for p in ordered) + "}"
        namespace: dict[str, Any] = {"_invoke": _invoke}
        exec(f"def _synthesized({rendered}):\n"
             f"    return _invoke({args_literal})[0]", namespace)
        inner = namespace["_synthesized"]
        # binding TypeErrors quote __qualname__, not __name__
        inner.__name__ = inner.__qualname__ = name or "tool"
        annotations: dict[str, Any] = {}
        for p in ordered:
            annotation = _annotation_for(properties[p])
            if p not in required and "default" not in properties[p]:
                # Absent-but-non-nullable params must admit None, or strict
                # schemas force the model to always send a value.
                annotation = Optional[annotation]
            annotations[p] = annotation
        annotations["return"] = str
        inner.__annotations__ = annotations

    def proxy(*args: Any, **kwargs: Any) -> str:
        # The invoker lets only 401/403 auth failures through, so a
        # TypeError here is the binding rejecting the arguments.
        try:
            return inner(*args, **kwargs)
        except TypeError as exc:
            payload, _ = _failure(
                f"Invalid arguments for {name}: {exc}", None,
                {"summary": "Invalid tool arguments",
                 "options": [f"Check the {name}() parameter names "
                             "and types"]},
                "INVALID_INPUT",
            )
            return _dumps(payload)
    proxy.__signature__ = inspect.signature(inner)  # type: ignore[attr-defined]
    proxy.__annotations__ = dict(inner.__annotations__)
    proxy.__name__ = proxy.__qualname__ = name or "tool"
    proxy.__doc__ = _tool_docstring(description or "", properties)
    return proxy


_BRIDGES: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()
_BRIDGES_LOCK = threading.Lock()


def _cloud_bridge(client, gated: bool = False):
    """One bridge per client and endpoint gate (``gated`` = the read-only
    ?tools=read endpoint); tool discovery and instructions share a session
    per gate. Weak-keyed off the instance so clients stay picklable; the
    lock closes the check-then-set race under concurrent first calls."""
    with _BRIDGES_LOCK:
        # A rotated api_key or moved BASE_URL rebuilds the bridges.
        auth = (client.BASE_URL, client.api_key)
        bridges, seen = _BRIDGES.get(client) or ({}, None)
        if seen != auth:
            bridges = {}
        bridge = bridges.get(gated)
        if bridge is None:
            from .mcp_bridge import McpBridge
            bridge = McpBridge(
                f"{auth[0]}/mcp" + ("?tools=read" if gated else ""),
                {"Authorization": f"Bearer {auth[1]}"},
            )
            bridges[gated] = bridge
            _BRIDGES[client] = (bridges, auth)
        return bridge


def _require_doc_selection(doc_ids) -> None:
    """An empty selection fails loud: washed to None it would mean
    "everything", while the tool-layer allowlist would mean "nothing"."""
    if doc_ids is not None and not doc_ids:
        raise PageIndexAPIError(
            "doc_id is empty. Pass one or more document IDs, or omit "
            "doc_id to give the agent the whole library.")


def _require_local_scope(client, doc_ids) -> None:
    """The allowlist is enforced in-process; cloud tools take none, so
    accepting doc_ids there would be advisory-only — refuse loudly."""
    _require_doc_selection(doc_ids)
    if doc_ids is not None and getattr(client, "api_key", None):
        raise PageIndexAPIError(
            "doc_ids scoping applies to local tools only — the managed "
            "cloud chat scopes doc_id server-side, and own-model chat "
            "over cloud documents targets documents at the prompt level, "
            "without a tool-layer allowlist."
        )


def _tool_specs(client, include_management: bool = False, doc_ids=None,
                ) -> "list[tuple[str, str, dict, Callable[[dict], tuple[str, bool]]]]":
    """(name, description, schema, invoke) per tool, for adapters that take
    the wire schema verbatim. ``invoke`` returns (envelope_text, is_error).
    Schemas are copies (frameworks keep the dict by reference). ``doc_ids``
    is the local chat scope."""
    _require_local_scope(client, doc_ids)
    if getattr(client, "api_key", None):
        bridge = _cloud_bridge(client, gated=not include_management)
        tools_meta = bridge.list_tools()
        if not tools_meta:
            raise PageIndexAPIError(
                "The MCP server returned no tools — a zero-tool agent would "
                "answer from the model's own knowledge, not the documents, "
                "with nothing to signal it."
            )
        return [(str(meta.get("name") or "tool"),
                 meta.get("description") or "",
                 copy.deepcopy(meta.get("inputSchema"))
                 or {"type": "object", "properties": {}},
                 _bridge_invoker(bridge, str(meta.get("name") or "tool"),
                                 meta.get("inputSchema") or {}))
                for meta in tools_meta]

    def local_invoke(name: str) -> "Callable[[dict], tuple[str, bool]]":
        def invoke(arguments: dict) -> tuple[str, bool]:
            return call_tool(client, name, arguments, doc_ids=doc_ids)
        return invoke

    return [(name, _local_description(name), _local_schema(name),
             local_invoke(name))
            for name in tool_names(include_management)]


def build_agent_tools(client, include_management: bool = False,
                      doc_ids=None) -> list[Callable[..., str]]:
    """Plain synchronous functions bound to `client`.

    Cloud: one function per tool of the live cloud MCP tool set, signatures
    synthesized from the server's schemas, calls proxied over MCP. Local:
    the built-in contract tools over the local store. Every function returns
    the JSON envelope as a string and never raises for arguments its
    signature accepts — except a cloud 401/403, which re-raises
    PageIndexAPIError (cloud-only parameters are absent from the local
    signatures; the call_tool path answers them with the guided envelope).
    ``doc_ids`` is the local allowlist, as in ``_tool_specs``.
    """
    return [_make_tool_function(name, description, schema, invoke)
            for name, description, schema, invoke
            in _tool_specs(client, include_management, doc_ids)]


# ── agent instructions ──

_INSTRUCTIONS_HEADER = (
    "中望建筑法规智能助手是一个建筑法规问答助手，知识库收录建筑行业规范条文"
    "（防火、给排水、暖通、电气等专业），用于回答条文相关的技术与设计问题。"
)

_READING_WORKFLOW = f"""\
阅读流程：
- 超过 {STRUCTURE_FIRST_PAGE_THRESHOLD} 页的文档：先调用 get_document_structure() 查看
  第一层章节列表（含每章概要与页范围）；对相关章节，把响应中的 node_id 传回同一工具
  逐层下钻，直到定位到目标条文的页号，再用 get_page_content() 读取该页范围的条文原文。
  每次调用只返回一层的节点列表。
- 小文档（{STRUCTURE_FIRST_PAGE_THRESHOLD} 页以内）：直接调用 get_page_content()。"""

_TOOL_USAGE_RULES = """\
工具使用规则：
- 只有在所有必需参数都已具备或可明确推断时才调用工具，绝不编造占位值。
- 工具返回错误时，把返回的 next_steps/options 告知用户，不要盲目重试。"""

_DISCOVERY = """\
文档发现：
- browse_documents() —— 默认的文档发现工具，任何涉及文档的问题优先调用它。
  它按时间倒序列出文档的名称和描述；将结果与用户意图匹配，
  如果 has_more 为 true，用 `offset: next_offset` 翻页继续。
- 召回完备性（硬性要求）：同一主题往往同时出现在多本规范中（强制性通用规范
  GB55xxx 与专门标准并存）。浏览后必须从列表中找出【所有】与问题主题相关的
  法规并逐一检索，宁多勿漏——不能因为在其中一本里找到了答案就停止。"""

_DECISION = """\
决策规则：
- 「我有哪些文档 / 列一下 / 最近的」→ browse_documents()
- 任何需要文档才能回答的问题（包括「找关于 Y 的那篇文档」）→ 先 browse_documents()，
  再从结果中挑出名称/描述与问题匹配的文档"""

_AFTER_DISCOVERY = """\
- 仅当问题与任何文档都不可能有关系时（例如「法国的首都是哪里」）才跳过文档发现。
- 发现之后：只有 1 个匹配或明显最佳匹配 → 直接阅读并回答，不必询问用户；
  多个同样相关 → 请用户选择。
- 返回结果 ≠ 正确结果。如果返回的文档与用户意图明显不符（主题、时期、文档类型不对），
  视同「未找到」，继续执行下面的 PERSISTENCE 协议。"""

_PERSISTENCE = """\
持久性协议（在得出目标文档不在库中的结论之前）：
本协议同时适用于「结果为空」和「有结果但都不匹配用户意图」两种情况。
不要一次发现失败就放弃，按顺序执行以下步骤：
1. browse_documents() 并将每条返回的名称/描述与用户意图比对
2. 用 `limit: 50` 和 `offset: next_offset` 翻完整库直到 has_more 为 false —— 必须完成，
   才允许得出「未找到」的结论
3. 宽松匹配复查：名称/描述中的同义词、缩写、部分标题都可能指向目标
只有三步全部尝试过，才允许得出文档不在库中的结论。不要退回通用知识作答 ——
如果用户的问题指向他们自己的文档，必须穷尽所有发现路径。"""

AGENT_INSTRUCTIONS = "\n\n".join([
    _INSTRUCTIONS_HEADER,
    _READING_WORKFLOW,
    _TOOL_USAGE_RULES,
    _DISCOVERY,
    _DECISION,
    _AFTER_DISCOVERY,
    _PERSISTENCE,
])


def _base_instructions(client, include_management: bool = False) -> str:
    """Cloud: the live instructions the MCP server serves for the tool set
    actually shipped. Local: the built-in subset instructions."""
    if not getattr(client, "api_key", None):
        return AGENT_INSTRUCTIONS
    instructions = _cloud_bridge(
        client, gated=not include_management).instructions()
    if not isinstance(instructions, str) or not instructions.strip():
        raise PageIndexAPIError(
            "The MCP server returned no agent instructions — refusing to "
            "substitute the SDK's local-subset guidance, which does not "
            "cover the cloud tool set."
        )
    return instructions


def doc_targeting_block(client, doc_id, scoped: bool = False) -> Optional[str]:
    """The doc_id targeting text: names, metadata, and the directive to work
    within those documents. Shared by agent_instructions and the local chat
    surfaces (a leading conversation item on the OpenAI surfaces, a system
    block on messages()). Raises when a doc_id's name is shadowed by a newer
    same-name document — the name-addressed tools could not reach it. With
    ``scoped`` (surfaces whose tools resolve names inside the doc_id
    allowlist) only a same-name duplicate within the targeted set
    shadows."""
    if doc_id is None:
        return None
    doc_ids = [doc_id] if isinstance(doc_id, str) else list(doc_id)
    _require_doc_selection(doc_ids)
    details = []
    missing = []
    for one_id in doc_ids:
        try:
            details.append(client.get_document(one_id))
        except PageIndexAPIError as exc:
            # Batch only a definite not-found/denied (local raises carry no
            # status); a cloud transport failure (429/5xx) propagates raw.
            if exc.status_code not in (None, 403, 404):
                raise
            missing.append(str(one_id))
    if missing:
        raise PageIndexAPIError(
            "Documents not found or access denied: " + ", ".join(missing))
    # Scoped: the listing only backfills the target docs' metadata (list
    # entries carry it, get_document does not — cloud parity), so paging
    # can stop at those ids. Unscoped needs it all for the shadow check.
    listing = _all_documents(client, stop_ids=doc_ids if scoped else None)
    documents = ([{**detail, "id": one_id}
                  for one_id, detail in zip(doc_ids, details)]
                 if scoped else listing)
    for one_id, detail in zip(doc_ids, details):
        entry, _ = _resolve_document(client, str(detail.get("name")),
                                     documents=documents)
        if entry is not None and entry.get("id") != one_id:
            raise PageIndexAPIError(
                f'Document "{detail.get("name")}" (doc_id: {one_id}) is '
                "shadowed by a newer document with the same name (doc_id: "
                f'{entry.get("id")}). The tools address documents by name '
                "and would read the newer one. Rename or remove the "
                "duplicate, or pass the newer doc_id."
            )
    by_id = {doc.get("id"): doc for doc in listing}
    for one_id, detail in zip(doc_ids, details):
        if detail.get("metadata") is None:
            tags = _flat_metadata(by_id.get(one_id, {}).get("metadata"))
            if tags is not None:
                detail["metadata"] = tags
    context = json.dumps(details, ensure_ascii=False)
    if len(details) == 1:
        return (
            f"The user has specified document: {details[0].get('name')}\n"
            f"Document metadata: {context}\n"
            "Use this document's name to retrieve its content with "
            "get_document_structure() and get_page_content()."
        )
    names = ", ".join(str(item.get("name")) for item in details)
    return (
        f"The user has specified documents: {names}\n"
        f"Documents metadata: {context}\n"
        "Use these documents' names to retrieve their content with "
        "get_document_structure() and get_page_content()."
    )


def build_agent_instructions(client, doc_id=None, scoped: bool = False,
                             include_management: bool = False) -> str:
    """Orchestration guidance for document QA agents; with doc_id, appends
    the target documents and directs the agent to work within them."""
    base = _base_instructions(client, include_management)
    block = doc_targeting_block(client, doc_id, scoped=scoped)
    return base if block is None else base + "\n\n" + block
