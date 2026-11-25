"""财务对账交接接口：受理、更正、领取、明细文件与回执。

指标查询的可用结果由 `GET /api/data_export/handover/query-results` 交给数据导出，
之后所有交接动作都带 `task_id` 与 `acceptance_no`，保证一笔明细能追到取数批次。
"""
from __future__ import annotations

import csv
import io
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from app.services.handover import HandoverError, handover_repo

router = APIRouter(prefix="/api/data_export/handover", tags=["财务对账交接"])


class HandoverCreatePayload(BaseModel):
    task_id: int
    acceptance_no: str | None = None
    query_id: int | None = None
    fields: list[str] | None = None
    rows: list[dict[str, Any]] | None = None
    stop_after: str | None = None


class HandoverCorrectionPayload(BaseModel):
    task_id: int
    region: str
    amount: float | None = None
    delta: float | None = None


class HandoverClaimPayload(BaseModel):
    task_id: int
    version: int | None = None


def _error(exc: HandoverError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"ok": False, "message": str(exc), "error": str(exc)},
    )


@router.get("/query-results")
def handover_query_results(query_id: int = Query(...)) -> Any:
    """指标查询把可用结果交给数据导出。"""
    from app.services.handover import available_region_results
    try:
        return available_region_results(query_id)
    except HandoverError as exc:
        return _error(exc)


@router.get("")
def list_handovers(task_id: int = Query(...)) -> Any:
    try:
        return handover_repo.list_orders(task_id)
    except HandoverError as exc:
        return _error(exc)


@router.post("")
def create_handover(payload: HandoverCreatePayload) -> Any:
    """登记受理单并发起交接；传 stop_after 时写到该地区后停住。"""
    try:
        return handover_repo.create(
            payload.task_id,
            acceptance_no=payload.acceptance_no,
            query_id=payload.query_id,
            fields=payload.fields,
            rows=payload.rows,
            stop_after=payload.stop_after,
        )
    except HandoverError as exc:
        return _error(exc)


@router.get("/{acceptance_no}/detail")
def handover_detail(
    acceptance_no: str,
    task_id: int = Query(...),
    version: int | None = Query(default=None),
) -> Any:
    """对账明细的 JSON 视图，含批次血缘与明细摘要。"""
    try:
        detail = handover_repo.detail(task_id, acceptance_no, version=version)
    except HandoverError as exc:
        return _error(exc)
    reader = csv.reader(io.StringIO(detail["content"].decode("utf-8-sig")))
    records = list(reader)
    header = records[0] if records else []
    body = [dict(zip(header, values)) for values in records[1:]]
    return {
        "ok": True,
        "filename": detail["filename"],
        "fields": detail["fields"],
        "version": detail["version"],
        "status": detail["status"],
        "batch_ids": detail["batch_ids"],
        "headers": detail["headers"],
        "rows": body,
    }


@router.get("/{acceptance_no}/detail.csv")
def handover_detail_file(
    acceptance_no: str,
    task_id: int = Query(...),
    version: int | None = Query(default=None),
) -> Any:
    """下载可交接的对账明细文件；完整后才允许下载。"""
    try:
        detail = handover_repo.detail(
            task_id, acceptance_no, version=version, require_claimed=True)
    except HandoverError as exc:
        return _error(exc)
    headers = {"Content-Disposition": f'attachment; filename="{detail["filename"]}"'}
    for key, value in detail["headers"].items():
        # HTTP 头只允许 latin-1：中文状态用百分号编码，原文在 JSON 视图里给出。
        headers[key] = quote(value, safe=",:-_") if not value.isascii() else value
    return StreamingResponse(
        iter([detail["content"]]),
        media_type="text/csv; charset=utf-8",
        headers=headers,
    )


@router.get("/{acceptance_no}/receipt")
def handover_receipt(
    acceptance_no: str,
    task_id: int = Query(...),
    version: int | None = Query(default=None),
) -> Any:
    try:
        return handover_repo.receipt(task_id, acceptance_no, version=version)
    except HandoverError as exc:
        return _error(exc)


@router.get("/{acceptance_no}/reconciliation")
def handover_reconciliation(
    acceptance_no: str,
    task_id: int = Query(...),
    version: int | None = Query(default=None),
) -> Any:
    """财务对账结论：用了哪几次取数、原单接续或撤销、是否需要另发单。"""
    try:
        return handover_repo.reconciliation(task_id, acceptance_no, version=version)
    except HandoverError as exc:
        return _error(exc)


@router.get("/{acceptance_no}/batches/{batch_id}")
def handover_batch(acceptance_no: str, batch_id: str, task_id: int = Query(...)) -> Any:
    try:
        return handover_repo.get_batch(task_id, acceptance_no, batch_id)
    except HandoverError as exc:
        return _error(exc)


@router.post("/{acceptance_no}/corrections")
def correct_handover(acceptance_no: str, payload: HandoverCorrectionPayload) -> Any:
    """对已受理交接做金额更正，生成新版本并交代原版本去向。"""
    try:
        return handover_repo.correct(
            payload.task_id, acceptance_no,
            region=payload.region, amount=payload.amount, delta=payload.delta,
        )
    except HandoverError as exc:
        return _error(exc)


@router.post("/{acceptance_no}/claim")
def claim_handover(acceptance_no: str, payload: HandoverClaimPayload) -> Any:
    """领取完整交接件，签发与原单明细同批数据的回执。"""
    try:
        return handover_repo.claim(
            payload.task_id, acceptance_no, version=payload.version)
    except HandoverError as exc:
        return _error(exc)


@router.get("/{acceptance_no}")
def get_handover(
    acceptance_no: str,
    task_id: int = Query(...),
    version: int | None = Query(default=None),
) -> Any:
    try:
        return handover_repo.get(task_id, acceptance_no, version=version)
    except HandoverError as exc:
        return _error(exc)
