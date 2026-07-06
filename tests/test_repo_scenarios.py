"""代码仓 yml 分析端到端测试（模拟真实代码仓结构 + 干扰项）。

用 _build_repo 构造完整的模拟代码仓（BFT/DDL/DQ/LTS/ADMS/Release），
测试在真实目录结构和各种干扰项下，代码仓 yml 分析能否正确工作。

覆盖场景：
- 深层目录下的规则组能被正确分析
- DDL 能从代码仓根自动定位（跨 BFT→DDL 子树）
- 干扰项（BftMetric/DQ/Release 同名 yml）不会被误读
- 实时层 DDL 不会干扰离线层 DDL 发现
- 两层 DDL（DWS_EDW/DWS_RT_EDW）的查找顺序
- 端到端：read_yml + DDL发现 + analyze_pipeline 全链路

运行:
    pytest tests/test_repo_scenarios.py -v
"""

import sys
import json
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ANALYZER_REF = PROJECT_ROOT / "dws-pipeline-analyzer" / "references"
FIXTURES = PROJECT_ROOT / "tests" / "fixtures" / "analyzer"
sys.path.insert(0, str(ANALYZER_REF))
sys.path.insert(0, str(FIXTURES))

from _build_repo import build_mock_repo


@pytest.fixture
def mock_repo(tmp_path):
    """构造模拟代码仓。"""
    return build_mock_repo(tmp_path / "repo")


class TestRepoRootDiscovery:
    """代码仓根定位（从规则组目录向上找 BFT/+DDL/）。"""

    def test_find_root_from_deep_group(self, mock_repo):
        """从深层规则组目录能向上找到代码仓根。"""
        from analyzer import _find_repo_root
        group_dir = mock_repo["group_dir"]
        # 规则组在 BFT/BftWideTable/P_TRADE/SUB_TRADE/DWB_TRADE_ORDER_D（5层深）
        root = _find_repo_root(group_dir)
        assert root == mock_repo["repo_root"].resolve()

    def test_find_root_not_confused_by_release(self, mock_repo):
        """Release 目录里的同名规则组不应被误认为代码仓根。"""
        from analyzer import _find_repo_root
        # Release 里也有 BFT/.../DWB_TRADE_ORDER_D，但 Release 下没有 DDL/
        release_group = (mock_repo["repo_root"] / "Release" / "202401" /
                         "BFT" / "BftWideTable" / "P_TRADE" / "SUB_TRADE" /
                         "DWB_TRADE_ORDER_D")
        root = _find_repo_root(release_group)
        # 应该向上找到真正的代码仓根（Release 外面有 BFT/+DDL/）
        assert root == mock_repo["repo_root"].resolve()


class TestDdlAutoDiscovery:
    """DDL 自动发现（跨 BFT→DDL 子树定位）。"""

    def test_discover_ddl_from_repo(self, mock_repo):
        """能从代码仓结构自动定位目标表的 DDL 目录。"""
        from analyzer import _auto_discover_ddl_from_repo, read_yml

        group_dir = mock_repo["group_dir"]
        raw = read_yml(str(group_dir))
        rules = raw["rules"]

        ddl_dir = _auto_discover_ddl_from_repo(group_dir, rules)
        assert ddl_dir != "", "应找到 DDL 目录"
        # 找到的 DDL 目录应在 DWS_EDW 下（离线层优先）
        assert "DWS_EDW" in ddl_dir, f"应优先找 DWS_EDW，实际 {ddl_dir}"
        # DDL 文件存在
        ddl_file = Path(ddl_dir) / "dwb_trade_order_d.sql"
        assert ddl_file.exists()

    def test_ddl_finds_offline_not_realtime(self, mock_repo):
        """离线层(DWS_EDW)和实时层(DWS_RT_EDW)有同名表时，应优先找离线层。"""
        from analyzer import _auto_discover_ddl_from_repo, read_yml
        group_dir = mock_repo["group_dir"]
        raw = read_yml(str(group_dir))
        ddl_dir = _auto_discover_ddl_from_repo(group_dir, raw["rules"])
        assert "DWS_EDW" in ddl_dir, "同名表应优先匹配 DWS_EDW（离线层）"

    def test_ddl_not_found_returns_empty(self, mock_repo):
        """表没有 DDL 时返回空字符串，不崩溃。"""
        from analyzer import _auto_discover_ddl_from_repo
        from engine import RawRule
        # 构造一个不存在的表
        group_dir = mock_repo["group_dir"]
        fake_rules = [RawRule(target_schema="dws", target_table="not_exist_table", rule_type=1)]
        ddl_dir = _auto_discover_ddl_from_repo(group_dir, fake_rules)
        assert ddl_dir == ""

    def test_ddl_skips_view_directory(self, mock_repo):
        """DDL 发现在 table/ 目录找，不找 view/ 目录。"""
        from analyzer import _auto_discover_ddl_from_repo
        from engine import RawRule
        # dwb_trade_order_d_v 只在 view 目录，table 目录没有
        group_dir = mock_repo["group_dir"]
        fake_rules = [RawRule(target_schema="dws", target_table="dwb_trade_order_d_v", rule_type=1)]
        ddl_dir = _auto_discover_ddl_from_repo(group_dir, fake_rules)
        assert ddl_dir == "", "view 目录的 DDL 不应被发现"


class TestYmlAnalysisNotDisturbed:
    """代码仓分析不被干扰项影响。"""

    def test_bft_widetable_not_metric(self, mock_repo):
        """分析宽表规则组时，不会读入指标(BftMetric)的规则。"""
        from analyzer import read_yml
        raw = read_yml(str(mock_repo["group_dir"]))
        # 宽表规则组应只有 R0001/R0002，不含指标 M0001
        rule_codes = {r.rule_code for r in raw["rules"]}
        assert rule_codes == {"R0001", "R0002"}, f"不应混入指标规则，实际 {rule_codes}"

    def test_release_not_read(self, mock_repo):
        """Release 目录里的旧版 yml 不会被读入。"""
        from analyzer import read_yml
        # 明确读 BFT 下的规则组，不读 Release
        raw = read_yml(str(mock_repo["group_dir"]))
        for rule in raw["rules"]:
            # Release 里的 R0001 的 SQL 是 "SELECT 1"，真正的不是
            if rule.rule_code == "R0001":
                assert "ods_trade_order" in rule.query_sql, \
                    "不应读到 Release 里的旧版 SQL"
                assert "SELECT 1" not in rule.query_sql

    def test_dq_yml_not_read(self, mock_repo):
        """DQ 目录的 yml 不会被当执行规则读入。"""
        from analyzer import read_yml
        raw = read_yml(str(mock_repo["group_dir"]))
        rule_codes = {r.rule_code for r in raw["rules"]}
        assert "DQ0001" not in rule_codes, "不应读入 DQ 规则"

    def test_separate_groups_not_mixed(self, mock_repo):
        """不同规则组的 yml 不会互相混入。"""
        from analyzer import read_yml
        raw1 = read_yml(str(mock_repo["group_dir"]))
        raw2 = read_yml(str(mock_repo["other_group_dir"]))
        codes1 = {r.rule_code for r in raw1["rules"]}
        codes2 = {r.rule_code for r in raw2["rules"]}
        assert codes1 == {"R0001", "R0002"}
        assert codes2 == {"R1001"}
        assert codes1.isdisjoint(codes2), "两个规则组不应互相混入规则"


class TestEndToEndRepoAnalysis:
    """端到端：代码仓 yml → DDL发现 → analyze_pipeline 全链路。"""

    def test_full_pipeline_from_repo(self, mock_repo, tmp_path):
        """从代码仓规则组目录出发，全链路分析成功，DDL 被正确利用。"""
        from analyzer import read_yml, _auto_discover_ddl_from_repo
        from engine import analyze_pipeline, detect_dialect, parse_ddl_for_metadata

        # 1. 读 yml
        group_dir = mock_repo["group_dir"]
        raw = read_yml(str(group_dir))
        assert len(raw["rules"]) == 2

        # 2. 方言检测
        sqls = [r.query_sql for r in raw["rules"] if r.query_sql]
        dialect = detect_dialect(sqls)

        # 3. DDL 发现
        ddl_dir = _auto_discover_ddl_from_repo(group_dir, raw["rules"])
        assert ddl_dir != "", "DDL 应被发现"

        # 4. 引擎分析（带 DDL）
        knowledge, _ = analyze_pipeline(
            raw["rules"], raw["target_fields"], raw["group_variables"], dialect,
            ddl_dir=ddl_dir, rule_group_code=raw["rule_group_code"],
        )

        # 5. 验证 knowledge 正确性
        assert knowledge["meta"]["target_table"] == "dwb_trade_order_d"
        # DDL 的字段类型应被注入（证明 DDL 被正确利用了）
        tf_types = knowledge["meta"]["target_field_types"]
        assert "total_amount" in tf_types, "DDL 的字段类型应被注入到 meta"
        assert "DECIMAL" in tf_types["total_amount"].upper()
        # DDL 的字段注释应被注入
        tf_comments = knowledge["meta"]["target_field_comments"]
        assert "total_amount" in tf_comments
        assert "订单总额" in tf_comments["total_amount"]

    def test_full_pipeline_without_ddl(self, mock_repo):
        """DDL 找不到时（或不传），分析仍成功，只是缺字段类型。"""
        from analyzer import read_yml
        from engine import analyze_pipeline, detect_dialect

        raw = read_yml(str(mock_repo["group_dir"]))
        sqls = [r.query_sql for r in raw["rules"] if r.query_sql]
        dialect = detect_dialect(sqls)

        # 不传 ddl_dir（模拟 DDL 找不到的场景）
        knowledge, _ = analyze_pipeline(
            raw["rules"], raw["target_fields"], raw["group_variables"], dialect,
            ddl_dir="",
        )

        assert knowledge["meta"]["target_table"] == "dwb_trade_order_d"
        # 没有 DDL，target_field_types 应为空
        assert knowledge["meta"]["target_field_types"] == {}
        # 但 knowledge 仍完整
        assert len(knowledge["topology"]["steps"]) == 2
        assert len(knowledge["field_mappings"]["fields"]) > 0

    def test_cla_analyze_via_main(self, mock_repo, tmp_path):
        """模拟 CLI 调用：python analyzer.py --input {规则组目录} --output docs/"""
        from analyzer import main
        import sys as _sys

        group_dir = mock_repo["group_dir"]
        output_base = tmp_path / "output"

        # 模拟命令行参数
        old_argv = _sys.argv
        _sys.argv = ["analyzer.py", "--input", str(group_dir),
                     "--output", str(output_base)]
        try:
            main()
        finally:
            _sys.argv = old_argv

        # 验证输出
        output_dir = output_base / "DWB_TRADE_ORDER_D"
        assert (output_dir / "knowledge_draft.json").exists(), "knowledge_draft.json 应生成"
        # 读 knowledge 验证
        kj = json.loads((output_dir / "knowledge_draft.json").read_text(encoding="utf-8"))
        assert kj["meta"]["target_table"] == "dwb_trade_order_d"
        # DDL 应被自动发现并利用（CLI 场景）
        assert kj["meta"]["target_field_types"], "CLI 场景 DDL 应被自动发现"

    def test_cla_input_dispatch_file_vs_dir(self, mock_repo, tmp_path):
        """输入分流：文件走 read_excel，目录走 read_yml。"""
        # 这里只验证 yml（目录）路径不报错且产出正确
        # xlsx 路径已在 test_end_to_end 覆盖
        from analyzer import main
        import sys as _sys

        group_dir = mock_repo["group_dir"]
        output_base = tmp_path / "output_dir"

        old_argv = _sys.argv
        _sys.argv = ["analyzer.py", "--input", str(group_dir),
                     "--output", str(output_base)]
        try:
            main()  # 目录输入不应报错
        finally:
            _sys.argv = old_argv

        output_dir = output_base / "DWB_TRADE_ORDER_D"
        assert (output_dir / "knowledge_draft.json").exists()
