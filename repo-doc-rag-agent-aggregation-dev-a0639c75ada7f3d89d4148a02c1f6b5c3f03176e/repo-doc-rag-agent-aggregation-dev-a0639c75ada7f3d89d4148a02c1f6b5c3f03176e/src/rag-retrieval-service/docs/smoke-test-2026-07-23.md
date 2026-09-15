# Retrieval Service Live Smoke Test

- Generated at: `2026-07-23T05:17:45.484270+00:00`
- Backend: real read-only PostgreSQL evaluation scope and real Ark API
- Visible document count: `499`
- Iterations per category: `2`
- Sensitive document IDs, names and extracted query terms are redacted.

## Summary

| Category | Success | LLM failures | Average latency (ms) | Average LLM calls | Average chunks |
|---|---:|---:|---:|---:|---:|
| `scope_direct` | 2/2 | 0 | 3521.0 | 2.0 | 1.0 |
| `routed_direct` | 2/2 | 0 | 3796.0 | 3.0 | 1.0 |
| `routed_focused` | 2/2 | 0 | 5452.5 | 4.0 | 3.0 |
| `routed_broad` | 2/2 | 0 | 3040.5 | 2.0 | 2.0 |

## Run Details

### scope_direct

Query template: `你能看到哪些文档`

#### Iteration 1

```json
{
  "iteration": 1,
  "llm_records": [
    {
      "duration_ms": 3798,
      "error_category": null,
      "output": {
        "structured_output": {
          "routed_broad": [],
          "routed_direct": [],
          "routed_focused": [],
          "scope_direct": [
            {
              "queries": [
                "你能看到哪些文档"
              ]
            }
          ]
        }
      },
      "phase": "classification",
      "status": "ok"
    },
    {
      "duration_ms": 1384,
      "error_category": null,
      "output": {
        "structured_output": {},
        "tool_calls": [
          {
            "arguments": {
              "return_metainfo_types": [
                "doc_name"
              ]
            },
            "name": "get_docs_metainfo"
          }
        ]
      },
      "phase": "scope_tool_planner",
      "status": "ok"
    }
  ],
  "response": {
    "chunk_count": 1,
    "chunks": [
      {
        "chunk_id": "scope:g0001:metainfo",
        "content_characters": 566,
        "content_truncated": false,
        "document_id": null,
        "page_number": null,
        "path": "request:scope",
        "source_type": "scope_metadata"
      }
    ],
    "classification": {
      "routed_broad": 0,
      "routed_direct": 0,
      "routed_focused": 0,
      "scope_direct": 1
    },
    "coverage": {
      "complete": false,
      "covered_group_refs": [
        "g0001"
      ],
      "degraded_group_refs": [
        "g0001"
      ],
      "truncated": true,
      "truncated_by": "workflow_degradation",
      "uncovered_group_refs": []
    },
    "usage": {
      "actual_mode": "semantic",
      "candidate_document_count": 4,
      "completion_tokens": 82,
      "inspected_node_count": 0,
      "inspected_page_count": 0,
      "latency_ms": 5229,
      "llm_request_count": 2,
      "prompt_tokens": 2060,
      "returned_count": 1,
      "returned_tokens": 497,
      "tokenizer": "cl100k_base",
      "total_tokens": 2142
    },
    "warning_codes": [
      "SCOPE_ENUMERATION_TRUNCATED"
    ]
  },
  "status": "ok"
}
```

#### Iteration 2

```json
{
  "iteration": 2,
  "llm_records": [
    {
      "duration_ms": 976,
      "error_category": null,
      "output": {
        "structured_output": {
          "routed_broad": [],
          "routed_direct": [],
          "routed_focused": [],
          "scope_direct": [
            {
              "queries": [
                "你能看到哪些文档"
              ]
            }
          ]
        }
      },
      "phase": "classification",
      "status": "ok"
    },
    {
      "duration_ms": 820,
      "error_category": null,
      "output": {
        "structured_output": {},
        "tool_calls": [
          {
            "arguments": {
              "return_metainfo_types": [
                "doc_name"
              ]
            },
            "name": "get_docs_metainfo"
          }
        ]
      },
      "phase": "scope_tool_planner",
      "status": "ok"
    }
  ],
  "response": {
    "chunk_count": 1,
    "chunks": [
      {
        "chunk_id": "scope:g0001:metainfo",
        "content_characters": 566,
        "content_truncated": false,
        "document_id": null,
        "page_number": null,
        "path": "request:scope",
        "source_type": "scope_metadata"
      }
    ],
    "classification": {
      "routed_broad": 0,
      "routed_direct": 0,
      "routed_focused": 0,
      "scope_direct": 1
    },
    "coverage": {
      "complete": false,
      "covered_group_refs": [
        "g0001"
      ],
      "degraded_group_refs": [
        "g0001"
      ],
      "truncated": true,
      "truncated_by": "workflow_degradation",
      "uncovered_group_refs": []
    },
    "usage": {
      "actual_mode": "semantic",
      "candidate_document_count": 4,
      "completion_tokens": 82,
      "inspected_node_count": 0,
      "inspected_page_count": 0,
      "latency_ms": 1813,
      "llm_request_count": 2,
      "prompt_tokens": 2060,
      "returned_count": 1,
      "returned_tokens": 497,
      "tokenizer": "cl100k_base",
      "total_tokens": 2142
    },
    "warning_codes": [
      "SCOPE_ENUMERATION_TRUNCATED"
    ]
  },
  "status": "ok"
}
```

### routed_direct

Query template: `这篇文档有几页`

#### Iteration 1

```json
{
  "iteration": 1,
  "llm_records": [
    {
      "duration_ms": 1060,
      "error_category": null,
      "output": {
        "structured_output": {
          "routed_broad": [],
          "routed_direct": [
            {
              "queries": [
                "查看这篇文档的页数"
              ],
              "target_docs_description": "当前请求范围包含的1篇文档",
              "target_docs_keywords": []
            }
          ],
          "routed_focused": [],
          "scope_direct": []
        }
      },
      "phase": "classification",
      "status": "ok"
    },
    {
      "duration_ms": 1087,
      "error_category": null,
      "output": {
        "structured_output": {
          "decisions": [
            {
              "doc_id": "<DOC_ID>",
              "grade": "possible",
              "group_ref": "g0001"
            }
          ]
        }
      },
      "phase": "document_routing:1/1",
      "status": "ok"
    },
    {
      "duration_ms": 1777,
      "error_category": null,
      "output": {
        "structured_output": {},
        "tool_calls": [
          {
            "arguments": {
              "doc_ids": [
                "<DOC_ID>"
              ],
              "return_metainfo_types": [
                "page_count"
              ]
            },
            "name": "get_docs_metainfo"
          }
        ]
      },
      "phase": "direct_tool_planner",
      "status": "ok"
    }
  ],
  "response": {
    "chunk_count": 1,
    "chunks": [
      {
        "chunk_id": "direct:g0001:metainfo",
        "content_characters": 69,
        "content_truncated": false,
        "document_id": null,
        "page_number": null,
        "path": "document:metainfo",
        "source_type": "scope_metadata"
      }
    ],
    "classification": {
      "routed_broad": 0,
      "routed_direct": 1,
      "routed_focused": 0,
      "scope_direct": 0
    },
    "coverage": {
      "complete": true,
      "covered_group_refs": [
        "g0001"
      ],
      "degraded_group_refs": [],
      "truncated": false,
      "truncated_by": null,
      "uncovered_group_refs": []
    },
    "usage": {
      "actual_mode": "semantic",
      "candidate_document_count": 1,
      "completion_tokens": 222,
      "inspected_node_count": 0,
      "inspected_page_count": 0,
      "latency_ms": 3943,
      "llm_request_count": 3,
      "prompt_tokens": 4102,
      "returned_count": 1,
      "returned_tokens": 70,
      "tokenizer": "cl100k_base",
      "total_tokens": 4324
    },
    "warning_codes": []
  },
  "status": "ok"
}
```

#### Iteration 2

```json
{
  "iteration": 2,
  "llm_records": [
    {
      "duration_ms": 1088,
      "error_category": null,
      "output": {
        "structured_output": {
          "routed_broad": [],
          "routed_direct": [
            {
              "queries": [
                "查看这篇文档的页数"
              ],
              "target_docs_description": "单篇文档",
              "target_docs_keywords": []
            }
          ],
          "routed_focused": [],
          "scope_direct": []
        }
      },
      "phase": "classification",
      "status": "ok"
    },
    {
      "duration_ms": 924,
      "error_category": null,
      "output": {
        "structured_output": {
          "decisions": [
            {
              "doc_id": "<DOC_ID>",
              "grade": "possible",
              "group_ref": "g0001"
            }
          ]
        }
      },
      "phase": "document_routing:1/1",
      "status": "ok"
    },
    {
      "duration_ms": 1617,
      "error_category": null,
      "output": {
        "structured_output": {},
        "tool_calls": [
          {
            "arguments": {
              "doc_ids": [
                "<DOC_ID>"
              ],
              "return_metainfo_types": [
                "page_count"
              ]
            },
            "name": "get_docs_metainfo"
          }
        ]
      },
      "phase": "direct_tool_planner",
      "status": "ok"
    }
  ],
  "response": {
    "chunk_count": 1,
    "chunks": [
      {
        "chunk_id": "direct:g0001:metainfo",
        "content_characters": 69,
        "content_truncated": false,
        "document_id": null,
        "page_number": null,
        "path": "document:metainfo",
        "source_type": "scope_metadata"
      }
    ],
    "classification": {
      "routed_broad": 0,
      "routed_direct": 1,
      "routed_focused": 0,
      "scope_direct": 0
    },
    "coverage": {
      "complete": true,
      "covered_group_refs": [
        "g0001"
      ],
      "degraded_group_refs": [],
      "truncated": false,
      "truncated_by": null,
      "uncovered_group_refs": []
    },
    "usage": {
      "actual_mode": "semantic",
      "candidate_document_count": 1,
      "completion_tokens": 217,
      "inspected_node_count": 0,
      "inspected_page_count": 0,
      "latency_ms": 3649,
      "llm_request_count": 3,
      "prompt_tokens": 4097,
      "returned_count": 1,
      "returned_tokens": 70,
      "tokenizer": "cl100k_base",
      "total_tokens": 4314
    },
    "warning_codes": []
  },
  "status": "ok"
}
```

### routed_focused

Query template: `这篇文档中关于<FOCUSED_TERM>的具体内容是什么`

#### Iteration 1

```json
{
  "iteration": 1,
  "llm_records": [
    {
      "duration_ms": 1139,
      "error_category": null,
      "output": {
        "structured_output": {
          "routed_broad": [],
          "routed_direct": [],
          "routed_focused": [
            {
              "queries": [
                "这篇文档中关于<FOCUSED_TERM>的具体内容是什么"
              ],
              "target_docs_description": "包含<FOCUSED_TERM>相关内容的文档",
              "target_docs_keywords": []
            }
          ],
          "scope_direct": []
        }
      },
      "phase": "classification",
      "status": "ok"
    },
    {
      "duration_ms": 927,
      "error_category": null,
      "output": {
        "structured_output": {
          "decisions": [
            {
              "doc_id": "<DOC_ID>",
              "grade": "possible",
              "group_ref": "g0001"
            }
          ]
        }
      },
      "phase": "document_routing:1/1",
      "status": "ok"
    },
    {
      "duration_ms": 966,
      "error_category": null,
      "output": {
        "structured_output": {},
        "tool_calls": [
          {
            "arguments": {
              "decision": "fallback_full_document"
            },
            "name": "finish_tree_navigation"
          }
        ]
      },
      "phase": "tree_navigation:<DOC_ID>:1",
      "status": "ok"
    },
    {
      "duration_ms": 1474,
      "error_category": null,
      "output": {
        "structured_output": {
          "decisions": [
            {
              "grade": "accept",
              "page_number": 4
            },
            {
              "grade": "accept",
              "page_number": 8
            },
            {
              "grade": "accept",
              "page_number": 3
            },
            {
              "grade": "accept",
              "page_number": 2
            },
            {
              "grade": "accept",
              "page_number": 7
            },
            {
              "grade": "accept",
              "page_number": 6
            },
            {
              "grade": "accept",
              "page_number": 1
            },
            {
              "grade": "accept",
              "page_number": 5
            }
          ]
        }
      },
      "phase": "page_inspection:1/1",
      "status": "ok"
    }
  ],
  "response": {
    "chunk_count": 3,
    "chunks": [
      {
        "chunk_id": "<DOC_ID>:page:1",
        "content_characters": 1250,
        "content_truncated": false,
        "document_id": "<DOC_ID>",
        "page_number": null,
        "path": "document:<DOC_ID>:page:1",
        "source_type": "page"
      },
      {
        "chunk_id": "<DOC_ID>:page:2",
        "content_characters": 1667,
        "content_truncated": false,
        "document_id": "<DOC_ID>",
        "page_number": null,
        "path": "document:<DOC_ID>:page:2",
        "source_type": "page"
      },
      {
        "chunk_id": "<DOC_ID>:page:3",
        "content_characters": 1655,
        "content_truncated": false,
        "document_id": "<DOC_ID>",
        "page_number": null,
        "path": "document:<DOC_ID>:page:3",
        "source_type": "page"
      }
    ],
    "classification": {
      "routed_broad": 0,
      "routed_direct": 0,
      "routed_focused": 1,
      "scope_direct": 0
    },
    "coverage": {
      "complete": false,
      "covered_group_refs": [
        "g0001"
      ],
      "degraded_group_refs": [],
      "truncated": true,
      "truncated_by": "max_return_tokens",
      "uncovered_group_refs": []
    },
    "usage": {
      "actual_mode": "semantic",
      "candidate_document_count": 1,
      "completion_tokens": 278,
      "inspected_node_count": 7,
      "inspected_page_count": 8,
      "latency_ms": 4557,
      "llm_request_count": 4,
      "prompt_tokens": 15632,
      "returned_count": 3,
      "returned_tokens": 3786,
      "tokenizer": "cl100k_base",
      "total_tokens": 15910
    },
    "warning_codes": [
      "RESULT_TOKEN_BUDGET_EXCEEDED"
    ]
  },
  "status": "ok"
}
```

#### Iteration 2

```json
{
  "iteration": 2,
  "llm_records": [
    {
      "duration_ms": 1612,
      "error_category": null,
      "output": {
        "structured_output": {
          "routed_broad": [],
          "routed_direct": [],
          "routed_focused": [
            {
              "queries": [
                "这篇文档中关于<FOCUSED_TERM>的具体内容是什么"
              ],
              "target_docs_description": "单篇文档",
              "target_docs_keywords": []
            }
          ],
          "scope_direct": []
        }
      },
      "phase": "classification",
      "status": "ok"
    },
    {
      "duration_ms": 1189,
      "error_category": null,
      "output": {
        "structured_output": {
          "decisions": [
            {
              "doc_id": "<DOC_ID>",
              "grade": "possible",
              "group_ref": "g0001"
            }
          ]
        }
      },
      "phase": "document_routing:1/1",
      "status": "ok"
    },
    {
      "duration_ms": 1102,
      "error_category": null,
      "output": {
        "structured_output": {},
        "tool_calls": [
          {
            "arguments": {
              "decision": "fallback_full_document"
            },
            "name": "finish_tree_navigation"
          }
        ]
      },
      "phase": "tree_navigation:<DOC_ID>:1",
      "status": "ok"
    },
    {
      "duration_ms": 2397,
      "error_category": null,
      "output": {
        "structured_output": {
          "decisions": [
            {
              "grade": "accept",
              "page_number": 4
            },
            {
              "grade": "accept",
              "page_number": 8
            },
            {
              "grade": "accept",
              "page_number": 3
            },
            {
              "grade": "accept",
              "page_number": 2
            },
            {
              "grade": "accept",
              "page_number": 7
            },
            {
              "grade": "accept",
              "page_number": 6
            },
            {
              "grade": "accept",
              "page_number": 1
            },
            {
              "grade": "accept",
              "page_number": 5
            }
          ]
        }
      },
      "phase": "page_inspection:1/1",
      "status": "ok"
    }
  ],
  "response": {
    "chunk_count": 3,
    "chunks": [
      {
        "chunk_id": "<DOC_ID>:page:1",
        "content_characters": 1250,
        "content_truncated": false,
        "document_id": "<DOC_ID>",
        "page_number": null,
        "path": "document:<DOC_ID>:page:1",
        "source_type": "page"
      },
      {
        "chunk_id": "<DOC_ID>:page:2",
        "content_characters": 1667,
        "content_truncated": false,
        "document_id": "<DOC_ID>",
        "page_number": null,
        "path": "document:<DOC_ID>:page:2",
        "source_type": "page"
      },
      {
        "chunk_id": "<DOC_ID>:page:3",
        "content_characters": 1655,
        "content_truncated": false,
        "document_id": "<DOC_ID>",
        "page_number": null,
        "path": "document:<DOC_ID>:page:3",
        "source_type": "page"
      }
    ],
    "classification": {
      "routed_broad": 0,
      "routed_direct": 0,
      "routed_focused": 1,
      "scope_direct": 0
    },
    "coverage": {
      "complete": false,
      "covered_group_refs": [
        "g0001"
      ],
      "degraded_group_refs": [],
      "truncated": true,
      "truncated_by": "max_return_tokens",
      "uncovered_group_refs": []
    },
    "usage": {
      "actual_mode": "semantic",
      "candidate_document_count": 1,
      "completion_tokens": 274,
      "inspected_node_count": 7,
      "inspected_page_count": 8,
      "latency_ms": 6348,
      "llm_request_count": 4,
      "prompt_tokens": 15628,
      "returned_count": 3,
      "returned_tokens": 3786,
      "tokenizer": "cl100k_base",
      "total_tokens": 15902
    },
    "warning_codes": [
      "RESULT_TOKEN_BUDGET_EXCEEDED"
    ]
  },
  "status": "ok"
}
```

### routed_broad

Query template: `请详细总结这篇文档`

#### Iteration 1

```json
{
  "iteration": 1,
  "llm_records": [
    {
      "duration_ms": 1938,
      "error_category": null,
      "output": {
        "structured_output": {
          "routed_broad": [
            {
              "queries": [
                "详细总结这篇文档"
              ],
              "target_docs_description": "单篇文档",
              "target_docs_keywords": []
            }
          ],
          "routed_direct": [],
          "routed_focused": [],
          "scope_direct": []
        }
      },
      "phase": "classification",
      "status": "ok"
    },
    {
      "duration_ms": 1444,
      "error_category": null,
      "output": {
        "structured_output": {
          "decisions": [
            {
              "doc_id": "<DOC_ID>",
              "grade": "possible",
              "group_ref": "g0001"
            }
          ]
        }
      },
      "phase": "document_routing:1/1",
      "status": "ok"
    }
  ],
  "response": {
    "chunk_count": 2,
    "chunks": [
      {
        "chunk_id": "<DOC_ID>:page:2",
        "content_characters": 1667,
        "content_truncated": false,
        "document_id": "<DOC_ID>",
        "page_number": null,
        "path": "document:<DOC_ID>:page:2",
        "source_type": "page"
      },
      {
        "chunk_id": "<DOC_ID>:page:3",
        "content_characters": 1655,
        "content_truncated": false,
        "document_id": "<DOC_ID>",
        "page_number": null,
        "path": "document:<DOC_ID>:page:3",
        "source_type": "page"
      }
    ],
    "classification": {
      "routed_broad": 1,
      "routed_direct": 0,
      "routed_focused": 0,
      "scope_direct": 0
    },
    "coverage": {
      "complete": false,
      "covered_group_refs": [
        "g0001"
      ],
      "degraded_group_refs": [
        "g0001"
      ],
      "truncated": true,
      "truncated_by": "max_return_tokens",
      "uncovered_group_refs": []
    },
    "usage": {
      "actual_mode": "semantic",
      "candidate_document_count": 1,
      "completion_tokens": 120,
      "inspected_node_count": 8,
      "inspected_page_count": 8,
      "latency_ms": 3407,
      "llm_request_count": 2,
      "prompt_tokens": 2405,
      "returned_count": 2,
      "returned_tokens": 2715,
      "tokenizer": "cl100k_base",
      "total_tokens": 2525
    },
    "warning_codes": [
      "BROAD_TRUNCATED_BY_BUDGET",
      "RESULT_TOKEN_BUDGET_EXCEEDED"
    ]
  },
  "status": "ok"
}
```

#### Iteration 2

```json
{
  "iteration": 2,
  "llm_records": [
    {
      "duration_ms": 1603,
      "error_category": null,
      "output": {
        "structured_output": {
          "routed_broad": [
            {
              "queries": [
                "详细总结这篇文档"
              ],
              "target_docs_description": "单篇文档",
              "target_docs_keywords": []
            }
          ],
          "routed_direct": [],
          "routed_focused": [],
          "scope_direct": []
        }
      },
      "phase": "classification",
      "status": "ok"
    },
    {
      "duration_ms": 1046,
      "error_category": null,
      "output": {
        "structured_output": {
          "decisions": [
            {
              "doc_id": "<DOC_ID>",
              "grade": "possible",
              "group_ref": "g0001"
            }
          ]
        }
      },
      "phase": "document_routing:1/1",
      "status": "ok"
    }
  ],
  "response": {
    "chunk_count": 2,
    "chunks": [
      {
        "chunk_id": "<DOC_ID>:page:2",
        "content_characters": 1667,
        "content_truncated": false,
        "document_id": "<DOC_ID>",
        "page_number": null,
        "path": "document:<DOC_ID>:page:2",
        "source_type": "page"
      },
      {
        "chunk_id": "<DOC_ID>:page:3",
        "content_characters": 1655,
        "content_truncated": false,
        "document_id": "<DOC_ID>",
        "page_number": null,
        "path": "document:<DOC_ID>:page:3",
        "source_type": "page"
      }
    ],
    "classification": {
      "routed_broad": 1,
      "routed_direct": 0,
      "routed_focused": 0,
      "scope_direct": 0
    },
    "coverage": {
      "complete": false,
      "covered_group_refs": [
        "g0001"
      ],
      "degraded_group_refs": [
        "g0001"
      ],
      "truncated": true,
      "truncated_by": "max_return_tokens",
      "uncovered_group_refs": []
    },
    "usage": {
      "actual_mode": "semantic",
      "candidate_document_count": 1,
      "completion_tokens": 120,
      "inspected_node_count": 8,
      "inspected_page_count": 8,
      "latency_ms": 2674,
      "llm_request_count": 2,
      "prompt_tokens": 2405,
      "returned_count": 2,
      "returned_tokens": 2715,
      "tokenizer": "cl100k_base",
      "total_tokens": 2525
    },
    "warning_codes": [
      "BROAD_TRUNCATED_BY_BUDGET",
      "RESULT_TOKEN_BUDGET_EXCEEDED"
    ]
  },
  "status": "ok"
}
```

## Acceptance Notes

- Every successful run must classify into its expected category and return at least one chunk.
- `routed_direct` and `routed_focused` exercise strict OpenAI function calling.
- Focused tree scan records contain node IDs only; candidate page arrays are expanded by code.
- LLM failures, rule fallback warnings, token truncation and incomplete coverage remain visible above.
