#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
더불어민주당 보도자료 수집기 (v2)

수정 사항 (docs/diagnosis.md 참고)
- 목록 날짜: <time datetime> 속성은 전부 2023-02-14로 고정되어 있어 쓰지 않는다.
  화면에 보이는 날짜 텍스트를 쓴다.
- 쿠키: 사이트가 CYBER 쿠키를 심고 307로 되돌린다. requests.Session으로 쿠키를 유지한다.
- 본문: 본문이 비어 있으면 첨부파일(PDF)을 받아 pdftotext로 텍스트를 뽑는다.
  (공란 게시물 대부분이 첨부 PDF에만 내용이 있다.)
- 실패 기록: 요청 실패는 재시도 큐에 넣고, 끝까지 실패한 항목은 status=fetch_failed로 남긴다.
  조용히 버리지 않는다.
- 재개: 목록과 상세 결과를 JSONL로 체크포인트에 쌓고, 다시 실행하면 이어서 받는다.

사용
  python crawl_dp.py --out ./out --board 188            # 보도자료 (brd=188)
  python crawl_dp.py --out ./out --board 188 --limit-pages 3 --no-details   # 목록만 시험
  python crawl_dp.py --out ./out --board 188 --sample-ids 1196283,1224463  # 특정 글 상세만

게시판 번호: 보도자료 188, 논평·브리핑 11, 모두발언 230, 정책브리핑 13 (사이트 메뉴 기준)
"""

import argparse
import concurrent.futures
import glob
import gzip
import json
import os
import random
import re
import subprocess
import sys
import time
from datetime import datetime
from urllib.parse import urljoin, urlparse, parse_qs

import requests
from bs4 import BeautifulSoup

SITE = "https://theminjoo.kr"
LIST_URL = SITE + "/main/sub/news/list.php"
VIEW_URL = SITE + "/main/sub/news/view.php"

BOARDS = {
    "보도자료": 188,
    "논평_브리핑": 11,
    "모두발언": 230,
    "정책브리핑": 13,
}
BOARD_BY_ID = {v: k for k, v in BOARDS.items()}

TIMEOUT = (10, 60)  # (연결, 읽기) 제한 시간 — 느린 응답이 한 항목을 오래 붙잡지 않도록
MAX_RETRIES = 3
WORKERS = 3  # 동시 요청 수 (사이트 부담을 줄이려고 낮게 유지)
ZERO_WIDTH = re.compile(r"[﻿​‌‍\xa0]")
DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
    "Accept-Language": "ko-KR,ko;q=0.9",
}


class FetchError(RuntimeError):
    pass


# 수집 불가 사유 (CSV의 fail_reason 컬럼). 수집에 성공한 행은 빈 값이다.
# 상세 기록(details.jsonl)에는 fail_code(세부 코드)를 함께 남긴다.
SUCCESS_STATUS = {"ok", "attachment_ok", "image_ocr"}
REASON_TEXT = {
    "site_denied": "사이트 접근 거부: 첨부 요청에 '잘못된 접근' 경고가 돌아옴",
    "no_body": "본문·첨부·이미지 모두 없음 (빈 게시물)",
    "image_no_src": "본문 이미지 주소(src) 없음",
    "image_download_failed": "본문 이미지 다운로드 실패 (외부 서버 오류 등)",
    "image_ocr_empty": "이미지는 받았으나 OCR 텍스트 없음 (글자 없는 사진·도표 등)",
    "image_unknown": "이미지 상태 미기록 (backfill 필요)",
    "extract_failed": "첨부 텍스트 추출 실패 (스캔 PDF 등 추정)",
    "fetch_failed": "상세 페이지 요청 실패 (재시도 후에도 실패)",
}


def image_fail_code(total, downloaded):
    """본문 이미지 상태 → 세부 코드."""
    if total == 0:
        return "image_no_src"
    if downloaded == 0:
        return "image_download_failed"
    return "image_ocr_empty"


def reason_text(status, fail_code=""):
    """status/fail_code → 사람이 읽는 수집 불가 사유. 성공이면 빈 문자열."""
    if status in SUCCESS_STATUS:
        return ""
    code = fail_code or status
    if code.startswith("attachment_unsupported:"):
        return "미지원 첨부 형식 (" + code.split(":", 1)[1] + ")"
    if code.startswith("extract_failed"):
        return REASON_TEXT["extract_failed"]
    if code == "image_only_ocr_empty":
        return REASON_TEXT["image_unknown"]
    return REASON_TEXT.get(code, "기타: " + str(status))


def make_session():
    s = requests.Session()  # 쿠키 유지 (CYBER 쿠키 리다이렉트 통과)
    s.headers.update(HEADERS)
    return s


def get(session, url, **kw):
    """재시도 포함 GET. 최종 실패는 FetchError."""
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


def list_url(brd, sno):
    if sno == 0:
        return f"{LIST_URL}?brd={brd}"
    return f"{LIST_URL}?sno={sno}&par=&&brd={brd}"


def parse_list(html, brd):
    """목록 한 페이지의 항목. 빈 목록이면 빈 리스트."""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for item in soup.find_all("div", {"class": "board-item"}):
        a = item.find("a", href=True)
        if not a:
            continue
        m = re.search(r"post=(\d+)", a["href"])
        if not m:
            continue
        post = int(m.group(1))
        cat_el = item.find("p", {"class": "category"})
        cat = cat_el.get_text(strip=True) if cat_el else ""
        title_el = a.find("span")
        title = (title_el or a).get_text(" ", strip=True)
        time_el = item.find("time")
        shown = time_el.get_text(strip=True) if time_el else ""  # 화면 날짜 (속성 datetime은 쓰지 않음)
        m2 = DATE_RE.search(shown)
        date = "-".join(m2.groups()) if m2 else ""
        out.append({
            "post_id": post,
            "brd": brd,
            "title": title,
            "category": cat,
            "date": date,
            "date_raw": shown,
            "url": f"{VIEW_URL}?brd={brd}&post={post}",
        })
    return out


def walk_list(session, brd, raw_dir, limit_pages=None, log=print):
    """sno=0,20,40... 를 빈 페이지 2회 연속까지 순회. 게시물 목록을 반환."""
    items, seen = [], set()
    sno, empty_run, pages = 0, 0, 0
    os.makedirs(raw_dir, exist_ok=True)
    raw_path = os.path.join(raw_dir, f"list_brd{brd}.jsonl")
    with open(raw_path, "w", encoding="utf-8") as rawf:
        while True:
            if limit_pages is not None and pages >= limit_pages:
                break
            time.sleep(random.uniform(0.3, 0.8))
            r = get(session, list_url(brd, sno))
            got = parse_list(r.text, brd)
            rawf.write(json.dumps({"sno": sno, "n": len(got), "post_ids": [g["post_id"] for g in got]}, ensure_ascii=False) + "\n")
            pages += 1
            if not got:
                empty_run += 1
                if empty_run >= 2:
                    break
            else:
                empty_run = 0
                for g in got:
                    if g["post_id"] in seen:
                        continue
                    seen.add(g["post_id"])
                    items.append(g)
            sno += 20
    log(f"  목록 {pages}페이지, 고유 게시물 {len(items)}건 (brd={brd})")
    return items


def extract_body(soup):
    """본문 텍스트. 공란이면 빈 문자열."""
    c = soup.find("div", {"class": "board-view__contents"})
    if c is None:
        return None  # 컨테이너 자체가 없음 (응답 이상 → 재시도 대상)
    for tag in c.find_all(["script", "style"]):
        tag.decompose()
    raw = c.get_text("\n")
    lines = [ZERO_WIDTH.sub("", ln).strip() for ln in raw.split("\n")]
    lines = [ln for ln in lines if ln]
    return "\n".join(lines)


def extract_pub_date(soup):
    li = soup.find("li", {"class": "date"})
    if not li:
        return ""
    m = DATE_RE.search(li.get_text(" ", strip=True))
    return "-".join(m.groups()) if m else ""


def attachment_links(soup):
    out = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "download.php" in href:
            name = a.get_text(strip=True)
            out.append((urljoin(SITE + "/main/sub/", href), name))
    return out


def clean_extracted(t):
    t = ZERO_WIDTH.sub("", t)
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n\s*\n+", "\n", t)
    return t.strip()


def extract_text_from_file(path, ext):
    """확장자별 텍스트 추출. 지원하지 않으면 None."""
    try:
        if ext == ".pdf":
            res = subprocess.run(["pdftotext", path, "-"], capture_output=True, timeout=120)
            return clean_extracted(res.stdout.decode("utf-8", errors="ignore"))
        if ext == ".hwp":
            res = subprocess.run(["hwp5txt", path], capture_output=True, timeout=120)
            return clean_extracted(res.stdout.decode("utf-8", errors="ignore"))
    except (OSError, subprocess.SubprocessError):
        return ""
    return None


def fetch_attachment_text(session, links, att_dir, post_id):
    """첨부의 본문 텍스트 추출 (PDF, HWP). 반환: (text, names, status)"""
    names, texts, unsupported, failed, denied = [], [], [], [], []
    for url, name in links:
        names.append(name)
        ext = os.path.splitext(name)[1].lower()
        if ext not in (".pdf", ".hwp"):
            unsupported.append(ext or "no-ext")
            continue
        r = get(session, url)
        os.makedirs(att_dir, exist_ok=True)
        path = os.path.join(att_dir, f"{post_id}_{len(names)}{ext}")
        with open(path, "wb") as f:
            f.write(r.content)
        if r.content[:200].lstrip().startswith(b"<script>alert"):
            # 사이트가 파일 대신 '잘못된 접근' 경고를 돌려준 경우 (게시물 권한 문제로 추정)
            denied.append(name)
            continue
        t = extract_text_from_file(path, ext)
        if t:
            texts.append(t)
        else:
            failed.append(name)
    text = "\n".join(texts)
    if text:
        return text, names, "attachment_ok"
    if failed:
        return "", names, "extract_failed:" + ",".join(failed)[:200]
    if denied:
        return "", names, "site_denied"
    if unsupported:
        return "", names, "attachment_unsupported:" + ",".join(sorted(set(unsupported)))
    return "", names, "no_body"


def image_srcs(soup):
    """본문 이미지의 절대 URL 목록 (data: 링크 제외)."""
    c = soup.find("div", {"class": "board-view__contents"})
    if c is None:
        return []
    return [urljoin(SITE, img["src"]) for img in c.find_all("img", src=True)
            if not img["src"].startswith("data:")]


def ocr_body_images(session, soup, post_id, img_dir, max_images=30):
    """본문 이미지를 받아 tesseract(kor+eng)로 OCR.
    반환: (텍스트, 이미지 주소 수, 다운로드 성공 수). 텍스트가 없으면 빈 문자열."""
    srcs = image_srcs(soup)
    texts, downloaded = [], 0
    if not srcs:
        return "", 0, 0
    os.makedirs(img_dir, exist_ok=True)
    for n, url in enumerate(srcs[:max_images], 1):
        # 이미지 하나가 외부 서버 오류(503 등)로 실패해도 게시물 전체를 실패시키지 않는다.
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
            t = clean_extracted(res.stdout.decode("utf-8", errors="ignore"))
        except (OSError, subprocess.SubprocessError):
            t = ""
        if t:
            texts.append(t)
    return "\n".join(texts), len(srcs), downloaded


def backfill_fail_code(row, raw_dir):
    """이전에 저장된 행(fail_code 없음)의 이미지 상태를 원본 HTML과 raw/images로 다시 판정한다."""
    if row.get("fail_code") or row.get("status") != "image_only_ocr_empty":
        return row.get("fail_code", "")
    gz = os.path.join(raw_dir, "detail", f"{row['brd']}_{row['post_id']}.html.gz")
    if not os.path.exists(gz):
        return "image_unknown"
    with gzip.open(gz, "rt", encoding="utf-8") as f:
        soup = BeautifulSoup(f.read(), "html.parser")
    srcs = image_srcs(soup)
    total = len(srcs)
    downloaded = 0
    img_dir = os.path.join(raw_dir, "images")
    for n in range(1, min(total, 30) + 1):  # ocr_body_images와 같은 파일 이름 규칙
        if glob.glob(os.path.join(img_dir, f"{row['post_id']}_{n}.*")):
            downloaded += 1
    return image_fail_code(total, downloaded)


def extract_writer(text):
    if not text:
        return ""
    lines = [ln for ln in text.split("\n") if ln.strip()]
    for ln in reversed(lines[-6:]):
        if re.search(r"민주당|대변인|공보|위원장|위원회", ln):
            return ln.strip()
    return ""


def fetch_detail(session, item, raw_dir, att_dir):
    """상세 한 건. 실패 시 예외(호출자가 재시도 큐 처리)."""
    r = get(session, item["url"])
    soup = BeautifulSoup(r.text, "html.parser")
    body = extract_body(soup)
    if body is None:
        raise FetchError("본문 컨테이너 없음 (응답 이상)")
    # 원본 보존 (gzip)
    os.makedirs(os.path.join(raw_dir, "detail"), exist_ok=True)
    with gzip.open(os.path.join(raw_dir, "detail", f"{item['brd']}_{item['post_id']}.html.gz"), "wt", encoding="utf-8") as f:
        f.write(r.text)

    pub = extract_pub_date(soup)
    fail_code = ""
    if len(body) >= 5:
        status, names, text, source = "ok", [], body, "body"
    else:
        links = attachment_links(soup)
        text, names, status = fetch_attachment_text(session, links, att_dir, item["post_id"])
        source = "attachment" if text else "none"
        if status == "no_body":
            c = soup.find("div", {"class": "board-view__contents"})
            if c is not None and c.find("img"):
                ocr, total, downloaded = ocr_body_images(
                    session, soup, item["post_id"], os.path.join(raw_dir, "images"))
                if ocr:
                    text, source, status = ocr, "ocr", "image_ocr"
                else:
                    status = "image_only_ocr_empty"
                    fail_code = image_fail_code(total, downloaded)
    return {
        **item,
        "pub_date": pub,
        "date_mismatch": bool(pub and item["date"] and pub != item["date"]),
        "writer": extract_writer(text),
        "text": text,
        "text_source": source,
        "status": status,
        "fail_code": fail_code,
        "attachments": "|".join(names),
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
    }


def run_details(session, items, out_dir, raw_dir, log=print):
    """체크포인트(JSONL) 기반 상세 수집. 재실행 시 완료 건 건너뜀. 실패는 재시도 큐 2라운드."""
    ck = os.path.join(out_dir, "details.jsonl")
    done = set()
    if os.path.exists(ck):
        with open(ck, encoding="utf-8") as f:
            for line in f:
                try:
                    done.add(json.loads(line)["post_id"])
                except (ValueError, KeyError):
                    pass
    att_dir = os.path.join(raw_dir, "attachments")
    todo = [it for it in items if it["post_id"] not in done]
    log(f"  상세 대상 {len(todo)}건 (완료 {len(done)}건 건너뜀)")

    def work(it):
        time.sleep(random.uniform(0.3, 0.8))  # 요청 간격 (워커별)
        try:
            return it, fetch_detail(session, it, raw_dir, att_dir), None
        except (FetchError, requests.RequestException, OSError) as e:
            return it, None, e

    failed = []
    with open(ck, "a", encoding="utf-8") as out, \
            concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for rnd in range(3):
            queue = todo if rnd == 0 else failed
            failed = []
            futures = [pool.submit(work, it) for it in queue]
            for i, fut in enumerate(concurrent.futures.as_completed(futures), 1):
                it, rec, err = fut.result()
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

    # 끝까지 실패한 항목도 기록으로 남긴다 (조용한 누락 방지)
    with open(ck, "a", encoding="utf-8") as out:
        for it in failed:
            out.write(json.dumps({**it, "status": "fetch_failed", "text": "", "writer": "",
                                  "text_source": "none", "attachments": "", "pub_date": "",
                                  "date_mismatch": False,
                                  "fetched_at": datetime.now().isoformat(timespec="seconds")},
                                 ensure_ascii=False) + "\n")
    return len(failed)


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


def write_csv(out_dir, rows, name):
    import csv
    cols = ["post_id", "board_name", "title", "category", "date", "pub_date", "date_mismatch",
            "writer", "text", "text_source", "status", "fail_reason", "attachments", "url", "fetched_at"]
    path = os.path.join(out_dir, name)
    # 같은 post_id가 여러 번 있으면 마지막 기록을 사용
    last = {}
    for r in rows:
        last[r["post_id"]] = r
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, quoting=csv.QUOTE_ALL, extrasaction="ignore")
        w.writeheader()
        for r in last.values():
            r = dict(r)
            r["board_name"] = BOARD_BY_ID.get(r.get("brd"), "")
            r["fail_reason"] = reason_text(r.get("status", ""), r.get("fail_code", ""))
            w.writerow(r)
    return path, len(last)


def ocr_existing(session, out_dir, raw_dir, log=print):
    """이미 수집된 image_only 행을 저장된 원본 HTML로 다시 보고 OCR 결과로 갱신한다."""
    ck = os.path.join(out_dir, "details.jsonl")
    rows = load_jsonl(ck)
    changed = 0
    for r in rows:
        if r.get("status") not in ("image_only", "image_only_ocr_empty"):
            continue
        gz = os.path.join(raw_dir, "detail", f"{r['brd']}_{r['post_id']}.html.gz")
        if not os.path.exists(gz):
            continue
        with gzip.open(gz, "rt", encoding="utf-8") as f:
            soup = BeautifulSoup(f.read(), "html.parser")
        time.sleep(random.uniform(0.3, 0.8))
        ocr, total, downloaded = ocr_body_images(session, soup, r["post_id"], os.path.join(raw_dir, "images"))
        if ocr:
            r["text"], r["text_source"], r["status"] = ocr, "ocr", "image_ocr"
            r["fail_code"] = ""
        else:
            r["status"] = "image_only_ocr_empty"
            r["fail_code"] = image_fail_code(total, downloaded)
        r["writer"] = extract_writer(r["text"])
        changed += 1
        if changed % 20 == 0:
            log(f"    OCR {changed}건 완료")
    tmp = ck + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, ck)
    log(f"  image_only OCR 갱신 {changed}건")
    return changed


EMPTY_TEXT = {"", "nan", "본문 조회 실패"}


def old_post_ids(path, board_name):
    """기존 CSV의 같은 게시판에서 (본문 있는 post_id 집합, 본문 빈 post_id 집합)을 돌려준다."""
    import csv as _csv
    _csv.field_size_limit(sys.maxsize)
    with_text, empty = set(), set()
    with open(path, encoding="utf-8-sig", newline="") as f:
        for r in _csv.DictReader(f):
            if r.get("board_name") != board_name:
                continue
            m = re.search(r"post=(\d+)", r.get("url") or "")
            if not m:
                continue
            (empty if (r.get("text") or "").strip() in EMPTY_TEXT else with_text).add(int(m.group(1)))
    return with_text, empty - with_text


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
    ap.add_argument("--board", type=int, required=True, help="brd 번호 (188=보도자료, 11=논평·브리핑, 230=모두발언, 13=정책브리핑)")
    ap.add_argument("--limit-pages", type=int, default=None, help="목록 페이지 수 제한 (시험용)")
    ap.add_argument("--no-details", action="store_true", help="목록만 수집")
    ap.add_argument("--since", type=ymd, default=None, help="이 날짜(포함) 이후 글만 수집 (YYYY-MM-DD)")
    ap.add_argument("--until", type=ymd, default=None, help="이 날짜(포함) 이전 글만 수집 (YYYY-MM-DD)")
    ap.add_argument("--sample-ids", default="", help="쉼표로 구분한 post_id만 상세 수집 (시험용)")
    ap.add_argument("--reuse-list", action="store_true", help="이전 list.json이 있으면 목록 재수집 생략")
    ap.add_argument("--exclude-csv", default="", help="이 CSV에 본문이 있는 같은 게시판 post_id는 건너뜀 (델타 수집, 본문이 비었던 글은 다시 받음)")
    ap.add_argument("--refetch-empty", default="", help="이 CSV에서 본문이 비어 있던 같은 게시판 post_id만 다시 수집")
    ap.add_argument("--ocr-existing", action="store_true", help="기존 image_only 행을 OCR로 갱신하고 CSV를 다시 씀")
    ap.add_argument("--backfill-reasons", action="store_true",
                    help="--from-dir의 기존 수집분에 fail_code를 채워 --out에 새 details.jsonl/CSV를 씀 (원본 raw는 읽기만)")
    ap.add_argument("--from-dir", default="", help="--backfill-reasons의 원본 출력 루트 (brd{N}_{이름} 폴더가 들어 있는 곳)")
    args = ap.parse_args(argv)

    if args.board not in BOARD_BY_ID:
        raise SystemExit(f"알 수 없는 brd: {args.board}")
    name = BOARD_BY_ID[args.board]
    out_dir = os.path.join(args.out, f"brd{args.board}_{name}")
    raw_dir = os.path.join(out_dir, "raw")
    os.makedirs(out_dir, exist_ok=True)

    def log(msg):
        print(msg, file=sys.stderr, flush=True)

    if args.backfill_reasons:
        if not args.from_dir:
            raise SystemExit("--backfill-reasons에는 --from-dir이 필요합니다.")
        src_dir = os.path.join(args.from_dir, f"brd{args.board}_{name}")
        src_raw = os.path.join(src_dir, "raw")  # 원본은 읽기만 한다
        rows = load_jsonl(os.path.join(src_dir, "details.jsonl"))
        for r in rows:
            if r.get("status") == "image_only_ocr_empty" and not r.get("fail_code"):
                r["fail_code"] = backfill_fail_code(r, src_raw)
        with open(os.path.join(out_dir, "details.jsonl"), "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        path, n = write_csv(out_dir, rows, f"{name}.csv")
        log(f"✓ 사유 백필 저장 {path} ({n}행)")
        return 0

    session = make_session()
    session.get(LIST_URL + f"?brd={args.board}", timeout=TIMEOUT)  # CYBER 쿠키 확보

    if args.ocr_existing:
        ocr_existing(session, out_dir, raw_dir, log=log)
        rows = load_jsonl(os.path.join(out_dir, "details.jsonl"))
        path, n = write_csv(out_dir, rows, f"{name}.csv")
        log(f"✓ 저장 {path} ({n}행)")
        return 0

    list_path = os.path.join(out_dir, "list.json")
    if args.reuse_list and os.path.exists(list_path):
        with open(list_path, encoding="utf-8") as f:
            items = json.load(f)
        log(f"  목록 재사용: {len(items)}건 ({list_path})")
    else:
        items = walk_list(session, args.board, raw_dir, limit_pages=args.limit_pages, log=log)
        with open(list_path, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False)

    items = filter_period(items, args.since, args.until, log=log)
    if args.no_details:
        log("목록만 저장했습니다.")
        return 0

    if args.sample_ids:
        keep = {int(x) for x in args.sample_ids.split(",") if x.strip()}
        items = [it for it in items if it["post_id"] in keep]

    if args.exclude_csv or args.refetch_empty:
        # 기존 CSV 기준. --exclude-csv: 본문이 있는 post_id만 건너뛴다 (본문이 비었던 글은 다시 받음).
        # --refetch-empty: 기존 CSV에서 본문이 비어 있던 post_id만 다시 받는다.
        with_text, empty = old_post_ids(args.exclude_csv or args.refetch_empty, name)
        before = len(items)
        if args.refetch_empty:
            items = [it for it in items if it["post_id"] in empty]
            log(f"  기존 빈 본문 재수집: 목록 {before}건 중 {len(items)}건 (기존 빈 본문 {len(empty)}건)")
        else:
            items = [it for it in items if it["post_id"] not in with_text]
            log(f"  기존 CSV 본문 있는 {len(with_text)}건 제외: {before}건 → {len(items)}건 남음")

    failed = run_details(session, items, out_dir, raw_dir, log=log)
    rows = load_jsonl(os.path.join(out_dir, "details.jsonl"))
    path, n = write_csv(out_dir, rows, f"{name}.csv")
    statuses = {}
    for r in rows:
        statuses[r.get("status")] = statuses.get(r.get("status"), 0) + 1
    summary = {"board": name, "brd": args.board, "list_count": len(items), "rows": n,
               "fetch_failed": failed, "status_counts": statuses,
               "generated_at": datetime.now().isoformat(timespec="seconds")}
    with open(os.path.join(out_dir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    log(f"✓ 저장 {path} ({n}행) 상태: {statuses}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
