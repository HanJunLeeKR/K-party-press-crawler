#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
진보당(jinboparty.com) 보도자료·논평·모두발언 수집기

사이트 구조
- 게시판(p, b): 보도자료 (286, b_1_111, 12건/쪽) / 논평 (15, b_1_2, 20건/쪽) / 모두발언 (315, b_1_125, 20건/쪽)
- 목록: /pages/index.php?nPage=N&p=..&b=..&cate=&f=&s=  항목은 javascript:js_board_view('{id}')
  범위를 넘는 nPage는 마지막 쪽을 되풀이하므로, 새 ID가 하나도 없는 쪽이 나오면 종료한다.
- 상세: /pages/?p=..&b=..&m=read&bn={id}. 제목 div.table_view_h p, 작성자·등록일 ul.info_list, 본문 div.content_box.
- 간헐적 연결 재설정이 있어 재시도(간격 증가)와 라운드 재큐잉을 쓴다.

사용
  python crawl_jinbo.py --out ./out
  python crawl_jinbo.py --out ./out --exclude-csv 기존.csv
"""
import argparse, base64, concurrent.futures, csv, gzip, json, os, random, re, subprocess, sys, time
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

SITE = "https://jinboparty.com"
BOARDS = {"보도자료": (286, "b_1_111"), "논평": (15, "b_1_2"), "모두발언": (315, "b_1_125")}
TIMEOUT = (10, 60)
MAX_RETRIES = 4
WORKERS = 3
ZERO_WIDTH = re.compile(r"[\u200b\u200c\u200d\ufeff\xa0]")
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)", "Accept-Language": "ko-KR,ko;q=0.9"}
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
            r.encoding = "utf-8"  # 서버가 charset을 알리지 않아 requests가 ISO-8859-1로 해석함
            return r
        except requests.RequestException as e:
            last = e
            time.sleep(3 * attempt)
    raise FetchError(f"GET 실패 {url}: {last}")


def reason_text(status, fail_code=""):
    if status in SUCCESS_STATUS:
        return ""
    return REASON_TEXT.get(fail_code or status, "기타: " + str(status))


def clean(t):
    return ZERO_WIDTH.sub("", t).strip()


def norm_date(s):
    m = re.search(r"(\d{4})[-.]\s*(\d{1,2})[-.]\s*(\d{1,2})", s or "")
    return f"{int(m[1]):04d}-{int(m[2]):02d}-{int(m[3]):02d}" if m else ""


def list_url(board):
    return f"{SITE}/pages/index.php"


def read_url(board, pid):
    p, bid = BOARDS[board]
    return f"{SITE}/pages/?p={p}&b={bid}&m=read&bn={pid}"


def parse_list(html, board):
    """카드형(보도자료)과 표형(논평·모두발언) 목록을 모두 처리한다."""
    soup = BeautifulSoup(html, "html.parser")
    out, seen = [], set()
    for a in soup.find_all("a", href=re.compile(r"js_board_view\('(\d+)'\)")):
        pid = int(re.search(r"js_board_view\('(\d+)'\)", a["href"]).group(1))
        if pid in seen:
            continue
        seen.add(pid)
        row = a.find_parent("li") or a
        ti = row.select_one("p.title, ._tit")
        wr = row.select_one("p.edit_ellip_1")
        dt = re.search(r"\d{4}\.\d{2}\.\d{2}", row.get_text(" ", strip=True))
        out.append({"post_id": pid, "board_name": board, "url": read_url(board, pid),
                    "title": clean(ti.get_text(" ", strip=True)) if ti else "",
                    "category": board, "writer": clean(wr.get_text(" ", strip=True)) if wr else "",
                    "date": norm_date(dt.group(0)) if dt else ""})
    return out


def fetch_list_page(session, board, page, raw_dir):
    p, bid = BOARDS[board]
    d = os.path.join(raw_dir, "list", board)
    path = os.path.join(d, f"p{page}.html.gz")
    if os.path.exists(path):  # 이미 받은 목록 쪽은 재사용
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return parse_list(f.read(), board)
    time.sleep(random.uniform(0.3, 0.7))
    r = get(session, list_url(board), params={"nPage": page, "p": p, "b": bid, "cate": "", "f": "", "s": ""})
    os.makedirs(d, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as f:
        f.write(r.text)
    return parse_list(r.text, board)


def walk_list(session, raw_dir, log=print):
    """게시판별로 새 ID가 없는 쪽(범위 초과로 마지막 쪽이 반복됨)이 나올 때까지 순차 수집."""
    got, failed_pages = {}, []
    for board in BOARDS:
        seen, page, bad = set(), 1, 0
        while True:
            try:
                rows = fetch_list_page(session, board, page, raw_dir)
            except (FetchError, OSError):
                failed_pages.append((board, page))
                page += 1
                bad += 1
                if bad >= 5:
                    break
                continue
            bad = 0
            new = [r for r in rows if r["post_id"] not in seen]
            if not new:
                break
            for r in new:
                seen.add(r["post_id"])
                got[(board, r["post_id"])] = r
            page += 1
        log(f"  {board}: {page - 1}쪽, {len(seen)}건")
    items = sorted(got.values(), key=lambda x: (x["board_name"], x["post_id"]))
    log(f"  전체 {len(items)}건, 실패 쪽 {failed_pages}")
    return items, failed_pages


def ocr_images(session, conts, post_id, img_dir, max_images=30):
    srcs = [i["src"] for i in conts.find_all("img", src=True)]
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
            res = subprocess.run(["tesseract", path, "-", "-l", "kor+eng", "--psm", "3"], capture_output=True, timeout=180)
            t = clean(res.stdout.decode("utf-8", errors="ignore"))
        except (OSError, subprocess.SubprocessError):
            t = ""
        if t:
            texts.append(t)
    return "\n".join(texts), len(srcs), downloaded


def fetch_detail(session, item, raw_dir):
    cache = os.path.join(raw_dir, "detail", item["board_name"], f"{item['post_id']}.html.gz")
    if os.path.exists(cache):  # 이미 받은 원본 HTML은 재사용 (재파싱)
        with gzip.open(cache, "rt", encoding="utf-8") as f:
            html = f.read()
    else:
        html = get(session, item["url"]).text
    soup = BeautifulSoup(html, "html.parser")
    conts = soup.select_one("div.content_box")
    head = soup.select_one("div.table_view_h")
    if conts is None or head is None:
        raise FetchError("div.content_box/table_view_h 없음 (응답 이상)")
    if not os.path.exists(cache):
        os.makedirs(os.path.dirname(cache), exist_ok=True)
        with gzip.open(cache, "wt", encoding="utf-8") as f:
            f.write(html)
    meta = {}
    for li in head.select("ul.info_list > li"):
        k = li.find("b")
        if k:
            meta[k.get_text(strip=True)] = clean(li.get_text(" ", strip=True).replace(k.get_text(strip=True), "", 1))
    t = head.select_one("p")
    title = clean(t.get_text(" ", strip=True)) if t else item["title"]
    body = clean(conts.get_text("\n", strip=True))
    status, text, source, fail_code = "ok", body, "body", ""
    if len(body) < 5:
        status, text, source = "no_body", "", "none"
        if conts.find("img", src=True):
            ocr, total, dl = ocr_images(session, conts, f"{item['board_name']}_{item['post_id']}",
                                        os.path.join(raw_dir, "images"))
            if ocr:
                status, text, source = "image_ocr", ocr, "ocr"
            else:
                status = "image_only_ocr_empty"
                fail_code = "image_no_src" if total == 0 else "image_download_failed" if dl == 0 else "image_ocr_empty"
    pub = norm_date(meta.get("등록일", ""))
    return {**item, "title": title or item["title"], "writer": meta.get("작성자", ""),
            "pub_date": pub, "date_mismatch": bool(pub and item["date"] and pub != item["date"]),
            "text": text, "text_source": source, "status": status, "fail_code": fail_code,
            "fetched_at": datetime.now().isoformat(timespec="seconds")}


def load_jsonl(path):
    rows = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    pass
    return rows


def run_details(session, items, out_dir, raw_dir, log=print):
    ck = os.path.join(out_dir, "details.jsonl")
    done = {(r["board_name"], r["post_id"]) for r in load_jsonl(ck)}
    todo = [it for it in items if (it["board_name"], it["post_id"]) not in done]
    log(f"  상세 대상 {len(todo)}건 (완료 {len(done)}건 건너뜀)")

    def work(it):
        if not os.path.exists(os.path.join(raw_dir, "detail", it["board_name"], f"{it['post_id']}.html.gz")):
            time.sleep(random.uniform(0.4, 0.9))  # 캐시가 있으면 서버 요청이 없으므로 대기 불필요
        try:
            return it, fetch_detail(session, it, raw_dir), None
        except (FetchError, requests.RequestException, OSError) as e:
            return it, None, e

    failed = []
    with open(ck, "a", encoding="utf-8") as out, concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for rnd in range(3):
            queue = todo if rnd == 0 else failed
            failed = []
            for i, (it, rec, err) in enumerate(pool.map(work, queue), 1):
                if rec is None:
                    failed.append(it)
                    continue
                out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                out.flush()
                if i % 100 == 0:
                    log(f"    round {rnd}: {i}/{len(queue)}")
            if not failed:
                break
            log(f"  라운드 {rnd} 실패 {len(failed)}건 → 재시도")
            time.sleep(60)
        for it in failed:
            out.write(json.dumps({**it, "pub_date": "", "date_mismatch": False, "text": "", "text_source": "none",
                                  "status": "fetch_failed", "fail_code": "",
                                  "fetched_at": datetime.now().isoformat(timespec="seconds")}, ensure_ascii=False) + "\n")
    return len(failed)


def old_text_ids(path):
    csv.field_size_limit(sys.maxsize)
    ok = set()
    with open(path, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            m = re.search(r"bn=(\d+)", r.get("url") or "")
            if m and (r.get("text") or "").strip() not in ("", "nan"):
                ok.add(int(m.group(1)))
    return ok


def write_csv(out_dir, rows, name):
    cols = ["post_id", "board_name", "title", "category", "date", "pub_date", "date_mismatch", "writer",
            "text", "text_source", "status", "fail_reason", "url", "fetched_at"]
    path = os.path.join(out_dir, name)
    last = {(r["board_name"], r["post_id"]): r for r in rows}
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, quoting=csv.QUOTE_ALL, extrasaction="ignore")
        w.writeheader()
        for r in last.values():
            r = dict(r)
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
    ap.add_argument("--no-details", action="store_true")
    ap.add_argument("--since", type=ymd, default=None, help="이 날짜(포함) 이후 글만 수집 (YYYY-MM-DD)")
    ap.add_argument("--until", type=ymd, default=None, help="이 날짜(포함) 이전 글만 수집 (YYYY-MM-DD)")
    ap.add_argument("--exclude-csv", default="")
    args = ap.parse_args(argv)
    log = lambda m: print(m, file=sys.stderr, flush=True)

    out_dir = os.path.join(args.out, "jinbo")
    raw_dir = os.path.join(out_dir, "raw")
    os.makedirs(out_dir, exist_ok=True)
    session = make_session()
    log("[진보당] 목록 수집")
    items, failed_pages = walk_list(session, raw_dir, log=log)
    labels = {}
    for it in items:
        labels[it["board_name"]] = labels.get(it["board_name"], 0) + 1
    log(f"  게시판별: {labels}")
    with open(os.path.join(out_dir, "list.json"), "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False)
    items = filter_period(items, args.since, args.until, log=log)
    if args.no_details:
        return 0
    if args.exclude_csv:
        keep = old_text_ids(args.exclude_csv)
        items = [it for it in items if it["post_id"] not in keep]
    failed = run_details(session, items, out_dir, raw_dir, log=log)
    rows = load_jsonl(os.path.join(out_dir, "details.jsonl"))
    path, n = write_csv(out_dir, rows, "진보당보도자료.csv")
    st = {}
    for r in rows:
        st[r.get("status")] = st.get(r.get("status"), 0) + 1
    with open(os.path.join(out_dir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump({"boards": list(BOARDS), "label_counts": labels, "rows": n, "list_failed_pages": failed_pages,
                   "fetch_failed": failed, "status_counts": st,
                   "generated_at": datetime.now().isoformat(timespec="seconds")}, f, ensure_ascii=False, indent=2)
    log(f"✓ 저장 {path} ({n}행) 상태: {st}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
