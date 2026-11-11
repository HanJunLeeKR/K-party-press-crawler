#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
기본소득당(basicincomeparty.kr) 논평·보도자료 수집기

사이트 구조
- 그누보드 계열. 목록은 /bikr/press?page=N (한 페이지 15건), 마지막 페이지 번호는 페이저에 있다.
- 목록 행: td.td_subject 안의 a(href=/bikr/press/{id}), td.td_name (작성자), td.td_datetime (YY.MM.DD.).
- 상세: article#bo_v. 본문은 #bo_v_con, 작성자와 작성일은 section#bo_v_info.

기존 데이터 문제 (docs/diagnosis.md)
- 690건(36%)이 '본문 조회 실패'였다. 사이트 응답은 정상이었고, 연결 리셋(Connection reset)이 간헐적으로 난다.
  여기서는 연결 실패를 재시도(간격 증가 + 3라운드 큐)하고, 최종 실패는 fetch_failed로 기록한다.
- 본문이 비어 있으면 '본문 없음'과 '요청 실패'를 구분해서 기록한다.

수집 방식
- 목록: 1쪽부터 마지막 쪽까지 병렬 수집 (원본 HTML은 raw/list 에 보존). 실패 쪽은 한 번 더 받는다.
- 상세: 목록의 id로 /bikr/press/{id}를 받는다. 본문이 짧으면 이미지 OCR(tesseract kor+eng)을 시도한다.
- 델타 모드(--exclude-csv): 기존 CSV에 본문이 있는 항목은 건너뛰고, 실패였던 항목은 다시 받는다.

사용
  python crawl_basicincome.py --out ./out
  python crawl_basicincome.py --out ./out --exclude-csv 기본소득당보도자료.csv
  python crawl_basicincome.py --out ./out --no-details
"""

import argparse
import base64
import concurrent.futures
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

SITE = "https://basicincomeparty.kr"
LIST_PATH = "/bikr/press"
BOARD_NAME = "논평보도자료"  # 기존 CSV의 board_name과 맞춤
TIMEOUT = (10, 60)
MAX_RETRIES = 4
WORKERS = 3
ZERO_WIDTH = re.compile(r"[​‌‍﻿\xa0]")
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
    "image_only_ocr_empty": "본문이 이미지뿐이며 OCR 텍스트 없음",
    "fetch_failed": "상세 페이지 요청 실패 (재시도 후에도 실패)",
    "list_failed": "목록 페이지 요청 실패 (재시도 후에도 실패)",
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
            time.sleep(3 * attempt)  # 연결 리셋 대비: 3, 6, 9초 간격
    raise FetchError(f"GET 실패 {url}: {last}")


def reason_text(status, fail_code=""):
    if status in SUCCESS_STATUS:
        return ""
    code = fail_code or status
    return REASON_TEXT.get(code, "기타: " + str(status))


def clean(text):
    return ZERO_WIDTH.sub("", text).strip()


def norm_date(s):
    """'26.11.11.' 또는 '2026.02.26. 17:18' → '2026-11-11'"""
    m = re.search(r"(\d{2,4})\.\s*(\d{1,2})\.\s*(\d{1,2})", s or "")
    if not m:
        return ""
    y, mo, d = m.groups()
    y = int(y)
    if y < 100:
        y += 2000
    return f"{y:04d}-{int(mo):02d}-{int(d):02d}"


def parse_list(html):
    """목록 한 쪽. 반환: [{post_id, url, title, category, writer, date}]"""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for tr in soup.find_all("tr"):
        sub = tr.find("td", class_="td_subject")
        if sub is None:
            continue
        a = sub.find("a", href=re.compile(r"/bikr/press/\d+"))
        if a is None:
            continue
        pid = int(re.search(r"/bikr/press/(\d+)", a["href"]).group(1))
        cate = tr.find("a", class_="bo_cate_link")
        name = tr.find("td", class_="td_name")
        dt = tr.find("td", class_="td_datetime")
        out.append({
            "post_id": pid,
            "url": f"{SITE}{LIST_PATH}/{pid}",
            "title": clean(a.get_text(" ", strip=True)),
            "category": clean(cate.get_text(" ", strip=True)) if cate else "",
            "writer": clean(name.get_text(" ", strip=True)) if name else "",
            "date": norm_date(dt.get_text(" ", strip=True)) if dt else "",
        })
    return out


def last_page(session, raw_dir):
    """페이저의 가장 큰 쪽 번호를 찾고, 그 쪽이 비어 있지 않은지 확인한다."""
    r = get(session, f"{SITE}{LIST_PATH}")
    nums = [int(m) for m in re.findall(r"[?&]page=(\d+)", r.text)]
    cand = max(nums) if nums else 1
    if not parse_list(get(session, f"{SITE}{LIST_PATH}", params={"page": cand}).text):
        raise FetchError(f"마지막 쪽 {cand}이 비어 있음")
    if parse_list(get(session, f"{SITE}{LIST_PATH}", params={"page": cand + 1}).text):
        raise FetchError("페이저 최대값보다 뒤 쪽에도 목록이 있음 (페이저 해석 확인 필요)")
    return cand


def fetch_list_page(session, page, raw_dir):
    r = get(session, f"{SITE}{LIST_PATH}", params={"page": page})
    os.makedirs(os.path.join(raw_dir, "list"), exist_ok=True)
    with gzip.open(os.path.join(raw_dir, "list", f"p{page}.html.gz"), "wt", encoding="utf-8") as f:
        f.write(r.text)
    return parse_list(r.text)


def walk_list(session, raw_dir, log=print):
    last = last_page(session, raw_dir)
    log(f"  마지막 쪽 {last}")
    got, failed_pages = {}, []

    def work(p):
        time.sleep(random.uniform(0.3, 0.8))
        try:
            return p, fetch_list_page(session, p, raw_dir)
        except (FetchError, requests.RequestException, OSError):
            return p, None

    pages = list(range(1, last + 1))
    for rnd in range(3):
        todo = pages if rnd == 0 else failed_pages
        failed_pages = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
            for p, rows in pool.map(work, todo):
                if rows is None:
                    failed_pages.append(p)
                    continue
                for row in rows:
                    got.setdefault(row["post_id"], row)
        if not failed_pages:
            break
        log(f"  목록 라운드 {rnd} 실패 {len(failed_pages)}쪽 → 재시도")
        time.sleep(30)
    if failed_pages:
        log(f"  ⚠ 끝내 실패한 목록 쪽: {failed_pages}")
    items = sorted(got.values(), key=lambda x: x["post_id"])
    log(f"  고유 {len(items)}건")
    return items, failed_pages


def image_srcs_and_ocr(session, conts, post_id, img_dir, max_images=30):
    """본문 이미지 OCR. data: 이미지는 디코딩. 반환: (텍스트, 이미지 수, 다운로드 성공 수)"""
    srcs = [img["src"] for img in conts.find_all("img", src=True)]
    if not srcs:
        return "", 0, 0
    os.makedirs(img_dir, exist_ok=True)
    texts, downloaded = [], 0
    for n, src in enumerate(srcs[:max_images], 1):
        if src.startswith("data:image/"):
            head, _, b64 = src.partition(",")
            ext = "." + head.split("/")[1].split(";")[0].replace("jpeg", "jpg")
            try:
                content = base64.b64decode(b64)
            except (ValueError, TypeError):
                continue
        else:
            url = urljoin(SITE, src)
            try:
                content = get(session, url).content
            except FetchError:
                continue
            ext = os.path.splitext(urlparse(url).path)[1].lower() or ".jpg"
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


def fetch_detail(session, item, raw_dir):
    r = get(session, item["url"])
    soup = BeautifulSoup(r.text, "html.parser")
    art = soup.find("article", id="bo_v")
    if art is None:
        raise FetchError("article#bo_v 없음 (응답 이상)")
    os.makedirs(os.path.join(raw_dir, "detail"), exist_ok=True)
    with gzip.open(os.path.join(raw_dir, "detail", f"{item['post_id']}.html.gz"), "wt", encoding="utf-8") as f:
        f.write(r.text)

    info = art.find("section", id="bo_v_info")
    info_text = info.get_text(" ", strip=True) if info else ""
    wm = re.search(r"작성자\s*:\s*(\S+)", info_text)
    dm = re.search(r"작성일\s*:\s*([\d.\s:]+)", info_text)
    pub = norm_date(dm.group(1)) if dm else ""

    conts = art.find("div", id="bo_v_con")
    body = clean(conts.get_text("\n", strip=True)) if conts else ""
    fail_code = ""
    if len(body) >= 5:
        status, text, source = "ok", body, "body"
    else:
        status, text, source = "no_body", "", "none"
        if conts is not None and conts.find("img", src=True):
            ocr, total, downloaded = image_srcs_and_ocr(
                session, conts, item["post_id"], os.path.join(raw_dir, "images"))
            if ocr:
                status, text, source = "image_ocr", ocr, "ocr"
            else:
                status = "image_only_ocr_empty"
                fail_code = ("image_no_src" if total == 0 else
                             "image_download_failed" if downloaded == 0 else "image_ocr_empty")
    return {
        **item,
        "writer": wm.group(1) if wm else item.get("writer", ""),
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

    def work(it):
        time.sleep(random.uniform(0.5, 1.0))
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
            time.sleep(60)
        for it in failed:
            out.write(json.dumps({**it, "pub_date": "", "date_mismatch": False, "text": "",
                                  "text_source": "none", "status": "fetch_failed", "fail_code": "",
                                  "fetched_at": datetime.now().isoformat(timespec="seconds")},
                                 ensure_ascii=False) + "\n")
    return len(failed)


def old_text_ids(path):
    csv.field_size_limit(sys.maxsize)
    ok = set()
    with open(path, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            m = re.search(r"/press/(\d+)", r.get("url") or "")
            if not m:
                continue
            t = (r.get("text") or "").strip()
            if t and t not in ("nan", "본문 조회 실패"):
                ok.add(int(m.group(1)))
    return ok


def write_csv(out_dir, rows, name):
    cols = ["post_id", "board_name", "title", "category", "date", "pub_date", "date_mismatch",
            "writer", "text", "text_source", "status", "fail_reason", "url", "fetched_at"]
    path = os.path.join(out_dir, name)
    last = {}
    for r in rows:
        last[r["post_id"]] = r
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, quoting=csv.QUOTE_ALL, extrasaction="ignore")
        w.writeheader()
        for r in last.values():
            r = dict(r)
            r["board_name"] = BOARD_NAME
            r["fail_reason"] = reason_text(r.get("status", ""), r.get("fail_code", ""))
            w.writerow(r)
    return path, len(last)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--no-details", action="store_true")
    ap.add_argument("--exclude-csv", default="", help="기존 CSV: 본문 있는 항목은 건너뛰고 나머지는 다시 받음")
    args = ap.parse_args(argv)

    def log(msg):
        print(msg, file=sys.stderr, flush=True)

    out_dir = os.path.join(args.out, "basicincome")
    raw_dir = os.path.join(out_dir, "raw")
    os.makedirs(out_dir, exist_ok=True)
    session = make_session()

    log("[기본소득당] 목록 수집")
    items, failed_pages = walk_list(session, raw_dir, log=log)
    with open(os.path.join(out_dir, "list.json"), "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False)
    if args.no_details:
        return 0

    if args.exclude_csv:
        keep = old_text_ids(args.exclude_csv)
        before = len(items)
        items = [it for it in items if it["post_id"] not in keep]
        log(f"  기존 CSV 본문 있는 {len(keep)}건 제외: {before}건 → {len(items)}건 남음")

    failed = run_details(session, items, out_dir, raw_dir, log=log)
    rows = load_jsonl(os.path.join(out_dir, "details.jsonl"))
    path, n = write_csv(out_dir, rows, "기본소득당보도자료.csv")
    statuses = {}
    for r in rows:
        statuses[r.get("status")] = statuses.get(r.get("status"), 0) + 1
    summary = {"board": BOARD_NAME, "list_count": len(items), "rows": n,
               "list_failed_pages": failed_pages, "fetch_failed": failed,
               "status_counts": statuses,
               "generated_at": datetime.now().isoformat(timespec="seconds")}
    with open(os.path.join(out_dir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    log(f"✓ 저장 {path} ({n}행) 상태: {statuses}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
