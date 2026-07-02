"""
一键导入 + 自动图谱索引接口

此文件为私有部署扩展接口，独立于上游默认实现。
当上游更新时，只需在 __init__.py 中保持一行导入即可。

使用方式：
    POST /api/knowledge/databases/{kb_id}/import-and-graph

功能：
    1. 上传文件到 MinIO
    2. 添加文件记录到知识库
    3. 解析文件（MinerU）
    4. 向量化入库（Embedding）
    5. 自动构建知识图谱（如果已配置）
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Final, Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from yuxi import knowledge_base
from yuxi.knowledge.graphs.milvus_graph_service import MilvusGraphService
from yuxi.knowledge.parser import is_supported_file_extension
from yuxi.knowledge.utils import calculate_content_hash
from yuxi.services.task_service import TaskContext, tasker
from yuxi.storage.minio.client import MinIOClient, aupload_file_to_minio, get_minio_client
from yuxi.storage.postgres.models_business import User
from yuxi.utils import logger
from yuxi.utils.upload_utils import MAX_UPLOAD_SIZE_BYTES, read_upload_with_limit

from server.utils.auth_middleware import get_admin_user

from server.routers.knowledge_router import (
    ACTIVE_DOCUMENT_ACTION_TASK_STATUSES,
    _ensure_database_supports_documents,
    _has_running_graph_build_task,
)

import_graph = APIRouter(prefix="/knowledge", tags=["import-graph"])

DEFAULT_GRAPH_BATCH_SIZE: Final[int] = 20
DEFAULT_OCR_ENGINE: Final[str] = "mineru_ocr"
GRAPH_BATCH_SIZE_MAX: Final[int] = 200
GRAPH_BATCH_SIZE_MIN: Final[int] = 1
GRAPH_BUILD_SCOPE: Final[str] = "knowledge_base_pending_chunks"

type JsonPrimitive = str | int | float | bool | None
type JsonValue = JsonPrimitive | list[JsonValue] | dict[str, JsonValue]


class ImportPipelineOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parse: bool = True
    index: bool = True
    build_graph: bool = True


class ImportParseOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ocr_engine: str = DEFAULT_OCR_ENGINE
    ocr_engine_config: dict[str, JsonValue] = Field(default_factory=dict)


class ImportIndexOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chunk_preset_id: str | None = None
    max_tokens: int | None = Field(default=None, ge=1)
    overlap_percent: int | None = Field(default=None, ge=0, le=99)
    overlap_ratio: float | None = Field(default=None, ge=0, le=1)


class ImportGraphOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    batch_size: int = Field(
        default=DEFAULT_GRAPH_BATCH_SIZE,
        ge=GRAPH_BATCH_SIZE_MIN,
        le=GRAPH_BATCH_SIZE_MAX,
    )


class ImportDocumentOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_path: str | None = None
    parent_id: str | None = None
    duplicate_strategy: Literal["reject", "skip"] = "reject"


class ImportGraphRequestOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pipeline: ImportPipelineOptions = Field(default_factory=ImportPipelineOptions)
    parse_options: ImportParseOptions = Field(default_factory=ImportParseOptions)
    index_options: ImportIndexOptions = Field(default_factory=ImportIndexOptions)
    graph_options: ImportGraphOptions = Field(default_factory=ImportGraphOptions)
    document_options: ImportDocumentOptions = Field(default_factory=ImportDocumentOptions)

    @model_validator(mode="after")
    def validate_pipeline(self) -> ImportGraphRequestOptions:
        if self.pipeline.index and not self.pipeline.parse:
            raise ValueError("pipeline.index=true requires pipeline.parse=true")
        if self.pipeline.build_graph and not self.pipeline.index:
            raise ValueError("pipeline.build_graph=true requires pipeline.index=true")
        return self


class ImportGraphAddRecordError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class ImportGraphPipelineRequest:
    kb_id: str
    filename: str
    file_size: int
    content_hash: str
    minio_url: str
    operator_id: str
    processing_params: dict[str, JsonValue]
    options: ImportGraphRequestOptions


def load_import_graph_options(
    options_json: str | None,
    *,
    legacy_ocr_engine: str = DEFAULT_OCR_ENGINE,
    legacy_graph_batch_size: int = DEFAULT_GRAPH_BATCH_SIZE,
) -> ImportGraphRequestOptions:
    if options_json:
        try:
            parsed = ImportGraphRequestOptions.model_validate_json(options_json)
        except ValidationError as exc:
            raise ValueError(exc.json()) from exc
        return parsed

    return ImportGraphRequestOptions(
        parse_options=ImportParseOptions(ocr_engine=legacy_ocr_engine),
        graph_options=ImportGraphOptions(batch_size=legacy_graph_batch_size),
    )


def build_processing_params(options: ImportGraphRequestOptions) -> dict[str, JsonValue]:
    params: dict[str, JsonValue] = {
        "ocr_engine": options.parse_options.ocr_engine,
        "ocr_engine_config": dict(options.parse_options.ocr_engine_config),
    }

    if options.index_options.chunk_preset_id:
        params["chunk_preset_id"] = options.index_options.chunk_preset_id

    chunk_parser_config: dict[str, JsonValue] = {}
    if options.index_options.max_tokens is not None:
        chunk_parser_config["chunk_token_num"] = options.index_options.max_tokens

    overlap_percent = resolve_overlap_percent(options.index_options)
    if overlap_percent is not None:
        chunk_parser_config["overlapped_percent"] = overlap_percent

    if chunk_parser_config:
        params["chunk_parser_config"] = chunk_parser_config

    return params


def resolve_overlap_percent(options: ImportIndexOptions) -> int | None:
    if options.overlap_percent is not None:
        return options.overlap_percent
    if options.overlap_ratio is None:
        return None
    return int(round(options.overlap_ratio * 100))


def build_duplicate_skip_response(
    *,
    content_hash: str,
    file_size: int,
    filename: str,
    options: ImportGraphRequestOptions,
) -> dict[str, JsonValue]:
    return {
        "message": "知识库中已存在相同内容的文件，已按调用方策略跳过",
        "status": "skipped",
        "duplicate": True,
        "options": options.model_dump(mode="json"),
        "file_info": {
            "filename": filename,
            "size": file_size,
            "content_hash": content_hash,
        },
    }


async def run_import_graph_pipeline(
    context: TaskContext,
    request: ImportGraphPipelineRequest,
) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {
        "overall_status": "running",
        "file_info": {
            "filename": request.filename,
            "size": request.file_size,
            "content_hash": request.content_hash,
            "minio_url": request.minio_url,
        },
        "options": request.options.model_dump(mode="json"),
        "document_import": {"status": "running"},
        "stages": {},
        "graph_build": {"status": "pending", "scope": GRAPH_BUILD_SCOPE},
    }

    await context.set_message("阶段 1/4：添加文件记录")
    await context.set_progress(5.0, "添加文件记录到知识库")
    add_params = {
        "content_hashes": {request.minio_url: request.content_hash},
        "file_sizes": {request.minio_url: request.file_size},
        "content_type": "file",
        "source_path": request.options.document_options.source_path,
        "parent_id": request.options.document_options.parent_id,
        **request.processing_params,
    }
    try:
        file_meta = await knowledge_base.add_file_record(
            request.kb_id,
            request.minio_url,
            params=add_params,
            operator_id=request.operator_id,
        )
    except Exception as exc:
        logger.error(
            "一键导入-添加记录失败 kb_id=%s filename=%s error=%s",
            request.kb_id,
            request.filename,
            exc,
        )
        raise ImportGraphAddRecordError(str(exc)) from exc

    file_id = file_meta["file_id"]
    result["stages"] = {"add_record": {"status": "success", "file_id": file_id}}
    result["document_import"] = {
        "status": "success",
        "file_id": file_id,
        "file_status": "uploaded",
    }

    if request.options.pipeline.parse:
        await context.set_message("阶段 2/4：解析文件")
        await context.set_progress(20.0, "解析文档内容")
        file_meta = await knowledge_base.parse_file(
            request.kb_id,
            file_id,
            operator_id=request.operator_id,
        )
        result["stages"]["parse"] = {"status": file_meta.get("status", "parsed")}
        result["document_import"] = {
            "status": "success",
            "file_id": file_id,
            "file_status": file_meta.get("status", "parsed"),
        }
    else:
        result["stages"]["parse"] = {"status": "skipped", "reason": "调用方禁用自动解析"}

    if request.options.pipeline.index:
        await context.set_message("阶段 3/4：向量化入库")
        await context.set_progress(45.0, "文档分块并生成向量")
        file_meta = await knowledge_base.index_file(
            request.kb_id,
            file_id,
            operator_id=request.operator_id,
        )
        result["stages"]["index"] = {"status": file_meta.get("status", "indexed")}
        result["document_import"] = {
            "status": "success",
            "file_id": file_id,
            "file_status": file_meta.get("status", "indexed"),
        }
    else:
        result["stages"]["index"] = {"status": "skipped", "reason": "调用方禁用自动入库"}

    if request.options.pipeline.build_graph:
        await context.set_message("阶段 4/4：检查图谱配置")
        await context.set_progress(70.0, "检查是否配置了图谱抽取器")
        graph_service = MilvusGraphService()
        try:
            graph_status = await graph_service.get_status(request.kb_id)
        except Exception:
            graph_status = {}

        if graph_status.get("locked"):
            await context.set_progress(75.0, "开始构建知识图谱")
            if await _has_running_graph_build_task(request.kb_id):
                result["graph_build"] = {
                    "status": "skipped",
                    "scope": GRAPH_BUILD_SCOPE,
                    "reason": "已有正在运行的图谱构建任务",
                }
            else:
                try:
                    build_result = await graph_service.build_pending_chunks(
                        request.kb_id,
                        batch_size=request.options.graph_options.batch_size,
                        context=context,
                    )
                    result["graph_build"] = {
                        "status": "success",
                        "scope": GRAPH_BUILD_SCOPE,
                        "success": build_result.get("success", 0),
                        "failed": build_result.get("failed", 0),
                    }
                except Exception as exc:
                    logger.warning(
                        "一键导入-图谱构建异常 kb_id=%s file_id=%s error=%s",
                        request.kb_id,
                        file_id,
                        exc,
                    )
                    result["graph_build"] = {
                        "status": "failed",
                        "scope": GRAPH_BUILD_SCOPE,
                        "error": str(exc),
                    }
        else:
            result["graph_build"] = {
                "status": "skipped",
                "scope": GRAPH_BUILD_SCOPE,
                "reason": "未配置图谱抽取器，请先在知识库设置中配置图谱构建",
            }
    else:
        result["graph_build"] = {
            "status": "skipped",
            "scope": GRAPH_BUILD_SCOPE,
            "reason": "调用方禁用自动图谱构建",
        }

    graph_result = result["graph_build"]
    if result["document_import"]["status"] != "success":
        result["overall_status"] = "failed"
        final_message = "文档导入失败"
    elif graph_result["status"] == "failed":
        result["overall_status"] = "partial_success"
        final_message = "文档导入完成，但图谱构建失败"
    elif (
        request.options.pipeline.build_graph
        and graph_result["status"] == "skipped"
        and graph_result.get("reason") != "调用方禁用自动图谱构建"
    ):
        result["overall_status"] = "partial_success"
        final_message = "文档导入完成，但未执行知识图谱构建"
    elif graph_result["status"] == "success":
        result["overall_status"] = "success"
        final_message = "文档导入完成，图谱构建成功"
    elif not request.options.pipeline.parse:
        result["overall_status"] = "success"
        final_message = "文件已添加到知识库"
    elif not request.options.pipeline.index:
        result["overall_status"] = "success"
        final_message = "文档解析完成"
    else:
        result["overall_status"] = "success"
        final_message = "文档导入完成"

    await context.set_progress(100.0, final_message)
    await context.set_result(result)
    logger.info(
        "一键导入流程完成 kb_id=%s filename=%s overall_status=%s",
        request.kb_id,
        request.filename,
        result["overall_status"],
    )
    return result


@import_graph.post("/databases/{kb_id}/import-and-graph")
async def import_and_graph(
    kb_id: str,
    file: UploadFile = File(...),
    options: str | None = Form(default=None, description="结构化导入配置 JSON"),
    graph_batch_size: int = Query(
        default=DEFAULT_GRAPH_BATCH_SIZE,
        ge=1,
        le=200,
        description="图谱构建批次大小",
    ),
    ocr_engine: str = Query(
        default=DEFAULT_OCR_ENGINE,
        description="OCR引擎: disable/rapid_ocr/mineru_ocr/mineru_official/pp_structure_v3_ocr/deepseek_ocr",
    ),
    current_user: User = Depends(get_admin_user),
):
    """一键导入文件到知识库，并在已配置图谱时触发待处理 Chunk 的图谱构建。"""
    if not file.filename:
        raise HTTPException(status_code=400, detail="文件名为空")

    await _ensure_database_supports_documents(kb_id, "一键导入")

    ext = os.path.splitext(file.filename)[1].lower()
    if not is_supported_file_extension(file.filename):
        raise HTTPException(status_code=400, detail=f"不支持的文件类型: {ext}")

    basename, ext = os.path.splitext(file.filename)
    filename = f"{basename}{ext}".lower()

    try:
        import_options = load_import_graph_options(
            options,
            legacy_ocr_engine=ocr_engine,
            legacy_graph_batch_size=graph_batch_size,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"导入配置无效: {exc}") from exc

    try:
        file_bytes = await read_upload_with_limit(
            file,
            max_size_bytes=MAX_UPLOAD_SIZE_BYTES,
            too_large_message="文件过大，当前仅支持 100 MB 以内的文件",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    content_hash = await calculate_content_hash(file_bytes)
    file_exists = await knowledge_base.file_existed_in_db(kb_id, content_hash)
    if file_exists:
        if import_options.document_options.duplicate_strategy == "skip":
            return build_duplicate_skip_response(
                content_hash=content_hash,
                file_size=len(file_bytes),
                filename=filename,
                options=import_options,
            )
        raise HTTPException(status_code=409, detail="知识库中已存在相同内容的文件")

    timestamp = int(time.time() * 1000)
    minio_filename = f"{basename}_{timestamp}{ext}"
    bucket_name = MinIOClient.KB_BUCKETS["documents"]
    object_name = f"{kb_id}/upload/{minio_filename}"
    minio_url = await aupload_file_to_minio(bucket_name, object_name, file_bytes)
    cleanup_attempted = False
    pipeline_request = ImportGraphPipelineRequest(
        kb_id=kb_id,
        filename=filename,
        file_size=len(file_bytes),
        content_hash=content_hash,
        minio_url=minio_url,
        operator_id=current_user.uid,
        processing_params=build_processing_params(import_options),
        options=import_options,
    )

    async def cleanup_uploaded_object() -> None:
        nonlocal cleanup_attempted
        if cleanup_attempted:
            return
        cleanup_attempted = True
        try:
            await get_minio_client().adelete_file(bucket_name, object_name)
            logger.info("一键导入-已清理未入库文件 kb_id=%s object_name=%s", kb_id, object_name)
        except Exception as cleanup_error:
            logger.warning("一键导入-清理未入库文件失败 kb_id=%s object_name=%s error=%s", kb_id, object_name, cleanup_error)

    async def run_full_pipeline(context):
        try:
            return await run_import_graph_pipeline(context, pipeline_request)
        except ImportGraphAddRecordError:
            await cleanup_uploaded_object()
            raise
        except Exception as exc:
            logger.error("一键导入流程失败 kb_id=%s filename=%s error=%s", kb_id, filename, exc)
            raise

    try:
        task, created = await tasker.enqueue_unique_by_payload(
            name=f"一键导入: {filename}",
            task_type="document_action",
            payload={"kb_id": kb_id, "filename": filename, "content_hash": content_hash},
            coroutine=run_full_pipeline,
            payload_match={"kb_id": kb_id, "content_hash": content_hash},
            statuses=ACTIVE_DOCUMENT_ACTION_TASK_STATUSES,
        )
    except Exception:
        await cleanup_uploaded_object()
        raise

    if not created:
        await cleanup_uploaded_object()
        raise HTTPException(status_code=409, detail="该文件正在处理中，请勿重复提交")

    logger.info(
        "一键导入任务已提交 kb_id=%s filename=%s content_hash=%s task_id=%s",
        kb_id,
        filename,
        content_hash,
        task.id,
    )
    return {
        "message": "一键导入任务已提交",
        "status": "queued",
        "task_id": task.id,
        "options": import_options.model_dump(mode="json"),
        "file_info": {
            "filename": filename,
            "size": len(file_bytes),
            "content_hash": content_hash,
            "minio_url": minio_url,
        },
    }
