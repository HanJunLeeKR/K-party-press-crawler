#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Weekly updater: collect the newest posts of the seven parties and APPEND the new rows to the
party CSV files of the Hugging Face dataset.

What it does, for each party
  1. Runs that party's crawler for the last N days (default 21), newest list pages only.
  2. Downloads the party's current CSV from the Hugging Face dataset.
  3. Keeps only the posts that are not in the CSV yet (matched by URL).
  4. Appends those rows at the END of the CSV. Existing rows are never changed.
  5. Checks that the first part of the file is byte-for-byte unchanged, then uploads it.

Safety rules built in
  * Append-only: the old bytes of every CSV must be identical after the update (checked by hash).
  * A party with an implausible number of new rows (default > 3000) is skipped, not uploaded.
  * --dry-run does everything except the upload.
  * One failing party does not stop the others; the script exits with an error at the end.
  * Change detection: an empty list, no posts at all, many posts without title/date, or many
    posts without body text is reported as a problem (exit code 1 -> e-mail alert in GitHub
    Actions). Rows that look broken are not uploaded.
  * Hugging Face keeps the full history of the dataset, so any update can be reverted.

Usage
  export HF_TOKEN=hf_xxx            # a token with write access to the dataset
  python tools/hf_update.py --repo hanjunlee/KoreanPartyPressRelease --days 21
  python tools/hf_update.py --dry-run --parties 사회민주당 기본소득당     # test without uploading (token still needed to download)
"""

import argparse
import csv
import glob
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import merge_parties as M  # noqa: E402  (shared schema helpers: COLS, frame, finalize, bi_times)

CRAWLERS = os.path.join(ROOT, "crawlers")
KST = timezone(timedelta(hours=9))
FILE_FMT = "{party}_보도자료.csv"
MAX_NEW_ROWS = 3000

# How to run each party's crawler. "limit" is the starting number of newest list pages.
# {since} and {limit} are filled in. Each entry is one command (a party may need several).
PARTIES = {
    "더불어민주당": {
        "limit": 20,
        "cmds": [["crawl_dp.py", "--board", str(b)] for b in (188, 11, 230, 13)],
        "cols": {},
    },
    "국민의힘": {
        "limit": 20,
        "cmds": [["crawl_ppp.py", "--board", "all", "--workers", "1", "--delay", "1.0"]],
        "cols": {"category": "section"},
    },
    "조국혁신당": {
        "limit": None,  # reads the site's API; no list pages to limit
        "cmds": [["crawl_cj.py", "--boards", "기자회견문", "논평브리핑", "정책 브리핑", "보도자료"]],
        "cols": {"post_id": "id"},
    },
    "개혁신당": {
        "limit": 10,
        "cmds": [["crawl_reform.py", "--board", "all"]],
        "cols": {},
    },
    "기본소득당": {
        "limit": 10,
        "cmds": [["crawl_basicincome.py"]],
        "cols": {},
    },
    "진보당": {
        "limit": 10,
        "cmds": [["crawl_jinbo.py"]],
        "cols": {},
    },
    "사회민주당": {
        "quiet_ok": True,   # publishes rarely: zero posts in 21 days is normal
        "limit": 5,
        "cmds": [["crawl_sdp.py"]],
        "cols": {},
    },
}


def log(msg):
    print(msg, flush=True)


def url_key(u):
    """Normalise a URL so that http/https, www and trailing slashes do not matter."""
    u = (u or "").strip()
    u = re.sub(r"^https?://(www\.)?", "", u)
    u = u.split("#")[0].rstrip("/")
    return u


def newest_date(workdir):
    """Oldest date seen in any list.json under workdir (None if no list.json)."""
    oldest = None
    for p in glob.glob(os.path.join(workdir, "**", "list.json"), recursive=True):
        try:
            items = json.load(open(p, encoding="utf-8"))
        except (OSError, ValueError):
            continue
        ds = [M.norm_dt(i.get("date", ""))[0] for i in items if i.get("date")]
        ds = [d for d in ds if d]
        if ds:
            oldest = min(ds) if oldest is None else min(oldest, min(ds))
    return oldest


def run_crawlers(party, spec, since, workdir):
    """Run the crawler commands. The list pages are widened until the list reaches back past `since`."""
    limit = spec["limit"]
    for attempt in range(4):
        failed = False
        for cmd in spec["cmds"]:
            script, rest = cmd[0], cmd[1:]
            args = [sys.executable, os.path.join(CRAWLERS, script), "--out", workdir,
                    "--since", since] + rest
            if limit:
                args += ["--limit-pages", str(limit)]
            log(f"  $ {' '.join(os.path.basename(a) if a.endswith('.py') else a for a in args)}")
            r = subprocess.run(args)
            if r.returncode != 0:
                failed = True
        if failed:
            raise RuntimeError("a crawler command failed")
        if not limit:
            return
        oldest = newest_date(workdir)
        if oldest is None or oldest <= since:
            return
        log(f"  list only reaches back to {oldest} (> {since}); doubling list pages")
        limit *= 2
        if limit > 400:
            log("  reached the page limit; continuing with what was collected")
            return
    return


def load_new_rows(party, spec, workdir, since):
    paths = [p for p in glob.glob(os.path.join(workdir, "**", "*.csv"), recursive=True)
             if os.sep + "raw" + os.sep not in p]
    if not paths:
        empty = pd.DataFrame(columns=M.COLS)
        return empty, empty
    frames = [M.rd(p) for p in sorted(paths)]
    df = pd.concat(frames, ignore_index=True)
    df = M.frame(df, party, f"auto_update_{datetime.now(KST).strftime('%Y-%m-%d')}", **spec["cols"])
    if party == "기본소득당":  # the site shows the time of day only inside each post page
        times = M.bi_times(os.path.join(workdir, "basicincome", "raw", "detail"))
        df["datetime"] = [times.get(str(p), d) for p, d in zip(df["post_id"], df["datetime"])]
    df = M.finalize(df, party)
    return df, df[df["date"] >= since].copy()


def health_check(party, spec, workdir, all_rows, in_period, days):
    """Detect a changed website. Returns (problems, block).

    problems: list of human-readable messages. block=True means the collected rows look broken
    and must NOT be appended to the dataset.
    """
    problems, block = [], False
    # a list page that parsed to nothing is the typical sign of a redesigned site
    for p in glob.glob(os.path.join(workdir, "**", "list.json"), recursive=True):
        try:
            n = len(json.load(open(p, encoding="utf-8")))
        except (OSError, ValueError):
            n = 0
        if n == 0:
            problems.append(f"{party}: the list of posts is empty ({os.path.relpath(p, workdir)}). "
                            "The website structure may have changed.")
            block = True
    n = len(in_period)
    if n == 0 and not spec.get("quiet_ok") and not block:
        problems.append(f"{party}: no posts at all in the last {days} days. "
                        "The website may have changed (or the site is blocking this server).")
    total = len(all_rows)
    if total >= 10:
        no_title = float((all_rows["title"].str.strip() == "").mean())
        no_date = float((all_rows["date"].str.strip() == "").mean())
        if no_title > 0.1 or no_date > 0.1:
            problems.append(f"{party}: {no_title:.0%} of the collected posts have no title and {no_date:.0%} "
                            "have no date. The page layout may have changed; nothing was uploaded for this party.")
            block = True
    if n >= 10:
        no_text = float((in_period["text"].str.strip() == "").mean())
        if no_text > 0.4:
            problems.append(f"{party}: {no_text:.0%} of the recent posts have no body text. "
                            "The post-page layout may have changed.")
    return problems, block


def file_sha(path, nbytes):
    h = hashlib.sha256()
    left = nbytes
    with open(path, "rb") as f:
        while left > 0:
            chunk = f.read(min(1 << 20, left))
            if not chunk:
                break
            h.update(chunk)
            left -= len(chunk)
    return h.hexdigest()


def append_rows(csv_path, new, party):
    """Append `new` to the CSV without touching existing bytes. Returns (rows_added)."""
    size0 = os.path.getsize(csv_path)
    sha0 = file_sha(csv_path, size0)
    with open(csv_path, "rb") as f:
        head = f.readline().decode("utf-8-sig").strip()
        f.seek(max(size0 - 1, 0))
        last = f.read(1)
    cols = next(csv.reader([head]))
    if cols != M.COLS:
        raise RuntimeError(f"unexpected header in the existing CSV: {cols}")
    with open(csv_path, "ab") as f:
        if last != b"\n":
            f.write(b"\n")
    size1 = os.path.getsize(csv_path)
    new[M.COLS].to_csv(csv_path, mode="a", header=False, index=False, encoding="utf-8",
                       quoting=csv.QUOTE_ALL, lineterminator="\n")
    # integrity: all old bytes (plus the optional newline) are unchanged
    if file_sha(csv_path, size0) != sha0:
        raise RuntimeError("existing bytes changed; aborting")
    return len(new)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default="hanjunlee/KoreanPartyPressRelease", help="Hugging Face dataset id")
    ap.add_argument("--days", type=int, default=21, help="collect posts from the last N days (default 21)")
    ap.add_argument("--parties", nargs="*", default=list(PARTIES), choices=list(PARTIES))
    ap.add_argument("--workdir", default="./_update", help="scratch folder (crawler output and downloaded CSVs)")
    ap.add_argument("--dry-run", action="store_true", help="do everything except the upload")
    ap.add_argument("--max-new-rows", type=int, default=MAX_NEW_ROWS)
    args = ap.parse_args()

    from huggingface_hub import HfApi, CommitOperationAdd, hf_hub_download

    token = os.environ.get("HF_TOKEN")
    if not token:
        sys.exit("HF_TOKEN is not set. The dataset is gated, so a token is needed even to download it.")
    api = HfApi(token=token)

    since = (datetime.now(KST) - timedelta(days=args.days)).strftime("%Y-%m-%d")
    log(f"Collecting posts since {since} ({args.days} days) for: {', '.join(args.parties)}")
    os.makedirs(args.workdir, exist_ok=True)

    ops, summary, errors, problems = [], {}, [], []
    for party in args.parties:
        spec = PARTIES[party]
        log(f"\n=== {party} ===")
        try:
            out = os.path.join(args.workdir, "crawl", party)
            run_crawlers(party, spec, since, out)
            all_rows, new = load_new_rows(party, spec, out, since)
            log(f"  collected {len(new)} posts in the period")
            found, block = health_check(party, spec, out, all_rows, new, args.days)
            for msg in found:
                log(f"  WARNING: {msg}")
            problems += found
            if block:
                summary[party] = 0
                continue
            # rows whose page could not be downloaded are not appended now; they are retried next week
            if "status" in new.columns:
                failed_rows = new["status"] == "fetch_failed"
                if failed_rows.any():
                    msg = f"{party}: {int(failed_rows.sum())} posts could not be downloaded and were skipped (retried next run)."
                    log(f"  WARNING: {msg}")
                    problems.append(msg)
                    new = new[~failed_rows]

            fname = FILE_FMT.format(party=party)
            local = hf_hub_download(args.repo, fname, repo_type="dataset", token=token,
                                    local_dir=os.path.join(args.workdir, "hf"))
            ex = pd.read_csv(local, dtype=str, keep_default_na=False, usecols=["url", "title", "date"])
            have_urls = {url_key(u) for u in ex["url"]}
            have_td = {(re.sub(r"\s+", "", t), d) for t, d in zip(ex["title"], ex["date"])}
            is_new = [url_key(u) not in have_urls and (re.sub(r"\s+", "", t), d) not in have_td
                      for u, t, d in zip(new["url"], new["title"], new["date"])]
            new = new[is_new]
            new = new.drop_duplicates("url").sort_values(["date", "board_name"], kind="stable")
            log(f"  {len(new)} are new (not in {fname})")
            summary[party] = len(new)
            if len(new) == 0:
                continue
            if len(new) > args.max_new_rows:
                raise RuntimeError(f"{len(new)} new rows exceeds the safety limit {args.max_new_rows}; not uploading")
            append_rows(local, new, party)
            log(f"  appended {len(new)} rows to {fname}")
            ops.append(CommitOperationAdd(path_in_repo=fname, path_or_fileobj=local))
        except Exception as e:  # keep going with the other parties
            log(f"  ERROR for {party}: {e}")
            errors.append(party)
            problems.append(f"{party}: the update failed with an error: {e}")

    log("\n=== summary ===")
    for p, n in summary.items():
        log(f"  {p}: +{n}")
    if errors:
        log(f"  failed: {', '.join(errors)}")
    for msg in problems:
        log(f"  problem: {msg}")

    if ops and not args.dry_run:
        msg = "Weekly update: " + ", ".join(f"{p} +{n}" for p, n in summary.items() if n)
        c = api.create_commit(args.repo, repo_type="dataset", operations=ops, commit_message=msg)
        log(f"uploaded: {c.commit_url}")
    elif ops:
        log("dry run: nothing uploaded (appended files are in " + os.path.join(args.workdir, "hf") + ")")
    else:
        log("no new posts to upload")
    with open(os.path.join(args.workdir, "update_summary.json"), "w", encoding="utf-8") as f:
        json.dump({"since": since, "new_rows": summary, "failed": errors, "problems": problems,
                   "run_at": datetime.now(KST).isoformat(timespec="seconds")}, f, ensure_ascii=False, indent=2)
    # a non-zero exit marks the GitHub run as failed, which triggers the e-mail alert
    sys.exit(1 if (errors or problems) else 0)


if __name__ == "__main__":
    main()
