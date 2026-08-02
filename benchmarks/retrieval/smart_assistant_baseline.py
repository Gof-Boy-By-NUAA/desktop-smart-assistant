"""SmartAssistant 原始关键词检索基线适配器。"""

from __future__ import annotations

import ast
import hashlib
import tempfile
from pathlib import Path
from typing import Dict, Sequence

from agent.memory.storage import MemoryChunk, MemoryStorage

from .dataset import RetrievalDocument


SMART_ASSISTANT_SOURCE_ZIP_SHA256 = (
    "f121b65e04e0cf7b008e6404f7a9e3ce11cee8457ed58252b9a57b20366bcf58"
)
ORIGINAL_STORAGE_FILE_SHA256 = (
    "428973c3c834cfb3875e1bba6a02707ffb8f9acb912157a07181a10221298c98"
)
ORIGINAL_MEMORY_STORAGE_AST_SHA256 = (
    "165d8c5c16fcd07dbe484eb8cd3e367302e34e9b12ffe2e5a72f3807fab07b98"
)
_NON_BASELINE_METHODS = frozenset(
    {"delete_by_source", "delete_missing_file_sources"}
)


class SmartAssistantKeywordBaseline:
    """调用经原始 ZIP 抽象语法树指纹核验的 MemoryStorage。"""

    engine_id = "smart-assistant-memory-storage-keyword"

    def __init__(self):
        self._baseline_evidence = verify_original_memory_storage()
        self._temporary_dir = tempfile.TemporaryDirectory()
        self._storage = MemoryStorage(
            Path(self._temporary_dir.name) / "smart-assistant-baseline.db"
        )
        self._path_to_document_id: Dict[str, str] = {}

    @property
    def capabilities(self) -> Dict[str, object]:
        """返回当前 SQLite 检索能力。"""

        return {
            "fts5_available": self._storage.fts5_available,
            "trigram_fts5_available": self._storage.trigram_fts5_available,
            "vector_enabled": False,
            **self._baseline_evidence,
        }

    @property
    def implementation_sha256(self) -> str:
        """绑定基线适配器与已核验的原版类指纹。"""

        return implementation_fingerprint()

    @property
    def implementation_paths(self) -> Sequence[str]:
        """列出基线适配器和被 AST 指纹绑定的原版实现路径。"""

        return implementation_paths()

    def index(self, documents: Sequence[RetrievalDocument]) -> None:
        """以单文档单分块方式建立基线索引。"""

        chunks = []
        for document in documents:
            path = "cmrc2018/%s.md" % document.document_id
            self._path_to_document_id[path] = document.document_id
            text = "%s\n%s" % (document.title, document.text)
            chunks.append(
                MemoryChunk(
                    id=document.document_id,
                    user_id=None,
                    scope="shared",
                    source="knowledge",
                    path=path,
                    start_line=1,
                    end_line=1,
                    text=text,
                    embedding=None,
                    hash=MemoryStorage.compute_hash(text),
                    metadata={"dataset": "cmrc2018_dev"},
                )
            )
        self._storage.save_chunks_batch(chunks)

    def search(self, query: str, limit: int = 10) -> Sequence[str]:
        """执行现有关键词检索并返回文档标识。"""

        results = self._storage.search_keyword(
            query=query,
            user_id=None,
            scopes=["shared"],
            limit=limit,
        )
        return [
            self._path_to_document_id[result.path]
            for result in results
            if result.path in self._path_to_document_id
        ]

    def close(self) -> None:
        """释放数据库和临时目录。"""

        self._storage.close()
        self._temporary_dir.cleanup()


def implementation_paths() -> Sequence[str]:
    """公开原版基线指纹绑定的仓库相对路径。"""

    return (
        "benchmarks/retrieval/smart_assistant_baseline.py",
        "agent/memory/storage.py",
    )


def implementation_fingerprint(
    repository_root: Path | None = None,
) -> str:
    """从当前适配器字节和冻结原版类指纹复算基线标识。"""

    root = Path(repository_root or Path(__file__).resolve().parents[2])
    digest = hashlib.sha256()
    digest.update((root / "benchmarks/retrieval/smart_assistant_baseline.py").read_bytes())
    digest.update(ORIGINAL_MEMORY_STORAGE_AST_SHA256.encode("ascii"))
    return digest.hexdigest()


def verify_original_memory_storage() -> Dict[str, object]:
    """核验基线调用到的类与原始 SmartAssistant ZIP 中的实现一致。"""

    repository_root = Path(__file__).resolve().parents[2]
    source = (repository_root / "agent/memory/storage.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    storage_class = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "MemoryStorage"
        ),
        None,
    )
    if storage_class is None:
        raise RuntimeError("无法定位 SmartAssistant 原版 MemoryStorage 基线")
    storage_class.body = [
        node
        for node in storage_class.body
        if not (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in _NON_BASELINE_METHODS
        )
    ]
    normalized = ast.dump(storage_class, include_attributes=False).encode("utf-8")
    actual = hashlib.sha256(normalized).hexdigest()
    if actual != ORIGINAL_MEMORY_STORAGE_AST_SHA256:
        raise RuntimeError(
            "当前 MemoryStorage 与冻结的 SmartAssistant 原版基线不一致"
        )
    return {
        "smart_assistant_source_zip_sha256": SMART_ASSISTANT_SOURCE_ZIP_SHA256,
        "original_storage_file_sha256": ORIGINAL_STORAGE_FILE_SHA256,
        "original_memory_storage_ast_sha256": actual,
        "original_memory_storage_ast_verified": True,
        "excluded_non_baseline_methods": sorted(_NON_BASELINE_METHODS),
    }
