#!/usr/bin/env python3
"""
Diagnose why Jinbo Party (진보당) detail pages fail on GitHub Actions runners.

It requests list pages and then detail pages one by one, and records for every request:
time, URL, HTTP status, elapsed seconds, error type (ConnectTimeout / ReadTimeout /
ConnectionError / HTTP error), response size, and whether the page has the expected
content block. After the first failure it probes the home page every 30 seconds to see
whether the whole site became unreachable (an IP block) and for how long.

Output: _diag/jinbo_requests.csv and _diag/jinbo_summary.json (no credentials needed).
Usage:  python tools/diag_jinbo.py --n 150 --delay 0.7 --probe-minutes 10
"""
import argparse, csv, json, os, socket, sys, time
from datetime import datetime, timezone
import requests
from bs4 import BeautifulSoup

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "crawlers"))
import crawl_jinbo as J  # reuse SITE, BOARDS, HEADERS, parse_list, read_url


def one(session, url, params=None, check=False, timeout=(10, 30)):
    t0 = time.time(); rec = {"time": datetime.now(timezone.utc).isoformat(timespec="seconds"), "url": url + (("?" + "&".join(f"{k}={v}" for k, v in params.items())) if params else "")}
    try:
        r = session.get(url, params=params, timeout=timeout)
        rec.update(status=r.status_code, error="", size=len(r.content), elapsed=round(time.time() - t0, 2),
                   server=r.headers.get("Server", ""), final_url=r.url)
        if check:
            r.encoding = "utf-8"
            s = BeautifulSoup(r.text, "html.parser")
            rec["has_content"] = bool(s.select_one("div.content_box") and s.select_one("div.table_view_h"))
            if not rec["has_content"]:
                rec["snippet"] = " ".join(s.get_text(" ", strip=True).split())[:200]
        return rec, r
    except requests.RequestException as e:
        rec.update(status="", error=type(e).__name__, detail=str(e)[:200], size=0, elapsed=round(time.time() - t0, 2))
        return rec, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=150, help="detail pages to request")
    ap.add_argument("--delay", type=float, default=0.7, help="seconds between requests")
    ap.add_argument("--probe-minutes", type=float, default=10)
    ap.add_argument("--out", default="_diag")
    a = ap.parse_args(); os.makedirs(a.out, exist_ok=True)
    host = J.SITE.split("//")[1].split("/")[0]
    summ = {"site": J.SITE, "runner_time": datetime.now(timezone.utc).isoformat()}
    try:
        summ["dns"] = sorted({x[4][0] for x in socket.getaddrinfo(host, 443)})
    except OSError as e:
        summ["dns"] = f"failed: {e}"
    try:
        summ["runner_public_ip"] = requests.get("https://api.ipify.org", timeout=10).text
    except requests.RequestException:
        summ["runner_public_ip"] = "unknown"
    s = requests.Session(); s.headers.update(J.HEADERS)
    rows, items = [], []
    for board, (p, bid) in J.BOARDS.items():
        for page in (1, 2, 3):
            rec, r = one(s, J.list_url(board), {"nPage": page, "p": p, "b": bid, "cate": "", "f": "", "s": ""})
            rec["kind"] = f"list:{board}"; rows.append(rec)
            if r is not None and r.ok:
                r.encoding = "utf-8"
                items += J.parse_list(r.text, board)
            time.sleep(a.delay)
    print(f"list requests: {len(rows)}, posts found: {len(items)}", flush=True)
    seen, first_fail = set(), None
    # interleave boards so a board-specific problem is visible
    by = {}
    for it in items:
        if (it["board_name"], it["post_id"]) not in seen:
            seen.add((it["board_name"], it["post_id"])); by.setdefault(it["board_name"], []).append(it)
    order = []
    while any(by.values()):
        for b in list(by):
            if by[b]: order.append(by[b].pop(0))
    for i, it in enumerate(order[: a.n], 1):
        rec, r = one(s, it["url"], check=True)
        rec.update(kind=f"detail:{it['board_name']}", post_id=it["post_id"], seq=i); rows.append(rec)
        bad = rec["error"] or rec["status"] != 200 or not rec.get("has_content", False)
        if bad and first_fail is None:
            first_fail = i; print(f"first failure at detail #{i}: {rec.get('error') or rec.get('status')} {rec.get('snippet','')}", flush=True)
        if i % 25 == 0: print(f"  {i} details, failures so far: {sum(1 for x in rows if x['kind'].startswith('detail') and (x['error'] or x['status']!=200 or not x.get('has_content')))}", flush=True)
        time.sleep(a.delay)
    summ["first_failure_at_detail"] = first_fail
    # probe whether the site as a whole is unreachable after failures, and when it recovers
    probes = []
    if first_fail is not None and a.probe_minutes > 0:
        end = time.time() + a.probe_minutes * 60
        while time.time() < end:
            rec, _ = one(s, J.SITE + "/", timeout=(10, 20)); rec["kind"] = "probe:home"; rows.append(rec)
            fresh = requests.Session(); fresh.headers.update(J.HEADERS)
            rec2, _ = one(fresh, order[0]["url"], check=True); rec2["kind"] = "probe:detail_new_session"; rows.append(rec2)
            probes.append((rec["time"], rec["error"] or rec["status"], rec2["error"] or rec2["status"]))
            time.sleep(30)
    summ["probes"] = probes
    det = [x for x in rows if x["kind"].startswith("detail")]
    from collections import Counter
    summ["detail_requests"] = len(det)
    summ["detail_outcomes"] = dict(Counter((x["error"] or str(x["status"])) + ("" if x.get("has_content") else "/no_content") for x in det))
    summ["by_board"] = {b: dict(Counter((x["error"] or str(x["status"])) for x in det if x["kind"] == f"detail:{b}")) for b in J.BOARDS}
    summ["median_elapsed_ok"] = sorted(x["elapsed"] for x in det if x["status"] == 200)[len([x for x in det if x["status"] == 200]) // 2] if any(x["status"] == 200 for x in det) else None
    keys = sorted({k for x in rows for k in x})
    with open(os.path.join(a.out, "jinbo_requests.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=keys); w.writeheader(); w.writerows(rows)
    json.dump(summ, open(os.path.join(a.out, "jinbo_summary.json"), "w"), ensure_ascii=False, indent=2)
    print(json.dumps(summ, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
