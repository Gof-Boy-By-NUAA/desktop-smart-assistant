"""CMRC 2018 检索评测数据加载器。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence


SOURCE_ID = "cmrc2018_dev"
_MANIFEST_PATH = Path(__file__).with_name("data_sources.json")


@dataclass(frozen=True)
class RetrievalDocument:
    """评测语料中的单个文档。"""

    document_id: str
    title: str
    text: str


@dataclass(frozen=True)
class RetrievalQuery:
    """带人工相关文档标注的评测问题。"""

    query_id: str
    text: str
    relevant_document_ids: Sequence[str]


@dataclass(frozen=True)
class RetrievalDataset:
    """检索语料和问题集合。"""

    source_id: str
    source_sha256: str
    documents: Sequence[RetrievalDocument]
    queries: Sequence[RetrievalQuery]


def load_source_manifest() -> Dict[str, Dict[str, str]]:
    """读取固定版本的数据来源清单。"""

    return json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))


def file_sha256(path: Path) -> str:
    """流式计算文件的 SHA-256。"""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            block = stream.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def verify_dataset_file(path: Path, source_id: str = SOURCE_ID) -> str:
    """校验数据文件是否与来源清单完全一致。"""

    source = load_source_manifest()[source_id]
    actual = file_sha256(path)
    expected = source["sha256"]
    if actual != expected:
        raise ValueError(
            "数据文件哈希不匹配: expected=%s actual=%s path=%s"
            % (expected, actual, path)
        )
    return actual


def ensure_cmrc2018_dev(cache_root: Optional[Path] = None) -> Path:
    """从固定 Git 提交获取并校验 CMRC 2018 开发集。"""

    source = load_source_manifest()[SOURCE_ID]
    cache_root = Path(cache_root or Path("benchmarks") / ".cache").resolve()
    cache_root.mkdir(parents=True, exist_ok=True)
    repository_dir = cache_root / "cmrc2018-source"
    dataset_path = repository_dir / Path(source["path"])

    if dataset_path.exists():
        verify_dataset_file(dataset_path)
        _verify_repository_commit(repository_dir, source["commit"])
        return dataset_path

    if repository_dir.exists():
        raise RuntimeError("CMRC 2018 缓存目录不完整，请人工检查: %s" % repository_dir)

    temporary_dir = Path(tempfile.mkdtemp(prefix="cmrc2018-", dir=str(cache_root)))
    try:
        _run_git(["init", str(temporary_dir)])
        _run_git(
            ["-C", str(temporary_dir), "remote", "add", "origin", source["repository"]]
        )
        _run_git(
            [
                "-C",
                str(temporary_dir),
                "fetch",
                "--depth",
                "1",
                "origin",
                source["commit"],
            ]
        )
        _run_git(["-C", str(temporary_dir), "checkout", "--detach", "FETCH_HEAD"])
        os.replace(str(temporary_dir), str(repository_dir))
    except Exception:
        shutil.rmtree(str(temporary_dir), ignore_errors=True)
        raise

    dataset_path = repository_dir / Path(source["path"])
    verify_dataset_file(dataset_path)
    _verify_repository_commit(repository_dir, source["commit"])
    return dataset_path


def load_cmrc2018_dev(path: Path, verify_source: bool = True) -> RetrievalDataset:
    """把官方阅读理解数据转换为文档检索任务。"""

    path = Path(path)
    source_hash = verify_dataset_file(path) if verify_source else file_sha256(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("CMRC 2018 开发集顶层必须是列表")

    documents: List[RetrievalDocument] = []
    queries: List[RetrievalQuery] = []
    seen_document_ids = set()
    seen_query_ids = set()

    for item in payload:
        document_id = _required_text(item, "context_id")
        if document_id in seen_document_ids:
            raise ValueError("发现重复 context_id: %s" % document_id)
        seen_document_ids.add(document_id)
        documents.append(
            RetrievalDocument(
                document_id=document_id,
                title=_optional_text(item, "title"),
                text=_required_text(item, "context_text"),
            )
        )

        raw_queries = item.get("qas")
        if not isinstance(raw_queries, list) or not raw_queries:
            raise ValueError("文档缺少 qas: %s" % document_id)
        for raw_query in raw_queries:
            query_id = _required_text(raw_query, "query_id")
            if query_id in seen_query_ids:
                raise ValueError("发现重复 query_id: %s" % query_id)
            seen_query_ids.add(query_id)
            queries.append(
                RetrievalQuery(
                    query_id=query_id,
                    text=_required_text(raw_query, "query_text"),
                    relevant_document_ids=(document_id,),
                )
            )

    return RetrievalDataset(
        source_id=SOURCE_ID,
        source_sha256=source_hash,
        documents=documents,
        queries=queries,
    )


def _required_text(value: object, key: str) -> str:
    """读取并校验非空字符串字段。"""

    if not isinstance(value, dict):
        raise ValueError("数据项必须是对象")
    field_value = value.get(key)
    if not isinstance(field_value, str) or not field_value.strip():
        raise ValueError("字段 %s 必须是非空字符串" % key)
    return field_value.strip()


def _optional_text(value: object, key: str) -> str:
    """读取允许为空但必须保持字符串类型的字段。"""

    if not isinstance(value, dict):
        raise ValueError("数据项必须是对象")
    field_value = value.get(key, "")
    if not isinstance(field_value, str):
        raise ValueError("字段 %s 必须是字符串" % key)
    return field_value.strip()


def _verify_repository_commit(repository_dir: Path, expected_commit: str) -> None:
    """确认缓存仓库位于来源清单指定提交。"""

    completed = subprocess.run(
        [
            "git",
            "-c",
            "safe.directory=%s" % repository_dir.resolve(),
            "-C",
            str(repository_dir),
            "rev-parse",
            "HEAD",
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    actual_commit = completed.stdout.strip()
    if actual_commit != expected_commit:
        raise ValueError(
            "数据仓库提交不匹配: expected=%s actual=%s"
            % (expected_commit, actual_commit)
        )


def _run_git(arguments: Sequence[str]) -> None:
    """运行 Git 命令并在失败时保留原始错误。"""

    subprocess.run(["git"] + list(arguments), check=True)
