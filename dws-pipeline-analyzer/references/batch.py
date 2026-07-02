"""批量分析生成交付件工具。

对多个规则组批量执行 analyzer + view_generator，生成交付件。
支持分批处理（每批默认 50 个规则组）和 AI 增强可选。

使用:
    python run.py batch --input execution_tasks.xlsx --output docs/
    python run.py batch --input execution_tasks.xlsx --output docs/ --batch-size 30 --no-ai
"""

import sys
import os
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

    内存策略：
        每个【批次】在独立子进程里执行。子进程跑完退出，进程 RSS 立刻归还系统。
        这是为了解决"单进程跑数百个规则组时 RSS 持续膨胀导致 MemoryError"——
        Python/C 分配器对大量临时大对象（解析 AST、knowledge dict）有不归还行为，
        函数内 del/gc 无效（对象本就释放了，是内存页不还给 OS）。
        子进程隔离是治本方案，且 subprocess 在 Windows/macOS 行为一致。
        子进程失败时自动降级为进程内执行（兼容小批量/测试场景）。
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from analyzer import read_excel

    # 读取并按规则组分组（主进程只读一次，大 workbook 常驻可接受，不是累积源）
    raw = read_excel(excel_path)
    all_rules = raw["rules"]
    global_group_en = (raw.get("rule_group_en") or "").strip()

    # 按规则组编码分组
    # 每个组的目录名取【该组】第一条非空 rule_group_en（每行存了），而非全局值，
    # 否则多个 code 不同的组会撞同一个英文名 → 全写进同一目录互相覆盖。
    groups_map = {}
    unknown_idx = 0
    for rule in all_rules:
        code = rule.rule_group_code
        if not code:
            code = f"_SOLO_{rule.rule_code or f'ROW{unknown_idx}'}"
            unknown_idx += 1
        if code not in groups_map:
            # 优先用本组的英文名；本组没填再兜底全局；最后兜底 code
            en = (rule.rule_group_en or "").strip() or global_group_en or code
            groups_map[code] = {
                "rule_group_code": code,
                "rule_group_en": en,
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

    # 分批处理 —— 每批开子进程执行（子进程退出即归还 RSS）
    for batch_start in range(0, total, batch_size):
        batch_end = min(batch_start + batch_size, total)
        batch_num = batch_start // batch_size + 1

        print(f"--- 批次 {batch_num}（{batch_start+1}-{batch_end}/{total}）---")

        batch_results = _run_batch_in_subprocess(
            excel_path, output_dir, no_ai, ddl_dir,
            batch_start, batch_end)
        results.extend(batch_results)

        for r in batch_results:
            status = "[OK]" if r.success else "[FAIL]"
            print(f"  {status} {r.rule_group_en} ({r.target_table})")

        print()

    # 汇总
    success_count = sum(1 for r in results if r.success)
    print(f"=== 完成: {success_count}/{total} 成功 ===")
    return results


def _run_batch_in_subprocess(excel_path, output_dir, no_ai, ddl_dir,
                             batch_start, batch_end):
    """在子进程里执行一批规则组，返回 BatchResult 列表。

    子进程通过命令行参数接收批次范围，逐组调用 _process_group，
    把结果摘要写入临时 JSON，主进程读取后回收子进程（RSS 归还）。
    子进程异常时降级为进程内执行（保证小批量/测试可用）。
    """
    import json
    import subprocess
    import tempfile

    # 用本脚本自身作为子进程入口，通过环境变量传递批次参数（避免命令行长度/转义问题）
    script = str(Path(__file__).resolve())
    result_file = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, prefix="batch_result_")
    result_path = result_file.name
    result_file.close()

    env = dict(os.environ)
    env["DWS_BATCH_MODE"] = "child"
    env["DWS_BATCH_INPUT"] = str(excel_path)
    env["DWS_BATCH_OUTPUT"] = str(output_dir)
    env["DWS_BATCH_NO_AI"] = "1" if no_ai else "0"
    env["DWS_BATCH_DDL_DIR"] = str(ddl_dir or "")
    env["DWS_BATCH_START"] = str(batch_start)
    env["DWS_BATCH_END"] = str(batch_end)
    env["DWS_BATCH_RESULT"] = result_path

    try:
        proc = subprocess.run(
            [sys.executable, script], env=env,
            capture_output=False, text=True)
        ok = (proc.returncode == 0)
    except Exception:
        # 子进程启动失败（极端环境）→ 降级进程内执行
        ok = False

    if ok and Path(result_path).exists():
        try:
            with open(result_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return [BatchResult(**item) for item in data]
        except Exception:
            ok = False

    # 降级：子进程不可用或结果读取失败，回退到进程内执行（保证功能可用）
    return _run_batch_inprocess(excel_path, output_dir, no_ai, ddl_dir,
                                batch_start, batch_end)


def _run_batch_inprocess(excel_path, output_dir, no_ai, ddl_dir,
                         batch_start, batch_end):
    """进程内执行一批（降级路径 + 测试入口）。"""
    from analyzer import read_excel

    raw = read_excel(excel_path)
    all_rules = raw["rules"]
    global_group_en = (raw.get("rule_group_en") or "").strip()

    groups_map = {}
    unknown_idx = 0
    for rule in all_rules:
        code = rule.rule_group_code
        if not code:
            code = f"_SOLO_{rule.rule_code or f'ROW{unknown_idx}'}"
            unknown_idx += 1
        if code not in groups_map:
            en = (rule.rule_group_en or "").strip() or global_group_en or code
            groups_map[code] = {
                "rule_group_code": code,
                "rule_group_en": en,
                "rules": [],
            }
        groups_map[code]["rules"].append(rule)

    groups = list(groups_map.values())
    batch_groups = groups[batch_start:batch_end]
    return [_process_group(g, output_dir, raw, no_ai, ddl_dir)
            for g in batch_groups]


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
            # 这里生成 summary 供 AI 读取（注意：定义有 7 个参数，含 data_flow）
            from analyzer import _generate_ai_summary
            summary_text = _generate_ai_summary(knowledge, rules, parsed_map, topology,
                                                 field_mappings, quality, data_flow)
            (out_dir / "knowledge_summary.md").write_text(summary_text, encoding="utf-8", newline="\n")

        result.success = True
    except Exception as e:
        result.error = f"{type(e).__name__}: {e}"
        print(f"  [ERROR] {group_en}: {result.error}", file=sys.stderr)

    return result


def _run_child_batch():
    """子进程入口：从环境变量读批次参数，执行后写结果 JSON。

    由 _run_batch_in_subprocess 通过 DWS_BATCH_MODE=child 触发。
    子进程退出即归还 RSS，是批量内存隔离的核心。
    """
    import json
    from dataclasses import asdict

    excel_path = os.environ["DWS_BATCH_INPUT"]
    output_dir = os.environ["DWS_BATCH_OUTPUT"]
    no_ai = os.environ.get("DWS_BATCH_NO_AI") == "1"
    ddl_dir = os.environ.get("DWS_BATCH_DDL_DIR", "")
    batch_start = int(os.environ["DWS_BATCH_START"])
    batch_end = int(os.environ["DWS_BATCH_END"])
    result_path = os.environ["DWS_BATCH_RESULT"]

    results = _run_batch_inprocess(
        excel_path, output_dir, no_ai, ddl_dir, batch_start, batch_end)

    with open(result_path, "w", encoding="utf-8") as f:
        json.dump([asdict(r) for r in results], f, ensure_ascii=False)


def main():
    # 子进程模式：由 run_batch 通过环境变量触发，执行单批后退出（RSS 归还）
    if os.environ.get("DWS_BATCH_MODE") == "child":
        try:
            _run_child_batch()
            sys.exit(0)
        except Exception as e:
            print(f"  [子进程错误] {type(e).__name__}: {e}", file=sys.stderr)
            sys.exit(1)

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
