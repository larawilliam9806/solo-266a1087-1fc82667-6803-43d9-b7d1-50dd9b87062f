import os
import sqlite3
import tempfile
import threading

import pytest

_tmpdir = tempfile.mkdtemp(prefix="metrodp-")
os.environ["APP_DB"] = os.path.join(_tmpdir, "test.db")

import app as app_module  # noqa: E402

CSV_V1 = "乘客ID,进站,出站\np1,A,X\np2,A,X\np3,B,X\np4,A,Y\np5,C,Z\n"
CSV_V2 = "乘客ID,进站,出站\np1,A,X\np2,B,Y\n"


@pytest.fixture(autouse=True)
def clean_db():
    conn = sqlite3.connect(os.environ["APP_DB"])
    conn.executescript("DELETE FROM queries; DELETE FROM day_state;"
                       " DELETE FROM passengers; DELETE FROM manifests;")
    conn.close()
    yield


@pytest.fixture
def client():
    return app_module.app.test_client()


def upload(client, date, csv_text):
    return client.post("/upload", data={"date": date, "csv": csv_text})


def query(client, date, ins, outs):
    return client.post("/query", json={"date": date, "in_stations": ins,
                                       "out_stations": outs})


def status(client, date):
    return client.get(f"/api/status?date={date}").get_json()


# ------------------------------------------------------------- 上传与锁定

def test_upload_ok_and_replace_before_lock(client):
    r = upload(client, "2026-11-01", CSV_V1)
    assert r.status_code == 200 and r.get_json()["rows"] == 5
    # 锁定前可整日替换
    r = upload(client, "2026-11-01", CSV_V2)
    assert r.status_code == 200 and r.get_json()["rows"] == 2
    st = status(client, "2026-11-01")
    assert st["rows"] == 2 and not st["locked"]


def test_upload_rejected_after_lock(client):
    upload(client, "2026-11-02", CSV_V1)
    r = query(client, "2026-11-02", ["A"], ["X"])
    assert r.status_code == 200
    r = upload(client, "2026-11-02", CSV_V2)
    assert r.status_code == 409
    assert "锁定" in r.get_json()["error"]
    # 源数据未被改动
    assert status(client, "2026-11-02")["rows"] == 5


def test_duplicate_passenger_rejected(client):
    r = upload(client, "2026-11-03", "乘客ID,进站,出站\np1,A,X\np1,B,Y\n")
    assert r.status_code == 400
    assert "重复" in r.get_json()["error"]


def test_csv_without_header_and_english_header(client):
    r = upload(client, "2026-11-04", "p1,A,X\np2,B,Y\n")
    assert r.status_code == 200 and r.get_json()["rows"] == 2
    r = upload(client, "2026-11-05", "passenger_id,in_station,out_station\np1,A,X\n")
    assert r.status_code == 200 and r.get_json()["rows"] == 1


# ------------------------------------------------------------- 查询与缓存

def test_query_returns_noisy_only(client):
    upload(client, "2026-11-06", CSV_V1)
    r = query(client, "2026-11-06", ["A"], ["X"])
    body = r.get_json()
    assert r.status_code == 200 and body["ok"]
    assert isinstance(body["noisy_count"], float)
    assert "true_count" not in body and "count" not in body
    assert status(client, "2026-11-06")["budget_spent"] == 0.5
    assert status(client, "2026-11-06")["locked"]


def test_same_query_reused_regardless_of_order_and_duplicates(client):
    upload(client, "2026-11-07", CSV_V1)
    r1 = query(client, "2026-11-07", ["A", "B"], ["X", "Y"]).get_json()
    r2 = query(client, "2026-11-07", ["B", "A", "A"], ["Y", "X", "X"]).get_json()
    assert r1["noisy_count"] == r2["noisy_count"]
    assert r2["cached"] is True
    # 第二次未扣费
    assert status(client, "2026-11-07")["budget_spent"] == 0.5


def test_budget_limit_four_queries(client):
    upload(client, "2026-11-08", CSV_V1)
    ok, rejected = 0, 0
    for i in range(6):
        r = query(client, "2026-11-08", [f"S{i}"], ["X"])
        if r.status_code == 200:
            ok += 1
        else:
            rejected += 1
            assert r.status_code == 403
            assert "预算不足" in r.get_json()["error"]
    assert ok == 4 and rejected == 2  # 2 / 0.5 = 4
    assert status(client, "2026-11-08")["budget_spent"] == 2.0
    # 预算用完后, 已缓存的查询仍然返回原值
    r = query(client, "2026-11-08", ["S0"], ["X"])
    assert r.status_code == 200 and r.get_json()["cached"] is True


def test_budget_is_per_date(client):
    upload(client, "2026-11-09", CSV_V1)
    upload(client, "2026-11-10", CSV_V1)
    for _ in range(4):
        assert query(client, "2026-11-09", [f"S{_}"], ["X"]).status_code == 200
    # 另一个日期仍有完整预算
    assert query(client, "2026-11-10", ["S0"], ["X"]).status_code == 200


# ------------------------------------------------------------- 并发

def test_concurrent_distinct_queries_never_overspend(client):
    upload(client, "2026-11-11", CSV_V1)
    results = []

    def worker(i):
        c = app_module.app.test_client()
        results.append(query(c, "2026-11-11", [f"T{i}"], ["X"]).status_code)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(200) == 4
    assert results.count(403) == 8
    assert status(client, "2026-11-11")["budget_spent"] == 2.0


def test_concurrent_identical_query_single_result(client):
    upload(client, "2026-11-12", CSV_V1)
    bodies = []

    def worker():
        c = app_module.app.test_client()
        bodies.append(query(c, "2026-11-12", ["A", "B"], ["X"]).get_json())

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(b["ok"] for b in bodies)
    assert len({b["noisy_count"] for b in bodies}) == 1  # 只有一份结果
    assert status(client, "2026-11-12")["budget_spent"] == 0.5  # 只扣一次
    conn = sqlite3.connect(os.environ["APP_DB"])
    n = conn.execute("SELECT COUNT(*) FROM queries WHERE date='2026-11-12'").fetchone()[0]
    conn.close()
    assert n == 1


# ------------------------------------------------------------- 持久化

def test_restart_keeps_results_and_budget(client):
    upload(client, "2026-11-13", CSV_V1)
    r1 = query(client, "2026-11-13", ["A"], ["X"]).get_json()
    # 模拟重启: 重新初始化数据库连接(文件不变)
    app_module.init_db()
    client2 = app_module.app.test_client()
    r2 = query(client2, "2026-11-13", ["A"], ["X"]).get_json()
    assert r2["cached"] is True and r2["noisy_count"] == r1["noisy_count"]
    assert status(client2, "2026-11-13")["budget_spent"] == 0.5
    # 重启后上传仍被拒绝
    assert upload(client2, "2026-11-13", CSV_V2).status_code == 409


# ------------------------------------------------------------- 噪声分布

def test_laplace_noise_distribution():
    samples = [app_module.laplace_noise() for _ in range(20000)]
    mean = sum(samples) / len(samples)
    var = sum((s - mean) ** 2 for s in samples) / len(samples)
    assert abs(mean) < 0.1            # 均值 ≈ 0
    assert abs(var - 8.0) < 0.8       # Laplace(0,2) 方差 = 2 * 2^2 = 8


# ------------------------------------------------------------- 验签导入

import hashlib
import hmac as hmac_mod
import json

KEY = "unit-test-signing-key"


def sign_manifest(date, revision, csv_text, key=KEY, digest=None):
    data = csv_text.encode("utf-8")
    sha = digest or hashlib.sha256(data).hexdigest()
    message = json.dumps({"date": date, "revision": revision, "sha256": sha},
                         sort_keys=True, separators=(",", ":"))
    sig = hmac_mod.new(key.encode(), message.encode(), hashlib.sha256).hexdigest()
    return {"date": date, "revision": revision, "sha256": sha, "signature": sig}


def do_import(client, date, csv_text, manifest, key=KEY):
    app_module.os.environ["MANIFEST_HMAC_KEY"] = key
    return client.post("/import", data={
        "date": date, "csv": csv_text, "manifest": json.dumps(manifest)})


def import_ok(client, date, csv_text, revision):
    manifest = sign_manifest(date, revision, csv_text)
    r = do_import(client, date, csv_text, manifest)
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def test_signed_import_and_digest_bound(client):
    body = import_ok(client, "2026-12-01", CSV_V1, 1)
    assert body["verified"] and body["revision"] == 1
    st = status(client, "2026-12-01")
    assert st["verified"] and st["revision"] == 1 and st["rows"] == 5
    # 篡改 CSV 后摘要不匹配
    bad = sign_manifest("2026-12-02", 1, CSV_V1)
    r = do_import(client, "2026-12-02", CSV_V2, bad)
    assert r.status_code == 409 and r.get_json()["reason"] == "digest_mismatch"
    assert status(client, "2026-12-02")["rows"] == 0


def test_signature_binds_date_revision_digest(client):
    csv_text = CSV_V2
    manifest = sign_manifest("2026-12-03", 1, csv_text)
    # 换日期
    r = do_import(client, "2026-12-04", csv_text, manifest)
    assert r.status_code == 409 and r.get_json()["reason"] == "date_mismatch"
    # 改修订号
    manifest2 = dict(manifest, revision=2)
    r = do_import(client, "2026-12-03", csv_text, manifest2)
    assert r.status_code == 403 and r.get_json()["reason"] == "bad_signature"
    # 错密钥
    r = do_import(client, "2026-12-03", csv_text, manifest, key="wrong")
    assert r.status_code == 403 and r.get_json()["reason"] == "bad_signature"
    assert status(client, "2026-12-03")["rows"] == 0


def test_missing_key_rejected(client):
    manifest = sign_manifest("2026-12-05", 1, CSV_V1)
    app_module.os.environ.pop("MANIFEST_HMAC_KEY", None)
    r = client.post("/import", data={"date": "2026-12-05", "csv": CSV_V1,
                                     "manifest": json.dumps(manifest)})
    assert r.status_code == 503 and r.get_json()["reason"] == "key_not_configured"


def test_plain_upload_cannot_overwrite_verified_day(client):
    import_ok(client, "2026-12-06", CSV_V1, 1)
    r = upload(client, "2026-12-06", CSV_V2)
    assert r.status_code == 409 and r.get_json()["reason"] == "verified_import_exists"
    assert status(client, "2026-12-06")["rows"] == 5


def test_higher_revision_replaces_whole_day(client):
    import_ok(client, "2026-12-07", CSV_V1, 1)
    import_ok(client, "2026-12-07", CSV_V2, 2)
    st = status(client, "2026-12-07")
    assert st["rows"] == 2 and st["revision"] == 2


def test_same_revision_same_file_is_idempotent(client):
    import_ok(client, "2026-12-08", CSV_V1, 3)
    written = sqlite3.connect(os.environ["APP_DB"]).execute(
        "SELECT COUNT(*) FROM passengers WHERE date='2026-12-08'").fetchone()[0]
    body = import_ok(client, "2026-12-08", CSV_V1, 3)
    assert body["unchanged"] is True
    assert status(client, "2026-12-08")["rows"] == written == 5
    assert status(client, "2026-12-08")["revision"] == 3


def test_same_revision_different_file_rejected(client):
    import_ok(client, "2026-12-09", CSV_V1, 2)
    r = do_import(client, "2026-12-09", CSV_V2, sign_manifest("2026-12-09", 2, CSV_V2))
    assert r.status_code == 409 and r.get_json()["reason"] == "revision_conflict"
    assert status(client, "2026-12-09")["rows"] == 5


def test_downgrade_revision_rejected(client):
    import_ok(client, "2026-12-10", CSV_V1, 4)
    r = do_import(client, "2026-12-10", CSV_V2, sign_manifest("2026-12-10", 3, CSV_V2))
    assert r.status_code == 409 and r.get_json()["reason"] == "revision_downgrade"
    assert status(client, "2026-12-10")["rows"] == 5


def test_query_lock_blocks_all_later_revisions(client):
    import_ok(client, "2026-12-11", CSV_V1, 1)
    assert query(client, "2026-12-11", ["A"], ["X"]).status_code == 200
    r = do_import(client, "2026-12-11", CSV_V2, sign_manifest("2026-12-11", 9, CSV_V2))
    assert r.status_code == 409 and r.get_json()["reason"] == "locked"
    assert status(client, "2026-12-11")["rows"] == 5
    assert status(client, "2026-12-11")["revision"] == 1


def test_new_revision_keeps_published_results_and_budget(client):
    import_ok(client, "2026-12-12", CSV_V1, 1)
    import_ok(client, "2026-12-12", CSV_V2, 2)   # 查询前可整日升级
    first = query(client, "2026-12-12", ["A"], ["X"]).get_json()
    # 查询后锁定: 更高修订号也被拒绝, 已发布结果与预算保持原状
    r = do_import(client, "2026-12-12", CSV_V1, sign_manifest("2026-12-12", 3, CSV_V1))
    assert r.status_code == 409 and r.get_json()["reason"] == "locked"
    again = query(client, "2026-12-12", ["A"], ["X"]).get_json()
    assert again["cached"] is True and again["noisy_count"] == first["noisy_count"]
    st = status(client, "2026-12-12")
    assert st["budget_spent"] == 0.5 and st["revision"] == 2 and st["rows"] == 2


def test_failed_import_leaves_revision_and_data_untouched(client):
    import_ok(client, "2026-12-13", CSV_V1, 1)
    # 合法签名但 CSV 解析失败 -> 不得推进修订号或改数据
    bad_csv = "乘客ID,进站,出站\np1,A,X\np1,B,Y\n"
    r = do_import(client, "2026-12-13", bad_csv,
                  sign_manifest("2026-12-13", 5, bad_csv))
    assert r.status_code == 400
    st = status(client, "2026-12-13")
    assert st["revision"] == 1 and st["rows"] == 5 and st["verified"]


def test_import_then_plain_upload_before_any_query(client):
    import_ok(client, "2026-12-14", CSV_V2, 1)
    r = upload(client, "2026-12-14", CSV_V1)
    assert r.status_code == 409
    assert status(client, "2026-12-14")["rows"] == 2


def test_concurrent_imports_same_revision_one_winner(client):
    csv_text = CSV_V1
    manifest = sign_manifest("2026-12-15", 1, csv_text)
    results = []

    def worker():
        c = app_module.app.test_client()
        results.append(do_import(c, "2026-12-15", csv_text, manifest).status_code)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(code == 200 for code in results)
    assert status(client, "2026-12-15")["rows"] == 5
    assert status(client, "2026-12-15")["revision"] == 1


def test_restart_keeps_verified_revision(client):
    import_ok(client, "2026-12-16", CSV_V1, 2)
    app_module.init_db()
    c2 = app_module.app.test_client()
    st = status(c2, "2026-12-16")
    assert st["verified"] and st["revision"] == 2
    r = do_import(c2, "2026-12-16", CSV_V2, sign_manifest("2026-12-16", 1, CSV_V2))
    assert r.status_code == 409 and r.get_json()["reason"] == "revision_downgrade"


def test_upload_with_manifest_routes_to_verified_import(client):
    manifest = sign_manifest("2026-12-17", 1, CSV_V1)
    app_module.os.environ["MANIFEST_HMAC_KEY"] = KEY
    r = client.post("/upload", data={"date": "2026-12-17", "csv": CSV_V1,
                                     "manifest": json.dumps(manifest)})
    assert r.status_code == 200 and r.get_json()["verified"] is True
    assert status(client, "2026-12-17")["revision"] == 1
    # 之后普通上传不能覆盖
    assert upload(client, "2026-12-17", CSV_V2).status_code == 409


def test_manifest_with_date_and_text_forms(client):
    # 清单以文件上传 + 日期由请求提供; 同时验证 canonical 消息兼容
    manifest = sign_manifest("2026-12-18", 2, CSV_V1)
    raw = json.dumps(manifest).encode()
    import io as _io
    app_module.os.environ["MANIFEST_HMAC_KEY"] = KEY
    r = client.post("/import", data={
        "date": "2026-12-18",
        "csv": (_io.BytesIO(CSV_V1.encode()), "d.csv"),
        "manifest": (_io.BytesIO(raw), "d.json"),
    }, content_type="multipart/form-data")
    assert r.status_code == 200, r.get_json()
    assert status(client, "2026-12-18")["verified"]


def test_replay_old_batch_after_downgrade_window(client):
    import_ok(client, "2026-12-19", CSV_V1, 1)
    import_ok(client, "2026-12-19", CSV_V2, 2)
    # 重放修订号 1 的旧批次: 直接拒绝(不覆盖新版)
    r = do_import(client, "2026-12-19", CSV_V1, sign_manifest("2026-12-19", 1, CSV_V1))
    assert r.status_code == 409 and r.get_json()["reason"] == "revision_downgrade"
    assert status(client, "2026-12-19")["rows"] == 2
    assert status(client, "2026-12-19")["revision"] == 2


def test_mid_transaction_failure_rolls_back_rows_and_revision(client, monkeypatch):
    import_ok(client, "2026-12-20", CSV_V1, 1)
    real = app_module._replace_day_rows
    calls = {"n": 0}

    def flaky(conn, date, rows):
        calls["n"] += 1
        conn.execute("DELETE FROM passengers WHERE date = ?", (date,))
        conn.execute("INSERT INTO passengers VALUES (?,?,?,?)",
                     (date, "ghost", "Q", "Q"))
        raise sqlite3.OperationalError("simulated crash")

    monkeypatch.setattr(app_module, "_replace_day_rows", flaky)
    r = do_import(client, "2026-12-20", CSV_V2, sign_manifest("2026-12-20", 2, CSV_V2))
    assert r.status_code == 500
    monkeypatch.setattr(app_module, "_replace_day_rows", real)
    st = status(client, "2026-12-20")
    assert st["revision"] == 1 and st["rows"] == 5
    assert query(client, "2026-12-20", ["A"], ["X"]).status_code == 200


def test_page_shows_revision_and_verification(client):
    import_ok(client, "2026-12-21", CSV_V1, 4)
    html = client.get("/").get_data(as_text=True)
    assert "验签导入" in html and "修订号" in html
    assert status(client, "2026-12-21")["revision"] == 4


def test_stations_read_sees_whole_version_during_imports(client):
    big_a = "乘客ID,进站,出站\n" + "".join(
        f"a{i},A{i % 7},X\n" for i in range(400))
    big_b = "乘客ID,进站,出站\n" + "".join(
        f"b{i},B{i % 7},Y\n" for i in range(400))
    set_a = {f"A{i}" for i in range(7)} | {"X"}
    set_b = {f"B{i}" for i in range(7)} | {"Y"}
    observed, stop = [], threading.Event()

    def reader():
        c = app_module.app.test_client()
        while not stop.is_set():
            data = c.get("/api/stations?date=2026-12-22").get_json()
            if data["ok"] and data["stations"]:
                observed.append(frozenset(data["stations"]))

    t = threading.Thread(target=reader)
    t.start()
    try:
        for rev in range(1, 6):
            text = big_a if rev % 2 else big_b
            import_ok(client, "2026-12-22", text, rev)
    finally:
        stop.set(); t.join()

    assert observed, "reader did not observe any snapshot"
    for snap in observed:
        assert snap in (frozenset(set_a), frozenset(set_b)), snap
    st = status(client, "2026-12-22")
    assert st["revision"] == 5 and st["rows"] == 400
