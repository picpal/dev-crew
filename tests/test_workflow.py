import pytest
from devcrew.schema import Role
from devcrew.workflow import (ALLOWED_BY_TRIGGER, DECISION_ACTIONS, DEFAULT_TEMPLATE,
                              NodeSpec, Step, WorkflowError, WorkflowTemplate, next_step)


def test_default_template_shape():
    ids = [n.node_id for n in DEFAULT_TEMPLATE.nodes]
    assert ids == ["explore", "develop", "review", "qa"]
    assert DEFAULT_TEMPLATE.node("explore").conditional
    assert DEFAULT_TEMPLATE.node("review").loop_back_to == "develop"
    assert DEFAULT_TEMPLATE.node("qa").loop_back_to == "develop"
    assert DEFAULT_TEMPLATE.index("review") == 2


def test_template_validation():
    n = NodeSpec("a", Role.DEVELOPER, "m: {task}")
    with pytest.raises(WorkflowError):
        WorkflowTemplate("t", ())                       # empty
    with pytest.raises(WorkflowError):
        WorkflowTemplate("t", (n, n))                   # duplicate id
    with pytest.raises(WorkflowError):
        WorkflowTemplate("t", (NodeSpec("a", Role.QA, "m", loop_back_to="nope"),))


@pytest.mark.parametrize("transition,kind", [
    ("PASS", "ADVANCE"), ("NOT_PASS", "LOOP"), ("NEED_REPLAN", "DECIDE"),
    ("BLOCKED", "DECIDE"), ("INSUFFICIENT_CAPABILITY", "DECIDE")])
def test_next_step_full_table(transition, kind):
    node = DEFAULT_TEMPLATE.node("review")
    s = next_step(node, transition)
    assert s.kind == kind
    if kind == "LOOP":
        assert s.target == "develop"
    if kind == "DECIDE":
        assert s.trigger == transition


def test_next_step_self_loop_when_no_loop_back():
    s = next_step(DEFAULT_TEMPLATE.node("develop"), "NOT_PASS")
    assert s == Step("LOOP", target="develop")


def test_next_step_unknown_transition():
    with pytest.raises(WorkflowError):
        next_step(DEFAULT_TEMPLATE.node("develop"), "WAT")


# 노드 실행 중에 걸리는 트리거와, 실행이 시작되기 **전에** 걸리는 트리거는 다르다.
# 앞엣것은 엔진이 처리하고, 뒤엣것은 Slack 계층이 처리한다.
NODE_TRIGGERS = {"CLASSIFY", "NEED_REPLAN", "BLOCKED",
                 "INSUFFICIENT_CAPABILITY", "LOOP_GUARD_EXCEEDED"}
PREFLIGHT_TRIGGERS = {"UNKNOWN_REPO"}


def test_allowed_by_trigger_actions_are_known():
    for trigger, actions in ALLOWED_BY_TRIGGER.items():
        assert actions and set(actions) <= set(DECISION_ACTIONS)
    assert set(ALLOWED_BY_TRIGGER) == NODE_TRIGGERS | PREFLIGHT_TRIGGERS


def test_create_repo_is_never_offered_at_a_node_trigger():
    """CREATE_REPO는 실행 이전 결정이다 — 엔진에는 이 action을 실행할 코드가 없다.

    노드 트리거에 섞이면 leader가 루프 한가운데서 고를 수 있게 되고, 엔진은
    모르는 action을 받아 그 실행을 못 이어간다.
    """
    for trigger in NODE_TRIGGERS:
        assert "CREATE_REPO" not in ALLOWED_BY_TRIGGER[trigger], trigger
