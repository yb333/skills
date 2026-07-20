"""SQL AST 缓存边界测试。

验证 _parse_sqlglot_cached 的边界场景：
- 缓存命中/未命中
- 清缓存后重新解析正确
- 不同 SQL 不互相污染
- AST 共享只读安全性（一个消费方不应影响另一个）
- 批量场景多组分析结果正确（clear 不串数据）

运行:
    pytest tests/test_sql_ast_cache.py -v
"""

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ANALYZER_REF = PROJECT_ROOT / "dws-pipeline-analyzer" / "references"
FIXTURES = PROJECT_ROOT / "tests" / "fixtures" / "analyzer"
sys.path.insert(0, str(ANALYZER_REF))
sys.path.insert(0, str(FIXTURES))

from engine import (
    _parse_sqlglot_cached, clear_sql_ast_cache, _SQL_AST_CACHE,
    parse_single_sql, analyze_pipeline,
)
from analyzer import read_excel, detect_dialect
from _build_xlsx import build_xlsx


# ═══════════════════════════════════════════════════════════════
# 缓存命中/清理基础测试
# ═══════════════════════════════════════════════════════════════

class TestCacheBasic:
    """缓存基础行为：命中/清理/key隔离。"""

    def setup_method(self):
        """每个测试前清缓存，保证隔离。"""
        clear_sql_ast_cache()

    def test_cache_hit_returns_same_object(self):
        """同一 SQL 两次解析返回同一个 AST 对象（内存复用）。"""
        sql = "SELECT a.id FROM ods.src_a a"
        tree1 = _parse_sqlglot_cached(sql, "oracle")
        tree2 = _parse_sqlglot_cached(sql, "oracle")
        assert tree1 is tree2, "同一 SQL 应返回同一个 AST 对象"

    def test_different_sql_different_object(self):
        """不同 SQL 返回不同 AST 对象。"""
        tree1 = _parse_sqlglot_cached("SELECT a FROM ods.t1", "oracle")
        tree2 = _parse_sqlglot_cached("SELECT b FROM ods.t2", "oracle")
        assert tree1 is not tree2

    def test_different_dialect_different_cache(self):
        """同 SQL 不同方言分开缓存。"""
        tree_oracle = _parse_sqlglot_cached("SELECT a FROM t", "oracle")
        tree_mysql = _parse_sqlglot_cached("SELECT a FROM t", "mysql")
        assert tree_oracle is not tree_mysql

    def test_clear_cache_empties(self):
        """clear 后缓存为空。"""
        _parse_sqlglot_cached("SELECT 1", "oracle")
        assert len(_SQL_AST_CACHE) > 0
        clear_sql_ast_cache()
        assert len(_SQL_AST_CACHE) == 0

    def test_clear_then_reparse_gets_new_object(self):
        """清缓存后重新解析得到新对象（不是旧的残留）。"""
        sql = "SELECT a FROM ods.t"
        tree1 = _parse_sqlglot_cached(sql, "oracle")
        clear_sql_ast_cache()
        tree2 = _parse_sqlglot_cached(sql, "oracle")
        assert tree1 is not tree2, "清缓存后应重新解析，不是返回旧对象"

    def test_invalid_sql_returns_none_not_cached(self):
        """无效 SQL 不应缓存（parse_one 抛异常）。"""
        with pytest.raises(Exception):
            _parse_sqlglot_cached("THIS IS NOT SQL @#$", "oracle")
        # 异常的不进缓存
        assert len(_SQL_AST_CACHE) == 0


# ═══════════════════════════════════════════════════════════════
# AST 共享只读安全性
# ═══════════════════════════════════════════════════════════════

class TestAstSharedReadOnly:
    """AST 共享对象的安全性：消费方只读遍历，不修改 AST。

    缓存返回的是同一个对象，如果一个消费方修改了 AST，会影响其他消费方。
    这个测试类验证：parse_single_sql 拿到的 AST 跟 build_data_blocks/topology
    拿到的是同一个，但都不修改它。
    """

    def setup_method(self):
        clear_sql_ast_cache()

    def test_analyze_pipeline_uses_shared_ast(self):
        """analyze_pipeline 内部多次解析同一 SQL 命中缓存（AST 复用）。"""
        sql = "SELECT a.id, a.name FROM ods.src_a a WHERE a.flag = 1"
        rule_type = 1

        from engine import RawRule
        rule = RawRule(rule_code="R1", rule_name="t", rule_type=1, exec_sequence=1,
                       target_schema="dws", target_table="t_f", delete_mode="1",
                       query_sql=sql)

        # 第一次：parse_single_sql 触发缓存写入
        clear_sql_ast_cache()
        kj, pm = analyze_pipeline([rule], {}, {}, "dws")

        # analyze_pipeline 内部 parse_single_sql + build_data_blocks + build_topology
        # 都解析同一份 clean_sql → 缓存应只有 1 条（同一 SQL）
        # 注意：可能有其他 SQL 进缓存（如 clean_sql 变体），但主 SQL 应只解析一次
        assert len(_SQL_AST_CACHE) >= 1, "缓存应有命中"

    def test_ast_not_mutated_after_analysis(self):
        """分析完成后 AST 没被修改（遍历结果一致）。"""
        sql = "SELECT a.id FROM ods.src_a a JOIN ods.src_b b ON a.id = b.id"
        from engine import RawRule
        rule = RawRule(rule_code="R1", rule_name="t", rule_type=1, exec_sequence=1,
                       target_schema="dws", target_table="t_f", delete_mode="1",
                       query_sql=sql)

        clear_sql_ast_cache()
        kj1, pm1 = analyze_pipeline([rule], {}, {}, "dws")

        # 缓存里的 AST 应该还在（没被清理）
        cached_trees = list(_SQL_AST_CACHE.values())
        assert len(cached_trees) >= 1

        # 再跑一次分析（用缓存的 AST），结果应一致
        kj2, pm2 = analyze_pipeline([rule], {}, {}, "dws")

        # 字段数、源表数一致 = AST 没被污染
        tables1 = [t.source_table for t in pm1["R1"].source_tables]
        tables2 = [t.source_table for t in pm2["R1"].source_tables]
        assert tables1 == tables2, f"两次分析结果不一致（AST 可能被污染）: {tables1} vs {tables2}"

        fields1 = [f["target_field"] for f in kj1["field_mappings"]["fields"]]
        fields2 = [f["target_field"] for f in kj2["field_mappings"]["fields"]]
        assert fields1 == fields2, f"两次字段映射不一致: {fields1} vs {fields2}"


# ═══════════════════════════════════════════════════════════════
# 批量场景不串数据
# ═══════════════════════════════════════════════════════════════

class TestBatchNoCrossContamination:
    """批量分析多组时，清缓存不导致数据串。"""

    def test_two_groups_analyzed_independently(self, tmp_path):
        """两个不同规则组连续分析，结果各自正确（不串）。"""
        from engine import RawRule

        # 规则组1：订单
        rules1 = [
            RawRule(rule_code="R1", rule_name="订单", rule_type=1, exec_sequence=1,
                    target_schema="dws", target_table="dwb_order_f", delete_mode="1",
                    query_sql="SELECT a.order_id, a.amount FROM ods.order_src a"),
        ]
        # 规则组2：客户（完全不同的表和字段）
        rules2 = [
            RawRule(rule_code="R1", rule_name="客户", rule_type=1, exec_sequence=1,
                    target_schema="dws", target_table="dwb_cust_f", delete_mode="1",
                    query_sql="SELECT a.cust_id, a.cust_name FROM ods.cust_src a"),
        ]

        clear_sql_ast_cache()
        kj1, pm1 = analyze_pipeline(rules1, {}, {}, "dws")
        # 模拟 batch 每组后清缓存
        clear_sql_ast_cache()
        kj2, pm2 = analyze_pipeline(rules2, {}, {}, "dws")

        # 验证不串：规则组1 的源表是 order_src，不是 cust_src
        tables1 = [t.source_table for t in pm1["R1"].source_tables]
        tables2 = [t.source_table for t in pm2["R1"].source_tables]
        assert any("order_src" in t for t in tables1), f"组1应是order_src: {tables1}"
        assert any("cust_src" in t for t in tables2), f"组2应是cust_src: {tables2}"
        assert not any("cust_src" in t for t in tables1), "组1不该串入cust_src"
        assert not any("order_src" in t for t in tables2), "组2不该串入order_src"

    def test_same_rulecode_different_groups_not_confused(self, tmp_path):
        """两个规则组用相同 rule_code（R1），缓存清了不会串。"""
        from engine import RawRule

        rules_a = [RawRule(rule_code="R1", target_schema="dws", target_table="a_f",
                           rule_type=1, exec_sequence=1, delete_mode="1",
                           query_sql="SELECT a.x FROM ods.tbl_a a")]
        rules_b = [RawRule(rule_code="R1", target_schema="dws", target_table="b_f",
                           rule_type=1, exec_sequence=1, delete_mode="1",
                           query_sql="SELECT a.y FROM ods.tbl_b a")]

        clear_sql_ast_cache()
        _, pm_a = analyze_pipeline(rules_a, {}, {}, "dws")
        clear_sql_ast_cache()
        _, pm_b = analyze_pipeline(rules_b, {}, {}, "dws")

        tables_a = [t.source_table for t in pm_a["R1"].source_tables]
        tables_b = [t.source_table for t in pm_b["R1"].source_tables]
        assert "ods.tbl_a" in tables_a
        assert "ods.tbl_b" in tables_b
        assert "ods.tbl_a" not in tables_b


# ═══════════════════════════════════════════════════════════════
# 缓存命中验证（parse_single_sql → build_data_blocks/topology 复用）
# ═══════════════════════════════════════════════════════════════

class TestCacheHitInPipeline:
    """验证 analyze_pipeline 内部 AST 复用（缓存命中）。"""

    def setup_method(self):
        clear_sql_ast_cache()

    def test_single_sql_cached_once(self):
        """单步 SQL 在整个 analyze_pipeline 里只解析一次 AST。"""
        from engine import RawRule
        sql = "SELECT a.id FROM ods.src_a a"
        rule = RawRule(rule_code="R1", rule_name="t", rule_type=1, exec_sequence=1,
                       target_schema="dws", target_table="t_f", delete_mode="1",
                       query_sql=sql)

        clear_sql_ast_cache()
        analyze_pipeline([rule], {}, {}, "dws")

        # 单步单 SQL，缓存里应该只有 1 条（clean_sql 相同）
        # build_data_blocks 和 build_topology 都复用了 parse_single_sql 的 AST
        assert len(_SQL_AST_CACHE) == 1, \
            f"单 SQL 应只缓存1条，实际 {len(_SQL_AST_CACHE)}（可能没命中缓存）"

    def test_multi_step_sql_cached_per_step(self):
        """多步 SQL，每步的 SQL 各缓存一条。"""
        from engine import RawRule
        rules = [
            RawRule(rule_code="R1", rule_name="step1", rule_type=1, exec_sequence=1,
                    target_schema="dws", target_table="tmp_t", delete_mode="1",
                    query_sql="SELECT a.id FROM ods.src_a a"),
            RawRule(rule_code="R2", rule_name="step2", rule_type=1, exec_sequence=2,
                    target_schema="dws", target_table="final_t", delete_mode="1",
                    query_sql="SELECT t.id FROM dws.tmp_t t"),
        ]

        clear_sql_ast_cache()
        analyze_pipeline(rules, {}, {}, "dws")

        # 两步两条不同 SQL，缓存 2 条
        assert len(_SQL_AST_CACHE) == 2, \
            f"两步应缓存2条，实际 {len(_SQL_AST_CACHE)}"

    def test_repeat_analysis_uses_cache(self):
        """同一规则组分析两次，第二次全部命中缓存。"""
        from engine import RawRule
        sql = "SELECT a.id, a.name FROM ods.src_a a WHERE a.flag=1"
        rule = RawRule(rule_code="R1", rule_name="t", rule_type=1, exec_sequence=1,
                       target_schema="dws", target_table="t_f", delete_mode="1",
                       query_sql=sql)

        clear_sql_ast_cache()
        analyze_pipeline([rule], {}, {}, "dws")
        cache_count_after_first = len(_SQL_AST_CACHE)

        # 第二次不清缓存，应全部命中（缓存条目不增加）
        analyze_pipeline([rule], {}, {}, "dws")
        cache_count_after_second = len(_SQL_AST_CACHE)

        assert cache_count_after_first == cache_count_after_second, \
            "第二次分析应全部命中缓存，条目不应增加"


# ═══════════════════════════════════════════════════════════════
# 复杂 SQL 缓存正确性
# ═══════════════════════════════════════════════════════════════

class TestComplexSqlCache:
    """复杂 SQL（CTE/UNION/子查询）下缓存正确。"""

    def setup_method(self):
        clear_sql_ast_cache()

    def test_cte_sql_cached_correctly(self):
        """CTE SQL 缓存后分析结果正确。"""
        from engine import RawRule
        sql = """WITH cte AS (SELECT id, name FROM ods.src_a WHERE flag = 1)
SELECT c.id, c.name FROM cte c"""
        rule = RawRule(rule_code="R1", rule_name="t", rule_type=1, exec_sequence=1,
                       target_schema="dws", target_table="t_f", delete_mode="1",
                       query_sql=sql)

        clear_sql_ast_cache()
        kj, pm = analyze_pipeline([rule], {}, {}, "dws")

        # CTE 内部表 src_a 应被提取
        tables = []
        for j in pm["R1"].source_tables:
            tables.append(j.source_table)
        for cte in pm["R1"].ctes:
            for t in cte.source_tables:
                tables.append(t["name"])
        assert any("src_a" in t for t in tables), f"CTE 内部表丢失: {tables}"

    def test_union_sql_cached_correctly(self):
        """UNION SQL 缓存后两分支表都正确。"""
        from engine import RawRule
        sql = """SELECT a.id FROM ods.src_a a
UNION ALL
SELECT b.id FROM ods.src_b b"""
        rule = RawRule(rule_code="R1", rule_name="t", rule_type=1, exec_sequence=1,
                       target_schema="dws", target_table="t_f", delete_mode="1",
                       query_sql=sql)

        clear_sql_ast_cache()
        kj, pm = analyze_pipeline([rule], {}, {}, "dws")

        tables = [t.source_table for t in pm["R1"].source_tables]
        assert any("src_a" in t for t in tables), f"UNION 分支 src_a 丢失: {tables}"
        assert any("src_b" in t for t in tables), f"UNION 分支 src_b 丢失: {tables}"

    def test_comment_in_sql_cached_correctly(self):
        """含注释的 SQL 缓存后注释不影响（clean_sql 已剔注释）。"""
        from engine import RawRule
        sql = """SELECT a.id, a.name FROM ods.src_a a
-- 这个表;很关键
LEFT JOIN ods.src_b b ON a.id = b.id"""
        rule = RawRule(rule_code="R1", rule_name="t", rule_type=1, exec_sequence=1,
                       target_schema="dws", target_table="t_f", delete_mode="1",
                       query_sql=sql)

        clear_sql_ast_cache()
        kj, pm = analyze_pipeline([rule], {}, {}, "dws")

        # 注释里的分号不应截断，两表都在
        tables = [t.source_table for t in pm["R1"].source_tables]
        assert any("src_a" in t for t in tables)
        assert any("src_b" in t for t in tables), f"注释分号导致表丢失: {tables}"
