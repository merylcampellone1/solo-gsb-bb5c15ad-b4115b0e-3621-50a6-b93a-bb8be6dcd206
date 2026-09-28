"""FastAPI 入口：JSON Schema 反例最小化 + 确定性变异生成服务。"""

from __future__ import annotations

import os
from typing import Annotated, Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from jsonschema.exceptions import SchemaError

from .minimizer import Minimizer, build_validator, pointer_from_path
from .mutator import Mutator
from .differ import (
    DiffMinimizer,
    UnusableSchemaError,
    side_result as first_error_side,
)

# ---- 配置（全部可通过环境变量覆盖）----------------------------------------

DEFAULT_MAX_EVALUATIONS = int(os.getenv("DEFAULT_MAX_EVALUATIONS", "1000"))
HARD_MAX_EVALUATIONS = int(os.getenv("HARD_MAX_EVALUATIONS", "100000"))

app = FastAPI(
    title="JSON Schema Counterexample Minimizer",
    description=(
        "对未通过 JSON Schema 校验的实例做确定性 delta debugging 最小化，"
        "锁定首个校验错误的关键字，只保留仍触发同一关键字的候选；"
        "也可以给定合法样例，确定性生成覆盖各类约束关键字的最小变异实例；"
        "还可以对比新旧两版 schema 对同一实例的通过/拒绝差异，并确定性缩减"
        "出更小的差异见证。"
    ),
    version="1.2.0",
)


class MinimizeRequest(BaseModel):
    # JSON Schema 本身可以是 object 或 boolean（true/false schema）。
    # 字段名避开 BaseModel.schema，JSON 键通过 alias 保持为 "schema"。
    schema_: Annotated[
        Any,
        Field(
            alias="schema",
            title="JSON Schema",
            description="待针对校验的 JSON Schema，支持 draft 3/4/6/7/2019-09/2020-12。",
        ),
    ]
    instance: Any = Field(
        ...,
        title="未通过校验的 JSON 实例",
    )
    max_evaluations: int | None = Field(
        default=None,
        ge=1,
        le=HARD_MAX_EVALUATIONS,
        description=(
            "最大校验评估次数（含对原实例的首次校验）；"
            f"默认 {DEFAULT_MAX_EVALUATIONS}，硬上限 {HARD_MAX_EVALUATIONS}。"
        ),
    )


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/minimize")
def minimize(req: MinimizeRequest) -> JSONResponse:
    max_evaluations = (
        req.max_evaluations
        if req.max_evaluations is not None
        else DEFAULT_MAX_EVALUATIONS
    )

    # schema 自身不合法：返回 422。
    try:
        validator = build_validator(req.schema_)
    except SchemaError as exc:
        return JSONResponse(
            status_code=422,
            content={
                "error": "invalid_schema",
                "message": exc.message,
                "path": list(exc.absolute_path),
            },
        )

    minimizer = Minimizer(validator, req.instance, max_evaluations)
    # 第 1 次评估同时负责"实例确实非法"的确认与目标关键字锁定
    # （lock 内部已设置 target_keyword，含 false schema 的 None 关键字）。
    first_error = minimizer.lock()
    if first_error is None:
        return JSONResponse(
            status_code=422,
            content={
                "error": "instance_valid",
                "message": "提供的实例已通过 schema 校验，无需最小化；本接口只接受反例。",
            },
        )

    result = minimizer.minimize()
    return JSONResponse(status_code=200, content=result.to_dict())


class MutationsRequest(BaseModel):
    schema_: Annotated[
        Any,
        Field(
            alias="schema",
            title="JSON Schema",
            description="待针对校验的 JSON Schema，支持 draft 3/4/6/7/2019-09/2020-12。",
        ),
    ]
    instance: Any = Field(
        ...,
        title="合法 JSON 样例",
        description="必须通过 schema 校验，作为所有变异的起点。",
    )
    seed: Any = Field(
        default=None,
        title="复现种子",
        description=(
            "可选。变异生成本身不消耗随机源；相同 (schema, instance, seed) "
            "必然得到相同响应，seed 用于标识与复现单个变异。"
        ),
    )
    max_evaluations: int | None = Field(
        default=None,
        ge=1,
        le=HARD_MAX_EVALUATIONS,
        description=(
            "最大校验评估次数（含对样例的首次校验）；"
            f"默认 {DEFAULT_MAX_EVALUATIONS}，硬上限 {HARD_MAX_EVALUATIONS}。"
        ),
    )


@app.post("/mutations")
def mutations(req: MutationsRequest) -> JSONResponse:
    max_evaluations = (
        req.max_evaluations
        if req.max_evaluations is not None
        else DEFAULT_MAX_EVALUATIONS
    )

    # schema 自身不合法：返回 422。
    try:
        validator = build_validator(req.schema_)
    except SchemaError as exc:
        return JSONResponse(
            status_code=422,
            content={
                "error": "invalid_schema",
                "message": exc.message,
                "path": list(exc.absolute_path),
            },
        )

    mutator = Mutator(
        req.schema_,
        req.instance,
        validator,
        seed=req.seed,
        max_evaluations=max_evaluations,
    )

    # 第 1 次评估：样例必须合法，否则没有变异起点。
    try:
        first_error = mutator.evaluate(req.instance)
    except Exception as exc:
        # 校验器连一次完整校验都无法运行（坏 $ref、非法正则等）：按不可用 schema 处理。
        return JSONResponse(
            status_code=422,
            content={
                "error": "invalid_schema",
                "message": f"schema 无法用于校验: {exc}",
                "path": [],
            },
        )
    if first_error is not None:
        return JSONResponse(
            status_code=422,
            content={
                "error": "instance_invalid",
                "message": "提供的样例未通过 schema 校验；本接口需要合法样例作为变异起点。",
                "keyword": first_error.validator,
                "instance_path": pointer_from_path(tuple(first_error.absolute_path)),
            },
        )

    result = mutator.generate()
    return JSONResponse(status_code=200, content=result.to_dict())


class SchemaDiffRequest(BaseModel):
    # JSON Schema 本身可以是 object 或 boolean（true/false schema）。
    old_schema: Any = Field(
        ...,
        title="旧版 JSON Schema",
        description="作为对比基线的 JSON Schema，支持 draft 3/4/6/7/2019-09/2020-12。",
    )
    new_schema: Any = Field(
        ...,
        title="新版 JSON Schema",
        description="作为对比目标的 JSON Schema，支持 draft 3/4/6/7/2019-09/2020-12。",
    )
    instance: Any = Field(
        ...,
        title="待对比的 JSON 实例",
    )
    max_evaluations: int | None = Field(
        default=None,
        ge=1,
        le=HARD_MAX_EVALUATIONS,
        description=(
            "最大双版评估次数（含对原实例在两版上的首次校验）；"
            "每次评估对候选在旧版、新版上各校验一次；"
            f"默认 {DEFAULT_MAX_EVALUATIONS}，硬上限 {HARD_MAX_EVALUATIONS}。"
        ),
    )


def _invalid_schema_body(which: str, exc: SchemaError) -> dict[str, Any]:
    return {
        "error": "invalid_schema",
        "which": which,
        "message": exc.message,
        "path": list(exc.absolute_path),
    }


@app.post("/schema-diff")
def schema_diff(req: SchemaDiffRequest) -> JSONResponse:
    max_evaluations = (
        req.max_evaluations
        if req.max_evaluations is not None
        else DEFAULT_MAX_EVALUATIONS
    )

    # 两个 schema 各自独立校验：任一不合法都返回 422，并用 which 指明版本。
    try:
        old_validator = build_validator(req.old_schema)
    except SchemaError as exc:
        return JSONResponse(
            status_code=422,
            content=_invalid_schema_body("old_schema", exc),
        )
    try:
        new_validator = build_validator(req.new_schema)
    except SchemaError as exc:
        return JSONResponse(
            status_code=422,
            content=_invalid_schema_body("new_schema", exc),
        )

    differ = DiffMinimizer(old_validator, new_validator, req.instance, max_evaluations)

    # 第 1 次双版评估同时确认两版通过/拒绝状态与各自的首个错误。
    try:
        old_err, new_err = differ.lock()
    except UnusableSchemaError as exc:
        # check_schema 通过但实际无法校验（坏 $ref、非法正则等）：按无效 schema 处理。
        return JSONResponse(
            status_code=422,
            content={
                "error": "invalid_schema",
                "which": exc.which,
                "message": str(exc),
                "path": [],
            },
        )

    content: dict[str, Any] = {
        "old": first_error_side(old_err),
        "new": first_error_side(new_err),
        "diverges": differ.diverges,
        "direction": differ.direction,
        "original_instance": req.instance,
        "evaluations": differ.evaluations,
        "stopped_by_budget": False,
        "trace": [entry.to_dict() for entry in differ.trace],
    }

    # 两版结果相同（同通过或同拒绝）：不缩减，直接返回状态与首个错误。
    if not differ.diverges:
        content["minimal_instance"] = None
        return JSONResponse(status_code=200, content=content)

    # 结果相反：沿用 /minimize 的确定性缩减步骤，每个候选都必须保持两版组合。
    result = differ.minimize()
    content.update(
        {
            "minimal_instance": result.minimal_instance,
            "evaluations": result.evaluations,
            "stopped_by_budget": result.stopped_by_budget,
            "trace": [entry.to_dict() for entry in result.trace],
        }
    )
    return JSONResponse(status_code=200, content=content)
