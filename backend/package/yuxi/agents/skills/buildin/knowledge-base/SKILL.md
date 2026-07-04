---
name: knowledge-base
slug: knowledge-base
description: "使用本地知识库进行检索、打开文档、文档内定位和查看思维导图。当用户需要基于已配置知识库回答问题、核验资料或引用文档内容时使用此技能。"
---

# 知识库技能

当用户要求基于项目知识库、内部资料、上传入库文档或知识图谱相关内容回答问题时，使用此技能。

## 可用工具

- `list_kbs`：列出当前会话可访问且已启用的知识库。
- `query_kb`：按 `kb_id` 在指定知识库中检索内容，返回 `file_id` 和相关片段。
- `open_kb_document`：按 `kb_id` 和 `file_id` 打开文档原文窗口，适合查看更完整上下文。
- `find_kb_document`：在已知文档内用关键词或正则定位段落。
- `get_mindmap`：查看知识库思维导图结构。
- `search_file`：按文件名关键词搜索知识库中的文件，支持指定知识库或跨知识库，返回文件列表与分页信息。

## 操作流程

1. 需要先确认当前会话有哪些知识库可用；不确定时调用 `list_kbs`。
2. 针对用户问题选择最相关的知识库，使用 `query_kb` 检索。
3. 如果检索片段不足以回答，使用返回的 `file_id` 调用 `open_kb_document` 查看上下文。
4. 如果用户问题涉及具体地址、编号、术语、指标或原文证据，直接使用 `find_kb_document` 在候选文档内精确定位，不要先用 shell 命令尝试。
5. 当用户关心知识库结构、文件分类或知识框架时，使用 `get_mindmap`。

## 关键约束

- 只能访问当前会话配置和用户权限允许的知识库。
- 不要编造 `kb_id` 或 `file_id`；优先从 `list_kbs` 和 `query_kb` 的返回结果中获取。
- 回答需要可追溯时，应说明依据来自哪个知识库、文件或检索片段。
- Dify 等外部只读知识库可能只支持检索，不一定支持打开全文或文档内查找；遇到工具返回限制说明时，应如实告知用户。

## 防循环约束

- **禁止用 shell 命令处理知识库工具结果**：当 `query_kb`、`open_kb_document` 或 `find_kb_document` 返回结果较大并被保存到文件（如 `/home/gem/user-data/outputs/large_tool_results/call_*`）时，禁止通过 `grep`、`sed`、`awk`、`cat` 等 shell 命令去提取内容。必须使用本技能提供的 `open_kb_document` 或 `find_kb_document` 工具查看原文。
- **精确查找优先用 `find_kb_document`**：用户问题涉及具体地址、房间号、手机号、身份证号、编号、人名等可关键词定位的信息时，优先使用 `find_kb_document`，而不是反复用 `query_kb` 换不同问法。
- **最多尝试 3 次**：`query_kb` + `open_kb_document` + `find_kb_document` 的调用总次数不要超过 3 次。如果 3 次后仍无法确定答案，停止检索，向用户说明已查到的信息和无法确定的部分，不要继续调用任何工具或 shell 命令。
- **不要对同一关键词换表达式重试**：如果一次 `query_kb` 或 `find_kb_document` 已经覆盖了用户问题的核心关键词，不要仅因为“想再确认”就发起第二次相似查询。
