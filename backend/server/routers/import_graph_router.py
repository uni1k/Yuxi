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

import os
import time

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
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
GRAPH_BUILD_SCOPE = "knowledge_base_pending_chunks"


@import_graph.post("/databases/{kb_id}/import-and-graph")
async def import_and_graph(
    kb_id: str,
    file: UploadFile = File(...),
    graph_batch_size: int = Query(default=20, ge=1, le=200, description="图谱构建批次大小"),
    ocr_engine: str = Query(
        default="mineru_ocr",
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
        raise HTTPException(status_code=409, detail="知识库中已存在相同内容的文件")

    timestamp = int(time.time() * 1000)
    minio_filename = f"{basename}_{timestamp}{ext}"
    bucket_name = MinIOClient.KB_BUCKETS["documents"]
    object_name = f"{kb_id}/upload/{minio_filename}"
    minio_url = await aupload_file_to_minio(bucket_name, object_name, file_bytes)
    cleanup_attempted = False

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

    async def run_full_pipeline(context: TaskContext):
        result = {
            "overall_status": "running",
            "file_info": {
                "filename": filename,
                "size": len(file_bytes),
                "content_hash": content_hash,
                "minio_url": minio_url,
            },
            "document_import": {"status": "running"},
            "stages": {},
            "graph_build": {
                "status": "pending",
                "scope": GRAPH_BUILD_SCOPE,
            },
        }

        await context.set_message("阶段 1/4：添加文件记录")
        await context.set_progress(5.0, "添加文件记录到知识库")
        try:
            add_params = {
                "content_hashes": {minio_url: content_hash},
                "file_sizes": {minio_url: len(file_bytes)},
                "content_type": "file",
                "ocr_engine": ocr_engine,
            }
            file_meta = await knowledge_base.add_file_record(
                kb_id, minio_url, params=add_params, operator_id=current_user.uid
            )
            file_id = file_meta["file_id"]
            result["stages"]["add_record"] = {"status": "success", "file_id": file_id}
        except Exception as exc:
            await cleanup_uploaded_object()
            logger.error("一键导入-添加记录失败 kb_id=%s filename=%s error=%s", kb_id, filename, exc)
            result["document_import"] = {"status": "failed", "error": str(exc)}
            result["stages"]["add_record"] = {"status": "failed", "error": str(exc)}
            raise

        await context.set_message("阶段 2/4：解析文件")
        await context.set_progress(20.0, "解析文档内容")
        try:
            file_meta = await knowledge_base.parse_file(kb_id, file_id, operator_id=current_user.uid)
            result["stages"]["parse"] = {"status": file_meta.get("status", "parsed")}
        except Exception as exc:
            logger.error("一键导入-解析文件失败 kb_id=%s file_id=%s error=%s", kb_id, file_id, exc)
            result["document_import"] = {"status": "failed", "error": str(exc)}
            result["stages"]["parse"] = {"status": "failed", "error": str(exc)}
            raise

        await context.set_message("阶段 3/4：向量化入库")
        await context.set_progress(45.0, "文档分块并生成向量")
        try:
            await knowledge_base.update_file_params(kb_id, file_id, {}, operator_id=current_user.uid)
            file_meta = await knowledge_base.index_file(kb_id, file_id, operator_id=current_user.uid)
            result["stages"]["index"] = {"status": file_meta.get("status", "indexed")}
            result["document_import"] = {
                "status": "success",
                "file_id": file_id,
                "index_status": file_meta.get("status", "indexed"),
            }
        except Exception as exc:
            logger.error("一键导入-向量化入库失败 kb_id=%s file_id=%s error=%s", kb_id, file_id, exc)
            result["document_import"] = {"status": "failed", "error": str(exc), "file_id": file_id}
            result["stages"]["index"] = {"status": "failed", "error": str(exc)}
            raise

        await context.set_message("阶段 4/4：检查图谱配置")
        await context.set_progress(70.0, "检查是否配置了图谱抽取器")

        graph_service = MilvusGraphService()
        try:
            graph_status = await graph_service.get_status(kb_id)
        except Exception:
            graph_status = {}

        if graph_status.get("locked"):
            await context.set_progress(75.0, "开始构建知识图谱")
            try:
                if await _has_running_graph_build_task(kb_id):
                    result["graph_build"] = {
                        "status": "skipped",
                        "scope": GRAPH_BUILD_SCOPE,
                        "reason": "已有正在运行的图谱构建任务",
                    }
                else:
                    build_result = await graph_service.build_pending_chunks(
                        kb_id, batch_size=graph_batch_size, context=context
                    )
                    result["graph_build"] = {
                        "status": "success",
                        "scope": GRAPH_BUILD_SCOPE,
                        "success": build_result.get("success", 0),
                        "failed": build_result.get("failed", 0),
                    }
            except Exception as exc:
                logger.warning("一键导入-图谱构建异常 kb_id=%s file_id=%s error=%s", kb_id, file_id, exc)
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

        if result["document_import"]["status"] != "success":
            result["overall_status"] = "failed"
            final_message = "文档导入失败"
        elif result["graph_build"]["status"] == "success":
            result["overall_status"] = "success"
            final_message = "文档导入完成，图谱构建成功"
        elif result["graph_build"]["status"] == "failed":
            result["overall_status"] = "partial_success"
            final_message = "文档导入完成，但图谱构建失败"
        else:
            result["overall_status"] = "partial_success"
            final_message = "文档导入完成，未触发图谱构建"

        await context.set_progress(100.0, final_message)
        await context.set_result(result)
        return result

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
        "file_info": {
            "filename": filename,
            "size": len(file_bytes),
            "content_hash": content_hash,
            "minio_url": minio_url,
        },
    }
