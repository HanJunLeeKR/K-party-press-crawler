#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
국민의힘(peoplepowerparty.kr) 보도자료 수집기 (v2)

이전 버전의 문제 (docs/diagnosis.md)
- 빈 본문 376건: HTTP 200 응답이 왔는데 본문이 비었고, 요청 실패와 구분하지 않아 재시도 대상에서 빠졌다.
  여기서는 본문 공란을 '실패'로 기록하고 재수집 대상으로 다시 넣는다.
- 첫 요청 연결 리셋: 재시도 간격을 늘리고(2초 단위 증가), 실패 건은 재시도 큐 3라운드 뒤에 fetch_failed로 남긴다.
- 307 리다이렉트: requests.Session으로 쿠키를 유지한다 (DP와 같은 문제).
- 조기 종료: 빈 페이지가 연속 2번 나와야 목록 순회를 끝낸다. 마지막 페이지는 이진 탐색으로 확인한다.

수집 방식
- 목록: /news/comment/{bid}?page=N (행 10개). 원본 HTML은 raw/list 에 보존한다.
- 상세: /news/comment_view/{bid}/{id}. 본문은 dd.conts (hwpEditorBoardContent가 있으면 그 안).
- 델타 모드(--exclude-csv): 기존 CSV에 본문이 있는 post는 건너뛰고, 본문이 비었던 post는 다시 받는다.

사용
  python crawl_ppp.py --out ./out                                   # 전체 목록 + 전체 상세
  python crawl_ppp.py --out ./out --exclude-csv 국민의힘.csv        # 기존 파일에 없는 것 + 빈 본문만
  python crawl_ppp.py --out ./out --no-details                      # 목록만
"""

import argparse
import base64
import csv
import gzip
import json
import os
import random
import re
import sys
import time
import concurrent.futures
from datetime import datetime
from urllib.parse import urljoin, urlparse

import subprocess

import requests
from bs4 import BeautifulSoup

SITE = "https://www.peoplepowerparty.kr"
BOARDS = {
    # 게시판 ID: 기존 CSV의 board_name
    "BBSDD0001": "대변인_논평보도자료",
    "BBSDD0002": "원내_보도자료",
    "BBSDD0003": "정책위_보도자료",
    "BBSDD0042": "미디어특위_보도자료",
}
TIMEOUT = (10, 60)
MAX_RETRIES = 3
WORKERS = 3
DELAY = 0.0  # >0이면 요청마다 DELAY~DELAY*1.5초 대기 (--delay)
DATE_RE = re.compile(r"(\d{4})[-.](\d{1,2})[-.](\d{1,2})")
ZERO_WIDTH = re.compile(r"[​‌‍﻿\xa0]")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
    "Accept-Language": "ko-KR,ko;q=0.9",
}

SUCCESS_STATUS = {"ok", "image_ocr"}
REASON_TEXT = {
    "no_body": "본문·이미지 모두 없음 (빈 게시물)",
    "image_only_ocr_empty": "본문이 이미지뿐이며 OCR 텍스트 없음 (글자 없는 사진·도표 등)",
    "image_download_failed": "본문 이미지 다운로드 실패 (외부 서버 오류 등)",
    "image_no_src": "본문 이미지 주소(src) 없음",
    "fetch_failed": "상세 페이지 요청 실패 (재시도 후에도 실패)",
    "list_failed": "목록 페이지 요청 실패 (재시도 후에도 실패)",
}


class FetchError(RuntimeError):
    pass


def make_session():
    s = requests.Session()  # 쿠키 유지 (307 리다이렉트 통과)
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
            time.sleep(2 * attempt)  # 연결 리셋 대비 간격을 늘린다
    raise FetchError(f"GET 실패 {url}: {last}")


def pause(lo, hi):
    """요청 간 대기. --delay를 주면 그 값(초)~1.5배로 대기한다."""
    if DELAY > 0:
        time.sleep(random.uniform(DELAY, DELAY * 1.5))
    else:
        time.sleep(random.uniform(lo, hi))


def reason_text(status, fail_code=""):
    if status in SUCCESS_STATUS:
        return ""
    code = fail_code or status
    return REASON_TEXT.get(code, "기타: " + str(status))


def clean(text):
    return ZERO_WIDTH.sub("", text).strip()


def norm_date(s):
    m = DATE_RE.search(s or "")
    if not m:
        return ""
    y, mo, d = m.groups()
    return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"


def parse_list(html, bid):
    """목록 한 페이지. 반환: [{post_id, bid, url, title, section, date}]"""
    soup = BeautifulSoup(html, "html.parser")
    tbl = soup.find("div", class_="board-tbl")
    if tbl is None or tbl.find("tbody") is None:
        return []
    out = []
    for tr in tbl.find("tbody").find_all("tr"):
        a = tr.find("a", href=True)
        if a is None:
            continue
        m = re.search(rf"/comment_view/{bid}/(\d+)", a["href"])
        if not m:
            continue
        sec = tr.find("td", class_="class")
        dt = tr.find("td", class_="date")
        out.append({
            "post_id": int(m.group(1)),
            "bid": bid,
            "url": urljoin(SITE, f"/news/comment_view/{bid}/{m.group(1)}"),
            "title": clean(a.get_text(" ", strip=True)),
            "section": clean(sec.get_text(" ", strip=True)) if sec else "",
            "date": norm_date(dt.get_text(" ", strip=True)) if dt else "",
        })
    return out


def last_page(session, bid):
    """목록의 마지막 비어 있지 않은 페이지를 이진 탐색으로 찾는다."""
    def has_rows(p):
        return len(parse_list(get(session, f"{SITE}/news/comment/{bid}", params={"page": p}).text, bid)) > 0
    lo, hi = 1, 2
    while has_rows(hi):
        lo, hi = hi, hi * 2
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if has_rows(mid):
            lo = mid
        else:
            hi = mid
    return lo


def fetch_list_page(session, bid, page, raw_dir):
    r = get(session, f"{SITE}/news/comment/{bid}", params={"page": page})
    os.makedirs(os.path.join(raw_dir, "list"), exist_ok=True)
    with gzip.open(os.path.join(raw_dir, "list", f"{bid}_p{page}.html.gz"), "wt", encoding="utf-8") as f:
        f.write(r.text)
    return parse_list(r.text, bid)


def walk_list(session, bid, raw_dir, log=print):
    """마지막 페이지까지 병렬로 받고, 실패한 페이지는 한 번 더 받는다. post_id 기준 중복 제거."""
    last = last_page(session, bid)
    log(f"  [{BOARDS[bid]}] 마지막 페이지 {last} (약 {last * 10}건 이하)")
    pages = list(range(1, last + 1))
    got, failed_pages = {}, []

    def work(p):
        pause(0.2, 0.5)
        try:
            return p, fetch_list_page(session, bid, p, raw_dir), None
        except (FetchError, requests.RequestException, OSError) as e:
            return p, None, e

    for rnd in range(2):
        todo = pages if rnd == 0 else failed_pages
        failed_pages = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
            for p, rows, err in pool.map(work, todo):
                if rows is None:
                    failed_pages.append(p)
                    continue
                for row in rows:
                    got.setdefault(row["post_id"], row)
        if not failed_pages:
            break
        log(f"  목록 라운드 {rnd} 실패 {len(failed_pages)}쪽 → 재시도")
        time.sleep(20)
    if failed_pages:
        log(f"  ⚠ 끝내 실패한 목록 페이지: {failed_pages}")
    items = sorted(got.values(), key=lambda x: x["post_id"])
    log(f"  [{BOARDS[bid]}] 고유 {len(items)}건 (목록 실패 페이지 {len(failed_pages)})")
    return items, failed_pages


def ocr_images(session, conts, post_id, img_dir, max_images=30):
    """본문 이미지 OCR (tesseract kor+eng). 반환: (텍스트, 이미지 주소 수, 다운로드 성공 수)"""
    srcs = [img["src"] for img in conts.find_all("img", src=True)]
    if not srcs:
        return "", 0, 0
    os.makedirs(img_dir, exist_ok=True)
    texts, downloaded = [], 0
    for n, src in enumerate(srcs[:max_images], 1):
        if src.startswith("data:image/"):
            # 본문에 base64로 직접 넣은 이미지: 디코딩해서 저장
            head, _, b64 = src.partition(",")
            ext = "." + head.split("/")[1].split(";")[0].replace("jpeg", "jpg")
            try:
                content = base64.b64decode(b64)
            except (ValueError, TypeError):
                continue
        else:
            try:
                content = get(session, urljoin(SITE, src)).content
            except FetchError:
                continue
            ext = os.path.splitext(urlparse(urljoin(SITE, src)).path)[1].lower() or ".jpg"
        path = os.path.join(img_dir, f"{post_id}_{n}{ext}")
        with open(path, "wb") as f:
            f.write(content)
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


def extract_body(soup):
    """본문 텍스트. hwpEditorBoardContent 칸이 비어 있는 게시물이 있어서(본문이 칸 밖에 있음)
    칸에 글이 없으면 dd.conts 전체 텍스트를 쓴다."""
    conts = soup.select_one("dd.conts")
    if conts is None:
        return None
    hwp = conts.find("div", {"id": "hwpEditorBoardContent"})
    if hwp is not None:
        t = clean(hwp.get_text("\n", strip=True))
        if len(t) >= 5:
            return t
    return clean(conts.get_text("\n", strip=True))


def fetch_detail(session, item, raw_dir):
    r = get(session, item["url"])
    soup = BeautifulSoup(r.text, "html.parser")
    body = extract_body(soup)
    if body is None:
        raise FetchError("dd.conts 없음 (응답 이상)")
    os.makedirs(os.path.join(raw_dir, "detail"), exist_ok=True)
    with gzip.open(os.path.join(raw_dir, "detail", f"{item['bid']}_{item['post_id']}.html.gz"), "wt", encoding="utf-8") as f:
        f.write(r.text)
    dd = soup.select_one("dd.date")
    pub = norm_date(dd.get_text(" ", strip=True)) if dd else ""
    fail_code = ""
    if len(body) >= 5:
        status, text = "ok", body
    else:
        status, text = "no_body", ""
        conts = soup.select_one("dd.conts")
        if conts is not None and conts.find("img"):
            ocr, total, downloaded = ocr_images(session, conts, item["post_id"], os.path.join(raw_dir, "images"))
            if ocr:
                status, text = "image_ocr", ocr
            else:
                status = "image_only_ocr_empty"
                fail_code = ("image_no_src" if total == 0 else
                             "image_download_failed" if downloaded == 0 else "image_only_ocr_empty")
    return {
        **item,
        "pub_date": pub,
        "date_mismatch": bool(pub and item["date"] and pub != item["date"]),
        "text": text,
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
    """체크포인트(JSONL) 기반. 완료 건은 건너뛴다. 실패는 재시도 3라운드."""
    ck = os.path.join(out_dir, "details.jsonl")
    done = {r["post_id"] for r in load_jsonl(ck) if r.get("status") in SUCCESS_STATUS | {"no_body"}}
    todo = [it for it in items if it["post_id"] not in done]
    log(f"  상세 대상 {len(todo)}건 (완료 {len(done)}건 건너뜀)")

    def work(it):
        pause(0.3, 0.8)
        try:
            return it, fetch_detail(session, it, raw_dir), None
        except (FetchError, requests.RequestException, OSError) as e:
            return it, None, e

    failed = []
    with open(ck, "a", encoding="utf-8") as out, \
            concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for rnd in range(3):
            queue = todo if rnd == 0 else failed
            failed = []
            for i, (it, rec, err) in enumerate(pool.map(work, queue), 1):
                if rec is None:
                    failed.append(it)
                    continue
                out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                out.flush()
                if i % 200 == 0:
                    log(f"    round {rnd}: {i}/{len(queue)}")
            if not failed:
                break
            log(f"  라운드 {rnd} 실패 {len(failed)}건 → 재시도")
            time.sleep(30)
        for it in failed:
            out.write(json.dumps({**it, "pub_date": "", "date_mismatch": False, "text": "",
                                  "status": "fetch_failed",
                                  "fetched_at": datetime.now().isoformat(timespec="seconds")},
                                 ensure_ascii=False) + "\n")
    return len(failed)


def old_text_ids(path):
    """기존 CSV에서 본문이 있는 post_id와 본문이 빈 post_id를 나눠 돌려준다."""
    csv.field_size_limit(sys.maxsize)
    ok, empty = set(), set()
    with open(path, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            m = re.search(r"/comment_view/BBSDD\d+/(\d+)", r.get("url") or "")
            if not m:
                continue
            pid = int(m.group(1))
            t = (r.get("text") or "").strip()
            if t and t not in ("nan", "본문 조회 실패"):
                ok.add(pid)
            else:
                empty.add(pid)
    return ok, empty


def write_csv(out_dir, rows, name):
    cols = ["post_id", "board_name", "title", "section", "date", "pub_date", "date_mismatch",
            "text", "text_source", "status", "fail_reason", "url", "fetched_at"]
    path = os.path.join(out_dir, name)
    last = {}
    for r in rows:
        last[r["post_id"]] = r
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, quoting=csv.QUOTE_ALL, extrasaction="ignore")
        w.writeheader()
        for r in last.values():
            r = dict(r)
            r["board_name"] = BOARDS.get(r.get("bid"), "")
            r["fail_reason"] = reason_text(r.get("status", ""), r.get("fail_code", ""))
            r["text_source"] = {"ok": "body", "image_ocr": "ocr"}.get(r.get("status", ""), "none")
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
    ap.add_argument("--no-details", action="store_true", help="목록만 수집")
    ap.add_argument("--since", type=ymd, default=None, help="이 날짜(포함) 이후 글만 수집 (YYYY-MM-DD)")
    ap.add_argument("--until", type=ymd, default=None, help="이 날짜(포함) 이전 글만 수집 (YYYY-MM-DD)")
    ap.add_argument("--workers", type=int, default=3,
                    help="동시 요청 수 (기본 3). 서버가 연결을 끊으면 1로 낮추세요")
    ap.add_argument("--delay", type=float, default=0.0,
                    help="요청마다 대기할 초 (기본 0.2~0.8초). 느리게 받으려면 1.5 등으로 지정")
    ap.add_argument("--reuse-list", action="store_true",
                    help="출력 폴더에 list.json이 있으면 목록을 다시 받지 않고 그대로 사용 (이어 받기용)")
    ap.add_argument("--exclude-csv", default="", help="기존 CSV: 본문 있는 항목은 건너뛰고 빈 본문 항목은 다시 받음")
    args = ap.parse_args(argv)

    def log(msg):
        print(msg, file=sys.stderr, flush=True)

    keep_ok, keep_empty = (set(), set())
    if args.exclude_csv:
        keep_ok, keep_empty = old_text_ids(args.exclude_csv)
        log(f"  기존 CSV: 본문 있는 {len(keep_ok)}건 제외, 빈 본문 {len(keep_empty)}건 재수집")

    global WORKERS, DELAY
    WORKERS, DELAY = max(1, args.workers), max(0.0, args.delay)
    session = make_session()
    for i in range(6):  # 쿠키 확보 (서버가 첫 연결을 끊는 경우가 있어 재시도)
        try:
            session.get(SITE + "/news/comment/BBSDD0001", timeout=TIMEOUT)
            break
        except requests.RequestException:
            if i == 5:
                raise
            time.sleep(10 * (i + 1))
    bids = list(BOARDS) if args.board == "all" else [args.board]
    for bid in bids:
        name = BOARDS[bid]
        out_dir = os.path.join(args.out, f"ppp_{bid}")
        raw_dir = os.path.join(out_dir, "raw")
        os.makedirs(out_dir, exist_ok=True)
        log(f"[{name}] 목록 수집")
        list_path = os.path.join(out_dir, "list.json")
        if args.reuse_list and os.path.exists(list_path) and os.path.getsize(list_path) > 2:
            with open(list_path, encoding="utf-8") as f:
                items = json.load(f)
            failed_pages = []
            log(f"  기존 list.json 사용 ({len(items)}건) — 목록 재수집 생략")
        else:
            items, failed_pages = walk_list(session, bid, raw_dir, log=log)
            with open(list_path, "w", encoding="utf-8") as f:
                json.dump(items, f, ensure_ascii=False)
        items = filter_period(items, args.since, args.until, log=log)
        if args.no_details:
            continue
        if args.exclude_csv:
            before = len(items)
            items = [it for it in items if it["post_id"] not in keep_ok]
            log(f"  기존 CSV 제외: {before}건 → {len(items)}건 남음")
        failed = run_details(session, items, out_dir, raw_dir, log=log)
        rows = load_jsonl(os.path.join(out_dir, "details.jsonl"))
        path, n = write_csv(out_dir, rows, f"{name}.csv")
        statuses = {}
        for r in rows:
            statuses[r.get("status")] = statuses.get(r.get("status"), 0) + 1
        summary = {"board": name, "bid": bid, "list_count": len(items), "rows": n,
                   "list_failed_pages": failed_pages, "fetch_failed": failed,
                   "status_counts": statuses,
                   "generated_at": datetime.now().isoformat(timespec="seconds")}
        with open(os.path.join(out_dir, "summary.json"), "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        log(f"✓ 저장 {path} ({n}행) 상태: {statuses}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
