"""确定性差异见证最小化：``POST /schema-diff`` 的缩减内核。

同一 JSON 实例在旧版 / 新版两个 JSON Schema 下结果相反（一个通过、一个拒绝）
时，以 **保持两版通过/拒绝组合** 为唯一“有趣性”判据，直接复用
:class:`~app.minimizer.Minimizer` 的全部确定性缩减步骤（坍缩 → 标量阶梯 →
对象/数组/字符串 ddmin → 递归子节点 → 外层不动点）。被接受的每个候选都必须
与原实例产生完全相同的 ``(旧版状态, 新版状态)`` 组合；两版结果相同则不缩减。

一次评估 = 对一个候选在两版 schema 上各执行一次完整校验。全程无随机源，
相同输入必然得到相同结果。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .minimizer import BudgetExhausted, Minimizer, pointer_from_path

# 新版拒绝旧版通过的实例：新版对该见证更严格。
DIRECTION_NEW_REJECTS = "new_rejects"
# 新版通过旧版拒绝的实例：新版对该见证更宽松。
DIRECTION_NEW_ACCEPTS = "new_accepts"


class UnusableSchemaError(Exception):
    """schema 通过了 check_schema 但无法实际执行校验（坏 $ref、非法正则等）。"""

    def __init__(self, which: str, cause: Exception):
        super().__init__(f"{which} schema 无法用于校验: {cause}")
        self.which = which
        self.cause = cause


def side_result(err: Any) -> dict[str, Any]:
    """把单个校验器的首个错误（或 ``None`` 表示通过）整理成响应侧结构。"""
    if err is None:
        return {"status": "pass", "keyword": None, "error_path": None, "message": None}
    return {
        "status": "reject",
        "keyword": err.validator,
        "error_path": pointer_from_path(tuple(err.absolute_path)),
        "message": err.message,
    }


def _error_side(cause: Exception) -> dict[str, Any]:
    """校验器对候选无法运行时的侧结构：既非通过也非拒绝，候选一律不接受。"""
    return {
        "status": "error",
        "keyword": None,
        "error_path": None,
        "message": f"schema 无法用于校验: {cause}",
    }


@dataclass
class DiffTraceEntry:
    """单次双版评估的轨迹记录。"""

    index: int
    op: str
    path: str
    detail: str
    # 候选是否保持了原实例的两版通过/拒绝组合并因此被接受。
    kept: bool
    # 旧版 / 新版各自的侧结构：{status, keyword, error_path, message}。
    old: dict[str, Any]
    new: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "op": self.op,
            "path": self.path,
            "detail": self.detail,
            "kept": self.kept,
            "old": self.old,
            "new": self.new,
        }


@dataclass
class DiffResult:
    minimal_instance: Any
    original_instance: Any
    direction: str
    evaluations: int
    stopped_by_budget: bool
    trace: list[DiffTraceEntry] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "minimal_instance": self.minimal_instance,
            "original_instance": self.original_instance,
            "direction": self.direction,
            "evaluations": self.evaluations,
            "stopped_by_budget": self.stopped_by_budget,
            "trace": [entry.to_dict() for entry in self.trace],
        }


class DiffMinimizer(Minimizer):
    """以“两版 schema 的通过/拒绝组合”为有趣性判据的最小化器。

    缩减步骤（坍缩 / 标量阶梯 / ddmin / 递归不动点 / 预算机制）全部继承自
    :class:`Minimizer`，只替换“候选是否被接受”的判定：候选必须与原实例保持
    相同的两版通过/拒绝组合。
    """

    def __init__(self, old_validator, new_validator, instance: Any, max_evaluations: int):
        # 旧版校验器占住基类的 validator 位置；实际评估同时使用两个校验器。
        super().__init__(old_validator, instance, max_evaluations)
        self.old_validator = old_validator
        self.new_validator = new_validator
        # 原实例在两版下的通过状态；lock 前为 None，任何候选都不接受。
        self.old_passed: bool | None = None
        self.new_passed: bool | None = None
        # 覆盖基类的单版轨迹，改记双版评估轨迹。
        self.trace: list[DiffTraceEntry] = []

    # ---- 校验 / 预算 -----------------------------------------------------

    @staticmethod
    def _first_error_safe(validator, candidate: Any):
        """返回 ``(首个错误或 None, 异常或 None)``；校验器无法运行时后者非空。"""
        try:
            return next(iter(validator.iter_errors(candidate)), None), None
        except Exception as exc:  # 坏 $ref、非法正则等：与 /mutations 同样兜底。
            return None, exc

    def evaluate(self, candidate: Any, op: str, path: tuple, detail: str):
        """评估一个候选：两版各校验一次，按组合是否保持决定 ``kept``。

        命中预算抛出 :class:`BudgetExhausted`（基类缩减循环负责中止）；
        返回 ``((旧版错误, 新版错误), 是否保持组合)``，错误位为 ``None``
        表示该版通过。
        """
        if self.evaluations >= self.max_evaluations:
            self.stopped_by_budget = True
            raise BudgetExhausted()
        self.evaluations += 1

        old_err, old_exc = self._first_error_safe(self.old_validator, candidate)
        new_err, new_exc = self._first_error_safe(self.new_validator, candidate)

        # lock 阶段校验器无法运行：schema 不可用，交由接口层转 422。
        if op == "lock":
            if old_exc is not None:
                raise UnusableSchemaError("old_schema", old_exc)
            if new_exc is not None:
                raise UnusableSchemaError("new_schema", new_exc)

        if old_exc is not None:
            old_side = _error_side(old_exc)
            old_passed: bool | None = None
        else:
            old_side = side_result(old_err)
            old_passed = old_err is None
        if new_exc is not None:
            new_side = _error_side(new_exc)
            new_passed: bool | None = None
        else:
            new_side = side_result(new_err)
            new_passed = new_err is None

        # 有趣性：两版状态都与原实例完全一致（任一版崩溃都不算保持）。
        kept = (
            self.old_passed is not None
            and self.new_passed is not None
            and old_passed == self.old_passed
            and new_passed == self.new_passed
        )
        self.trace.append(
            DiffTraceEntry(
                self.evaluations,
                op,
                pointer_from_path(path),
                detail,
                kept,
                old_side,
                new_side,
            )
        )
        return (old_err, new_err), kept

    def lock(self):
        """第 1 次评估：记录原实例在两版下的通过/拒绝状态，返回两侧首个错误。"""
        (old_err, new_err), _ = self.evaluate(
            self.root,
            "lock",
            (),
            "validate original instance against both schema versions",
        )
        self.old_passed = old_err is None
        self.new_passed = new_err is None
        return old_err, new_err

    # ---- 差异属性 ---------------------------------------------------------

    @property
    def diverges(self) -> bool:
        """两版结果是否相反（一通过、一拒绝）。"""
        return self.old_passed is not None and self.old_passed != self.new_passed

    @property
    def direction(self) -> str | None:
        """差异方向：``new_rejects`` / ``new_accepts``；不相反时为 ``None``。"""
        if not self.diverges:
            return None
        if self.new_passed is False:
            return DIRECTION_NEW_REJECTS
        return DIRECTION_NEW_ACCEPTS

    # ---- 主流程（复用基类不动点循环，仅替换结果对象）-----------------------

    def minimize(self) -> DiffResult:
        # 基类循环只使用 evaluate(...) 的 kept 位与 root/budget 状态，
        # 因此直接复用；MinimizeResult 中与单版相关的字段不再对外暴露。
        base = super().minimize()
        return DiffResult(
            minimal_instance=base.minimal_instance,
            original_instance=base.original_instance,
            direction=self.direction,
            evaluations=base.evaluations,
            stopped_by_budget=base.stopped_by_budget,
            trace=self.trace,
        )
