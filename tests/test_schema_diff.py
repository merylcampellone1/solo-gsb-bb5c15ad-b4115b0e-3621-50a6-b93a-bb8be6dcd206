"""POST /schema-diff 的端到端 / 单元测试。运行：python -m pytest -q"""

from fastapi.testclient import TestClient

from app.main import app
from app.minimizer import build_validator
from app.differ import DiffMinimizer

client = TestClient(app)


def run(old_schema, new_schema, instance, max_evaluations=1000):
    return client.post(
        "/schema-diff",
        json={
            "old_schema": old_schema,
            "new_schema": new_schema,
            "instance": instance,
            "max_evaluations": max_evaluations,
        },
    )


def status(schema, instance):
    err = next(iter(build_validator(schema).iter_errors(instance)), None)
    return "reject" if err else "pass"


# ---- HTTP 层 ---------------------------------------------------------------


def test_healthz_still_ok():
    assert client.get("/healthz").json() == {"status": "ok"}


def test_missing_fields_returns_422():
    resp = client.post(
        "/schema-diff",
        json={"old_schema": {"type": "integer"}},
    )
    assert resp.status_code == 422


def test_bad_max_evaluations_returns_422():
    resp = run({"type": "integer"}, {"type": "integer"}, 1, max_evaluations=0)
    assert resp.status_code == 422


def test_invalid_old_schema_returns_422():
    resp = run({"type": "not-a-type"}, {"type": "integer"}, 1)
    assert resp.status_code == 422
    data = resp.json()
    assert data["error"] == "invalid_schema"
    assert data["which"] == "old_schema"


def test_invalid_new_schema_returns_422():
    resp = run({"type": "integer"}, {"type": "not-a-type"}, 1)
    assert resp.status_code == 422
    data = resp.json()
    assert data["error"] == "invalid_schema"
    assert data["which"] == "new_schema"


def test_unresolvable_ref_schema_returns_422():
    # check_schema 能过，但实际校验时解析 $ref 失败：旧版不可用 -> 422。
    resp = run({"$ref": "#/definitions/missing"}, True, {})
    assert resp.status_code == 422
    assert resp.json()["which"] == "old_schema"


# ---- 两版结果相同：不缩减 ---------------------------------------------------


def test_both_reject_no_reduction():
    old = {"type": "integer"}
    new = {"type": "integer", "minimum": 10}
    resp = run(old, new, "not-an-int")
    assert resp.status_code == 200
    data = resp.json()
    assert data["diverges"] is False
    assert data["direction"] is None
    assert data["minimal_instance"] is None
    # 仅对原实例做一次双版评估。
    assert data["evaluations"] == 1
    assert data["stopped_by_budget"] is False
    assert data["old"]["status"] == "reject"
    assert data["new"]["status"] == "reject"
    assert len(data["trace"]) == 1 and data["trace"][0]["op"] == "lock"


def test_both_pass_no_reduction():
    resp = run({}, {"type": "object"}, {"a": 1})
    data = resp.json()
    assert data["diverges"] is False
    assert data["direction"] is None
    assert data["minimal_instance"] is None
    assert data["old"]["status"] == "pass"
    assert data["new"]["status"] == "pass"
    assert data["evaluations"] == 1


# ---- 结果相反：缩减差异见证 -------------------------------------------------


def test_new_accepts_collapses_to_empty_object():
    # 旧版缺 id 即拒绝，新版无约束。null 会让旧版改报 type 但仍然拒绝，
    # 组合 (旧拒, 新过) 依旧保持，因此见证一路坍缩到 null。
    old = {
        "type": "object",
        "required": ["id"],
        "properties": {"id": {"type": "integer"}},
    }
    new = {}
    instance = {"tags": ["a" * 20, "b" * 20], "extra": {"deep": [1, 2, 3, 4]}}
    data = run(old, new, instance).json()
    assert data["diverges"] is True
    assert data["direction"] == "new_accepts"
    assert data["old"]["status"] == "reject"
    assert data["old"]["keyword"] == "required"
    assert data["old"]["error_path"] == ""
    assert data["new"]["status"] == "pass"
    assert data["new"]["keyword"] is None
    assert data["minimal_instance"] is None
    assert data["stopped_by_budget"] is False


def test_new_rejects_keeps_shortest_array_witness():
    # 旧版只要求 array：null 会让旧版也拒绝，组合 (旧过, 新拒) 被破坏，
    # 因此最短见证是 [] 而不是 null。
    old = {"type": "array"}
    new = {"type": "array", "minItems": 3}
    instance = [10, 20]
    data = run(old, new, instance).json()
    assert data["direction"] == "new_rejects"
    assert data["minimal_instance"] == []
    # 缩减后的见证仍保持原有两版组合。
    assert status(old, []) == "pass"
    assert status(new, []) == "reject"


def test_boolean_schema_pair():
    data = run(False, True, {"a": [1, 2, 3]}).json()
    assert data["old"]["status"] == "reject"
    assert data["new"]["status"] == "pass"
    assert data["direction"] == "new_accepts"
    # false 对任何实例都拒绝、true 对任何实例都通过，null 即最小见证。
    assert data["minimal_instance"] is None


def test_every_kept_candidate_preserves_combination():
    old = {
        "type": "object",
        "required": ["id"],
        "properties": {"id": {"type": "integer"}},
    }
    new = {}
    instance = {"tags": ["a", "b"], "nested": {"x": 1}}
    data = run(old, new, instance).json()

    def combo(candidate):
        return (status(old, candidate), status(new, candidate))

    original_combo = combo(instance)
    assert original_combo == ("reject", "pass")
    # 每个被接受的候选（含 lock 对应的原实例）都必须保持两版组合。
    for entry in data["trace"]:
        assert entry["old"]["status"] in {"pass", "reject"}
        assert entry["new"]["status"] in {"pass", "reject"}
        if entry["kept"]:
            assert (entry["old"]["status"], entry["new"]["status"]) == original_combo
    # 最终见证同样保持组合。
    assert combo(data["minimal_instance"]) == original_combo


def test_trace_is_paired_and_contiguous():
    old = {"type": "array"}
    new = {"type": "array", "minItems": 2}
    data = run(old, new, [1]).json()
    trace = data["trace"]
    assert trace[0]["op"] == "lock"
    for i, entry in enumerate(trace, start=1):
        assert entry["index"] == i
        assert set(entry["old"]) == {"status", "keyword", "error_path", "message"}
        assert set(entry["new"]) == {"status", "keyword", "error_path", "message"}
    assert data["evaluations"] == len(trace)


def test_budget_one_evaluation_keeps_original_and_flags():
    old = {"type": "array"}
    new = {"type": "array", "minItems": 3}
    instance = [1, 2]
    data = run(old, new, instance, max_evaluations=1).json()
    assert data["diverges"] is True
    assert data["direction"] == "new_rejects"
    assert data["evaluations"] == 1
    assert data["stopped_by_budget"] is True
    # 预算在 lock 后立即耗尽，实例原样返回。
    assert data["minimal_instance"] == [1, 2]


def test_determinism():
    old = {"type": "object", "required": ["a", "b", "c"]}
    new = {"type": "object"}
    instance = {"a": "v", "b": [1, 2, 3, 4], "c": 42, "d": {"k": "v"}, "e": None}
    first = run(old, new, instance).json()
    second = run(old, new, instance).json()
    assert first["minimal_instance"] == second["minimal_instance"]
    assert first["trace"] == second["trace"]


# ---- 直接测试 DiffMinimizer -----------------------------------------------


def test_diff_minimizer_direct_use():
    old_v = build_validator({"type": "array"})
    new_v = build_validator({"type": "array", "minItems": 4})
    dm = DiffMinimizer(old_v, new_v, [1, 2, 3], max_evaluations=100)
    old_err, new_err = dm.lock()
    assert old_err is None
    assert new_err is not None and new_err.validator == "minItems"
    assert dm.diverges is True
    assert dm.direction == "new_rejects"
    result = dm.minimize()
    assert result.minimal_instance == []
    assert result.stopped_by_budget is False
    assert result.evaluations == len(result.trace)
