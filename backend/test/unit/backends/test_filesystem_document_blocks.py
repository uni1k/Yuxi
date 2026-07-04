from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

from langchain_core.messages import ToolMessage

from yuxi.agents.backends.composite import create_agent_filesystem_middleware


def test_filesystem_middleware_parses_document_file_blocks_before_model_context(monkeypatch) -> None:
    parsed_sources: list[str] = []

    def _parse_doc(source: str, params: dict | None = None) -> str:
        parsed_sources.append(source)
        assert source.endswith(".doc")
        assert params is None
        return "# Parsed DOC\n\n正文"

    parser_module = ModuleType("yuxi.knowledge.parser.unified")
    parser_module.Parser = type("Parser", (), {"parse": staticmethod(_parse_doc)})
    monkeypatch.setitem(sys.modules, "yuxi.knowledge.parser.unified", parser_module)

    middleware = create_agent_filesystem_middleware(tool_token_limit_before_evict=None)
    request = SimpleNamespace(tool_call={"name": "read_file"}, runtime=SimpleNamespace())
    tool_result = ToolMessage(
        content=[{"type": "file", "base64": "0M8R4A==", "mime_type": "application/msword"}],
        name="read_file",
        tool_call_id="call-read",
        additional_kwargs={
            "read_file_path": "/home/gem/user-data/uploads/report.doc",
            "read_file_media_type": "application/msword",
        },
    )

    result = middleware.wrap_tool_call(request, lambda _: tool_result)

    assert result.content == "# Parsed DOC\n\n正文"
    assert result.content_blocks == [{"type": "text", "text": "# Parsed DOC\n\n正文"}]
    assert result.additional_kwargs["read_file_path"] == "/home/gem/user-data/uploads/report.doc"
    assert result.additional_kwargs["read_file_parsed_as_markdown"] is True
    assert len(parsed_sources) == 1
