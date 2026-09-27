"""端到端 / 单元测试。运行：python -m pytest -q"""

from fastapi.testclient import TestClient

from app.main import app
from app.minimizer import Minimizer, _scalar_candidates, build_validator

client = TestClient(app)


def run(schema, instance, max_evaluations=1000):
    resp = client.post(
        "/minimize",
        json={"schema": schema, "instance": instance, "max_evaluations": max_evaluations},
    )
    return resp


# ---- HTTP 层 ---------------------------------------------------------------


def test_valid_instance_returns_422():
    resp = run({"type": "object"}, {"a": 1})
    assert resp.status_code == 422
    assert resp.json()["error"] == "instance_valid"


def test_invalid_schema_returns_422():
    resp = run({"type": "not-a-type"}, {})
    assert resp.status_code == 422
    assert resp.json()["error"] == "invalid_schema"


def test_bad_max_evaluations_returns_422():
    resp = client.post(
        "/minimize",
        json={"schema": False, "instance": 1, "max_evaluations": 0},
    )
    assert resp.status_code == 422


def test_healthz():
    assert client.get("/healthz").json() == {"status": "ok"}


# ---- 最小化行为 -------------------------------------------------------------


def test_object_field_removal_keeps_required_keyword():
    # 所有属性类型都正确，保证首个错误来自 required（jsonschema 按
    # type -> required -> ... 的固定顺序报告）。
    schema = {
        "type": "object",
        "required": ["name", "age"],
        "properties": {"name": {"type": "string"}, "age": {"type": "integer"}},
    }
    instance = {"name": "abcdef", "extra": [1, 2, 3, 4], "nested": {"x": 10}}
    resp = run(schema, instance)
    assert resp.status_code == 200
    data = resp.json()
    assert data["target_keyword"] == "required"
    minimal = data["minimal_instance"]
    # age 缺失即为最小 required 反例；所有无关字段都被删除。
    assert minimal == {}
    assert data["stopped_by_budget"] is False
    # 第 1 次评估是 lock。
    assert data["trace"][0]["op"] == "lock"
    assert data["evaluations"] == len(data["trace"])


def test_array_chunk_removal_minItems():
    schema = {"type": "array", "minItems": 3}
    instance = [10, 20]
    resp = run(schema, instance)
    data = resp.json()
    assert data["target_keyword"] == "minItems"
    # 空数组也触发 minItems，因此整体坍缩为 []。
    assert data["minimal_instance"] == []


def test_scalar_type_keyword_reduces_to_null():
    schema = {"type": "integer"}
    resp = run(schema, "a very long string indeed")
    data = resp.json()
    assert data["target_keyword"] == "type"
    assert data["minimal_instance"] is None


def test_string_minLength_keeps_shortest_illegal_string():
    schema = {"type": "string", "minLength": 30}
    resp = run(schema, "hello-world-long")
    data = resp.json()
    assert data["target_keyword"] == "minLength"
    # 空串也会触发 minLength，因此最小反例就是空串。
    assert data["minimal_instance"] == ""


def test_string_maxLength_collapses_to_one_char():
    schema = {"type": "string", "maxLength": 2}
    resp = run(schema, "abcdefghij")
    data = resp.json()
    assert data["target_keyword"] == "maxLength"
    assert len(data["minimal_instance"]) == 3


def test_numeric_multipleof_reduces_without_oscillation():
    schema = {"type": "integer", "multipleOf": 3}
    resp = run(schema, 100)
    data = resp.json()
    assert data["target_keyword"] == "multipleOf"
    minimal = data["minimal_instance"]
    # 1 不是 3 的倍数，是最小的非零反例；绝不能在 1/-1 间振荡。
    assert minimal == 1
    assert data["stopped_by_budget"] is False


def test_enum_scalar():
    schema = {"enum": [1, 2, 3]}
    resp = run(schema, 99999)
    data = resp.json()
    assert data["target_keyword"] == "enum"
    # null 不在 enum 中，应一步到位。
    assert data["minimal_instance"] is None


def test_boolean_schema_false():
    resp = run(False, {"a": [1, 2, 3]})
    data = resp.json()
    # false schema 没有具体关键字，jsonschema 报告的是 False/false schema 分支。
    assert data["minimal_instance"] is None


def test_nested_object_minimization():
    schema = {
        "type": "object",
        "properties": {
            "outer": {
                "type": "object",
                "properties": {"inner": {"type": "array", "minItems": 4}},
            }
        },
    }
    instance = {"outer": {"inner": [1, 2, 3]}, "junk": "x" * 50}
    data = run(schema, instance).json()
    # junk 被删；inner 为空数组仍触发 minItems，故最终坍缩为 []。
    assert data["minimal_instance"] == {"outer": {"inner": []}}


def test_determinism():
    schema = {"type": "object", "required": ["a", "b", "c", "z"]}
    instance = {"a": "longvalue", "b": [1, 2, 3, 4], "c": 42, "d": {"k": "v"}, "e": None}
    first = run(schema, instance).json()
    second = run(schema, instance).json()
    assert first["minimal_instance"] == second["minimal_instance"]
    assert first["trace"] == second["trace"]


def test_budget_stop_flag_and_limit():
    # 数组长度本身合法，但每个元素对象都缺必填键 a（首错为 required）。
    # 空数组与 null 都不再触发 required，因此无法整体坍缩，ddmin 只能
    # 逐块缩减；预算极小时在块删除阶段就耗尽。
    schema = {"type": "array", "items": {"type": "object", "required": ["a"]}}
    instance = [{"b": i} for i in range(40)]
    data = run(schema, instance, max_evaluations=8).json()
    assert data["stopped_by_budget"] is True
    assert data["evaluations"] <= 8


def test_every_trace_step_is_consumed():
    schema = {"type": "array", "minItems": 30}
    data = run(schema, list(range(20))).json()
    for entry in data["trace"]:
        assert {"index", "op", "path", "detail", "kept", "triggered_keyword"} <= entry.keys()
    idxs = [e["index"] for e in data["trace"]]
    assert idxs == list(range(1, len(idxs) + 1))


# ---- 直接测试 Minimizer / 标量阶梯 -----------------------------------------


def test_scalar_candidates_ordering_and_dedup():
    cands = _scalar_candidates(100)
    assert 0 in cands and 1 in cands
    # 严格递减的候选集不应包含原值。
    assert 100 not in cands
    assert _scalar_candidates(None) == []
    assert False in _scalar_candidates(True)
    assert "x" in _scalar_candidates("xyz")
    # null 与空串统一由坍缩阶段负责。
    assert None not in cands
    assert "" not in _scalar_candidates("xyz")


def test_draft4_detection():
    v = build_validator({"$schema": "http://json-schema.org/draft-04/schema#", "type": "integer"})
    from jsonschema import Draft4Validator

    assert isinstance(v, Draft4Validator)
    m = Minimizer(v, "not-an-int", 100)
    m.lock()
    assert m.target_keyword == "type"
    result = m.minimize()
    assert result.minimal_instance is None
