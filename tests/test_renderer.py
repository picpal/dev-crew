from devcrew.report.renderer import render


def make_view():
    return {
        "task_id": "TASK-1", "status": "DONE", "lead_time_min": 12,
        "instances": [{"instance_id": "DEV-1", "role": "DEVELOPER",
                       "model": "claude-sonnet-5", "effort": "high"}],
        "loops": [{"iteration": 1, "verdict": "PASS"}],
        "decisions": ["<script>alert(1)</script>주입 시도"],
    }


def test_render_is_self_contained():
    html = render(make_view())
    assert "<html" in html and "TASK-1" in html
    # 외부 요청 0: http(s) 소스 참조가 없어야 한다
    assert "src=\"http" not in html and "href=\"http" not in html
    assert "@import" not in html


def test_render_escapes_agent_output():
    html = render(make_view())
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_render_records_template_version():
    assert 'data-template-version="' in render(make_view())
