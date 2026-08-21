"""charts — 도식 스펙(JSON) → 인라인 SVG. 모델은 데이터만, 그림은 하네스가."""


def test_bar_chart_is_inline_svg_without_script():
    from devcrew.report.charts import bar_chart
    out = bar_chart([("세션", 80.0), ("리포트", 50.0)], unit="%")
    assert out.startswith("<svg") and "<script" not in out
    assert "세션" in out and "80" in out


def test_unknown_diagram_type_falls_back_to_table():
    """그림을 못 그렸다고 해설을 잃지 않는다."""
    from devcrew.report.charts import render_diagram
    out = render_diagram({"type": "sankey", "items": [{"label": "a", "value": 1}]})
    assert "<table" in out and "<svg" not in out


def test_diagram_escapes_labels():
    from devcrew.report.charts import render_diagram
    out = render_diagram({"type": "bar", "items": [{"label": "<script>x</script>", "value": 1}]})
    assert "<script>" not in out and "&lt;script&gt;" in out


def test_flow_diagram_renders_nodes_and_edges():
    from devcrew.report.charts import render_diagram
    out = render_diagram({"type": "flow",
                          "nodes": [{"id": "a", "label": "출제"}, {"id": "b", "label": "검증"}],
                          "edges": [{"from": "a", "to": "b", "label": "문항"}]})
    assert "<svg" in out and "출제" in out and "검증" in out and "문항" in out


def test_flow_with_unknown_edge_endpoint_does_not_crash():
    from devcrew.report.charts import render_diagram
    out = render_diagram({"type": "flow", "nodes": [{"id": "a", "label": "A"}],
                          "edges": [{"from": "a", "to": "ghost", "label": "x"}]})
    assert "<svg" in out and "A" in out


def test_none_and_malformed_diagram_render_nothing():
    from devcrew.report.charts import render_diagram
    assert render_diagram(None) == ""
    assert render_diagram({"type": "bar"}) == ""              # items 없음
    assert render_diagram({"type": "bar", "items": []}) == ""


def test_bar_values_that_are_not_numbers_are_dropped():
    """모델이 문자열 수치를 흘려도 그래프가 깨지지 않는다."""
    from devcrew.report.charts import render_diagram
    out = render_diagram({"type": "bar", "items": [{"label": "a", "value": "많음"},
                                                   {"label": "b", "value": 3}]})
    assert "<svg" in out and "b" in out and "많음" not in out
