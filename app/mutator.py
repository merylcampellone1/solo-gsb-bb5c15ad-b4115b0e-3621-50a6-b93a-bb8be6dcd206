"""确定性变异生成器：给定 JSON Schema 与一个合法样例，为各类约束构造最小变异实例。

流程:
1. 沿 schema 的 applicator（``properties`` / ``patternProperties`` /
   ``additionalProperties`` / ``items`` / ``prefixItems`` / ``allOf`` / 本地
   ``$ref``）与样例结构同步遍历，为每个适用的约束关键字生成一个 *变异目标*
   （关键字、JSON Pointer、确定性的候选替换值阶梯）：

   - ``required``：删除单个必填属性（每个必填属性一个目标）；
   - ``type``：替换为确定性的异型哨兵值；
   - ``enum`` / ``const``：替换为取值集合外的确定性哨兵值；
   - ``minimum`` / ``maximum`` / ``exclusiveMinimum`` / ``exclusiveMaximum``：
     取边界外最近值（draft 4 的布尔形式由 ``minimum`` / ``maximum`` 目标覆盖）；
   - ``multipleOf``：破坏整除性的最近值；
   - ``minLength`` / ``maxLength``：截断 / 填充到边界外一个字符；
   - ``minItems`` / ``maxItems``：截断 / 填充到边界外一个元素；
   - ``minProperties`` / ``maxProperties``：删 / 增属性到边界外一个；

2. 每个候选都用真实校验器验证：只有 *首个* 校验错误的关键字与预期一致才被
   接受（与 /minimize 的锁定语义一致），保证返回的变异必然触发声明的关键字；
3. 同一变异实例按规范 JSON 序列化去重；无法构造的关键字记入 ``skipped``。

全程无随机源。每个变异携带由 (schema, instance, seed) 派生的可复现 seed，
相同请求必然得到相同响应。
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Any

from .minimizer import BudgetExhausted, pointer_from_path, set_at

# 异型哨兵候选的固定顺序：null 最简单，与 minimizer 的坍缩哲学一致。
_TYPE_SENTINELS: tuple = (None, True, 0, 0.5, "", [], {})

# 取值集合外哨兵（通用部分），同型哨兵由 _outside_candidates 按实例类型补充。
_GENERIC_SENTINELS: tuple = (None, True, 0, 0.5, "", [], {})

# 遍历到的约束关键字中，识别但暂不支持构造变异的，直接列入 skipped。
_UNSUPPORTED_REASONS = {
    "pattern": "暂不支持为 pattern 构造变异（需要反向构造正则匹配串）",
    "uniqueItems": "暂不支持为 uniqueItems 构造变异",
}


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_num(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _types_of(value: Any) -> frozenset:
    """按 jsonschema 语义返回值所属的 JSON 类型集合（1.0 同时是 number 与 integer）。"""
    if value is None:
        return frozenset({"null"})
    if isinstance(value, bool):
        return frozenset({"boolean"})
    if isinstance(value, int):
        return frozenset({"integer", "number"})
    if isinstance(value, float):
        types = {"number"}
        if value.is_integer():
            types.add("integer")
        return frozenset(types)
    if isinstance(value, str):
        return frozenset({"string"})
    if isinstance(value, list):
        return frozenset({"array"})
    if isinstance(value, dict):
        return frozenset({"object"})
    return frozenset()


def _json_equal(a: Any, b: Any) -> bool:
    """与 jsonschema 的 enum/const 比较语义一致：bool 与 0/1 不相等，1 与 1.0 相等。"""
    if isinstance(a, bool) != isinstance(b, bool):
        return False
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    if isinstance(a, str) and isinstance(b, str):
        return a == b
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_json_equal(x, y) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_json_equal(a[k], b[k]) for k in a)
    return type(a) is type(b) and a == b


def _canonical(value: Any) -> str:
    """规范 JSON 序列化：键排序、无空白，用作去重键与 seed 派生材料。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _brief(value: Any, limit: int = 24) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _outside_candidates(instance_value: Any, forbidden: list) -> list:
    """取值集合外的确定性候选：先同型哨兵，再通用哨兵，过滤掉集合内成员。"""
    same_type: list = []
    if isinstance(instance_value, str):
        same_type = ["", "mutated", instance_value + "!"]
    elif _is_int(instance_value):
        same_type = [instance_value + 1, 0, -1]
    elif isinstance(instance_value, float):
        same_type = [instance_value + 1.0, 0.5, -0.5]
    elif isinstance(instance_value, list):
        same_type = [[], [None]]
    elif isinstance(instance_value, dict):
        same_type = [{}, {"mutated": None}]

    ordered: list = []
    for cand in [*same_type, *_GENERIC_SENTINELS]:
        if any(_json_equal(cand, f) for f in forbidden):
            continue
        if any(_json_equal(cand, seen) for seen in ordered):
            continue
        ordered.append(cand)
    return ordered


def _resolve_local_ref(root: Any, ref: str) -> Any:
    """解析本地 ``$ref``（``#`` 或 ``#/...``），失败返回 None；远程引用不解析。"""
    if ref == "#":
        return root
    if not ref.startswith("#/"):
        return None
    node = root
    for raw in ref[2:].split("/"):
        part = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            return None
    return node


@dataclass
class _Target:
    """一个变异目标：在 ``edit_path`` 处依次尝试候选替换值。"""

    keyword: str
    pointer: str
    edit_path: tuple
    # [(人类可读描述, 替换值)]，按确定性优先级排序。
    candidates: list


@dataclass
class Mutation:
    index: int
    pointer: str
    keyword: str
    instance: Any
    seed: str
    detail: str
    error_pointer: str

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "pointer": self.pointer,
            "expected_keyword": self.keyword,
            "instance": self.instance,
            "seed": self.seed,
            "detail": self.detail,
            "error_pointer": self.error_pointer,
        }


@dataclass
class SkippedKeyword:
    pointer: str
    keyword: str
    reason: str

    def to_dict(self) -> dict:
        return {"pointer": self.pointer, "keyword": self.keyword, "reason": self.reason}


@dataclass
class MutationsResult:
    mutations: list
    skipped: list
    seed: str
    evaluations: int
    stopped_by_budget: bool

    def to_dict(self) -> dict:
        return {
            "mutations": [m.to_dict() for m in self.mutations],
            "skipped": [s.to_dict() for s in self.skipped],
            "seed": self.seed,
            "evaluations": self.evaluations,
            "stopped_by_budget": self.stopped_by_budget,
        }


def _dedup_targets(targets: list) -> list:
    """同一约束经多条 schema 路径（如 allOf + properties）到达时只保留首个目标。"""
    unique: list = []
    seen: set = set()
    for t in targets:
        key = (t.keyword, t.pointer, _canonical([v for _, v in t.candidates]))
        if key not in seen:
            seen.add(key)
            unique.append(t)
    return unique


def _dedup_skipped(skipped: list) -> list:
    unique: list = []
    seen: set = set()
    for s in skipped:
        key = (s.keyword, s.pointer, s.reason)
        if key not in seen:
            seen.add(key)
            unique.append(s)
    return unique


class Mutator:
    """对单个 (schema, instance) 对执行确定性变异生成。"""

    def __init__(
        self,
        schema: Any,
        instance: Any,
        validator,
        *,
        seed: Any = None,
        max_evaluations: int,
    ):
        self.schema = schema
        self.root = instance
        self.validator = validator
        self.max_evaluations = max_evaluations
        self.evaluations = 0
        self.stopped_by_budget = False
        # seed 派生材料固定包含 schema / instance / 请求 seed 三元组：
        # 相同输入必然得到相同 base seed，与是否显式传 seed 无关。
        material = _canonical({"schema": schema, "instance": instance, "seed": seed})
        self.base_seed = hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]

    # ---- 校验 / 预算 -----------------------------------------------------

    def evaluate(self, instance: Any):
        """计一次评估并返回首个校验错误（无错误为 None）；预算耗尽抛 BudgetExhausted。"""
        if self.evaluations >= self.max_evaluations:
            self.stopped_by_budget = True
            raise BudgetExhausted()
        self.evaluations += 1
        return next(iter(self.validator.iter_errors(instance)), None)

    # ---- 主流程 -----------------------------------------------------------

    def generate(self) -> MutationsResult:
        targets: list[_Target] = []
        skipped: list[SkippedKeyword] = []
        self._collect(self.schema, self.root, (), targets, skipped, set())
        targets = _dedup_targets(targets)
        skipped = _dedup_skipped(skipped)

        mutations: list[Mutation] = []
        seen: set = set()
        budget_dead = False
        for target in targets:
            if budget_dead:
                skipped.append(
                    SkippedKeyword(target.pointer, target.keyword, "评估预算耗尽")
                )
                continue
            accepted = False
            duplicate_only = False
            for detail, value in target.candidates:
                mutated = set_at(self.root, target.edit_path, value)
                try:
                    err = self.evaluate(mutated)
                except BudgetExhausted:
                    budget_dead = True
                    skipped.append(
                        SkippedKeyword(target.pointer, target.keyword, "评估预算耗尽")
                    )
                    break
                except Exception:
                    # 校验器对该候选无法运行（如变异后触达坏 $ref）：换下一个候选。
                    continue
                # 只接受首个错误关键字与预期一致的候选（锁定语义）。
                if err is None or err.validator != target.keyword:
                    continue
                key = _canonical(mutated)
                if key in seen:
                    duplicate_only = True
                    continue
                seen.add(key)
                mutations.append(
                    Mutation(
                        index=len(mutations) + 1,
                        pointer=target.pointer,
                        keyword=target.keyword,
                        instance=mutated,
                        seed=self._seed_for(target.keyword, target.pointer, mutated),
                        detail=detail,
                        error_pointer=pointer_from_path(tuple(err.absolute_path)),
                    )
                )
                accepted = True
                break
            if not accepted and not budget_dead:
                reason = (
                    "所有候选变异均与已生成的变异重复"
                    if duplicate_only
                    else "所有候选均未触发预期关键字（可能被其他约束抢先拦截）"
                )
                skipped.append(SkippedKeyword(target.pointer, target.keyword, reason))

        return MutationsResult(
            mutations=mutations,
            skipped=skipped,
            seed=self.base_seed,
            evaluations=self.evaluations,
            stopped_by_budget=self.stopped_by_budget,
        )

    def _seed_for(self, keyword: str, pointer: str, mutated: Any) -> str:
        material = f"{self.base_seed}|{keyword}|{pointer}|{_canonical(mutated)}"
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]

    # ---- 目标收集 -----------------------------------------------------------

    def _collect(
        self,
        node: Any,
        inst: Any,
        path: tuple,
        targets: list,
        skipped: list,
        visited: set,
    ) -> None:
        if not isinstance(node, dict):
            return  # 布尔 schema 没有可遍历的约束。
        ident = (id(node), path)
        if ident in visited:
            return  # $ref 环（如递归 schema）：同一 schema 节点在同一路径只展开一次。
        visited.add(ident)

        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#"):
            resolved = _resolve_local_ref(self.schema, ref)
            if isinstance(resolved, dict):
                self._collect(resolved, inst, path, targets, skipped, visited)

        pointer = pointer_from_path(path)
        self._collect_keywords(node, inst, path, pointer, targets, skipped)
        self._collect_children(node, inst, path, targets, skipped, visited)

    def _collect_keywords(self, node, inst, path, pointer, targets, skipped) -> None:
        def target(keyword, candidates, ptr=None):
            targets.append(_Target(keyword, ptr if ptr is not None else pointer, path, candidates))

        # required：每个必填属性一个目标（删除该属性）。
        required = node.get("required")
        if isinstance(required, list) and isinstance(inst, dict):
            for key in required:
                if not isinstance(key, str) or key not in inst:
                    continue
                reduced = {k: v for k, v in inst.items() if k != key}
                target(
                    "required",
                    [(f"remove required property {key!r}", reduced)],
                    ptr=pointer_from_path(path + (key,)),
                )

        # type：替换为首个类型集合之外的哨兵值。
        declared = node.get("type")
        if isinstance(declared, (str, list)):
            allowed = {declared} if isinstance(declared, str) else set(declared)
            cands = [
                (f"replace {_brief(inst)} with {_brief(s)}", s)
                for s in _TYPE_SENTINELS
                if not (_types_of(s) & allowed)
            ]
            if cands:
                target("type", cands)
            elif allowed:
                skipped.append(
                    SkippedKeyword(pointer, "type", "type 允许全部 JSON 类型，无法构造异型值")
                )

        # enum / const：替换为取值集合外的哨兵值。
        if isinstance(node.get("enum"), list):
            cands = [
                (f"replace {_brief(inst)} with {_brief(c)} (outside enum)", c)
                for c in _outside_candidates(inst, node["enum"])
            ]
            if cands:
                target("enum", cands)
            else:
                skipped.append(
                    SkippedKeyword(pointer, "enum", "无法构造 enum 取值集合外的候选值")
                )
        if "const" in node:
            cands = [
                (f"replace {_brief(inst)} with {_brief(c)} (!= const)", c)
                for c in _outside_candidates(inst, [node["const"]])
            ]
            if cands:
                target("const", cands)
            else:
                skipped.append(
                    SkippedKeyword(pointer, "const", "无法构造与 const 不同的候选值")
                )

        # 数值边界：取边界外最近值；整型样例额外补一个保持整型的候选，
        # 避免与 type: integer 冲突时被 type 关键字抢先拦截。
        minimum = node.get("minimum")
        if _is_num(minimum) and _is_num(inst):
            cands = [(f"set to {minimum - 1} (< minimum {minimum})", minimum - 1)]
            if _is_int(inst):
                nearest = math.ceil(minimum) - 1
                cands.append((f"set to {nearest} (< minimum {minimum})", nearest))
            target("minimum", cands)

        maximum = node.get("maximum")
        if _is_num(maximum) and _is_num(inst):
            cands = [(f"set to {maximum + 1} (> maximum {maximum})", maximum + 1)]
            if _is_int(inst):
                nearest = math.floor(maximum) + 1
                cands.append((f"set to {nearest} (> maximum {maximum})", nearest))
            target("maximum", cands)

        # exclusiveMinimum/Maximum 仅处理 draft 6+ 的数值形式；
        # draft 4 的布尔修饰符由上面的 minimum/maximum 目标覆盖。
        ex_min = node.get("exclusiveMinimum")
        if _is_num(ex_min) and _is_num(inst):
            cands = [(f"set to boundary {ex_min} (exclusiveMinimum)", ex_min)]
            if _is_int(inst) and not float(ex_min).is_integer():
                nearest = math.floor(ex_min)
                cands.append((f"set to {nearest} (<= exclusiveMinimum {ex_min})", nearest))
            target("exclusiveMinimum", cands)

        ex_max = node.get("exclusiveMaximum")
        if _is_num(ex_max) and _is_num(inst):
            cands = [(f"set to boundary {ex_max} (exclusiveMaximum)", ex_max)]
            if _is_int(inst) and not float(ex_max).is_integer():
                nearest = math.ceil(ex_max)
                cands.append((f"set to {nearest} (>= exclusiveMaximum {ex_max})", nearest))
            target("exclusiveMaximum", cands)

        multiple = node.get("multipleOf")
        if _is_num(multiple) and _is_num(inst):
            cands = []
            for cand in (inst + 1, inst - 1, inst + multiple / 2, inst + 0.5):
                if not any(_json_equal(cand, c) for _, c in cands):
                    cands.append((f"set to {cand} (not a multiple of {multiple})", cand))
            target("multipleOf", cands)

        min_length = node.get("minLength")
        if _is_int(min_length) and isinstance(inst, str):
            if min_length == 0:
                skipped.append(
                    SkippedKeyword(pointer, "minLength", "minLength 为 0，无法构造更短的字符串")
                )
            else:
                target(
                    "minLength",
                    [(f"truncate string {len(inst)} -> {min_length - 1} chars", inst[: min_length - 1])],
                )

        max_length = node.get("maxLength")
        if _is_int(max_length) and isinstance(inst, str):
            pad = max_length + 1 - len(inst)  # 合法样例保证 pad > 0
            cands = [(f"extend string {len(inst)} -> {max_length + 1} chars", inst + "x" * pad)]
            if inst:
                # 重复末字符 / 重复整体：更有可能保住 pattern 等其他约束。
                cands.append((f"extend string {len(inst)} -> {max_length + 1} chars", inst + inst[-1] * pad))
                repeated = (inst * (pad // len(inst) + 2))[: max_length + 1]
                cands.append((f"extend string {len(inst)} -> {max_length + 1} chars", repeated))
            target("maxLength", cands)

        min_items = node.get("minItems")
        if _is_int(min_items) and isinstance(inst, list):
            if min_items == 0:
                skipped.append(
                    SkippedKeyword(pointer, "minItems", "minItems 为 0，无法构造更短的数组")
                )
            else:
                target(
                    "minItems",
                    [(f"truncate array {len(inst)} -> {min_items - 1} items", inst[: min_items - 1])],
                )

        max_items = node.get("maxItems")
        if _is_int(max_items) and isinstance(inst, list):
            pad = max_items + 1 - len(inst)  # 合法样例保证 pad > 0
            cands = []
            if inst:
                cands.append((f"extend array {len(inst)} -> {max_items + 1} items", inst + [copy.deepcopy(inst[-1])] * pad))
            for fill in (None, 0, ""):
                cands.append((f"extend array {len(inst)} -> {max_items + 1} items", inst + [fill] * pad))
            target("maxItems", cands)

        min_props = node.get("minProperties")
        if _is_int(min_props) and isinstance(inst, dict):
            if min_props == 0:
                skipped.append(
                    SkippedKeyword(pointer, "minProperties", "minProperties 为 0，无法构造更少属性的对象")
                )
            else:
                keys = list(inst.keys())
                cands = [
                    (
                        f"remove properties {len(inst)} -> {min_props - 1}",
                        {k: inst[k] for k in keys[: min_props - 1]},
                    )
                ]
                # 优先保留 required 键的候选：避免被 required 关键字抢先拦截。
                req_keys = [
                    k for k in node.get("required", [])
                    if isinstance(k, str) and k in inst
                ] if isinstance(node.get("required"), list) else []
                if req_keys:
                    keep = req_keys + [k for k in keys if k not in req_keys][: max(min_props - 1 - len(req_keys), 0)]
                    if len(keep) == min_props - 1:
                        cands.append((
                            f"remove properties {len(inst)} -> {min_props - 1} (keep required)",
                            {k: inst[k] for k in keep},
                        ))
                target("minProperties", cands)

        max_props = node.get("maxProperties")
        if _is_int(max_props) and isinstance(inst, dict):
            need = max_props + 1 - len(inst)  # 合法样例保证 need > 0
            fresh: list[str] = []
            i = 0
            while len(fresh) < need:
                name = f"k{i}"
                if name not in inst:
                    fresh.append(name)
                i += 1
            cands = [
                (
                    f"add properties {len(inst)} -> {max_props + 1}",
                    {**inst, **{k: fill for k in fresh}},
                )
                for fill in (None, 0, "")
            ]
            target("maxProperties", cands)

        # 识别但暂不支持的约束：明示跳过原因。
        for keyword, reason in _UNSUPPORTED_REASONS.items():
            present = node.get(keyword)
            applies = (
                (keyword == "pattern" and isinstance(present, str) and isinstance(inst, str))
                or (keyword == "uniqueItems" and present is True and isinstance(inst, list))
            )
            if applies:
                skipped.append(SkippedKeyword(pointer, keyword, reason))

    def _collect_children(self, node, inst, path, targets, skipped, visited) -> None:
        if isinstance(inst, dict):
            props = node.get("properties")
            if isinstance(props, dict):
                for key, sub in props.items():
                    if isinstance(key, str) and key in inst:
                        self._collect(sub, inst[key], path + (key,), targets, skipped, visited)

            compiled = []
            patterns = node.get("patternProperties")
            if isinstance(patterns, dict):
                for pattern, sub in patterns.items():
                    try:
                        rx = re.compile(pattern)
                    except re.error:
                        continue  # 非法正则：校验阶段同样无法运行，遍历直接跳过。
                    compiled.append(rx)
                    for key in inst:
                        if rx.search(key):
                            self._collect(sub, inst[key], path + (key,), targets, skipped, visited)

            additional = node.get("additionalProperties")
            if isinstance(additional, dict):
                declared_keys = set(props) if isinstance(props, dict) else set()
                for key in inst:
                    if key in declared_keys:
                        continue
                    if any(rx.search(key) for rx in compiled):
                        continue
                    self._collect(additional, inst[key], path + (key,), targets, skipped, visited)

        if isinstance(inst, list):
            prefix = node.get("prefixItems")
            if isinstance(prefix, list):
                for i, sub in enumerate(prefix[: len(inst)]):
                    self._collect(sub, inst[i], path + (i,), targets, skipped, visited)

            items = node.get("items")
            if isinstance(items, dict):
                # 2020-12：items 作用于 prefixItems 之后；更早 draft 作用于全部元素。
                start = len(prefix) if isinstance(prefix, list) else 0
                for i in range(start, len(inst)):
                    self._collect(items, inst[i], path + (i,), targets, skipped, visited)
            elif isinstance(items, list):
                # 旧版 tuple form（draft 2019-09 及以前）。
                for i, sub in enumerate(items[: len(inst)]):
                    self._collect(sub, inst[i], path + (i,), targets, skipped, visited)

        all_of = node.get("allOf")
        if isinstance(all_of, list):
            for sub in all_of:
                self._collect(sub, inst, path, targets, skipped, visited)
