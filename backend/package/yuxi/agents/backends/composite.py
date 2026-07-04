from __future__ import annotations

import base64
import tempfile
from dataclasses import dataclass
from pathlib import Path

from deepagents.backends.composite import (
    CompositeBackend,
    _remap_file_info_path,
    _route_for_path,
    _strip_route_from_pattern,
)
from deepagents.backends.protocol import FileInfo, GlobResult
from deepagents.middleware.filesystem import FilesystemMiddleware
from langchain_core.messages import ToolMessage

from yuxi.agents.skills.service import normalize_string_list
from yuxi.utils.paths import VIRTUAL_PATH_CONVERSATION_HISTORY, VIRTUAL_PATH_LARGE_TOOL_RESULTS, VIRTUAL_PATH_OUTPUTS

from .sandbox import ProvisionerSandboxBackend
from .skills_backend import SelectedSkillsReadonlyBackend

_TOOL_RESULT_EVICTION_EXEMPT_TOOLS = frozenset({"read_file", "open_kb_document"})
_PARSER_DOCUMENT_EXTENSIONS = frozenset(
    {
        ".csv", ".doc", ".docm", ".docx", ".et", ".htm", ".html", ".json",
        ".md", ".ofd", ".pdf", ".pptx", ".txt", ".wps", ".xls", ".xlsx",
    }
)


def _coerce_glob_result(result) -> GlobResult:
    if isinstance(result, GlobResult):
        return result
    return GlobResult(matches=result or [])


class CustomCompositeBackend(CompositeBackend):
    """修复 glob 路由逻辑的 CompositeBackend。"""

    def glob(self, pattern: str, path: str = "/") -> GlobResult:
        backend, backend_path, route_prefix = _route_for_path(
            default=self.default,
            sorted_routes=self.sorted_routes,
            path=path,
        )
        if route_prefix is not None:
            result = _coerce_glob_result(backend.glob(pattern, backend_path))
            if result.error:
                return result
            return GlobResult(matches=[_remap_file_info_path(fi, route_prefix) for fi in (result.matches or [])])

        if path is None or path == "/":
            results: list[FileInfo] = []
            default_result = _coerce_glob_result(self.default.glob(pattern, path))
            if default_result.error:
                return default_result
            results.extend(default_result.matches or [])
            for route_prefix, backend in self.routes.items():
                route_pattern = _strip_route_from_pattern(pattern, route_prefix)
                result = _coerce_glob_result(backend.glob(route_pattern, "/"))
                if result.error:
                    return result
                results.extend(_remap_file_info_path(fi, route_prefix) for fi in (result.matches or []))
            results.sort(key=lambda x: x.get("path", ""))
            return GlobResult(matches=results)

        return _coerce_glob_result(self.default.glob(pattern, path))

    async def aglob(self, pattern: str, path: str = "/") -> GlobResult:
        backend, backend_path, route_prefix = _route_for_path(
            default=self.default,
            sorted_routes=self.sorted_routes,
            path=path,
        )
        if route_prefix is not None:
            result = _coerce_glob_result(await backend.aglob(pattern, backend_path))
            if result.error:
                return result
            return GlobResult(matches=[_remap_file_info_path(fi, route_prefix) for fi in (result.matches or [])])

        if path is None or path == "/":
            results: list[FileInfo] = []
            default_result = _coerce_glob_result(await self.default.aglob(pattern, path))
            if default_result.error:
                return default_result
            results.extend(default_result.matches or [])
            for route_prefix, backend in self.routes.items():
                route_pattern = _strip_route_from_pattern(pattern, route_prefix)
                result = _coerce_glob_result(await backend.aglob(route_pattern, "/"))
                if result.error:
                    return result
                results.extend(_remap_file_info_path(fi, route_prefix) for fi in (result.matches or []))
            results.sort(key=lambda x: x.get("path", ""))
            return GlobResult(matches=results)

        return _coerce_glob_result(await self.default.aglob(pattern, path))


class YuxiFilesystemMiddleware(FilesystemMiddleware):
    """Filesystem middleware that budgets large tool outputs before they hit model context."""

    def wrap_tool_call(self, request, handler):
        tool_result = handler(request)

        tool_result = self._parse_read_file_document_result(request, tool_result)

        if self._tool_token_limit_before_evict is None:
            return tool_result
        if request.tool_call["name"] in _TOOL_RESULT_EVICTION_EXEMPT_TOOLS:
            return tool_result

        return self._intercept_large_tool_result(tool_result, request.runtime)

    async def awrap_tool_call(self, request, handler):
        tool_result = await handler(request)

        tool_result = await self._aparse_read_file_document_result(request, tool_result)

        if self._tool_token_limit_before_evict is None:
            return tool_result
        if request.tool_call["name"] in _TOOL_RESULT_EVICTION_EXEMPT_TOOLS:
            return tool_result

        return await self._aintercept_large_tool_result(tool_result, request.runtime)

    @staticmethod
    def _read_file_path_from_result(result: ToolMessage) -> str | None:
        path = result.additional_kwargs.get("read_file_path")
        return path if isinstance(path, str) and path else None

    @staticmethod
    def _read_file_base64_block(result: ToolMessage) -> dict | None:
        blocks = result.content_blocks
        if len(blocks) != 1:
            return None
        block = blocks[0]
        if block.get("type") != "file":
            return None
        content = block.get("base64")
        return block if isinstance(content, str) and content else None

    @staticmethod
    def _temporary_document_path(file_path: str, base64_content: str) -> Path:
        suffix = Path(file_path).suffix.lower()
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp_file:
            tmp_file.write(base64.b64decode(base64_content, validate=True))
            return Path(tmp_file.name)

    @staticmethod
    def _document_tool_message(result: ToolMessage, markdown: str) -> ToolMessage:
        return ToolMessage(
            content=markdown,
            name=result.name,
            tool_call_id=result.tool_call_id,
            status=result.status,
            additional_kwargs={**result.additional_kwargs, "read_file_parsed_as_markdown": True},
        )

    def _parse_read_file_document_result(self, request, tool_result):
        if request.tool_call["name"] != "read_file" or not isinstance(tool_result, ToolMessage):
            return tool_result

        file_path = self._read_file_path_from_result(tool_result)
        if file_path is None or Path(file_path).suffix.lower() not in _PARSER_DOCUMENT_EXTENSIONS:
            return tool_result

        block = self._read_file_base64_block(tool_result)
        if block is None:
            return tool_result

        tmp_path = self._temporary_document_path(file_path, block["base64"])
        try:
            from yuxi.knowledge.parser.unified import Parser

            return self._document_tool_message(tool_result, Parser.parse(str(tmp_path)))
        finally:
            tmp_path.unlink(missing_ok=True)

    async def _aparse_read_file_document_result(self, request, tool_result):
        if request.tool_call["name"] != "read_file" or not isinstance(tool_result, ToolMessage):
            return tool_result

        file_path = self._read_file_path_from_result(tool_result)
        if file_path is None or Path(file_path).suffix.lower() not in _PARSER_DOCUMENT_EXTENSIONS:
            return tool_result

        block = self._read_file_base64_block(tool_result)
        if block is None:
            return tool_result

        tmp_path = self._temporary_document_path(file_path, block["base64"])
        try:
            from yuxi.knowledge.parser.unified import Parser

            return self._document_tool_message(tool_result, await Parser.aparse(str(tmp_path)))
        finally:
            tmp_path.unlink(missing_ok=True)


@dataclass(frozen=True)
class _BackendScope:
    thread_id: str
    uid: str
    readable_skills: list[str]
    file_thread_id: str
    skills_thread_id: str

    @classmethod
    def from_runtime(cls, runtime) -> _BackendScope:
        config = getattr(runtime, "config", None)
        configurable = config.get("configurable", {}) if isinstance(config, dict) else {}
        context = getattr(runtime, "context", None)
        state = getattr(runtime, "state", None)
        return cls.from_sources(
            configurable if isinstance(configurable, dict) else {},
            context,
            state if isinstance(state, dict) else {},
            readable_skills_source=context,
            error_context="runtime configurable context",
        )

    @classmethod
    def from_sources(cls, *sources, readable_skills_source, error_context: str) -> _BackendScope:
        def string_value(key: str) -> str | None:
            for source in sources:
                value = source.get(key) if isinstance(source, dict) else getattr(source, key, None)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            return None

        thread_id = string_value("thread_id")
        if not thread_id:
            raise ValueError(f"thread_id is required in {error_context}")

        uid = string_value("uid")
        if not uid:
            raise ValueError(f"uid is required in {error_context}")

        selected = getattr(readable_skills_source, "_readable_skills", [])
        return cls(
            thread_id=thread_id,
            uid=uid,
            readable_skills=normalize_string_list(selected if isinstance(selected, list) else []),
            file_thread_id=string_value("file_thread_id") or thread_id,
            skills_thread_id=string_value("skills_thread_id") or thread_id,
        )

    def create_backend(self) -> CompositeBackend:
        return CustomCompositeBackend(
            default=ProvisionerSandboxBackend(
                thread_id=self.thread_id,
                uid=self.uid,
                readable_skills=self.readable_skills,
                file_thread_id=self.file_thread_id,
                skills_thread_id=self.skills_thread_id,
            ),
            routes={
                "/skills/": SelectedSkillsReadonlyBackend(selected_slugs=self.readable_skills),
            },
            artifacts_root=VIRTUAL_PATH_OUTPUTS,
        )


def create_agent_composite_backend(runtime) -> CompositeBackend:
    return _BackendScope.from_runtime(runtime).create_backend()


def create_agent_filesystem_middleware(
    tool_token_limit_before_evict: int | None = None,
    *,
    context=None,
) -> FilesystemMiddleware:
    backend = create_agent_composite_backend
    if context is not None:
        backend = _BackendScope.from_sources(
            context,
            readable_skills_source=context,
            error_context="runtime context",
        ).create_backend()
    middleware = YuxiFilesystemMiddleware(
        backend=backend,
        tool_token_limit_before_evict=tool_token_limit_before_evict,
    )
    middleware._large_tool_results_prefix = VIRTUAL_PATH_LARGE_TOOL_RESULTS
    middleware._conversation_history_prefix = VIRTUAL_PATH_CONVERSATION_HISTORY
    return middleware
