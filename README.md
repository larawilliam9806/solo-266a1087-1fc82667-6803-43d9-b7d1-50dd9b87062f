# 地铁每日首程客流 · 差分隐私查询平台

地铁数据分析员向合作研究方提供每日首程客流统计的网页服务。基于 **Flask + SQLite**,
对每次新查询的结果添加 **Laplace(0, 2)** 噪声(安全随机源 `secrets.SystemRandom`),
并按 **每次 0.5、每日总额 2** 的隐私预算计费。查询页只显示加噪结果。

上游按日期交付首程 CSV 时, 运营方可在浏览器中同时上传 CSV 与清单完成**验签导入**:
清单包含日期、递增修订号、CSV 原始字节的 SHA-256 与 HMAC-SHA256 签名。验签使用
本地配置的密钥, 签名绑定日期、修订号与文件摘要, 可识别篡改与旧批次重放。

## 快速启动

### Docker Compose(推荐)

```bash
docker compose up --build
```

访问 http://localhost:8000 。SQLite 数据库保存在名为 `appdata` 的卷中,
容器重启后已发布结果、预算消耗、修订号与锁定状态均不丢失。

### 本地运行

```bash
pip install -r requirements.txt
python app.py            # 开发模式, 监听 8000 端口
# 或: gunicorn --bind 0.0.0.0:8000 --workers 2 --threads 8 app:app
```

数据库路径由环境变量 `APP_DB` 指定(默认 `./app.db`, 容器内为 `/data/app.db`)。

### 验签密钥

验签导入使用本地配置的 HMAC 密钥(不写入数据库, 不随代码分发):

```bash
cp .env.example .env         # 填写 MANIFEST_HMAC_KEY, .env 已被 .gitignore 忽略
docker compose up --build

# 或使用密钥文件
MANIFEST_HMAC_KEY_FILE=/run/secrets/hmac_key ./...
```

支持的环境变量(按顺序取第一个非空值): `MANIFEST_HMAC_KEY`、`APP_HMAC_KEY`、
`HMAC_KEY`、`SIGNING_KEY`、`APP_SIGNING_KEY`、`HMAC_SECRET`、`VERIFY_KEY`;
密钥文件变量: `MANIFEST_HMAC_KEY_FILE`、`APP_HMAC_KEY_FILE`、`HMAC_KEY_FILE`。
未配置密钥时服务可正常查询, 但 `/import` 返回 `503 key_not_configured`。

## 功能与规则

| 规则 | 实现 |
| --- | --- |
| 按日期上传 CSV(乘客ID, 进站, 出站), 每位乘客仅一行 | `POST /upload`, 乘客ID 重复即拒绝; 支持中/英文表头或无表头 |
| CSV + 清单同时上传, 用本地密钥验签导入 | `POST /import`, 支持 multipart 文件或 JSON 字段 |
| 清单绑定日期、递增修订号、CSV 原始字节 SHA-256 与 HMAC-SHA256 | `verify_manifest`: 日期须一致, 摘要按原始字节计算, 签名消息包含三者 |
| 某日首次验签导入后, 普通上传不能覆盖 | `day_state.verified`, `/upload` 返回 `409 verified_import_exists` |
| 仅更高修订号的**有效**文件可整日替换 | 验签 + CSV 校验通过后才在写事务内比较修订号 |
| 同版同文件重传不改数据; 冲突或降版拒绝 | `409 revision_conflict` / `409 revision_downgrade`; 相同则 `unchanged=true` |
| 首个成功查询锁定该日, 禁止后续替换 | `day_state.locked`, `/import` 返回 `409 locked` |
| 源行、修订号、验签状态一起生效 | 三者同一 SQLite 写事务提交, 查询只能看到完整的旧版或新版 |
| 失败/重启不留"修订号已推进但数据未更新" | 先校验后写事务; 事务失败整体回滚; 状态持久化于 SQLite |
| 已发布带噪结果与预算不受替换影响 | `queries` / `day_state.budget_spent` 仅在查询时变化 |
| 锁定前可整日替换(未验签的普通上传) | `day_state.locked`, 替换与锁定检查在同一事务内 |
| 选择进/出站集合查询匹配人数 | `POST /query`, 集合与顺序、重复选择无关(排序去重后规范化) |
| 新查询加 Laplace(0, 2) 噪声, 扣 0.5 预算, 每日总额 2 | 逆变换采样 + `day_state.budget_spent` |
| 相同查询复用原结果, 不再扣费 | `queries` 表主键 `(date, in_key, out_key)`, 命中即返回 |
| 并发不超支、相同查询只生成一份结果 | `BEGIN IMMEDIATE` 写事务串行化 + 事务内复查 + 主键约束 |
| 预算不足不返回新结果 | 事务内检查, 不足即回滚并返回 403 |
| 扣费/保存结果/锁定源数据同生共死 | 三者在**同一 SQLite 事务**中提交或回滚 |
| 响应丢失、重启后重查仍返回原值 | 先提交事务再响应; 结果持久化于 SQLite |

### 清单格式与签名

清单是 JSON 对象(上传为文件或直接作为 `manifest` 字段):

```json
{
  "date": "2026-11-01",
  "revision": 3,
  "sha256": "58369f733940f0a365b25e41a159eadde63a514f1d75bd881fc534cdec377ec4",
  "signature": "2140e2362f23c65a4e657d947c64cbb99105121769666af5616e83c3d1fb82eb"
}
```

- `sha256`: **CSV 文件原始字节**的 SHA-256(小写十六进制; 不做换行/编码归一化)
- `revision`: 正整数, 同一天从 1 开始递增
- `signature`: `HMAC-SHA256(key, message)` 的十六进制或 Base64
- `message`(UTF-8, 键排序、无空格、无换行):

  ```json
  {"date":"2026-11-01","revision":3,"sha256":"58369f..."}
  ```

上游生成清单(也可自行用任意语言实现):

```bash
python tools/make_manifest.py --date 2026-11-01 --revision 3 \
    --csv daily.csv --key-file /run/secrets/hmac_key --out daily.json
```

验签时也兼容常见的分隔符写法(`date\nrevision\ndigest`、`date:revision:digest`
等)与 Base64 签名; 但任何写法都必须同时包含日期、修订号与摘要, 因此签名无法
被旧批次或其他文件重放。

## API

### `POST /upload`
表单字段: `date`(YYYY-MM-DD), `file`(CSV 文件) 或 `csv`(CSV 文本)。
- `200` `{ok, rows, revision: 0, verified: false}` 替换成功
- `400` CSV 非法(空字段、乘客ID 重复、列数不足等)
- `409` 当天源数据已被首个查询锁定(`locked`), 或已完成验签导入
  (`verified_import_exists`)

### `POST /import`
CSV 与清单一起提交: multipart 字段 `csv`(文件) 与 `manifest`(JSON 文件),
或 JSON 请求体 `{date, csv, manifest}`; `date` 可选, 若提供必须与清单一致。
- `200` `{ok, revision, rows, verified, unchanged, csv_sha256?}` 验签并导入成功;
  `unchanged: true` 表示同版同文件, 数据未改动
- `400` 缺字段、清单格式错误、修订号非法、CSV 非法
- `403` 签名不匹配(`bad_signature`)
- `409` 摘要不一致(`digest_mismatch`)、日期不一致(`date_mismatch`)、
  同版不同文件(`revision_conflict`)、降版(`revision_downgrade`)、
  已被首个查询锁定(`locked`)
- `503` 服务未配置验签密钥(`key_not_configured`)

### `POST /query`
JSON: `{"date": "2026-11-01", "in_stations": ["西直门"], "out_stations": ["国贸", "望京"]}`
- `200` `{ok, noisy_count, cached, budget_remaining?}` — 只返回加噪人数
- `403` 当日预算不足, 不返回新结果
- 相同日期 + 相同站点集合(任意顺序/重复)返回首次发布的同一数值

### 其他
- `GET /api/stations?date=` 当天出现过的全部站点(供页面多选)
- `GET /api/status?date=` 行数、锁定状态、预算消耗、当前修订号与验签状态
- `GET /api/queries?date=` 当天已发布的全部加噪结果

## 测试

```bash
pip install pytest
python -m pytest tests/ -q
```

覆盖: 普通上传替换与锁定、验签导入(摘要/日期/修订号绑定、错密钥、篡改)、
验签后禁止普通覆盖、更高修订号整日替换、同版幂等与冲突、降版拒绝、
查询锁定后禁止替换、失败导入不推进修订号、并发导入幂等、重启后修订号保持、
规范化复用(乱序/重复)、预算上限(4 次新查询)、并发不超支、相同查询仅一份结果、
Laplace(0, 2) 噪声分布(均值 0、方差 8)。
