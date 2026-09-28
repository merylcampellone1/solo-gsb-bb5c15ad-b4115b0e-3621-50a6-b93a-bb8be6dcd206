"""Deterministic delta-debugging minimizer for JSON Schema counterexamples.

流程:
1. 对原始实例做首次校验，锁定 *首个* 校验错误的 ``keyword``；
2. 以该关键字作为 "有趣性" 判定：任何缩减后的候选只有在仍然触发
   同一关键字时才被接受；
3. 使用确定性的分层 delta debugging：
   - 坍缩：先尝试同类型空值（``[]`` / ``{}`` / ``""``）与 ``null``；
   - 标量：按固定的候选阶梯简化（false / 0 / 减半 …）；
   - 对象：经典 ddmin 删除字段块，随后递归处理每个剩余字段；
   - 数组：经典 ddmin 删除连续区间块，随后递归处理每个剩余元素；
   - 字符串：对标量阶梯无效的场景（如 maxLength）按字符块做 ddmin；
4. 外层不断迭代直到一轮完整遍历不再发生任何变化（不动点）。

整个过程不使用任何随机源，对相同输入必然得到相同结果。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from jsonschema import Draft201909Validator, Draft202012Validator, Draft3Validator, Draft4Validator, Draft6Validator, Draft7Validator

# 支持的 draft 识别（依据 schema 内的 $schema）。bool schema（{} / false）
# 统一按 Draft 2020-12 处理。
_VALIDATORS = (
    Draft3Validator,
    Draft4Validator,
    Draft6Validator,
    Draft7Validator,
    Draft201909Validator,
    Draft202012Validator,
)


def build_validator(schema: Any):
    """根据 ``$schema`` 选择校验器，缺省使用 Draft 2020-12。"""
    meta = schema.get("$schema") if isinstance(schema, dict) else None
    cls = Draft202012Validator
    if isinstance(meta, str):
        for candidate in _VALIDATORS:
            if meta == candidate.META_SCHEMA.get("$schema"):
                cls = candidate
                break
    # 提前抛出 schema 自身不合法的问题。
    cls.check_schema(schema)
    return cls(schema)


class BudgetExhausted(Exception):
    """评估次数预算耗尽（内部控制流异常）。"""


class SchemaUnusable(Exception):
    """schema 能通过元校验，但实际执行一次校验时即失败（坏 $ref / 非法正则等）。"""

    def __init__(self, side: str, original: Exception):
        self.side = side
        self.original = original
        super().__init__(f"{side}: {original}")


# 锁定前的哨兵目标：任何实际关键字（含 None）都不会与它相等。
_UNLOCKED = object()


@dataclass
class TraceEntry:
    """单次评估的轨迹记录。"""

    index: int
    op: str
    path: str
    detail: str
    # 候选是否仍触发锁定关键字并因此被接受为新的当前实例。
    kept: bool
    # 候选触发的首个错误关键字；候选变为合法实例时为 None。
    triggered_keyword: str | None = None
    # 触发错误时错误对象所在的 JSON Pointer（可能为空串表示根）。
    error_path: str | None = None
    message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "op": self.op,
            "path": self.path,
            "detail": self.detail,
            "kept": self.kept,
            "triggered_keyword": self.triggered_keyword,
            "error_path": self.error_path,
            "message": self.message,
        }


@dataclass
class MinimizeResult:
    minimal_instance: Any
    original_instance: Any
    target_keyword: Any
    evaluations: int
    stopped_by_budget: bool
    trace: list[TraceEntry] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "minimal_instance": self.minimal_instance,
            "original_instance": self.original_instance,
            "target_keyword": self.target_keyword,
            "evaluations": self.evaluations,
            "stopped_by_budget": self.stopped_by_budget,
            "trace": [entry.to_dict() for entry in self.trace],
        }


def pointer_from_path(path: tuple) -> str:
    """把内部路径元组编码成 JSON Pointer（RFC 6901）。"""
    return "" if not path else "/" + "/".join(
        str(p).replace("~", "~0").replace("/", "~1") for p in path
    )


def get_at(root: Any, path: tuple) -> Any:
    node = root
    for p in path:
        node = node[p]
    return node


def set_at(root: Any, path: tuple, value: Any) -> Any:
    """不可变式地把 ``path`` 处的值替换为 ``value``，返回新的根。"""
    if not path:
        return value
    head, rest = path[0], path[1:]
    if isinstance(root, dict):
        new_root = dict(root)
        new_root[head] = set_at(root[head], rest, value)
        return new_root
    if isinstance(root, list):
        new_root = list(root)
        new_root[head] = set_at(root[head], rest, value)
        return new_root
    raise TypeError(f"cannot descend into {type(root).__name__}")


def _scalar_candidates(value: Any) -> list[Any]:
    """按固定优先级返回标量简化候选（不含与原值完全相等者）。

    跨类型的 ``null`` 与空串由 :func:`_collapse_candidates` 统一负责，
    这里只处理同类型标量阶梯，避免与坍缩阶段重复评估。
    """

    def tag(v: Any):
        # bool 必须在 int 之前判定；用 (类型标识, 表示) 去重。
        if isinstance(v, bool):
            return ("bool", v)
        if isinstance(v, int):
            return ("int", v)
        if isinstance(v, float):
            return ("float", v)
        return (type(v).__name__, v)

    original = tag(value)
    ordered: list[Any] = []
    seen: set = set()

    def push(cand: Any) -> None:
        t = tag(cand)
        if t != original and t not in seen:
            seen.add(t)
            ordered.append(cand)

    if isinstance(value, bool):
        push(False)
    elif isinstance(value, int):
        for cand in (0, 1, -1):
            push(cand)
        mag = abs(value)
        while mag > 1:
            mag //= 2
            push(mag)
            push(-mag)
    elif isinstance(value, float):
        if value == value and value not in (float("inf"), float("-inf")):
            for cand in (0.0, 1.0, -1.0):
                push(cand)
            mag = abs(value)
            while mag > 1.0:
                mag /= 2.0
                push(mag)
                push(-mag)
    elif isinstance(value, str) and len(value) > 1:
        # 单字符通常就是字符串标量缩减的常见终点；空串由坍缩阶段负责。
        push(value[:1])
    return ordered


def _collapse_candidates(value: Any) -> list[Any]:
    """结构坍缩候选：先同类型空值，再 null。"""
    cands: list[Any] = []
    if isinstance(value, (dict, list, str)) and len(value) > 0:
        cands.append({} if isinstance(value, dict) else [] if isinstance(value, list) else "")
    if value is not None:
        cands.append(None)
    return cands


def _scalar_rank(value: Any) -> tuple:
    """标量的良基复杂度序：被接受的新值必须严格更小，杜绝振荡。"""
    if value is None:
        return (0,)
    if isinstance(value, bool):
        return (1, int(value))
    if isinstance(value, int):
        return (2, 0 if value == 0 else 1, abs(value))
    if isinstance(value, float):
        return (3, 0 if value == 0.0 else 1, abs(value))
    if isinstance(value, str):
        return (4, len(value))
    return (5,)


class Minimizer:
    """对单个 (schema, instance) 对执行最小化。"""

    def __init__(self, validator, instance: Any, max_evaluations: int):
        self.validator = validator
        self.original = instance
        self.root = instance
        self.max_evaluations = max_evaluations
        self.evaluations = 0
        self.trace: list[TraceEntry] = []
        # 锁定前使用哨兵，保证第一次 lock 评估不会误判为"命中关键字"。
        self.target_keyword: Any = _UNLOCKED
        self.stopped_by_budget = False

    # ---- 校验 / 预算 -----------------------------------------------------

    def _spend(self) -> None:
        """消耗一次完整 JSON 校验的评估配额；耗尽则抛 ``BudgetExhausted``。"""
        if self.evaluations >= self.max_evaluations:
            self.stopped_by_budget = True
            raise BudgetExhausted()
        self.evaluations += 1

    def _first_error(self, instance: Any, validator=None):
        # iter_errors 按 schema/instance 的固定遍历顺序深度优先产出错误，
        # 结合锁定的依赖版本，结果完全确定。
        validator = self.validator if validator is None else validator
        return next(iter(validator.iter_errors(instance)), None)

    def _is_kept(self, err: Any) -> bool:
        """有趣性判据：候选的首个错误是否仍命中锁定关键字。

        子类（如双 schema 差异最小化）可覆盖为其他保持条件。
        """
        return (
            err is not None
            and self.target_keyword is not _UNLOCKED
            and err.validator == self.target_keyword
        )

    def evaluate(self, candidate: Any, op: str, path: tuple, detail: str) -> tuple[Any, bool]:
        """评估一次候选，记录轨迹；命中预算则抛出 ``BudgetExhausted``。

        返回 ``(首个错误对象或 None, 是否满足有趣性)``。
        锁定（lock）阶段第二个值恒为 False。
        """
        self._spend()
        err = self._first_error(candidate)
        index = self.evaluations
        kept = self._is_kept(err)
        if err is None:
            self.trace.append(
                TraceEntry(index, op, pointer_from_path(path), detail, False)
            )
            return None, False
        self.trace.append(
            TraceEntry(
                index,
                op,
                pointer_from_path(path),
                detail,
                kept,
                err.validator,
                pointer_from_path(tuple(err.absolute_path)),
                err.message,
            )
        )
        return err, kept

    def lock(self) -> Any:
        """第 1 次评估：校验原始实例，返回首个错误对象（合法则为 None）。

        实例非法时顺带把首个错误关键字锁定为 :attr:`target_keyword`；
        ``false`` 这类布尔 schema 的 ``validator`` 为 None，同样合法。
        """
        err = self.evaluate(self.root, "lock", (), "validate original instance")[0]
        if err is not None:
            self.target_keyword = err.validator
        return err

    # ---- 主循环 -----------------------------------------------------------

    def minimize(self) -> MinimizeResult:
        while True:
            changed_holder = [False]
            try:
                self._visit((), changed_holder)
            except BudgetExhausted:
                break
            if not changed_holder[0]:
                break
        return MinimizeResult(
            minimal_instance=self.root,
            original_instance=self.original,
            target_keyword=self.target_keyword,
            evaluations=self.evaluations,
            stopped_by_budget=self.stopped_by_budget,
            trace=self.trace,
        )

    def _visit(self, path: tuple, changed_holder: list[bool]) -> None:
        node = get_at(self.root, path)

        # 1) 结构坍缩：同类型空值 / null。
        if self._collapse(path, node, changed_holder):
            return

        # 2) 标量阶梯（字符串的单字符尝试也在这里）。
        if isinstance(node, (bool, int, float, str)):
            if self._simplify_scalar(path, node, changed_holder):
                return

        # 3) 容器自身的 delta debugging（删字段块 / 数组块 / 字符块）。
        node = get_at(self.root, path)
        if isinstance(node, dict):
            self._ddmin(path, kind="object", changed_holder=changed_holder)
        elif isinstance(node, list):
            self._ddmin(path, kind="array", changed_holder=changed_holder)
        elif isinstance(node, str) and len(node) >= 2:
            self._ddmin(path, kind="string", changed_holder=changed_holder)

        # 4) 递归处理剩余的子节点；从最新 root 重新取节点。
        node = get_at(self.root, path)
        if isinstance(node, dict):
            for key in list(node.keys()):
                self._visit(path + (key,), changed_holder)
        elif isinstance(node, list):
            for i in range(len(node)):
                self._visit(path + (i,), changed_holder)

    # ---- 坍缩 -------------------------------------------------------------

    def _collapse(self, path: tuple, node: Any, changed_holder: list[bool]) -> bool:
        for cand in _collapse_candidates(node):
            detail = f"collapse {self._describe_type(node)} -> {self._describe(cand)}"
            candidate_root = set_at(self.root, path, cand)
            if self.evaluate(candidate_root, "collapse", path, detail)[1]:
                self.root = candidate_root
                changed_holder[0] = True
                return True
        return False

    @staticmethod
    def _describe_type(value: Any) -> str:
        if isinstance(value, dict):
            return f"object({len(value)})"
        if isinstance(value, list):
            return f"array({len(value)})"
        if isinstance(value, str):
            return f"string({len(value)})"
        return Minimizer._describe(value)

    # ---- 标量 -------------------------------------------------------------

    def _simplify_scalar(self, path: tuple, node: Any, changed_holder: list[bool]) -> bool:
        current_rank = _scalar_rank(node)
        for cand in _scalar_candidates(node):
            # 只尝试严格更小的候选，保证复杂度单调下降。
            if _scalar_rank(cand) >= current_rank:
                continue
            detail = f"scalar ladder {self._describe(node)} -> {self._describe(cand)}"
            candidate_root = set_at(self.root, path, cand)
            if self.evaluate(candidate_root, "scalar", path, detail)[1]:
                self.root = candidate_root
                changed_holder[0] = True
                # 命中一个候选即停：访问者下一轮会继续简化新值。
                return True
        return False

    @staticmethod
    def _describe(value: Any) -> str:
        if value is None:
            return "null"
        if isinstance(value, str):
            shown = value if len(value) <= 20 else value[:17] + "..."
            return repr(shown)
        if isinstance(value, (dict, list)) and len(value) == 0:
            return "{}" if isinstance(value, dict) else "[]"
        return str(value)

    # ---- 经典 ddmin（Zeller）作用于键/索引序列 ---------------------------

    def _ddmin(
        self,
        path: tuple,
        *,
        kind: str,
        changed_holder: list[bool],
    ) -> None:
        n = 2
        while True:
            # 每次迭代都从最新父节点生成规范标签：对象是键的有序序列，
            # 数组/字符串是 0..len-1 的位置序列。
            parent = get_at(self.root, path)
            items = list(parent.keys()) if kind == "object" else list(range(len(parent)))
            if len(items) < 2:
                break
            chunks = self._split(items, n)
            reduction = False

            # 逐块删除。
            for chunk in chunks:
                removed = set(chunk)
                remaining = [it for it in items if it not in removed]
                candidate = self._rebuild(path, kind, remaining)
                detail = self._chunk_detail(kind, chunk, action="remove")
                if self.evaluate(candidate, f"ddmin-{kind}", path, detail)[1]:
                    self.root = candidate
                    items = remaining
                    changed_holder[0] = True
                    n = max(n - 1, 2)
                    reduction = True
                    break

            if reduction:
                continue

            # 补集测试（只保留一个块）；接受后下一轮按新父节点重新编号。
            if len(chunks) >= 2:
                for chunk in chunks:
                    candidate = self._rebuild(path, kind, list(chunk))
                    detail = self._chunk_detail(kind, chunk, action="keep")
                    if self.evaluate(candidate, f"ddmin-{kind}", path, detail)[1]:
                        self.root = candidate
                        changed_holder[0] = True
                        n = max(len(chunk), 2)
                        reduction = True
                        break

            if reduction:
                continue

            # 无法再缩减：增加粒度后重试，直到块数等于元素数。
            if n < len(items):
                n = min(len(items), n * 2)
            else:
                break

    def _rebuild(self, path: tuple, kind: str, remaining: list) -> Any:
        # 始终基于最新 root 取父节点（上一次接受可能已经缩短了容器）。
        parent = get_at(self.root, path)
        if kind == "object":
            new_node: Any = {k: parent[k] for k in remaining}
        elif kind == "array":
            new_node = [parent[i] for i in remaining]
        else:  # string
            new_node = "".join(parent[i] for i in remaining)
        return set_at(self.root, path, new_node)

    @staticmethod
    def _split(items: list, n: int) -> list[list]:
        """把有序序列切成 n 个尽量等长的连续块。"""
        length = len(items)
        n = max(1, min(n, length))
        base, extra = divmod(length, n)
        chunks: list[list] = []
        start = 0
        for i in range(n):
            size = base + (1 if i < extra else 0)
            chunks.append(items[start : start + size])
            start += size
        return chunks

    @staticmethod
    def _chunk_detail(kind: str, chunk: list, *, action: str) -> str:
        if kind == "object":
            names = ", ".join(repr(k) for k in chunk)
            return f"{action} fields [{names}]"
        if kind == "string":
            target = "characters"
        else:
            target = "elements"
        if len(chunk) == 1:
            where = f"{target} index {chunk[0]}"
        else:
            where = f"{target} indices {chunk[0]}..{chunk[-1]}"
        return f"{action} {where}"


# ---- 双 schema 差异见证最小化 ----------------------------------------------


def error_info(err: Any) -> dict[str, Any] | None:
    """把首个校验错误序列化为响应中的 JSON 结构；合法时为 None。"""
    if err is None:
        return None
    return {
        "keyword": err.validator,
        "instance_path": pointer_from_path(tuple(err.absolute_path)),
        "schema_path": pointer_from_path(tuple(err.absolute_schema_path)),
        "message": err.message,
    }


@dataclass
class DiffTraceEntry:
    """单次候选评估（同时校验新旧两版 schema）的轨迹记录。"""

    index: int
    op: str
    path: str
    detail: str
    kept: bool
    old_valid: bool
    new_valid: bool
    old_keyword: str | None = None
    new_keyword: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "op": self.op,
            "path": self.path,
            "detail": self.detail,
            "kept": self.kept,
            "old_valid": self.old_valid,
            "new_valid": self.new_valid,
            "old_keyword": self.old_keyword,
            "new_keyword": self.new_keyword,
        }


@dataclass
class SchemaDiffResult:
    reduced_instance: Any
    original_instance: Any
    differs: bool
    direction: str | None
    old_valid: bool
    new_valid: bool
    old_first_error: dict | None
    new_first_error: dict | None
    evaluations: int
    stopped_by_budget: bool
    trace: list = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "reduced_instance": self.reduced_instance,
            "original_instance": self.original_instance,
            "differs": self.differs,
            "direction": self.direction,
            "old_valid": self.old_valid,
            "new_valid": self.new_valid,
            "old_first_error": self.old_first_error,
            "new_first_error": self.new_first_error,
            "evaluations": self.evaluations,
            "stopped_by_budget": self.stopped_by_budget,
            "trace": [entry.to_dict() for entry in self.trace],
        }


class SchemaDiffMinimizer(Minimizer):
    """对 (old_schema, new_schema, instance) 做差异见证最小化。

    有趣性不再是"仍触发同一关键字"，而是"新旧两版的通过/拒绝组合与原始
    组合完全一致"——即候选必须保持两版结果仍然相反。缩减算子（坍缩 /
    标量阶梯 / ddmin / 递归不动点）与预算记账全部沿用 :class:`Minimizer`。

    每个候选需要执行两次完整 JSON 校验（旧版、新版各一次），各计 1 次
    评估；首次锁定校验同样消耗 2 次评估。
    """

    def __init__(self, old_validator, new_validator, instance: Any, max_evaluations: int):
        super().__init__(None, instance, max_evaluations)
        self.old_validator = old_validator
        self.new_validator = new_validator
        # 锁定的目标组合 (old_valid, new_valid)；结果相反时才进入缩减。
        self.target_pair: tuple[bool, bool] | None = None
        # 当前 root 最近一次校验得到的错误对（lock 后初始化，接受候选时更新）。
        self.root_old_err: Any = None
        self.root_new_err: Any = None

    # ---- 校验 / 预算（每次评估 = 旧版 + 新版各一次完整校验）----------------

    def _check_side(self, validator, candidate: Any, side: str):
        try:
            return self._first_error(candidate, validator)
        except BudgetExhausted:
            raise
        except Exception as exc:
            # schema 已通过 check_schema，但实际校验对该实例无法运行
            # （坏 $ref、非法正则等）：按不可用 schema 处理。
            raise SchemaUnusable(side, exc) from exc

    def evaluate(self, candidate: Any, op: str, path: tuple, detail: str) -> tuple[Any, bool]:
        """返回 ``((旧版首个错误或 None, 新版首个错误或 None), 是否保持差异组合)``。"""
        self._spend()
        old_err = self._check_side(self.old_validator, candidate, "old")
        self._spend()
        new_err = self._check_side(self.new_validator, candidate, "new")
        kept = self._is_kept((old_err, new_err))
        if kept:
            self.root_old_err = old_err
            self.root_new_err = new_err
        self.trace.append(
            DiffTraceEntry(
                # index 记的是候选序号（每候选两次评估），evaluations 另计总额。
                len(self.trace) + 1,
                op,
                pointer_from_path(path),
                detail,
                kept,
                old_err is None,
                new_err is None,
                old_err.validator if old_err is not None else None,
                new_err.validator if new_err is not None else None,
            )
        )
        return (old_err, new_err), kept

    def _is_kept(self, errs: Any) -> bool:
        if self.target_pair is None:
            return False
        (old_err, new_err) = errs
        return (old_err is None, new_err is None) == self.target_pair

    # ---- 主流程 -----------------------------------------------------------

    def lock_pair(self) -> tuple[Any, Any]:
        """前 2 次评估：在原实例上分别校验旧版与新版，锁定通过/拒绝组合。"""
        self._spend()
        old_err = self._check_side(self.old_validator, self.root, "old")
        self._spend()
        new_err = self._check_side(self.new_validator, self.root, "new")
        self.target_pair = (old_err is None, new_err is None)
        self.target_keyword = None  # 差异最小化不使用关键字锁。
        self.root_old_err = old_err
        self.root_new_err = new_err
        return old_err, new_err

    @staticmethod
    def _direction(old_valid: bool, new_valid: bool) -> str | None:
        if old_valid and not new_valid:
            # 旧版接受、新版拒绝：新版本更严格。
            return "newly_rejected"
        if not old_valid and new_valid:
            # 旧版拒绝、新版接受：新版本更宽松。
            return "newly_accepted"
        return None

    def run(self) -> SchemaDiffResult:
        old_err, new_err = self.lock_pair()
        old_valid, new_valid = self.target_pair

        if old_valid == new_valid:
            # 两版结果相同：不存在差异，不做任何缩减。
            return SchemaDiffResult(
                reduced_instance=self.original,
                original_instance=self.original,
                differs=False,
                direction=None,
                old_valid=old_valid,
                new_valid=new_valid,
                old_first_error=error_info(old_err),
                new_first_error=error_info(new_err),
                evaluations=self.evaluations,
                stopped_by_budget=False,
                trace=self.trace,
            )

        while True:
            changed_holder = [False]
            try:
                self._visit((), changed_holder)
            except BudgetExhausted:
                break
            if not changed_holder[0]:
                break

        # 终态错误即当前 root 最近一次被接受候选的错误对（无需额外评估）。
        final_old_err = self.root_old_err
        final_new_err = self.root_new_err
        return SchemaDiffResult(
            reduced_instance=self.root,
            original_instance=self.original,
            differs=True,
            direction=self._direction(final_old_err is None, final_new_err is None),
            old_valid=final_old_err is None,
            new_valid=final_new_err is None,
            old_first_error=error_info(final_old_err),
            new_first_error=error_info(final_new_err),
            evaluations=self.evaluations,
            stopped_by_budget=self.stopped_by_budget,
            trace=self.trace,
        )
