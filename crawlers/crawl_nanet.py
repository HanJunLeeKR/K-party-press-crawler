#!/usr/bin/env python3
"""국회도서관 입법정보포털(NANET) 정당 보도자료 수집기 -> 정당별 CSV (프로젝트 공통 16열 스키마).

단계: (1) list  : 정당(PA) 목록 전체 수집 -> list_PA.jsonl  (로그인 불필요)
      (2) detail: 대상 정당만 본문 수집     -> details_nanet.jsonl
      (3) build : 정당별 CSV 생성            -> out/{정당}_보도자료.csv

혼입 방지: 키워드 검색을 쓰지 않는다. 정당 전체 목록을 받은 뒤 기관명(dpPub)이
--parties 와 '완전 일치'하는 것만 남긴다(예: '민주당' != '더불어민주당').
공동 발표(쉼표로 여러 기관)는 완전 일치하지 않으므로 자동 제외된다.

사용 예:
  export NANET_API_KEY=...        # 선택. 현재 웹 수집 방식은 키가 필요 없고, 키를 쓰는 API 경로 확장용 자리.
  python crawl_nanet.py all --parties 새누리당 자유한국당
  python crawl_nanet.py list ; python crawl_nanet.py detail --limit 1200 ; python crawl_nanet.py build
모든 단계는 재시작 가능(이미 받은 것은 건너뜀).
"""
import argparse, csv, collections, html, json, os, re, sys, time
import concurrent.futures as cf
from datetime import datetime
import requests
from bs4 import BeautifulSoup

BASE = "https://law.nanet.go.kr/policy/assemblyBodo"
API_KEY = os.environ.get("NANET_API_KEY", "")   # 필요 시 요청 헤더/파라미터에 연결
UA = {"User-Agent": "Mozilla/5.0"}
COLS = ("party,party_name_at_time,post_id,board_name,title,category,date,datetime,date_note,writer,text,url,"
        "text_source,status,fail_reason,fetched_at,source").split(",")
DEFAULT_PARTIES = ["새정치민주연합", "새누리당", "자유한국당", "미래통합당", "민주당"]
TRUNC = 3990  # og 메타 본문이 약 4,000자에서 잘리는 것으로 보여 이 이상이면 표시


def get(session, url, params, tries=4):
    err = None
    for a in range(tries):
        try:
            r = session.get(url, params=params, timeout=60,
                            headers={"X-API-KEY": API_KEY} if API_KEY else None)
            r.raise_for_status(); r.encoding = "utf-8"; return r
        except requests.RequestException as e:
            err = e; time.sleep(5 * (a + 1))
    raise err


def cmd_list(a):
    s = requests.Session(); s.headers.update(UA)
    done = set()
    if os.path.exists(a.list_file):
        done = {json.loads(l)["_page"] for l in open(a.list_file, encoding="utf-8")}
    p = 1
    with open(a.list_file, "a", encoding="utf-8") as f:
        while True:
            if p in done: p += 1; continue
            r = get(s, f"{BASE}/list.do", {"srchDsc": "ALL", "srchKeyword": "", "srchBodoGubun": "PA",
                    "listNum": 100, "pageNum": p, "displaySort": "pubDateDesc"})
            boxes = BeautifulSoup(r.text, "html.parser").select("input[name=checkItem]")
            if not boxes: break
            for c in boxes:
                g = lambda k: c.get(k.lower()) or c.get(k, "")
                f.write(json.dumps({"controlNo": g("controlNo"), "title": c.get("title", ""),
                        "pubDate": g("pubDate"), "dpPub": g("dpPub"), "dpSereNm": g("dpSereNm"),
                        "dataType": g("dataType"), "_page": p}, ensure_ascii=False) + "\n")
            f.flush(); print("page", p, flush=True); p += 1; time.sleep(a.delay)


def targets(a):
    recs = [json.loads(l) for l in open(a.list_file, encoding="utf-8")]
    return [r for r in recs if r["dpPub"] in a.parties]   # 완전 일치


def fetch(r):
    s = requests.Session(); s.headers.update(UA)
    try:
        x = get(s, f"{BASE}/detail.do", {"controlNo": r["controlNo"]})
    except requests.RequestException as e:
        return {**r, "status": "fetch_failed", "text": "", "fail_reason": str(e)[:120],
                "fetched_at": datetime.now().isoformat(timespec="seconds")}
    m = BeautifulSoup(x.text, "html.parser").find("meta", attrs={"property": ["og:desctiption", "og:description"]})
    body = html.unescape(m.get("content", "")) if m else ""
    body = re.sub(r"<br\s*/?>", "\n", body, flags=re.I)
    body = re.sub(r"<[^>]+>", "", body)
    body = "\n".join(l.strip() for l in body.splitlines() if l.strip())
    return {**r, "status": "ok" if len(body) > 20 else "no_body", "text": body, "text_source": "og_meta",
            "fail_reason": "첨부파일 참조" if "첨부파일 참조" in body else "",
            "fetched_at": datetime.now().isoformat(timespec="seconds")}


def cmd_detail(a):
    done = set()
    if os.path.exists(a.detail_file):
        done = {json.loads(l)["controlNo"] for l in open(a.detail_file, encoding="utf-8")}
    todo = [r for r in targets(a) if r["controlNo"] not in done][: a.limit]
    print("todo", len(todo), flush=True)
    with open(a.detail_file, "a", encoding="utf-8") as f, cf.ThreadPoolExecutor(a.workers) as ex:
        for i, rec in enumerate(ex.map(fetch, todo), 1):
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            if i % 200 == 0: f.flush(); print(i, flush=True)


def cmd_build(a):
    by = collections.defaultdict(list)
    for l in open(a.detail_file, encoding="utf-8"):
        r = json.loads(l)
        if r["dpPub"] not in a.parties: continue
        p = r["pubDate"]; fr = r.get("fail_reason", "")
        if len(r["text"]) >= TRUNC:
            fr = (fr + "; " if fr else "") + "본문 절단 가능(og_meta 길이 한계)"
        by[r["dpPub"]].append({"party": r["dpPub"], "party_name_at_time": r["dpPub"], "post_id": r["controlNo"],
            "board_name": "국회도서관 정당 보도자료", "title": r["title"], "category": r["dpSereNm"],
            "date": f"{p[:4]}-{p[4:6]}-{p[6:8]}", "datetime": "", "date_note": "NANET pubDate(발행일)",
            "writer": "", "text": r["text"],
            "url": f"{BASE}/detail.do?controlNo={r['controlNo']}", "text_source": "og_meta",
            "status": r["status"], "fail_reason": fr, "fetched_at": r.get("fetched_at", ""),
            "source": "nanet_" + datetime.now().strftime("%Y-%m-%d")})
    os.makedirs(a.out, exist_ok=True)
    for party, rows in by.items():
        rows.sort(key=lambda x: (x["date"], x["post_id"]))
        with open(os.path.join(a.out, f"{party}_보도자료.csv"), "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, COLS, quoting=csv.QUOTE_ALL, lineterminator="\n")
            w.writeheader(); w.writerows(rows)
        print(party, len(rows), rows[0]["date"], rows[-1]["date"])


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("step", choices=["list", "detail", "build", "all"])
    ap.add_argument("--parties", nargs="+", default=DEFAULT_PARTIES, help="기관명 완전 일치 대상")
    ap.add_argument("--list-file", dest="list_file", default="list_PA.jsonl")
    ap.add_argument("--detail-file", dest="detail_file", default="details_nanet.jsonl")
    ap.add_argument("--out", default="out")
    ap.add_argument("--limit", type=int, default=10**9)
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--delay", type=float, default=0.6)
    a = ap.parse_args()
    if a.step in ("list", "all"): cmd_list(a)
    if a.step in ("detail", "all"): cmd_detail(a)
    if a.step in ("build", "all"): cmd_build(a)
