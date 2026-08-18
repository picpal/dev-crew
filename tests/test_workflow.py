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


def test_allowed_by_trigger_actions_are_known():
    for trigger, actions in ALLOWED_BY_TRIGGER.items():
        assert actions and set(actions) <= set(DECISION_ACTIONS)
    assert set(ALLOWED_BY_TRIGGER) == {"CLASSIFY", "NEED_REPLAN", "BLOCKED",
                                       "INSUFFICIENT_CAPABILITY", "LOOP_GUARD_EXCEEDED"}
