import pytest

from app.services.report_agent import (
    Report,
    ReportAgent,
    ReportManager,
    ReportStatus,
)


def _report(report_id: str, simulation_id: str, created_at: str) -> Report:
    return Report(
        report_id=report_id,
        simulation_id=simulation_id,
        graph_id="graph-1",
        simulation_requirement="requirement",
        status=ReportStatus.COMPLETED,
        created_at=created_at,
    )


def test_report_by_simulation_returns_the_newest_report(tmp_path, monkeypatch):
    monkeypatch.setattr(ReportManager, "REPORTS_DIR", str(tmp_path / "reports"))
    old = _report("report_old", "sim-1", "2026-01-01T00:00:00")
    new = _report("report_new", "sim-1", "2026-02-01T00:00:00")
    ReportManager.save_report(old)
    ReportManager.save_report(new)
    ReportManager.save_report(_report("report_other", "sim-2", "2026-03-01T00:00:00"))

    report = ReportManager.get_report_by_simulation("sim-1")

    assert report is not None
    assert report.report_id == "report_new"


def test_report_id_cannot_escape_the_reports_directory(tmp_path, monkeypatch):
    reports_dir = tmp_path / "uploads" / "reports"
    (reports_dir / "report_ok").mkdir(parents=True)
    (tmp_path / "uploads" / "projects").mkdir()
    monkeypatch.setattr(ReportManager, "REPORTS_DIR", str(reports_dir))

    assert ReportManager.delete_report("..") is False
    assert ReportManager.get_report("../meta") is None
    assert ReportManager.get_progress("..") is None
    assert ReportManager.get_generated_sections("..") == []
    assert ReportManager.get_console_log("..")["logs"] == []
    assert ReportManager.get_agent_log("..")["logs"] == []
    assert sorted(p.name for p in (tmp_path / "uploads").iterdir()) == [
        "projects",
        "reports",
    ]

    with pytest.raises(ValueError):
        ReportManager._get_report_folder("..")


def test_report_markdown_falls_back_to_section_file(tmp_path, monkeypatch):
    monkeypatch.setattr(ReportManager, "REPORTS_DIR", str(tmp_path / "reports"))
    report = _report("report_md", "sim-1", "2026-01-01T00:00:00")
    report.markdown_content = ""
    ReportManager.save_report(report)
    with open(ReportManager._get_report_markdown_path("report_md"), "w", encoding="utf-8") as handle:
        handle.write("# assembled")

    loaded = ReportManager.get_report("report_md")

    assert loaded is not None
    assert loaded.markdown_content == "# assembled"


def test_parse_tool_calls_ignores_unusable_payloads():
    agent = ReportAgent.__new__(ReportAgent)

    assert agent._parse_tool_calls("<tool_call>{}</tool_call>") == []
    assert agent._parse_tool_calls(
        '<tool_call>{"name": null, "parameters": {}}</tool_call>'
    ) == []

    calls = agent._parse_tool_calls(
        '<tool_call>{"tool": "quick_search", "params": {"query": "q"}}</tool_call>'
    )
    assert calls == [{"name": "quick_search", "parameters": {"query": "q"}}]


def test_parse_tool_calls_keeps_valid_calls_after_invalid_ones():
    agent = ReportAgent.__new__(ReportAgent)
    response = (
        '<tool_call>{}</tool_call>'
        '<tool_call>{"name": "quick_search", "parameters": {"query": "q"}}</tool_call>'
    )

    calls = agent._parse_tool_calls(response)

    assert [call["name"] for call in calls] == ["quick_search"]
