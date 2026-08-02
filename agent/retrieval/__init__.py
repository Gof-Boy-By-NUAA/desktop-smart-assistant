"""权限化检索模块。"""

from .lexical import (
    IndexedDocument,
    LexicalSearchResult,
    TenantAwareLexicalIndex,
)

__all__ = [
    "IndexedDocument",
    "LexicalSearchResult",
    "TenantAwareLexicalIndex",
]
