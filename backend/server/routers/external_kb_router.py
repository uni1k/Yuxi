"""Dify External Knowledge API compatible router.

Exposes a single ``POST /retrieval`` endpoint so Dify can retrieve chunks
from Yuxi knowledge bases. The endpoint follows the contract documented at:
https://docs.dify.ai/en/use-dify/knowledge/external-knowledge-api
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from yuxi import knowledge_base
from yuxi.storage.postgres.models_business import User
from yuxi.utils import logger

from server.utils.auth_middleware import get_required_user

external_kb_router = APIRouter(prefix="/external/knowledge", tags=["external-knowledge"])


class RetrievalSetting(BaseModel):
    """Dify retrieval settings."""

    top_k: int = Field(default=5, ge=1, le=100, description="Maximum number of chunks to retrieve")
    score_threshold: float = Field(default=0.0, ge=0.0, le=1.0, description="Minimum similarity score")


class ExternalRetrievalRequest(BaseModel):
    """Request body sent by Dify to an external knowledge service."""

    knowledge_id: str = Field(..., description="Identifier of the knowledge base in Yuxi")
    query: str = Field(..., min_length=1, description="User search query")
    retrieval_setting: RetrievalSetting = Field(default_factory=RetrievalSetting)
    metadata_condition: dict | None = Field(default=None, description="Optional metadata filter (currently ignored)")


class ExternalRecord(BaseModel):
    """Single retrieval result in Dify-compatible format."""

    content: str
    score: float
    title: str
    metadata: dict


class ExternalRetrievalResponse(BaseModel):
    """Response body expected by Dify."""

    records: list[ExternalRecord]


@external_kb_router.post(
    "/retrieval",
    response_model=ExternalRetrievalResponse,
    summary="Dify 外部知识库检索",
    description="兼容 Dify External Knowledge API 的检索端点，Dify 会通过此端点获取 Yuxi 知识库中的文本块。",
)
async def retrieve_external_knowledge(
    request: ExternalRetrievalRequest,
    current_user: User = Depends(get_required_user),
) -> ExternalRetrievalResponse:
    """Retrieve chunks from a Yuxi knowledge base for Dify."""
    kb_id = request.knowledge_id
    user_info = current_user.to_dict()

    logger.info(f"External KB retrieval: kb_id={kb_id}, user={user_info.get('uid')}")

    # Check knowledge base accessibility
    is_accessible = await knowledge_base.check_accessible(user_info, kb_id)
    if not is_accessible:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="没有权限访问该知识库",
        )

    # aquery will merge saved query params (including search_mode) with the
    # kwargs we provide here, so we only override top_k and score_threshold.
    query_meta = {
        "final_top_k": request.retrieval_setting.top_k,
        "similarity_threshold": request.retrieval_setting.score_threshold,
    }

    try:
        results = await knowledge_base.aquery(
            request.query,
            kb_id=kb_id,
            **query_meta,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(f"External KB retrieval failed for kb_id={kb_id}: {exc}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"知识库检索失败: {exc}",
        ) from exc

    records: list[ExternalRecord] = []
    for item in results:
        if not isinstance(item, dict):
            continue

        content = item.get("content")
        if not content:
            continue

        metadata = item.get("metadata") or {}
        if not isinstance(metadata, dict):
            metadata = {}

        # Ensure metadata is never null (Dify rejects null metadata)
        safe_metadata = {str(k): v for k, v in metadata.items() if v is not None}

        records.append(
            ExternalRecord(
                content=str(content),
                score=float(item.get("score") or 0.0),
                title=str(metadata.get("source") or metadata.get("filename") or "未知来源"),
                metadata=safe_metadata,
            )
        )

    return ExternalRetrievalResponse(records=records)
