#!/usr/bin/env python3
"""上游交付辅助工具: 为一份 CSV 生成验签清单.

用法:
    python tools/make_manifest.py --date 2026-11-01 --revision 3 \
        --csv daily.csv --key-file /run/secrets/hmac_key --out daily.json

密钥也可以直接由环境变量 MANIFEST_HMAC_KEY(或 APP_HMAC_KEY 等)提供。
签名算法: HMAC-SHA256(key, message), message 为键排序、无空格的 JSON:
    {"date":"YYYY-MM-DD","revision":N,"sha256":"<小写十六进制>"}
"""

import argparse
import hashlib
import hmac
import json
import os
import sys

KEY_ENV_VARS = ("MANIFEST_HMAC_KEY", "APP_HMAC_KEY", "HMAC_KEY", "SIGNING_KEY")


def canonical_signing_message(date, revision, sha256_hex):
    """与 app.canonical_signing_message 保持一致: 键排序、无空格的 JSON."""
    return json.dumps({"date": date, "revision": int(revision),
                       "sha256": sha256_hex.lower()},
                      sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def load_key(args):
    if args.key:
        return args.key.encode("utf-8")
    if args.key_file:
        with open(args.key_file, "rb") as fh:
            return fh.read().strip()
    for name in KEY_ENV_VARS:
        value = os.environ.get(name)
        if value:
            return value.encode("utf-8")
    raise SystemExit("未提供密钥: 使用 --key / --key-file 或设置 MANIFEST_HMAC_KEY")


def main():
    parser = argparse.ArgumentParser(description="生成 CSV 验签清单")
    parser.add_argument("--date", required=True, help="数据日期 YYYY-MM-DD")
    parser.add_argument("--revision", required=True, type=int, help="递增修订号(>=1)")
    parser.add_argument("--csv", required=True, help="CSV 文件路径")
    parser.add_argument("--out", help="输出清单路径(默认 stdout)")
    parser.add_argument("--key", help="HMAC 密钥(不推荐, 会留在 shell 历史)")
    parser.add_argument("--key-file", help="保存 HMAC 密钥的文件")
    args = parser.parse_args()

    if args.revision < 1:
        raise SystemExit("修订号必须是正整数(从 1 开始)")

    with open(args.csv, "rb") as fh:
        data = fh.read()
    digest = hashlib.sha256(data).hexdigest()
    message = canonical_signing_message(args.date, args.revision, digest)
    signature = hmac.new(load_key(args), message.encode("utf-8"),
                         hashlib.sha256).hexdigest()
    manifest = {"date": args.date, "revision": args.revision,
                "sha256": digest, "signature": signature}

    text = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
    else:
        sys.stdout.write(text)


if __name__ == "__main__":
    main()
