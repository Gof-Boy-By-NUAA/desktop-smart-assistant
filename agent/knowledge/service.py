"""
Knowledge service for handling knowledge base operations.

Provides a unified interface for listing, reading, and graphing knowledge files,
callable from the web console, API, or CLI.

Knowledge file layout (under workspace_root):
    knowledge/index.md
    knowledge/log.md
    knowledge/<category>/<slug>.md
"""

import os
import posixpath
import re
import asyncio
import hashlib
import json
import threading
from pathlib import Path
from typing import Optional, Iterable
from urllib.parse import quote

from common.log import logger
from config import conf
from agent.memory.config import MemoryConfig
from agent.memory.manager import MemoryManager
from agent.memory.governance import IdentityContext, MemoryScope, Sensitivity
from agent.knowledge.contracts import KnowledgeWriteCommand
from agent.knowledge.runtime import GovernedKnowledgeRuntime


class KnowledgeService:
    """
    High-level service for knowledge base queries.
    Operates directly on the filesystem.
    """

    PROTECTED_FILES = {"index.md", "log.md"}
    INVALID_NAME_RE = re.compile(r'[<>:"|?*\x00-\x1f]')
    IMPORT_EXTENSIONS = {".md", ".txt"}
    MAX_IMPORT_FILES = 100
    MAX_IMPORT_FILE_SIZE = 10 * 1024 * 1024
    MAX_IMPORT_TOTAL_SIZE = 200 * 1024 * 1024

    def __init__(
        self,
        workspace_root: str,
        memory_manager=None,
        identity=None,
        knowledge_runtime=None,
        session_id: Optional[str] = None,
    ):
        self.workspace_root = os.path.abspath(workspace_root)
        self.knowledge_dir = os.path.join(self.workspace_root, "knowledge")
        self._memory_manager = memory_manager
        self.session_id = session_id
        tenant_id = MemoryConfig(workspace_root=self.workspace_root).tenant_id
        self.identity = identity or IdentityContext(
            tenant_id=tenant_id,
            actor_user_id="local-user",
            roles=frozenset({"knowledge:write_shared"}),
            trace_id="knowledge-local-service",
            auth_source="smart-assistant-local-knowledge-service",
        )
        self._knowledge_runtime = knowledge_runtime
        self._owns_knowledge_runtime = knowledge_runtime is None

    def runtime(self) -> GovernedKnowledgeRuntime:
        """延迟创建知识运行时，纯列表和图谱请求无需打开数据库。"""

        if self._knowledge_runtime is None:
            self._knowledge_runtime = GovernedKnowledgeRuntime(
                self.workspace_root,
                tenant_id=self.identity.tenant_id,
            )
        return self._knowledge_runtime

    def resolve_citation(self, uri: str) -> dict:
        """Resolve a v3 citation using only the identity built at the trust boundary."""

        from dataclasses import asdict

        citation = self.runtime().resolve_verified_citation(
            self.identity, uri, session_id=self.session_id
        )
        return asdict(citation)

    def close(self):
        """释放此服务自行创建的知识检索连接。"""

        if self._owns_knowledge_runtime and self._knowledge_runtime is not None:
            self._knowledge_runtime.close()
            self._knowledge_runtime = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def _resolve_path(self, rel_path: str, *, kind: Optional[str] = None,
                      allow_missing: bool = True) -> tuple:
        if not isinstance(rel_path, str) or not rel_path.strip():
            raise ValueError("path is required")
        rel_path = rel_path.replace("\\", "/").strip("/")
        parts = rel_path.split("/")
        if any(not p or p in (".", "..") or self.INVALID_NAME_RE.search(p) for p in parts):
            raise ValueError("invalid path")
        if kind == "document" and not rel_path.lower().endswith(".md"):
            raise ValueError("document path must end with .md")

        root = Path(self.knowledge_dir).resolve()
        candidate = root.joinpath(*parts)
        # Resolve the nearest existing ancestor so a symlink cannot be used
        # to escape when the final destination does not exist yet.
        ancestor = candidate
        while not ancestor.exists() and ancestor != root:
            ancestor = ancestor.parent
        try:
            ancestor.resolve().relative_to(root)
        except ValueError:
            raise ValueError("path outside knowledge dir")
        if candidate.exists():
            try:
                candidate.resolve().relative_to(root)
            except ValueError:
                raise ValueError("path outside knowledge dir")
        elif not allow_missing:
            raise FileNotFoundError(f"path not found: {rel_path}")
        return rel_path, candidate

    def _ensure_not_protected(self, rel_path: str):
        if rel_path in self.PROTECTED_FILES:
            raise ValueError(f"protected knowledge file: {rel_path}")

    def _manager(self):
        if self._memory_manager is None:
            # Reuse the shared embedding provider selection so knowledge index
            # sync gets vectors too, instead of degrading to keyword-only.
            from agent.memory.embedding import create_default_embedding_provider
            embedding_provider = create_default_embedding_provider()
            self._memory_manager = MemoryManager(
                MemoryConfig(workspace_root=self.workspace_root),
                embedding_provider=embedding_provider,
            )
        return self._memory_manager

    @staticmethod
    def _run_sync(coro):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)
        result = []
        error = []

        def runner():
            try:
                result.append(asyncio.run(coro))
            except Exception as exc:
                error.append(exc)

        thread = threading.Thread(target=runner)
        thread.start()
        thread.join()
        if error:
            raise error[0]
        return result[0] if result else None

    def _sync_index(self, old_paths: Iterable[str], force: bool = False):
        old_paths = sorted(set(old_paths))
        if not old_paths and not force:
            return
        manager = self._manager()
        for rel_path in old_paths:
            manager.storage.delete_by_path(f"knowledge/{rel_path}")
        manager.mark_dirty()
        self._run_sync(manager.sync())

    @staticmethod
    def _extract_title(md_path: Path, fallback: str) -> str:
        """Read a markdown file's H1 title, falling back to the file stem."""
        try:
            with open(md_path, "r", encoding="utf-8") as f:
                for _ in range(20):
                    line = f.readline()
                    if not line:
                        break
                    stripped = line.strip()
                    if stripped.startswith("# "):
                        return stripped[2:].strip() or fallback
        except Exception:
            pass
        return fallback

    def rebuild_index_md(self) -> bool:
        """Regenerate knowledge/index.md from the actual directory tree.

        Keeps the index in sync with real files so it never drifts or loses
        documents. Returns True when the file was (re)written.
        """
        root = Path(self.knowledge_dir)
        if not root.is_dir():
            return False
        runtime = self.runtime()
        allowed_paths = {
            record.projection_path
            for record in runtime.list_active(
                self.identity, session_id=self.session_id
            )
            if runtime.is_compatibility_projection(record)
            and record.projection_path
        }

        def collect(dir_path: Path) -> list:
            # Return sorted (rel_path, title) tuples for *.md under dir_path,
            # excluding protected files at the knowledge root and dot files.
            entries = []
            for md in sorted(dir_path.rglob("*.md")):
                rel = md.relative_to(root).as_posix()
                if any(part.startswith(".") for part in md.relative_to(root).parts):
                    continue
                if rel in self.PROTECTED_FILES:
                    continue
                if rel not in allowed_paths:
                    continue
                entries.append((rel, self._extract_title(md, md.stem)))
            return entries

        all_entries = collect(root)

        def link(rel: str) -> str:
            # Encode each path segment so spaces / special chars stay valid in
            # markdown links, while keeping the slashes between segments.
            encoded = "/".join(quote(part) for part in rel.split("/"))
            return f"./{encoded}"

        lines = ["# 知识库目录", ""]
        # Root-level documents first (no category dir).
        root_docs = [(rel, title) for rel, title in all_entries if "/" not in rel]
        for rel, title in root_docs:
            lines.append(f"- [{title}]({link(rel)})")
        if root_docs:
            lines.append("")

        # Group remaining documents by their top-level category.
        categories = {}
        for rel, title in all_entries:
            if "/" not in rel:
                continue
            category = rel.split("/", 1)[0]
            categories.setdefault(category, []).append((rel, title))

        for category in sorted(categories.keys()):
            lines.append(f"## {category}")
            for rel, title in categories[category]:
                lines.append(f"- [{title}]({link(rel)})")
            lines.append("")

        content = "\n".join(lines).rstrip() + "\n"
        index_path = root / "index.md"
        try:
            index_path.write_text(content, encoding="utf-8")
            return True
        except Exception as exc:
            logger.warning(f"[KnowledgeService] Failed to rebuild index.md: {exc}")
            return False

    def _sanitize_document_name(self, filename: str) -> str:
        name = os.path.basename((filename or "").replace("\\", "/")).strip()
        if not name:
            raise ValueError("filename is required")
        stem, ext = os.path.splitext(name)
        if ext.lower() not in self.IMPORT_EXTENSIONS:
            raise ValueError(f"unsupported file type: {ext or name}")
        if not stem or stem in (".", "..") or self.INVALID_NAME_RE.search(stem):
            raise ValueError("invalid filename")
        safe_name = f"{stem}.md"
        self._ensure_not_protected(safe_name)
        return safe_name

    @staticmethod
    def _decode_document_content(content) -> str:
        if isinstance(content, str):
            return content
        if not isinstance(content, (bytes, bytearray)):
            raise ValueError("document content is required")
        try:
            return bytes(content).decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise ValueError("document content must be valid UTF-8") from error

    def _knowledge_scope(self) -> tuple:
        """根据可信身份选择 Web/CLI 兼容写入的默认可见范围。"""

        if self.identity.has_any_role("admin", "knowledge:write_shared"):
            return MemoryScope.SHARED, Sensitivity.INTERNAL
        return MemoryScope.USER, Sensitivity.PRIVATE

    def _visible_records_by_path(self) -> dict:
        """按当前身份的路径优先级折叠事实库记录。"""

        records = {}
        priorities = {}
        runtime = self.runtime()
        for record in runtime.list_active(
            self.identity, session_id=self.session_id
        ):
            if not record.projection_path:
                continue
            key = runtime.repository.projection_key(record.projection_path)
            if (
                record.scope is MemoryScope.SESSION
                and record.owner_user_id == self.identity.actor_user_id
                and record.session_id == self.session_id
            ):
                priority = 0
            elif (
                record.scope is MemoryScope.USER
                and record.owner_user_id == self.identity.actor_user_id
            ):
                priority = 1
            elif record.scope is MemoryScope.SHARED:
                priority = 2
            else:
                priority = 3
            candidate = (priority, record.document_id)
            if key not in priorities or candidate < priorities[key]:
                priorities[key] = candidate
                records[key] = record
        return records

    def _find_record(self, rel_path: str):
        return self.runtime().find_by_logical_path(
            self.identity, rel_path, session_id=self.session_id
        )

    def _visible_category_paths(self) -> set:
        runtime = self.runtime()
        categories = set(
            runtime.list_categories(self.identity, session_id=self.session_id)
        )
        for record in self._visible_records_by_path().values():
            parts = record.projection_path.split("/")[:-1]
            categories.update(
                "/".join(parts[:index]) for index in range(1, len(parts) + 1)
            )
        return categories

    def _category_exists(self, rel_path: str) -> bool:
        key = self.runtime().repository.projection_key(rel_path)
        return any(
            self.runtime().repository.projection_key(path) == key
            for path in self._visible_category_paths()
        )

    @staticmethod
    def _idempotency_key(operation: str, payload: dict) -> str:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return "%s-%s" % (operation, hashlib.sha256(encoded).hexdigest())

    def _write_governed_document(
        self,
        rel_path: str,
        content: str,
        document_id: Optional[str] = None,
        template=None,
    ):
        """把页面写入事实库，由运行时生成 Markdown 兼容投影。"""

        return self.runtime().write(
            self.identity,
            self._build_governed_write_command(
                rel_path, content, document_id, template=template
            ),
        )

    def _build_governed_write_command(
        self,
        rel_path: str,
        content: str,
        document_id: Optional[str] = None,
        template=None,
    ) -> KnowledgeWriteCommand:
        """构造单条和批量导入共用的受治理知识写入命令。"""

        title = self._extract_title(Path(self.knowledge_dir) / rel_path, Path(rel_path).stem)
        for line in content.splitlines()[:20]:
            if line.strip().startswith("# "):
                title = line.strip()[2:].strip() or title
                break
        collection_id = rel_path.split("/", 1)[0] if "/" in rel_path else "root"
        if template is None:
            scope, sensitivity = self._knowledge_scope()
            owner_user_id = None
            bound_session_id = (
                self.session_id if scope is MemoryScope.SESSION else None
            )
            metadata = {"ingress": "knowledge-service"}
        else:
            scope, sensitivity = template.scope, template.sensitivity
            owner_user_id = template.owner_user_id
            bound_session_id = template.session_id
            metadata = dict(template.metadata)
        payload = {
            "actor_user_id": self.identity.actor_user_id,
            "document_id": document_id,
            "path": rel_path,
            "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "scope": scope.value,
        }
        return KnowledgeWriteCommand(
            content=content,
            title=title,
            source_ref="knowledge/%s" % rel_path,
            collection_id=collection_id,
            idempotency_key=self._idempotency_key("knowledge-write", payload),
            document_id=document_id,
            projection_path=rel_path,
            scope=scope,
            owner_user_id=owner_user_id,
            session_id=bound_session_id,
            sensitivity=sensitivity,
            metadata=metadata,
        )

    def _resolve_import_destination(self, target_category: str, filename: str,
                                    conflict_strategy: str,
                                    reserved_paths=None) -> tuple:
        target_rel, target_full = self._resolve_path(target_category, kind="category")
        if not self._category_exists(target_rel):
            raise FileNotFoundError(f"category not found: {target_rel}")

        reserved_paths = reserved_paths or set()
        safe_name = self._sanitize_document_name(filename)
        destination = target_full / safe_name
        rel_path = f"{target_rel}/{safe_name}"
        path_key = self.runtime().repository.projection_key(rel_path)

        if self._find_record(rel_path) is not None or path_key in reserved_paths:
            if conflict_strategy == "skip":
                return rel_path, destination, "skip"
            if conflict_strategy == "rename":
                stem = destination.stem
                suffix = destination.suffix
                for index in range(1, 1000):
                    candidate = target_full / f"{stem}-{index}{suffix}"
                    candidate_rel = f"{target_rel}/{candidate.name}"
                    candidate_key = self.runtime().repository.projection_key(
                        candidate_rel
                    )
                    if (
                        self._find_record(candidate_rel) is None
                        and candidate_key not in reserved_paths
                    ):
                        return candidate_rel, candidate, "write"
                raise FileExistsError(f"target already exists: {rel_path}")
            if conflict_strategy != "overwrite":
                raise ValueError("invalid conflict strategy")
        return rel_path, destination, "write"

    def create_document(self, path: str, content: str = "", overwrite: bool = False) -> dict:
        rel_path, _ = self._resolve_path(path, kind="document")
        self._ensure_not_protected(rel_path)
        if len((content or "").encode("utf-8")) > self.MAX_IMPORT_FILE_SIZE:
            raise ValueError("file too large")
        existing = self._find_record(rel_path)
        if existing is not None and not overwrite:
            raise FileExistsError(f"target already exists: {rel_path}")
        old_paths = [rel_path] if existing is not None else []
        record = self._write_governed_document(
            rel_path,
            content or "",
            document_id=existing.document_id if existing else None,
        )
        # Keep index.md in sync before reindexing so it is indexed too.
        self.rebuild_index_md()
        self._sync_index(old_paths, force=True)
        return {
            "path": rel_path,
            "created": True,
            "overwritten": existing is not None,
            "document_id": record.document_id,
            "version": record.version,
            "content_hash": record.content_hash,
        }

    def import_documents(self, target_category: str, files: Iterable[dict],
                         conflict_strategy: str = "skip") -> dict:
        if not isinstance(files, list):
            raise ValueError("files must be a list")
        if len(files) > self.MAX_IMPORT_FILES:
            raise ValueError(f"too many files: max {self.MAX_IMPORT_FILES}")
        results = []
        old_paths = []
        prepared = []
        reserved_paths = set()
        skipped = failed = 0
        total_size = 0

        for item in files:
            filename = item.get("filename") if isinstance(item, dict) else None
            try:
                content_bytes = item.get("content") if isinstance(item, dict) else None
                size = len(content_bytes.encode("utf-8")) if isinstance(content_bytes, str) else len(content_bytes or b"")
                total_size += size
                if total_size > self.MAX_IMPORT_TOTAL_SIZE:
                    raise ValueError("import batch too large")
                if size > self.MAX_IMPORT_FILE_SIZE:
                    raise ValueError("file too large")
                rel_path, destination, mode = self._resolve_import_destination(
                    target_category,
                    filename,
                    conflict_strategy,
                    reserved_paths=reserved_paths,
                )
                if mode == "skip":
                    skipped += 1
                    results.append({"filename": filename, "path": rel_path, "status": "skipped",
                                    "reason": "target_exists"})
                    continue

                existing = self._find_record(rel_path)
                old_exists = existing is not None
                content = self._decode_document_content(content_bytes)
                command = self._build_governed_write_command(
                    rel_path, content, existing.document_id if existing else None
                )
                result_index = len(results)
                results.append(None)
                prepared.append((result_index, filename, rel_path, old_exists, command))
                reserved_paths.add(
                    self.runtime().repository.projection_key(rel_path)
                )
                if old_exists:
                    old_paths.append(rel_path)
            except Exception as exc:
                failed += 1
                results.append({"filename": filename or "", "status": "failed", "reason": str(exc)})

        imported = 0
        if prepared:
            runtime = self.runtime()
            records = runtime.write_batch(
                self.identity,
                [item[4] for item in prepared],
                sync_derivatives=False,
            )
            runtime.rebuild_derivatives()
            for prepared_item, record in zip(prepared, records):
                result_index, filename, rel_path, old_exists, _ = prepared_item
                results[result_index] = {
                    "filename": filename,
                    "path": rel_path,
                    "status": "imported",
                    "overwritten": old_exists,
                    "document_id": record.document_id,
                    "version": record.version,
                    "content_hash": record.content_hash,
                }
            imported = len(records)
            # Keep index.md in sync before reindexing so it is indexed too.
            self.rebuild_index_md()
            self._sync_index(old_paths, force=True)
        return {"results": results, "imported": imported, "skipped": skipped, "failed": failed}

    def create_category(self, path: str) -> dict:
        rel_path, _ = self._resolve_path(path, kind="category")
        if self._category_exists(rel_path):
            return {"path": rel_path, "created": False, "reason": "already_exists"}
        scope, _ = self._knowledge_scope()
        created = self.runtime().create_category(
            self.identity,
            rel_path,
            scope=scope,
            session_id=self.session_id,
        )
        return {"path": rel_path, "created": created}

    def rename_category(self, path: str, new_path: str) -> dict:
        old_rel, _ = self._resolve_path(path, kind="category")
        new_rel, _ = self._resolve_path(new_path, kind="category")
        old_key = self.runtime().repository.projection_key(old_rel)
        new_key = self.runtime().repository.projection_key(new_rel)
        if new_key.startswith(old_key + "/"):
            raise ValueError("target category cannot be inside source category")
        visible_categories = self._visible_category_paths()
        records_by_path = self._visible_records_by_path()
        prefix = old_key + "/"
        records = [
            record
            for key, record in records_by_path.items()
            if key.startswith(prefix)
        ]
        if not any(
            self.runtime().repository.projection_key(item) == old_key
            for item in visible_categories
        ) and not records:
            return {
                "old_path": old_rel,
                "path": new_rel,
                "moved": False,
                "reason": "not_found",
            }
        if self._category_exists(new_rel):
            raise FileExistsError(f"target already exists: {new_rel}")
        runtime = self.runtime()
        prepared = []
        old_paths = []
        for record in records:
            old_document_rel = record.projection_path
            suffix = old_document_rel[len(old_rel):].lstrip("/")
            new_document_rel = f"{new_rel}/{suffix}"
            occupant = self._find_record(new_document_rel)
            if occupant is not None and occupant.document_id != record.document_id:
                raise FileExistsError(
                    f"target already exists: {new_document_rel}"
                )
            prepared.append(
                self._build_governed_write_command(
                    new_document_rel,
                    record.content,
                    document_id=record.document_id,
                    template=record,
                )
            )
            old_paths.append(old_document_rel)
        if prepared:
            runtime.write_batch(
                self.identity, prepared, sync_derivatives=False
            )
            runtime.rebuild_derivatives()
        runtime.rename_category_facts(
            self.identity, old_rel, new_rel, session_id=self.session_id
        )
        self.rebuild_index_md()
        self._sync_index(old_paths)
        return {
            "old_path": old_rel,
            "path": new_rel,
            "moved_documents": len(records),
        }

    def delete_category(self, path: str, confirm: bool = False) -> dict:
        rel_path, _ = self._resolve_path(path, kind="category")
        path_key = self.runtime().repository.projection_key(rel_path)
        prefix = path_key + "/"
        governed_records = [
            record
            for key, record in self._visible_records_by_path().items()
            if key.startswith(prefix)
        ]
        visible_categories = self._visible_category_paths()
        matching_categories = [
            category
            for category in visible_categories
            if (
                self.runtime().repository.projection_key(category) == path_key
                or self.runtime().repository.projection_key(category).startswith(prefix)
            )
        ]
        if not matching_categories and not governed_records:
            return {"path": rel_path, "deleted": False, "reason": "not_found"}
        documents = sorted(record.projection_path for record in governed_records)
        if (documents or len(matching_categories) > 1) and not confirm:
            raise ValueError("category is not empty; confirmation is required")
        result = self.delete_documents(documents)
        self.runtime().delete_category_facts(
            self.identity, rel_path, session_id=self.session_id
        )
        self.rebuild_index_md()
        return {
            "path": rel_path,
            "deleted": True,
            "deleted_documents": result["deleted"],
        }

    def delete_documents(self, paths: Iterable[str]) -> dict:
        if not isinstance(paths, list):
            raise ValueError("paths must be a list")
        results = []
        deleted = []
        for path in paths:
            rel_path, _ = self._resolve_path(path, kind="document")
            self._ensure_not_protected(rel_path)
            record = self._find_record(rel_path)
            if record is not None:
                key = self._idempotency_key(
                    "knowledge-revoke",
                    {
                        "actor_user_id": self.identity.actor_user_id,
                        "document_id": record.document_id,
                        "reason": "knowledge service deletion",
                    },
                )
                revoked = self.runtime().revoke(
                    self.identity,
                    record.document_id,
                    key,
                    "knowledge service deletion",
                )
                deleted.append(rel_path)
                results.append(
                    {
                        "path": rel_path,
                        "deleted": True,
                        "document_id": revoked.document_id,
                        "version": revoked.version,
                    }
                )
                continue
            deleted.append(rel_path)
            results.append(
                {"path": rel_path, "deleted": False, "reason": "not_found"}
            )
        self._sync_index(deleted)
        return {"results": results, "deleted": sum(1 for item in results if item["deleted"])}

    def move_documents(self, paths: Iterable[str], target_category: str) -> dict:
        if not isinstance(paths, list):
            raise ValueError("paths must be a list")
        target_rel, _ = self._resolve_path(target_category, kind="category")
        if not self._category_exists(target_rel):
            raise FileNotFoundError(f"category not found: {target_rel}")
        results = []
        moved_old_paths = []
        for path in paths:
            rel_path, _ = self._resolve_path(path, kind="document")
            self._ensure_not_protected(rel_path)
            record = self._find_record(rel_path)
            if record is None:
                results.append(
                    {"path": rel_path, "moved": False, "reason": "not_found"}
                )
                continue
            new_rel = "%s/%s" % (target_rel, rel_path.rsplit("/", 1)[-1])
            occupant = self._find_record(new_rel)
            if occupant is not None and occupant.document_id != record.document_id:
                results.append({"path": rel_path, "moved": False, "reason": "target_exists",
                                "target": new_rel})
                continue
            if self.runtime().repository.projection_key(new_rel) == self.runtime().repository.projection_key(rel_path):
                results.append(
                    {
                        "path": rel_path,
                        "moved": False,
                        "reason": "already_in_target",
                        "target": new_rel,
                    }
                )
                continue
            try:
                moved = self._write_governed_document(
                    new_rel,
                    record.content,
                    document_id=record.document_id,
                    template=record,
                )
                moved_old_paths.append(rel_path)
                results.append({
                    "path": rel_path,
                    "moved": True,
                    "target": new_rel,
                    "document_id": record.document_id,
                    "version": moved.version,
                })
            except FileNotFoundError:
                results.append(
                    {"path": rel_path, "moved": False, "reason": "not_found"}
                )
        if moved_old_paths:
            self.rebuild_index_md()
        self._sync_index(moved_old_paths)
        return {"results": results, "moved": len(moved_old_paths)}

    @staticmethod
    def _remove_empty_directories(root: Path):
        """只删除已经为空的目录，绝不连带删除非知识文件。"""

        if not root.exists():
            return
        directories = sorted(
            [path for path in root.rglob("*") if path.is_dir()],
            key=lambda path: len(path.parts),
            reverse=True,
        )
        for directory in directories + [root]:
            try:
                directory.rmdir()
            except OSError:
                pass

    # ------------------------------------------------------------------
    # list — directory tree with stats
    # ------------------------------------------------------------------
    def list_tree(self) -> dict:
        """
        Return the knowledge directory tree grouped by category,
        supporting arbitrarily nested sub-directories.

        Returns::

            {
                "tree": [
                    {
                        "dir": "concepts",
                        "files": [
                            {"name": "moe.md", "title": "MoE", "size": 1234},
                        ],
                        "children": []
                    },
                    {
                        "dir": "platform",
                        "files": [],
                        "children": [
                            {
                                "dir": "analysis",
                                "files": [{"name": "perf.md", ...}],
                                "children": []
                            }
                        ]
                    },
                ],
                "stats": {"pages": 15, "size": 32768},
                "enabled": true
            }
        """
        root_node = {"files": [], "children": {}}

        def ensure_category(category_path: str) -> dict:
            node = root_node
            for part in category_path.split("/"):
                node = node["children"].setdefault(
                    part, {"files": [], "children": {}}
                )
            return node

        for category_path in sorted(
            self._visible_category_paths(),
            key=self.runtime().repository.projection_key,
        ):
            ensure_category(category_path)

        records = sorted(
            self._visible_records_by_path().values(),
            key=lambda record: self.runtime().repository.projection_key(
                record.projection_path
            ),
        )
        stats = {
            "pages": len(records),
            "size": sum(len(record.content.encode("utf-8")) for record in records),
        }
        for record in records:
            parts = record.projection_path.split("/")
            file_info = {
                "name": parts[-1],
                "title": record.title,
                "size": len(record.content.encode("utf-8")),
            }
            node = ensure_category("/".join(parts[:-1])) if len(parts) > 1 else root_node
            node["files"].append(file_info)

        knowledge_root = Path(self.knowledge_dir)
        for protected_name in sorted(self.PROTECTED_FILES):
            protected_path = knowledge_root / protected_name
            if not protected_path.is_file():
                continue
            root_node["files"].append(
                {
                    "name": protected_name,
                    "title": protected_name[:-3],
                    "size": protected_path.stat().st_size,
                }
            )

        def serialize(node: dict) -> list:
            result = []
            for name, child in sorted(node["children"].items()):
                result.append(
                    {
                        "dir": name,
                        "files": sorted(
                            child["files"], key=lambda item: item["name"].casefold()
                        ),
                        "children": serialize(child),
                    }
                )
            return result

        return {
            "root_files": sorted(
                root_node["files"], key=lambda item: item["name"].casefold()
            ),
            "tree": serialize(root_node),
            "stats": stats,
            "enabled": conf().get("knowledge", True),
        }

    def _scan_dir(self, dir_path: str, stats: dict, is_root: bool = False) -> tuple:
        """
        Recursively scan a directory.

        :return: (files, children) where files is a list of .md file dicts
                 in this directory and children is a list of sub-directory nodes.
        """
        files = []
        children = []
        for name in sorted(os.listdir(dir_path)):
            if name.startswith("."):
                continue
            full = os.path.join(dir_path, name)
            if os.path.isdir(full):
                sub_files, sub_children = self._scan_dir(full, stats)
                children.append({"dir": name, "files": sub_files, "children": sub_children})
            elif name.endswith(".md"):
                size = os.path.getsize(full)
                if not is_root:
                    stats["pages"] += 1
                    stats["size"] += size
                # Prefer the H1 heading as a readable title for normal docs.
                # System files (index.md / log.md) keep their filename so the
                # tree never hides what they actually are.
                title = name[:-3]
                if name not in self.PROTECTED_FILES:
                    try:
                        with open(full, "r", encoding="utf-8") as f:
                            first_line = f.readline().strip()
                        if first_line.startswith("# "):
                            title = first_line[2:].strip() or title
                    except Exception:
                        pass
                files.append({"name": name, "title": title, "size": size})
        return files, children

    # ------------------------------------------------------------------
    # read — single file content
    # ------------------------------------------------------------------
    def read_file(self, rel_path: str) -> dict:
        """
        Read a single knowledge markdown file.

        :param rel_path: Relative path within knowledge/, e.g. ``concepts/moe.md``
        :return: dict with ``content`` and ``path``
        :raises ValueError: if path is invalid or escapes knowledge dir
        :raises FileNotFoundError: if file does not exist
        """
        rel_path, full_path = self._resolve_path(rel_path, kind="document")
        record = self._find_record(rel_path)
        if record is not None:
            return {
                "content": record.content,
                "path": rel_path,
                "document_id": record.document_id,
                "version": record.version,
                "content_hash": record.content_hash,
            }
        if rel_path not in self.PROTECTED_FILES:
            raise FileNotFoundError(
                f"governed knowledge document not found: {rel_path}"
            )
        if not full_path.is_file():
            raise FileNotFoundError(f"file not found: {rel_path}")

        with open(full_path, "r", encoding="utf-8") as f:
            content = f.read()
        return {"content": content, "path": rel_path}

    # ------------------------------------------------------------------
    # graph — nodes and links for visualization
    # ------------------------------------------------------------------
    def build_graph(self) -> dict:
        """
        Parse all knowledge pages and extract cross-reference links.

        Returns::

            {
                "nodes": [
                    {"id": "concepts/moe.md", "label": "MoE", "category": "concepts"},
                    ...
                ],
                "links": [
                    {"source": "concepts/moe.md", "target": "entities/deepseek.md"},
                    ...
                ]
            }
        """
        nodes = {}
        links = []
        link_re = re.compile(r'\[([^\]]*)\]\(([^)]+\.md)\)')

        records = self._visible_records_by_path()
        for record in records.values():
            rel = record.projection_path
            parts = rel.split("/")
            category = parts[0] if len(parts) > 1 else "root"
            nodes[rel] = {
                "id": rel,
                "label": record.title,
                "category": category,
            }
            for _, link_target in link_re.findall(record.content):
                target_rel = posixpath.normpath(
                    posixpath.join(posixpath.dirname(rel), link_target)
                )
                if (
                    target_rel == ".."
                    or target_rel.startswith("../")
                    or target_rel.startswith("/")
                    or target_rel == rel
                ):
                    continue
                links.append({"source": rel, "target": target_rel})

        valid_ids = set(nodes.keys())
        links = [l for l in links if l["source"] in valid_ids and l["target"] in valid_ids]
        seen = set()
        deduped = []
        for l in links:
            key = tuple(sorted([l["source"], l["target"]]))
            if key not in seen:
                seen.add(key)
                deduped.append(l)

        return {
            "nodes": [nodes[key] for key in sorted(nodes)],
            "links": deduped,
        }

    # ------------------------------------------------------------------
    # dispatch — single entry point for protocol messages
    # ------------------------------------------------------------------
    def dispatch(self, action: str, payload: Optional[dict] = None) -> dict:
        """
        Dispatch a knowledge management action.

        :param action: ``list``, ``read``, or ``graph``
        :param payload: action-specific payload
        :return: protocol-compatible response dict
        """
        payload = payload or {}
        try:
            if action == "list":
                result = self.list_tree()
                return {"action": action, "code": 200, "message": "success", "payload": result}

            elif action == "read":
                path = payload.get("path")
                if not path:
                    return {"action": action, "code": 400, "message": "path is required", "payload": None}
                result = self.read_file(path)
                return {"action": action, "code": 200, "message": "success", "payload": result}

            elif action == "graph":
                result = self.build_graph()
                return {"action": action, "code": 200, "message": "success", "payload": result}

            elif action == "create_category":
                result = self.create_category(payload.get("path"))
            elif action == "rename_category":
                result = self.rename_category(payload.get("path"), payload.get("new_path"))
            elif action == "delete_category":
                result = self.delete_category(payload.get("path"), payload.get("confirm", False))
            elif action == "delete_documents":
                result = self.delete_documents(payload.get("paths") or [])
            elif action == "move_documents":
                result = self.move_documents(payload.get("paths") or [], payload.get("target_category"))
            elif action == "create_document":
                result = self.create_document(payload.get("path"), payload.get("content", ""),
                                              payload.get("overwrite", False))
            elif action == "import_documents":
                result = self.import_documents(
                    payload.get("target_category"),
                    payload.get("files") or [],
                    payload.get("conflict_strategy", "skip"),
                )
            else:
                return {"action": action, "code": 400, "message": f"unknown action: {action}", "payload": None}
            return {"action": action, "code": 200, "message": "success", "payload": result}

        except ValueError as e:
            return {"action": action, "code": 403, "message": str(e), "payload": None}
        except FileNotFoundError as e:
            return {"action": action, "code": 404, "message": str(e), "payload": None}
        except FileExistsError as e:
            return {"action": action, "code": 409, "message": str(e), "payload": None}
        except Exception as e:
            logger.error(f"[KnowledgeService] dispatch error: action={action}, error={e}")
            return {"action": action, "code": 500, "message": str(e), "payload": None}

