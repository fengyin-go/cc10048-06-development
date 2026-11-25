"""财务对账交接：受理、停住、更正、领取与批次追溯。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.handover import handover_repo

client = TestClient(app)

TASK = 1


@pytest.fixture(autouse=True)
def _clean_handover():
    handover_repo.reset()
    yield
    handover_repo.reset()


def _create(**overrides):
    payload = {
        "task_id": TASK,
        "query_id": 1,
        "fields": ["地区", "入账额"],
        "rows": [{"地区": "华东", "入账额": 60}, {"地区": "华南", "入账额": 40}],
        "stop_after": "华东",
    }
    payload.update(overrides)
    return client.post("/api/data_export/handover", json=payload)


class TestQueryResultsHandoff:
    def test_query_results_available_to_export(self):
        resp = client.get("/api/data_export/handover/query-results?query_id=1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["confirmed"] is True
        assert data["available"] is True
        assert {row["地区"] for row in data["rows"]} >= {"华东", "华南"}

    def test_archived_query_has_no_results(self):
        client.post("/api/dim_query/1/actions", json={"action": "分享方案"})
        # 3 号种子记录已是已分享；归档状态没有对应动作，用不存在方案验证错误分支。
        resp = client.get("/api/data_export/handover/query-results?query_id=99999")
        assert resp.status_code == 404
        assert resp.json()["ok"] is False


class TestCreateAndWriteStop:
    def test_stops_after_region_written(self):
        data = _create().json()
        assert data["ok"] is True
        assert data["status"] == "写入中"
        assert data["written_regions"] == ["华东"]
        assert data["pending_regions"] == ["华南"]
        assert data["complete"] is False
        assert data["claimable"] is False
        assert [row["地区"] for row in data["written_rows"]] == ["华东"]
        assert [row["入账额"] for row in data["written_rows"]] == [60]
        assert data["lineage"][0]["fetch_batch_id"].startswith("FETCH-")
        assert data["fields"] == ["地区", "入账额"]

    def test_unselected_fields_never_leak(self):
        data = _create().json()
        acceptance_no = data["acceptance_no"]
        assert data["fields"] == ["地区", "入账额"]
        for row in data["written_rows"]:
            assert set(row) == {"地区", "入账额"}
        for row in data["declared_rows"]:
            assert set(row) == {"地区", "入账额"}
        for batch in data["fetch_batches"]:
            assert set(batch) & {"渠道"} == set()

        detail = client.get(
            f"/api/data_export/handover/{acceptance_no}/detail?task_id={TASK}")
        assert detail.status_code == 409  # 尚未完整，不能取明细

    def test_incomplete_handover_cannot_be_claimed(self):
        data = _create().json()
        resp = client.post(
            f"/api/data_export/handover/{data['acceptance_no']}/claim",
            json={"task_id": TASK},
        )
        assert resp.status_code == 409
        assert "尚未完整" in resp.json()["message"]

    def test_incomplete_handover_cannot_be_downloaded(self):
        data = _create().json()
        resp = client.get(
            f"/api/data_export/handover/{data['acceptance_no']}/detail.csv?task_id={TASK}")
        assert resp.status_code == 409

    def test_complete_but_unclaimed_handover_cannot_download(self):
        data = _create(stop_after=None).json()
        assert data["complete"] is True
        assert data["claimable"] is True
        resp = client.get(
            f"/api/data_export/handover/{data['acceptance_no']}/detail.csv?task_id={TASK}")
        assert resp.status_code == 409
        assert "先领取" in resp.json()["message"]

    def test_fields_must_include_region(self):
        resp = _create(fields=["入账额"])
        assert resp.status_code == 400
        assert "地区" in resp.json()["message"]

    def test_unknown_field_rejected(self):
        resp = _create(fields=["地区", "成本"])
        assert resp.status_code == 400

    def test_duplicate_retry_single_valid_handover(self):
        first = _create().json()
        second = _create(acceptance_no=first["acceptance_no"]).json()
        assert second["duplicate"] is True
        assert second["acceptance_no"] == first["acceptance_no"]
        assert second["version"] == 1
        assert second["handover_no"] == first["handover_no"]
        assert second["payload_matches_existing"] is True

        listing = client.get(f"/api/data_export/handover?task_id={TASK}").json()
        assert listing["total"] == 1

    def test_retry_with_changed_payload_stays_same_order(self):
        first = _create().json()
        second = _create(
            acceptance_no=first["acceptance_no"],
            rows=[{"地区": "华东", "入账额": 99}, {"地区": "华南", "入账额": 40}],
            stop_after=None,
        ).json()
        assert second["duplicate"] is True
        assert second["payload_matches_existing"] is False
        assert second["version"] == 1
        assert second["written_regions"] == ["华东"]


class TestCorrectionFlow:
    def _stopped(self):
        return _create().json()

    def test_correction_creates_new_version_and_continues_original(self):
        first = self._stopped()
        acceptance_no = first["acceptance_no"]

        corrected = client.post(
            f"/api/data_export/handover/{acceptance_no}/corrections",
            json={"task_id": TASK, "region": "华南", "amount": 45},
        ).json()
        assert corrected["version"] == 2
        assert corrected["status"] == "待领取"
        assert corrected["complete"] is True
        assert corrected["claimable"] is True
        assert corrected["written_regions"] == ["华东", "华南"]
        assert corrected["written_rows"] == [
            {"地区": "华东", "入账额": 60},
            {"地区": "华南", "入账额": 45},
        ]

        versions = {item["version"]: item for item in corrected["versions"]}
        assert versions[1]["status"] == "接续完成"

        # 华东沿用原批次，华南重新取数
        line_by_region = {item["region"]: item for item in corrected["lineage"]}
        assert line_by_region["华东"]["fetch_batch_id"] == first["lineage"][0]["fetch_batch_id"]
        assert line_by_region["华南"]["fetch_batch_id"] != first["lineage"][0]["fetch_batch_id"]
        assert line_by_region["华南"]["sequence"] > line_by_region["华东"]["sequence"]

    def test_five_yuan_delta_correction(self):
        first = self._stopped()
        acceptance_no = first["acceptance_no"]
        corrected = client.post(
            f"/api/data_export/handover/{acceptance_no}/corrections",
            json={"task_id": TASK, "region": "华南", "delta": 5},
        ).json()
        assert corrected["version"] == 2
        assert {row["地区"]: row["入账额"] for row in corrected["written_rows"]} == {
            "华东": 60, "华南": 45,
        }
        receipt = client.post(
            f"/api/data_export/handover/{acceptance_no}/claim",
            json={"task_id": TASK},
        ).json()["receipt"]
        assert receipt["total_amount"] == 105

    def test_claim_original_acceptance_no(self):
        first = self._stopped()
        acceptance_no = first["acceptance_no"]
        client.post(
            f"/api/data_export/handover/{acceptance_no}/corrections",
            json={"task_id": TASK, "region": "华南", "amount": 45},
        )

        claimed = client.post(
            f"/api/data_export/handover/{acceptance_no}/claim",
            json={"task_id": TASK},
        ).json()
        assert claimed["claimed"] is True
        assert claimed["status"] == "已领取"
        assert claimed["version"] == 2

        receipt = client.get(
            f"/api/data_export/handover/{acceptance_no}/receipt?task_id={TASK}").json()
        assert receipt["version"] == 2
        body = receipt["receipt"]
        assert body["total_amount"] == 105
        assert body["row_count"] == 2
        assert body["fields"] == ["地区", "入账额"]
        assert body["confirmation"] == "已确认"
        for row in body["rows"]:
            assert set(row) == {"地区", "入账额"}
        assert len(body["batch_ids"]) == 2
        assert len(set(body["batch_ids"])) == 2
        for item in body["lineage"]:
            assert item["fetch_batch_id"] in body["batch_ids"]

    def test_detail_file_matches_receipt_and_excludes_channel(self):
        first = self._stopped()
        acceptance_no = first["acceptance_no"]
        client.post(
            f"/api/data_export/handover/{acceptance_no}/corrections",
            json={"task_id": TASK, "region": "华南", "amount": 45},
        )
        claimed = client.post(
            f"/api/data_export/handover/{acceptance_no}/claim",
            json={"task_id": TASK},
        ).json()
        receipt = claimed["receipt"]

        resp = client.get(
            f"/api/data_export/handover/{acceptance_no}/detail.csv?task_id={TASK}")
        assert resp.status_code == 200
        assert "text/csv" in resp.headers["content-type"]
        assert resp.headers["X-Detail-SHA256"] == receipt["detail_sha256"]
        assert resp.headers["X-Receipt-No"] == receipt["receipt_no"]
        assert set(resp.headers["X-Fetch-Batches"].split(",")) == set(receipt["batch_ids"])

        text = resp.content.decode("utf-8-sig")
        lines = text.splitlines()
        assert lines[0] == "地区,入账额"
        assert "渠道" not in text
        assert lines[1:] == ["华东,60", "华南,45"]

    def test_reconciliation_after_claim(self):
        first = self._stopped()
        acceptance_no = first["acceptance_no"]
        client.post(
            f"/api/data_export/handover/{acceptance_no}/corrections",
            json={"task_id": TASK, "region": "华南", "amount": 45},
        )
        client.post(
            f"/api/data_export/handover/{acceptance_no}/claim",
            json={"task_id": TASK},
        )
        report = client.get(
            f"/api/data_export/handover/{acceptance_no}/reconciliation?task_id={TASK}").json()
        assert report["claimed"] is True
        assert report["declared_total"] == 105
        assert report["received_total"] == 105
        assert report["total_matches"] is True
        assert report["original_version"]["continued"] is True
        assert report["original_version"]["status"] == "接续完成"
        assert report["new_acceptance_required"] is False
        assert "无需另发受理单" in report["conclusion"]

        regions = {item["region"]: item for item in report["regions"]}
        assert regions["华南"]["received_amount"] == 45
        assert regions["华南"]["difference"] == 0
        assert regions["华东"]["fetch_batch_id"] != regions["华南"]["fetch_batch_id"]
        assert regions["华南"]["fetch_sequence"] > regions["华东"]["fetch_sequence"]

    def test_claiming_superseded_version_reports_continued(self):
        first = self._stopped()
        acceptance_no = first["acceptance_no"]
        client.post(
            f"/api/data_export/handover/{acceptance_no}/corrections",
            json={"task_id": TASK, "region": "华南", "amount": 45},
        )
        resp = client.post(
            f"/api/data_export/handover/{acceptance_no}/claim",
            json={"task_id": TASK, "version": 1},
        )
        assert resp.status_code == 409
        assert "接续完成" in resp.json()["message"]

        original = client.get(
            f"/api/data_export/handover/{acceptance_no}/reconciliation?task_id={TASK}&version=1"
        ).json()
        assert original["continued"] is True
        assert original["new_acceptance_required"] is False
        assert "接续完成" in original["conclusion"]

    def test_correction_after_claim_requires_new_acceptance(self):
        first = self._stopped()
        acceptance_no = first["acceptance_no"]
        client.post(
            f"/api/data_export/handover/{acceptance_no}/corrections",
            json={"task_id": TASK, "region": "华南", "amount": 45},
        )
        client.post(
            f"/api/data_export/handover/{acceptance_no}/claim",
            json={"task_id": TASK},
        )
        resp = client.post(
            f"/api/data_export/handover/{acceptance_no}/corrections",
            json={"task_id": TASK, "region": "华南", "amount": 50},
        )
        assert resp.status_code == 409
        assert "另发受理单" in resp.json()["message"]

    def test_repeated_claim_returns_same_receipt(self):
        first = _create(stop_after=None).json()
        acceptance_no = first["acceptance_no"]
        one = client.post(
            f"/api/data_export/handover/{acceptance_no}/claim",
            json={"task_id": TASK},
        ).json()
        two = client.post(
            f"/api/data_export/handover/{acceptance_no}/claim",
            json={"task_id": TASK},
        ).json()
        assert two["duplicate"] is True
        assert two["receipt"]["receipt_no"] == one["receipt"]["receipt_no"]
        assert two["version"] == one["version"]

    def test_superseded_version_detail_file_not_served(self):
        first = self._stopped()
        acceptance_no = first["acceptance_no"]
        client.post(
            f"/api/data_export/handover/{acceptance_no}/corrections",
            json={"task_id": TASK, "region": "华南", "amount": 45},
        )
        resp = client.get(
            f"/api/data_export/handover/{acceptance_no}/detail.csv?task_id={TASK}&version=1")
        assert resp.status_code == 409
        assert "v2" in resp.json()["message"]

    def test_correction_of_written_region_revokes_original(self):
        first = _create().json()  # 华东已写，华南未写
        acceptance_no = first["acceptance_no"]
        corrected = client.post(
            f"/api/data_export/handover/{acceptance_no}/corrections",
            json={"task_id": TASK, "region": "华东", "amount": 70},
        ).json()
        versions = {item["version"]: item for item in corrected["versions"]}
        assert versions[1]["status"] == "已撤销"

        report = client.get(
            f"/api/data_export/handover/{acceptance_no}/reconciliation?task_id={TASK}&version=1"
        ).json()
        assert report["revoked"] is True
        assert report["superseded_by"]["version"] == 2
        assert report["new_acceptance_required"] is False

    def test_correction_outside_declared_regions_rejected(self):
        first = self._stopped()
        resp = client.post(
            f"/api/data_export/handover/{first['acceptance_no']}/corrections",
            json={"task_id": TASK, "region": "华北", "amount": 10},
        )
        assert resp.status_code == 400


class TestBatchTraceability:
    def test_every_row_traces_to_confirmed_batch(self):
        data = _create(stop_after=None).json()
        acceptance_no = data["acceptance_no"]
        for item in data["lineage"]:
            resp = client.get(
                f"/api/data_export/handover/{acceptance_no}/batches/"
                f"{item['fetch_batch_id']}?task_id={TASK}"
            )
            assert resp.status_code == 200
            batch = resp.json()["batch"]
            assert batch["confirmed"] is True
            assert batch["confirmation"] == "已确认"
            assert batch["region"] == item["region"]
            assert batch["acceptance_no"] == acceptance_no

    def test_batch_of_other_acceptance_not_visible(self):
        first = _create(stop_after=None).json()
        other = _create(
            acceptance_no=None, rows=[{"地区": "华北", "入账额": 35}], stop_after=None,
        ).json()
        batch_id = other["lineage"][0]["fetch_batch_id"]
        resp = client.get(
            f"/api/data_export/handover/{first['acceptance_no']}/batches/"
            f"{batch_id}?task_id={TASK}"
        )
        assert resp.status_code == 404


class TestExportExecution:
    def test_execute_exposes_downloadable_detail(self):
        first = _create(stop_after=None).json()
        result = client.get(f"/api/domain/data_export/{TASK}/execute").json()
        assert result["detail_available"] is True
        assert result["handover"]["acceptance_no"] == first["acceptance_no"]
        assert result["handover"]["detail_filename"].endswith(".csv")
        assert result["handover"]["fetch_batches"]
        assert result["handover"]["detail_url"].startswith("/api/data_export/handover/")
        # 未领取前任务仍在导出中，领取后才算完成。
        task = client.get(f"/api/data_export/{TASK}").json()
        assert task["status"] == "导出中"

        client.post(
            f"/api/data_export/handover/{first['acceptance_no']}/claim",
            json={"task_id": TASK},
        )
        result = client.get(f"/api/domain/data_export/{TASK}/execute").json()
        assert result["handover"]["claimed"] is True
        task = client.get(f"/api/data_export/{TASK}").json()
        assert task["status"] == "已完成"

    def test_execute_reports_incomplete_handover(self):
        first = _create().json()
        result = client.get(f"/api/domain/data_export/{TASK}/execute").json()
        assert result["detail_available"] is False
        assert result["handover"]["complete"] is False
        assert result["handover"]["acceptance_no"] == first["acceptance_no"]
        assert "完整后方可领取" in result["message"]

    def test_execute_without_handover_says_preview_only(self):
        result = client.get(f"/api/domain/data_export/{TASK}/execute").json()
        assert result["detail_available"] is False
        assert result["handover"] is None
        assert "预览" in result["message"]


class TestOrderNotFound:
    def test_unknown_acceptance(self):
        resp = client.get("/api/data_export/handover/SL-NOPE/reconciliation?task_id=1")
        assert resp.status_code == 404

    def test_unknown_task(self):
        resp = client.get("/api/data_export/handover?task_id=99999")
        assert resp.status_code == 404
