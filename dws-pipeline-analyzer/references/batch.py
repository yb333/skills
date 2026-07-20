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


def run_batch(excel_path: str, output_dir: str, batch_size: int = 20,
              no_ai: bool = False, ddl_dir: str = "") -> list:
    """批量分析多个规则组，生成交付件。

    Args:
        excel_path: Excel 文件路径（含多个规则组）
        output_dir: 输出基础目录（每个规则组在其下建子目录）
        batch_size: 每批处理的规则组数量（默认 20）
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

    输出策略（配合内存隔离）：
        子进程 stdout/stderr 始终重定向到「每批日志文件」而非继承主进程 stdout；
        逐组详细状态也写入日志文件。stdout 只保留批次级进度 + 最终汇总（输出量
        与规则组数无关，为常数级）。这是为根治"逐组 print 累积超出上游捕获管道
        上限（如 AI 工具捕获 stdout）导致整个进程树被 SIGKILL"——典型表现为
        「前两批正常、第三批起步即被杀」。
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from analyzer import read_excel

    # 输出目录与日志目录创建：路径无效/权限不足/磁盘满时给出清晰错误，
    # 而非让后续子进程静默失败（表现为「连 batch_logs 都没产出」）。
    try:
        Path(output_dir).mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"错误: 无法创建输出目录 {output_dir}: {e}", file=sys.stderr)
        raise

    # 日志目录【始终创建】：逐组详细输出永远写文件，绝不进 stdout。
    # 早期 bug：曾用 verbose 开关控制日志是否落盘，verbose=True 时所有逐组输出
    # 全进 stdout，规则组多时累积超出上游捕获管道上限（如 AI 工具捕获 stdout），
    # 整个进程树被 SIGKILL —— 用户为「看到更多输出」加 --verbose，反而被杀。
    log_dir = Path(output_dir) / "batch_logs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        # 日志目录建不了：stdout 仍只留批次级进度（绝不能把逐组输出退回 stdout，
        # 否则又回到累积被杀的老路）。记录警告，log_dir 置 None 让下游跳过写日志。
        print(f"警告: 无法创建日志目录 {log_dir}: {e}（逐组详细日志将无法落盘）", file=sys.stderr)
        log_dir = None

    # 主进程只读一次 Excel。读完后只保留分组结果（rules 已在 groups 内），
    # 释放 raw 里的 target_fields/group_variables 等大对象，降低主进程常驻内存。
    raw = read_excel(excel_path)
    all_rules = raw["rules"]
    global_group_en = (raw.get("rule_group_en") or "").strip()

    # 按规则组编码分组，并处理同名（英文名相同）不同码的规则组（实时/离线区等）
    groups = _group_rules_by_code(all_rules, global_group_en)
    total = len(groups)
    results = []

    # 主进程不再需要 raw 的大对象（target_fields/group_variables 等），主动释放。
    # 注意：_process_group 内部会按需自己读 Excel 取这些数据，主进程不必持有。
    del raw

    print(f"=== 批量分析 ===")
    print(f"输入: {excel_path}")
    print(f"规则组数: {total}")
    print(f"批量大小: {batch_size}")
    print(f"AI 增强: {'跳过' if no_ai else '启用'}")
    print(f"输出目录: {output_dir}")
    if log_dir:
        print(f"详细日志: {log_dir}/batch_*.log（stdout 只保留批次级进度，查细节看日志）")
    print()

    # 分批处理 —— 每批开子进程执行（子进程退出即归还 RSS）
    for batch_start in range(0, total, batch_size):
        batch_end = min(batch_start + batch_size, total)
        batch_num = batch_start // batch_size + 1

        print(f"--- 批次 {batch_num}（{batch_start+1}-{batch_end}/{total}）开始 ---")

        batch_results = _run_batch_in_subprocess(
            excel_path, output_dir, no_ai, ddl_dir,
            batch_start, batch_end, log_dir, batch_num)
        results.extend(batch_results)

        # 逐组详细状态：始终写日志文件。stdout 绝不打印逐组内容（与规则组数成正比，
        # 大批量时会累积超出捕获管道上限导致被杀），只在有失败时打一行简要汇总。
        _log_group_results(batch_results, batch_num, log_dir)

        fail_count = sum(1 for r in batch_results if not r.success)
        print(f"--- 批次 {batch_num} 完成：{len(batch_results)} 组"
              f"（成功 {len(batch_results)-fail_count}，失败 {fail_count}）---")
        if fail_count and log_dir:
            print(f"    ⚠ {fail_count} 组失败，详见: {log_dir}/batch_{batch_num}.log")
        print()

    # 汇总
    success_count = sum(1 for r in results if r.success)
    print(f"=== 完成: {success_count}/{total} 成功 ===")
    if log_dir:
        fails = [r for r in results if not r.success]
        if fails:
            print(f"失败 {len(fails)} 组，详见: {log_dir}/batch_*.log")
    return results


def _log_group_results(batch_results, batch_num, log_dir):
    """逐组详细状态写入日志文件。

    铁律：逐组详细内容（与规则组数成正比）一律只写日志，绝不进 stdout。
    stdout 只允许出现与规则组数无关的常数级简要信息（批次进度、失败计数）。
    """
    lines = []
    for r in batch_results:
        status = "[OK]" if r.success else "[FAIL]"
        line = f"  {status} {r.rule_group_en} ({r.target_table})"
        lines.append(line)
        if not r.success and r.error:
            lines.append(f"        ↳ {r.error}")

    # 追加到批次日志（与子进程日志同文件，便于一次性排查整批）
    if log_dir is not None:
        log_path = log_dir / f"batch_{batch_num}.log"
        with open(log_path, "a", encoding="utf-8") as f:
            f.write("=== 逐组结果 ===\n")
            f.write("\n".join(lines) + "\n")


def _group_rules_by_code(all_rules, global_group_en):
    """按 rule_group_code 分组，并处理同名（英文名相同）不同码的规则组。

    历史 bug：输出目录名只用 rule_group_en（英文名），当两个 code 不同但英文名
    相同的规则组（典型：实时区/离线区同名表）同时存在时，两者写进同一目录互相
    覆盖（generate_* 均覆盖写），后跑的组把先跑的组完整覆盖，造成交付件丢失。

    解法：分组完成后扫描英文名冲突，对冲突的组在其目录名后追加 code 去重。
    目录名策略（_process_group 按 group['dir_name'] 取用）：
        - 无冲突：保持英文原名（向后兼容）
        - 有冲突：{英文名}__{code}
    """
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

    # 检测英文名冲突 → 冲突组目录名追加 code
    from collections import Counter
    name_counts = Counter(g["rule_group_en"] for g in groups)
    for g in groups:
        en = g["rule_group_en"]
        if name_counts[en] > 1:
            # 英文名相同但 code 不同（实时区/离线区等）→ 追加 code 防止目录互相覆盖
            g["dir_name"] = f"{en}__{g['rule_group_code']}"
        else:
            g["dir_name"] = en

    return groups


def _run_batch_in_subprocess(excel_path, output_dir, no_ai, ddl_dir,
                             batch_start, batch_end, log_dir=None, batch_num=None):
    """在子进程里执行一批规则组，返回 BatchResult 列表。

    子进程通过命令行参数接收批次范围，逐组调用 _process_group，
    把结果摘要写入临时 JSON，主进程读取后回收子进程（RSS 归还）。
    子进程异常时降级为进程内执行（保证小批量/测试可用）。

    输出隔离（关键）：
        子进程 stdout/stderr 重定向到「每批日志文件」，不再继承主进程 stdout。
        否则子进程里 generate_*/_process_group 的逐组 print 会汇入主进程 stdout，
        规则组数多时累积超出上游捕获管道上限（如 AI 工具捕获 stdout 的缓冲区），
        整个进程树会被 SIGKILL —— 表现为「前两批正常、第三批起步即被杀」。
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

    # 子进程 stdout/stderr 重定向到每批日志文件，根治 stdout 累积
    child_stdout = None
    child_stderr = None
    log_path = None
    if log_dir is not None and batch_num is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"batch_{batch_num}.log"
        # 覆盖写：每批日志独立。子进程详细输出（[OK] 行、Step 进度、错误）都进来
        child_stdout = open(log_path, "w", encoding="utf-8")
        child_stderr = subprocess.STDOUT  # 合并 stderr 进同一日志

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
            stdout=child_stdout, stderr=child_stderr, text=True)
        ok = (proc.returncode == 0)
    except Exception:
        # 子进程启动失败（极端环境）→ 降级进程内执行
        ok = False
    finally:
        if child_stdout is not None:
            child_stdout.close()

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

    groups = _group_rules_by_code(all_rules, global_group_en)
    batch_groups = groups[batch_start:batch_end]
    # 逐组隔离：单个组处理异常绝不拖垮同批其它组。
    # 历史 bug：用列表推导 [_process_group(g,...) for g in batch_groups]，
    # 一旦某个组抛出 _process_group 内 try 未覆盖的异常（openpyxl
    # IllegalCharacterError、json 序列化、OOM 被杀等），整个列表推导式中断，
    # 该组之后的所有组都不会被处理 → 表现为「一批里有的组正常、有的直接没有文件夹」。
    results = []
    for g in batch_groups:
        try:
            results.append(_process_group(g, output_dir, raw, no_ai, ddl_dir))
        except Exception as e:
            # 兜底：_process_group 理论上不该抛（内部有 try），但极端异常
            # （如 openpyxl 写 Excel、子进程信号）可能穿透。这里保证不中断，
            # 且失败组也带 output_dir（便于定位/重跑）。
            import re
            code = g.get("rule_group_code", "")
            dir_name = g.get("dir_name") or g.get("rule_group_en", "") or code or "unknown"
            safe_name = re.sub(r'[<>:"/\\|?*\s]', "_", dir_name).strip("_") or code or "unknown"
            results.append(BatchResult(
                rule_group_code=code,
                rule_group_en=g.get("rule_group_en", ""),
                output_dir=str(Path(output_dir) / safe_name),
                error=f"{type(e).__name__}: {e}",
            ))
    return results


def _process_group(group, output_dir, raw, no_ai, ddl_dir):
    """处理单个规则组：解析 + 生成交付件。"""
    from analyzer import (detect_dialect, analyze_pipeline,
                          _generate_ai_summary, _is_intermediate_table)
    from view_generator import (generate_mapping, generate_asset_report,
                                generate_tech_design)

    rules = group["rules"]
    code = group["rule_group_code"]
    group_en = group["rule_group_en"]

    # 安全目录名：优先 dir_name（经重名去重处理），清洗非法字符；
    # 清洗后为空（英文名全是特殊字符或缺失）时回退 code，绝不落到空目录名
    # （空目录名会让 out_dir=output_dir 本身，多组写进根目录互相覆盖）。
    import re
    dir_name = group.get("dir_name") or group_en or code or "unknown"
    safe_name = re.sub(r'[<>:"/\\|?*\s]', "_", dir_name).strip("_") or code or "unknown"
    out_dir = Path(output_dir) / safe_name

    result = BatchResult(
        rule_group_code=code,
        rule_group_en=group_en,
        output_dir=str(out_dir),  # 提前赋值，失败组也可定位/追踪
    )

    try:
        # 目标表（取最后一个非中间表，仅用于日志/结果展示；
        # knowledge.meta.target_table 由 analyze_pipeline 内部按 max(exec_sequence) 算）
        for rule in reversed(rules):
            if not _is_intermediate_table(rule.target_table):
                result.target_table = f"{rule.target_schema}.{rule.target_table}" if rule.target_schema else rule.target_table
                break
        if not result.target_table and rules:
            result.target_table = f"{rules[-1].target_schema}.{rules[-1].target_table}"

        # 输出目录创建（output_dir 已在 try 外预赋值）
        out_dir.mkdir(parents=True, exist_ok=True)

        # 核心解析（与单条路径共用 analyze_pipeline，单一真相，杜绝两套逻辑漂移）。
        # 历史 bug：批量曾独立复制 Step3-7，单条路径新增的 data_blocks、
        # structured_summary、auto_step_desc、DDL 元数据等都没同步，导致批量产出缺数据块。
        target_fields = raw.get("target_fields", {}) or {}
        group_variables = raw.get("group_variables", {}) or {}
        sqls = [r.query_sql for r in rules if r.query_sql]
        dialect = detect_dialect(sqls)
        knowledge, parsed_map = analyze_pipeline(
            rules, target_fields, group_variables, dialect,
            ddl_dir=ddl_dir, source_file="", rule_group_code=code,
        )

        # 生成 knowledge_draft.json
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
            # 批量场景下只标记，实际 AI 增强由命令流程处理；
            # 这里生成 summary 供 AI 读取（_generate_ai_summary 定义有 7 个参数，含 data_flow）
            topology = knowledge["topology"]
            data_flow = knowledge["data_flow"]
            field_mappings = knowledge["field_mappings"]
            quality = knowledge["quality"]
            summary_text = _generate_ai_summary(knowledge, rules, parsed_map, topology,
                                                 field_mappings, quality, data_flow)
            (out_dir / "knowledge_summary.md").write_text(summary_text, encoding="utf-8", newline="\n")

        result.success = True
    except Exception as e:
        result.error = f"{type(e).__name__}: {e}"
        # output_dir 已在对象初始化时预赋值，失败组也能定位目录（便于排查/重跑）。
        # 详细错误经 _log_group_results 写入批次日志；这里不再 print，避免 stdout 累积。
    finally:
        # 每组分析完清 SQL AST 缓存，防批量场景内存持续增长
        try:
            from engine import clear_sql_ast_cache
            clear_sql_ast_cache()
        except Exception:
            pass
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
    parser.add_argument("--batch-size", type=int, default=20,
                        help="每批处理的规则组数量（默认 20）。单批内解析 AST+knowledge "
                             "随组数累积，复杂 SQL（多层 CTE/UNION/窗口）单组占内存大，"
                             "50 组易触发单进程内存超限。20 为实测安全值。")
    parser.add_argument("--no-ai", action="store_true", help="跳过 AI 增强（只生成脚本产物）")
    parser.add_argument("--ddl-dir", default="", help="DDL 文件目录（可选）")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"错误: 文件不存在: {input_path}", file=sys.stderr)
        sys.exit(1)

    run_batch(str(input_path), args.output, args.batch_size, args.no_ai,
              args.ddl_dir)


if __name__ == "__main__":
    main()
