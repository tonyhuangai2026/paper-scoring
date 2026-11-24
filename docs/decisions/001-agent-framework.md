# 子 Agent 框架选择

## 需要解决的问题

子 Agent 需要通过兼容网关调用同一模型，提交结构化判读，并在必要时附上 PDF 页图。每次判读的上下文应由本轮规则和单篇材料构成。主程序需要保持最多三个本机进程并行、逐篇保存原判、续跑和确定性汇总。

## 选择 Pydantic AI

每个子进程创建一个具名 Pydantic AI `Agent`。使用 `OpenAIChatModel`、`OpenAICompatibleProvider`、JSON 对象输出和 `BinaryContent` 页图。每次 `Agent.run()` 从新上下文开始，重试仍由调用方控制。程序验证研究类型的分支、原文证据和标签取值。清单、并发、文件写入及验收由现有 Python 主程序负责。

换框架后，已有旧框架实验用新的 `--run-dir` 开始，避免把两种模型调用方案的原判混入同一份一致性统计。验收报告记录使用的框架及版本。本程序可验证发送给网关的请求范围，网关内部的路由与缓存由服务方管理。

## 对照过的方案

- Agno 的 `OpenAILike` 和 `Agent` 也支持兼容网关、图像及结构化输出。当前项目的重点是本地校验和 JSON 兼容模式，Pydantic AI 的 `PromptedOutput` 与现有规则契约衔接直接。
- LangGraph 适合显式建模状态机和持久化工作流。当前项目已有论文级检查点，并发与恢复策略已通过测试，迁移到图编排会另外改变这些部分。

## 官方资料

- [Pydantic AI 兼容 API](https://pydantic.dev/docs/ai/models/compatible-apis/)
- [Pydantic AI 输出模式](https://pydantic.dev/docs/ai/core-concepts/output/)
- [Agno OpenAILike](https://docs.agno.com/reference/models/openai-like)
- [LangGraph 概览](https://docs.langchain.com/oss/python/langgraph/overview)
