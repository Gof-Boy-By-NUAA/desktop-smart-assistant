"""Web SSE 工具结果的严格 JSON 与 Citation v3 传输契约。"""

import hashlib
import json
from pathlib import Path
from queue import Queue
from types import SimpleNamespace

from channel.web import web_channel


# @singleton 把类包装成工厂，这里从闭包取得原始类以隔离全局单例状态。
WebChannel = dict(zip(
    web_channel.WebChannel.__code__.co_freevars,
    (cell.cell_contents for cell in web_channel.WebChannel.__closure__),
))["cls"]


def _strict_sse_tool_end(result):
    request_id = "citation-transport-request"
    channel = SimpleNamespace(
        sse_queues={request_id: Queue()},
        sse_last_active={},
    )
    callback = WebChannel._make_sse_callback(channel, request_id)
    callback({
        "type": "tool_execution_end",
        "data": {
            "tool_call_id": "knowledge-call",
            "tool_name": "knowledge_search",
            "status": "success",
            "result": result,
            "execution_time": 0.125,
        },
    })

    generator = WebChannel.stream_response(channel, request_id)
    raw_event = next(generator).decode("utf-8")
    generator.close()
    assert raw_event.startswith("data: ")
    return json.loads(raw_event[6:])


def _citation(index: int, quote_size: int = 128) -> dict:
    section_id = hashlib.sha256(f"section-{index}".encode()).hexdigest()
    evidence_id = hashlib.sha256(f"evidence-{index}".encode()).hexdigest()
    content_hash = hashlib.sha256(f"content-{index}".encode()).hexdigest()
    quote_hash = hashlib.sha256(f"quote-{index}".encode()).hexdigest()
    source_ref_hash = hashlib.sha256(f"source-{index}".encode()).hexdigest()
    uri = (
        f"knowledge://document-{index}/v/1/sections/{section_id}"
        f"/evidence/{evidence_id}#bytes=0-{quote_size}"
        f"&content_hash={content_hash}&quote_hash={quote_hash}"
        f"&source_ref_hash={source_ref_hash}&citation_version=3"
    )
    return {
        "uri": uri,
        "citation_version": 3,
        "document_id": f"document-{index}",
        "document_version": 1,
        "section_id": section_id,
        "evidence_id": evidence_id,
        "source_ref": f"cmrc2018/{index}.json",
        "source_ref_hash": source_ref_hash,
        "byte_start": 0,
        "byte_end": quote_size,
        "content_hash": content_hash,
        "quote_hash": quote_hash,
        "quote": "证据" * quote_size,
    }


def _knowledge_result(quote_size: int = 128) -> dict:
    return {
        "result_count": 20,
        "results": [
            {
                "title": f"知识文档 {index}",
                "score": 1.0 - index / 100,
                "citation": _citation(index, quote_size=quote_size),
            }
            for index in range(20)
        ],
    }


def test_sse_preserves_json_object_instead_of_python_repr():
    result = {
        "result_count": 1,
        "results": [{"title": "可解析对象", "score": 0.99}],
    }

    event = _strict_sse_tool_end(result)

    assert event["result"] == result
    assert event["result_transport"]["encoding"] == "json"
    assert event["result_transport"]["truncated"] is False
    assert isinstance(event["result"], dict)


def test_sse_preserves_twenty_complete_citation_v3_results():
    result = _knowledge_result()

    event = _strict_sse_tool_end(result)

    assert event["result_transport"]["truncated"] is False
    assert len(event["result"]["results"]) == 20
    for expected, actual in zip(result["results"], event["result"]["results"]):
        assert actual["citation"]["citation_version"] == 3
        assert actual["citation"]["source_ref_hash"] == expected["citation"]["source_ref_hash"]
        assert actual["citation"]["uri"] == expected["citation"]["uri"]
        assert actual["citation"]["uri"].endswith("&citation_version=3")


def test_oversized_result_uses_summary_without_cutting_twenty_citations():
    result = _knowledge_result(quote_size=10_000)
    assert len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > 256 * 1024

    event = _strict_sse_tool_end(result)

    transport = event["result_transport"]
    summary = event["result"]
    assert transport["truncated"] is True
    assert transport["strategy"] == "structured_summary"
    assert transport["citation_count"] == 20
    assert transport["preserved_citation_count"] == 20
    assert transport["transmitted_json_bytes"] <= transport["max_json_bytes"]
    assert summary["_transport_summary"] is True
    assert summary["result_count"] == 20
    assert len(summary["citations"]) == 20
    for index, citation_ref in enumerate(summary["citations"]):
        expected = result["results"][index]["citation"]
        assert citation_ref == {
            "citation_version": 3,
            "source_ref_hash": expected["source_ref_hash"],
            "uri": expected["uri"],
        }


def test_non_json_object_has_typed_marker_and_never_uses_repr():
    class ReprMustNotLeak:
        def __repr__(self):
            return "PYTHON_REPR_MUST_NOT_APPEAR"

    event = _strict_sse_tool_end({"opaque": ReprMustNotLeak()})

    encoded = json.dumps(event, ensure_ascii=False, allow_nan=False)
    assert "PYTHON_REPR_MUST_NOT_APPEAR" not in encoded
    assert event["result"]["opaque"]["reason"] == "unsupported_json_type"
    assert event["result"]["opaque"]["_transport_type"].endswith("ReprMustNotLeak")


def test_frontend_renders_structured_results_and_summary_state():
    root = Path(__file__).parents[1]
    source = (root / "channel/web/static/js/console.js").read_text(encoding="utf-8")
    tool_end_block = source[
        source.index("} else if (item.type === 'tool_end')"):
        source.index("} else if (item.type === 'image')")
    ]

    assert "formatToolResultForDisplay(" in tool_end_block
    assert "item.result_transport?.truncated === true" in tool_end_block
    assert "String(item.result)" not in tool_end_block
    assert "Structured summary" in tool_end_block
    assert "结构化摘要" in tool_end_block
    assert "function stableToolResultValue(value)" in source
    assert "Object.keys(value).sort()" in source
    assert "function stableStringifyToolResult(value)" in source
    assert "JSON.stringify(stableToolResultValue(value), null, 2)" in source
    assert "结果过大，已显示结构化摘要" in source
