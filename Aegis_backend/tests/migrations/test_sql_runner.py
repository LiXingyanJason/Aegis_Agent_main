"""历史 SQL 基线迁移的脚本拆分测试。"""

from pathlib import Path

from migrations.sql_runner import split_sql_statements


def test_split_sql_statements_keeps_dollar_quoted_function_body() -> None:
    """测试函数体中的分号不会被错误识别为独立 SQL 语句。"""
    script = "CREATE FUNCTION test() RETURNS void AS $$ BEGIN PERFORM 1; END; $$ LANGUAGE plpgsql; SELECT 1;"

    statements = list(split_sql_statements(script))

    assert statements == [
        "CREATE FUNCTION test() RETURNS void AS $$ BEGIN PERFORM 1; END; $$ LANGUAGE plpgsql",
        "SELECT 1",
    ]


def test_initial_schema_script_can_be_split_without_empty_statements() -> None:
    """测试现有初始 Schema 可被迁移执行器完整拆分。"""
    root_directory = Path(__file__).resolve().parents[2]
    script = (root_directory / "migrations" / "sql" / "001_initial_schema.sql").read_text(
        encoding="utf-8"
    )

    statements = list(split_sql_statements(script))

    assert len(statements) > 50
    assert any("CREATE OR REPLACE FUNCTION aegis_set_updated_at()" in item for item in statements)
    assert any("CREATE POLICY conversations_owner" in item for item in statements)
