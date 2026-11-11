#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
정당별 병합 파일 생성 (원자료는 건드리지 않고 새 폴더에 쓴다)

공통 컬럼
  party, post_id, board_name, title, category, date, datetime, date_note, writer, text, url,
  text_source, status, fail_reason, fetched_at, source

규칙
- date: YYYY-MM-DD. 시각이 있으면 datetime(YYYY-MM-DD HH:MM)에 남긴다 (현재 기본소득당 상세 HTML에만 시각이 있음).
- 더불어민주당 2003-11-11은 사이트 일괄값으로 보이므로 date는 원본 그대로 두고 date_note에 표시한다.
- 같은 글(게시물 ID 기준)이 기존 CSV와 새 수집분에 모두 있으면: 본문 있는 쪽 우선, 둘 다 있으면 새 수집분.
  선택되지 않은 쪽에서 빈 칸(작성자·분류·날짜·시각)을 채운다.
- 개혁신당은 전체 재수집(v3)이 기준이다. 기존 URL이 옛 형식이고 기존 날짜도 믿을 수 없어 제목으로 대응시키며, 날짜는 v3 값을 쓴다.
- 본문은 가공하지 않는다 (정제는 별도 단계).
- 구조가 깨진 기존 행은 병합하지 않고 rejects/ 에 따로 저장한다.

사용
  python merge_parties.py --work /home/claude/work --out ./merged
"""
import argparse, csv, glob, gzip, json, os, re, sys
from collections import defaultdict

import pandas as pd
from bs4 import BeautifulSoup

COLS = ["party", "post_id", "board_name", "title", "category", "date", "datetime", "date_note", "writer",
        "text", "url", "text_source", "status", "fail_reason", "fetched_at", "source"]
EMPTY_TEXT = {"", "nan", "본문 조회 실패"}
DATE_RE = re.compile(r"(\d{4})[.\-/]\s*(\d{1,2})[.\-/]\s*(\d{1,2})")
TIME_RE = re.compile(r"(?<!\d)([01]?\d|2[0-3]):([0-5]\d)(?!\d)")
PLACEHOLDER = "2003-11-11"
csv.field_size_limit(sys.maxsize)


def rd(path):
    return pd.read_csv(path, dtype=str, keep_default_na=False)


def norm_dt(s):
    """문자열 → (date, datetime). 시각이 없으면 datetime은 빈 문자열."""
    m = DATE_RE.search(s or "")
    if not m:
        return "", ""
    d = f"{int(m[1]):04d}-{int(m[2]):02d}-{int(m[3]):02d}"
    t = TIME_RE.search((s or "")[m.end():])
    return d, (f"{d} {int(t[1]):02d}:{t[2]}" if t else "")


def has_text(t):
    return (t or "").strip() not in EMPTY_TEXT


def frame(df, party, source, **cols):
    """원본 DataFrame을 공통 컬럼으로 맞춘다. cols는 {공통컬럼: 원본컬럼}."""
    out = pd.DataFrame({c: "" for c in COLS}, index=df.index)
    out["party"], out["source"] = party, source
    for dst in COLS:
        src = cols.get(dst, dst)
        if src in df.columns and dst not in ("party", "source"):
            out[dst] = df[src].fillna("")
    dd = out["date"].map(norm_dt)
    out["date"] = dd.map(lambda x: x[0])
    dt = dd.map(lambda x: x[1])
    out["datetime"] = out["datetime"].where(out["datetime"] != "", dt)
    return out.reset_index(drop=True)


def key_of(party, row):
    u = row["url"]
    if party == "더불어민주당":
        m = re.search(r"post=(\d+)", u)
        return m[1] if m else None
    if party == "국민의힘":
        m = re.search(r"comment_view/(BBSDD\d+)/(\d+)", u)
        return f"{m[1]}/{m[2]}" if m else None
    if party == "기본소득당":
        m = re.search(r"/press/(\d+)", u)
        return m[1] if m else None
    if party == "조국혁신당":
        m = re.search(r"rebuildingkoreaparty\.kr/news/([\w\-]+/\d+)", u)
        return m[1] if m else None
    return None


def assign_keys(df, party, is_old=False):
    if party == "개혁신당":  # (제목, n번째) — 옛 URL이 달라 URL로 대응 불가, 기존 파일의 날짜도 신뢰할 수 없음
        seen = defaultdict(int)
        keys = []
        for t in df["title"]:
            k = re.sub(r"\s+", "", t)
            # 기존 파일은 같은 글이 여러 번 들어 있어(제목 중복 175건) 제목당 한 건으로 본다
            keys.append(f"{k}|{0 if is_old else seen[k]}")
            seen[k] += 1
        return keys
    return [key_of(party, r) for _, r in df.iterrows()]


def pick(a, b):
    """같은 글의 두 행 중 하나를 고르고, 빈 칸은 다른 쪽에서 채운다. a=기존, b=새 수집."""
    ta, tb = has_text(a["text"]), has_text(b["text"])
    prim, sec = (b, a) if (tb or not ta) else (a, b)
    out = dict(prim)
    for c in ("category", "writer", "title", "date", "datetime", "post_id"):
        if not out.get(c) and sec.get(c):
            out[c] = sec[c]
    # 날짜·시각은 상세 페이지를 읽은 새 수집분이 더 믿을 만하다 (기존 개혁신당 날짜는 수집일 값이 섞여 있었음)
    for c in ("date", "datetime"):
        if b.get(c):
            out[c] = b[c]
    return out


def merge_two(party, old, new, log):
    old = old.copy()
    new = new.copy()
    old["_k"] = assign_keys(old, party, is_old=True)
    new["_k"] = assign_keys(new, party)
    for df in (old, new):
        df["_t"] = df["text"].map(has_text)
    # 같은 출처 안의 중복: 본문 있는 쪽, 같으면 뒤쪽
    def dedupe(df):
        keyed = df[df["_k"].notna()].sort_values("_t", kind="stable").drop_duplicates("_k", keep="last")
        return pd.concat([keyed, df[df["_k"].isna()]]).sort_index()
    n_old0, n_new0 = len(old), len(new)
    old, new = dedupe(old), dedupe(new)
    log["old_rows"], log["new_rows"] = n_old0, n_new0
    log["dup_within_old"], log["dup_within_new"] = n_old0 - len(old), n_new0 - len(new)
    newmap = {k: r for k, r in new.set_index("_k", drop=False).iterrows() if k is not None}
    rows, used = [], set()
    old_text_total = 0
    for _, r in old.iterrows():
        k = r["_k"]
        if k is not None and k in newmap:
            used.add(k)
            rows.append(pick(r.to_dict(), newmap[k].to_dict()))
        else:
            rows.append(r.to_dict())
    for k, r in newmap.items():
        if k not in used:
            rows.append(r.to_dict())
    for _, r in new[new["_k"].isna()].iterrows():
        rows.append(r.to_dict())
    out = pd.DataFrame(rows)
    log["matched"] = len(used)
    log["old_only"] = len(old) - len(used)
    log["new_only"] = len(new) - len(used)
    # 본문 유실 검사: 기존에 본문 있던 키가 최종에서 비면 안 된다
    final_has = {r["_k"]: has_text(r["text"]) for _, r in out.iterrows() if r["_k"] is not None}
    lost = [k for _, r in old.iterrows() if r["_t"] and (k := r["_k"]) is not None and not final_has.get(k, False)]
    log["text_lost_vs_old"] = len(lost)
    return out.drop(columns=["_k", "_t"], errors="ignore")


def bi_times(raw_dir):
    """기본소득당 상세 HTML의 '작성일 : 2022.01.18. 11:16 게시'에서 시각을 읽는다."""
    out = {}
    for f in glob.glob(os.path.join(raw_dir, "*.html.gz")):
        pid = os.path.basename(f).split(".")[0]
        t = gzip.open(f, "rt", encoding="utf-8", errors="ignore").read()
        sp = BeautifulSoup(t, "html.parser")
        info = sp.find("section", id="bo_v_info")
        s = info.get_text(" ", strip=True) if info else ""
        m = re.search(r"작성일\s*:\s*([\d.\s:]+)", s)
        if m:
            d, dt = norm_dt(m.group(1))
            if dt:
                out[pid] = dt
    return out


def finalize(df, party):
    df = df.copy()
    df["party"] = party
    df["date_note"] = ""
    if party == "더불어민주당":
        df.loc[df["date"] == PLACEHOLDER, "date_note"] = "사이트 일괄값 추정(2003-11-11)"
    df.loc[df["date"] == "", "date_note"] = "날짜 없음"
    df["_pid"] = pd.to_numeric(df["post_id"], errors="coerce")
    df = df.sort_values(["date", "board_name", "_pid"], kind="stable").drop(columns="_pid")
    return df[COLS]


def save(df, path):
    df.to_csv(path, index=False, encoding="utf-8-sig", quoting=csv.QUOTE_ALL)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", default="/home/claude/work")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    W = a.work
    os.makedirs(os.path.join(a.out, "rejects"), exist_ok=True)
    report = {}

    # ---------- 더불어민주당 ----------
    P = "더불어민주당"
    old = rd(f"{W}/data/더불어민주당보도자료_2003-11-11_2026-02-04_cleaned.csv")
    bad = old[~old["board_name"].isin(["논평_브리핑", "모두발언", "보도자료"])]
    save(bad, os.path.join(a.out, "rejects", "더불어민주당_구조깨진행.csv"))
    # 엑셀 셀 한도(32,767자)로 본문이 잘리며 URL이 다음 조각 행의 제목으로 밀려난 경우: 바로 앞 행의 URL로 복원
    repaired = []
    for i, r in bad.iterrows():
        if r["title"].startswith("http") and i - 1 in old.index and old.at[i - 1, "url"] == "":
            old.at[i - 1, "url"] = r["title"]
            repaired.append(i - 1)
    repaired_urls = set(old.loc[repaired, "url"])
    old = old.drop(bad.index)
    delta = pd.concat([rd(f) for f in sorted(glob.glob(f"{W}/step3/hf_final/v2_delta/*.csv"))], ignore_index=True)
    log = {"rejected_malformed_old_rows": len(bad), "old_rows_blank_url": int((old["url"] == "").sum())}
    o = frame(old, P, "기존 CSV", date="published_date")
    o.loc[o["url"].isin(repaired_urls), "source"] = "기존 CSV(URL 복원, 본문 32,767자에서 잘림)"
    log["url_repaired_rows"] = len(repaired)
    n = frame(delta, P, "v2_delta")
    o["post_id"] = [key_of(P, r) or "" for _, r in o.iterrows()]
    m = merge_two(P, o, n, log)
    m = finalize(m, P)
    save(m, os.path.join(a.out, "더불어민주당_병합.csv"))
    log["final_rows"] = len(m)
    log["date_placeholder_rows"] = int((m["date_note"] != "").sum())
    report[P] = log

    # ---------- 국민의힘 ----------
    P = "국민의힘"
    old = rd(f"{W}/data/국민의힘보도자료_2000-03-10_2026-02-03_cleaned.csv")
    new = rd(f"{W}/step4/ppp_final/국민의힘_델타_2026-11-11.csv")
    log = {}
    o = frame(old, P, "기존 CSV", category="section")
    nn = frame(new, P, "v4_ppp", category="section")
    o["post_id"] = [(key_of(P, r) or "").split("/")[-1] for _, r in o.iterrows()]
    m = finalize(merge_two(P, o, nn, log), P)
    save(m, os.path.join(a.out, "국민의힘_병합.csv"))
    log["final_rows"] = len(m)
    report[P] = log

    # ---------- 기본소득당 ----------
    P = "기본소득당"
    old = rd(f"{W}/data/기본소득당보도자료.csv")
    new = rd(f"{W}/step5/bi_final/기본소득당_델타_2026-11-11.csv")
    log = {}
    times = bi_times(f"{W}/step5/bi_delta/basicincome/raw/detail")
    o = frame(old, P, "기존 CSV")
    nn = frame(new, P, "v5_basicincome")
    nn["datetime"] = [times.get(str(p), d) for p, d in zip(nn["post_id"], nn["datetime"])]
    o["post_id"] = [key_of(P, r) or "" for _, r in o.iterrows()]
    m = finalize(merge_two(P, o, nn, log), P)
    save(m, os.path.join(a.out, "기본소득당_병합.csv"))
    log["final_rows"] = len(m)
    log["rows_with_datetime"] = int((m["datetime"] != "").sum())
    report[P] = log

    # ---------- 개혁신당 (v3가 기준) ----------
    P = "개혁신당"
    old = rd(f"{W}/data/개혁신당보도자료.csv")
    new = rd(f"{W}/step3/reform_final/개혁신당보도자료_2024-03-16_2026-11-10.csv")
    log = {}
    o = frame(old, P, "기존 CSV")
    nn = frame(new, P, "v3_reform")
    m = finalize(merge_two(P, o, nn, log), P)
    save(m, os.path.join(a.out, "개혁신당_병합.csv"))
    log["final_rows"] = len(m)
    report[P] = log

    # ---------- 조국혁신당 (기존만) ----------
    P = "조국혁신당"
    old = rd(f"{W}/data/조국혁신당보도자료_2024-03-04_2026-02-05.csv")
    log = {}
    o = frame(old, P, "기존 CSV")
    o["post_id"] = [(key_of(P, r) or "").split("/")[-1] for _, r in o.iterrows()]
    o["_k"] = assign_keys(o, P)
    log["old_rows"] = len(o)
    d0 = len(o)
    o["_t"] = o["text"].map(has_text)
    o = o.sort_values("_t", kind="stable").drop_duplicates("_k", keep="last").drop(columns=["_k", "_t"])
    log["dup_within_old"] = d0 - len(o)
    m = finalize(o, P)
    save(m, os.path.join(a.out, "조국혁신당_병합.csv"))
    log["final_rows"] = len(m)
    report[P] = log

    # ---------- 사회민주당 / 진보당 (신규 수집만) ----------
    for P, f, src in [("사회민주당", f"{W}/step6/sdp_run/sdp/사회민주당_브리핑.csv", "v6_sdp"),
                      ("진보당", f"{W}/step6/jb_fixed/jinbo/진보당보도자료.csv", "v7_jinbo")]:
        d = rd(f)
        m = finalize(frame(d, P, src), P)
        save(m, os.path.join(a.out, f"{P}_병합.csv"))
        report[P] = {"new_rows": len(d), "final_rows": len(m)}

    with open(os.path.join(a.out, "merge_report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
