"""受控运维：生成一次性邀请码并只输出明文一次。"""
from __future__ import annotations

import argparse
import hashlib
import secrets

import psycopg


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn", required=True)
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--expires-days", type=int, default=30)
    args = parser.parse_args()
    if args.count < 1 or args.count > 100:
        raise SystemExit("--count 必须在 1..100")
    if args.expires_days < 1 or args.expires_days > 365:
        raise SystemExit("--expires-days 必须在 1..365")

    codes: list[str] = []
    hashes: list[str] = []
    for _ in range(args.count):
        code = secrets.token_urlsafe(18)
        codes.append(code)
        hashes.append(hashlib.sha256(code.encode("utf-8")).hexdigest())

    with psycopg.connect(args.dsn) as conn:
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO invite_code (code_hash, expires_at)
                VALUES (%s, NOW() + (%s || ' days')::interval)
                """,
                [(code_hash, args.expires_days) for code_hash in hashes],
            )
    for code in codes:
        print(code)


if __name__ == "__main__":
    main()
