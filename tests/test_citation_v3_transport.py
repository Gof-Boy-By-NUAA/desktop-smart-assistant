# encoding:utf-8
"""验证 Citation v3 在智能体循环和会话持久化边界中的完整传输。"""

import json
import tempfile
from pathlib import Path

from agent.memory.conversation_store import ConversationStore
from agent.protocol.agent_stream import (
    AgentStreamExecutor,
    MAX_CURRENT_TURN_RESULT_CHARS,
    _serialize_tool_result_for_model,
)
from bridge.agent_initializer import AgentInitializer


_DOCUMENT_ID = "doc-transport-7319"
_SECTION_ID = "1" * 64
_EVIDENCE_ID = "2" * 64
_CONTENT_HASH = "3" * 64
_QUOTE_HASH = "4" * 64
_SOURCE_REF_HASH = "5" * 64
_CITATION_URI = (
    f"knowledge://{_DOCUMENT_ID}/v/7/sections/{_SECTION_ID}"
    f"/evidence/{_EVIDENCE_ID}#bytes=0-42&content_hash={_CONTENT_HASH}"
    f"&quote_hash={_QUOTE_HASH}&source_ref_hash={_SOURCE_REF_HASH}"
    "&citation_version=3"
)


def _citation_payload(quote: str = "先执行零点校准，再记录读数。") -> dict:
    return {
        "uri": _CITATION_URI,
        "citation_version": 3,
        "document_id": _DOCUMENT_ID,
        "document_version": 7,
        "section_id": _SECTION_ID,
        "evidence_id": _EVIDENCE_ID,
        "source_ref": "knowledge/calibration.md",
        "source_ref_hash": _SOURCE_REF_HASH,
        "byte_start": 0,
        "byte_end": 42,
        "content_hash": _CONTENT_HASH,
        "quote_hash": _QUOTE_HASH,
        "quote": quote,
    }


def test_tool_result_serialization_preserves_all_citation_v3_fields():
    payload = {
        "result_count": 1,
        "results": [{"score": 1.0, "citation": _citation_payload()}],
    }

    content, transport_error, original_chars = _serialize_tool_result_for_model(
        {"status": "success", "result": payload},
        "knowledge_search",
        {"query": "零点校准", "limit": 5},
    )

    decoded = json.loads(content)
    citation = decoded["results"][0]["citation"]
    assert transport_error is False
    assert original_chars == len(content)
    assert citation == _citation_payload()
    assert citation["uri"] == _CITATION_URI
    assert citation["source_ref_hash"] == _SOURCE_REF_HASH
    assert citation["citation_version"] == 3


def test_oversized_knowledge_search_returns_complete_json_without_partial_uri():
    oversized = {
        "result_count": 20,
        "results": [
            {
                "rank": index + 1,
                "citation": _citation_payload(quote="证据正文" * 1000),
            }
            for index in range(20)
        ],
    }

    content, transport_error, original_chars = _serialize_tool_result_for_model(
        {"status": "success", "result": oversized},
        "knowledge_search",
        {"query": "校准", "limit": 20},
    )

    decoded = json.loads(content)
    assert transport_error is True
    assert original_chars > MAX_CURRENT_TURN_RESULT_CHARS
    assert len(content) < MAX_CURRENT_TURN_RESULT_CHARS
    assert decoded["status"] == "error"
    assert decoded["error"]["code"] == "tool_result_too_large"
    assert decoded["error"]["requested_limit"] == 20
    assert "knowledge://" not in content
    assert _CITATION_URI not in content


def test_prepare_messages_normalizes_synthetic_and_legacy_truncated_results():
    executor = object.__new__(AgentStreamExecutor)
    executor.messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "synthetic-result",
                    "content": "Cancelled by user before this tool finished.",
                    "is_error": True,
                },
                {
                    "type": "tool_result",
                    "tool_use_id": "legacy-result",
                    "content": (
                        '{"uri":"knowledge://tenant/doc/v/1/section/broken'
                        "\n[Output truncated: 90000 chars total]"
                    ),
                },
            ],
        }
    ]

    prepared = executor._prepare_messages()
    blocks = prepared[0]["content"]
    assert json.loads(blocks[0]["content"]) == (
        "Cancelled by user before this tool finished."
    )
    legacy = json.loads(blocks[1]["content"])
    assert legacy["error"]["code"] == "legacy_truncated_tool_result"
    assert blocks[1]["is_error"] is True
    assert "knowledge://" not in blocks[1]["content"]


def test_history_display_decodes_json_string_tool_result_without_extra_quotes():
    with tempfile.TemporaryDirectory() as temp_dir:
        store = ConversationStore(Path(temp_dir) / "display.db")
        content, transport_error, _ = _serialize_tool_result_for_model(
            {"status": "success", "result": "命令执行完成"},
            "bash",
            {"command": "status"},
        )
        assert transport_error is False
        store.append_messages(
            "display-session",
            [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": "检查状态"}],
                },
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "bash-call-1",
                            "name": "bash",
                            "input": {"command": "status"},
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "bash-call-1",
                            "content": content,
                        }
                    ],
                },
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "状态正常"}],
                },
            ],
            channel_type="web",
        )

        history = store.load_history_page("display-session", page=1, page_size=10)
        assistant_turn = next(
            message for message in history["messages"] if message["role"] == "assistant"
        )
        tool_step = next(
            step for step in assistant_turn["steps"] if step["type"] == "tool"
        )
        assert tool_step["result"] == "命令执行完成"


def test_final_answer_with_complete_v3_uri_survives_persist_and_reload():
    with tempfile.TemporaryDirectory() as temp_dir:
        store = ConversationStore(Path(temp_dir) / "conversation.db")
        tool_content, transport_error, _ = _serialize_tool_result_for_model(
            {
                "status": "success",
                "result": {
                    "result_count": 1,
                    "results": [{"citation": _citation_payload()}],
                },
            },
            "knowledge_search",
            {"query": "零点校准", "limit": 5},
        )
        assert transport_error is False

        final_answer = f"校准步骤已经核验，完整引用：{_CITATION_URI}"
        stored_messages = [
            {
                "role": "user",
                "content": [{"type": "text", "text": "怎样执行零点校准？"}],
            },
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "knowledge-call-1",
                        "name": "knowledge_search",
                        "input": {"query": "零点校准", "limit": 5},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "knowledge-call-1",
                        "content": tool_content,
                    }
                ],
            },
            {
                "role": "assistant",
                "content": [{"type": "text", "text": final_answer}],
            },
        ]
        store.append_messages("citation-session", stored_messages, channel_type="web")

        loaded = store.load_messages("citation-session", max_turns=10)
        loaded_tool_block = loaded[2]["content"][0]
        loaded_citation = json.loads(loaded_tool_block["content"])["results"][0][
            "citation"
        ]
        assert loaded_citation["uri"] == _CITATION_URI
        assert loaded_citation["source_ref_hash"] == _SOURCE_REF_HASH
        assert loaded_citation["citation_version"] == 3

        restored = AgentInitializer._filter_text_only_messages(loaded)
        assert restored == [
            {
                "role": "user",
                "content": [{"type": "text", "text": "怎样执行零点校准？"}],
            },
            {
                "role": "assistant",
                "content": [{"type": "text", "text": final_answer}],
            },
        ]
        assert _CITATION_URI in restored[1]["content"][0]["text"]
        assert all(
            block["type"] == "text"
            for message in restored
            for block in message["content"]
        )

        history = store.load_history_page("citation-session", page=1, page_size=10)
        assistant_turn = next(
            message for message in history["messages"] if message["role"] == "assistant"
        )
        assert assistant_turn["content"] == final_answer
        assert _CITATION_URI in assistant_turn["content"]


def test_web_and_desktop_citation_clicks_resolve_in_app_and_never_escape_to_os():
    root = Path(__file__).parents[1]
    web_source = (root / "channel/web/static/js/console.js").read_text(encoding="utf-8")
    desktop_markdown = (root / "desktop/src/renderer/src/components/Markdown.tsx").read_text(encoding="utf-8")
    desktop_main = (root / "desktop/src/main/index.ts").read_text(encoding="utf-8")
    external_policy = (root / "desktop/src/main/external-url.ts").read_text(encoding="utf-8")

    assert "link_governed_citations" in web_source
    assert "resolveKnowledgeCitation(href, a)" in web_source
    assert "fetch('/api/knowledge/citation/resolve'" in web_source
    assert "JSON.stringify({ uri: uri })" in web_source
    assert "link_governed_citations" in desktop_markdown
    assert "href.startsWith('knowledge://')" in desktop_markdown
    assert "onCitationLink?.(href)" in desktop_markdown
    assert "openExternalSafely(url)" in desktop_main
    assert "parsed.protocol !== 'https:' && parsed.protocol !== 'http:'" in external_policy
    assert "LOCAL_HOSTNAMES" in external_policy
    assert "shell.openExternal(url)" not in desktop_main
