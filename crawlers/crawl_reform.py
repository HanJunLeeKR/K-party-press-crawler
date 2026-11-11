#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
개혁신당 보도자료·논평브리핑 수집기

이전 버전의 문제 (docs/diagnosis.md)
- URL 이중 결합: base_url + 절대경로 href → https://www.reformparty.krhttps://... 로 깨짐.
  여기서는 urljoin만 쓴다.
- 목록은 서버 렌더링이라 requests만으로 충분하다 (상세 본문은 article 안의 .a-body).

수집 방식
- 목록: /news/{briefing|press}?page=N 을 빈 페이지가 2번 나올 때까지 순회 (목록 원본은 raw에 보존)
- 상세: 목록의 href를 그대로 쓴다. 본문이 짧으면 이미지 OCR(tesseract kor+eng)을 시도한다.
- 실패 기록: 수집 불가 사유를 fail_reason 컬럼에 남긴다 (성공 행은 빈 값).
- 재개: 상세 결과를 details.jsonl에 체크포인트로 쌓는다. 다시 실행하면 완료 건을 건너뛴다.

사용
  python crawl_reform.py --out ./out                      # 두 게시판 전체
  python crawl_reform.py --out ./out --board press --limit-pages 2 --no-details   # 목록만 시험
"""

import argparse
import csv
import gzip
import json
import os
import random
import re
import subprocess
import sys
import time
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

SITE = "https://www.reformparty.kr"
BOARDS = {
    # key(사이트 경로): (기존 CSV의 board_name, 카테고리 표시명)
    "briefing": ("논평브리핑", "논평·브리핑"),
    "press": ("보도자료", "보도자료"),
}
TIMEOUT = (10, 60)
MAX_RETRIES = 3
PAGE_SIZE = 20
ZERO_WIDTH = re.compile(r"[​‌‍﻿\xa0]")
DATE_RE = re.compile(r"(\d{4})\.(\d{2})\.(\d{2})")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
    "Accept-Language": "ko-KR,ko;q=0.9",
}

SUCCESS_STATUS = {"ok", "image_ocr"}
REASON_TEXT = {
    "no_body": "본문·이미지 모두 없음 (빈 게시물)",
    "image_no_src": "본문 이미지 주소(src) 없음",
    "image_download_failed": "본문 이미지 다운로드 실패 (외부 서버 오류 등)",
    "image_ocr_empty": "이미지는 받았으나 OCR 텍스트 없음 (글자 없는 사진·도표 등)",
    "fetch_failed": "상세 페이지 요청 실패 (재시도 후에도 실패)",
    "list_missing": "목록에서 상세 주소를 찾지 못함",
}


class FetchError(RuntimeError):
    pass


def make_session():
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def get(session, url, **kw):
    last = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = session.get(url, timeout=TIMEOUT, **kw)
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            last = e
            time.sleep(2 * attempt)
    raise FetchError(f"GET 실패 {url}: {last}")


def reason_text(status, fail_code=""):
    if status in SUCCESS_STATUS:
        return ""
    code = fail_code or status
    return REASON_TEXT.get(code, "기타: " + str(status))


def clean(text):
    return ZERO_WIDTH.sub("", text).strip()


def parse_list(html, key):
    """목록 한 페이지. 반환: [{post_id, url, title, date, writer, category}]"""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for wrap in soup.find_all("div", class_="news-item-wrap"):
        a = wrap.find("a", href=True)
        if a is None:
            continue
        url = urljoin(SITE, a["href"])  # 이중 결합 버그 방지: urljoin만 사용
        m = re.search(rf"/news/{key}/(\d+)", urlparse(url).path)
        if not m:
            continue
        title_el = wrap.find(class_="ni-title")
        meta = wrap.find(class_="ni-meta")
        date = ""
        writer = ""
        if meta is not None:
            dm = DATE_RE.search(meta.get_text(" ", strip=True))
            if dm:
                date = "-".join(dm.groups())
            au = meta.find(class_="author")
            writer = clean(au.get_text(" ", strip=True)) if au else ""
        badge = wrap.find(class_="ni-badge")
        out.append({
            "post_id": int(m.group(1)),
            "brd": key,
            "url": url,
            "title": clean(title_el.get_text(" ", strip=True)) if title_el else "",
            "date": date,
            "writer": writer,
            "category": clean(badge.get_text(" ", strip=True)) if badge else BOARDS[key][1],
        })
    return out


def walk_list(session, key, raw_dir, limit_pages=None, log=print):
    """빈 페이지가 연속 2번 나오면 끝으로 본다. post_id 기준으로 중복 제거."""
    items, seen = [], set()
    empty_run, page = 0, 1
    list_raw = os.path.join(raw_dir, "list")
    os.makedirs(list_raw, exist_ok=True)
    while True:
        if limit_pages and page > limit_pages:
            break
        r = get(session, f"{SITE}/news/{key}", params={"page": page})
        with gzip.open(os.path.join(list_raw, f"{key}_p{page}.html.gz"), "wt", encoding="utf-8") as f:
            f.write(r.text)
        got = parse_list(r.text, key)
        if not got:
            empty_run += 1
            if empty_run >= 2:
                break
        else:
            empty_run = 0
            new = [g for g in got if g["post_id"] not in seen]
            for g in new:
                seen.add(g["post_id"])
            items.extend(new)
        if page % 10 == 0:
            log(f"    목록 {key} page {page}: 누적 {len(items)}건")
        page += 1
        time.sleep(random.uniform(0.2, 0.5))
    log(f"  목록 {key}: {page - 1 - empty_run}쪽 순회, 고유 {len(items)}건")
    return items


def image_srcs(body):
    if body is None:
        return []
    return [urljoin(SITE, img["src"]) for img in body.find_all("img", src=True)
            if not img["src"].startswith("data:")]


def ocr_images(session, body, post_id, img_dir, max_images=30):
    """이미지 OCR. 반환: (텍스트, 이미지 주소 수, 다운로드 성공 수)"""
    srcs = image_srcs(body)
    if not srcs:
        return "", 0, 0
    os.makedirs(img_dir, exist_ok=True)
    texts, downloaded = [], 0
    for n, url in enumerate(srcs[:max_images], 1):
        try:
            r = get(session, url)
        except FetchError:
            continue
        ext = os.path.splitext(urlparse(url).path)[1].lower() or ".jpg"
        path = os.path.join(img_dir, f"{post_id}_{n}{ext}")
        with open(path, "wb") as f:
            f.write(r.content)
        downloaded += 1
        try:
            res = subprocess.run(["tesseract", path, "-", "-l", "kor+eng", "--psm", "3"],
                                 capture_output=True, timeout=180)
            t = clean(res.stdout.decode("utf-8", errors="ignore"))
        except (OSError, subprocess.SubprocessError):
            t = ""
        if t:
            texts.append(t)
    return "\n".join(texts), len(srcs), downloaded


def fetch_detail(session, item, raw_dir):
    r = get(session, item["url"])
    soup = BeautifulSoup(r.text, "html.parser")
    art = soup.find("article")
    if art is None:
        raise FetchError("article 요소 없음 (응답 이상)")
    rawd = os.path.join(raw_dir, "detail")
    os.makedirs(rawd, exist_ok=True)
    with gzip.open(os.path.join(rawd, f"{item['brd']}_{item['post_id']}.html.gz"), "wt", encoding="utf-8") as f:
        f.write(r.text)

    body_el = art.find(class_="a-body")
    body = clean(body_el.get_text("\n", strip=True)) if body_el else ""
    hm = DATE_RE.search(art.get_text(" ", strip=True))
    pub = "-".join(hm.groups()) if hm else ""

    status, fail_code, source = "ok", "", "body"
    text = body
    if len(body) < 5:
        source = "none"
        if body_el is not None and body_el.find("img"):
            ocr, total, downloaded = ocr_images(session, body_el, item["post_id"], os.path.join(raw_dir, "images"))
            if ocr:
                text, source, status = ocr, "ocr", "image_ocr"
            else:
                status = "image_only_ocr_empty"
                fail_code = ("image_no_src" if total == 0
                             else "image_download_failed" if downloaded == 0
                             else "image_ocr_empty")
        else:
            status = "no_body"
            text = ""

    return {
        **item,
        "pub_date": pub,
        "date_mismatch": bool(pub and item["date"] and pub != item["date"]),
        "text": text,
        "text_source": source,
        "status": status,
        "fail_code": fail_code,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
    }


def load_jsonl(path):
    if not os.path.exists(path):
        return []
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
    return rows


def run_details(session, items, out_dir, raw_dir, log=print):
    ck = os.path.join(out_dir, "details.jsonl")
    done = {r["post_id"] for r in load_jsonl(ck)}
    todo = [it for it in items if it["post_id"] not in done]
    log(f"  상세 대상 {len(todo)}건 (완료 {len(done)}건 건너뜀)")
    failed = 0
    with open(ck, "a", encoding="utf-8") as out:
        for i, it in enumerate(todo, 1):
            time.sleep(random.uniform(0.3, 0.8))
            try:
                rec = fetch_detail(session, it, raw_dir)
            except (FetchError, requests.RequestException, OSError) as e:
                failed += 1
                rec = {**it, "pub_date": "", "date_mismatch": False, "text": "", "text_source": "none",
                       "status": "fetch_failed", "fail_code": "", "error": str(e)[:200],
                       "fetched_at": datetime.now().isoformat(timespec="seconds")}
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")
            out.flush()
            if i % 100 == 0:
                log(f"    {i}/{len(todo)}")
    return failed


def write_csv(out_dir, rows, name):
    cols = ["post_id", "board_name", "title", "category", "date", "pub_date", "date_mismatch",
            "writer", "text", "text_source", "status", "fail_reason", "url", "fetched_at"]
    path = os.path.join(out_dir, name)
    last = {}
    for r in rows:
        last[r["post_id"]] = r  # 같은 post_id는 마지막 기록 사용
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, quoting=csv.QUOTE_ALL, extrasaction="ignore")
        w.writeheader()
        for r in last.values():
            r = dict(r)
            r["board_name"] = BOARDS[r["brd"]][0]
            r["fail_reason"] = reason_text(r.get("status", ""), r.get("fail_code", ""))
            w.writerow(r)
    return path, len(last)


def ymd(s):
    """argparse용 날짜 검증: YYYY-MM-DD."""
    try:
        datetime.strptime(s, "%Y-%m-%d")
    except ValueError:
        raise argparse.ArgumentTypeError(f"날짜는 YYYY-MM-DD 형식이어야 합니다: {s!r}")
    return s


def filter_period(items, since, until, log=print):
    """기간(양끝 포함)으로 목록 항목을 거른다. 목록에 날짜가 없는 글은 기간을 확인할 수 없어 제외한다."""
    if not since and not until:
        return items
    kept, undated = [], 0
    for it in items:
        d = it.get("date") or ""
        if not d:
            undated += 1
        elif (not since or d >= since) and (not until or d <= until):
            kept.append(it)
    log(f"  기간 {since or '처음'} ~ {until or '끝'}: {len(items)}건 → {len(kept)}건"
        + (f" (날짜 없는 {undated}건 제외)" if undated else ""))
    return kept


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--board", choices=list(BOARDS) + ["all"], default="all")
    ap.add_argument("--limit-pages", type=int, default=None)
    ap.add_argument("--no-details", action="store_true")
    ap.add_argument("--since", type=ymd, default=None, help="이 날짜(포함) 이후 글만 수집 (YYYY-MM-DD)")
    ap.add_argument("--until", type=ymd, default=None, help="이 날짜(포함) 이전 글만 수집 (YYYY-MM-DD)")
    args = ap.parse_args(argv)

    def log(msg):
        print(msg, file=sys.stderr, flush=True)

    keys = list(BOARDS) if args.board == "all" else [args.board]
    session = make_session()
    for key in keys:
        name = BOARDS[key][0]
        out_dir = os.path.join(args.out, f"reform_{key}")
        raw_dir = os.path.join(out_dir, "raw")
        os.makedirs(out_dir, exist_ok=True)
        log(f"[{name}] 목록 수집")
        items = walk_list(session, key, raw_dir, limit_pages=args.limit_pages, log=log)
        with open(os.path.join(out_dir, "list.json"), "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False)
        items = filter_period(items, args.since, args.until, log=log)
        if args.no_details:
            continue
        failed = run_details(session, items, out_dir, raw_dir, log=log)
        rows = load_jsonl(os.path.join(out_dir, "details.jsonl"))
        path, n = write_csv(out_dir, rows, f"{name}.csv")
        statuses = {}
        for r in rows:
            statuses[r.get("status")] = statuses.get(r.get("status"), 0) + 1
        summary = {"board": name, "key": key, "list_count": len(items), "rows": n,
                   "fetch_failed": failed, "status_counts": statuses,
                   "generated_at": datetime.now().isoformat(timespec="seconds")}
        with open(os.path.join(out_dir, "summary.json"), "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        log(f"✓ 저장 {path} ({n}행) 상태: {statuses}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
