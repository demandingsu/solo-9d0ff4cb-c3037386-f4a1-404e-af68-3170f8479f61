from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from training_portal import main as app_main
from training_portal.service import Portal


@pytest.fixture()
def portal(tmp_path):
    instance = Portal(tmp_path / "data")
    instance.start_workers()
    yield instance
    instance.shutdown()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    instance = Portal(tmp_path / "data")
    monkeypatch.setattr(app_main, "portal", instance)
    with TestClient(app_main.app) as test_client:
        yield test_client, instance
    instance.shutdown()


CSV_SPRING = """batch,respondent_id,text
B1,R1,春天第一段访谈正文
B1,R2,春天第二段访谈正文
B1,R3,春天第三段访谈正文
B2,R4,另一个批次的正文
"""


def upload(instance: Portal, csv_text: str, name: str = "u.csv") -> dict:
    return instance.ingest_upload(name, csv_text.encode("utf-8"))


def wait_for(instance: Portal, job_id: int, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for job in instance.jobs():
            if job["id"] == job_id:
                if job["status"] in ("completed", "blocked", "failed"):
                    return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not settle")


# ---------------------------------------------------------------------------
# ingestion
# ---------------------------------------------------------------------------
def test_upload_parses_headers_and_reports_counts(portal):
    result = upload(portal, CSV_SPRING)
    assert result["accepted"] == 4
    assert result["batches"] == ["B1", "B2"]
    assert portal.stats()["utterances"] == 4


def test_upload_accepts_chinese_headers(portal):
    csv_text = "访谈批次,受访者编号,文本\nB1,R1,中文表头也支持\n"
    assert upload(portal, csv_text)["accepted"] == 1


def test_upload_rejects_missing_column(portal):
    from training_portal.service import ValidationError

    with pytest.raises(ValidationError):
        upload(portal, "batch,text\nB1,缺一列\n")


# ---------------------------------------------------------------------------
# generation
# ---------------------------------------------------------------------------
def test_generation_produces_csv_for_selected_batch(portal):
    upload(portal, CSV_SPRING)
    video = portal.create_job("B1")
    job = wait_for(portal, video)
    assert job["status"] == "completed"

    export = portal.exports()[0]
    handle, name, error = portal.open_download(export["id"])
    assert error is None and handle is not None
    content = handle.read().decode("utf-8")
    handle.close()
    assert "春天第一段访谈正文" in content
    assert "另一个批次的正文" not in content
    assert content.splitlines()[0] == "batch,respondent_id,text"


def test_generation_of_empty_batch_is_rejected(portal):
    from training_portal.service import ValidationError

    with pytest.raises(ValidationError):
        portal.create_job("does-not-exist")


# ---------------------------------------------------------------------------
# revocation
# ---------------------------------------------------------------------------
def test_revocation_deletes_rows_and_locks_existing_exports(portal):
    upload(portal, CSV_SPRING)
    job_id = portal.create_job("B1")
    wait_for(portal, job_id)
    export_id = portal.exports()[0]["id"]

    result = portal.revoke("R2")
    assert result["deleted_rows"] == 1
    assert result["affected_exports"] == 1

    handle, _name, error = portal.open_download(export_id)
    assert handle is None
    assert "撤回" in error
    assert not [e for e in portal.exports() if e["id"] == export_id and e["status"] == "ready"]


def test_revocation_keeps_unaffected_files_downloadable(portal):
    upload(portal, CSV_SPRING)
    job_b1 = portal.create_job("B1")
    job_b2 = portal.create_job("B2")
    wait_for(portal, job_b1)
    wait_for(portal, job_b2)
    exports = {e["batch"]: e for e in portal.exports()}

    portal.revoke("R1")

    handle, _name, error = portal.open_download(exports["B2"]["id"])
    assert error is None and handle is not None
    handle.close()


def test_revocation_blocks_later_upload_of_same_id(portal):
    upload(portal, CSV_SPRING)
    portal.revoke("R1")

    result = upload(portal, "batch,respondent_id,text\nB9,R1,重新上传同号文本\n")
    assert result["accepted"] == 0
    assert result["skipped_revoked"] == 1
    assert "B9" not in [b["name"] for b in portal.batches()]


def test_revocation_blocks_original_text_under_new_id(portal):
    upload(portal, CSV_SPRING)
    portal.revoke("R1")

    csv_text = "batch,respondent_id,text\nB9,R1-ALT,春天第一段访谈正文\n"
    result = upload(portal, csv_text)
    assert result["accepted"] == 0
    assert result["skipped_revoked"] == 1


def test_revocation_is_persistent_across_restart(tmp_path):
    data_dir = tmp_path / "data"
    first = Portal(data_dir)
    upload(first, CSV_SPRING)
    first.revoke("R2")
    first.shutdown()

    second = Portal(data_dir)
    try:
        handle, _name, error = second.open_download(1) if second.exports() else (None, "", "")
        assert second.stats()["revocations"] == 1
        result = upload(second, "batch,respondent_id,text\nB1,R2,换新文本再试\n")
        assert result["skipped_revoked"] == 1
        assert handle is None
    finally:
        second.shutdown()


@pytest.mark.parametrize("respondent", ["R1", "R2", "R3", "R4"])
def test_revocation_of_any_respondent_leaves_consistent_state(portal, respondent):
    upload(portal, CSV_SPRING)
    job = portal.create_job("B1")
    wait_for(portal, job)
    portal.revoke(respondent)

    in_batch = respondent in {"R1", "R2", "R3"}
    for export in portal.exports():
        if export["status"] == "ready":
            handle, _name, error = portal.open_download(export["id"])
            assert error is None
            handle.close()

    export_id = portal.exports()[0]["id"]
    handle, _n, error = portal.open_download(export_id)
    if in_batch:
        assert handle is None and "撤回" in error
    else:
        # A respondent from another batch must not take this file down.
        assert handle is not None and error is None
        handle.close()


# ---------------------------------------------------------------------------
# mid-generation revocation
# ---------------------------------------------------------------------------
def test_revocation_during_generation_prevents_publication(client):
    test_client, instance = client
    repair = "\n".join(f"B1,R{i},第{i}段较长的访谈正文内容用于演示生成过程中的撤回" for i in range(200))
    upload(instance, "batch,respondent_id,text\n" + repair)
    instance.db.set_settings({"pause_generation": "1"})

    r = test_client.post("/generate", data={"batch": "B1"})
    assert r.status_code == 200

    deadline = time.monotonic() + 5
    paused = None
    while time.monotonic() < deadline:
        paused = next((j for j in instance.jobs() if j["status"] == "paused"), None)
        if paused:
            break
        time.sleep(0.05)
    assert paused is not None, "job never reached the pause gate"

    r = test_client.post("/revoke", data={"respondent_id": "R37"})
    assert r.status_code == 200

    instance.db.set_settings({"pause_generation": "0"})
    deadline = time.monotonic() + 5
    settled = None
    while time.monotonic() < deadline:
        settled = instance.jobs()[0]
        if settled["status"] in ("blocked", "completed", "failed"):
            break
        time.sleep(0.05)
    assert settled["status"] == "blocked"

    assert instance.exports() == []
    assert "R37" in r.text or "撤回" in r.text


# ---------------------------------------------------------------------------
# delete failures, pending cleanup, restarts
# ---------------------------------------------------------------------------
def test_delete_failure_leaves_pending_and_blocks_download(portal):
    upload(portal, CSV_SPRING)
    job = portal.create_job("B1")
    wait_for(portal, job)
    export_id = portal.exports()[0]["id"]

    portal.db.set_settings({"delete_failures_remaining": 2})
    portal.revoke("R2")

    deadline = time.monotonic() + 5
    export = None
    while time.monotonic() < deadline:
        export = next(e for e in portal.exports() if e["id"] == export_id)
        if export["status"] == "pending_delete" and export["delete_attempts"] >= 1:
            break
        time.sleep(0.05)
    assert export["status"] == "pending_delete"
    assert export["last_delete_error"]
    assert export["delete_attempts"] >= 1

    handle, _name, error = portal.open_download(export_id)
    assert handle is None and error

    # The file still exists on disk but is not reachable under a download name
    # until the cleanup budget is exhausted.
    path = portal.export_dir / export["stored_name"]
    assert path.exists()

    portal.db.set_settings({"delete_failures_remaining": 0})
    portal.retry_cleanup_now()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        export = next(e for e in portal.exports() if e["id"] == export_id)
        if export["status"] == "deleted":
            break
        time.sleep(0.05)
    assert export["status"] == "deleted"
    assert not path.exists()


def test_restart_continues_pending_cleanup_and_never_reopens(tmp_path):
    data_dir = tmp_path / "data"
    first = Portal(data_dir)
    first.start_workers()
    try:
        upload(first, CSV_SPRING)
        job = first.create_job("B1")
        wait_for(first, job)
        export_id = first.exports()[0]["id"]
        first.db.set_settings({"delete_always_fail": "1"})
        first.revoke("R1")
        time.sleep(0.3)
        pending = next(e for e in first.exports() if e["id"] == export_id)
        assert pending["status"] == "pending_delete"
        assert (first.export_dir / pending["stored_name"]).exists()
        handle, _n, error = first.open_download(export_id)
        assert handle is None
    finally:
        first.shutdown()

    second = Portal(data_dir)
    second.start_workers()
    try:
        # The switch is still on after restart: cleanup retries and still fails,
        # and the file remains non-downloadable.
        time.sleep(0.4)
        row = next(e for e in second.exports() if e["id"] == export_id)
        assert row["status"] == "pending_delete"
        handle, _n, error = second.open_download(export_id)
        assert handle is None and error

        # Clearing the switch lets the retry succeed; the file is gone for good.
        second.db.set_settings({"delete_always_fail": "0"})
        second.retry_cleanup_now()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            row = next(e for e in second.exports() if e["id"] == export_id)
            if row["status"] == "deleted":
                break
            time.sleep(0.05)
        assert row["status"] == "deleted"
        assert not (second.export_dir / row["stored_name"]).exists()
    finally:
        second.shutdown()


def test_startup_sweeps_orphan_files_and_demotes_missing_files(tmp_path):
    data_dir = tmp_path / "data"
    first = Portal(data_dir)
    first.start_workers()
    try:
        upload(first, CSV_SPRING)
        job = first.create_job("B1")
        wait_for(first, job)
        stored = first.exports()[0]["stored_name"]
    finally:
        first.shutdown()

    # Simulate an interrupted delete that left the file behind and an
    # interrupted publication that left a temp file.
    assert (data_dir / "exports" / stored).unlink() is None
    (data_dir / "exports" / "orphan.csv").write_text("leaked", encoding="utf-8")
    (data_dir / "tmp" / "half-written.csv").write_text("leaked", encoding="utf-8")

    second = Portal(data_dir)
    second.start_workers()
    try:
        assert not (data_dir / "exports" / "orphan.csv").exists()
        assert not list((data_dir / "tmp").glob("*.csv"))
        row = second.exports()[0]
        assert row["status"] == "pending_delete"
        handle, _n, error = second.open_download(row["id"])
        assert handle is None
    finally:
        second.shutdown()


def test_restart_fails_interrupted_jobs_and_keeps_queued(tmp_path):
    data_dir = tmp_path / "data"
    first = Portal(data_dir)
    try:
        upload(first, CSV_SPRING)
        # Simulate a crash while the generator held a job mid-run, and a
        # second job still waiting in the queue. The queued job carries the
        # same persisted corpus snapshot a real submission would have, so its
        # scope must survive the restart.
        with first.db.transaction() as conn:
            crashed = conn.execute(
                "INSERT INTO jobs(batch, status, requested_at) "
                "VALUES ('B1', 'running', ?)",
                ("2026-01-01T00:00:00+00:00",),
            )
            queued_cursor = conn.execute(
                "INSERT INTO jobs(batch, status, requested_at, total_rows) "
                "VALUES ('B2', 'queued', ?, 1)",
                ("2026-01-01T00:00:00+00:00",),
            )
            queued_id = int(queued_cursor.lastrowid)
            b2_row = conn.execute(
                "SELECT id, respondent_id FROM utterances WHERE batch = 'B2'"
            ).fetchone()
            conn.execute(
                "INSERT INTO job_utterances(job_id, utterance_id, respondent_id, "
                "position) VALUES (?, ?, ?, 0)",
                (queued_id, int(b2_row["id"]), b2_row["respondent_id"]),
            )
            assert int(crashed.lastrowid)
    finally:
        first.shutdown()

    second = Portal(data_dir)
    second.start_workers()
    try:
        jobs = {job["id"]: job for job in second.jobs()}
        crashed = next(j for j in jobs.values() if j["batch"] == "B1")
        queued = next(j for j in jobs.values() if j["batch"] == "B2")
        assert crashed["status"] == "failed"
        assert "重启" in crashed["message"]

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            queued = next(j for j in second.jobs() if j["batch"] == "B2")
            if queued["status"] in ("completed", "blocked", "failed"):
                break
            time.sleep(0.05)
        assert queued["status"] == "completed"
    finally:
        second.shutdown()


def test_download_blocks_text_withdrawn_before_export(portal):
    """Text ingested under another id must take an export down after revocation.

    This is the adversarial ordering: the duplicate copy is exported while its
    owner still has consent, then the real owner withdraws.
    """
    upload(
        portal,
        "batch,respondent_id,text\n"
        "B1,R-A,同一段访谈原文\n"
        "B1,R-B,另一段访谈原文\n"
        "B1,R-OTHER,同一段访谈原文\n",
    )
    job = portal.create_job("B1")
    wait_for(portal, job)
    export_id = portal.exports()[0]["id"]

    handle, _n, error = portal.open_download(export_id)
    assert error is None
    handle.close()

    portal.revoke("R-A")

    handle, _n, error = portal.open_download(export_id)
    assert handle is None and error
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        row = next(e for e in portal.exports() if e["id"] == export_id)
        if row["status"] == "deleted":
            break
        time.sleep(0.05)
    assert row["status"] == "deleted"


# ---------------------------------------------------------------------------
# fixed corpus scope: uploads interleaved with a submitted job
# ---------------------------------------------------------------------------
class _ChunkBarrier:
    """Deterministic interleaving point inside the chunked export loop.

    The generator parks after the first chunk has been streamed (the same
    boundary the real 20-row chunking creates), so the test can upload
    supplementary rows *while the job is already running* without relying on
    sleeps or timing luck.
    """

    def __init__(self) -> None:
        self._reached = threading.Event()
        self._release = threading.Event()

    def wait_reached(self, timeout: float = 5.0) -> None:
        if not self._reached.wait(timeout):
            raise AssertionError("generator never reached the chunk barrier")

    def release(self) -> None:
        self._release.set()

    def __call__(self, *args, **kwargs):
        self._reached.set()
        if not self._release.wait(timeout=10.0):
            raise AssertionError("chunk barrier was never released")
        return None


def _corpus_csv(rows: int) -> str:
    return "batch,respondent_id,text\n" + "\n".join(
        f"B1,R{i:03d},第{i}段访谈正文内容用于稳定复现上传交错" for i in range(1, rows + 1)
    )


def _wait_job(instance: Portal, job_id: int) -> dict:
    return wait_for(instance, job_id)


def _export_for_job(instance: Portal, job_id: int) -> dict:
    return next(e for e in instance.exports() if e["job_id"] == job_id)


def test_upload_during_generation_keeps_submitted_scope_and_progress(portal):
    """The 21 -> 22 interleaving, reproduced deterministically.

    A 21-row job streams its first 20-row chunk and parks; a supplementary
    upload for the same batch lands while generation is in flight. The fixed
    submission-time scope must keep the job, its progress and the exported file
    at 21 rows; the new row only enters the next generation.
    """
    upload(portal, _corpus_csv(21))
    job_id = portal.create_job("B1")
    assert next(j for j in portal.jobs() if j["id"] == job_id)["total_rows"] == 21

    barrier = _ChunkBarrier()
    original_abort = portal._abort_reason
    portal._abort_reason = lambda *a, **k: (barrier(), original_abort(*a, **k))[1]
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = next(j for j in portal.jobs() if j["id"] == job_id)
            if job["status"] == "running":
                break
            time.sleep(0.02)
        barrier.wait_reached()

        # Supplementary interview for the same batch, uploaded mid-generation.
        upload(portal, "batch,respondent_id,text\nB1,R999,提交任务之后才补充上传的访谈\n")
        assert portal.batches() and portal.stats()["utterances"] == 22
        barrier.release()

        job = _wait_job(portal, job_id)
    finally:
        portal._abort_reason = original_abort

    assert job["status"] == "completed"
    assert job["total_rows"] == 21
    # Progress never overshoots the fixed scope, at any sampled moment.
    assert job["written_rows"] == 21

    export = _export_for_job(portal, job_id)
    handle, _name, error = portal.open_download(export["id"])
    assert error is None and handle is not None
    content = handle.read().decode("utf-8")
    handle.close()
    # Header plus exactly the 21 in-scope rows; the late row is not exported.
    body_lines = [line for line in content.splitlines() if line]
    assert len(body_lines) == 22
    assert "提交任务之后才补充上传的访谈" not in content
    assert export["row_count"] == 21

    # The supplementary row enters the next generation of the same batch.
    next_job = portal.create_job("B1")
    job2 = _wait_job(portal, next_job)
    assert job2["total_rows"] == 22 and job2["written_rows"] == 22
    handle, _name, error = portal.open_download(_export_for_job(portal, next_job)["id"])
    assert error is None and handle is not None
    content2 = handle.read().decode("utf-8")
    handle.close()
    assert "提交任务之后才补充上传的访谈" in content2
    assert len([line for line in content2.splitlines() if line]) == 23


def test_queued_job_scope_survives_restart_and_excludes_later_upload(tmp_path):
    """The fixed scope is persisted: a queued job keeps it across a restart."""
    data_dir = tmp_path / "data"
    first = Portal(data_dir)  # no workers: the job stays queued
    try:
        upload(first, _corpus_csv(21))
        job_id = first.create_job("B1")
        upload(first, "batch,respondent_id,text\nB1,R999,排队期间补充上传的访谈\n")
        queued = next(j for j in first.jobs() if j["id"] == job_id)
        assert queued["status"] == "queued" and queued["total_rows"] == 21
    finally:
        first.shutdown()

    second = Portal(data_dir)
    second.start_workers()
    try:
        job = _wait_job(second, job_id)
        assert job["status"] == "completed"
        assert job["total_rows"] == 21 and job["written_rows"] == 21
        export = _export_for_job(second, job_id)
        assert export["row_count"] == 21
        handle, _name, error = second.open_download(export["id"])
        assert error is None and handle is not None
        content = handle.read().decode("utf-8")
        handle.close()
        assert "排队期间补充上传的访谈" not in content

        next_job = second.create_job("B1")
        job2 = _wait_job(second, next_job)
        assert job2["total_rows"] == 22
    finally:
        second.shutdown()


def test_revocation_while_queued_blocks_job_through_persisted_scope(tmp_path):
    """Revocation is not delayed by the fixed scope: a queued job is blocked.

    The respondent's utterance rows are deleted by the revocation, but the
    job's submission-time snapshot still names them, so the block works even
    though generation has not started and survives a restart in between.
    """
    data_dir = tmp_path / "data"
    first = Portal(data_dir)  # no workers: the job stays queued
    try:
        upload(first, _corpus_csv(21))
        job_id = first.create_job("B1")
        result = first.revoke("R005")
        assert result["deleted_rows"] == 1
        job = next(j for j in first.jobs() if j["id"] == job_id)
        assert job["status"] == "blocked"
        assert first.exports() == []
    finally:
        first.shutdown()

    second = Portal(data_dir)
    second.start_workers()
    try:
        time.sleep(0.3)
        job = next(j for j in second.jobs() if j["id"] == job_id)
        assert job["status"] == "blocked"
        assert second.exports() == []
    finally:
        second.shutdown()


def test_revocation_of_late_uploaded_respondent_does_not_block_earlier_job(portal):
    """Scope semantics: rows uploaded after submission belong to the next run."""
    upload(portal, _corpus_csv(21))
    job_id = portal.create_job("B1")
    # R999 is never part of job_id's fixed scope.
    upload(portal, "batch,respondent_id,text\nB1,R999,后来才上传的受访者语料\n")
    portal.revoke("R999")

    job = _wait_job(portal, job_id)
    assert job["status"] == "completed"
    export = _export_for_job(portal, job_id)
    handle, _name, error = portal.open_download(export["id"])
    assert error is None and handle is not None
    handle.close()


def test_web_progress_matches_export_rows_with_interleaved_upload(client):
    """End-to-end through the browser API: status progress equals export rows."""
    test_client, instance = client
    r = test_client.post(
        "/upload",
        files={"file": ("corpus.csv", _corpus_csv(21).encode("utf-8"), "text/csv")},
    )
    assert r.status_code == 200
    r = test_client.post("/generate", data={"batch": "B1"})
    assert r.status_code == 200
    job_id = instance.jobs()[0]["id"]

    barrier = _ChunkBarrier()
    original_abort = instance._abort_reason
    instance._abort_reason = lambda *a, **k: (barrier(), original_abort(*a, **k))[1]
    try:
        barrier.wait_reached()
        r = test_client.post(
            "/upload",
            files={
                "file": (
                    "supplement.csv",
                    "batch,respondent_id,text\nB1,R999,网页流程中交错上传的补充访谈\n".encode(
                        "utf-8"
                    ),
                    "text/csv",
                )
            },
        )
        assert r.status_code == 200
        barrier.release()
        job = _wait_job(instance, job_id)
    finally:
        instance._abort_reason = original_abort

    status = test_client.get("/api/status").json()
    api_job = next(j for j in status["jobs"] if j["id"] == job_id)
    api_export = next(e for e in status["exports"] if e["job_id"] == job_id)
    assert api_job["total_rows"] == 21
    assert api_job["written_rows"] == api_job["total_rows"]
    assert api_export["row_count"] == api_job["written_rows"]

    r = test_client.get(f"/download/{api_export['id']}")
    assert r.status_code == 200
    assert len([line for line in r.text.splitlines() if line]) == api_export["row_count"] + 1
    assert "网页流程中交错上传的补充访谈" not in r.text
