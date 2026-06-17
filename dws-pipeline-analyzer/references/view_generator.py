#!/usr/bin/env python3
"""
dws-pipeline-analyzer view_generator — 视图生成器
从 knowledge_final.json 生成多种输出视图。

Usage:
    dws-run analyzer view_generator \
        --input knowledge_final.json \
        --output docs/output/{target_table}/ \
        [--views mapping,asset,techspec]

支持的视图:
    mapping   → mapping.xlsx        (实体级+属性级字段映射)
    asset     → asset_report.html   (资产说明书，交互式 HTML)
    techspec  → tech_design.md      (技术设计文档)

默认: all (生成全部视图)
"""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
import re

# ── 工具函数 ──────────────────────────────────────────────

def _clean(s):
    """清洗字符串，None 转 空字符串"""
    if s is None:
        return ""
    return str(s).strip()


def _merge_ai_markdown(knowledge: dict, ai_text: str) -> None:
    """解析 AI 输出的自然语言 markdown，合并到 knowledge 的 business_logic。

    AI 输出格式:
        # 整体描述
        （描述文字）

        ## step_1
        （这步的描述）

        ## step_2
        ...

        ## 关键字段
        - 字段名: 含义
    """
    bl = knowledge.setdefault("business_logic", {})
    if "step_descriptions" not in bl:
        bl["step_descriptions"] = []
    if "key_transforms" not in bl:
        bl["key_transforms"] = []

    # 去掉 markdown 代码块标记
    text = ai_text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:markdown)?\n?", "", text)
        text = re.sub(r"\n?```$", "", text)

    # 按 ## 分段
    sections = re.split(r"^## ", text, flags=re.MULTILINE)

    for section in sections:
        section = section.strip()
        if not section:
            continue

        # 第一行是标题
        lines = section.split("\n", 1)
        title = lines[0].strip().lower()
        content = lines[1].strip() if len(lines) > 1 else ""

        # 去掉标题行的 # 前缀
        if title.startswith("# "):
            title = title[2:]

        if "整体描述" in title or title.startswith("# 整体"):
            bl["summary"] = content

        elif title.startswith("step_") or re.match(r"step_\d+", title):
            # 提取 step_id
            step_match = re.match(r"(step_\d+)", title)
            if step_match:
                step_id = step_match.group(1)
                # 找已有的 step_description
                desc = next((d for d in bl["step_descriptions"] if d.get("step_id") == step_id), None)
                if desc:
                    desc["purpose"] = content.split("\n")[0] if content else desc.get("purpose", "")
                    desc["logic"] = content
                    desc["is_auto_generated"] = False
                else:
                    bl["step_descriptions"].append({
                        "step_id": step_id,
                        "purpose": content.split("\n")[0] if content else "",
                        "logic": content,
                        "is_auto_generated": False,
                    })

        elif "关键字段" in title:
            # 解析 - 字段名: 含义
            for line in content.split("\n"):
                line = line.strip()
                if line.startswith("- ") or line.startswith("* "):
                    parts = line[2:].split(":", 1)
                    if len(parts) == 2:
                        fname = parts[0].strip()
                        meaning = parts[1].strip()
                        # 更新或添加 key_transforms
                        existing = next((kt for kt in bl["key_transforms"] if kt.get("field") == fname), None)
                        if existing:
                            existing["meaning"] = meaning
                        else:
                            bl["key_transforms"].append({"field": fname, "meaning": meaning})


def _schema_table(schema, table):
    """拼接 schema.table"""
    s = _clean(schema)
    t = _clean(table)
    if s and t:
        return f"{s}.{t}"
    return t or s


def _split_schema_table(full):
    """拆分 schema.table"""
    if "." in full:
        parts = full.split(".", 1)
        return parts[0].strip(), parts[1].strip()
    return "", full.strip()


def _layer_from_schema(schema, table=""):
    """从 schema 和 table 推断数仓层级"""
    s = (schema or "").upper()
    t = (table or "").upper()
    combined = s + "." + t if s else t

    if "ODS" in combined:
        return "ODS"
    if "DIM" in combined:
        return "DIM"
    if "DWB" in combined or "DWD" in combined or "DWL" in combined:
        return "DWB"
    if "DWS" in combined:
        return "DWS"
    if "ADS" in combined or "RPT" in combined or "SLPRD" in combined:
        return "ADS"
    if "TMP" in combined or "TEMP" in combined:
        return "TMP"
    # 无 schema 的短名（CTE、子查询）标记为 CTE
    if not s and t:
        return "CTE"
    return ""  # 不显示 UNKNOWN


# ── 数据转换 ──────────────────────────────────────────────

def build_report_data(knowledge):
    """将 knowledge 结构转换为 HTML 模板所需的 REPORT_DATA"""

    meta = knowledge.get("meta", {})
    topo = knowledge.get("topology", {})
    df = knowledge.get("data_flow", {})
    fm = knowledge.get("field_mappings", {})
    bl = knowledge.get("business_logic", {})
    quality = knowledge.get("quality", {})

    steps_list = topo.get("steps", [])
    data_flow_steps = df.get("steps", [])
    fields_list = fm.get("fields", [])

    # ── 构建 CTE 索引（用于穿透）──
    cte_index = _build_cte_index(data_flow_steps)
    cte_names_upper = set(cte_index.keys())
    target_types = meta.get("target_field_types", {})
    patterns = meta.get("patterns", [])

    # ── summary ──（取最大 exec_sequence 步骤的目标表作为最终目标表）
    target_table = ""
    if steps_list:
        _max_seq = max(s.get("exec_sequence", 0) for s in steps_list)
        _max_step = next((s for s in steps_list if s.get("exec_sequence", 0) == _max_seq), steps_list[0])
        ts = _max_step.get("target_schema", "")
        tt = _max_step.get("target_table", "")
        target_table = _schema_table(ts, tt)

    cm = quality.get("complexity_metrics", {})
    scenarios = topo.get("scenarios", [])
    summary = {
        "target_table": target_table,
        "table_cn_name": bl.get("summary", "").split("，")[0] if bl.get("summary") else "",
        "description": bl.get("summary", ""),
        "rule_count": len(steps_list),
        "field_count": len(set(f.get("target_field", "") for f in fields_list if f.get("target_field"))),
        "source_count": len(df.get("tables", [])),
        "scenario_count": len([s for s in scenarios if not s.get("is_common", False)]),
        "is_multi_scenario": len(scenarios) > 1,
        "generated_at": meta.get("analysis_time", datetime.now().strftime("%Y-%m-%d %H:%M")),
        "patterns": patterns,
        "scenarios": [{"id": s["id"], "name": s["name"], "rule_count": s["rule_count"],
                       "is_common": s.get("is_common", False)} for s in scenarios],
        "complexity": {
            "max_join_count": cm.get("max_join_count", 0),
            "max_cte_count": cm.get("max_cte_count", 0),
            "total_source_tables": cm.get("total_source_tables", 0),
            "total_case_when_branches": cm.get("total_case_when_branches", 0),
        },
    }

    # ── lineage (分层布局) ──
    lineage = _build_lineage_layout(topo, df, bl)

    # ── target_schema (目标表结构) ──
    schema_fields = []
    # 去重：同名字段只保留一个（跨步骤合并）
    seen_fields = {}
    for f in fields_list:
        fname = f.get("target_field", "")
        if fname and fname not in seen_fields:
            ai_transform = next(
                (kt for kt in bl.get("key_transforms", []) if kt.get("field") == fname),
                {}
            )
            seen_fields[fname] = {
                "name": fname,
                "type": target_types.get(fname.lower(), ""),
                "meaning": ai_transform.get("meaning", ""),
                "transform_type": f.get("transform_type", ""),
                "producing_step": f.get("producing_step", ""),
            }
    schema_fields = list(seen_fields.values())

    # ── steps (步骤详情 + SQL) ──
    steps_out = []
    for s in steps_list:
        step_id = s["step_id"]
        df_step = next((d for d in data_flow_steps if d.get("step_id") == step_id), {})
        # 优先用 AI 的 description，没有则用脚本兜底的（来自 business_logic.step_descriptions）
        all_step_descs = bl.get("step_descriptions", [])
        ai_step = next(
            (d for d in all_step_descs if d.get("step_id") == step_id),
            {}
        )

        steps_out.append({
            "step_id": step_id,
            "rule_code": s.get("rule_code", ""),
            "rule_name": s.get("rule_name", ""),
            "exec_sequence": s.get("exec_sequence", 0),
            "scenario_id": s.get("scenario_id", ""),
            "scenario_name": s.get("scenario_name", ""),
            "is_common_step": s.get("is_common_step", False),
            "delete_mode_label": s.get("delete_mode_label", ""),
            "delete_condition": s.get("delete_condition", ""),
            "source_tables": s.get("source_tables_from_sql", []),
            "target_table": s.get("target_table", ""),
            "purpose": ai_step.get("purpose", ""),
            "logic": ai_step.get("logic", ""),
            "raw_sql": df_step.get("raw_sql", ""),
            "ctes": [
                {
                    "name": c.get("name", ""),
                    "source_tables": [
                        {"name": st.get("name", ""), "alias": st.get("alias", ""), "join_type": st.get("join_type", "FROM")}
                        for st in c.get("source_tables", [])
                    ],
                    "field_count": len(c.get("fields", [])),
                }
                for c in df_step.get("ctes", [])
            ],
        })

    # data_dependencies（供字段链路树连线过滤用）
    data_deps = topo.get("data_dependencies", [])

    # ── 构建 alias→物理表名 映射（用于字段来源翻译）──
    alias_table_map = {}  # {step_id: {alias(UPPER): physical_table}}
    for s in data_flow_steps:
        sid = s.get("step_id", "")
        amap = {}
        for j in s.get("joins", []):
            alias = (j.get("alias") or "").upper()
            tbl = j.get("source_table", "")
            if alias and tbl:
                amap[alias] = tbl
        alias_table_map[sid] = amap

    # ── 构建 CTE 索引（用于字段来源 CTE 穿透）──
    cte_index = _build_cte_index(data_flow_steps)
    cte_names_upper = set(cte_index.keys())

    # ── fields (按场景分组，同场景内按目标表+字段名去重) ──
    def _resolve_sources(field_data, step_id):
        """构建字段来源列表，把别名翻译成物理表名，CTE 穿透到物理源表"""
        amap = alias_table_map.get(step_id, {})
        sources = []
        for l in field_data.get("lineage", []):
            src_alias = l.get("source_table", "") or l.get("alias", "")
            if not src_alias or src_alias.upper() in ("NULL", "NONE"):
                continue

            # 检查是否来自 CTE
            cte_name = l.get("cte_name", "")
            if cte_name and cte_name.upper() in cte_index:
                # CTE 穿透：从 lineage 的 cte_source_fields 取物理表字段
                cte_info = cte_index[cte_name.upper()]
                cte_field_info = cte_info["fields_map"].get(
                    (l.get("source_field", "") or "").upper(), {}
                )
                cte_alias_to_table = cte_info["alias_to_table"]
                cte_source_fields = cte_field_info.get("source_fields", l.get("cte_source_fields", []))
                for csf in cte_source_fields:
                    csf_alias = (csf.get("alias", "") or "").upper()
                    csf_field = csf.get("field", "")
                    physical_table = cte_alias_to_table.get(csf_alias, "")
                    if physical_table:
                        sources.append({
                            "table": physical_table,
                            "alias": csf.get("alias", ""),
                            "field": csf_field,
                        })
            else:
                # 普通字段：别名 → 物理表名
                physical_table = amap.get(src_alias.upper(), src_alias)
                sources.append({
                    "table": physical_table,
                    "alias": src_alias,
                    "field": l.get("source_field", ""),
                })
        return sources

    step_info_map = {}
    for s in steps_list:
        step_info_map[s["step_id"]] = {
            "target_table": s.get("target_table", ""),
            "scenario_name": s.get("scenario_name", "默认"),
            "rule_name": s.get("rule_name", ""),
            "exec_sequence": s.get("exec_sequence", 0),
        }

    # ── field_chain_map (字段 → 完整链路树，供详情面板用) ──
    # 提前构建（fields_out 需要用它计算最重加工类型）
    field_chain_map = {}
    for f in fields_list:
        fname = f.get("target_field", "")
        fname_lower = fname.lower()
        si = step_info_map.get(f.get("producing_step", ""), {})
        step_id = f.get("producing_step", "")
        sources = _resolve_sources(f, f.get("producing_step", ""))
        raw_sql = ""
        field_lineage = f.get("lineage", [])
        if field_lineage:
            raw_sql = field_lineage[0].get("raw_sql", "")

        if fname_lower not in field_chain_map:
            field_chain_map[fname_lower] = {"target_field": fname, "chains": []}
        field_chain_map[fname_lower]["chains"].append({
            "step_id": step_id,
            "rule_name": si.get("rule_name", step_id),
            "scenario": si.get("scenario_name", ""),
            "exec_sequence": si.get("exec_sequence", 0),
            "target_table": si.get("target_table", ""),
            "transform_type": f.get("transform_type", "expression"),
            "sources": sources,
            "raw_sql": raw_sql,
        })

    # ── fields (全局去重，不按场景分组；取链路中最重加工类型) ──
    fields_out = []
    seen_fields_lower = {}  # {field_lower: idx}

    # 先按 exec_sequence 排序，最终步骤优先
    sorted_fields = sorted(fields_list, key=lambda f: -step_info_map.get(f.get("producing_step", ""), {}).get("exec_sequence", 0))

    for f in sorted_fields:
        fname = f.get("target_field", "")
        fname_lower = fname.lower()
        si = step_info_map.get(f.get("producing_step", ""), {})
        scenario = si.get("scenario_name", "默认")
        target_table = si.get("target_table", "")
        sources = _resolve_sources(f, f.get("producing_step", ""))

        # 取链路中最重的加工类型
        chain_for_field = field_chain_map.get(fname_lower, {}).get("chains", [])
        chain_priority = {"unknown": -1, "direct": 0, "value": 1, "fallback": 2, "case_when": 3, "expression": 4, "aggregate": 5, "pivot": 6, "window": 7}
        best_tt = f.get("transform_type", "expression")
        for cc in chain_for_field:
            cc_tt = cc.get("transform_type", "expression")
            if chain_priority.get(cc_tt, 0) > chain_priority.get(best_tt, 0):
                best_tt = cc_tt

        # 取最初来源（seq 最小步骤的 sources，穿透到物理源表）
        origin_sources = []
        if chain_for_field:
            min_seq = min(c.get("exec_sequence", 0) for c in chain_for_field)
            origin_chains = [c for c in chain_for_field if c.get("exec_sequence", 0) == min_seq]
            seen_origin = set()
            for oc in origin_chains:
                for s in oc.get("sources", []):
                    key = f"{s.get('table','')}.{s.get('field','')}"
                    if key not in seen_origin and s.get("table"):
                        seen_origin.add(key)
                        origin_sources.append(s)

        # 判断是否在最终目标表中
        _max_seq_step = next((s for s in steps_list if s.get("exec_sequence", 0) == max(s.get("exec_sequence", 0) for s in steps_list)), None) if steps_list else None
        _final_target = (_max_seq_step.get("target_table", "") if _max_seq_step else "").lower()
        _producing_target = si.get("target_table", "").lower()
        is_final_field = _final_target and _final_target in _producing_target

        if fname_lower in seen_fields_lower:
            continue
        seen_fields_lower[fname_lower] = len(fields_out)
        fields_out.append({
            "target_field": fname,
            "producing_step": f.get("producing_step", ""),
            "target_table": target_table,
            "scenario": scenario,
            "transform_type": best_tt,
            "origin_sources": origin_sources,
            "is_final_field": is_final_field,
            "in_target_fields": f.get("in_target_fields", False),
            "excel_source_field": f.get("excel_source_field", ""),
            "sources": sources,
        })

    # ── field_chain_map (字段 → 完整链路树，供详情面板用) ──
    # 每个 field 收集它在所有步骤+所有场景中的来源
    field_chain_map = {}
    for f in fields_list:
        fname = f.get("target_field", "")
        fname_lower = fname.lower()
        si = step_info_map.get(f.get("producing_step", ""), {})
        step_id = f.get("producing_step", "")
        sources = _resolve_sources(f, f.get("producing_step", ""))

        if fname_lower not in field_chain_map:
            field_chain_map[fname_lower] = {
                "target_field": fname,
                "chains": [],  # 所有步骤的来源
            }
        # 取 raw_sql（加工表达式）
        raw_sql = ""
        field_lineage = f.get("lineage", [])
        if field_lineage:
            raw_sql = field_lineage[0].get("raw_sql", "")

        field_chain_map[fname_lower]["chains"].append({
            "step_id": step_id,
            "rule_name": si.get("rule_name", step_id),
            "scenario": si.get("scenario_name", ""),
            "exec_sequence": si.get("exec_sequence", 0),
            "target_table": si.get("target_table", ""),
            "transform_type": f.get("transform_type", "expression"),
            "sources": sources,
            "raw_sql": raw_sql,
        })

    # ── field_details (CTE 穿透血缘链) ──
    field_details = {}
    for f in fields_list:
        fname = f.get("target_field", "")
        new_type = f.get("transform_type", "expression")
        if fname in field_details:
            existing_type = field_details[fname].get("transform_type", "expression")
            if existing_type != "direct" and new_type == "direct":
                continue
        ai_transform = next(
            (kt for kt in bl.get("key_transforms", []) if kt.get("field") == fname),
            {}
        )

        # CTE 穿透血缘链
        penetration_chain = _build_penetration_chain(f.get("lineage", []), cte_index, cte_names_upper)

        field_details[fname] = {
            "target_field": fname,
            "producing_step": f.get("producing_step", ""),
            "rule_code": f.get("rule_code", ""),
            "transform_type": new_type,
            "in_target_fields": f.get("in_target_fields", False),
            "lineage": f.get("lineage", []),
            "penetration_chain": penetration_chain,
            "validation": f.get("validation", None),
            "meaning": ai_transform.get("meaning", ""),
        }

    # ── quality ──
    quality_out = {
        "complexity": quality.get("complexity_metrics", {}),
        "issues": quality.get("issues", []),
        "ai_insights": quality.get("ai_insights", []),
    }

    return {
        "summary": summary,
        "lineage": lineage,
        "schema_fields": schema_fields,
        "steps": steps_out,
        "fields": fields_out,
        "field_chain_map": field_chain_map,
        "data_deps": data_deps,
        "field_details": field_details,
        "quality": quality_out,
    }


def _format_join(join_type: str, source_table: str) -> str:
    """格式化 JOIN 描述（避免 'LEFT JOIN JOIN ON' 重复）"""
    if join_type == "FROM":
        return f"FROM {source_table}"
    return f"{join_type} {source_table}"


def _build_cte_index(data_flow_steps):
    """构建 CTE 索引用于穿透。"""
    cte_index = {}
    for s in data_flow_steps:
        for cte in s.get("ctes", []):
            cte_key = cte.get("name", "").upper()
            alias_to_table = {}
            for st in cte.get("source_tables", []):
                talias = st.get("alias", "").upper()
                tname = st.get("name", "")
                if talias:
                    alias_to_table[talias] = tname
            fields_map = {}
            for cf in cte.get("fields", []):
                if isinstance(cf, dict) and cf.get("name"):
                    fields_map[cf["name"].upper()] = cf
            cte_index[cte_key] = {
                "alias_to_table": alias_to_table,
                "fields_map": fields_map,
                "source_tables": cte.get("source_tables", []),
            }
    return cte_index


def _build_penetration_chain(lineages, cte_index, cte_names_upper, visited=None, depth=0):
    """构建字段穿透血缘链（用于详情面板展示）。

    返回: [{level, node_type, name, field, transform_type, expression}, ...]
    """
    if visited is None:
        visited = set()
    if depth > 10 or not lineages:
        return []

    chain = []
    for l in lineages:
        src_table = l.get("source_table", "")
        src_field = l.get("source_field", "")
        cte_name = l.get("cte_name", "")
        raw_sql = l.get("raw_sql", "")
        cte_transform = l.get("cte_transform_type", "")

        if cte_name and cte_name.upper() in cte_index:
            if cte_name.upper() in visited:
                continue
            visited.add(cte_name.upper())

            # CTE 节点
            chain.append({
                "level": depth,
                "node_type": "cte",
                "name": cte_name,
                "field": src_field,
                "transform_type": cte_transform or "unknown",
                "expression": l.get("cte_expression", raw_sql),
            })

            # 递归 CTE 内部
            cte_info = cte_index[cte_name.upper()]
            cte_field_info = cte_info["fields_map"].get(src_field.upper(), {})
            cte_source_fields = cte_field_info.get("source_fields", l.get("cte_source_fields", []))
            cte_alias_to_table = cte_info["alias_to_table"]

            for csf in cte_source_fields:
                csf_alias = csf.get("alias", "").upper()
                csf_field = csf.get("field", "")
                physical_table = cte_alias_to_table.get(csf_alias, "")

                if physical_table:
                    sch, tbl = _split_schema_table(physical_table)
                    chain.append({
                        "level": depth + 1,
                        "node_type": "physical",
                        "name": physical_table,
                        "field": csf_field,
                        "alias": csf.get("alias", ""),
                        "transform_type": "direct",
                        "expression": "",
                    })
                elif csf_alias in cte_names_upper:
                    # 嵌套 CTE
                    nested_chain = _build_penetration_chain(
                        [{"source_table": csf_alias, "source_field": csf_field, "cte_name": csf_alias}],
                        cte_index, cte_names_upper, visited, depth + 2
                    )
                    chain.extend(nested_chain)

            visited.discard(cte_name.upper())
        elif src_table and src_table.upper() not in cte_names_upper:
            # 物理源表
            chain.append({
                "level": depth,
                "node_type": "physical",
                "name": src_table,
                "field": src_field,
                "alias": l.get("alias", ""),
                "transform_type": l.get("transform", "direct"),
                "expression": raw_sql,
            })

    return chain


def _build_lineage_layout(topo, df, bl=None):
    """构建数据流向图布局。

    布局策略：
    - 步骤按 exec_sequence 分列（同 seq 同列）
    - 中间表放在产出步骤和消费步骤之间
    - 目标表放在最后一列
    - 来源表标记 hidden=true（默认不显示，可切换）
    - 来源表标注引用次数（被几个步骤使用）
    - 交互高亮：点击步骤高亮直接前后关系
    """
    tables = df.get("tables", [])
    steps_list = topo.get("steps", [])
    data_deps = topo.get("data_dependencies", [])
    data_flow_steps = df.get("steps", [])
    bl = bl or {}

    # ── 1. 分类节点 ──
    all_target_tables = {}  # {table_full(UPPER): step_id}
    for s in steps_list:
        tf = _schema_table(s.get("target_schema", ""), s.get("target_table", ""))
        all_target_tables[tf.upper()] = s["step_id"]

    final_targets = set()
    if steps_list:
        max_seq = max(s.get("exec_sequence", 0) for s in steps_list)
        for s in steps_list:
            if s.get("exec_sequence", 0) == max_seq:
                tf = _schema_table(s.get("target_schema", ""), s.get("target_table", ""))
                final_targets.add(tf)

    # CTE 名
    cte_names = set()
    cte_source_map = {}
    for s in data_flow_steps:
        for cte in s.get("ctes", []):
            cn = cte.get("name", "")
            if cn:
                cte_names.add(cn)
                phys = []
                for st in cte.get("source_tables", []):
                    tname = st.get("name", "")
                    if tname and tname.upper() not in cte_names:
                        phys.append(tname)
                cte_source_map[cn] = phys

    # 来源表引用计数 + 主表识别
    source_ref_count = {}  # {table_name(UPPER): count}
    step_primary_tables = {}  # {step_id: set(table_name(UPPER))}  主表集合
    for s in steps_list:
        sid = s["step_id"]
        primary = set()
        for src in s.get("source_tables_from_sql", []):
            su = src.upper()
            source_ref_count[su] = source_ref_count.get(su, 0) + 1
        # 从 data_flow 获取 JOIN 类型，识别主表
        df_step = next((d for d in data_flow_steps if d.get("step_id") == sid), {})
        for j in df_step.get("joins", []):
            jt = (j.get("join_type") or "").upper()
            tbl = (j.get("source_table") or "").upper()
            if jt == "FROM":
                # FROM 表始终是主表
                primary.add(tbl)
            elif "INNER" in jt or "CROSS" in jt:
                # INNER JOIN / CROSS JOIN 也是主表
                primary.add(tbl)
        step_primary_tables[sid] = primary
    # CTE 内部表也算
    for cte_name, phys_list in cte_source_map.items():
        for p in phys_list:
            pu = p.upper()
            source_ref_count[pu] = source_ref_count.get(pu, 0) + 1

    # ── 2. 构建节点 ──
    nodes = {}
    edges_list = []

    # 步骤节点
    step_seq_map = {}  # {step_id: seq}
    for s in steps_list:
        sid = s["step_id"]
        seq = s.get("exec_sequence", 0)
        step_seq_map[sid] = seq
        rule_name = s.get("rule_name", "")
        scenario_name = s.get("scenario_name", "")
        label = rule_name if rule_name else sid
        if scenario_name:
            label = f"[{scenario_name}] {label}"
        nodes[sid] = {
            "type": "step",
            "label": label,
            "hidden": False,
            "step_data": {
                "rule_code": s.get("rule_code", ""),
                "exec_sequence": seq,
                "scenario_name": scenario_name,
            },
        }

    # 表节点
    seen_tables = set()
    for t in tables:
        tname = _schema_table(t.get("schema", ""), t.get("name", ""))
        if not tname or tname in seen_tables:
            continue
        seen_tables.add(tname)
        if tname in cte_names:
            continue

        if tname in final_targets:
            node_type = "target"
            cn = bl.get("summary", "").split("，")[0] if bl.get("summary") else ""
            label = tname if not cn else f"{tname} ({cn})"
            hidden = False
        elif tname.upper() in all_target_tables:
            node_type = "intermediate"
            label = tname
            hidden = False
        else:
            node_type = "source"
            ref_count = source_ref_count.get(tname.upper(), 1)
            label = f"{tname} (×{ref_count})" if ref_count > 1 else tname
            hidden = True  # 来源表默认隐藏

        # 判断是否是某个步骤的主表
        is_primary = False
        for sid, primary_set in step_primary_tables.items():
            if tname.upper() in primary_set:
                is_primary = True
                break

        nodes[tname] = {"type": node_type, "label": label, "hidden": hidden, "step_data": None,
                        "is_primary": is_primary}

    # CTE 内部物理表
    for cte_name, phys_list in cte_source_map.items():
        for p in phys_list:
            if p not in seen_tables and p not in cte_names:
                seen_tables.add(p)
                ref_count = source_ref_count.get(p.upper(), 1)
                label = f"{p} (×{ref_count})" if ref_count > 1 else p
                nodes[p] = {"type": "source", "label": label, "hidden": True, "step_data": None,
                            "is_primary": False}

    # ── 3. 构建边 ──
    # 步骤 → 目标表/中间表
    step_to_table = {}  # {step_id: table_name}
    for s in steps_list:
        sid = s["step_id"]
        tf = _schema_table(s.get("target_schema", ""), s.get("target_table", ""))
        if tf in nodes:
            edges_list.append({"from": sid, "to": tf, "label": "", "type": "step_to_table"})
            step_to_table[sid] = tf

    # 来源表 → 步骤
    table_to_steps = {}  # {table: [step_ids]}
    for s in steps_list:
        sid = s["step_id"]
        for src in s.get("source_tables_from_sql", []):
            if src in nodes and src != sid:
                edges_list.append({"from": src, "to": sid, "label": "", "type": "source_to_step"})
                table_to_steps.setdefault(src, []).append(sid)
        # CTE 内部源表 → 步骤
        df_step = next((d for d in data_flow_steps if d.get("step_id") == sid), {})
        for cte in df_step.get("ctes", []):
            for st in cte.get("source_tables", []):
                tname = st.get("name", "")
                if tname in nodes and tname != sid:
                    edges_list.append({"from": tname, "to": sid, "label": f"CTE:{cte.get('name','')}", "type": "source_to_step"})
                    table_to_steps.setdefault(tname, []).append(sid)

    # 数据依赖（中间表 → 后续步骤）
    for dep in data_deps:
        from_step = dep.get("from", "")
        to_step = dep.get("to", "")
        # from_step 写的表 → to_step 读
        intermediate_table = step_to_table.get(from_step, "")
        if intermediate_table and intermediate_table in nodes and to_step in nodes:
            edges_list.append({"from": intermediate_table, "to": to_step, "label": "", "type": "dep"})

    # ── 4. 按 exec_sequence 分列计算坐标 ──
    LAYER_WIDTH = 280
    NODE_HEIGHT = 36
    NODE_GAP = 10
    MARGIN_TOP = 30
    MARGIN_LEFT = 20

    # 步骤的列 = exec_sequence
    # 中间表的列 = 产出步骤的列 + 0.5（放在步骤右边、下一步骤左边）
    # 目标表的列 = max_seq + 1
    # 来源表的列 = 使用它的步骤的列 - 0.5（放在步骤左边），但默认隐藏不渲染

    max_seq = max(step_seq_map.values()) if step_seq_map else 0

    # 计算每个节点的列号（浮点数，整数列放步骤，小数列放表）
    node_col = {}
    for nid, ninfo in nodes.items():
        if ninfo["type"] == "step":
            node_col[nid] = step_seq_map.get(nid, 0)
        elif ninfo["type"] == "target":
            node_col[nid] = max_seq + 1
        elif ninfo["type"] == "intermediate":
            # 找产出它的步骤
            producer_step = all_target_tables.get(nid.upper())
            if producer_step:
                node_col[nid] = step_seq_map.get(producer_step, 0) + 0.5
            else:
                node_col[nid] = max_seq + 0.5
        else:  # source
            # 放在使用它的最早步骤（最小 exec_sequence）的列 - 0.5
            # 这样所有引用此表的步骤都在表的右边，避免视觉回连
            using_steps = table_to_steps.get(nid, [])
            if using_steps:
                min_seq = min(step_seq_map.get(sid, 999) for sid in using_steps)
                node_col[nid] = min_seq - 0.5
            else:
                node_col[nid] = -0.5

    # 计算实际渲染列（包含所有节点，隐藏节点也占位以便开关打开时有坐标）
    visible_cols = sorted(set(node_col.values()))
    col_to_x = {}
    for i, col in enumerate(visible_cols):
        col_to_x[col] = MARGIN_LEFT + i * LAYER_WIDTH

    # 计算每个节点的 Y 坐标（同列垂直排列，隐藏节点也分配坐标）
    col_nodes = {}
    for nid in nodes:
        col = node_col[nid]
        col_nodes.setdefault(col, []).append(nid)

    max_nodes_in_col = max(len(v) for v in col_nodes.values()) if col_nodes else 0
    total_height = MARGIN_TOP * 2 + max_nodes_in_col * (NODE_HEIGHT + NODE_GAP)

    positions = {}
    node_meta = {}
    node_id_counter = 0

    for col in sorted(col_nodes.keys()):
        x = col_to_x.get(col, MARGIN_LEFT)
        col_nids = sorted(col_nodes[col], key=lambda n: (nodes[n]["type"] != "step", n))
        col_count = len(col_nids)
        total_h = col_count * (NODE_HEIGHT + NODE_GAP) - NODE_GAP
        start_y = max(MARGIN_TOP, (total_height - total_h) / 2)

        for ni, name in enumerate(col_nids):
            y = start_y + ni * (NODE_HEIGHT + NODE_GAP)
            node_id = f"n_{node_id_counter}"
            node_id_counter += 1
            positions[name] = node_id

            ninfo = nodes.get(name, {})
            node_meta[node_id] = {
                "id": node_id,
                "name": name,
                "label": ninfo.get("label", name),
                "x": x,
                "y": y,
                "width": 220,
                "height": NODE_HEIGHT,
                "type": ninfo.get("type", "source"),
                "layer": visible_cols.index(col) if col in visible_cols else 0,
                "hidden": ninfo.get("hidden", False),
                "step_data": ninfo.get("step_data"),
                "source_ref_count": source_ref_count.get(name.upper(), 1) if ninfo.get("type") == "source" else 0,
                "is_primary": ninfo.get("is_primary", False),
            }

    # 隐藏节点也需要 id（用于交互高亮时显示）
    for nid in nodes:
        if nid not in positions:
            node_id = f"n_{node_id_counter}"
            node_id_counter += 1
            positions[nid] = node_id
            ninfo = nodes[nid]
            node_meta[node_id] = {
                "id": node_id,
                "name": nid,
                "label": ninfo.get("label", nid),
                "x": 0,
                "y": 0,
                "width": 220,
                "height": NODE_HEIGHT,
                "type": ninfo.get("type", "source"),
                "layer": -1,
                "hidden": True,
                "step_data": None,
                "source_ref_count": source_ref_count.get(nid.upper(), 1),
                "is_primary": ninfo.get("is_primary", False),
            }

    # ── 5. 构建边输出 ──
    edges_out = []
    for e in edges_list:
        f, t = e["from"], e["to"]
        if f in positions and t in positions:
            edges_out.append({
                "from": positions[f],
                "to": positions[t],
                "label": e.get("label", ""),
                "type": e.get("type", "data_flow"),
                "from_hidden": nodes.get(f, {}).get("hidden", False),
                "to_hidden": nodes.get(t, {}).get("hidden", False),
            })

    self_ref_ids = []
    for sr in topo.get("self_references", []):
        target = sr.get("table", "")
        if target in positions:
            self_ref_ids.append(positions[target])

    total_width = MARGIN_LEFT * 2 + len(visible_cols) * LAYER_WIDTH
    return {
        "nodes": list(node_meta.values()),
        "edges": edges_out,
        "self_references": self_ref_ids,
        "layout": {
            "width": total_width,
            "height": total_height,
            "layer_count": len(visible_cols),
            "compact": max_nodes_in_col > 8,
            "layer_width": LAYER_WIDTH,
            "has_hidden_sources": any(n.get("hidden") for n in node_meta.values()),
        },
        "schedule_groups": [
            {"sequence": g.get("sequence", 0), "steps": g.get("parallel_steps", [])}
            for g in topo.get("schedule_plan", [])
        ],
    }


def _build_lineage(topo, df, bl=None):
    """构建血缘图的节点和边"""
    tables = df.get("tables", [])
    steps_list = topo.get("steps", [])
    data_deps = topo.get("data_dependencies", [])
    self_refs = topo.get("self_references", [])
    sched_plan = topo.get("schedule_plan", [])
    bl = bl or {}

    nodes = []
    edges = []
    node_ids = set()

    # Target node — show full schema.table
    target_full = ""
    if steps_list:
        ts = steps_list[0].get("target_schema", "")
        tt = steps_list[0].get("target_table", "")
        target_full = _schema_table(ts, tt)
        cn_name = bl.get("summary", "").split("，")[0] if bl.get("summary") else ""
        nodes.append({
            "id": "target",
            "name": target_full if ts else tt,
            "schema": ts,
            "role": "target",
            "layer": _layer_from_schema(ts, tt),
            "label_extra": cn_name,
        })
        node_ids.add("target")

    # Source nodes — show full schema.table or CTE name
    seen_sources = set()
    for s in steps_list:
        for src in s.get("source_tables_from_sql", []):
            if src in seen_sources:
                continue
            seen_sources.add(src)
            sch, tbl = _split_schema_table(src)
            # Don't add target table as source (self-ref handled separately)
            if _schema_table(sch, tbl) == target_full:
                continue
            nid = f"src_{len(nodes)}"
            display_name = src if sch else tbl  # CTE has no schema, show short name
            nodes.append({
                "id": nid,
                "name": display_name,
                "schema": sch,
                "role": "source",
                "layer": _layer_from_schema(sch, tbl),
            })
            node_ids.add(nid)

    # 建立 source_table -> node_id 映射
    # key 用原始 source_tables_from_sql 的值（与 steps 中的引用一致）
    src_to_node = {}
    for raw_src in seen_sources:
        sch, tbl = _split_schema_table(raw_src)
        if _schema_table(sch, tbl) == target_full:
            continue
        # 找到对应的 node
        for n in nodes:
            if n["role"] != "source":
                continue
            n_sch = n.get("schema", "")
            n_tbl_raw = n.get("name", "")
            # 匹配：schema.table 或 裸名
            if raw_src == n_tbl_raw or (n_sch and f"{n_sch}.{n_tbl_raw}" == raw_src) or (n_sch and n_tbl_raw == raw_src):
                src_to_node[raw_src] = n["id"]
                break

    # Step nodes + edges
    for i, s in enumerate(steps_list):
        sid = f"step_{i}"
        rc = s.get("rule_code", "")
        # Find AI purpose for this step
        ai_step = next(
            (d for d in bl.get("step_descriptions", []) if d.get("step_id") == s["step_id"]),
            {}
        )
        purpose = ai_step.get("purpose", "")
        step_label = f"{rc}"
        if purpose:
            step_label += f": {purpose}"

        nodes.append({
            "id": sid,
            "name": step_label,
            "schema": "",
            "role": "step",
            "layer": "",
        })

        # edges: source → step
        for src in s.get("source_tables_from_sql", []):
            sch, tbl = _split_schema_table(src)
            full = _schema_table(sch, tbl)
            if full == target_full:
                # self-reference: target → step (用虚线)
                edges.append({"from": "target", "to": sid, "label": s["step_id"]})
            else:
                nid = src_to_node.get(src)
                if nid:
                    edges.append({"from": nid, "to": sid, "label": ""})

        # edges: step → target (只有写入非视图的步骤才连 target)
        # 视图步骤(step_2)写入的是 _i 表，不直接写入目标 _f 表
        step_target = _schema_table(s.get("target_schema", ""), s.get("target_table", ""))
        if step_target == target_full:
            edges.append({"from": sid, "to": "target", "label": s["step_id"]})
        else:
            # 非主目标的写入步骤：创建一个对应的 target 节点
            alt_target_id = None
            for n in nodes:
                if n["role"] == "target" and n.get("name") == step_target:
                    alt_target_id = n["id"]
                    break
            if not alt_target_id:
                alt_id = f"tgt_{len(nodes)}"
                ts, tt = _split_schema_table(step_target)
                nodes.append({
                    "id": alt_id,
                    "name": step_target,
                    "schema": ts,
                    "role": "target",
                    "layer": _layer_from_schema(ts, tt),
                })
                alt_target_id = alt_id
            edges.append({"from": sid, "to": alt_target_id, "label": s["step_id"]})

    # Self-reference node ids
    self_ref_ids = []
    for sr in self_refs:
        self_ref_ids.append("target")  # self-ref always on target

    return {
        "nodes": nodes,
        "edges": edges,
        "self_references": self_ref_ids,
        "schedule_groups": [{"sequence": g.get("sequence", 0), "steps": g.get("parallel_steps", [])} for g in sched_plan],
    }


# ── 视图生成: asset_report.html ─────────────────────────

def generate_asset_report(knowledge, output_dir):
    """生成资产说明书 HTML"""
    report_data = build_report_data(knowledge)

    # 读取模板
    template_path = Path(__file__).parent / "templates" / "asset_report.html"
    if not template_path.exists():
        print(f"  错误: 模板文件不存在: {template_path}", file=sys.stderr)
        return False

    template = template_path.read_text(encoding="utf-8")

    # 替换占位符
    json_str = json.dumps(report_data, ensure_ascii=False, indent=2)
    # 转义 </script> 避免浏览器误解析
    json_str_safe = json_str.replace("</script>", "<\\/script>")
    html = template.replace("{{REPORT_DATA}}", json_str_safe)

    # 替换 title 占位符
    target_table = report_data["summary"]["target_table"]
    html = html.replace("{{TARGET_TABLE}}", target_table)

    # 写入
    output_path = Path(output_dir) / "asset_report.html"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    print(f"  ✓ 资产说明书: {output_path}")
    return True


# ── 视图生成: mapping.xlsx ──────────────────────────────

def generate_mapping(knowledge, output_dir):
    """生成 Mapping Excel

    规则：
    - 实体级 mapping：只展示物理源表 → 目标表（CTE/中间表不显示）
    - 属性级 mapping：穿透 CTE 到物理源表字段
    """
    try:
        from openpyxl import Workbook
    except ImportError:
        print("  错误: 缺少 openpyxl，请 pip install openpyxl", file=sys.stderr)
        return False

    topo = knowledge.get("topology", {})
    df = knowledge.get("data_flow", {})
    fm = knowledge.get("field_mappings", {})
    bl = knowledge.get("business_logic", {})

    steps_list = topo.get("steps", [])
    data_flow_steps = df.get("steps", [])
    fields_list = fm.get("fields", [])
    tables_info = df.get("tables", [])

    # ── 构建 CTE 字典：{CTE名(UPPER): {source_tables, fields_map}} ──
    cte_index = {}
    cte_names_upper = set()
    for s in data_flow_steps:
        for cte in s.get("ctes", []):
            cte_key = cte.get("name", "").upper()
            cte_names_upper.add(cte_key)
            src_tables = cte.get("source_tables", [])
            # 构建 alias → 物理表名 的映射
            alias_to_table = {}
            for st in src_tables:
                talias = st.get("alias", "").upper()
                tname = st.get("name", "")
                if talias:
                    alias_to_table[talias] = tname
            # 构建 field_name → source_fields 的映射
            fields_map = {}
            for cf in cte.get("fields", []):
                if isinstance(cf, dict) and cf.get("name"):
                    fields_map[cf["name"].upper()] = cf
            cte_index[cte_key] = {
                "alias_to_table": alias_to_table,
                "fields_map": fields_map,
                "source_tables": src_tables,
            }

    # ── 构建 target table 列表（用于过滤）──
    target_tables_set = set()
    for s in steps_list:
        tf = _schema_table(s.get("target_schema", ""), s.get("target_table", ""))
        target_tables_set.add(tf)

    wb = Workbook()

    # DDL 元数据（字段类型+中文名）
    meta = knowledge.get("meta", {})
    ddl_types = meta.get("target_field_types", {})
    ddl_comments = meta.get("target_field_comments", {})

    # ════════════════════════════════════════════════════════════
    # Sheet 1: 实体级 mapping — 物理源表 → 目标表
    # ════════════════════════════════════════════════════════════
    ws1 = wb.active
    ws1.title = "实体级mapping"
    # 源表别名放在源表表名后面
    headers1 = [
        "源表schema", "源表物理表名", "源表别名", "源表中文名",
        "目标表schema", "目标表中文名", "目标表物理表名",
        "关联&限定条件", "备注", "调度任务名称", "执行路径", "依赖参数"
    ]
    ws1.append(headers1)

    target_schema = steps_list[0].get("target_schema", "") if steps_list else ""
    target_table = ""
    if steps_list:
        _max_seq = max(s.get("exec_sequence", 0) for s in steps_list)
        _max_steps = [s for s in steps_list if s.get("exec_sequence", 0) == _max_seq]
        target_table = _max_steps[0].get("target_table", "") if _max_steps else ""
    target_cn = bl.get("summary", "").split("，")[0] if bl.get("summary") else target_table

    # 收集所有物理源表（含 CTE 内部物理表），排除 CTE 名和目标表自身
    seen_sources = set()
    entity_rows = []
    for s in steps_list:
        sid = s["step_id"]
        df_step = next((d for d in data_flow_steps if d.get("step_id") == sid), {})
        joins = df_step.get("joins", [])
        ctes = df_step.get("ctes", [])

        # 主查询 JOIN（物理表）
        for j in joins:
            src_full = j.get("source_table", "")
            if src_full in seen_sources or src_full in target_tables_set:
                continue
            # 排除 CTE 名（无 schema 的短名）
            if src_full.upper() in cte_names_upper:
                continue
            seen_sources.add(src_full)
            sch, tbl = _split_schema_table(src_full)
            join_type = j.get("join_type", "")
            join_cond = j.get("join_condition", "")
            if join_type == "FROM":
                relation = "主表"
            else:
                relation = f"{join_type} ON {join_cond}" if join_cond else join_type
            entity_rows.append([
                sch, tbl, j.get("alias", ""), "",
                target_schema, target_cn, target_table,
                relation, "", "", "", "",
            ])

        # CTE 内部物理表
        for cte in ctes:
            for st in cte.get("source_tables", []):
                tname = st.get("name", "")
                if not tname or tname in seen_sources or tname in target_tables_set:
                    continue
                # 排除 CTE 名引用（嵌套 CTE）
                if tname.upper() in cte_names_upper:
                    continue
                seen_sources.add(tname)
                sch, tbl = _split_schema_table(tname)
                talias = st.get("alias", "")
                jt = st.get("join_type", "FROM")
                relation = f"CTE {cte['name']} 内部 {jt}" if jt != "FROM" else f"CTE {cte['name']} 主表"
                entity_rows.append([
                    sch, tbl, talias, "",
                    target_schema, target_cn, target_table,
                    relation, "", "", "", "",
                ])

    for row in entity_rows:
        ws1.append(row)

    # ════════════════════════════════════════════════════════════
    # Sheet 2: 属性级 mapping — 穿透 CTE 到物理源表字段
    # ════════════════════════════════════════════════════════════
    ws2 = wb.create_sheet("属性级mapping")
    # 源表别名放在源表物理表名后面
    headers2 = [
        "场景", "源表schema", "源表物理表名", "源表别名", "源字段名", "源字段类型",
        "映射规则", "映射表达式",
        "目标字段名", "目标字段中文名", "目标字段类型"
    ]
    ws2.append(headers2)

    # 过滤视图步骤
    def _is_view_step(step_id: str) -> bool:
        df_step = next((d for d in data_flow_steps if d.get("step_id") == step_id), {})
        raw_sql = (df_step.get("raw_sql", "") or "").upper()
        if "CREATE VIEW" in raw_sql or "CREATE OR REPLACE VIEW" in raw_sql:
            return True
        return False

    # 构建 step_id → scenario_name 映射
    step_scenario = {}
    for s in steps_list:
        sid = s.get("step_id", "")
        step_scenario[sid] = s.get("scenario_name", "")

    # 字段按场景分组，同场景内字段去重（优先保留加工版本）
    # 不同场景可以有相同字段名（各自独立映射）
    scenario_fields: dict[str, list] = {}  # {scenario: [fields]}
    seen_in_scenario: dict[str, set] = {}   # {scenario: {field_names}}

    for f in fields_list:
        fname = f.get("target_field", "")
        step_id = f.get("producing_step", "")
        if _is_view_step(step_id) or not fname:
            continue

        scenario = step_scenario.get(step_id, "默认场景")
        if scenario not in scenario_fields:
            scenario_fields[scenario] = []
            seen_in_scenario[scenario] = set()

        # 同场景内去重
        if fname in seen_in_scenario[scenario]:
            # 找到已有的，优先保留加工版本
            for i, existing in enumerate(scenario_fields[scenario]):
                if existing.get("target_field") == fname:
                    existing_tt = existing.get("transform_type", "expression")
                    new_tt = f.get("transform_type", "expression")
                    priority = {"unknown": -1, "value": 0, "direct": 1, "expression": 2, "fallback": 3, "case_when": 4, "aggregate": 5, "pivot": 6, "window": 7}
                    if priority.get(new_tt, 0) > priority.get(existing_tt, 0):
                        scenario_fields[scenario][i] = f
                    break
        else:
            seen_in_scenario[scenario].add(fname)
            scenario_fields[scenario].append(f)

    # 按场景顺序输出
    for scenario, sc_fields in scenario_fields.items():
        for f in sc_fields:
            tt = f.get("transform_type", "expression")
            rule_map = {"direct": "直取", "value": "赋值"}
            rule = rule_map.get(tt, "加工")

            target_field_name = f.get("target_field", "")
            field_cn = ddl_comments.get(target_field_name.lower(), "")
            field_type = ddl_types.get(target_field_name.lower(), "")

            lineages = f.get("lineage", [])
            physical_sources = _resolve_physical_sources(lineages, cte_index, cte_names_upper, set())

            if not physical_sources:
                ws2.append([
                    scenario, "", "", "", "", "",
                    rule, "",
                    target_field_name, field_cn, field_type,
                ])
            else:
                for ps in physical_sources:
                    ws2.append([
                        scenario, ps["schema"], ps["table"], ps.get("alias", ""), ps["field"], "",
                        rule, ps.get("raw_sql", ""),
                        target_field_name, field_cn, field_type,
                    ])

    # 写入
    output_path = Path(output_dir) / "mapping.xlsx"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(output_path))
    print(f"  ✓ Mapping Excel: {output_path}")
    return True


def _resolve_physical_sources(lineages, cte_index, cte_names_upper, visited, depth=0):
    """穿透 CTE 到物理源表字段。

    lineages: field.lineage 列表
    cte_index: {CTE名(UPPER): {alias_to_table, fields_map, source_tables}}
    cte_names_upper: 所有 CTE 名的大写集合
    visited: 已访问的 CTE 名（防循环）
    depth: 递归深度（最大 10）

    返回: [{schema, table, field, alias, raw_sql}, ...]
    """
    if depth > 10 or not lineages:
        return []

    results = []
    for l in lineages:
        src_table = l.get("source_table", "")
        src_field = l.get("source_field", "")
        cte_name = l.get("cte_name", "")
        raw_sql = l.get("raw_sql", "")

        if cte_name and cte_name.upper() in cte_index:
            # 来源是 CTE — 递归穿透
            cte_info = cte_index[cte_name.upper()]
            if cte_name.upper() in visited:
                continue
            visited.add(cte_name.upper())

            cte_field_info = cte_info["fields_map"].get(src_field.upper(), {})
            cte_source_fields = cte_field_info.get("source_fields", l.get("cte_source_fields", []))
            cte_alias_to_table = cte_info["alias_to_table"]

            for csf in cte_source_fields:
                csf_alias = csf.get("alias", "").upper()
                csf_field = csf.get("field", "")

                # alias → 物理表
                physical_table = cte_alias_to_table.get(csf_alias, "")

                if physical_table:
                    # 找到物理表
                    sch, tbl = _split_schema_table(physical_table)
                    results.append({
                        "schema": sch,
                        "table": tbl,
                        "field": csf_field,
                        "alias": csf.get("alias", ""),
                        "raw_sql": raw_sql,
                    })
                elif csf_alias in cte_names_upper:
                    # 嵌套 CTE — 递归穿透
                    nested_cte = cte_index.get(csf_alias, {})
                    nested_fields_map = nested_cte.get("fields_map", {})
                    nested_field = nested_fields_map.get(csf_field.upper(), {})
                    nested_sources = nested_field.get("source_fields", [])
                    nested_alias_to_table = nested_cte.get("alias_to_table", {})
                    for nsf in nested_sources:
                        nsf_alias = nsf.get("alias", "").upper()
                        nsf_table = nested_alias_to_table.get(nsf_alias, "")
                        if nsf_table:
                            sch, tbl = _split_schema_table(nsf_table)
                            results.append({
                                "schema": sch,
                                "table": tbl,
                                "field": nsf.get("field", ""),
                                "alias": nsf.get("alias", ""),
                                "raw_sql": raw_sql,
                            })

            visited.discard(cte_name.upper())
        elif src_table and src_table.upper() not in cte_names_upper:
            # 直接物理源表
            sch, tbl = _split_schema_table(src_table)
            results.append({
                "schema": sch,
                "table": tbl,
                "field": src_field,
                "alias": l.get("alias", ""),
                "raw_sql": raw_sql,
            })

    return results


# ── 视图生成: tech_design.md ────────────────────────────

def generate_tech_design(knowledge, output_dir):
    """生成技术设计文档 Markdown"""
    topo = knowledge.get("topology", {})
    df = knowledge.get("data_flow", {})
    fm = knowledge.get("field_mappings", {})
    bl = knowledge.get("business_logic", {})
    quality = knowledge.get("quality", {})
    source = knowledge.get("source", {})

    steps_list = topo.get("steps", [])
    data_flow_steps = df.get("steps", [])
    fields_list = fm.get("fields", [])
    sched_plan = topo.get("schedule_plan", [])
    self_refs = topo.get("self_references", [])

    target_schema = steps_list[0].get("target_schema", "") if steps_list else ""
    target_table = ""
    if steps_list:
        _max_seq = max(s.get("exec_sequence", 0) for s in steps_list)
        _max_steps = [s for s in steps_list if s.get("exec_sequence", 0) == _max_seq]
        target_table = _max_steps[0].get("target_table", "") if _max_steps else ""
    target_full = _schema_table(target_schema, target_table)

    lines = []
    lines.append(f"# {target_table} 技术设计文档")
    lines.append("")
    lines.append(f"> 由 dws-pipeline-analyzer 从制品包反向生成")
    lines.append(f"> 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append("")

    # ── 1. 概述 ──
    lines.append("## 1. 概述")
    lines.append("")
    lines.append("| 项目 | 值 |")
    lines.append("|------|-----|")
    lines.append(f"| 目标表 | {target_full} |")
    lines.append(f"| 中文名 | {bl.get('summary', '').split('，')[0] if bl.get('summary') else '-'} |")
    lines.append(f"| 业务定位 | {bl.get('summary', '-')} |")
    lines.append(f"| 步骤数 | {len(steps_list)} |")
    lines.append(f"| 源表数 | {len(df.get('tables', []))} |")
    lines.append(f"| 字段数 | {len(fields_list)} |")
    lines.append(f"| 方言 | {knowledge.get('meta', {}).get('dialect', 'dws')} |")
    lines.append("")

    # ── 2. 复杂度分析 ──
    cm = quality.get("complexity_metrics", {})
    lines.append("## 2. 复杂度分析")
    lines.append("")
    lines.append("| 维度 | 值 |")
    lines.append("|------|-----|")
    lines.append(f"| 最大 JOIN 数 | {cm.get('max_join_count', '-')} |")
    lines.append(f"| CTE 数 | {cm.get('max_cte_count', '-')} |")
    lines.append(f"| 源表总数 | {cm.get('total_source_tables', '-')} |")
    lines.append(f"| CASE WHEN 分支 | {cm.get('total_case_when_branches', '-')} |")
    td = cm.get("transform_distribution", {})
    if td:
        dist_str = ", ".join(f"{k}={v}" for k, v in td.items())
        lines.append(f"| 转换类型分布 | {dist_str} |")
    lines.append("")

    # ── 3. 分段策略 ──
    lines.append("## 3. 分段策略")
    lines.append("")
    lines.append("| 步骤 | 规则编码 | 执行序列 | 源表 | 写入模式 |")
    lines.append("|------|---------|---------|------|---------|")
    for s in steps_list:
        wm = "TRUNCATE+INSERT" if s.get("delete_mode") == "1" else "APPEND"
        srcs = ", ".join(s.get("source_tables_from_sql", []))
        lines.append(f"| {s['step_id']} | {s.get('rule_code', '')} | {s.get('exec_sequence', 0)} | {srcs} | {wm} |")
    lines.append("")

    # 并行/串行说明
    if sched_plan:
        lines.append("**并行/串行关系:**")
        for g in sched_plan:
            seq = g.get("sequence", 0)
            psteps = ", ".join(g.get("parallel_steps", []))
            if len(g.get("parallel_steps", [])) > 1:
                lines.append(f"- 序列 {seq}: {psteps} （并行）")
            else:
                lines.append(f"- 序列 {seq}: {psteps}")
        lines.append("")

    # ── 4. 表级血缘 ──
    lines.append("## 4. 表级血缘")
    lines.append("")
    lines.append("```mermaid")
    lines.append("flowchart LR")

    # 构建 CTE 索引
    cte_index = _build_cte_index(data_flow_steps)
    cte_names_upper = set(cte_index.keys())
    target_tables_set = set()
    for s in steps_list:
        tf = _schema_table(s.get("target_schema", ""), s.get("target_table", ""))
        target_tables_set.add(tf)

    # 收集所有物理源表（含 CTE 内部表）
    all_phys_sources = []
    seen_src = set()
    for s in data_flow_steps:
        # 主查询 JOIN
        for j in s.get("joins", []):
            src_tbl = j.get("source_table", "")
            if src_tbl and src_tbl.upper() not in cte_names_upper and src_tbl not in target_tables_set and src_tbl not in seen_src:
                seen_src.add(src_tbl)
                all_phys_sources.append(src_tbl)
        # CTE 内部物理表
        for cte in s.get("ctes", []):
            for st in cte.get("source_tables", []):
                tname = st.get("name", "")
                if tname and tname.upper() not in cte_names_upper and tname not in target_tables_set and tname not in seen_src:
                    seen_src.add(tname)
                    all_phys_sources.append(tname)

    # 画物理源表 → 目标表
    target_safe = target_table.replace(".", "_")
    for src in all_phys_sources:
        src_safe = src.replace(".", "_")
        lines.append(f'    {src_safe}["{src}"] --> {target_safe}["{target_full}"]')

    # 目标表 → 下游视图
    for s in steps_list[1:]:
        tf = _schema_table(s.get("target_schema", ""), s.get("target_table", ""))
        if tf != target_full:
            tf_safe = tf.replace(".", "_")
            lines.append(f'    {target_safe}["{target_full}"] --> {tf_safe}["{tf}"]')

    # 自引用
    if self_refs:
        for sr in self_refs:
            lines.append(f'    {target_safe} -.->|自引用| {target_safe}')
    lines.append("```")
    lines.append("")

    # ── 5. 字段映射对照表 ──
    lines.append("## 5. 字段映射对照表")
    lines.append("")
    for s in steps_list:
        sid = s["step_id"]
        rc = s.get("rule_code", "")
        step_fields = [f for f in fields_list if f.get("producing_step") == sid]
        if not step_fields:
            continue

        lines.append(f"### {sid} ({rc})")
        lines.append("")
        lines.append("| # | 目标字段 | 物理源表 | 物理源字段 | 转换类型 | 映射表达式 |")
        lines.append("|---|---------|---------|-----------|---------|-----------|")
        for i, f in enumerate(step_fields, 1):
            # CTE 穿透获取物理源
            phys_sources = _resolve_physical_sources(
                f.get("lineage", []), cte_index, cte_names_upper, set()
            )
            tt = f.get('transform_type', '')
            raw_sql = ""
            if f.get("lineage"):
                raw_sql = f["lineage"][0].get("raw_sql", "")
            if len(raw_sql) > 100:
                raw_sql = raw_sql[:97] + "..."

            if phys_sources:
                ps0 = phys_sources[0]
                lines.append(f"| {i} | {f.get('target_field', '')} | {ps0.get('table', '-')} | {ps0.get('field', '-')} | {tt} | {raw_sql} |")
            else:
                lines.append(f"| {i} | {f.get('target_field', '')} | - | - | {tt} | {raw_sql} |")
        lines.append("")

    # ── 6. 数据处理逻辑 ──
    lines.append("## 6. 数据处理逻辑")
    lines.append("")
    for s in data_flow_steps:
        sid = s.get("step_id", "")
        ai_step = next(
            (d for d in bl.get("step_descriptions", []) if d.get("step_id") == sid), {}
        )
        lines.append(f"### {sid}: {ai_step.get('purpose', '')}")
        if ai_step.get("logic"):
            lines.append(f"- **加工逻辑**: {ai_step['logic']}")
        where = s.get("where_clause", "")
        if where:
            lines.append(f"- **过滤条件**: {where}")
        group_by = s.get("group_by", [])
        if group_by:
            lines.append(f"- **分组**: {', '.join(group_by)}")
        lines.append("")
        lines.append("```sql")
        lines.append(s.get("raw_sql", ""))
        lines.append("```")
        lines.append("")

    # ── 7. 质量评估 ──
    lines.append("## 7. 质量评估")
    lines.append("")
    issues = quality.get("issues", [])
    ai_insights = quality.get("ai_insights", [])
    if issues:
        lines.append("### 检测到的问题")
        lines.append("")
        lines.append("| 级别 | 类别 | 描述 |")
        lines.append("|------|------|------|")
        for iss in issues:
            lines.append(f"| {iss.get('severity', '')} | {iss.get('category', '')} | {iss.get('title', '')} |")
        lines.append("")

    if ai_insights:
        lines.append("### AI 建议")
        lines.append("")
        for ins in ai_insights:
            lines.append(f"- **[{ins.get('severity', '')}]** {ins.get('title', '')}")
            if ins.get("detail"):
                lines.append(f"  - {ins['detail']}")
            if ins.get("suggestion"):
                lines.append(f"  - 建议: {ins['suggestion']}")
        lines.append("")

    # ── 8. 上游任务依赖 ──
    lines.append("## 8. 上游任务依赖")
    lines.append("")
    lines.append("| 源表 | 别名 | 关联方式 | CTE归属 | 调度任务 |")
    lines.append("|------|------|---------|---------|---------|")
    seen_dep = set()
    for s in data_flow_steps:
        # 主查询 JOIN
        for j in s.get("joins", []):
            src = j.get("source_table", "")
            if src in seen_dep or src.upper() in cte_names_upper:
                continue
            seen_dep.add(src)
            jt = j.get("join_type", "")
            lines.append(f"| {src} | {j.get('alias', '-')} | {_format_join(jt, src)} | - | 待配置 |")
        # CTE 内部物理表
        for cte in s.get("ctes", []):
            cte_name = cte.get("name", "")
            for st in cte.get("source_tables", []):
                tname = st.get("name", "")
                if tname in seen_dep or tname.upper() in cte_names_upper:
                    continue
                seen_dep.add(tname)
                jt = st.get("join_type", "FROM")
                lines.append(f"| {tname} | {st.get('alias', '-')} | {_format_join(jt, tname)} | {cte_name} | 待配置 |")
    lines.append("")

    # ── 9. 执行平台配置 ──
    lines.append("## 9. 执行平台配置")
    lines.append("")
    raw_rules = source.get("rule_sheet_raw", [])
    if raw_rules:
        lines.append("| 配置项 | 值 |")
        lines.append("|--------|-----|")
        r0 = raw_rules[0] if raw_rules else {}
        config_map = {
            "项目编码": r0.get("project_code", ""),
            "数据源": r0.get("data_source", ""),
        }
        for k, v in config_map.items():
            lines.append(f"| {k} | {v or '待配置'} |")
    else:
        lines.append("*无执行平台配置信息*")
    lines.append("")

    # 写入
    output_path = Path(output_dir) / "tech_design.md"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"  ✓ 技术设计文档: {output_path}")
    return True


# ── CLI ──────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="dws-pipeline-analyzer 视图生成器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  dws-run analyzer view_generator --input knowledge_final.json --output docs/output/table/
  dws-run analyzer view_generator --input knowledge_final.json --output docs/output/table/ --views mapping,asset
        """,
    )
    parser.add_argument("--input", required=True, help="knowledge_draft.json 路径")
    parser.add_argument("--ai-input", default=None, help="knowledge_ai.md 路径（AI 增强结果，可选）")
    parser.add_argument("--output", required=True, help="输出目录")
    parser.add_argument(
        "--views",
        default="all",
        help="要生成的视图，逗号分隔: mapping,asset,techspec (默认: all)",
    )

    args = parser.parse_args()

    # 读取 knowledge
    input_path = Path(args.input)
    if not input_path.exists():
        print(f"错误: 输入文件不存在: {input_path}", file=sys.stderr)
        sys.exit(1)

    with open(input_path, "r", encoding="utf-8") as f:
        knowledge = json.load(f)

    # 合并 AI 增强结果（可选）
    if args.ai_input:
        ai_path = Path(args.ai_input)
        if ai_path.exists():
            ai_text = ai_path.read_text(encoding="utf-8")
            _merge_ai_markdown(knowledge, ai_text)
            print(f"  ✓ 已合并 AI 增强: {ai_path}")
        else:
            print(f"  ⚠ AI 输入文件不存在: {ai_path}（跳过）")

    # 输出目录: 直接用用户指定的 output 目录
    views_dir = Path(args.output)
    views_dir.mkdir(parents=True, exist_ok=True)
    views_str = args.views.strip().lower()
    if views_str == "all":
        views = ["mapping", "asset", "techspec"]
    else:
        views = [v.strip() for v in views_str.split(",") if v.strip()]

    print(f"═══ dws-pipeline-analyzer 视图生成器 ═══")
    print(f"输入: {input_path}")
    print(f"输出: {views_dir}")
    print(f"视图: {', '.join(views)}")
    print()

    results = {}
    for view in views:
        if view == "mapping":
            results["mapping"] = generate_mapping(knowledge, str(views_dir))
        elif view == "asset":
            results["asset"] = generate_asset_report(knowledge, str(views_dir))
        elif view == "techspec":
            results["techspec"] = generate_tech_design(knowledge, str(views_dir))
        else:
            print(f"  警告: 未知视图类型 '{view}'，跳过", file=sys.stderr)

    print()
    success = sum(1 for v in results.values() if v)
    total = len(results)
    print(f"═══ 完成: {success}/{total} 视图生成成功 ═══")

    if success < total:
        sys.exit(1)


if __name__ == "__main__":
    main()
