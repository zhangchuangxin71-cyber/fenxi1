# 模块说明：CLI 输出格式化工具。
# 主要用途：
# 1) 统一文档列表在终端日志中的展示样式；
# 2) 避免业务代码重复拼接输出字符串；
# 3) 让输出格式调整集中在一个模块内完成。


def format_doc_names_for_log(catalog: list[dict]) -> str:
    """将文档目录转换为便于阅读的日志文本。

    参数：
    - catalog: 文档目录列表，元素通常包含 ``doc_name`` 字段。

    返回：
    - 若无可展示文档名，返回 ``(none)``；
    - 否则按 ``- 文档名`` 逐行输出，适合直接打印到日志。
    """
    if not catalog:
        return "(none)"
    names = [str(item.get("doc_name", "") or "").strip() for item in catalog]
    names = [name for name in names if name]
    if not names:
        return "(none)"
    return "\n".join(f"- {name}" for name in names)

