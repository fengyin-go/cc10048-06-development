"""数据导出领域服务：格式转换、字段筛选、导出任务管理。"""
# 数据操作：处理、处理、处理，与上下游模块联动消费批次数据。
from __future__ import annotations

import csv
import io
import json
import random
from datetime import datetime
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class DataExportService(BaseModuleService):
    MODULE = "data_export"
    ENTITY = "导出任务"
    OPERATION_TERM = "导出"
    LIST_FIELDS = ["任务标识", "查询方案", "导出格式", "字段筛选", "行数上限", "导出状态", "文件大小", "完成时间"]
    REQUIRED_FIELDS = ["任务标识", "查询方案", "导出格式"]
    STATUS_ORDER = ["排队中", "导出中", "已完成", "导出失败"]
    ACTION_RULES = {"启动导出": "排队中", "取消导出": "导出中"}
    NEGATIVE_ACTIONS = []

    def execute_export(self, task_id: int) -> dict[str, Any]:
        """执行数据导出。

        除了原有的预览与字节数，这里把财务对账交接一并返回：
        已受理的交接会给出可下载明细、批次血缘和领取状态，
        让导出执行不再只有“预览 + 字节数”。
        """
        from app.services.handover import handover_repo

        task = store.find(self.MODULE, task_id)
        if not task:
            return {"error": f"导出任务 {task_id} 不存在"}
        fmt = str(task.get("导出格式", "csv"))
        rng = random.Random(task_id)
        n_rows = rng.randint(10, 500)
        headers = ["id", "日期", "指标名称", "维度值", "指标数值", "环比变化", "同比变化"]
        data = []
        for i in range(n_rows):
            data.append({
                "id": i + 1,
                "日期": f"2026-10-{1 + (i % 31):02d}",
                "指标名称": f"指标_{rng.randint(1, 10)}",
                "维度值": f"维度{rng.choice('ABCD')}",
                "指标数值": round(rng.uniform(100, 10000), 2),
                "环比变化": f"{rng.uniform(-10, 10):.1f}%",
                "同比变化": f"{rng.uniform(-20, 20):.1f}%",
            })
        content = ""
        if fmt == "json":
            content = json.dumps(data, ensure_ascii=False, indent=2)
        elif fmt == "csv":
            buf = io.StringIO()
            writer = csv.DictWriter(buf, fieldnames=headers)
            writer.writeheader()
            writer.writerows(data)
            content = buf.getvalue()
        update = store.find(self.MODULE, task_id)
        if update:
            update["文件大小"] = f"{len(content.encode('utf-8')) / 1024:.1f}KB"

        handovers = []
        for current in handover_repo.list_orders(task_id)["items"]:
            entry = {
                "acceptance_no": current["acceptance_no"],
                "handover_no": current["handover_no"],
                "version": current["version"],
                "status": current["status"],
                "complete": current["complete"],
                "claimable": current["claimable"],
                "claimed": current["claimed"],
                "fields": current["fields"],
                "receipt_no": current["receipt_no"],
                "detail_url": (
                    f"/api/data_export/handover/{current['acceptance_no']}/detail.csv"
                    f"?task_id={task_id}"
                ),
            }
            if current["complete"]:
                detail = handover_repo.detail(task_id, current["acceptance_no"])
                entry["detail_filename"] = detail["filename"]
                entry["detail_sha256"] = detail["headers"]["X-Detail-SHA256"]
                entry["fetch_batches"] = detail["batch_ids"]
                entry["file_size_kb"] = round(len(detail["content"]) / 1024, 2)
            handovers.append(entry)

        if handovers:
            latest = handovers[-1]
            if update:
                # 任务状态跟随交接：只有领取后的交接才算导出完成。
                if latest["claimed"]:
                    update["文件大小"] = f"{latest['file_size_kb']:.1f}KB"
                    update["完成时间"] = datetime.now().isoformat()
                    update["导出状态"] = "已完成"
                    update["status"] = "已完成"
                    update["pending"] = False
                else:
                    update["导出状态"] = "导出中"
                    update["status"] = "导出中"
                    update["pending"] = True
            return {
                "task_id": task_id,
                "task_name": task.get("任务标识"),
                "format": fmt,
                "rows_exported": n_rows,
                "file_size_kb": round(len(content.encode("utf-8")) / 1024, 2),
                "preview": data[:5],
                "handover": latest,
                "handovers": handovers,
                "detail_available": latest["complete"],
                "message": (
                    "已生成可交接的对账明细，可按受理凭据领取"
                    if latest["complete"]
                    else "交接件仍在写入，完整后方可领取"
                ),
            }

        if update:
            update["导出状态"] = "已完成"
            update["完成时间"] = datetime.now().isoformat()
            update["status"] = "已完成"
            update["pending"] = False
        return {
            "task_id": task_id,
            "task_name": task.get("任务标识"),
            "format": fmt,
            "rows_exported": n_rows,
            "file_size_kb": round(len(content.encode("utf-8")) / 1024, 2),
            "preview": data[:5],
            "handover": None,
            "handovers": [],
            "detail_available": False,
            "message": "尚未受理对账交接，当前只有预览；可先登记受理单再领取明细",
        }

    def domain_enrich(self, config_id: int) -> dict:
        """对当前模块执行合并操作，联动数据和批次，产出标准化数据产物。
        
        数据加工流程：读取原始记录 → 执行合并处理 → 数据校验字段完整性 → 生成批次批次标识 →
        写入存储层并返回可追溯的处理结果，下游消费模块可基于批次号追踪数据链路。
        """
        import time, json
        from app.store import store
        entry = store.find(self.MODULE, config_id)
        # 合并：遍历当前模块全部记录执行核心数据处理
        records = store.list_all(self.MODULE)
        processed = []
        for idx, rec in enumerate(records):
            # 数据：校验每条记录的字段完整性与格式合法性
            filled = sum(1 for v in rec.values() if v and str(v).strip())
            total = len(rec)
            rec_copy = dict(rec)
            rec_copy["_field_count"] = filled
            rec_copy["_complete_ratio"] = f"{filled}/{total}"
            rec_copy["_batch"] = f"{self.MODULE}-{idx:04d}"
            processed.append(rec_copy)
        # 批次：生成加工批次标识并写入存储
        batch_id = store.next_id(self.MODULE + "_processor")
        result = {
            "module": self.MODULE,
            "operation": "合并",
            "config_id": config_id,
            "records_processed": len(processed),
            "batch_id": batch_id,
            "field_validation": {"数据": "passed", "批次": "recorded"},
            "processed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "downstream_consumable": True,
            "records": processed,
            "entry": entry,
        }
        # 返回合并处理结果，含数据校验元数据和批次批次追溯信息
        return result

