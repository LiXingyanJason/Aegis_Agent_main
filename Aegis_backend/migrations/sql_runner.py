"""供历史 SQL 基线迁移复用的安全脚本执行器。"""

from collections.abc import Iterator
from pathlib import Path

from alembic import op


def split_sql_statements(script: str) -> Iterator[str]:
    """按分号拆分 PostgreSQL SQL，同时保留引号、注释和 dollar-quoted 函数体。"""
    start = 0
    index = 0
    quote: str | None = None
    dollar_tag: str | None = None
    line_comment = False
    block_comment = False

    while index < len(script):
        current = script[index]
        next_char = script[index + 1] if index + 1 < len(script) else ""
        if line_comment:
            if current == "\n":
                line_comment = False
            index += 1
            continue
        if block_comment:
            if current == "*" and next_char == "/":
                block_comment = False
                index += 2
            else:
                index += 1
            continue
        if dollar_tag is not None:
            if script.startswith(dollar_tag, index):
                index += len(dollar_tag)
                dollar_tag = None
            else:
                index += 1
            continue
        if quote is not None:
            if current == quote:
                if quote == "'" and next_char == "'":
                    index += 2
                    continue
                quote = None
            index += 1
            continue
        if current == "-" and next_char == "-":
            line_comment = True
            index += 2
            continue
        if current == "/" and next_char == "*":
            block_comment = True
            index += 2
            continue
        if current in {"'", '"'}:
            quote = current
            index += 1
            continue
        if current == "$":
            closing = script.find("$", index + 1)
            candidate = script[index : closing + 1] if closing >= 0 else ""
            if candidate and all(char.isalnum() or char == "_" or char == "$" for char in candidate):
                dollar_tag = candidate
                index += len(candidate)
                continue
        if current == ";":
            statement = script[start:index].strip()
            if statement:
                yield statement
            start = index + 1
        index += 1
    statement = script[start:].strip()
    if statement:
        yield statement


def execute_historical_sql(script_path: Path) -> None:
    """在当前 Alembic 事务中执行不可变历史 SQL，跳过其自带事务边界。"""
    for statement in split_sql_statements(script_path.read_text(encoding="utf-8")):
        executable_lines = [
            line for line in statement.splitlines() if not line.lstrip().startswith("--")
        ]
        if "\n".join(executable_lines).strip().upper() in {"BEGIN", "COMMIT"}:
            continue
        # 使用 Alembic Operation API，在线执行与 `--sql` 离线生成均可复用。
        op.execute(statement)
