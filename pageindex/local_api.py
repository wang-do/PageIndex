"""Local implementation of the PageIndex SDK surface."""
from __future__ import annotations

import json
import logging
import multiprocessing
import os
import re
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from .errors import PageIndexAPIError
from .local_store import DocStore
from .utils import run_off_loop

logger = logging.getLogger(__name__)

_SURROGATES = re.compile("[\ud800-\udfff]")


def _scrub_surrogates(text: str) -> str:
    """Lone surrogates (surrogateescape'd names, PyPDF2's surrogatepass
    decodes) cannot encode to UTF-8; replace with U+FFFD."""
    return _SURROGATES.sub("\ufffd", text)


def _now_iso() -> str:
    """Naive UTC, millisecond precision."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return now.replace(microsecond=now.microsecond // 1000 * 1000).isoformat()




class LocalAPI:
    """Backs PageIndexClient's local mode. One instance per client."""

    def __init__(self, storage_path: str, model: str, summary_model: str,
                 index_backend: dict | None = None):
        self._store = DocStore(storage_path)
        self._model = model
        self._summary_model = summary_model
        self._index_backend = index_backend
        from .utils import ConfigLoader
        self._config_loader = ConfigLoader()

    def _with_backend(self, func, *args):
        """Scope the indexing lane's connection overrides around one
        operation — runs inside whatever thread run_off_loop picked."""
        from .utils import _llm_backend
        token = _llm_backend.set(self._index_backend)
        try:
            return func(*args)
        finally:
            _llm_backend.reset(token)

    # ── indexing ──

    def submit_document(
        self,
        file_path: str,
        mode: str | None = None,
        beta_headers: list[str] | None = None,
        folder_id: str | None = None,
        metadata: dict | None = None,
    ) -> dict[str, Any]:
        if getattr(multiprocessing.current_process(), "_inheriting", False):
            raise PageIndexAPIError(
                "Failed to submit document: called again while a spawned worker "
                "process was importing your script. Put your top-level code under "
                "if __name__ == '__main__': so worker processes do not re-run it."
            )
        if beta_headers is not None:
            raise PageIndexAPIError(
                "Failed to submit document: beta_headers is not supported in local mode."
            )
        if folder_id is not None:
            raise PageIndexAPIError(
                "Failed to submit document: folders are not supported in local mode."
            )
        if metadata is not None:
            if not isinstance(metadata, dict):
                raise PageIndexAPIError(
                    "Failed to submit document: metadata must be a dict."
                )
            try:
                json.dumps(metadata, allow_nan=False)
            except (TypeError, ValueError) as e:
                raise PageIndexAPIError(
                    f"Failed to submit document: metadata must be valid JSON. {e}"
                ) from e
        if mode not in (None, "standard", "flash"):
            raise PageIndexAPIError(
                f"Failed to submit document: unknown local processing mode {mode!r}. "
                "Supported: 'flash' (default) or 'standard'."
            )
        if mode is None:
            mode = "flash"
        file_path = os.path.abspath(os.path.expanduser(str(file_path)))
        if not os.path.isfile(file_path):
            raise FileNotFoundError(f"No such file: {file_path}")
        if not file_path.lower().endswith(".pdf"):
            raise PageIndexAPIError(
                "Failed to submit document: only PDF files are supported in local mode."
            )

        try:
            page_texts = self._extract_page_texts(file_path)
        except PageIndexAPIError:
            raise
        except Exception as e:
            raise PageIndexAPIError(
                f"Failed to submit document: could not read PDF: {e}"
            ) from e
        if not any(text.strip() for text in page_texts):
            raise PageIndexAPIError(
                "Failed to submit document: PDF has no content. All pages are blank."
            )
        # Surrogates from a surrogateescape'd filesystem name would be
        # mangled by the store's errors="replace" write; scrub now so the
        # returned name is byte-for-byte the stored name.
        doc_name = _scrub_surrogates(os.path.basename(file_path))
        self._unique_doc_name(doc_name)

        try:
            if mode == "flash":
                structure, description = run_off_loop(
                    self._with_backend, self._index_flash, file_path
                )
            else:
                structure, description = run_off_loop(
                    self._with_backend, self._index_standard, file_path,
                    page_texts
                )
        except PageIndexAPIError:
            raise
        except Exception as e:
            raise PageIndexAPIError(f"Failed to submit document: {e}") from e
        self._check_page_bounds(structure, len(page_texts))

        doc_id = "pi-" + uuid.uuid4().hex
        pages = [{"page_index": i + 1, "markdown": text}
                 for i, text in enumerate(page_texts)]
        from .utils import remove_fields
        # Check-then-write under the store lock; the early pre-check above
        # is advisory only.
        with self._store.lock():
            meta = {
                "id": doc_id,
                "name": self._unique_doc_name(doc_name),
                "description": description,
                "status": "completed",
                "createdAt": _now_iso(),
                "pageNum": len(page_texts),
                "folderId": None,
                "metadata": metadata,
                "mode": mode,
            }
            self._store.save_document(
                doc_id, meta, remove_fields(structure, fields=["text"]), pages)
        return {"doc_id": doc_id, "name": meta["name"]}

    def submit_structure_json(
        self,
        json_path: str,
        metadata: dict | None = None,
    ) -> dict[str, Any]:
        """导入已有的 PageIndex 树和页面原文，返回本地文档标识。"""
        json_path = os.path.abspath(os.path.expanduser(str(json_path)))
        if not os.path.isfile(json_path):
            raise FileNotFoundError(f"No such file: {json_path}")
        if not json_path.lower().endswith(".json"):
            raise PageIndexAPIError(
                "Failed to import structure JSON: input must be a JSON file."
            )
        try:
            with open(json_path, "r", encoding="utf-8") as file:
                document = json.load(file)
        except (OSError, json.JSONDecodeError) as exc:
            raise PageIndexAPIError(
                f"Failed to import structure JSON: could not read JSON: {exc}"
            ) from exc
        if not isinstance(document, dict):
            raise PageIndexAPIError(
                "Failed to import structure JSON: root must be an object."
            )

        structure = deepcopy(document.get("structure"))
        pages = document.get("pages")
        doc_name = document.get("doc_name")
        if not isinstance(doc_name, str) or not doc_name.strip():
            raise PageIndexAPIError(
                "Failed to import structure JSON: doc_name must be a non-empty string."
            )
        if not isinstance(structure, list) or not structure:
            raise PageIndexAPIError(
                "Failed to import structure JSON: structure must be a non-empty array."
            )
        normalized_pages = self._validate_import_pages(pages)
        self._validate_import_structure(structure, len(normalized_pages))

        document_metadata = metadata if metadata is not None else document.get("metadata")
        self._validate_metadata(document_metadata)
        description = document.get("doc_description")
        if description is not None and not isinstance(description, str):
            raise PageIndexAPIError(
                "Failed to import structure JSON: doc_description must be a string."
            )

        doc_id = "pi-" + uuid.uuid4().hex
        from .utils import remove_fields
        with self._store.lock():
            stored_name = self._unique_doc_name(_scrub_surrogates(doc_name.strip()))
            meta = {
                "id": doc_id,
                "name": stored_name,
                "description": description,
                "status": "completed",
                "createdAt": _now_iso(),
                "pageNum": len(normalized_pages),
                "folderId": None,
                "metadata": document_metadata,
                "mode": "imported_json",
            }
            self._store.save_document(
                doc_id, meta, remove_fields(structure, fields=["text"]),
                normalized_pages,
            )
        return {"doc_id": doc_id, "name": stored_name}

    @staticmethod
    def _validate_metadata(metadata: dict | None) -> None:
        """验证用户元数据可被本地 JSON 存储。"""
        if metadata is None:
            return
        if not isinstance(metadata, dict):
            raise PageIndexAPIError("metadata must be a dict.")
        try:
            json.dumps(metadata, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise PageIndexAPIError(f"metadata must be valid JSON. {exc}") from exc

    @staticmethod
    def _validate_import_pages(pages: Any) -> list[dict[str, Any]]:
        """验证并规范化导入 JSON 的连续页面原文。"""
        if not isinstance(pages, list) or not pages:
            raise PageIndexAPIError(
                "Failed to import structure JSON: pages must be a non-empty array."
            )
        normalized: list[dict[str, Any]] = []
        for expected_index, page in enumerate(pages, start=1):
            if not isinstance(page, dict):
                raise PageIndexAPIError(
                    "Failed to import structure JSON: every page must be an object."
                )
            page_index = page.get("page_index")
            markdown = page.get("markdown")
            if page_index != expected_index or isinstance(page_index, bool):
                raise PageIndexAPIError(
                    "Failed to import structure JSON: pages must use consecutive "
                    "page_index values starting at 1."
                )
            if not isinstance(markdown, str):
                raise PageIndexAPIError(
                    "Failed to import structure JSON: every page needs string markdown."
                )
            normalized.append({"page_index": page_index, "markdown": markdown})
        return normalized

    @classmethod
    def _validate_import_structure(cls, structure: list, page_count: int) -> None:
        """验证导入树的节点字段、节点 ID 和页码范围。"""
        seen_ids: set[str] = set()
        stack = list(structure)
        while stack:
            node = stack.pop()
            if not isinstance(node, dict):
                raise PageIndexAPIError(
                    "Failed to import structure JSON: every structure node must be an object."
                )
            node_id = node.get("node_id")
            title = node.get("title")
            start = node.get("start_index")
            end = node.get("end_index")
            if not isinstance(node_id, str) or not node_id.strip():
                raise PageIndexAPIError(
                    "Failed to import structure JSON: every node needs non-empty node_id."
                )
            if node_id in seen_ids:
                raise PageIndexAPIError(
                    f"Failed to import structure JSON: duplicate node_id {node_id!r}."
                )
            seen_ids.add(node_id)
            if not isinstance(title, str) or not title.strip():
                raise PageIndexAPIError(
                    "Failed to import structure JSON: every node needs non-empty title."
                )
            if (isinstance(start, bool) or isinstance(end, bool)
                    or not isinstance(start, int) or not isinstance(end, int)
                    or start < 1 or end < start or end > page_count):
                raise PageIndexAPIError(
                    "Failed to import structure JSON: node page ranges must fall "
                    f"within 1-{page_count}."
                )
            if "summary" in node and not isinstance(node["summary"], str):
                raise PageIndexAPIError(
                    "Failed to import structure JSON: node summary must be a string."
                )
            children = node.get("nodes", [])
            if not isinstance(children, list):
                raise PageIndexAPIError(
                    "Failed to import structure JSON: node nodes must be an array."
                )
            stack.extend(children)

    def _unique_doc_name(self, name: str) -> str:
        """Mirror the cloud upload: a taken name gets _1.._99 appended,
        beyond that the submit is rejected."""
        taken = {meta.get("name") for meta in self._store.list_metas()}
        if name not in taken:
            return name
        base, ext = os.path.splitext(name)
        for num in range(1, 100):
            candidate = f"{base}_{num}{ext}"
            if candidate not in taken:
                return candidate
        raise PageIndexAPIError(
            "Failed to submit document: Too many files with similar names. "
            "Please use a different file name."
        )

    @staticmethod
    def _check_page_bounds(structure: list, page_count: int) -> None:
        """The tree (pdfium) and stored pages (PyPDF2) come from different
        parsers; a span outside 1..page_count IndexErrors every later read."""
        stack = list(structure)
        while stack:
            node = stack.pop()
            start, end = node.get("start_index"), node.get("end_index")
            if (start is not None and end is not None
                    and not (1 <= start and end <= page_count)):
                raise PageIndexAPIError(
                    f"Failed to submit document: the extracted structure "
                    f"references pages {start}-{end} outside the PDF's "
                    f"{page_count} readable pages."
                )
            stack.extend(node.get("nodes") or [])

    @staticmethod
    def _extract_page_texts(file_path: str) -> list[str]:
        import PyPDF2
        with open(file_path, "rb") as f:
            reader = PyPDF2.PdfReader(f)
            # PyPDF2 decodes broken ToUnicode maps with surrogatepass; lone
            # surrogates would crash every utf-8 JSON save downstream.
            return [_scrub_surrogates(page.extract_text() or "")
                    for page in reader.pages]

    def _index_standard(self, file_path: str, page_texts: list[str]) -> tuple[list, str | None]:
        from .page_index_classic import page_index_main
        import litellm
        page_list = [(text, litellm.token_counter(model=self._model, text=text))
                     for text in page_texts]
        opt = self._config_loader.load({
            "model": self._model,
            "summary_model": self._summary_model,
            "if_add_node_id": "yes",
            "if_add_node_summary": "yes",
            "if_add_node_text": "yes",
            "if_add_doc_description": "yes",
        })
        result = page_index_main(file_path, opt, logger=logger, page_list=page_list)
        structure = result.get("structure") or []
        if not structure:
            raise PageIndexAPIError(
                "Failed to submit document: standard indexing produced no structure."
            )
        return structure, result.get("doc_description")

    def _index_flash(self, file_path: str) -> tuple[list, str | None]:
        from .flash import page_index_flash
        from .utils import (create_clean_structure_for_description,
                            generate_doc_description, write_node_id)
        result = page_index_flash(file_path, summary=True,
                                  summary_model=self._summary_model,
                                  optimize="full",
                                  optimize_model=self._summary_model)
        structure = result.get("structure", [])
        if not structure:
            raise PageIndexAPIError(
                "Failed to submit document: PageIndex Flash could not extract "
                "a structure from this PDF. Try mode='standard', which builds "
                "the structure with the model."
            )
        write_node_id(structure)
        description = generate_doc_description(
            create_clean_structure_for_description(structure),
            model=self._summary_model,
        )
        return structure, description

    # ── tree / ocr ──

    def _load_tree_with_text(self, doc_id: str, error_prefix: str) -> list:
        from .utils import add_node_text
        structure = self._require_data(
            self._store.get_tree(doc_id), error_prefix)
        pages = self._require_pages(doc_id, error_prefix)
        pdf_pages = [(p.get("markdown", ""), 0) for p in pages]
        add_node_text(structure, pdf_pages)
        return structure

    def raw_tree(self, doc_id: str) -> list | None:
        """Stored tree verbatim — keeps start_index/end_index, which
        get_tree's cloud wire shape renames and drops."""
        return self._store.get_tree(doc_id)

    def get_tree(self, doc_id: str, node_summary: bool = False,
                 include_text: bool = True) -> dict[str, Any]:
        meta = self._require_doc(doc_id, "Failed to get tree result")
        if include_text:
            structure = self._load_tree_with_text(doc_id, "Failed to get tree result")
        else:
            structure = self._require_data(
                self._store.get_tree(doc_id), "Failed to get tree result")
        result = [_format_tree_node(node, node_summary) for node in structure]
        return self._completed_envelope(doc_id, result, meta)

    def get_ocr(self, doc_id: str, format: str = "page") -> dict[str, Any]:
        if format not in ["page", "node", "raw"]:
            raise ValueError("Format parameter must be 'page', 'node', or 'raw'")
        meta = self._require_doc(doc_id, "Failed to get OCR result")
        if format == "node":
            result: Any = []
            def _walk(nodes, level):
                for node in nodes:
                    result.append({
                        "title": node.get("title", ""),
                        "level": level,
                        "page_index": node.get("start_index"),
                        "text": node.get("text", ""),
                    })
                    _walk(node.get("nodes") or [], level + 1)
            _walk(self._load_tree_with_text(doc_id,
                                           "Failed to get OCR result"), 1)
        else:
            pages = self._require_pages(doc_id, "Failed to get OCR result")
            if format == "page":
                result = pages
            else:  # raw
                result = "\n\n".join(p.get("markdown", "") for p in pages)
        return self._completed_envelope(doc_id, result, meta)

    @staticmethod
    def _require_data(data, error_prefix: str):
        if data is None:
            raise PageIndexAPIError(f"{error_prefix}: stored document data is unreadable.")
        return data

    def _require_pages(self, doc_id: str, error_prefix: str) -> list:
        pages = self._require_data(self._store.get_pages(doc_id), error_prefix)
        if not pages:
            raise PageIndexAPIError(
                f"{error_prefix}: stored document has no page content.")
        return pages

    def _completed_envelope(self, doc_id: str, result, meta: dict) -> dict[str, Any]:
        return {
            "doc_id": doc_id,
            "status": "completed",
            "retrieval_ready": True,
            "result": result,
            "metadata": meta.get("metadata"),
            "features": {},
        }

    # ── document management ──

    def _require_doc(self, doc_id: str, error_prefix: str) -> dict:
        meta = self._store.get_meta(doc_id)
        if meta is None:
            raise PageIndexAPIError(f"{error_prefix}: Document not found.")
        return meta

    def get_document(self, doc_id: str) -> dict[str, Any]:
        meta = self._store.get_meta(doc_id)
        if meta is None:
            raise PageIndexAPIError("Failed to get document metadata: Document not found")
        return {key: meta.get(key) for key in
                ("id", "name", "description", "status", "createdAt", "pageNum", "folderId")}

    def delete_document(self, doc_id: str) -> dict[str, Any]:
        if not self._store.delete_document(doc_id):
            raise PageIndexAPIError("Failed to delete document: Document not found.")
        return {"message": "Document deleted successfully."}

    def list_documents(
        self,
        limit: int = 50,
        offset: int = 0,
        folder_id: str | None = None,
    ) -> dict[str, Any]:
        if limit < 1 or limit > 100:
            raise ValueError("limit must be between 1 and 100")
        if offset < 0:
            raise ValueError("offset must be non-negative")
        if folder_id is not None:
            raise PageIndexAPIError(
                "Failed to list documents: folders are not supported in local mode."
            )
        metas = sorted(self._store.list_metas(), key=lambda m: m.get("id") or "")
        metas.sort(key=lambda m: m.get("createdAt") or "", reverse=True)
        documents = [{
            "id": m.get("id"),
            "name": m.get("name"),
            "description": m.get("description"),
            "status": m.get("status"),
            "createdAt": m.get("createdAt"),
            "pageNum": m.get("pageNum", 0),
            "folderId": None,
            "metadata": m.get("metadata"),
            "features": {},
        } for m in metas[offset:offset + limit]]
        return {
            "documents": documents,
            "total": len(metas),
            "limit": limit,
            "offset": offset,
        }


def _format_tree_node(node: dict, node_summary: bool) -> dict:
    children = node.get("nodes") or []
    out = {
        "title": node.get("title", ""),
        "node_id": node.get("node_id"),
        "page_index": node.get("start_index"),
    }
    if node.get("key_items"):
        out["key_items"] = node["key_items"]
    if node_summary:
        summary = node.get("summary")
        if summary is not None:
            if children:
                out["prefix_summary"] = summary
            else:
                out["summary"] = summary
    if "text" in node:
        out["text"] = node["text"]
    if children:
        out["nodes"] = [_format_tree_node(child, node_summary) for child in children]
    return out
