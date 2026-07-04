from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.utils.auth_middleware import get_required_user

external_kb_router_module = importlib.import_module("server.routers.external_kb_router")


def _build_app(*, authenticated: bool = True, role: str = "admin") -> FastAPI:
    app = FastAPI()
    app.include_router(external_kb_router_module.external_kb_router, prefix="/api")

    if authenticated:

        async def fake_user():
            return SimpleNamespace(uid="user-1", role=role, department_id=1, to_dict=lambda: {
                "uid": "user-1",
                "role": role,
                "department_id": 1,
            })

        app.dependency_overrides[get_required_user] = fake_user

    return app


def _build_client(**kwargs) -> TestClient:
    return TestClient(_build_app(**kwargs))


@pytest.fixture
def mock_kb(monkeypatch: pytest.MonkeyPatch):
    """Patch knowledge_base methods used by the external KB router."""
    calls: dict[str, object] = {"check_accessible": [], "aquery": []}

    async def fake_check_accessible(user_info: dict, kb_id: str) -> bool:
        calls["check_accessible"].append({"user_info": user_info, "kb_id": kb_id})
        return kb_id != "forbidden-kb"

    async def fake_aquery(query_text: str, kb_id: str, **kwargs):
        calls["aquery"].append({"query_text": query_text, "kb_id": kb_id, "kwargs": kwargs})
        if kb_id == "empty-kb":
            return []
        return [
            {
                "content": "chunk one",
                "score": 0.95,
                "metadata": {"source": "doc1.pdf", "file_id": "f1", "chunk_id": "c1"},
            },
            {
                "content": "",
                "score": 0.88,
                "metadata": {"source": "doc2.pdf", "file_id": "f2", "chunk_id": "c2"},
            },
            {
                "content": "chunk three",
                "score": 0.82,
                "metadata": None,
            },
        ]

    monkeypatch.setattr(external_kb_router_module.knowledge_base, "check_accessible", fake_check_accessible)
    monkeypatch.setattr(external_kb_router_module.knowledge_base, "aquery", fake_aquery)

    return calls


def test_retrieval_requires_authentication():
    client = _build_client(authenticated=False)

    response = client.post(
        "/api/external/knowledge/retrieval",
        json={"knowledge_id": "kb-1", "query": "hello"},
    )

    assert response.status_code == 401


def test_retrieval_forbidden_when_kb_not_accessible(mock_kb: dict):
    client = _build_client()

    response = client.post(
        "/api/external/knowledge/retrieval",
        json={"knowledge_id": "forbidden-kb", "query": "hello"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "没有权限访问该知识库"
    assert mock_kb["check_accessible"] == [{"user_info": {"uid": "user-1", "role": "admin", "department_id": 1}, "kb_id": "forbidden-kb"}]


def test_retrieval_maps_dify_request_and_response(mock_kb: dict):
    client = _build_client()

    response = client.post(
        "/api/external/knowledge/retrieval",
        json={
            "knowledge_id": "kb-1",
            "query": "test query",
            "retrieval_setting": {"top_k": 5, "score_threshold": 0.7},
        },
    )

    assert response.status_code == 200, response.text
    data = response.json()
    assert "records" in data
    # Empty content and null metadata records are filtered/adjusted
    assert len(data["records"]) == 2

    first = data["records"][0]
    assert first["content"] == "chunk one"
    assert first["score"] == 0.95
    assert first["title"] == "doc1.pdf"
    assert first["metadata"] == {"source": "doc1.pdf", "file_id": "f1", "chunk_id": "c1"}

    second = data["records"][1]
    assert second["content"] == "chunk three"
    assert second["score"] == 0.82
    assert second["title"] == "未知来源"
    assert second["metadata"] == {}

    assert mock_kb["aquery"] == [
        {
            "query_text": "test query",
            "kb_id": "kb-1",
            "kwargs": {"final_top_k": 5, "similarity_threshold": 0.7},
        }
    ]


def test_retrieval_uses_defaults_when_retrieval_setting_missing(mock_kb: dict):
    client = _build_client()

    response = client.post(
        "/api/external/knowledge/retrieval",
        json={"knowledge_id": "kb-1", "query": "test"},
    )

    assert response.status_code == 200, response.text
    assert mock_kb["aquery"] == [
        {
            "query_text": "test",
            "kb_id": "kb-1",
            "kwargs": {"final_top_k": 5, "similarity_threshold": 0.0},
        }
    ]


def test_retrieval_returns_empty_records_when_no_results(mock_kb: dict):
    client = _build_client()

    response = client.post(
        "/api/external/knowledge/retrieval",
        json={"knowledge_id": "empty-kb", "query": "test"},
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"records": []}


def test_retrieval_validates_top_k_range():
    client = _build_client()

    response = client.post(
        "/api/external/knowledge/retrieval",
        json={
            "knowledge_id": "kb-1",
            "query": "test",
            "retrieval_setting": {"top_k": 0},
        },
    )

    assert response.status_code == 422


def test_retrieval_validates_score_threshold_range():
    client = _build_client()

    response = client.post(
        "/api/external/knowledge/retrieval",
        json={
            "knowledge_id": "kb-1",
            "query": "test",
            "retrieval_setting": {"score_threshold": 1.5},
        },
    )

    assert response.status_code == 422


def test_retrieval_handles_aquery_exception(monkeypatch: pytest.MonkeyPatch):
    async def fake_aquery(*_args, **_kwargs):
        raise RuntimeError("milvus down")

    monkeypatch.setattr(external_kb_router_module.knowledge_base, "check_accessible", lambda _u, _k: True)
    monkeypatch.setattr(external_kb_router_module.knowledge_base, "aquery", fake_aquery)

    client = _build_client()
    response = client.post(
        "/api/external/knowledge/retrieval",
        json={"knowledge_id": "kb-1", "query": "test"},
    )

    assert response.status_code == 500
    assert "知识库检索失败" in response.json()["detail"]
