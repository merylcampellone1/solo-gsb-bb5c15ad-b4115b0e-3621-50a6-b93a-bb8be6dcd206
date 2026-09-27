"""POST /mutations 的端到端 / 单元测试。运行：python -m pytest -q"""

import json
import re

from fastapi.testclient import TestClient

from app.main import app
from app.minimizer import build_validator
from app.mutator import Mutator, _json_equal, _types_of

client = TestClient(app)


def post(schema, instance, **extra):
    body = {"schema": schema, "instance": instance}
    body.update(extra)
    return client.post("/mutations", json=body)


def first_error_keyword(schema, instance):
    """用与线上相同的校验器求首个错误关键字（验证变异确实触发预期关键字）。"""
    err = next(iter(build_validator(schema).iter_errors(instance)), None)
    return err.validator if err else None


RICH_SCHEMA = {
    "type": "object",
    "required": ["id", "tags", "name"],
    "properties": {
        "id": {"type": "integer", "minimum": 1, "maximum": 100},
        "tags": {"type": "array", "minItems": 2, "items": {"type": "string", "minLength": 2}},
        "name": {"type": "string", "minLength": 3},
        "role": {"enum": ["admin", "user"]},
        "score": {"type": "number", "exclusiveMinimum": 0, "exclusiveMaximum": 1},
        "level": {"const": "junior"},
    },
}
RICH_INSTANCE = {
    "id": 50,
    "tags": ["ab", "cd", "ef"],
    "name": "alice",
    "role": "admin",
    "score": 0.5,
    "level": "junior",
}


# ---- HTTP 层 ---------------------------------------------------------------


def test_mutations_invalid_schema_returns_422():
    resp = post({"type": "not-a-type"}, {})
    assert resp.status_code == 422
    assert resp.json()["error"] == "invalid_schema"


def test_mutations_invalid_instance_returns_422():
    resp = post({"type": "integer"}, "not-an-int")
    assert resp.status_code == 422
    data = resp.json()
    assert data["error"] == "instance_invalid"
    assert data["keyword"] == "type"
    assert data["instance_path"] == ""


def test_mutations_invalid_instance_nested_error_path():
    resp = post({"properties": {"a": {"type": "integer"}}}, {"a": "x"})
    assert resp.status_code == 422
    assert resp.json()["instance_path"] == "/a"


def test_mutations_missing_field_returns_422():
    resp = client.post("/mutations", json={"schema": {"type": "object"}})
    assert resp.status_code == 422


def test_mutations_bad_max_evaluations_returns_422():
    resp = post({"type": "object"}, {}, max_evaluations=0)
    assert resp.status_code == 422


def test_mutations_boolean_schema_true_has_no_constraints():
    resp = post(True, {"a": 1})
    assert resp.status_code == 200
    data = resp.json()
    assert data["mutations"] == []
    assert data["skipped"] == []


def test_mutations_boolean_schema_false_rejects_any_instance():
    resp = post(False, {"a": 1})
    assert resp.status_code == 422
    assert resp.json()["error"] == "instance_invalid"


# ---- 核心保证：每个变异的首个错误关键字与预期一致 -----------------------------


def test_every_mutation_triggers_expected_keyword():
    data = post(RICH_SCHEMA, RICH_INSTANCE).json()
    assert data["mutations"], "应生成若干变异"
    covered = {m["expected_keyword"] for m in data["mutations"]}
    # 需求点名的约束类别全部覆盖到。
    assert {
        "required",
        "type",
        "minItems",
        "minLength",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "enum",
        "const",
    } <= covered
    for m in data["mutations"]:
        # 变异实例必须非法，且首个错误关键字正是声明的关键字。
        assert first_error_keyword(RICH_SCHEMA, m["instance"]) == m["expected_keyword"]
        # 变异实例与原样例不同。
        assert m["instance"] != RICH_INSTANCE
        # seed 是 16 位十六进制可复现标识。
        assert re.fullmatch(r"[0-9a-f]{16}", m["seed"])
    # 每个变异的 seed 互不相同。
    seeds = [m["seed"] for m in data["mutations"]]
    assert len(seeds) == len(set(seeds))
    # index 从 1 开始连续编号。
    assert [m["index"] for m in data["mutations"]] == list(range(1, len(data["mutations"]) + 1))


def test_mutations_are_single_point_changes():
    # 每个 required 变异恰好删除一个属性，其余字段保持原值。
    data = post(RICH_SCHEMA, RICH_INSTANCE).json()
    required_mutations = [m for m in data["mutations"] if m["expected_keyword"] == "required"]
    assert {m["pointer"] for m in required_mutations} == {"/id", "/tags", "/name"}
    for m in required_mutations:
        removed = m["pointer"].lstrip("/")
        expected = {k: v for k, v in RICH_INSTANCE.items() if k != removed}
        assert m["instance"] == expected


# ---- 各关键字的构造正确性 ----------------------------------------------------


def test_minItems_truncates_to_boundary():
    data = post({"type": "array", "minItems": 3}, [1, 2, 3, 4, 5]).json()
    [m] = [m for m in data["mutations"] if m["expected_keyword"] == "minItems"]
    assert m["instance"] == [1, 2]
    assert m["pointer"] == ""


def test_minLength_truncates_to_boundary():
    data = post({"type": "string", "minLength": 4}, "abcdef").json()
    [m] = [m for m in data["mutations"] if m["expected_keyword"] == "minLength"]
    assert m["instance"] == "abc"


def test_maxLength_extends_beyond_boundary():
    data = post({"type": "string", "maxLength": 3}, "ab").json()
    [m] = [m for m in data["mutations"] if m["expected_keyword"] == "maxLength"]
    assert len(m["instance"]) == 4


def test_maxItems_extends_beyond_boundary():
    data = post({"type": "array", "maxItems": 2}, [1, 2]).json()
    [m] = [m for m in data["mutations"] if m["expected_keyword"] == "maxItems"]
    assert len(m["instance"]) == 3


def test_minimum_and_maximum_step_outside():
    data = post({"type": "integer", "minimum": 5, "maximum": 10}, 7).json()
    by_keyword = {m["expected_keyword"]: m for m in data["mutations"]}
    assert by_keyword["minimum"]["instance"] == 4
    assert by_keyword["maximum"]["instance"] == 11


def test_minimum_with_float_boundary_keeps_integer():
    # minimum=5.5 且 type: integer：候选 4.5 会被 type 抢先拦截，
    # 生成器应退到保持整型的 5。
    data = post({"type": "integer", "minimum": 5.5}, 10).json()
    [m] = [m for m in data["mutations"] if m["expected_keyword"] == "minimum"]
    assert m["instance"] == 5


def test_exclusive_boundaries_hit_exact_boundary():
    data = post({"exclusiveMinimum": 5, "exclusiveMaximum": 10}, 7).json()
    by_keyword = {m["expected_keyword"]: m for m in data["mutations"]}
    assert by_keyword["exclusiveMinimum"]["instance"] == 5
    assert by_keyword["exclusiveMaximum"]["instance"] == 10


def test_enum_mutation_value_outside_enum():
    data = post({"enum": ["red", "green"]}, "red").json()
    [m] = [m for m in data["mutations"] if m["expected_keyword"] == "enum"]
    assert m["instance"] not in ["red", "green"]


def test_enum_mutation_respects_sibling_type():
    # 哨兵必须仍是字符串，否则首个错误会变成 type 而不是 enum。
    data = post({"type": "string", "enum": ["a", "b"]}, "a").json()
    [m] = [m for m in data["mutations"] if m["expected_keyword"] == "enum"]
    assert isinstance(m["instance"], str)
    assert m["instance"] not in ["a", "b"]


def test_const_mutation():
    data = post({"const": {"a": 1}}, {"a": 1}).json()
    [m] = [m for m in data["mutations"] if m["expected_keyword"] == "const"]
    assert m["instance"] != {"a": 1}


def test_multipleOf_mutation():
    data = post({"type": "integer", "multipleOf": 3}, 9).json()
    [m] = [m for m in data["mutations"] if m["expected_keyword"] == "multipleOf"]
    assert m["instance"] % 3 != 0


def test_minProperties_and_maxProperties():
    schema = {"type": "object", "minProperties": 2, "maxProperties": 3}
    data = post(schema, {"a": 1, "b": 2}).json()
    by_keyword = {m["expected_keyword"]: m for m in data["mutations"]}
    assert len(by_keyword["minProperties"]["instance"]) == 1
    assert len(by_keyword["maxProperties"]["instance"]) == 4


def test_type_mutation_changes_json_type():
    data = post({"type": "integer"}, 42).json()
    [m] = [m for m in data["mutations"] if m["expected_keyword"] == "type"]
    assert not isinstance(m["instance"], int)


def test_nested_items_and_properties_pointers():
    schema = {
        "type": "array",
        "items": {"type": "object", "required": ["k"], "properties": {"k": {"type": "integer"}}},
    }
    data = post(schema, [{"k": 1, "j": 2}]).json()
    by_pointer = {(m["pointer"], m["expected_keyword"]) for m in data["mutations"]}
    assert ("/0/k", "required") in by_pointer
    assert ("/0/k", "type") in by_pointer
    [m] = [m for m in data["mutations"] if m["pointer"] == "/0/k" and m["expected_keyword"] == "required"]
    assert m["instance"] == [{"j": 2}]


def test_local_ref_is_resolved():
    schema = {
        "$defs": {"positive": {"type": "integer", "minimum": 1}},
        "type": "object",
        "properties": {"count": {"$ref": "#/$defs/positive"}},
    }
    data = post(schema, {"count": 5}).json()
    by_keyword = {m["expected_keyword"] for m in data["mutations"]}
    assert "minimum" in by_keyword
    [m] = [m for m in data["mutations"] if m["expected_keyword"] == "minimum"]
    assert m["pointer"] == "/count"
    assert m["instance"] == {"count": 0}


def test_recursive_ref_terminates():
    schema = {
        "$defs": {
            "node": {
                "type": "object",
                "properties": {"child": {"$ref": "#/$defs/node"}, "v": {"type": "integer"}},
            }
        },
        "$ref": "#/$defs/node",
    }
    instance = {"child": {"v": 1}, "v": 2}
    resp = post(schema, instance)
    assert resp.status_code == 200
    # 递归 $ref 不死循环，且各层 type 变异都生成。
    pointers = {m["pointer"] for m in resp.json()["mutations"] if m["expected_keyword"] == "type"}
    assert "/v" in pointers and "/child/v" in pointers


def test_allOf_is_traversed():
    schema = {"allOf": [{"type": "integer", "minimum": 10}]}
    data = post(schema, 50).json()
    by_keyword = {m["expected_keyword"] for m in data["mutations"]}
    assert {"type", "minimum"} <= by_keyword


def test_draft4_boolean_exclusive_minimum_covered_by_minimum():
    schema = {
        "$schema": "http://json-schema.org/draft-04/schema#",
        "type": "integer",
        "minimum": 5,
        "exclusiveMinimum": True,
    }
    data = post(schema, 10).json()
    [m] = [m for m in data["mutations"] if m["expected_keyword"] == "minimum"]
    # draft-4 下边界违规由 minimum 关键字报告。
    assert m["instance"] == 4
    assert first_error_keyword(schema, m["instance"]) == "minimum"


# ---- 去重 -------------------------------------------------------------------


def test_identical_mutations_are_deduplicated():
    # 同一约束经 allOf 与 properties 两条路径到达，产生完全相同的变异实例。
    schema = {
        "allOf": [{"properties": {"x": {"enum": [1]}}}],
        "properties": {"x": {"enum": [1]}},
    }
    data = post(schema, {"x": 1}).json()
    instances = [json.dumps(m["instance"], sort_keys=True) for m in data["mutations"]]
    assert len(instances) == len(set(instances))
    enum_mutations = [m for m in data["mutations"] if m["expected_keyword"] == "enum"]
    assert len(enum_mutations) == 1


# ---- skipped ----------------------------------------------------------------


def test_minItems_zero_is_skipped():
    data = post({"type": "array", "minItems": 0}, [1]).json()
    assert [m for m in data["mutations"] if m["expected_keyword"] == "minItems"] == []
    skipped = {(s["keyword"], s["pointer"]) for s in data["skipped"]}
    assert ("minItems", "") in skipped


def test_minLength_zero_is_skipped():
    data = post({"type": "string", "minLength": 0}, "abc").json()
    skipped = {s["keyword"] for s in data["skipped"]}
    assert "minLength" in skipped


def test_full_type_union_is_skipped():
    schema = {"type": ["null", "boolean", "integer", "number", "string", "array", "object"]}
    data = post(schema, 1).json()
    assert [m for m in data["mutations"] if m["expected_keyword"] == "type"] == []
    skipped = {s["keyword"] for s in data["skipped"]}
    assert "type" in skipped


def test_pattern_and_uniqueItems_are_reported_unsupported():
    schema = {"type": "object", "properties": {
        "s": {"type": "string", "pattern": "^[a-z]+$"},
        "a": {"type": "array", "uniqueItems": True},
    }}
    data = post(schema, {"s": "abc", "a": [1, 2]}).json()
    skipped = {s["keyword"] for s in data["skipped"]}
    assert "pattern" in skipped
    assert "uniqueItems" in skipped


def test_unconstructible_keyword_falls_into_skipped():
    # additionalProperties: false 抢先拦截任何新键，maxProperties 无法干净触发。
    schema = {
        "type": "object",
        "properties": {"a": {}},
        "additionalProperties": False,
        "maxProperties": 1,
    }
    data = post(schema, {"a": 1}).json()
    assert [m for m in data["mutations"] if m["expected_keyword"] == "maxProperties"] == []
    skipped = {s["keyword"] for s in data["skipped"]}
    assert "maxProperties" in skipped


# ---- 确定性 / seed ------------------------------------------------------------


def test_determinism_same_request_same_response():
    first = post(RICH_SCHEMA, RICH_INSTANCE).json()
    second = post(RICH_SCHEMA, RICH_INSTANCE).json()
    assert first == second


def test_seed_is_reproducible_and_seed_sensitive():
    plain = post(RICH_SCHEMA, RICH_INSTANCE).json()
    seeded_a = post(RICH_SCHEMA, RICH_INSTANCE, seed="run-a").json()
    seeded_a2 = post(RICH_SCHEMA, RICH_INSTANCE, seed="run-a").json()
    seeded_b = post(RICH_SCHEMA, RICH_INSTANCE, seed="run-b").json()
    # 相同 seed 完全复现；不同 seed 改变 seed 标识但不改变变异内容。
    assert seeded_a == seeded_a2
    assert seeded_a["seed"] != seeded_b["seed"]
    assert [m["instance"] for m in seeded_a["mutations"]] == [m["instance"] for m in plain["mutations"]]
    assert [m["seed"] for m in seeded_a["mutations"]] != [m["seed"] for m in plain["mutations"]]
    assert [m["seed"] for m in seeded_a["mutations"]] != [m["seed"] for m in seeded_b["mutations"]]


# ---- 预算 -------------------------------------------------------------------


def test_budget_exhaustion_marks_remaining_skipped():
    data = post(RICH_SCHEMA, RICH_INSTANCE, max_evaluations=3).json()
    assert data["stopped_by_budget"] is True
    assert data["evaluations"] <= 3
    assert len(data["mutations"]) < 22  # 完整预算下是 22 个变异
    budget_skips = [s for s in data["skipped"] if "预算" in s["reason"]]
    assert budget_skips


# ---- 单元：类型集合与 JSON 相等 ----------------------------------------------


def test_types_of_semantics():
    assert _types_of(None) == {"null"}
    assert _types_of(True) == {"boolean"}
    assert _types_of(1) == {"integer", "number"}
    assert _types_of(1.0) == {"integer", "number"}  # jsonschema 视 1.0 为 integer
    assert _types_of(0.5) == {"number"}
    assert _types_of("x") == {"string"}
    assert _types_of([]) == {"array"}
    assert _types_of({}) == {"object"}


def test_json_equal_semantics():
    assert _json_equal(1, 1.0)
    assert not _json_equal(True, 1)
    assert not _json_equal(False, 0)
    assert _json_equal({"a": [1, True]}, {"a": [1.0, True]})
    assert not _json_equal({"a": 1}, {"a": 1, "b": 2})
    assert _json_equal(None, None)


def test_mutator_direct_usage():
    mutator = Mutator(
        {"type": "integer", "minimum": 2},
        10,
        build_validator({"type": "integer", "minimum": 2}),
        seed=None,
        max_evaluations=100,
    )
    assert mutator.evaluate(10) is None  # 样例合法
    result = mutator.generate()
    assert result.evaluations >= 1
    keywords = {m.keyword for m in result.mutations}
    assert {"type", "minimum"} <= keywords
