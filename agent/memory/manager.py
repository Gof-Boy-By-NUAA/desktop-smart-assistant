"""
Memory manager for AgentMesh

Provides high-level interface for memory operations
"""

import os
import json
import threading
import uuid
from typing import List, Optional, Dict, Any
from pathlib import Path
import hashlib
from datetime import datetime, timedelta

from agent.memory.config import MemoryConfig, get_default_memory_config
from agent.memory.storage import MemoryStorage, MemoryChunk, SearchResult
from agent.memory.chunker import TextChunker
from agent.memory.embedding import EmbeddingProvider, EmbeddingCache
from agent.memory.summarizer import MemoryFlushManager, create_memory_files_if_needed


# 同一进程内可能为多个会话创建 MemoryManager，共用锁可避免恢复与写入交错。
_GOVERNED_RUNTIME_LOCK = threading.RLock()


class MemoryManager:
    """
    Memory manager with hybrid search capabilities
    
    Provides long-term memory for agents with vector and keyword search
    """
    
    def __init__(
        self,
        config: Optional[MemoryConfig] = None,
        embedding_provider: Optional[EmbeddingProvider] = None,
        llm_model: Optional[Any] = None
    ):
        """
        Initialize memory manager
        
        Args:
            config: Memory configuration (uses global config if not provided)
            embedding_provider: Custom embedding provider (optional)
            llm_model: LLM model for summarization (optional)
        """
        self.config = config or get_default_memory_config()

        from agent.memory.governance import (
            GovernedMemoryRepository,
            GovernedMemoryService,
            ValidationError,
        )

        if (
            not isinstance(self.config.tenant_id, str)
            or not self.config.tenant_id.strip()
        ):
            raise ValidationError("tenant_id 不能为空")
        
        # Initialize storage
        db_path = self.config.get_db_path()
        self.storage = MemoryStorage(db_path)

        # 知识已迁移到独立事实库；必须在任何检索入口可用前同步清除旧副本。
        removed_knowledge_chunks = self.storage.delete_by_source("knowledge")
        if removed_knowledge_chunks:
            from common.log import logger

            logger.info(
                f"[MemoryManager] Removed {removed_knowledge_chunks} legacy knowledge chunks"
            )

        # 治理数据库是记忆事实源；投影与检索索引都可以从这里重建。
        self.governance_repository = GovernedMemoryRepository(
            self.config.get_governance_db_path()
        )
        self.governance_service = GovernedMemoryService(self.governance_repository)
        from agent.memory.governance.locks import governed_runtime_lock

        self._governed_runtime_file_lock = governed_runtime_lock(
            self.config.get_memory_dir(), self.config.tenant_id
        )

        # 新词法索引是可选增强，初始化失败时保留原有检索链。
        self.lexical_index = None
        if self.config.enable_governed_retrieval:
            try:
                from agent.retrieval import TenantAwareLexicalIndex

                self.lexical_index = TenantAwareLexicalIndex(
                    self.config.get_retrieval_db_path()
                )
            except Exception as error:
                from common.log import logger

                logger.warning(
                    f"[MemoryManager] Governed lexical retrieval unavailable: {error}"
                )
        
        # Initialize chunker
        self.chunker = TextChunker(
            max_tokens=self.config.chunk_max_tokens,
            overlap_tokens=self.config.chunk_overlap_tokens
        )
        
        # Embedding provider is owned by the caller (agent_initializer is the
        # canonical entry point and handles legacy/explicit + state validation).
        # When None is passed, memory degrades to keyword-only search instead
        # of silently re-initializing a vendor here, which would bypass the
        # caller's state checks and risk corrupting the index.
        self.embedding_provider = embedding_provider
        if self.embedding_provider is None:
            from common.log import logger
            logger.info(
                "[MemoryManager] No embedding provider; memory will use keyword search only"
            )

        # Cache for query embeddings (avoids redundant API calls within a session)
        self._embedding_cache = EmbeddingCache()


        # Initialize memory flush manager
        workspace_dir = self.config.get_workspace()
        self.flush_manager = MemoryFlushManager(
            workspace_dir=workspace_dir,
            llm_model=llm_model
        )
        
        # Ensure workspace directories exist
        self._init_workspace()

        # 启动时修复可能因上次进程中断而未完成的投影和派生索引。
        self._restore_governed_runtime()
        
        self._dirty = False
    
    def _init_workspace(self):
        """Initialize workspace directories"""
        memory_dir = self.config.get_memory_dir()
        memory_dir.mkdir(parents=True, exist_ok=True)
        
        # Create default memory files
        workspace_dir = self.config.get_workspace()
        create_memory_files_if_needed(workspace_dir)
    
    async def search(
        self,
        query: str,
        user_id: Optional[str] = None,
        max_results: Optional[int] = None,
        min_score: Optional[float] = None,
        include_shared: bool = True,
        session_id: Optional[str] = None,
    ) -> List[SearchResult]:
        """
        Search memory with hybrid search (vector + keyword)
        
        Args:
            query: Search query
            user_id: User ID for scoped search
            max_results: Maximum results to return
            min_score: Minimum score threshold
            include_shared: Include shared memories
            
        Returns:
            List of search results sorted by relevance
        """
        max_results = self.config.max_results if max_results is None else max_results
        min_score = self.config.min_score if min_score is None else min_score
        
        # Determine scopes
        scopes = []
        if include_shared:
            scopes.append("shared")
        if user_id:
            scopes.append("user")
        
        if not scopes:
            return []
        
        # Sync if needed
        if self.config.sync_on_search and self._dirty:
            await self.sync()
        
        from common.log import logger

        # Perform vector search (if embedding provider available).
        # Failures degrade silently to keyword-only — no exception is raised.
        vector_results = []
        if self.embedding_provider:
            try:
                provider_name = type(self.embedding_provider).__name__
                model_name = getattr(self.embedding_provider, 'model', '')
                cached = self._embedding_cache.get(query, provider_name, model_name)
                if cached is not None:
                    query_embedding = cached
                else:
                    query_embedding = self.embedding_provider.embed_query(query)
                    self._embedding_cache.put(query, provider_name, model_name, query_embedding)
                vector_results = self.storage.search_vector(
                    query_embedding=query_embedding,
                    user_id=user_id,
                    scopes=scopes,
                    limit=max_results * 2  # Get more candidates for merging
                )
                logger.info(f"[MemoryManager] Vector search found {len(vector_results)} results for query: {query}")
            except Exception as e:
                logger.error(
                    f"[MemoryManager] Vector search failed, falling back to keyword-only: {e}"
                )

        # 新索引正常返回空结果时不得回退，避免重新暴露已删除或无权访问的数据。
        lexical_search_succeeded = False
        keyword_results = []
        if self.lexical_index is not None:
            try:
                keyword_results = self._search_governed_lexical(
                    query=query,
                    user_id=user_id,
                    session_id=session_id,
                    limit=max_results * 2,
                )
                lexical_search_succeeded = True
                logger.info(
                    f"[MemoryManager] Governed lexical search found "
                    f"{len(keyword_results)} results for query: {query}"
                )
            except Exception as error:
                logger.error(
                    f"[MemoryManager] Governed lexical search failed, "
                    f"falling back to legacy keyword search: {error}"
                )

        if not lexical_search_succeeded:
            keyword_results = self.storage.search_keyword(
                query=query,
                user_id=user_id,
                scopes=scopes,
                limit=max_results * 2
            )
            logger.info(
                f"[MemoryManager] Legacy keyword search found "
                f"{len(keyword_results)} results for query: {query}"
            )

        # Merge results
        effective_vector_weight = self.config.vector_weight if vector_results else 0.0
        effective_keyword_weight = self.config.keyword_weight if keyword_results else 0.0
        total_weight = effective_vector_weight + effective_keyword_weight
        if total_weight > 0:
            effective_vector_weight /= total_weight
            effective_keyword_weight /= total_weight

        merged = self._merge_results(
            vector_results,
            keyword_results,
            effective_vector_weight,
            effective_keyword_weight
        )

        # Filter by min score and limit
        filtered = [r for r in merged if r.score >= min_score]
        return filtered[:max_results]
    
    async def add_memory(
        self,
        content: str,
        user_id: Optional[str] = None,
        scope: str = "shared",
        source: str = "memory",
        path: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ):
        """
        Add new memory content
        
        Args:
            content: Memory content
            user_id: User ID for user-scoped memory
            scope: Memory scope ("shared", "user", "session")
            source: Memory source ("memory" or "session")
            path: File path (auto-generated if not provided)
            metadata: Additional metadata
        """
        if not content.strip():
            return
        
        # Generate path if not provided
        if not path:
            content_hash = hashlib.md5(content.encode('utf-8')).hexdigest()[:8]
            if user_id and scope == "user":
                path = f"memory/users/{user_id}/memory_{content_hash}.md"
            else:
                path = f"memory/shared/memory_{content_hash}.md"
        
        # Chunk content
        chunks = self.chunker.chunk_text(content)
        
        # Generate embeddings (if provider available)
        texts = [chunk.text for chunk in chunks]
        if self.embedding_provider:
            embeddings = self.embedding_provider.embed_batch(texts)
        else:
            # No embeddings, just use None
            embeddings = [None] * len(texts)
        
        # Create memory chunks
        memory_chunks = []
        for chunk, embedding in zip(chunks, embeddings):
            chunk_id = self._generate_chunk_id(path, chunk.start_line, chunk.end_line)
            chunk_hash = MemoryStorage.compute_hash(chunk.text)
            
            memory_chunks.append(MemoryChunk(
                id=chunk_id,
                user_id=user_id,
                scope=scope,
                source=source,
                path=path,
                start_line=chunk.start_line,
                end_line=chunk.end_line,
                text=chunk.text,
                embedding=embedding,
                hash=chunk_hash,
                metadata=metadata
            ))
        
        # Save to storage
        self.storage.save_chunks_batch(memory_chunks)
        
        # Update file metadata
        file_hash = MemoryStorage.compute_hash(content)
        self.storage.update_file_metadata(
            path=path,
            source=source,
            file_hash=file_hash,
            mtime=int(os.path.getmtime(__file__)),  # Use current time
            size=len(content)
        )

    def remember(self, identity, command):
        """写入受治理记忆，并同步机器投影和词法索引。"""

        self._assert_runtime_tenant(identity.tenant_id)
        with _GOVERNED_RUNTIME_LOCK:
            record = self.governance_service.write(identity, command)
            self._drain_governed_derivative_job(
                identity.tenant_id, record.memory_id
            )
            return record

    def revoke(
        self,
        identity,
        memory_id: str,
        idempotency_key: str,
        reason: str,
    ):
        """撤销受治理记忆，并立即移除全部派生数据。"""

        self._assert_runtime_tenant(identity.tenant_id)
        with _GOVERNED_RUNTIME_LOCK:
            record = self.governance_service.revoke(
                identity,
                memory_id,
                idempotency_key,
                reason,
            )
            self._drain_governed_derivative_job(
                identity.tenant_id, memory_id
            )
            return record

    def rollback(
        self,
        identity,
        memory_id: str,
        target_version: int,
        idempotency_key: str,
        reason: str,
    ):
        """把历史版本恢复成新的有效版本，并刷新全部派生数据。"""

        self._assert_runtime_tenant(identity.tenant_id)
        with _GOVERNED_RUNTIME_LOCK:
            record = self.governance_service.rollback(
                identity,
                memory_id,
                target_version,
                idempotency_key,
                reason,
            )
            self._drain_governed_derivative_job(
                identity.tenant_id, memory_id
            )
            return record

    def get_governed_memory(
        self,
        identity,
        memory_id: str,
        session_id: Optional[str] = None,
    ):
        """通过治理服务读取记忆，不允许绕过租户、用户和会话边界。"""

        self._assert_runtime_tenant(identity.tenant_id)
        return self.governance_service.get(identity, memory_id, session_id=session_id)
    
    async def sync(self, force: bool = False):
        """
        Synchronize memory from files.

        Two-pass design to amortize embedding HTTP cost:
          1. Walk all files, chunk those whose hash changed, collect pending
             chunks across files. No embedding calls yet.
          2. Run a single embed_batch over the union of pending chunks (the
             provider auto-paginates by vendor cap), then persist per-file.

        For workspaces with many small files (101 files / ~1 chunk each), this
        cuts ~100 HTTP calls down to ~ceil(total_chunks / vendor_cap).

        Args:
            force: Force full reindex
        """
        memory_dir = self.config.get_memory_dir()
        workspace_dir = self.config.get_workspace()

        files_to_scan: List[tuple] = []  # (file_path, source, scope, user_id)

        memory_file = Path(workspace_dir) / "MEMORY.md"
        if memory_file.exists():
            files_to_scan.append((memory_file, "memory", "shared", None))

        if memory_dir.exists():
            for file_path in memory_dir.rglob("*.md"):
                rel_parts = file_path.relative_to(workspace_dir).parts
                if any(part.startswith('.') for part in rel_parts):
                    continue
                # Dream diaries are narrative reflections produced by Deep
                # Dream; their factual content has already been distilled
                # into MEMORY.md. Indexing them adds noisy near-duplicates
                # that crowd out the authoritative entry in retrieval.
                if "dreams" in rel_parts:
                    continue
                if "daily" in rel_parts:
                    if "users" in rel_parts or len(rel_parts) > 3:
                        user_idx = rel_parts.index("daily") + 1
                        user_id = rel_parts[user_idx] if user_idx < len(rel_parts) else None
                        scope = "user"
                    else:
                        user_id = None
                        scope = "shared"
                elif "users" in rel_parts:
                    user_idx = rel_parts.index("users") + 1
                    user_id = rel_parts[user_idx] if user_idx < len(rel_parts) else None
                    scope = "user"
                else:
                    user_id = None
                    scope = "shared"
                files_to_scan.append((file_path, "memory", scope, user_id))

        # Pass 1: inline chunking + change detection. Inlined (instead of
        # calling self._prepare_file_for_sync) so this method does not depend
        # on any sibling helpers — keeps it robust against partial reloads
        # where the class object is older than the method's source.
        pending: List[Dict[str, Any]] = []
        lexical_documents = []
        current_file_paths: List[str] = []
        workspace_dir_path = self.config.get_workspace()
        for file_path, source, scope, user_id in files_to_scan:
            try:
                content = file_path.read_text(encoding='utf-8')
            except Exception:
                continue
            file_hash = MemoryStorage.compute_hash(content)
            rel_path = str(file_path.relative_to(workspace_dir_path))
            current_file_paths.append(rel_path)
            chunks = self.chunker.chunk_text(content)

            if self.lexical_index is not None:
                from agent.memory.governance import MemoryScope, Sensitivity
                from agent.retrieval import IndexedDocument

                sensitivity = (
                    Sensitivity.INTERNAL if source == "knowledge" else Sensitivity.PRIVATE
                )
                for chunk in chunks:
                    lexical_documents.append(
                        IndexedDocument(
                            tenant_id=self.config.tenant_id,
                            document_id=self._generate_chunk_id(
                                rel_path, chunk.start_line, chunk.end_line
                            ),
                            scope=MemoryScope(scope),
                            owner_user_id=user_id,
                            title=file_path.stem,
                            text=chunk.text,
                            source_ref=rel_path,
                            collection_id="workspace",
                            sensitivity=sensitivity,
                            metadata={
                                "path": rel_path,
                                "start_line": chunk.start_line,
                                "end_line": chunk.end_line,
                                "source": source,
                                "user_id": user_id,
                            },
                        )
                    )

            if not force and self.storage.get_file_hash(rel_path) == file_hash:
                continue
            if not chunks:
                self.storage.delete_by_path(rel_path)
                stat = file_path.stat()
                self.storage.update_file_metadata(
                    path=rel_path,
                    source=source,
                    file_hash=file_hash,
                    mtime=int(stat.st_mtime),
                    size=stat.st_size,
                )
                continue
            pending.append({
                "file_path": file_path,
                "rel_path": rel_path,
                "source": source,
                "scope": scope,
                "user_id": user_id,
                "file_hash": file_hash,
                "chunks": chunks,
                "texts": [c.text for c in chunks],
            })

        stale_count = self.storage.delete_missing_file_sources(current_file_paths)
        if stale_count:
            from common.log import logger

            logger.info(f"[MemoryManager] Removed {stale_count} stale file index entries")

        if self.lexical_index is not None:
            try:
                self.lexical_index.replace_collection(
                    self.config.tenant_id,
                    "workspace",
                    lexical_documents,
                )
            except Exception as error:
                from common.log import logger

                logger.error(
                    f"[MemoryManager] Governed lexical sync failed: {error}"
                )

        if not pending:
            self._dirty = False
            return

        # Pass 2: single batched embed across all pending chunks.
        # CRITICAL: never touch the index until we hold valid embeddings.
        # If embed_batch fails, leave the existing index intact (chunks +
        # file_hash) so the next sync will retry the same files. Writing
        # NULL embeddings + updating file_hash here would mark the file as
        # "successfully synced" and silently strand it without vectors.
        all_texts: List[str] = []
        for entry in pending:
            all_texts.extend(entry["texts"])

        if not self.embedding_provider:
            # No provider configured at all (legacy keyword-only). Persist
            # chunks without embeddings — this is the user's intent.
            all_embeddings: List[Optional[List[float]]] = [None] * len(all_texts)
        else:
            try:
                all_embeddings = self.embedding_provider.embed_batch(all_texts)
            except Exception as e:
                from common.log import logger
                logger.error(
                    f"[MemoryManager] Batch embedding failed for {len(all_texts)} "
                    f"chunks across {len(pending)} files: {e}. "
                    f"Index left untouched; will retry on next sync."
                )
                # Bail before touching storage. self._dirty stays True so
                # callers know there is pending work.
                return

        # Pass 3: inline persist — same self-contained reasoning as Pass 1.
        cursor = 0
        for entry in pending:
            n = len(entry["texts"])
            entry_embeddings = all_embeddings[cursor:cursor + n]
            cursor += n

            rel_path = entry["rel_path"]
            self.storage.delete_by_path(rel_path)
            memory_chunks = []
            for chunk, embedding in zip(entry["chunks"], entry_embeddings):
                chunk_id = self._generate_chunk_id(rel_path, chunk.start_line, chunk.end_line)
                chunk_hash = MemoryStorage.compute_hash(chunk.text)
                memory_chunks.append(MemoryChunk(
                    id=chunk_id,
                    user_id=entry["user_id"],
                    scope=entry["scope"],
                    source=entry["source"],
                    path=rel_path,
                    start_line=chunk.start_line,
                    end_line=chunk.end_line,
                    text=chunk.text,
                    embedding=embedding,
                    hash=chunk_hash,
                    metadata=None,
                ))
            self.storage.save_chunks_batch(memory_chunks)
            stat = entry["file_path"].stat()
            self.storage.update_file_metadata(
                path=rel_path,
                source=entry["source"],
                file_hash=entry["file_hash"],
                mtime=int(stat.st_mtime),
                size=stat.st_size,
            )

        self._dirty = False

    def flush_memory(
        self,
        messages: list,
        user_id: Optional[str] = None,
        reason: str = "threshold",
        max_messages: int = 10,
        context_summary_callback=None,
    ) -> bool:
        """
        Flush conversation summary to daily memory file.

        Args:
            messages: Conversation message list
            user_id: Optional user ID
            reason: "threshold" | "overflow" | "daily_summary"
            max_messages: Max recent messages to include (0 = all)
            context_summary_callback: Optional callback(str) invoked with the
                daily summary text for in-context injection

        Returns:
            True if flush was dispatched
        """
        success = self.flush_manager.flush_from_messages(
            messages=messages,
            user_id=user_id,
            reason=reason,
            max_messages=max_messages,
            context_summary_callback=context_summary_callback,
        )
        if success:
            self._dirty = True
        return success
    
    def get_status(self) -> Dict[str, Any]:
        """Get memory status"""
        stats = self.storage.get_stats()
        return {
            'chunks': stats['chunks'],
            'files': stats['files'],
            'workspace': str(self.config.get_workspace()),
            'dirty': self._dirty,
            'embedding_enabled': self.embedding_provider is not None,
            'embedding_provider': self.config.embedding_provider if self.embedding_provider else 'disabled',
            'embedding_model': self.config.embedding_model if self.embedding_provider else 'N/A',
            'search_mode': (
                'hybrid (vector + governed lexical)'
                if self.embedding_provider and self.lexical_index
                else 'governed lexical only'
                if self.lexical_index
                else 'hybrid (vector + legacy keyword)'
                if self.embedding_provider
                else 'legacy keyword only (FTS5)'
            ),
            'governed_retrieval_enabled': self.lexical_index is not None,
            'governed_memory_records': len(
                self.governance_repository.list_active_records(self.config.tenant_id)
            ),
            'governed_memory_pending_derivatives': (
                self.governance_repository.count_derivative_jobs(
                    self.config.tenant_id
                )
            ),
            'tenant_id': self.config.tenant_id,
        }
    
    def mark_dirty(self):
        """Mark memory as dirty (needs sync)"""
        self._dirty = True
    
    def close(self):
        """Close memory manager and release resources"""
        self.storage.close()
        if self.lexical_index is not None:
            self.lexical_index.close()
        knowledge_runtime = getattr(self, "knowledge_runtime", None)
        if knowledge_runtime is not None:
            knowledge_runtime.close()
        self.governance_repository.close()
    
    # Helper methods

    def _search_governed_lexical(
        self,
        query: str,
        user_id: Optional[str],
        session_id: Optional[str],
        limit: int,
    ) -> List[SearchResult]:
        """把权限化词法结果转换为现有 SearchResult 契约。"""

        from agent.memory.governance import IdentityContext

        identity = IdentityContext(
            tenant_id=self.config.tenant_id,
            actor_user_id=user_id or "local-user",
            roles=frozenset(),
            trace_id="trace-memory-search",
            auth_source="smart-assistant-local-runtime",
        )
        results = self.lexical_index.search(
            identity,
            query,
            limit=limit,
            session_id=session_id,
        )
        converted = []
        for result in results:
            metadata = result.metadata
            if metadata.get("source") == "governed-memory":
                memory_id = str(metadata.get("memory_id", ""))
                try:
                    active = self.governance_service.get(
                        identity,
                        memory_id,
                        session_id=session_id,
                    )
                except Exception:
                    # 索引是派生数据；事实源不再允许读取时必须丢弃候选。
                    continue
                if int(metadata.get("version", 0)) != active.version:
                    # 更新索引失败时不返回过期内容，等待重试或下次启动修复。
                    continue
            converted.append(
                SearchResult(
                    path=str(metadata.get("path", result.source_ref)),
                    start_line=int(metadata.get("start_line", 1)),
                    end_line=int(metadata.get("end_line", 1)),
                    score=result.score,
                    snippet=result.text[:500] + ("..." if len(result.text) > 500 else ""),
                    source=str(metadata.get("source", "memory")),
                    user_id=metadata.get("user_id"),
                )
            )
        return converted

    def _restore_governed_runtime(self) -> None:
        """从事实库重建有效记录的投影和检索集合。"""

        with _GOVERNED_RUNTIME_LOCK:
            with self._governed_runtime_file_lock:
                self._purge_legacy_governed_projections()
                while True:
                    records = (
                        self.governance_repository.list_active_records(
                            self.config.tenant_id
                        )
                    )
                    active_ids = {record.memory_id for record in records}
                    for record in records:
                        if not self._governed_projection_matches(record):
                            self._write_governed_projection(record)
                        if not self._governed_projection_matches(record):
                            raise RuntimeError(
                                "治理记忆投影重建后内容不一致"
                            )

                    projection_dir = self._governed_projection_dir()
                    for projection_path in projection_dir.glob("*.md"):
                        if projection_path.stem not in active_ids:
                            projection_path.unlink()
                            if projection_path.exists():
                                raise RuntimeError(
                                    "治理记忆撤销投影删除后仍有残留"
                                )

                    if self.lexical_index is not None:
                        indexed_documents = [
                            self._governed_index_document(record)
                            for record in records
                        ]
                        self.lexical_index.replace_collection(
                            self.config.tenant_id,
                            "governed",
                            indexed_documents,
                        )
                        if not self.lexical_index.matches_tenant(
                            self.config.tenant_id, indexed_documents
                        ):
                            raise RuntimeError(
                                "治理记忆索引重建后缺失或内容不一致"
                            )

                    with self.governance_repository.transaction() as conn:
                        latest_records = (
                            self.governance_repository
                            .list_active_records_in_transaction(
                                conn, self.config.tenant_id
                            )
                        )
                        if latest_records != records:
                            continue
                        self.governance_repository.clear_derivative_jobs(
                            conn, self.config.tenant_id
                        )
                        return

    def _drain_governed_derivative_job(
        self, tenant_id: str, memory_id: str
    ) -> None:
        """在跨进程锁内把派生数据收敛到任务指定的最新版本。"""

        self._assert_runtime_tenant(tenant_id)
        with self._governed_runtime_file_lock:
            while True:
                with self.governance_repository.transaction() as conn:
                    target_version = (
                        self.governance_repository.get_derivative_job(
                            conn, tenant_id, memory_id
                        )
                    )
                    if target_version is None:
                        return
                    latest = self.governance_repository.get_latest(
                        conn, tenant_id, memory_id
                    )
                    if latest.version != target_version:
                        raise RuntimeError(
                            "治理记忆派生任务版本与最新事实不一致"
                        )

                if latest.status.value == "active":
                    self._synchronize_governed_record(latest)
                else:
                    self._remove_governed_derivatives(tenant_id, memory_id)

                with self.governance_repository.transaction() as conn:
                    current_target = (
                        self.governance_repository.get_derivative_job(
                            conn, tenant_id, memory_id
                        )
                    )
                    current_latest = self.governance_repository.get_latest(
                        conn, tenant_id, memory_id
                    )
                    if (
                        current_target != target_version
                        or current_latest.version != target_version
                    ):
                        continue
                    if not self.governance_repository.complete_derivative_job(
                        conn, tenant_id, memory_id, target_version
                    ):
                        raise RuntimeError(
                            "治理记忆派生任务完成标记发生并发冲突"
                        )
                    return

    def _synchronize_governed_record(self, record) -> None:
        """把一个有效版本同步到兼容投影和检索索引。"""

        self._assert_runtime_tenant(record.tenant_id)
        self._write_governed_projection(record)
        if not self._governed_projection_matches(record):
            raise RuntimeError("治理记忆投影写入后缺失或内容不一致")
        if self.lexical_index is not None:
            indexed_document = self._governed_index_document(record)
            self.lexical_index.index_documents([indexed_document])
            if not self.lexical_index.matches_document(indexed_document):
                raise RuntimeError("治理记忆索引写入后缺失或内容不一致")

    def _remove_governed_derivatives(
        self, tenant_id: str, memory_id: str
    ) -> None:
        """删除已撤销记忆的投影和索引文档。"""

        self._assert_runtime_tenant(tenant_id)
        projection_path = self._governed_projection_path(memory_id)
        try:
            projection_path.unlink()
        except FileNotFoundError:
            pass
        if projection_path.exists():
            raise RuntimeError("治理记忆撤销投影删除后仍有残留")
        if self.lexical_index is not None:
            self.lexical_index.delete_document(tenant_id, memory_id)
            if self.lexical_index.contains_document(
                tenant_id, memory_id
            ):
                raise RuntimeError("治理记忆撤销索引删除后仍有残留")

    def _assert_runtime_tenant(self, tenant_id: str) -> None:
        """拒绝把其他租户身份交给当前租户绑定的运行时。"""

        if tenant_id != self.config.tenant_id:
            from agent.memory.governance import ValidationError

            raise ValidationError("身份租户与记忆运行时租户不一致")

    def _governed_projection_dir(self) -> Path:
        """返回按租户哈希隔离的机器投影目录。"""

        tenant_key = hashlib.sha256(
            self.config.tenant_id.encode("utf-8")
        ).hexdigest()
        projection_dir = (
            self.config.get_memory_dir() / ".governed" / tenant_key
        )
        projection_dir.mkdir(parents=True, exist_ok=True)
        return projection_dir

    def _purge_legacy_governed_projections(self) -> None:
        """删除无法安全归属租户的旧版平铺派生投影。"""

        projection_root = self.config.get_memory_dir() / ".governed"
        projection_root.mkdir(parents=True, exist_ok=True)
        legacy_paths = list(projection_root.glob("*.md"))
        legacy_paths.extend(projection_root.glob(".*.tmp"))
        for legacy_path in legacy_paths:
            legacy_path.unlink()
            if legacy_path.exists():
                raise RuntimeError("旧版治理记忆投影清理后仍有残留")

    def _governed_projection_path(self, memory_id: str) -> Path:
        """校验标识符并返回对应投影路径。"""

        if (
            not isinstance(memory_id, str)
            or not memory_id.strip()
            or Path(memory_id).name != memory_id
            or "/" in memory_id
            or "\\" in memory_id
        ):
            from agent.memory.governance import ValidationError

            raise ValidationError("memory_id 不能包含路径分隔符")
        return self._governed_projection_dir() / f"{memory_id}.md"

    def _write_governed_projection(self, record) -> None:
        """同目录写临时文件后原子替换，避免暴露半写入内容。"""

        self._assert_runtime_tenant(record.tenant_id)
        projection_path = self._governed_projection_path(record.memory_id)
        content = self._governed_projection_content(record)
        temp_path = projection_path.with_name(
            f".{projection_path.name}.{uuid.uuid4().hex}.tmp"
        )
        try:
            with temp_path.open("w", encoding="utf-8", newline="\n") as output_file:
                output_file.write(content)
                output_file.flush()
                os.fsync(output_file.fileno())
            os.replace(temp_path, projection_path)
        finally:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass

    @staticmethod
    def _governed_projection_content(record) -> str:
        """生成可与磁盘投影逐字节核验的规范正文。"""

        header = {
            "tenant_id": record.tenant_id,
            "memory_id": record.memory_id,
            "version": record.version,
            "scope": record.scope.value,
            "owner_user_id": record.owner_user_id,
            "session_id": record.session_id,
            "sensitivity": record.sensitivity.value,
            "source_type": record.source_type,
            "source_ref": record.source_ref,
            "content_hash": record.content_hash,
        }
        lines = ["---"]
        lines.extend(
            f"{key}: {json.dumps(value, ensure_ascii=False)}"
            for key, value in header.items()
        )
        lines.extend(["---", "", record.content, ""])
        return "\n".join(lines)

    def _governed_projection_matches(self, record) -> bool:
        """核验兼容投影与当前不可变事实完全一致。"""

        self._assert_runtime_tenant(record.tenant_id)
        projection_path = self._governed_projection_path(record.memory_id)
        try:
            actual = projection_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return False
        return actual == self._governed_projection_content(record)

    def _governed_index_document(self, record):
        """把事实记录转换为带完整权限标签的词法文档。"""

        from agent.retrieval import IndexedDocument

        title = record.metadata.get("title", "受治理记忆")
        if not isinstance(title, str):
            title = "受治理记忆"
        return IndexedDocument(
            tenant_id=record.tenant_id,
            document_id=record.memory_id,
            scope=record.scope,
            owner_user_id=record.owner_user_id,
            session_id=record.session_id,
            title=title,
            text=record.content,
            source_ref=f"governed://{record.memory_id}",
            collection_id="governed",
            sensitivity=record.sensitivity,
            metadata={
                "path": f"governed://{record.memory_id}",
                "source": "governed-memory",
                "memory_id": record.memory_id,
                "version": record.version,
                "user_id": record.owner_user_id,
                "start_line": 1,
                "end_line": max(1, len(record.content.splitlines())),
                "source_ref": record.source_ref,
                "content_hash": record.content_hash,
            },
        )
    
    def _generate_chunk_id(self, path: str, start_line: int, end_line: int) -> str:
        """Generate unique chunk ID"""
        content = f"{path}:{start_line}:{end_line}"
        return hashlib.md5(content.encode('utf-8')).hexdigest()
    
    @staticmethod
    def _compute_temporal_decay(path: str, half_life_days: float = 30.0) -> float:
        """
        Compute temporal decay multiplier for dated memory files.
        
        Inspired by OpenClaw's temporal-decay: exponential decay based on file date.
        MEMORY.md and non-dated files are "evergreen" (no decay, multiplier=1.0).
        Daily files like memory/2025-03-01.md decay based on age.
        
        Formula: multiplier = exp(-ln2/half_life * age_in_days)
        """
        import re
        import math
        
        match = re.search(r'(\d{4})-(\d{2})-(\d{2})\.md$', path)
        if not match:
            return 1.0  # evergreen: MEMORY.md, non-dated files
        
        try:
            file_date = datetime(
                int(match.group(1)), int(match.group(2)), int(match.group(3))
            )
            age_days = (datetime.now() - file_date).days
            if age_days <= 0:
                return 1.0
            
            decay_lambda = math.log(2) / half_life_days
            return math.exp(-decay_lambda * age_days)
        except (ValueError, OverflowError):
            return 1.0
    
    def _merge_results(
        self,
        vector_results: List[SearchResult],
        keyword_results: List[SearchResult],
        vector_weight: float,
        keyword_weight: float
    ) -> List[SearchResult]:
        """Merge vector and keyword search results with temporal decay for dated files"""
        merged_map = {}
        
        for result in vector_results:
            key = (result.path, result.start_line, result.end_line)
            merged_map[key] = {
                'result': result,
                'vector_score': result.score,
                'keyword_score': 0.0
            }
        
        for result in keyword_results:
            key = (result.path, result.start_line, result.end_line)
            if key in merged_map:
                merged_map[key]['keyword_score'] = result.score
            else:
                merged_map[key] = {
                    'result': result,
                    'vector_score': 0.0,
                    'keyword_score': result.score
                }
        
        merged_results = []
        for entry in merged_map.values():
            combined_score = (
                vector_weight * entry['vector_score'] +
                keyword_weight * entry['keyword_score']
            )
            
            # Apply temporal decay for dated memory files
            result = entry['result']
            decay = self._compute_temporal_decay(result.path)
            combined_score *= decay
            
            merged_results.append(SearchResult(
                path=result.path,
                start_line=result.start_line,
                end_line=result.end_line,
                score=combined_score,
                snippet=result.snippet,
                source=result.source,
                user_id=result.user_id
            ))
        
        merged_results.sort(key=lambda r: r.score, reverse=True)
        return merged_results
