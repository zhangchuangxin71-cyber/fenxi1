from __future__ import annotations


def is_pure_continue(text: str) -> bool:
    normalized = "".join(text.lower().split()).strip("，。！!?.,;；:：")
    return normalized in {
        "继续",
        "重试",
        "刚才到哪了",
        "刚才到哪里了",
        "重新展示刚才结果",
        "重新展示结果",
        "给我看刚才的结果",
    }
