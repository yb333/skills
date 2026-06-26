"""字段使用情况批量搜索工具。

从多个规则组的 Excel 中，按关键字搜索字段的使用情况，
输出一张 Excel（按目标表分组）。

设计原则: 复用 analyzer 的完整解析能力（含 enrich 追溯），
field_search 只负责"搜索关键字 + 组织输出"，不复制解析逻辑。

使用:
    python run.py field_search --input execution_tasks.xlsx --keyword amount --output field_usage.xlsx
    python run.py field_search --input execution_tasks.xlsx --keyword "amount,user_id" --output field_usage.xlsx
"""

import sys
from pathlib import Path
from dataclasses import dataclass


@dataclass
class FieldUsage:
    """单个字段的使用记录。"""
    target_table: str = ""       # 目标表（schema.table）
    field_name: str = ""         # 字段名
    role: str = ""               # 字段角色：写入目标表/临时过程使用/辅助字段
    situation: str = ""          # 字段情况：直取/加工/关联带出/关联键/过滤条件
    source: str = ""             # 最初来源（物理源表.字段，辅助字段填使用步骤）
    detail: str = ""             # 详情（加工表达式/关联条件/过滤条件）


def read_excel_grouped(excel_path: str) -> list:
    """读取 Excel，按规则组编码分组返回。

    Returns: [{rule_group_code, rule_group_en, rules: [RawRule], ...}, ...]
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from analyzer import read_excel

    raw = read_excel(excel_path)
    all_rules = raw["rules"]

    # 按 rule_group_code 分组
    groups_map = {}
    for rule in all_rules:
        code = rule.rule_group_code or "UNKNOWN"
        if code not in groups_map:
            groups_map[code] = {
                "rule_group_code": code,
                "rule_group_en": raw.get("rule_group_en", code),
                "rules": [],
            }
        groups_map[code]["rules"].append(rule)

    return list(groups_map.values())


def search_field_usage(excel_path: str, keywords: list) -> list:
    """主入口：搜索字段使用情况。

    对每个规则组跑完整 analyzer 解析（含 enrich 追溯），复用所有解析逻辑。
    field_search 只负责搜索 + 组织输出。

    Args:
        excel_path: Excel 文件路径
        keywords: 关键字列表（如 ["amount", "user_id"]）

    Returns: [FieldUsage, ...] 按目标表分组排序
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from analyzer import (detect_dialect, parse_single_sql, build_topology,
                          build_data_flow, build_field_mappings,
                          enrich_join_key_lineage, enrich_field_physical_sources)

    groups = read_excel_grouped(excel_path)
    all_usages = []
    keywords_lower = [k.lower() for k in keywords]

    for group in groups:
        rules = group["rules"]
        if not rules:
            continue

        # 完整解析（复用 analyzer 全部能力，含 enrich 追溯）
        sqls = [r.query_sql for r in rules if r.query_sql]
        dialect = detect_dialect(sqls)
        parsed_map = {r.rule_code: parse_single_sql(r.query_sql, dialect) for r in rules}
        topology = build_topology(rules, parsed_map)
        data_flow = build_data_flow(rules, parsed_map)
        field_mappings = build_field_mappings(rules, parsed_map, {})
        enrich_join_key_lineage(data_flow, rules, parsed_map, topology, field_mappings)
        enrich_field_physical_sources(field_mappings, data_flow, rules, parsed_map, topology)

        # 搜索字段
        _search_group(rules, parsed_map, field_mappings, keywords_lower, all_usages)

    # 按目标表分组排序
    all_usages.sort(key=lambda u: (u.target_table, u.role, u.field_name))
    return all_usages


def _search_group(rules, parsed_map, field_mappings, keywords_lower, all_usages):
    """搜索单个规则组的字段使用情况。

    每个字段穿透到最终目标表，合并所有步骤的角色（斜杠分隔），一行输出。
    """
    fields_list = field_mappings.get("fields", [])

    # 最终目标表（非中间表的最后一个步骤的 target）
    final_target = ""
    for rule in reversed(rules):
        if not _is_intermediate(rule.target_table):
            final_target = f"{rule.target_schema}.{rule.target_table}" if rule.target_schema else rule.target_table
            break

    # 收集所有匹配字段的使用信息，按字段名（小写）合并
    field_usage_map = {}  # {field_lower: {roles: set, situations: set, source, details: []}}

    for rule in rules:
        parsed = parsed_map.get(rule.rule_code)
        if not parsed or parsed.parse_error:
            continue

        rule_idx = rules.index(rule)
        step_id = f"step_{rule_idx + 1}"
        is_final_step = not _is_intermediate(rule.target_table)

        # 1. SELECT 字段（写入/临时）
        for f in fields_list:
            if f.get("producing_step") != step_id:
                continue
            fname = f.get("target_field", "")
            matched = _match_field(fname, f, keywords_lower)
            if not matched:
                continue

            fl = fname.lower()
            if fl not in field_usage_map:
                field_usage_map[fl] = {"name": fname, "roles": [], "situations": [], "source": "", "details": []}

            entry = field_usage_map[fl]
            transform = f.get("transform_type", "direct")
            source = _get_physical_source(f) if is_final_step else ""

            if is_final_step:
                if "写入目标表" not in entry["roles"]:
                    entry["roles"].append("写入目标表")
                if not entry["source"]:
                    entry["source"] = source
                sit = _situation_label(transform)
                if sit not in entry["situations"]:
                    entry["situations"].append(sit)
                detail = _detail_label(f)
                if detail and detail not in entry["details"]:
                    entry["details"].append(detail)
            else:
                # 中间步骤的字段，只在详情里体现传递路径
                pass

        # 2. 辅助字段（关联键 + 过滤条件）
        seen_aux = set()
        for ju in parsed.join_usage:
            jf = ju.get("field", "")
            if not _match_keyword(jf, keywords_lower):
                continue
            aux_key = (jf.lower(), "关联键")
            if aux_key in seen_aux:
                continue
            seen_aux.add(aux_key)

            fl = jf.lower()
            if fl not in field_usage_map:
                field_usage_map[fl] = {"name": jf, "roles": [], "situations": [], "source": "", "details": []}
            entry = field_usage_map[fl]
            if "关联键" not in entry["roles"]:
                entry["roles"].append("关联键")
            cond = ju.get("on_condition", "")
            if cond and cond not in entry["details"]:
                entry["details"].append(f"关联: {cond}")

        for wu in parsed.where_usage:
            wf = wu.get("field", "")
            if not _match_keyword(wf, keywords_lower):
                continue
            aux_key = (wf.lower(), "过滤条件")
            if aux_key in seen_aux:
                continue
            seen_aux.add(aux_key)

            fl = wf.lower()
            if fl not in field_usage_map:
                field_usage_map[fl] = {"name": wf, "roles": [], "situations": [], "source": "", "details": []}
            entry = field_usage_map[fl]
            if "过滤条件" not in entry["roles"]:
                entry["roles"].append("过滤条件")
            cond = wu.get("condition", "")
            if cond and cond not in entry["details"]:
                entry["details"].append(f"过滤: {cond}")

    # 输出合并后的字段使用记录（目标表 = 最终目标表）
    for fl, entry in field_usage_map.items():
        if not entry["roles"]:
            continue
        all_usages.append(FieldUsage(
            target_table=final_target,
            field_name=entry["name"],
            role="/".join(entry["roles"]),
            situation="/".join(entry["situations"]) if entry["situations"] else "-",
            source=entry["source"] or "-",
            detail="；".join(entry["details"]) if entry["details"] else "-",
        ))


def _get_physical_source(f):
    """从 field 的 physical_source（enrich 注入）取物理源表.字段。"""
    ps_list = f.get("physical_source", [])
    if not ps_list:
        # 回退：用 lineage 第一项
        lineages = f.get("lineage", [])
        if lineages:
            src = lineages[0].get("source_table", "")
            field = lineages[0].get("source_field", "")
            if src and field:
                return f"{src}.{field}"
        return ""
    parts = []
    for ps in ps_list:
        tbl = ps.get("table", "")
        fld = ps.get("field", "")
        if tbl and fld:
            parts.append(f"{tbl}.{fld}")
    return "；".join(parts) if parts else ""


def _is_intermediate(table_name):
    """判断是否中间表（复用 analyzer 逻辑）。"""
    import re
    short = (table_name or "").strip().lower().split(".")[-1]
    return bool(re.search(r"(?:^tmp\d*$|_tmp\d*$|^temp\d*$|_temp\d*$|^tmp_|_tmp_|^temp_|_temp_)", short))


def _match_field(fname, f, keywords_lower):
    """字段名或加工表达式匹配关键字。"""
    if _match_keyword(fname, keywords_lower):
        return True
    for l in f.get("lineage", []):
        if _match_keyword(l.get("source_field", ""), keywords_lower):
            return True
        if _match_keyword(l.get("raw_sql", ""), keywords_lower):
            return True
    return False


def _match_keyword(text, keywords_lower):
    """文本是否包含任一关键字（大小写不敏感）。"""
    if not text:
        return False
    text_lower = text.lower()
    return any(k in text_lower for k in keywords_lower)


def _situation_label(transform):
    """字段情况标签。"""
    tt = transform or "direct"
    labels = {
        "direct": "直取", "value": "赋值", "aggregate": "加工(聚合)",
        "expression": "加工", "case_when": "加工(条件)", "fallback": "加工(兜底)",
        "window": "加工(窗口)", "pivot": "加工(行转列)",
    }
    return labels.get(tt, tt)


def _detail_label(f):
    """详情标签（加工表达式 / 关联信息）。"""
    parts = []
    for l in f.get("lineage", []):
        src_field = l.get("source_field", "")
        transform = l.get("transform", "direct")
        raw = l.get("raw_sql", "")
        if transform != "direct" and raw:
            parts.append(raw)
        elif src_field:
            parts.append(src_field)
    return "；".join(parts) if parts else ""


def output_excel(usages: list, output_path: str) -> bool:
    """输出字段使用情况到 Excel（一个大 sheet）。"""
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment
    except ImportError:
        print("[ERROR] 缺少 openpyxl", file=sys.stderr)
        return False

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "字段使用情况"

    headers = ["目标表", "字段名", "字段角色", "字段情况", "最初来源", "详情"]
    header_fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
    header_font = Font(color="FFFFFF", bold=True, size=11)
    ws.append(headers)
    for col in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=col)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center")

    for u in usages:
        ws.append([u.target_table, u.field_name, u.role, u.situation, u.source, u.detail])

    col_widths = [25, 20, 14, 16, 30, 40]
    for i, w in enumerate(col_widths, 1):
        ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = w

    wb.save(output_path)
    return True


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="字段使用情况批量搜索（支持多个规则组 + 多关键字）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例:
  python run.py field_search --input execution_tasks.xlsx --keyword amount --output field_usage.xlsx
  python run.py field_search --input execution_tasks.xlsx --keyword "amount,user_id" --output field_usage.xlsx
""",
    )
    parser.add_argument("--input", required=True, help="execution_tasks.xlsx 文件路径（含多个规则组）")
    parser.add_argument("--keyword", required=True, help="搜索关键字，多个用逗号分隔（如 amount,user_id）")
    parser.add_argument("--output", required=True, help="输出 Excel 路径")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"错误: 文件不存在: {input_path}", file=sys.stderr)
        sys.exit(1)

    keywords = [k.strip() for k in args.keyword.split(",") if k.strip()]
    if not keywords:
        print("错误: 至少提供一个关键字", file=sys.stderr)
        sys.exit(1)

    print(f"=== 字段使用情况搜索 ===")
    print(f"输入: {input_path}")
    print(f"关键字: {keywords}")
    print()

    print("Step 1: 解析 Excel（多规则组）...")
    usages = search_field_usage(str(input_path), keywords)
    print(f"  匹配到 {len(usages)} 条字段使用记录")

    print(f"\nStep 2: 输出 Excel...")
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    ok = output_excel(usages, str(output_path))
    if ok:
        print(f"  [OK] {output_path}")
        print(f"\n=== 完成 ===")
    else:
        print(f"\n=== 失败 ===", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
