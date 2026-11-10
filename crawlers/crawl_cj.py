#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
조국혁신당 보도자료 수집기 (v2: 내부 API 기반)

배경
- 사이트가 Next.js 클라이언트 렌더링으로 바뀌어 HTML 목록에는 기사 링크가 없다.
- 목록은 사이트가 쓰는 내부 API(POST /api/board/list)로 받는다.
- 본문은 API의 descriptionText에 전체가 들어 있다. 기존 방식(<p> 태그만 읽기)은
  본문 일부를 빠뜨렸다 (2026-11 진단 참고).

동작
- 게시판별로 recordSize=100 페이지를 lastPage까지 순회한다.
- 수집 건수가 API가 보고한 pagination.count와 다르면 실패로 처리한다 (조용한 누락 방지).
- 페이지 응답 원본(JSON)을 raw/ 아래에 보존한다.
- 요청 실패는 재시도 후에도 실패하면 예외로 중단한다. 빈 결과를 성공으로 취급하지 않는다.

사용
  python crawl_cj.py --out ./out            # 전체 게시판
  python crawl_cj.py --out ./out --boards 보도자료
"""

import argparse
import csv
import json
import os
import random
import re
import sys
import time
from datetime import datetime

import requests

BASE_SITE = "https://rebuildingkoreaparty.kr"
API_URL = "https://api.rebuildingkoreaparty.kr/api/board/list"
PARTY_NAME = "조국혁신당"

# 게시판 이름 -> categoryId (사이트 JS 번들의 getCategoryId 매핑과 동일)
BOARDS = {
    "기자회견문": 6,
    "논평브리핑": 7,
    "보도자료": 9,
}

RECORD_SIZE = 100
MAX_RETRIES = 3
TIMEOUT = 30

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Origin": BASE_SITE,
    "Referer": BASE_SITE + "/news/press-release",
    "Accept-Language": "ko-KR,ko;q=0.9",
}


class CrawlError(RuntimeError):
    """수집 실패. 부분 결과를 성공으로 저장하지 않기 위해 사용한다."""


def make_session():
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def fetch_page(session, category_id, page):
    """한 페이지를 가져온다. 재시도 후에도 실패하면 CrawlError."""
    body = {
        "page": page,
        "categoryId": category_id,
        "recordSize": RECORD_SIZE,
        "pageSize": 5,
        "order": "recent",
    }
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = session.post(API_URL, data=json.dumps(body), timeout=TIMEOUT)
            r.raise_for_status()
            data = r.json()
            if not data.get("success", False):
                raise CrawlError(f"API success=false (page={page}): {data.get('description')}")
            return data
        except (requests.RequestException, ValueError, CrawlError) as e:
            last_err = e
            time.sleep(2 * attempt)
    raise CrawlError(f"page={page} category={category_id} 실패: {last_err}")


def collect_board(session, board_name, category_id, raw_dir):
    """게시판 전체를 수집하고 원본 페이지를 저장한다."""
    first = fetch_page(session, category_id, 1)
    pagination = first["pagination"]
    total = pagination["count"]
    last_page = pagination["lastPage"]

    items = []
    pages_raw = []
    pages_raw.append(first)
    items.extend(first.get("list") or [])

    for page in range(2, last_page + 1):
        time.sleep(random.uniform(0.3, 0.8))
        data = fetch_page(session, category_id, page)
        pages_raw.append(data)
        items.extend(data.get("list") or [])

    # 원본 보존
    os.makedirs(raw_dir, exist_ok=True)
    raw_path = os.path.join(raw_dir, f"{board_name}_raw.json")
    with open(raw_path, "w", encoding="utf-8") as f:
        json.dump(pages_raw, f, ensure_ascii=False)

    # 건수 검증
    ids = [it["id"] for it in items]
    if len(ids) != total:
        raise CrawlError(
            f"[{board_name}] 수집 {len(ids)}건 != API 보고 {total}건 (lastPage={last_page})"
        )
    if len(set(ids)) != len(ids):
        dup = len(ids) - len(set(ids))
        raise CrawlError(f"[{board_name}] 중복 id {dup}건 발생 (페이지 경계 밀림 의심)")

    return items, total


def extract_writer(text):
    """본문 마지막 쪽 문단 중 소속/직책이 드러나는 줄을 작성자로 본다 (기존 규칙 유지)."""
    if not text:
        return ""
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    for ln in reversed(lines[-6:]):
        if "조국혁신당" in ln or "대변인" in ln or "위원회" in ln:
            return ln
    return ""


def to_row(board_name, item):
    created = (item.get("createdAt") or "").strip()
    date = created.split(" ")[0] if created else ""
    text = (item.get("descriptionText") or "").strip()
    return {
        "id": item["id"],
        "board_name": board_name,
        "title": (item.get("title") or "").replace("\n", " ").strip(),
        "category": item.get("categoryTitle") or board_name,
        "date": date,
        "writer": extract_writer(text),
        "text": text,
        "url": f"{BASE_SITE}/news/{BOARD_PATHS[board_name]}/{item['id']}",
    }


BOARD_PATHS = {
    "기자회견문": "press-conference",
    "논평브리핑": "commentary-briefing",
    "보도자료": "press-release",
}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="출력 폴더 (CSV와 raw JSON이 저장됨)")
    ap.add_argument("--boards", nargs="*", default=list(BOARDS), help="수집할 게시판 이름")
    ap.add_argument("--start", default="2024-03-04", help="이 날짜 이전 글은 제외 (YYYY-MM-DD)")
    ap.add_argument("--end", default="2100-01-01", help="이 날짜 이후 글은 제외 (YYYY-MM-DD)")
    args = ap.parse_args(argv)

    out_dir = args.out
    raw_dir = os.path.join(out_dir, "raw")
    os.makedirs(out_dir, exist_ok=True)

    session = make_session()
    rows = []
    summary = {}

    for board_name in args.boards:
        if board_name not in BOARDS:
            raise SystemExit(f"알 수 없는 게시판: {board_name}")
        items, total = collect_board(session, board_name, BOARDS[board_name], raw_dir)
        kept = 0
        for it in items:
            row = to_row(board_name, it)
            if not row["date"]:
                raise CrawlError(f"[{board_name}] 날짜 없음 id={row['id']}")
            if row["date"] < args.start or row["date"] > args.end:
                continue
            rows.append(row)
            kept += 1
        summary[board_name] = {"api_count": total, "collected": len(items), "kept_in_range": kept}
        print(f"✓ [{board_name}] API {total}건 / 수집 {len(items)}건 / 기간 내 {kept}건", file=sys.stderr)

    # 빈 본문은 조용히 버리지 않고 표시한다.
    empty = [r["id"] for r in rows if not r["text"]]
    if empty:
        print(f"⚠ 본문 공란 {len(empty)}건 (id 목록은 summary.json에 기록)", file=sys.stderr)

    cols = ["id", "board_name", "title", "category", "date", "writer", "text", "url"]
    csv_path = os.path.join(out_dir, f"{PARTY_NAME}_보도자료_{args.start}_{args.end}.csv")
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, quoting=csv.QUOTE_ALL)
        w.writeheader()
        for r in rows:
            w.writerow(r)

    with open(os.path.join(out_dir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump({
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "boards": summary,
            "rows": len(rows),
            "empty_text_ids": empty,
        }, f, ensure_ascii=False, indent=2)

    print(f"✓ 저장: {csv_path} ({len(rows)}행)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except CrawlError as e:
        print(f"✗ 수집 중단: {e}", file=sys.stderr)
        sys.exit(2)
