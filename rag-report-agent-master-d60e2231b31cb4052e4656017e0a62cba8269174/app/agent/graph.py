from langgraph.graph import END, START, StateGraph

from app.agent.nodes import (
    chitchat_node,
    doc_router_node,
    intent_node,
    outline_generate_node,
    outline_modify_node,
    qa_generate_node,
    query_rewrite_node,
    report_edit_node,
    report_write_node,
    retrieve_node,
    task_dispatch_node,
)
from app.agent.router import route_after_intent, route_after_retrieve, route_from_start
from app.agent.state import AgentState


def build_graph():
    graph = StateGraph(AgentState)
    graph.add_node("intent", intent_node)
    graph.add_node("doc_router", doc_router_node)
    graph.add_node("query_rewrite", query_rewrite_node)
    graph.add_node("retrieve", retrieve_node)
    graph.add_node("qa_generate", qa_generate_node)
    graph.add_node("chitchat", chitchat_node)
    graph.add_node("outline_generate", outline_generate_node)
    graph.add_node("outline_modify", outline_modify_node)
    graph.add_node("report_write", report_write_node)
    graph.add_node("report_edit", report_edit_node)
    graph.add_node("task_dispatch", task_dispatch_node)

    graph.add_conditional_edges(
        START,
        route_from_start,
        {
            "intent": "intent",
            "need_doc_retrieval": "query_rewrite",
        },
    )
    graph.add_conditional_edges(
        "intent",
        route_after_intent,
        {
            "chitchat": "chitchat",
            "outline_modify": "outline_modify",
            "need_rag": "query_rewrite",
            "task_dispatch": "task_dispatch",
        },
    )
    graph.add_edge("query_rewrite", "doc_router")
    graph.add_edge("doc_router", "retrieve")
    graph.add_conditional_edges(
        "retrieve",
        route_after_retrieve,
        {
            "intent": "intent",
        },
    )
    for node in [
        "qa_generate",
        "chitchat",
        "outline_generate",
        "outline_modify",
        "report_write",
        "report_edit",
        "task_dispatch",
    ]:
        graph.add_edge(node, END)
    return graph.compile()
