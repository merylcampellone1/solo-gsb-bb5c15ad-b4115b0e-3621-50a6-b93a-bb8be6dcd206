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

    def _first_error(self, instance: Any):
        # iter_errors 按 schema/instance 的固定遍历顺序深度优先产出错误，
        # 结合锁定的依赖版本，结果完全确定。
        return next(iter(self.validator.iter_errors(instance)), None)

    def evaluate(self, candidate: Any, op: str, path: tuple, detail: str) -> tuple[Any, bool]:
        """评估一次候选，记录轨迹；命中预算则抛出 ``BudgetExhausted``。

        返回 ``(首个错误对象或 None, 是否仍触发已锁定关键字)``。
        锁定（lock）阶段第二个值恒为 False。
        """
        if self.evaluations >= self.max_evaluations:
            self.stopped_by_budget = True
            raise BudgetExhausted()
        self.evaluations += 1
        err = self._first_error(candidate)
        index = self.evaluations
        if err is None:
            self.trace.append(
                TraceEntry(index, op, pointer_from_path(path), detail, False)
            )
            return None, False
        kept = (
            self.target_keyword is not _UNLOCKED
            and err.validator == self.target_keyword
        )
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
