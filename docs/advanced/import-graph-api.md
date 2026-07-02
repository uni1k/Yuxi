# 一键导入并构建知识图谱 API

`POST /api/knowledge/databases/{kb_id}/import-and-graph`

该接口为**私有化部署扩展接口**，用于把本地文件一次性导入知识库，并按配置自动完成解析、向量化入库和知识图谱构建。接口采用异步任务执行，提交成功后返回任务 ID，调用方可通过任务系统轮询处理结果。

## 认证方式

接口要求调用方具备**管理员身份**，与普通管理后台接口使用同一套认证：

- 在浏览器端通过登录态 Cookie 访问
- 在脚本中通过 `Authorization: Bearer <admin_token>` 访问（Token 获取方式与平台其他管理接口一致）

## 请求方式

- **Method**: `POST`
- **Content-Type**: `multipart/form-data`
- **Path Parameter**:
  - `kb_id`（必填）：目标知识库 ID

## 请求参数

### 表单字段

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `file` | File | 是 | 待上传文件，支持平台文档解析器所识别的格式 |
| `options` | string (JSON) | 否 | 结构化导入配置，JSON 字符串，详见下方 `options` 说明 |
| `graph_batch_size` | integer | 否 | 图谱构建批次大小，默认 `20`，范围 `[1, 200]`。未传 `options` 时生效，用于兼容旧调用方式 |
| `ocr_engine` | string | 否 | OCR 引擎，默认 `mineru_ocr`。未传 `options` 时生效，用于兼容旧调用方式 |

### `options` 配置详解

`options` 是一个 JSON 字符串，完整结构如下：

```json
{
  "pipeline": {
    "parse": true,
    "index": true,
    "build_graph": true
  },
  "parse_options": {
    "ocr_engine": "mineru_ocr",
    "ocr_engine_config": {}
  },
  "index_options": {
    "chunk_preset_id": null,
    "max_tokens": null,
    "overlap_percent": null,
    "overlap_ratio": null
  },
  "graph_options": {
    "batch_size": 20
  },
  "document_options": {
    "source_path": null,
    "parent_id": null,
    "duplicate_strategy": "reject"
  }
}
```

#### `pipeline` 流程开关

控制本次导入执行哪些阶段。必须满足依赖关系：

- `index=true` 时，`parse` 必须为 `true`
- `build_graph=true` 时，`index` 必须为 `true`

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `parse` | boolean | `true` | 是否自动解析文件为 Markdown |
| `index` | boolean | `true` | 是否自动分块并向量化入库 |
| `build_graph` | boolean | `true` | 是否自动构建知识图谱（需知识库已配置图谱抽取器） |

#### `parse_options` 解析选项

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `ocr_engine` | string | `mineru_ocr` | OCR 引擎，可选值与平台文档解析配置一致，如 `disable`、`rapid_ocr`、`mineru_ocr`、`mineru_official`、`pp_structure_v3_ocr`、`deepseek_ocr` 等 |
| `ocr_engine_config` | object | `{}` | 传递给 OCR 引擎的额外配置，结构取决于具体引擎 |

#### `index_options` 入库分块选项

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `chunk_preset_id` | string \| null | `null` | 分块策略 ID，未指定时使用知识库默认配置 |
| `max_tokens` | integer \| null | `null` | 分块最大 token 数，必须 `≥ 1` |
| `overlap_percent` | integer \| null | `null` | 分块重叠百分比，范围 `[0, 99]` |
| `overlap_ratio` | number \| null | `null` | 分块重叠比例，范围 `[0, 1]`，最终会被转换为百分比。若同时指定 `overlap_percent` 和 `overlap_ratio`，以 `overlap_percent` 为准 |

#### `graph_options` 图谱构建选项

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `batch_size` | integer | `20` | 每次处理的 chunk 数量，范围 `[1, 200]` |

#### `document_options` 文档元数据选项

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `source_path` | string \| null | `null` | 文件在知识库中的展示路径 |
| `parent_id` | string \| null | `null` | 父目录 ID，用于把文件放入指定文件夹 |
| `duplicate_strategy` | string | `reject` | 重复文件处理策略。`reject`：返回 409 拒绝；`skip`：返回已跳过，不重复上传 |

## 响应说明

### 1. 提交成功

当文件不存在冲突且成功提交异步任务时返回：

```json
{
  "message": "一键导入任务已提交",
  "status": "queued",
  "task_id": "task_xxxxxxxx",
  "options": {
    "pipeline": { "parse": true, "index": true, "build_graph": true },
    "parse_options": { "ocr_engine": "mineru_ocr", "ocr_engine_config": {} },
    "index_options": {
      "chunk_preset_id": null,
      "max_tokens": null,
      "overlap_percent": null,
      "overlap_ratio": null
    },
    "graph_options": { "batch_size": 20 },
    "document_options": {
      "source_path": null,
      "parent_id": null,
      "duplicate_strategy": "reject"
    }
  },
  "file_info": {
    "filename": "example.pdf",
    "size": 102400,
    "content_hash": "a1b2c3d4...",
    "minio_url": "minio://knowledgebases/..."
  }
}
```

### 2. 重复文件并选择跳过

当 `duplicate_strategy=skip` 且文件已存在时返回 `200`：

```json
{
  "message": "知识库中已存在相同内容的文件，已按调用方策略跳过",
  "status": "skipped",
  "duplicate": true,
  "options": { ... },
  "file_info": {
    "filename": "example.pdf",
    "size": 102400,
    "content_hash": "a1b2c3d4..."
  }
}
```

### 3. 重复文件并拒绝

当 `duplicate_strategy=reject`（默认）且文件已存在时返回 `409`：

```json
{
  "detail": "知识库中已存在相同内容的文件"
}
```

### 4. 文件正在处理中

当相同内容文件已有正在执行的一键导入任务时返回 `409`：

```json
{
  "detail": "该文件正在处理中，请勿重复提交"
}
```

### 5. 配置非法

当 `options` 格式错误或流程依赖不合法时返回 `400`：

```json
{
  "detail": "导入配置无效: ..."
}
```

## 异步任务结果

提交成功后，调用方可通过任务 ID 查询异步执行结果。任务成功完成时的结果结构如下：

```json
{
  "overall_status": "success",
  "file_info": {
    "filename": "example.pdf",
    "size": 102400,
    "content_hash": "a1b2c3d4...",
    "minio_url": "minio://knowledgebases/..."
  },
  "options": { ... },
  "document_import": {
    "status": "success",
    "file_id": "file_xxxxxxxx",
    "file_status": "indexed"
  },
  "stages": {
    "add_record": { "status": "success", "file_id": "file_xxxxxxxx" },
    "parse": { "status": "parsed" },
    "index": { "status": "indexed" }
  },
  "graph_build": {
    "status": "success",
    "scope": "knowledge_base_pending_chunks",
    "success": 15,
    "failed": 0
  }
}
```

`overall_status` 可能的取值：

| 取值 | 含义 |
|---|---|
| `success` | 文档导入完成，图谱构建成功（或图谱被跳过/禁用） |
| `partial_success` | 文档导入完成，但图谱构建失败或未执行 |
| `failed` | 文档导入失败 |

图谱构建失败不会导致整个任务抛异常，而是标记为 `partial_success`，文档主流程的结果仍然保留。

## 调用示例

### 仅上传并解析，不入库不建图

```bash
curl -X POST "https://your-yuxi-server/api/knowledge/databases/{kb_id}/import-and-graph" \
  -H "Authorization: Bearer <admin_token>" \
  -F "file=@example.pdf" \
  -F 'options={"pipeline":{"parse":true,"index":false,"build_graph":false}}'
```

### 自定义分块参数并跳过重复文件

```bash
curl -X POST "https://your-yuxi-server/api/knowledge/databases/{kb_id}/import-and-graph" \
  -H "Authorization: Bearer <admin_token>" \
  -F "file=@example.pdf" \
  -F 'options={
    "pipeline":{"parse":true,"index":true,"build_graph":true},
    "index_options":{"chunk_preset_id":"separator","max_tokens":512,"overlap_ratio":0.1},
    "document_options":{"duplicate_strategy":"skip"}
  }'
```

### Python 示例

```python
import json
import requests

base_url = "https://your-yuxi-server"
kb_id = "your_kb_id"
token = "<admin_token>"

options = {
    "pipeline": {"parse": True, "index": True, "build_graph": True},
    "parse_options": {"ocr_engine": "mineru_ocr"},
    "index_options": {"max_tokens": 512, "overlap_ratio": 0.1},
    "document_options": {"duplicate_strategy": "skip"},
}

with open("example.pdf", "rb") as f:
    resp = requests.post(
        f"{base_url}/api/knowledge/databases/{kb_id}/import-and-graph",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("example.pdf", f)},
        data={"options": json.dumps(options, ensure_ascii=False)},
    )

print(resp.status_code)
print(resp.json())
```

## 注意事项

1. 接口为管理员接口，普通用户 Token 无法调用
2. 文件大小限制为 100 MB，超限会返回 `400`
3. `options` 为 JSON 字符串，不是嵌套表单对象
4. 图谱构建依赖知识库已配置并锁定图谱抽取器，未配置时 `graph_build` 会标记为 `skipped`，整体状态为 `partial_success`
5. 旧调用方式（仅传 `file`、`graph_batch_size`、`ocr_engine`）仍然兼容，行为等同于默认 `options`
