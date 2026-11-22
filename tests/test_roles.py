from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from collaboration_runtime.roles import RoleDefinition, RoleFileError, parse_roles

ROOT = Path(__file__).resolve().parents[1] / "test samples"


@pytest.mark.parametrize("folder,ids", [
    ("scenario_1_software_engineering 2", ("lead", "dev", "qa", "release")),
    ("scenario_2_newsroom 2", ("chief_editor", "reporter", "fact_checker", "editor")),
    ("scenario_3_securities_fund 2", ("fund_manager", "research", "risk", "marketing")),
])
def test_course_files(folder, ids):
    text = (ROOT / folder / "01_role_initialization.md").read_text(encoding="utf-8")
    roles = parse_roles(text)
    assert tuple(role.role_id for role in roles) == ids
    for role in roles:
        assert f"```text\n{role.prompt}```" in text
    with pytest.raises(FrozenInstanceError):
        roles[0].name = "changed"


def test_variable_order_and_exact_multiline_crlf():
    text = ("# Roles\r\n\r\n| Agent ID | Role |\r\n|---|---|\r\n"
            "| `a` | Alpha |\r\n| `b_2` | Beta |\r\n\r\n"
            "## Beta (b_2)\r\n\r\n```text\r\n  Beta  \r\nsecond\r\n```\r\n\r\n"
            "## Alpha (a)\r\n```text\r\nAlpha’s line\r\n```\r\n")
    assert parse_roles(text) == (RoleDefinition("a", "Alpha", "Alpha’s line\r\n"),
                                 RoleDefinition("b_2", "Beta", "  Beta  \r\nsecond\r\n"))


BASE = "| Agent ID | Role |\n|---|---|\n| `a` | Alpha |\n\n## Alpha (a)\n```text\nPrompt\n```\n"


@pytest.mark.parametrize("text,line", [
    ("", 1),
    (BASE.replace("|---|---|", "wrong"), 2),
    (BASE.replace("`a`", "`A`"), 3),
    (BASE.replace("| `a` | Alpha |", "| `a` | Alpha |\n| `a` | Alpha |"), 4),
    (BASE.replace("## Alpha", "## Wrong"), 5),
    (BASE.replace("(a)", "(other)"), 5),
    (BASE.replace("Prompt\n", ""), 7),
    (BASE.replace("```\n", "", 1), 8),
    (BASE[:BASE.index("## Alpha")], 5),
    (BASE + "\n## Alpha (a)\n```text\nAgain\n```\n", 10),
])
def test_bad_input_has_line_number(text, line):
    with pytest.raises(RoleFileError) as exc:
        parse_roles(text)
    assert exc.value.line == line
    assert f"Line {line}:" in str(exc.value)
