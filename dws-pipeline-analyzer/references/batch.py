"""批量分析生成交付件工具。

对多个规则组批量执行 analyzer + view_generator，生成交付件。
支持分批处理（每批默认 50 个规则组）和 AI 增强可选。

使用:
    python run.py batch --input execution_tasks.xlsx --output docs/
    python run.py batch --input execution_tasks.xlsx --output docs/ --batch-size 30 --no-ai
"""

import sys
import time
from pathlib import Path
from dataclasses import dataclass


@dataclass
class BatchResult:
    """单个规则组的批量处理结果。"""
    rule_group_code: str = ""
    rule_group_en: str = ""
    target_table: str = ""
    success: bool = False
    output_dir: str = ""
    error: str = ""
    has_ai: bool = False


def run_batch(excel_path: str, output_dir: str, batch_size: int = 50,
              no_ai: bool = False, ddl_dir: str = "") -> list:
    """批量分析多个规则组，生成交付件。

    Args:
        excel_path: Excel 文件路径（含多个规则组）
        output_dir: 输出基础目录（每个规则组在其下建子目录）
        batch_size: 每批处理的规则组数量（默认 50）
        no_ai: 是否跳过 AI 增强
        ddl_dir: DDL 文件目录（可选）

    Returns: [BatchResult, ...]
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from analyzer import (read_excel, detect_dialect, parse_single_sql,
                          build_topology, build_data_flow, build_field_mappings,
                          analyze_quality, detect_patterns, build_source,
                          enrich_join_key_lineage, enrich_field_physical_sources,
                          _is_intermediate_table)
    from view_generator import (build_report_data, generate_mapping,
                                generate_asset_report, generate_tech_design)
    from datetime import datetime

    # 读取并按规则组分组
    raw = read_excel(excel_path)
    all_rules = raw["rules"]

    # 按规则组编码分组（复用 field_search 的分组逻辑）
    groups_map = {}
    unknown_idx = 0
    for rule in all_rules:
        code = rule.rule_group_code
        if not code:
            code = f"_SOLO_{rule.rule_code or f'ROW{unknown_idx}'}"
            unknown_idx += 1
        if code not in groups_map:
            groups_map[code] = {
                "rule_group_code": code,
                "rule_group_en": code,
                "rules": [],
            }
        groups_map[code]["rules"].append(rule)

    groups = list(groups_map.values())
    total = len(groups)
    results = []

    print(f"=== 批量分析 ===")
    print(f"输入: {excel_path}")
    print(f"规则组数: {total}")
    print(f"批量大小: {batch_size}")
    print(f"AI 增强: {'跳过' if no_ai else '启用'}")
    print(f"输出目录: {output_dir}")
    print()

    # 分批处理
    for batch_start in range(0, total, batch_size):
        batch_end = min(batch_start + batch_size, total)
        batch_num = batch_start // batch_size + 1
        batch_groups = groups[batch_start:batch_end]

        print(f"--- 批次 {batch_num}（{batch_start+1}-{batch_end}/{total}）---")

        for group in batch_groups:
            result = _process_group(group, output_dir, raw, no_ai, ddl_dir)
            results.append(result)
            status = "[OK]" if result.success else "[FAIL]"
            print(f"  {status} {result.rule_group_en} ({result.target_table})")

        print()

    # 汇总
    success_count = sum(1 for r in results if r.success)
    print(f"=== 完成: {success_count}/{total} 成功 ===")
    return results


def _process_group(group, output_dir, raw, no_ai, ddl_dir):
    """处理单个规则组：解析 + 生成交付件。"""
    from datetime import datetime
    from analyzer import (detect_dialect, parse_single_sql,
                          build_topology, build_data_flow, build_field_mappings,
                          analyze_quality, detect_patterns, build_source,
                          enrich_join_key_lineage, enrich_field_physical_sources,
                          _is_intermediate_table)
    from view_generator import (build_report_data, generate_mapping,
                                generate_asset_report, generate_tech_design)

    rules = group["rules"]
    code = group["rule_group_code"]
    group_en = group["rule_group_en"]

    result = BatchResult(
        rule_group_code=code,
        rule_group_en=group_en,
    )

    try:
        # 目标表（取最后一个非中间表）
        for rule in reversed(rules):
            if not _is_intermediate_table(rule.target_table):
                result.target_table = f"{rule.target_schema}.{rule.target_table}" if rule.target_schema else rule.target_table
                break
        if not result.target_table and rules:
            result.target_table = f"{rules[-1].target_schema}.{rules[-1].target_table}"

        # 输出目录：基础目录 / 规则组英文名
        import re
        safe_name = re.sub(r'[<>:"/\\|?*\s]', "_", group_en)
        out_dir = Path(output_dir) / safe_name
        out_dir.mkdir(parents=True, exist_ok=True)
        result.output_dir = str(out_dir)

        # 解析（复用 analyzer 全流程）
        sqls = [r.query_sql for r in rules if r.query_sql]
        dialect = detect_dialect(sqls)
        parsed_map = {r.rule_code: parse_single_sql(r.query_sql, dialect) for r in rules}
        topology = build_topology(rules, parsed_map)
        data_flow = build_data_flow(rules, parsed_map)
        field_mappings = build_field_mappings(rules, parsed_map, {})
        enrich_join_key_lineage(data_flow, rules, parsed_map, topology, field_mappings)
        enrich_field_physical_sources(field_mappings, data_flow, rules, parsed_map, topology)
        quality = analyze_quality(topology, data_flow, field_mappings, parsed_map)
        patterns = detect_patterns(parsed_map, topology)

        # 构造 knowledge
        knowledge = {
            "meta": {
                "source_type": "execution_tasks.xlsx",
                "analysis_time": datetime.now().isoformat(),
                "dialect": dialect, "total_rules": len(rules),
                "target_table": result.target_table.split(".")[-1],
                "patterns": patterns,
                "target_field_types": {}, "target_field_comments": {},
            },
            "topology": topology, "data_flow": data_flow,
            "field_mappings": field_mappings, "quality": quality,
            "business_logic": {"summary": "", "step_descriptions": [], "key_transforms": []},
            "source": build_source(rules, {}, {}, parsed_map),
        }

        # 生成 knowledge_draft.json + summary
        import json
        (out_dir / "knowledge_draft.json").write_text(
            json.dumps(knowledge, ensure_ascii=False, indent=2),
            encoding="utf-8", newline="\n")

        # 生成交付件
        generate_mapping(knowledge, str(out_dir))
        generate_asset_report(knowledge, str(out_dir))
        generate_tech_design(knowledge, str(out_dir))

        # AI 增强（可选）
        if not no_ai:
            result.has_ai = True
            # 批量场景下只标记，实际 AI 增强由 opencode 命令流程处理
            # 这里生成 summary 供 AI 读取
            from analyzer import _generate_ai_summary
            summary_text = _generate_ai_summary(knowledge, rules, parsed_map, topology,
                                                 field_mappings, quality)
            (out_dir / "knowledge_summary.md").write_text(summary_text, encoding="utf-8", newline="\n")

        result.success = True
    except Exception as e:
        result.error = f"{type(e).__name__}: {e}"
        print(f"  [ERROR] {group_en}: {result.error}", file=sys.stderr)

    return result


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="批量分析生成交付件（支持多个规则组 + 分批处理）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例:
  python run.py batch --input execution_tasks.xlsx --output docs/
  python run.py batch --input execution_tasks.xlsx --output docs/ --batch-size 30
  python run.py batch --input execution_tasks.xlsx --output docs/ --no-ai
""",
    )
    parser.add_argument("--input", required=True, help="execution_tasks.xlsx 文件路径（含多个规则组）")
    parser.add_argument("--output", required=True, help="输出基础目录")
    parser.add_argument("--batch-size", type=int, default=50, help="每批处理的规则组数量（默认 50）")
    parser.add_argument("--no-ai", action="store_true", help="跳过 AI 增强（只生成脚本产物）")
    parser.add_argument("--ddl-dir", default="", help="DDL 文件目录（可选）")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"错误: 文件不存在: {input_path}", file=sys.stderr)
        sys.exit(1)

    run_batch(str(input_path), args.output, args.batch_size, args.no_ai, args.ddl_dir)


if __name__ == "__main__":
    main()
