# 模块说明：统一 QA 会话入口与模式分发。
# 设计目的：
# 1) 把“单问模式”和“交互模式”的入口收敛到一个函数；
# 2) 屏蔽上层调用方对交互循环细节的感知；
# 3) 保持 execute_qa 的参数传递一致，便于后续扩展 QA 模式。

from qa.interactive import interactive_chat


async def run_qa_mode(
    args,
    *,
    client,
    execute_qa_fn,
    allowed_doc_ids: set[str] | None,
    bm25_prefilter,
):
    """根据命令行参数进入单问或交互问答模式。

    参数说明：
    - args: CLI 解析后的参数对象，包含 question / qa_mode / verbose 等开关。
    - client: PageIndexClient 实例，用于访问文档索引与检索能力。
    - execute_qa_fn: 统一问答执行函数（通常为 document_assistant_agent.execute_qa）。
    - allowed_doc_ids: 可选文档白名单；为 None 时表示不限制文档范围。
    - bm25_prefilter: BM25 预筛器实例，可为空。

    返回说明：
    - 单问模式（args.question 有值）返回一次问答结果 dict。
    - 交互模式返回 None（结果在会话循环中实时输出）。
    """
    if args.question:
        return await execute_qa_fn(
            client,
            args.question,
            qa_mode=args.qa_mode,
            verbose=args.verbose,
            stream_output=True,
            top_k_docs=args.top_k_docs,
            allowed_doc_ids=allowed_doc_ids,
            bm25_prefilter=bm25_prefilter,
            bm25_prefilter_top_k=args.bm25_prefilter_top_k,
            session_id=getattr(args, "session_id", None),
        )

    await interactive_chat(
        client,
        execute_qa_fn=execute_qa_fn,
        verbose=args.verbose,
        qa_mode=args.qa_mode,
        top_k_docs=args.top_k_docs,
        allowed_doc_ids=allowed_doc_ids,
        bm25_prefilter=bm25_prefilter,
        bm25_prefilter_top_k=args.bm25_prefilter_top_k,
        session_id=getattr(args, "session_id", None),
    )
    return None

