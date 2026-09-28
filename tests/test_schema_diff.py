"""POST /schema-diff 端到端测试。运行：python -m pytest -q"""

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def run(old_schema, new_schema, instance, max_evaluations=None):
    body = {"old_schema": old_schema, "new_schema": new_schema, "instance": instance}
    if max_evaluations is not None:
        body["max_evaluations"] = max_evaluations
    return client.post("/schema-diff", json=body)


def _revalidate(schema, instance):
    from app.minimizer import build_validator

    err = next(iter(build_validator(schema).iter_errors(instance)), None)
    return err is None, err


# ---- HTTP 层 ---------------------------------------------------------------


def test_invalid_old_schema_returns_422():
    resp = run({"type": "not-a-type"}, {"type": "integer"}, 1)
    assert resp.status_code == 422
    data = resp.json()
    assert data["error"] == "invalid_schema"
    assert data["side"] == "old"


def test_invalid_new_schema_returns_422():
    resp = run({"type": "integer"}, {"type": "not-a-type"}, 1)
    assert resp.status_code == 422
    data = resp.json()
    assert data["error"] == "invalid_schema"
    assert data["side"] == "new"


def test_missing_field_returns_422():
    resp = client.post("/schema-diff", json={"old_schema": {}})
    assert resp.status_code == 422


def test_max_evaluations_must_be_at_least_two():
    # 每个候选需要旧版、新版各一次校验，锁定本身就要 2 次评估。
    resp = run({}, False, 1, max_evaluations=1)
    assert resp.status_code == 422


def test_unusable_schema_at_validation_returns_422_with_side():
    # check_schema 通过，但实际校验时坏 $ref 无法解析。
    resp = run({}, {"$ref": "#/definitions/Missing"}, {"x": 1})
    assert resp.status_code == 422
    data = resp.json()
    assert data["error"] == "invalid_schema"
    assert data["side"] == "new"


# ---- 结果相同：不缩减 -------------------------------------------------------


def test_both_valid_no_reduction():
    resp = run({"type": "string"}, {"minLength": 1}, "hello")
    assert resp.status_code == 200
    data = resp.json()
    assert data["differs"] is False
    assert data["direction"] is None
    assert data["old_valid"] is True and data["new_valid"] is True
    assert data["reduced_instance"] == "hello"
    assert data["evaluations"] == 2
    assert data["stopped_by_budget"] is False
    assert data["trace"] == []
    assert data["old_first_error"] is None
    assert data["new_first_error"] is None


def test_both_invalid_no_reduction():
    resp = run(False, {"type": "integer"}, "x")
    data = resp.json()
    assert data["differs"] is False
    assert data["direction"] is None
    assert data["old_valid"] is False and data["new_valid"] is False
    assert data["reduced_instance"] == "x"
    # 两版首个错误均回传。
    assert data["old_first_error"] is not None
    assert data["new_first_error"]["keyword"] == "type"


# ---- 结果相反：确定性缩减 ---------------------------------------------------


def test_newly_rejected_direction_and_witness():
    # minItems 2 -> 3：[1,2] 旧版通过、新版拒绝（新版本更严格）。
    resp = run({"type": "array", "minItems": 2}, {"type": "array", "minItems": 3}, [1, 2])
    data = resp.json()
    assert data["differs"] is True
    assert data["direction"] == "newly_rejected"
    assert data["old_valid"] is True and data["new_valid"] is False
    assert data["new_first_error"]["keyword"] == "minItems"
    # 长度无法缩减（长度 1 时旧版也拒绝），只能把元素坍缩为 null。
    assert data["reduced_instance"] == [None, None]
    assert data["stopped_by_budget"] is False
    # 见证必须保持旧版通过、新版拒绝。
    assert _revalidate({"type": "array", "minItems": 2}, data["reduced_instance"])[0] is True
    assert _revalidate({"type": "array", "minItems": 3}, data["reduced_instance"])[0] is False


def test_newly_accepted_direction_and_witness():
    # minLength 3 -> 1：'hi' 旧版拒绝、新版接受（新版本更宽松）。
    resp = run({"type": "string", "minLength": 3}, {"type": "string", "minLength": 1}, "hi")
    data = resp.json()
    assert data["direction"] == "newly_accepted"
    assert data["old_valid"] is False and data["new_valid"] is True
    assert data["old_first_error"]["keyword"] == "minLength"
    # 单字符旧版仍拒绝（长度 1 < 3）、新版接受（长度 1 >= 1）。
    assert data["reduced_instance"] == "h"
    assert _revalidate({"type": "string", "minLength": 3}, "h")[0] is False
    assert _revalidate({"type": "string", "minLength": 1}, "h")[0] is True


def test_object_fields_stripped_while_old_stays_valid():
    # 新版 false 恒拒绝；旧版只要求必填 a。见证应删掉 b/c，只保留 a 的最简形式。
    old_schema = {"type": "object", "required": ["a"]}
    instance = {"a": 1, "b": [1, 2, 3], "c": "longstring-value"}
    data = run(old_schema, False, instance).json()
    assert data["direction"] == "newly_rejected"
    witness = data["reduced_instance"]
    assert witness == {"a": None}
    # {} 会让旧版也拒绝（required a），故不得被接受。
    assert _revalidate(old_schema, witness)[0] is True
    assert _revalidate(False, witness)[0] is False


def test_evaluations_count_two_per_candidate():
    # 第 1 个 trace 条目是首次候选（坍缩数组），其前还有锁定的 2 次评估。
    data = run(
        {"type": "array", "minItems": 2},
        {"type": "array", "minItems": 3},
        [1, 2],
    ).json()
    # 每个候选 2 次评估 + 锁定 2 次。
    assert data["evaluations"] == 2 + 2 * len(data["trace"])
    # trace 序号是候选序号（从 1 连续），评估总额另行统计。
    assert [t["index"] for t in data["trace"]] == list(range(1, len(data["trace"]) + 1))


def test_every_trace_entry_records_both_sides():
    data = run(
        {"type": "array", "minItems": 2},
        {"type": "array", "minItems": 3},
        [1, 2],
    ).json()
    for entry in data["trace"]:
        assert {
            "index",
            "op",
            "path",
            "detail",
            "kept",
            "old_valid",
            "new_valid",
            "old_keyword",
            "new_keyword",
        } <= entry.keys()
        # 被接受的候选必须保持原始的（通过, 拒绝）组合。
        if entry["kept"]:
            assert entry["old_valid"] is True and entry["new_valid"] is False


def test_determinism():
    payload = {
        "old_schema": {"type": "object", "required": ["a", "b"]},
        "new_schema": False,
        "instance": {"a": 1, "b": "xxxxx", "junk": [1, 2, 3, 4], "more": {"k": "v"}},
    }
    first = client.post("/schema-diff", json=payload).json()
    second = client.post("/schema-diff", json=payload).json()
    assert first["reduced_instance"] == second["reduced_instance"]
    assert first["trace"] == second["trace"]
    assert first["evaluations"] == second["evaluations"]


def test_budget_stop_flag_and_limit():
    # 预算 4 = 锁定 2 次 + 第一次候选（坍缩数组）2 次；坍缩被拒后预算即耗尽。
    data = run(
        {"type": "array", "minItems": 2},
        {"type": "array", "minItems": 3},
        [1, 2],
        max_evaluations=4,
    ).json()
    assert data["stopped_by_budget"] is True
    assert data["evaluations"] == 4
    assert data["direction"] == "newly_rejected"
    # 预算耗尽时保留已接受的缩减（此处没有候选被接受，故为原实例）。
    assert data["reduced_instance"] == [1, 2]


def test_budget_exactly_lock_only():
    data = run(
        {"type": "array", "minItems": 2},
        {"type": "array", "minItems": 3},
        [1, 2],
        max_evaluations=2,
    ).json()
    assert data["stopped_by_budget"] is True
    assert data["evaluations"] == 2
    assert data["trace"] == []


def test_hard_max_evaluations_boundary():
    over = 100001
    resp = run({}, False, 1, max_evaluations=over)
    assert resp.status_code == 422


def test_boolean_schemas_true_vs_false():
    # old: true 恒通过，new: false 恒拒绝：任意实例都是差异见证。
    data = run(True, False, {"deep": {"nested": [1, 2, 3]}}).json()
    assert data["direction"] == "newly_rejected"
    assert data["reduced_instance"] is None


@pytest.mark.parametrize(
    "old_schema,new_schema,instance",
    [
        ({"type": "integer"}, {"type": "integer", "minimum": 5}, 3),
        ({"type": "string", "maxLength": 3}, {"type": "string", "maxLength": 1}, "xx"),
        (False, {}, {"any": "thing"}),
    ],
)
def test_witness_always_preserves_the_pair(old_schema, new_schema, instance):
    data = run(old_schema, new_schema, instance).json()
    assert data["differs"] is True
    old_valid, _ = _revalidate(old_schema, data["reduced_instance"])
    new_valid, _ = _revalidate(new_schema, data["reduced_instance"])
    assert old_valid == data["old_valid"]
    assert new_valid == data["new_valid"]
    assert old_valid != new_valid
