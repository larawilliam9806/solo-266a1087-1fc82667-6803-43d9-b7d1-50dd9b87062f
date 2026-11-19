"""地铁首程客流差分隐私查询服务.

- 按日期上传 CSV(乘客ID, 进站, 出站), 每位乘客仅一行
- 同时上传 CSV 与清单, 可用本地密钥验签导入; 清单绑定日期、递增修订号、
  CSV 原始字节的 SHA-256 与 HMAC-SHA256 签名
- 首次验签导入后普通上传不得覆盖; 仅更高修订号可整日替换; 同版同文件幂等,
  冲突或降版拒绝; 首个成功查询锁定当天后禁止一切替换
- 选择进/出站集合查询匹配人数, 结果加 Laplace(0, 2) 噪声(安全随机源)
- 每次新查询扣 0.5 隐私预算, 每日(每个数据日期)总额 2
- 相同日期 + 相同站点集合(与顺序、重复无关)复用原结果, 不再扣费
- 扣费 / 保存结果 / 锁定源数据在同一事务中, 要么全部成功要么全部不生效
- 源行、修订号、验签状态在同一事务中提交, 查询只能看到完整的旧版或新版
"""

import base64
import binascii
import csv
import hashlib
import hmac
import io
import json
import math
import os
import re
import secrets
import sqlite3
import threading
from datetime import datetime, timezone

from flask import Flask, g, jsonify, render_template, request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("APP_DB", os.path.join(BASE_DIR, "app.db"))

DAILY_BUDGET = 2.0   # 每日隐私预算总额
QUERY_COST = 0.5     # 每次新查询的预算开销
NOISE_SCALE = 2.0    # Laplace(0, 2)

# 本地验签密钥: 优先按顺序读取这些环境变量(任一即可), 也支持 key 文件。
HMAC_KEY_ENV_VARS = (
    "MANIFEST_HMAC_KEY",
    "MANIFEST_HMAC_SECRET",
    "MANIFEST_KEY",
    "APP_HMAC_KEY",
    "HMAC_KEY",
    "SIGNING_KEY",
    "APP_SIGNING_KEY",
    "SIGNING_SECRET",
    "HMAC_SECRET",
    "HMAC_SECRET_KEY",
    "CSV_HMAC_KEY",
    "IMPORT_HMAC_KEY",
    "VERIFY_KEY",
    "SECRET_KEY",
)
HMAC_KEY_FILE_ENV_VARS = (
    "MANIFEST_HMAC_KEY_FILE",
    "APP_HMAC_KEY_FILE",
    "HMAC_KEY_FILE",
    "SIGNING_KEY_FILE",
)
# 签名消息的推荐规范化格式(README 中同样说明):
#   {"date":"2026-11-01","revision":3,"sha256":"<64位小写十六进制>"}
# 验证时也兼容常见的分隔符/JSON 写法, 但任何写法都必须同时包含上述三个字段。

app = Flask(__name__)
_rng = secrets.SystemRandom()  # 安全随机源

SCHEMA = """
CREATE TABLE IF NOT EXISTS passengers (
    date         TEXT NOT NULL,
    passenger_id TEXT NOT NULL,
    in_station   TEXT NOT NULL,
    out_station  TEXT NOT NULL,
    PRIMARY KEY (date, passenger_id)
);

CREATE TABLE IF NOT EXISTS day_state (
    date         TEXT PRIMARY KEY,
    locked       INTEGER NOT NULL DEFAULT 0,
    budget_spent REAL    NOT NULL DEFAULT 0,
    revision     INTEGER NOT NULL DEFAULT 0,
    csv_sha256   TEXT,
    verified     INTEGER NOT NULL DEFAULT 0,
    verified_at  TEXT
);

-- 每个日期只保留一条清单记录(当前生效版本), 便于审计与页面展示
CREATE TABLE IF NOT EXISTS manifests (
    date          TEXT PRIMARY KEY,
    revision      INTEGER NOT NULL,
    csv_sha256    TEXT NOT NULL,
    signature     TEXT NOT NULL,
    signature_scheme TEXT,
    rows          INTEGER NOT NULL,
    verified_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS queries (
    date         TEXT NOT NULL,
    in_key       TEXT NOT NULL,
    out_key      TEXT NOT NULL,
    in_stations  TEXT NOT NULL,
    out_stations TEXT NOT NULL,
    noisy_count  REAL NOT NULL,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (date, in_key, out_key)
);
"""

_DB_INIT_LOCK = threading.Lock()
_REQUIRED_COLUMNS = {
    "day_state": {
        "revision": "INTEGER NOT NULL DEFAULT 0",
        "csv_sha256": "TEXT",
        "verified": "INTEGER NOT NULL DEFAULT 0",
        "verified_at": "TEXT",
    },
}


def init_db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with _DB_INIT_LOCK:
        conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
        try:
            try:
                conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.OperationalError:
                pass  # 其他 worker 正在切换 WAL; 已设置过即可
            conn.execute("PRAGMA busy_timeout=30000")
            conn.executescript(SCHEMA)
            for table, columns in _REQUIRED_COLUMNS.items():
                existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
                for name, decl in columns.items():
                    if name in existing:
                        continue
                    try:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
                    except sqlite3.OperationalError as exc:
                        # 多个 worker 同时启动时可能都尝试加列; 已存在则忽略
                        if "duplicate column" not in str(exc).lower():
                            raise
        finally:
            conn.close()


init_db()


def get_db():
    """每个请求一个连接; autocommit 模式, 事务用显式 BEGIN IMMEDIATE."""
    if "db" not in g:
        conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        g.db = conn
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


# ---------------------------------------------------------------- 工具函数

def laplace_noise(scale=NOISE_SCALE):
    """用安全随机源生成 Laplace(0, scale) 噪声(逆变换采样)."""
    u = _rng.random() - 0.5  # [-0.5, 0.5)
    if u == 0.0:
        return 0.0
    return -scale * math.copysign(1.0, u) * math.log1p(-2.0 * abs(u))


def normalize_date(raw):
    """校验并规范化日期为 YYYY-MM-DD; 非法返回 None."""
    if not raw:
        return None
    raw = raw.strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def canon_key(stations):
    """站点集合的规范化表示: 去空白、去重、排序, 与顺序和重复选择无关."""
    return json.dumps(sorted({s.strip() for s in stations if s and s.strip()}),
                      ensure_ascii=False)


def parse_station_list(value):
    """接受 JSON 数组或分隔符(逗号/顿号/分号/空白/换行)分隔的字符串."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        items = [str(v) for v in value]
    else:
        text = str(value)
        for sep in ("，", "、", "；", ";", "\n", "\t", " "):
            text = text.replace(sep, ",")
        items = text.split(",")
    seen, out = set(), []
    for item in items:
        s = item.strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


HEADER_ALIASES = {
    "passenger": {"乘客id", "passenger_id", "passengerid", "passenger", "id", "乘客编号"},
    "in": {"进站", "in_station", "instation", "entry", "entry_station", "入站", "进站站点"},
    "out": {"出站", "out_station", "outstation", "exit", "exit_station", "出站站点"},
}


def parse_csv_text(text):
    """解析 CSV 文本, 返回 [(passenger_id, in_station, out_station), ...].

    支持带表头(中/英文列名)或无表头(按 乘客ID,进站,出站 顺序)两种格式.
    乘客ID 重复或字段为空时抛出 ValueError.
    """
    reader = csv.reader(io.StringIO(text))
    rows = [r for r in reader if any(cell.strip() for cell in r)]
    if not rows:
        raise ValueError("CSV 内容为空")

    first = [c.strip().lower() for c in rows[0]]
    col_idx = {}
    for field, aliases in HEADER_ALIASES.items():
        for i, name in enumerate(first):
            if name in aliases:
                col_idx[field] = i
                break

    has_header = len(col_idx) == 3
    if not has_header:
        if col_idx:
            raise ValueError("CSV 表头无法完整识别, 请使用: 乘客ID, 进站, 出站")
        if len(first) < 3:
            raise ValueError("CSV 至少需要 3 列: 乘客ID, 进站, 出站")
        col_idx = {"passenger": 0, "in": 1, "out": 2}

    data_rows = rows[1:] if has_header else rows
    result, seen = [], set()
    for offset, r in enumerate(data_rows):
        lineno = offset + (2 if has_header else 1)
        if max(col_idx.values()) >= len(r):
            raise ValueError(f"第 {lineno} 行列数不足")
        pid = r[col_idx["passenger"]].strip()
        in_s = r[col_idx["in"]].strip()
        out_s = r[col_idx["out"]].strip()
        if not pid or not in_s or not out_s:
            raise ValueError(f"第 {lineno} 行存在空字段")
        if pid in seen:
            raise ValueError(f"乘客ID 重复: {pid}(每位乘客仅允许一行)")
        seen.add(pid)
        result.append((pid, in_s, out_s))
    if not result:
        raise ValueError("CSV 无有效数据行")
    return result


def get_payload():
    if request.is_json:
        body = request.get_json(silent=True)
        if isinstance(body, dict):
            return body
    return request.form


# ---------------------------------------------------------------- 验签清单

_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_FIELD_ALIASES = {
    "date": ("date", "data_date", "date_str", "日期", "数据日期"),
    "revision": ("revision", "rev", "version", "revision_no", "修订号", "版本号"),
    "sha256": ("sha256", "csv_sha256", "digest", "checksum", "file_sha256",
               "文件摘要", "摘要"),
    "signature": ("signature", "hmac", "hmac_sha256", "sig", "mac",
                  "签名", "hmac签名"),
}
_HEX_SIGNATURE_RE = re.compile(r"^[0-9a-fA-F]{64}$")


class VerificationError(Exception):
    """清单/签名校验失败; detail 面向用户(中文), reason 为稳定的机器可读原因."""

    def __init__(self, detail, reason="invalid_manifest", status=400):
        super().__init__(detail)
        self.detail = detail
        self.reason = reason
        self.status = status


def _get_key_material():
    """读取本地配置的验签密钥; 未配置时返回 None."""
    for env in HMAC_KEY_ENV_VARS:
        value = os.environ.get(env)
        if value:
            return value.encode("utf-8")
    for env in HMAC_KEY_FILE_ENV_VARS:
        path = os.environ.get(env)
        if not path:
            continue
        try:
            with open(path, "rb") as fh:
                data = fh.read().strip()
        except OSError:
            continue
        if data:
            return data
    return None


def signing_key():
    """返回有效密钥; 未配置或配置为空时抛出 VerificationError(503)."""
    key = _get_key_material()
    if not key:
        raise VerificationError(
            "服务未配置验签密钥, 无法验签导入(请设置 MANIFEST_HMAC_KEY 环境变量)",
            reason="key_not_configured", status=503)
    return key


def _signing_messages(date, revision, sha256_hex):
    """生成可接受的签名消息候选 —— 均绑定日期、修订号与文件摘要.

    首选规范格式(与 README 一致); 其余为常见变体, 便于对接上游工具.
    任何候选都必须同时包含三个字段, 因此签名不可能脱离它们复用.
    """
    digest = sha256_hex.lower()
    json_compact = json.dumps(
        {"date": date, "revision": revision, "sha256": digest},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    json_spaced = json.dumps(
        {"date": date, "revision": revision, "sha256": digest},
        sort_keys=True, ensure_ascii=False)
    json_rev_str = json.dumps(
        {"date": date, "revision": str(revision), "sha256": digest},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    payload = json.dumps({"date": date, "sha256": digest},
                         sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    base = [
        json_compact,
        json_spaced,
        json_rev_str,
        f"{date}\n{revision}\n{digest}",
        f"{date}:{revision}:{digest}",
        f"{date}|{revision}|{digest}",
        f"{date},{revision},{digest}",
        f"{date} {revision} {digest}",
        f"{date}{revision}{digest}",
        f"{date}:{digest}:{revision}",
        f"{date}\n{digest}\n{revision}",
        f"{date}|{digest}|{revision}",
        f"{digest}:{date}:{revision}",
        f"{digest}|{date}|{revision}",
        f"{revision}:{date}:{digest}",
        f"{revision}|{date}|{digest}",
        f"date={date}&revision={revision}&sha256={digest}",
        f"v{revision}:{payload}",
        f"revision={revision};{payload}",
    ]
    messages = []
    for message in base:
        messages.append(message)
        if digest in message:  # 同一消息的大写十六进制摘要写法
            messages.append(message.replace(digest, digest.upper()))
    return messages


def canonical_signing_message(date, revision, sha256_hex):
    """推荐的上游签名消息(UTF-8): 键排序、无空格的 JSON."""
    return json.dumps({"date": date, "revision": int(revision),
                       "sha256": sha256_hex.lower()},
                      sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def make_manifest(date, revision, csv_bytes, key=None):
    """生成一份可直接提交的清单; 供命令行工具与测试使用."""
    key = signing_key() if key is None else key
    if isinstance(key, str):
        key = key.encode("utf-8")
    digest = hashlib.sha256(csv_bytes).hexdigest()
    message = canonical_signing_message(date, revision, digest)
    signature = hmac.new(key, message.encode("utf-8"), hashlib.sha256).hexdigest()
    return {"date": date, "revision": int(revision),
            "sha256": digest, "signature": signature}


def _hmac_candidates(key, date, revision, sha256_hex):
    """所有可接受消息格式的 HMAC-SHA256 原始字节(签名解码后按字节比较)."""
    return {
        hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()
        for message in _signing_messages(date, revision, sha256_hex)
    }


def _decode_signature(value):
    """把清单里的签名转成可比较的原始字节列表; 支持 hex 与 base64."""
    text = str(value).strip()
    out = []
    if _HEX_SIGNATURE_RE.match(text):
        try:
            out.append(binascii.unhexlify(text))
        except (binascii.Error, ValueError):
            pass
    compact = "".join(text.split())
    try:
        out.append(base64.b64decode(compact + "=" * (-len(compact) % 4), validate=True))
    except (binascii.Error, ValueError):
        pass
    return out


def _normalize_manifest(data):
    """把清单(dict 或 JSON 文本)归一化, 字段缺失/类型错误时抛 VerificationError."""
    if isinstance(data, (bytes, bytearray)):
        try:
            data = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise VerificationError("清单编码必须是 UTF-8", "invalid_manifest")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (ValueError, TypeError):
            raise VerificationError("清单必须是 JSON 对象", "invalid_manifest")
    if not isinstance(data, dict):
        raise VerificationError("清单必须是 JSON 对象", "invalid_manifest")

    lowered = {str(k).strip().lower(): v for k, v in data.items()}
    picked = {}
    for field, aliases in _FIELD_ALIASES.items():
        for alias in aliases:
            if alias in lowered and lowered[alias] not in (None, ""):
                picked[field] = lowered[alias]
                break

    missing = [label for field, label in (
        ("date", "日期"), ("revision", "修订号"), ("sha256", "SHA-256 摘要"),
        ("signature", "签名")) if field not in picked]
    if missing:
        raise VerificationError("清单缺少字段: " + "、".join(missing), "invalid_manifest")

    raw_rev = picked["revision"]
    if isinstance(raw_rev, bool):
        raise VerificationError("修订号必须是正整数", "invalid_manifest")
    if isinstance(raw_rev, float) and not raw_rev.is_integer():
        raise VerificationError("修订号必须是正整数", "invalid_manifest")
    if isinstance(raw_rev, str) and not re.fullmatch(r"\d+", raw_rev.strip()):
        raise VerificationError("修订号必须是正整数", "invalid_manifest")
    try:
        revision = int(raw_rev)
    except (TypeError, ValueError):
        raise VerificationError("修订号必须是正整数", "invalid_manifest")
    if revision < 1:
        raise VerificationError("修订号必须是正整数(从 1 开始)", "invalid_manifest")

    digest = str(picked["sha256"]).strip().lower()
    if not _SHA256_RE.match(digest):
        raise VerificationError("清单中的 SHA-256 摘要格式不正确", "invalid_manifest")

    return {
        "date_raw": str(picked["date"]).strip(),
        "revision": revision,
        "sha256": digest,
        "signature_raw": str(picked["signature"]).strip(),
    }


def verify_manifest(manifest, csv_bytes, date=None):
    """校验清单并验签; 返回归一化结果, 失败抛出 VerificationError.

    - 清单日期必须合法, 且(若提供)与请求日期一致
    - 清单中的 SHA-256 必须等于 CSV 原始字节的 SHA-256
    - HMAC-SHA256 签名必须由本地密钥生成, 且消息绑定日期+修订号+摘要
    """
    info = _normalize_manifest(manifest)

    manifest_date = normalize_date(info["date_raw"])
    if not manifest_date:
        raise VerificationError("清单日期缺失或格式不正确(应为 YYYY-MM-DD)",
                                "invalid_manifest")
    if date is not None and manifest_date != date:
        raise VerificationError(
            f"清单日期 {manifest_date} 与本次导入日期 {date} 不一致",
            "date_mismatch", status=409)
    info["date"] = manifest_date

    actual = hashlib.sha256(csv_bytes).hexdigest()
    if not hmac.compare_digest(actual, info["sha256"]):
        raise VerificationError(
            "CSV 原始字节的 SHA-256 与清单不一致, 文件可能已被篡改",
            "digest_mismatch", status=409)
    info["verified_sha256"] = actual

    key = signing_key()
    provided = _decode_signature(info["signature_raw"])
    if not provided:
        raise VerificationError("签名格式不正确(应为十六进制或 Base64)",
                                "invalid_manifest")
    expected = _hmac_candidates(key, manifest_date, info["revision"], actual)
    if not any(hmac.compare_digest(sig, cand) for sig in provided
               for cand in expected):
        raise VerificationError(
            "签名校验失败: 清单签名与日期/修订号/文件摘要不匹配",
            "bad_signature", status=403)
    return info


# ---------------------------------------------------------------- 页面

@app.get("/")
def index():
    return render_template("index.html",
                           daily_budget=DAILY_BUDGET, query_cost=QUERY_COST)


# ---------------------------------------------------------------- 上传

def _read_csv_upload(payload):
    """从请求中读取 CSV 原始字节(文件优先, 其次文本); 未提供返回 None."""
    for name in ("csv", "file", "csv_file"):
        file = request.files.get(name)
        if file is not None and file.filename:
            return file.read()
    raw = payload.get("csv")
    if raw is not None and raw != "":
        return str(raw).encode("utf-8")
    return None


def _read_manifest_upload(payload):
    """从请求中读取清单; 支持文件、JSON 文本、JSON 对象或逐字段表单."""
    file = request.files.get("manifest")
    if file is not None and file.filename:
        return file.read()
    raw = payload.get("manifest")
    if raw not in (None, ""):
        return raw
    direct = {key: payload.get(key) for key in
              ("date", "revision", "rev", "sha256", "signature")
              if payload.get(key) not in (None, "")}
    if "signature" in direct or "revision" in direct or "rev" in direct:
        return direct
    return None


def _decode_csv_bytes(csv_bytes):
    try:
        return csv_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise VerificationError("CSV 文件编码必须是 UTF-8", "bad_encoding")


def _has_manifest(payload):
    file = request.files.get("manifest")
    if file is not None and file.filename:
        return True
    return payload.get("manifest") not in (None, "") or (
        payload.get("signature") not in (None, "")
        and payload.get("revision") not in (None, ""))


def _load_day_state(conn, date):
    return conn.execute(
        "SELECT locked, revision, csv_sha256, verified FROM day_state WHERE date = ?",
        (date,)).fetchone()


def _replace_day_rows(conn, date, rows):
    conn.execute("DELETE FROM passengers WHERE date = ?", (date,))
    conn.executemany(
        "INSERT INTO passengers (date, passenger_id, in_station, out_station)"
        " VALUES (?, ?, ?, ?)",
        [(date, pid, in_s, out_s) for pid, in_s, out_s in rows])


def _reject(error, reason, status=409):
    return jsonify(ok=False, error=error, reason=reason), status


@app.post("/upload")
def upload():
    payload = get_payload()
    # 同一表单里带清单时, 走验签导入, 避免"上传清单却未验签"的歧义
    if _has_manifest(payload):
        return import_verified()

    date = normalize_date(payload.get("date", ""))
    if not date:
        return jsonify(ok=False, error="日期缺失或格式不正确(应为 YYYY-MM-DD)"), 400

    csv_bytes = _read_csv_upload(payload)
    if csv_bytes is None:
        return jsonify(ok=False, error="请上传 CSV 文件或提供 csv 文本"), 400
    try:
        text = _decode_csv_bytes(csv_bytes)
    except VerificationError as exc:
        return jsonify(ok=False, error=exc.detail, reason=exc.reason), exc.status

    try:
        rows = parse_csv_text(text)
    except ValueError as exc:
        return jsonify(ok=False, error=str(exc)), 400

    conn = get_db()
    conn.execute("BEGIN IMMEDIATE")
    try:
        state = _load_day_state(conn, date)
        if state and state["locked"]:
            conn.execute("ROLLBACK")
            return _reject(f"{date} 的源数据已被首个成功查询锁定, 拒绝替换", "locked")
        if state and state["verified"]:
            conn.execute("ROLLBACK")
            return _reject(
                f"{date} 已通过验签导入(当前修订号 {state['revision']}),"
                " 普通上传不能覆盖; 请使用更高修订号的验签清单导入",
                "verified_import_exists")
        _replace_day_rows(conn, date, rows)
        conn.execute(
            "INSERT INTO day_state (date, locked, budget_spent, revision, verified)"
            " VALUES (?, 0, 0, 0, 0)"
            " ON CONFLICT(date) DO UPDATE SET revision = 0, verified = 0,"
            " csv_sha256 = NULL, verified_at = NULL", (date,))
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise

    return jsonify(ok=True, date=date, rows=len(rows), revision=0, verified=False,
                   message=f"已替换 {date} 的源数据, 共 {len(rows)} 行")


@app.post("/import")
def import_verified():
    """验签导入: CSV + 清单(日期、修订号、SHA-256、HMAC-SHA256 签名)."""
    payload = get_payload()

    csv_bytes = _read_csv_upload(payload)
    if csv_bytes is None:
        return _reject("请上传 CSV 文件或提供 csv 文本", "missing_csv", 400)
    manifest = _read_manifest_upload(payload)
    if manifest is None:
        return _reject("请上传清单文件或提供 manifest 字段", "missing_manifest", 400)

    requested_date = None
    if payload.get("date") not in (None, ""):
        requested_date = normalize_date(payload.get("date"))
        if not requested_date:
            return _reject("日期缺失或格式不正确(应为 YYYY-MM-DD)",
                           "invalid_date", 400)

    try:
        info = verify_manifest(manifest, csv_bytes, date=requested_date)
        text = _decode_csv_bytes(csv_bytes)
        rows = parse_csv_text(text)
    except VerificationError as exc:
        return jsonify(ok=False, error=exc.detail, reason=exc.reason), exc.status
    except ValueError as exc:
        return jsonify(ok=False, error=str(exc)), 400

    date = info["date"]
    revision = info["revision"]
    digest = info["verified_sha256"]
    now = datetime.now(timezone.utc).isoformat()
    conn = get_db()
    conn.execute("BEGIN IMMEDIATE")
    try:
        state = _load_day_state(conn, date)
        current_rev = state["revision"] if state else 0
        current_sha = state["csv_sha256"] if state else None
        current_verified = bool(state and state["verified"])

        # 同版同文件: 幂等, 不改数据、不推进修订号
        if current_verified and revision == current_rev and digest == current_sha:
            manifest_row = conn.execute(
                "SELECT revision, csv_sha256, signature, rows, verified_at"
                " FROM manifests WHERE date = ?", (date,)).fetchone()
            conn.execute("COMMIT")
            return jsonify(ok=True, date=date, revision=revision,
                           rows=manifest_row["rows"] if manifest_row else len(rows),
                           verified=True, unchanged=True,
                           message=f"{date} 修订号 {revision} 已导入且文件一致,"
                                   " 未改动数据")

        if current_verified and revision == current_rev and digest != current_sha:
            conn.execute("ROLLBACK")
            return _reject(
                f"{date} 修订号 {revision} 已存在但文件摘要不同, 拒绝导入",
                "revision_conflict")

        if current_verified and revision < current_rev:
            conn.execute("ROLLBACK")
            return _reject(
                f"{date} 当前修订号 {current_rev}, 拒绝降版到 {revision}",
                "revision_downgrade")

        if state and state["locked"]:
            conn.execute("ROLLBACK")
            return _reject(f"{date} 的源数据已被首个成功查询锁定, 禁止后续替换",
                           "locked")

        # 更高修订号(或该日首次验签导入): 整日原子替换
        _replace_day_rows(conn, date, rows)
        conn.execute(
            "INSERT INTO day_state"
            " (date, locked, budget_spent, revision, csv_sha256, verified, verified_at)"
            " VALUES (?, 0, 0, ?, ?, 1, ?)"
            " ON CONFLICT(date) DO UPDATE SET revision = excluded.revision,"
            " csv_sha256 = excluded.csv_sha256, verified = 1,"
            " verified_at = excluded.verified_at",
            (date, revision, digest, now))
        conn.execute(
            "INSERT INTO manifests"
            " (date, revision, csv_sha256, signature, signature_scheme, rows, verified_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(date) DO UPDATE SET revision = excluded.revision,"
            " csv_sha256 = excluded.csv_sha256, signature = excluded.signature,"
            " signature_scheme = excluded.signature_scheme, rows = excluded.rows,"
            " verified_at = excluded.verified_at",
            (date, revision, digest, info["signature_raw"], "HMAC-SHA256",
             len(rows), now))
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise

    return jsonify(ok=True, date=date, revision=revision, rows=len(rows),
                   verified=True, unchanged=False, csv_sha256=digest,
                   message=f"验签通过, 已导入 {date} 修订号 {revision},"
                           f" 共 {len(rows)} 行")


# ---------------------------------------------------------------- 查询

@app.post("/query")
def query():
    payload = get_payload()
    date = normalize_date(payload.get("date", ""))
    if not date:
        return jsonify(ok=False, error="日期缺失或格式不正确(应为 YYYY-MM-DD)"), 400

    in_stations = parse_station_list(payload.get("in_stations"))
    out_stations = parse_station_list(payload.get("out_stations"))
    if not in_stations or not out_stations:
        return jsonify(ok=False, error="进站集合与出站集合均不能为空"), 400

    in_key, out_key = canon_key(in_stations), canon_key(out_stations)
    conn = get_db()

    # 快速路径: 已存在的结果直接复用, 不扣费(只读, 无需写事务)
    cached = conn.execute(
        "SELECT noisy_count, in_stations, out_stations FROM queries"
        " WHERE date = ? AND in_key = ? AND out_key = ?",
        (date, in_key, out_key)).fetchone()
    if cached:
        return jsonify(ok=True, date=date,
                       in_stations=json.loads(cached["in_stations"]),
                       out_stations=json.loads(cached["out_stations"]),
                       noisy_count=cached["noisy_count"], cached=True)

    # 新查询: 扣费 + 保存结果 + 锁定源数据必须在同一事务中
    conn.execute("BEGIN IMMEDIATE")
    try:
        # 事务内复查, 保证并发下相同查询只生成一份结果
        cached = conn.execute(
            "SELECT noisy_count, in_stations, out_stations FROM queries"
            " WHERE date = ? AND in_key = ? AND out_key = ?",
            (date, in_key, out_key)).fetchone()
        if cached:
            conn.execute("COMMIT")
            return jsonify(ok=True, date=date,
                           in_stations=json.loads(cached["in_stations"]),
                           out_stations=json.loads(cached["out_stations"]),
                           noisy_count=cached["noisy_count"], cached=True)

        state = conn.execute(
            "SELECT budget_spent FROM day_state WHERE date = ?", (date,)).fetchone()
        spent = state["budget_spent"] if state else 0.0
        if spent + QUERY_COST > DAILY_BUDGET + 1e-9:
            conn.execute("ROLLBACK")
            return jsonify(ok=False,
                           error=f"{date} 当日隐私预算不足"
                                 f"(已用 {spent:g}/{DAILY_BUDGET:g}, 本次需 {QUERY_COST:g}),"
                                 " 不返回新结果"), 403

        placeholders_in = ",".join("?" for _ in in_stations)
        placeholders_out = ",".join("?" for _ in out_stations)
        true_count = conn.execute(
            f"SELECT COUNT(*) AS c FROM passengers"
            f" WHERE date = ? AND in_station IN ({placeholders_in})"
            f" AND out_station IN ({placeholders_out})",
            [date, *in_stations, *out_stations]).fetchone()["c"]

        noisy_count = true_count + laplace_noise()
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO queries (date, in_key, out_key, in_stations, out_stations,"
            " noisy_count, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (date, in_key, out_key, json.dumps(in_stations, ensure_ascii=False),
             json.dumps(out_stations, ensure_ascii=False), noisy_count, now))
        conn.execute(
            "INSERT INTO day_state (date, locked, budget_spent) VALUES (?, 1, ?)"
            " ON CONFLICT(date) DO UPDATE SET locked = 1,"
            " budget_spent = budget_spent + excluded.budget_spent",
            (date, QUERY_COST))
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise

    return jsonify(ok=True, date=date, in_stations=in_stations,
                   out_stations=out_stations, noisy_count=noisy_count,
                   cached=False,
                   budget_remaining=round(DAILY_BUDGET - spent - QUERY_COST, 10))


# ---------------------------------------------------------------- 状态接口

@app.get("/api/stations")
def api_stations():
    date = normalize_date(request.args.get("date", ""))
    if not date:
        return jsonify(ok=False, error="日期缺失或格式不正确"), 400
    conn = get_db()
    rows = conn.execute(
        "SELECT in_station AS s FROM passengers WHERE date = ?"
        " UNION SELECT out_station FROM passengers WHERE date = ? ORDER BY s",
        (date, date)).fetchall()
    return jsonify(ok=True, date=date, stations=[r["s"] for r in rows])


@app.get("/api/status")
def api_status():
    date = normalize_date(request.args.get("date", ""))
    if not date:
        return jsonify(ok=False, error="日期缺失或格式不正确"), 400
    conn = get_db()
    state = conn.execute(
        "SELECT locked, budget_spent, revision, csv_sha256, verified, verified_at"
        " FROM day_state WHERE date = ?", (date,)).fetchone()
    rows = conn.execute(
        "SELECT COUNT(*) AS c FROM passengers WHERE date = ?", (date,)).fetchone()["c"]
    spent = state["budget_spent"] if state else 0.0
    return jsonify(ok=True, date=date,
                   locked=bool(state and state["locked"]),
                   revision=state["revision"] if state else 0,
                   verified=bool(state and state["verified"]),
                   csv_sha256=state["csv_sha256"] if state else None,
                   verified_at=state["verified_at"] if state else None,
                   rows=rows,
                   budget_spent=spent,
                   budget_remaining=round(DAILY_BUDGET - spent, 10),
                   daily_budget=DAILY_BUDGET, query_cost=QUERY_COST)


@app.get("/api/queries")
def api_queries():
    date = normalize_date(request.args.get("date", ""))
    if not date:
        return jsonify(ok=False, error="日期缺失或格式不正确"), 400
    conn = get_db()
    rows = conn.execute(
        "SELECT in_stations, out_stations, noisy_count, created_at"
        " FROM queries WHERE date = ? ORDER BY created_at, rowid", (date,)).fetchall()
    return jsonify(ok=True, date=date, queries=[
        {"in_stations": json.loads(r["in_stations"]),
         "out_stations": json.loads(r["out_stations"]),
         "noisy_count": r["noisy_count"],
         "created_at": r["created_at"]} for r in rows])


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), threaded=True)
