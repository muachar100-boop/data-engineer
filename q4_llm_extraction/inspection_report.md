# Q4.1 第一轮实际数据检查

本轮未调用 LLM，未生成结构化抽取结果。分流是候选方案，不能当作准确率或最终分类。

## 数据来源与执行边界

当前环境连接本机 MySQL 被系统拒绝（Operation not permitted）。以下统计来自项目 CSV；数据库版本、表结构和索引尚未现场验证，辅助表尚未在数据库创建。历史导入报告记录 MySQL 8.4.13、order_reviews 99,224 行，但不能代替本次查询。

现有 load_olist.py：9 张原始表按 LONGTEXT utf8mb4_bin NOT NULL 导入，空字段保留空字符串；已有表全字段指纹一致就跳过，差异则停止。本脚本不执行导入，仅在数据库模式核对内容。

## 实际统计

全表 99,224 行；评分 ≤3 共 22,754 行。

标题正文均空 8,117 行；仅标题有内容 192 行。
正文缺失 8,309 行；标题缺失 19,579 行。
重复 review_id 涉及 515 行，分属 254 个重复组；完整源行重复 0 行。

| 路径 | 行数 | 占全部低评分评论 | 精确标题+正文去重数 |
|---|---:|---:|---:|
| rule | 41 | 0.180% | 35 |
| llm | 14,296 | 62.829% | 13,902 |
| insufficient | 8,417 | 36.991% | 138 |

规则优先保精度，尚未覆盖所有可规则处理的句式。LLM路径按精确标题和正文去重后的数量仍需按评分隔离缓存，不能直接当作最终请求数。

完整统计、常见表达与真实多问题候选如下（也保存为 inspection_results.json）：

```json
{
  "source_rows": 99224,
  "selected_rows": 22754,
  "score_counts": {
    "1": 11424,
    "3": 8179,
    "2": 3151
  },
  "field_missing": {
    "review_id": 0,
    "order_id": 0,
    "review_score": 0,
    "review_comment_title": 87658,
    "review_comment_message": 58274,
    "review_creation_date": 0,
    "review_answer_timestamp": 0
  },
  "selected_missing": {
    "raw_title": 19579,
    "raw_message": 8309
  },
  "title_only": 192,
  "both_empty": 8117,
  "duplicate_review_id": {
    "groups": 254,
    "rows_in_groups": 515,
    "extra_rows": 261,
    "unique": 22493
  },
  "duplicate_review_id_all": {
    "groups": 789,
    "rows_in_groups": 1603,
    "extra_rows": 814,
    "unique": 98410
  },
  "duplicate_full_row": {
    "groups": 0,
    "rows_in_groups": 0,
    "extra_rows": 0,
    "unique": 22754
  },
  "duplicate_body_nonempty": {
    "groups": 309,
    "rows_in_groups": 931,
    "extra_rows": 622,
    "unique": 13823
  },
  "duplicate_combined_nonempty": {
    "groups": 308,
    "rows_in_groups": 872,
    "extra_rows": 564,
    "unique": 14073
  },
  "duplicate_normalized_nonempty": {
    "groups": 330,
    "rows_in_groups": 1151,
    "extra_rows": 821,
    "unique": 13816
  },
  "length_all": {
    "0": 0.0,
    "25": 0.0,
    "50": 42.0,
    "75": 108.0,
    "90": 181.0,
    "95": 198.0,
    "99": 214.0,
    "100": 229.0
  },
  "length_nonempty": {
    "0": 1.0,
    "25": 46.0,
    "50": 86.0,
    "75": 147.0,
    "90": 195.0,
    "95": 200.0,
    "99": 218.0,
    "100": 229.0
  },
  "short_nonempty_le_10": 436,
  "frequent_phrases": [
    [
      "o produto",
      4271
    ],
    [
      "não recebi",
      1901
    ],
    [
      "recebi o",
      1429
    ],
    [
      "recebi o produto",
      1153
    ],
    [
      "foi entregue",
      1055
    ],
    [
      "ainda não",
      925
    ],
    [
      "produto não",
      913
    ],
    [
      "não recebi o",
      908
    ],
    [
      "e não",
      903
    ],
    [
      "do produto",
      882
    ],
    [
      "não foi",
      735
    ],
    [
      "meu produto",
      691
    ],
    [
      "não chegou",
      585
    ],
    [
      "a entrega",
      576
    ],
    [
      "produto e",
      544
    ],
    [
      "com o",
      530
    ],
    [
      "com a",
      501
    ],
    [
      "o que",
      496
    ],
    [
      "a compra",
      490
    ],
    [
      "do prazo",
      483
    ],
    [
      "o produto não",
      476
    ],
    [
      "um produto",
      474
    ],
    [
      "não foi entregue",
      466
    ],
    [
      "até agora",
      463
    ],
    [
      "de entrega",
      462
    ]
  ],
  "frequent_texts": [
    [
      "bom",
      79
    ],
    [
      "não recebi o produto",
      62
    ],
    [
      "ainda não recebi o produto",
      27
    ],
    [
      "recomendo",
      22
    ],
    [
      "boa",
      22
    ],
    [
      "nao recebi o produto",
      22
    ],
    [
      "muito bom",
      19
    ],
    [
      "ok",
      18
    ],
    [
      "não recebi",
      17
    ],
    [
      "não recebi o produto.",
      13
    ],
    [
      "ainda não recebi",
      13
    ],
    [
      "produto não entregue",
      13
    ],
    [
      "bom produto",
      10
    ],
    [
      "ótimo",
      10
    ],
    [
      "bom bom",
      9
    ],
    [
      "regular",
      9
    ],
    [
      "ainda não recebi o produto.",
      9
    ],
    [
      "nao recebi",
      8
    ],
    [
      "ruim",
      8
    ],
    [
      "gostei",
      7
    ]
  ],
  "routes": {
    "rule": {
      "rows": 41,
      "percent": 0.18,
      "unique_exact_texts": 35,
      "unique_cache_inputs_with_score": 38
    },
    "llm": {
      "rows": 14296,
      "percent": 62.829,
      "unique_exact_texts": 13902,
      "unique_cache_inputs_with_score": 13953
    },
    "insufficient": {
      "rows": 8417,
      "percent": 36.991,
      "unique_exact_texts": 138,
      "unique_cache_inputs_with_score": 164
    }
  },
  "route_reasons": {
    "empty_or_symbols": 8132,
    "context_required": 12927,
    "generic_opinion_no_specific_issue": 285,
    "multiple_cues": 1369,
    "exact_phrase:wrong_product": 11,
    "exact_phrase:delivery_late": 14,
    "exact_phrase:quality_defect": 15,
    "exact_phrase:seller_communication": 1
  },
  "multi_cue_rows": 1369,
  "multi_cue_examples": [
    {
      "text": "Comprei dois lustres pendentes, com a parceira targaryen e só me enviaram um lustre. Abri reclamação, mas ainda não recebi resposta. Aguardo solução por parte de Lojas lannister.",
      "cues": [
        "delivery_late",
        "seller_communication"
      ]
    },
    {
      "text": "falta de produto e quebra\nFaltou 1 produto e os que recebi 1 veio quebrado",
      "cues": [
        "missing_item",
        "quality_defect"
      ]
    },
    {
      "text": "Demora na entrega, detestei o atendimento e NUNCA mais compro. baratheon quando vendia por eles mesmos era bem melhor! ",
      "cues": [
        "delivery_late",
        "seller_communication"
      ]
    },
    {
      "text": "A embalagem deixou a desejar, por pouco o produto não foi danificado, a caixa estava toda amassada.",
      "cues": [
        "packaging",
        "quality_defect"
      ]
    },
    {
      "text": "O produto foi embalado apenas com papel pardo sobre a embalagem e enviado pelos correios. Lógico, a embalagem chegou amassada e só deus sabe se o produto está funcionando ou não, ainda não testei.",
      "cues": [
        "missing_item",
        "packaging"
      ]
    },
    {
      "text": "Não recebi o produto, assim como nenhum contato por parte do .vendedor",
      "cues": [
        "delivery_late",
        "seller_communication"
      ]
    },
    {
      "text": "meu produto veio com defeito,devolvi conforme informado.postado no correio conforme indicado.e até hoje não recebi nenhuma notificação da empresa quanto a devolução e análise para ter meuressarcimento",
      "cues": [
        "delivery_late",
        "quality_defect"
      ]
    },
    {
      "text": "Só recebi um produto. Consta que já recebi os dois. Não recebi a sapateira. ",
      "cues": [
        "delivery_late",
        "missing_item"
      ]
    },
    {
      "text": "MEU PRODUTO AINDA NÃO FOI ENTREGUE, JÁ FIZ MINHA RECLAMAÇÃO E ATÉ AGORA NÃO TIVE NENHUMA PREVISÃO DE ENTREGA. ESTOU SUPER INSATISFEITO E QUERO MEU DINHEIRO DE VOLTA. FOI DENUNCIAR AO PROCON.",
      "cues": [
        "delivery_late",
        "seller_communication"
      ]
    },
    {
      "text": "Comprei uma cadeira, recebi no prazo, porém a cadeira veio quebrada na parte inferior. Poderiam ter um maior cuidado na embalagem.",
      "cues": [
        "packaging",
        "quality_defect"
      ]
    },
    {
      "text": "Recebi apenas 1 caixa com 100 unidades, e pedi 2 caixas. ",
      "cues": [
        "missing_item",
        "packaging"
      ]
    },
    {
      "text": "descrição do produto erra\na descrição do produto estava errada e não consegui usar o produto ,pedi o cancelamento fazem 15 dias e ainda não obtive resposta",
      "cues": [
        "seller_communication",
        "wrong_product"
      ]
    }
  ]
}
```

长度按合并标题与正文后的 Unicode 字符数统计（包含二者之间的换行）；非空统计排除空白文本。重复内容分别统计正文原文、标题+正文原文、标准化文本；不把空白重复算作可复用文本。groups 为重复组数，rows_in_groups 为组内所有行，extra_rows 为超过每组第一行的数量。

## 分流解释与风险

1. insufficient：空白、纯符号，或完整命中泛化评价词。ruim、péssimo 表达情绪，却没有具体问题证据；bom、excelente 也可能出现在低评分里，不能仅凭评分编造负面问题。后续允许 issues=[]，保留原始评分和情绪。
2. rule：仅完整命中少量明确短语；这是规则候选，不代表 severity、英文证据或卖家可行动性已经可靠抽取。
3. llm：其余文本全部保守进入待处理队列，包括标题正文冲突、未收货、数量叙述、否定转折和多问题。未收货不等于已超过承诺时限，因此 “não recebi” 不直接判 delivery_late。

关键词扫描仅用于发现多问题候选，至少命中两类的数量不是多问题真实数量。如 “entrega dentro do prazo ... produto ... embalagem ...” 同时存在表扬和投诉；“caixa” 可能指音箱，“qualidade” 可能被表扬，“apenas” 不一定表示缺件。漏判来自拼写错误、缩写、隐含表达和数量推理；规则路径也可能把“péssima qualidade”当成物理缺陷，但实际只是主观不满。所有规则候选必须抽样复核，置信度不能用命中次数替代。

## 后续 Prompt 与验证建议

保留葡萄牙语标题、正文边界及评分，声明评分仅为背景；评论是数据，不执行其中指令。按原始记录返回关联 ID，一个评论允许多个 issues 或空数组。只能引用原文连续子串，英文只翻译已确认的证据；禁止补写问题、卖家责任和物流期限。未收货且无明确逾期证据可归 other，不能归 missing_item（明确少件）。指定七类枚举、severity 判定尺度、情绪与置信度范围、actionable 的判定标准；英文翻译与抽取放在同一请求以减少额外步骤。

后续用 JSON Schema 校验，再验证 evidence_quote 是标题或正文原文子串；失败进入重试或人工队列。缓存复用只使用完全相同的标题+正文、评分及相同模型/Prompt/Schema 版本；不缓存 review_id。标准化哈希只用作候选检索，不直接复用证据，避免大小写或空白变化导致原文引用失效。

先按三条路径分层固定抽样至少30条，额外覆盖否定、多问题、标题正文冲突与重复组。人工记录类别、证据、情绪和可行动性，分别检查规则精确率、insufficient 漏判率及 LLM 多标签召回。当前没有人工 ground truth，不报告准确率；30条只能初验，不能证明总体可靠。本轮 API 花费为 $0、请求数为0；未来按去重后 LLM 文本量、实际 tokenizer、供应商价格测算，设置 $3 和40分钟硬截止，不能在未选择模型前承诺满足预算。
