"""财务对账交接：受理单、取数批次、交接版本与回执。

财务用多维查询结果核对地区入账时，要交接的不是“预览 + 字节数”，
而是一份可下载、可追溯、可领取的对账明细：

- 发起时选了哪些字段，交接件就只出现哪些字段；渠道等未选字段不得随附件给出。
- 每一笔明细都绑定一次已确认的取数批次，回执按行给出批次血缘。
- 交接件完整后才可以领取；明细与回执使用同一批已确认取数。
- 同一张受理单重复发起、重复领取都只返回同一份有效交接。
- 更正生成新版本：原版本被接续的记“接续完成”，被替换掉已写数据的记“已撤销”，
  财务仍可从原受理单核对原单去向，并判断是否需要另发受理单。
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import threading
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from app.store import store

# 指标查询结果里可以被选择交接的业务字段。渠道等字段只有被选中才允许输出。
DETAIL_FIELDS = ["地区", "入账额", "渠道", "日期", "指标名称", "指标数值"]
DEFAULT_FIELDS = ["地区", "入账额"]

STATUS_WRITING = "写入中"
STATUS_READY = "待领取"
STATUS_CLAIMED = "已领取"
STATUS_CONTINUED = "接续完成"
STATUS_REVOKED = "已撤销"

# 示例指标查询结果按地区给出入账额；报单可以在发起或更正时覆盖金额。
_REGION_RESULTS: dict[str, dict[str, Any]] = {
    "华东": {"地区": "华东", "入账额": 60, "渠道": "直营", "指标名称": "地区入账额"},
    "华南": {"地区": "华南", "入账额": 40, "渠道": "代理", "指标名称": "地区入账额"},
    "华北": {"地区": "华北", "入账额": 35, "渠道": "直营", "指标名称": "地区入账额"},
    "西南": {"地区": "西南", "入账额": 25, "渠道": "代理", "指标名称": "地区入账额"},
}


class HandoverError(Exception):
    """可读的业务拒绝；路由层按 status_code 转成响应。"""

    def __init__(self, message: str, status_code: int = 409) -> None:
        super().__init__(message)
        self.status_code = status_code


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _amount(value: Any, field_name: str = "入账额") -> int | float:
    """金额统一成数字；整额保持整数，避免 60.0 这类展示噪声。"""
    if isinstance(value, bool):
        raise HandoverError(f"{field_name}必须是数字", 400)
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        text = str(value if value is not None else "").strip().replace(",", "")
        try:
            number = float(text)
        except ValueError as exc:
            raise HandoverError(f"{field_name}「{value}」不是有效数字", 400) from exc
    if not math.isfinite(number):
        raise HandoverError(f"{field_name}必须是有限数字", 400)
    return int(number) if number.is_integer() else round(number, 2)


def available_region_results(query_id: int) -> dict[str, Any]:
    """指标查询把可用结果交给数据导出：返回地区维度的可交接结果。"""
    query = store.find("dim_query", int(query_id))
    if query is None:
        raise HandoverError(f"查询方案 {query_id} 不存在", 404)
    if str(query.get("status")) == "已归档":
        raise HandoverError(f"查询方案 {query_id} 已归档，没有可交接结果", 409)
    rows: list[dict[str, Any]] = []
    for base in _REGION_RESULTS.values():
        row = dict(base)
        row["日期"] = date.today().isoformat()
        row["指标数值"] = row["入账额"]
        rows.append(row)
    return {
        "ok": True,
        "query_id": int(query_id),
        "query_name": query.get("方案标识"),
        "query_status": query.get("status"),
        "confirmed": True,
        "available": True,
        "fields": list(DETAIL_FIELDS),
        "default_fields": list(DEFAULT_FIELDS),
        "rows": rows,
        "message": "指标查询可用结果已就绪，可交由数据导出办理交接",
    }


@dataclass
class FetchBatch:
    """一次已确认的取数；明细行通过 batch_id 追到它。"""

    batch_id: str
    sequence: int
    query_id: int | None
    acceptance_no: str
    version: int
    values: dict[str, Any]
    confirmed: bool
    created_at: str
    source: str = "指标查询"

    def public(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "sequence": self.sequence,
            "query_id": self.query_id,
            "acceptance_no": self.acceptance_no,
            "version": self.version,
            "region": self.values.get("地区"),
            "amount": self.values.get("入账额"),
            "confirmed": self.confirmed,
            "confirmation": "已确认" if self.confirmed else "待确认",
            "source": self.source,
            "created_at": self.created_at,
        }


@dataclass
class HandoverLine:
    """一条已写入的交接明细，值里可能含未选字段，输出时统一投影。"""

    values: dict[str, Any]
    batch_id: str


@dataclass
class HandoverVersion:
    version: int
    handover_no: str
    declared_rows: list[dict[str, Any]]
    status: str
    created_at: str
    lines: list[HandoverLine] = field(default_factory=list)
    receipt: dict[str, Any] | None = None
    claimed_at: str | None = None
    snapshot: list[dict[str, Any]] | None = None

    @property
    def declared_regions(self) -> list[str]:
        return [str(row.get("地区")) for row in self.declared_rows]

    @property
    def written_regions(self) -> list[str]:
        return [str(line.values.get("地区")) for line in self.lines]

    @property
    def pending_regions(self) -> list[str]:
        written = set(self.written_regions)
        return [region for region in self.declared_regions if region not in written]

    def complete(self) -> bool:
        return not self.pending_regions


@dataclass
class AcceptanceOrder:
    acceptance_no: str
    task_id: int
    query_id: int | None
    fields: list[str]
    created_at: str
    fingerprint: str
    versions: list[HandoverVersion] = field(default_factory=list)

    @property
    def current(self) -> HandoverVersion:
        return self.versions[-1]

    @property
    def declared_rows(self) -> list[dict[str, Any]]:
        return self.current.declared_rows


class HandoverRepository:
    """进程内交接仓库；真实项目可替换成数据库，规则保持一致。"""

    def __init__(self) -> None:
        self._orders: dict[str, AcceptanceOrder] = {}
        self._batches: dict[str, FetchBatch] = {}
        self._lock = threading.RLock()
        self._acceptance_seq = 0
        self._handover_seq = 0
        self._batch_seq = 0

    # ---- 基础工具 ----
    def reset(self) -> None:
        """测试用：清空交接数据，不动业务模块种子数据。"""
        with self._lock:
            self._orders.clear()
            self._batches.clear()
            self._acceptance_seq = 0
            self._handover_seq = 0
            self._batch_seq = 0

    def _next_acceptance_no(self) -> str:
        self._acceptance_seq += 1
        return f"SL-{date.today():%Y%m%d}-{self._acceptance_seq:03d}"

    def _next_handover_no(self) -> str:
        self._handover_seq += 1
        return f"HANDOVER-{self._handover_seq:04d}"

    def _next_batch(
        self,
        *,
        values: dict[str, Any],
        query_id: int | None,
        acceptance_no: str,
        version: int,
    ) -> FetchBatch:
        self._batch_seq += 1
        batch = FetchBatch(
            batch_id=f"FETCH-{date.today():%Y%m%d}-{self._batch_seq:04d}",
            sequence=self._batch_seq,
            query_id=query_id,
            acceptance_no=acceptance_no,
            version=version,
            values=dict(values),
            confirmed=True,
            created_at=_now(),
        )
        self._batches[batch.batch_id] = batch
        return batch

    @staticmethod
    def _project(values: dict[str, Any], fields: list[str]) -> dict[str, Any]:
        """只输出发起时选中的字段，渠道等未选字段不随交接件给出。"""
        return {name: values.get(name) for name in fields}

    @staticmethod
    def _fingerprint(
        task_id: int,
        query_id: int | None,
        fields: list[str],
        rows: list[dict[str, Any]],
        stop_after: str | None,
    ) -> str:
        payload = {
            "task_id": task_id,
            "query_id": query_id,
            "fields": fields,
            "rows": [{name: row.get(name) for name in fields} for row in rows],
            "stop_after": stop_after,
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _get_order(self, task_id: int, acceptance_no: str) -> AcceptanceOrder:
        order = self._orders.get(acceptance_no)
        if order is None or order.task_id != task_id:
            raise HandoverError(
                f"受理单 {acceptance_no} 不存在或不属于导出任务 {task_id}", 404)
        return order

    def _pick_version(
        self, order: AcceptanceOrder, version: int | None,
    ) -> HandoverVersion:
        if version is None:
            return order.current
        matches = [item for item in order.versions if item.version == int(version)]
        if not matches:
            raise HandoverError(f"受理单 {order.acceptance_no} 没有版本 v{version}", 404)
        return matches[0]

    # ---- 发起受理 ----
    def create(
        self,
        task_id: int,
        *,
        acceptance_no: str | None = None,
        query_id: int | None = None,
        fields: list[str] | None = None,
        rows: list[dict[str, Any]] | None = None,
        stop_after: str | None = None,
    ) -> dict[str, Any]:
        task = store.find("data_export", task_id)
        if task is None:
            raise HandoverError(f"导出任务 {task_id} 不存在", 404)

        with self._lock:
            if acceptance_no:
                existing = self._orders.get(acceptance_no)
                if existing is not None:
                    if existing.task_id != task_id:
                        raise HandoverError(
                            f"受理单 {acceptance_no} 已属于导出任务 {existing.task_id}", 409)
                    matches: bool | None = None
                    if query_id is not None or rows is not None:
                        try:
                            norm_fields, norm_rows = self._normalize(
                                query_id=query_id, fields=fields, rows=rows)
                        except HandoverError:
                            matches = False
                        else:
                            matches = (
                                norm_fields == existing.fields
                                and [
                                    {name: row.get(name) for name in norm_fields}
                                    for row in norm_rows
                                ] == [
                                    {name: row.get(name) for name in norm_fields}
                                    for row in existing.declared_rows
                                ]
                            )
                    # 同一张受理单重试只返回原有效交接，不复制第二份。
                    return self._public(
                        existing,
                        existing.current,
                        duplicate=True,
                        payload_matches_existing=matches,
                        message="同一张受理单已存在，沿用原有效交接，未重复生成",
                    )

            normalized_fields, normalized_rows = self._normalize(
                query_id=query_id, fields=fields, rows=rows)

            stop_index = len(normalized_rows)
            if stop_after:
                region = str(stop_after).strip()
                matches = [
                    idx for idx, row in enumerate(normalized_rows)
                    if str(row.get("地区")) == region
                ]
                if not matches:
                    raise HandoverError(f"停住地区「{region}」不在本次报单明细中", 400)
                stop_index = matches[-1] + 1

            acceptance_no = acceptance_no or self._next_acceptance_no()
            version = HandoverVersion(
                version=1,
                handover_no=self._next_handover_no(),
                declared_rows=[dict(row) for row in normalized_rows],
                status=STATUS_WRITING,
                created_at=_now(),
            )
            for row in normalized_rows[:stop_index]:
                batch = self._next_batch(
                    values=row,
                    query_id=query_id,
                    acceptance_no=acceptance_no,
                    version=version.version,
                )
                version.lines.append(HandoverLine(values=dict(row), batch_id=batch.batch_id))
            version.status = STATUS_WRITING if version.pending_regions else STATUS_READY

            order = AcceptanceOrder(
                acceptance_no=acceptance_no,
                task_id=task_id,
                query_id=query_id,
                fields=normalized_fields,
                created_at=_now(),
                fingerprint=self._fingerprint(
                    task_id, query_id, normalized_fields, normalized_rows, stop_after),
                versions=[version],
            )
            self._orders[acceptance_no] = order
            self._sync_task(task, order)

            message = (
                f"受理单已登记，交接件写入中，还差：{'、'.join(version.pending_regions)}"
                if version.pending_regions
                else "受理单已登记，交接件完整，可以领取"
            )
            return self._public(order, version, message=message)

    def _normalize(
        self,
        *,
        query_id: int | None,
        fields: list[str] | None,
        rows: list[dict[str, Any]] | None,
    ) -> tuple[list[str], list[dict[str, Any]]]:
        selected: list[str] = []
        for item in (fields or DEFAULT_FIELDS):
            name = str(item).strip()
            if name and name not in selected:
                selected.append(name)
        if not selected:
            selected = list(DEFAULT_FIELDS)
        unknown = [name for name in selected if name not in DETAIL_FIELDS]
        if unknown:
            raise HandoverError(
                f"交接字段不支持：{'、'.join(unknown)}；可选：{'、'.join(DETAIL_FIELDS)}", 400)
        if "地区" not in selected:
            raise HandoverError("交接字段必须包含「地区」，否则明细无法按地区核对", 400)

        source = available_region_results(query_id) if query_id is not None else None
        source_by_region = {
            str(row.get("地区")): row for row in (source["rows"] if source else [])
        }

        raw_rows = rows if rows is not None else (source["rows"] if source else [])
        if not raw_rows:
            raise HandoverError("请提供报单明细，或指定可用的指标查询方案", 400)

        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in raw_rows:
            if not isinstance(raw, dict):
                raise HandoverError("报单明细每行必须是对象", 400)
            region = str(raw.get("地区", raw.get("region", ""))).strip()
            if not region:
                raise HandoverError("报单明细缺少「地区」", 400)
            if region in seen:
                raise HandoverError(f"报单明细中地区 {region} 重复", 400)
            seen.add(region)
            if source is not None and region not in source_by_region:
                raise HandoverError(
                    f"查询方案 {query_id} 没有地区 {region} 的可用结果", 409)

            values = dict(source_by_region.get(region, {}))
            for name in DETAIL_FIELDS:
                if name in raw:
                    values[name] = raw[name]
            values["地区"] = region

            if "入账额" in values:
                values["入账额"] = _amount(values["入账额"])
            if "指标数值" in values:
                values["指标数值"] = _amount(values["指标数值"], "指标数值")
            if "日期" in values:
                values["日期"] = str(values["日期"])

            missing = [name for name in selected if values.get(name) in (None, "")]
            if missing:
                raise HandoverError(
                    f"地区 {region} 缺少交接字段：{'、'.join(missing)}", 400)
            normalized.append(values)
        return selected, normalized

    # ---- 更正与版本 ----
    def correct(
        self,
        task_id: int,
        acceptance_no: str,
        *,
        region: str,
        amount: Any = None,
        delta: Any = None,
    ) -> dict[str, Any]:
        with self._lock:
            order = self._get_order(task_id, acceptance_no)
            current = order.current
            if current.status in (STATUS_CLAIMED, STATUS_CONTINUED, STATUS_REVOKED):
                raise HandoverError(
                    f"当前交接状态为「{current.status}」，不能再更正；如需变更请另发受理单", 409)

            region = str(region or "").strip()
            declared_regions = current.declared_regions
            if region not in declared_regions:
                raise HandoverError(
                    f"更正地区「{region}」不在受理单范围内：{'、'.join(declared_regions)}", 400)

            previous_value = next(
                (row.get("入账额") for row in current.declared_rows
                 if str(row.get("地区")) == region),
                None,
            )
            if amount is None:
                if delta is None:
                    raise HandoverError("更正需要给出新的入账额或更正差额", 400)
                base = _amount(previous_value if previous_value is not None else 0)
                corrected_amount = _amount(base + _amount(delta, "更正差额"), "入账额")
            else:
                corrected_amount = _amount(amount)

            new_declared: list[dict[str, Any]] = []
            for row in current.declared_rows:
                item = dict(row)
                if str(item.get("地区")) == region:
                    item["入账额"] = corrected_amount
                    if "指标数值" in item:
                        item["指标数值"] = corrected_amount
                new_declared.append(item)

            version = HandoverVersion(
                version=current.version + 1,
                handover_no=self._next_handover_no(),
                declared_rows=new_declared,
                status=STATUS_WRITING,
                created_at=_now(),
            )
            carried: set[str] = set()
            for declared in new_declared:
                declared_region = str(declared.get("地区"))
                if declared_region == region:
                    # 被更正的这笔重新取数，形成一条新的已确认批次。
                    batch = self._next_batch(
                        values=declared,
                        query_id=order.query_id,
                        acceptance_no=order.acceptance_no,
                        version=version.version,
                    )
                    version.lines.append(
                        HandoverLine(values=dict(declared), batch_id=batch.batch_id))
                    continue
                prior = next(
                    (line for line in current.lines
                     if str(line.values.get("地区")) == declared_region),
                    None,
                )
                if prior is not None:
                    version.lines.append(
                        HandoverLine(values=dict(prior.values), batch_id=prior.batch_id))
                    carried.add(prior.batch_id)

            version.status = STATUS_WRITING if version.pending_regions else STATUS_READY

            if current.lines and all(line.batch_id in carried for line in current.lines):
                # 原版本已写入的每一笔都被新版本接续，原单属于“接续完成”。
                current.status = STATUS_CONTINUED
            else:
                # 原版本没有可接续的明细：要么没写入，要么已写地区被重新取数替换。
                current.status = STATUS_REVOKED

            order.versions.append(version)
            self._sync_task(store.find("data_export", task_id), order)

            message = (
                f"更正已确认，生成交接版本 v{version.version}；"
                f"原版本 v{current.version} 标记为「{current.status}」"
            )
            return self._public(order, version, message=message)

    # ---- 领取与回执 ----
    def claim(
        self,
        task_id: int,
        acceptance_no: str,
        *,
        version: int | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            order = self._get_order(task_id, acceptance_no)
            target = self._pick_version(order, version)

            if target.status == STATUS_CLAIMED:
                return self._public(
                    order,
                    target,
                    duplicate=True,
                    message="该交接已领取，返回原回执，未生成第二份有效交接",
                )
            if target.version != order.current.version:
                raise HandoverError(
                    f"版本 v{target.version} 状态为「{target.status}」，"
                    f"当前有效版本为 v{order.current.version}；"
                    + ("原单已接续完成，请按当前版本领取"
                       if target.status == STATUS_CONTINUED
                       else "原单已撤销或被取代，请核对对账结论"),
                    409,
                )
            if not target.complete():
                raise HandoverError(
                    f"交接件尚未完整（缺少：{'、'.join(target.pending_regions)}），不能领取", 409)
            if target.status != STATUS_READY:
                raise HandoverError(f"当前交接状态为「{target.status}」，不能领取", 409)

            receipt = self._issue_receipt(order, target)
            target.receipt = receipt
            target.snapshot = [dict(line.values) for line in target.lines]
            target.status = STATUS_CLAIMED
            target.claimed_at = _now()
            self._sync_task(store.find("data_export", task_id), order,
                            claimed_version=target)

            return self._public(
                order,
                target,
                message="交接件完整，已领取；明细与回执使用同一批已确认取数",
            )

    def _issue_receipt(
        self, order: AcceptanceOrder, version: HandoverVersion,
    ) -> dict[str, Any]:
        projected = [
            self._project(line.values, order.fields) for line in version.lines
        ]
        batches = [self._batches[line.batch_id] for line in version.lines]
        lineage = []
        for row, batch in zip(projected, batches):
            entry = dict(row)
            entry["fetch_batch_id"] = batch.batch_id
            entry["sequence"] = batch.sequence
            lineage.append(entry)

        content = self._render_csv(order.fields, projected)
        total = None
        if "入账额" in order.fields:
            total = _amount(sum(_amount(row.get("入账额")) for row in projected))

        return {
            "receipt_no": f"HZ-{order.acceptance_no}-V{version.version}",
            "acceptance_no": order.acceptance_no,
            "handover_no": version.handover_no,
            "version": version.version,
            "fields": list(order.fields),
            "row_count": len(projected),
            "rows": projected,
            "total_amount": total,
            "batch_ids": [batch.batch_id for batch in batches],
            "batches": [batch.public() for batch in batches],
            "lineage": lineage,
            "confirmed": all(batch.confirmed for batch in batches),
            "confirmation": (
                "已确认" if all(batch.confirmed for batch in batches) else "待确认"),
            # 与明细文件同一份字节的摘要，财务可据此确认拿到的就是这份回执对应的明细。
            "detail_sha256": hashlib.sha256(content).hexdigest(),
            "issued_at": _now(),
        }

    # ---- 查询与文件 ----
    def detail(
        self,
        task_id: int,
        acceptance_no: str,
        *,
        version: int | None = None,
        require_claimed: bool = False,
    ) -> dict[str, Any]:
        with self._lock:
            order = self._get_order(task_id, acceptance_no)
            target = self._pick_version(order, version)
            if target.version != order.current.version and target.status != STATUS_CLAIMED:
                # 已接续 / 已撤销的旧版本不再作为交接件下发，避免财务拿到失效文件。
                raise HandoverError(
                    f"版本 v{target.version} 状态为「{target.status}」，不能下载；"
                    f"请按当前版本 v{order.current.version} 领取", 409)
            if not target.complete():
                raise HandoverError(
                    f"交接件尚未完整（缺少：{'、'.join(target.pending_regions)}），不能下载", 409)
            if require_claimed and target.status != STATUS_CLAIMED:
                raise HandoverError(
                    "交接件完整但尚未领取，请先领取，明细文件与回执同批签发", 409)

            if target.snapshot is not None:
                rows = [self._project(row, order.fields) for row in target.snapshot]
            else:
                rows = [self._project(line.values, order.fields) for line in target.lines]
            content = self._render_csv(order.fields, rows)
            batches = [line.batch_id for line in target.lines]
            headers = {
                "X-Acceptance-No": order.acceptance_no,
                "X-Handover-No": target.handover_no,
                "X-Handover-Version": str(target.version),
                "X-Handover-Status": target.status,
                "X-Fetch-Batches": ",".join(batches),
                "X-Detail-SHA256": hashlib.sha256(content).hexdigest(),
            }
            if target.receipt:
                headers["X-Receipt-No"] = str(target.receipt["receipt_no"])
            return {
                "filename": f"reconciliation-{order.acceptance_no}-v{target.version}.csv",
                "content": content,
                "headers": headers,
                "fields": list(order.fields),
                "version": target.version,
                "status": target.status,
                "batch_ids": batches,
                "complete": True,
            }

    def receipt(
        self,
        task_id: int,
        acceptance_no: str,
        *,
        version: int | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            order = self._get_order(task_id, acceptance_no)
            target = self._pick_version(order, version)
            if target.receipt is None:
                raise HandoverError(
                    f"版本 v{target.version} 尚未领取，没有受理回执；请先领取完整交接件", 409)
            return {
                "ok": True,
                "acceptance_no": order.acceptance_no,
                "version": target.version,
                "status": target.status,
                "receipt": target.receipt,
            }

    def get(
        self,
        task_id: int,
        acceptance_no: str,
        *,
        version: int | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            order = self._get_order(task_id, acceptance_no)
            target = self._pick_version(order, version)
            return self._public(order, target)

    def list_orders(self, task_id: int) -> dict[str, Any]:
        if store.find("data_export", task_id) is None:
            raise HandoverError(f"导出任务 {task_id} 不存在", 404)
        with self._lock:
            items = [
                self._summary(order) for order in self._orders.values()
                if order.task_id == task_id
            ]
        return {"ok": True, "task_id": task_id, "items": items, "total": len(items)}

    def reconciliation(
        self,
        task_id: int,
        acceptance_no: str,
        *,
        version: int | None = None,
    ) -> dict[str, Any]:
        """给财务的对账结论：用了哪几次取数、原单接续还是撤销、是否需要另发单。"""
        with self._lock:
            order = self._get_order(task_id, acceptance_no)
            target = self._pick_version(order, version)
            batches = [self._batches[line.batch_id] for line in target.lines]
            received = {
                str(line.values.get("地区")): line.values.get("入账额")
                for line in target.lines
            }
            regions = []
            for declared in target.declared_rows:
                region = str(declared.get("地区"))
                expected = declared.get("入账额")
                actual = received.get(region)
                if actual is None:
                    status = "未写入"
                elif expected == actual:
                    status = "一致"
                else:
                    status = "差异"
                batch = next(
                    (item for item in batches if str(item.values.get("地区")) == region),
                    None,
                )
                regions.append({
                    "region": region,
                    "declared_amount": expected,
                    "received_amount": actual,
                    "difference": (
                        None if actual is None
                        else _amount(actual) - _amount(expected)
                    ),
                    "fetch_batch_id": batch.batch_id if batch else None,
                    "fetch_sequence": batch.sequence if batch else None,
                    "status": status,
                })

            declared_total = (
                _amount(sum(_amount(row.get("入账额")) for row in target.declared_rows))
                if "入账额" in order.fields else None
            )
            received_total = (
                _amount(sum(_amount(value) for value in received.values()
                            if value is not None))
                if "入账额" in order.fields else None
            )

            original = order.versions[0]
            index = order.versions.index(target)
            successor = (
                order.versions[index + 1] if index + 1 < len(order.versions) else None
            )
            # 撤销但已有后继版本时，按后继版本继续办理即可；只有撤销且无后续才需另发单。
            new_acceptance_required = (
                target.status == STATUS_REVOKED and successor is None
            )

            batch_note = "；".join(
                f"{batch.values.get('地区')}使用第{batch.sequence}次取数({batch.batch_id})"
                for batch in batches
            ) or "尚无已确认取数"
            if target.status == STATUS_REVOKED:
                if successor is not None:
                    conclusion = (
                        f"版本 v{target.version} 已撤销并被 v{successor.version} 取代，"
                        f"无需另发受理单，请按 v{successor.version} 办理（{batch_note}）"
                    )
                else:
                    conclusion = (
                        f"版本 v{target.version} 已撤销，已写明细未有效接续；"
                        f"如需办理请另发受理单（{batch_note}）"
                    )
            elif target.status == STATUS_CONTINUED:
                next_no = successor.version if successor else target.version
                conclusion = (
                    f"版本 v{target.version} 已接续完成至 v{next_no}，无需另发受理单；"
                    f"请按 v{next_no} 办理（{batch_note}）"
                )
            elif target.status == STATUS_CLAIMED:
                conclusion = (
                    f"版本 v{target.version} 已按原单领取，无需另发受理单；"
                    f"明细与回执同一批次（{batch_note}）"
                )
            elif target.status == STATUS_READY:
                conclusion = (
                    f"版本 v{target.version} 交接件完整，可按原单领取，无需另发受理单（{batch_note}）"
                )
            else:
                conclusion = (
                    f"版本 v{target.version} 仍在写入（缺 "
                    f"{'、'.join(target.pending_regions)}），完整后方可领取，无需另发受理单"
                )

            return {
                "ok": True,
                "task_id": task_id,
                "acceptance_no": order.acceptance_no,
                "query_id": order.query_id,
                "fields": list(order.fields),
                "active_version": order.current.version,
                "version": target.version,
                "status": target.status,
                "complete": target.complete(),
                "claimed": target.status == STATUS_CLAIMED,
                "claimable": (
                    target.version == order.current.version
                    and target.status == STATUS_READY
                ),
                "receipt_no": (target.receipt or {}).get("receipt_no"),
                "original_version": {
                    "version": original.version,
                    "status": original.status,
                    "continued": original.status == STATUS_CONTINUED,
                    "revoked": original.status == STATUS_REVOKED,
                },
                "superseded_by": None if successor is None else {
                    "version": successor.version,
                    "status": successor.status,
                },
                "continued": target.status == STATUS_CONTINUED,
                "revoked": target.status == STATUS_REVOKED,
                "new_acceptance_required": new_acceptance_required,
                "batches": [batch.public() for batch in batches],
                "batch_ids": [batch.batch_id for batch in batches],
                "regions": regions,
                "declared_total": declared_total,
                "received_total": received_total,
                "total_matches": declared_total == received_total,
                "conclusion": conclusion,
            }

    def get_batch(self, task_id: int, acceptance_no: str, batch_id: str) -> dict[str, Any]:
        with self._lock:
            order = self._get_order(task_id, acceptance_no)
            batch = self._batches.get(batch_id)
            if batch is None or batch.acceptance_no != order.acceptance_no:
                raise HandoverError(f"取数批次 {batch_id} 不存在", 404)
            used = [
                {
                    "version": version.version,
                    "handover_no": version.handover_no,
                    "status": version.status,
                    "claimed": version.status == STATUS_CLAIMED,
                }
                for version in order.versions
                if any(line.batch_id == batch_id for line in version.lines)
            ]
            return {
                "ok": True,
                "acceptance_no": order.acceptance_no,
                "batch": batch.public(),
                "used_by_versions": used,
            }

    # ---- 输出辅助 ----
    def _public(
        self,
        order: AcceptanceOrder,
        version: HandoverVersion,
        *,
        duplicate: bool = False,
        message: str = "",
        payload_matches_existing: bool | None = None,
    ) -> dict[str, Any]:
        batches = [self._batches[line.batch_id] for line in version.lines]
        pending_regions = set(version.pending_regions)
        result: dict[str, Any] = {
            "ok": True,
            "task_id": order.task_id,
            "acceptance_no": order.acceptance_no,
            "handover_no": version.handover_no,
            "version": version.version,
            "status": version.status,
            "fields": list(order.fields),
            "query_id": order.query_id,
            "declared_rows": [
                self._project(row, order.fields) for row in version.declared_rows
            ],
            "written_rows": [
                self._project(line.values, order.fields) for line in version.lines
            ],
            "pending_rows": [
                self._project(row, order.fields) for row in version.declared_rows
                if str(row.get("地区")) in pending_regions
            ],
            "pending_regions": version.pending_regions,
            "written_regions": version.written_regions,
            "complete": version.complete(),
            "claimable": (
                version.version == order.current.version
                and version.status == STATUS_READY
            ),
            "confirmed": all(batch.confirmed for batch in batches),
            "lineage": [
                {
                    "region": line.values.get("地区"),
                    "fetch_batch_id": line.batch_id,
                    "sequence": self._batches[line.batch_id].sequence,
                }
                for line in version.lines
            ],
            "fetch_batches": [batch.public() for batch in batches],
            "receipt": version.receipt,
            "claimed": version.status == STATUS_CLAIMED,
            "versions": [
                {
                    "version": item.version,
                    "handover_no": item.handover_no,
                    "status": item.status,
                    "complete": item.complete(),
                    "claimed": item.status == STATUS_CLAIMED,
                    "pending_regions": item.pending_regions,
                }
                for item in order.versions
            ],
            "duplicate": duplicate,
            "message": message,
        }
        if payload_matches_existing is not None:
            result["payload_matches_existing"] = payload_matches_existing
        return result

    def _summary(self, order: AcceptanceOrder) -> dict[str, Any]:
        current = order.current
        return {
            "acceptance_no": order.acceptance_no,
            "task_id": order.task_id,
            "query_id": order.query_id,
            "fields": list(order.fields),
            "handover_no": current.handover_no,
            "version": current.version,
            "status": current.status,
            "complete": current.complete(),
            "claimable": (
                current.version == order.current.version
                and current.status == STATUS_READY
            ),
            "claimed": current.status == STATUS_CLAIMED,
            "written_regions": current.written_regions,
            "pending_regions": current.pending_regions,
            "declared_rows": [
                self._project(row, order.fields) for row in current.declared_rows
            ],
            "written_rows": [
                self._project(line.values, order.fields) for line in current.lines
            ],
            "versions": [
                {
                    "version": item.version,
                    "status": item.status,
                    "handover_no": item.handover_no,
                }
                for item in order.versions
            ],
            "created_at": order.created_at,
            "receipt_no": (current.receipt or {}).get("receipt_no"),
        }

    @staticmethod
    def _render_csv(fields: list[str], rows: list[dict[str, Any]]) -> bytes:
        buffer = io.StringIO()
        buffer.write("﻿")
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(fields)
        for row in rows:
            values = []
            for name in fields:
                value = row.get(name)
                text = "" if value is None else str(value)
                if text.startswith(("=", "+", "-", "@")) and not HandoverRepository._numeric(text):
                    # 与通用导出保持一致：避免表格把文本当公式执行。
                    text = "'" + text
                values.append(text)
            writer.writerow(values)
        return buffer.getvalue().encode("utf-8")

    @staticmethod
    def _numeric(text: str) -> bool:
        try:
            return math.isfinite(float(text.replace(",", "")))
        except ValueError:
            return False

    @staticmethod
    def _sync_task(
        task: dict[str, Any] | None,
        order: AcceptanceOrder,
        *,
        claimed_version: HandoverVersion | None = None,
    ) -> None:
        if task is None:
            return
        if claimed_version is not None:
            detail = HandoverRepository._render_csv(
                order.fields,
                [HandoverRepository._project(line.values, order.fields)
                 for line in claimed_version.lines],
            )
            task["导出状态"] = "已完成"
            task["status"] = "已完成"
            task["pending"] = False
            task["完成时间"] = _now()
            task["文件大小"] = f"{len(detail) / 1024:.1f}KB"
            return
        task["导出状态"] = "导出中"
        task["status"] = "导出中"
        task["pending"] = True


handover_repo = HandoverRepository()
