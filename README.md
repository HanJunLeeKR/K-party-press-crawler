# K-party-press-crawler

The National Assembly Library(https://law.nanet.go.kr/policy/assemblyBodo/list.do) of Korea provides party-specific statement data, but it does not provide the predecessor statements of mainstream parties. Recently, Lim (2025, https://www.nature.com/articles/s41597-025-05220-4) has made a significant contribution to this field by providing press releases and datasets from mainstream Korean political parties, but statements and press releases from non-mainstream parties are limited, and data reflecting changes in the political context following President Yoon Suk Yeol's martial law are not provided. Therefore, we developed this data collector to also gather past statements and press releases from mainstream parties. This code includes automation to collect official statements and press release data weekly using Github Actions. The collected dataset is on Huggingface. Crawlers for the official press releases, commentaries, and briefings of the seven political parties represented in the National Assembly of South Korea (as of 2026):

| Party | English name |
|---|---|
| 더불어민주당 | Democratic Party of Korea, TheMinjoo |
| 국민의힘 | Conservative Party of Korea, People Power Party |
| 조국혁신당 | Rebuilding Korea Party |
| 개혁신당 | Reform Party |
| 기본소득당 | Basic Income Party |
| 사회민주당 | Social Democratic Party, Samindang |
| 진보당 | Progressive Party, Jinboparty |

This repository contains **code and documentation only**. The collected dataset (about 146,000 documents) is published on Hugging Face and is gated:

- Dataset: https://huggingface.co/datasets/hanjunlee/KoreanPartyPressRelease

If you only want to *use* the data, download the dataset. If you want to *re-collect*, update, or extend it (for example, to fetch only last month's releases), this repository is for you.

---

## Table of contents

1. [What you get](#1-what-you-get)
2. [Quick start (10 minutes)](#2-quick-start-10-minutes)
3. [Installation in detail](#3-installation-in-detail)
4. [The crawlers, party by party](#4-the-crawlers-party-by-party)
5. [Common recipes](#5-common-recipes)
6. [Understanding the output](#6-understanding-the-output)
7. [Troubleshooting](#7-troubleshooting)
8. [Collection criteria](#8-collection-criteria)
9. [Automatic weekly updates (GitHub Actions)](#9-automatic-weekly-updates-github-actions)
10. [Responsible use](#10-responsible-use)
11. [Repository layout](#11-repository-layout)
12. [Citation and contact](#12-citation-and-contact)

---

## 1. What you get

Each crawler visits one party's website, walks through the list pages of the selected boards (for example, "press releases" and "commentaries"), opens every post, and saves:

- a **CSV file** with one row per post (title, date, author, body text, URL, and so on),
- the **raw HTML** of every page you downloaded (compressed, in a `raw/` folder), so that you can re-parse later without contacting the website again,
- a **checkpoint file** (`details.jsonl`), so that if the run is interrupted you can run the same command again and it resumes where it stopped.

The crawlers are independent scripts. There is one file per party, and each relies mainly on `requests` and `beautifulsoup4`.

## 2. Quick start (10 minutes)

This walks you through a small, safe first run: the Social Democratic Party briefings from the last few months.

**Step 1. Install Python 3.10 or newer.** Check with:

```bash
python --version        # on some systems: python3 --version
```

**Step 2. Download the code.**

```bash
git clone https://github.com/HanJunLeeKR/k-party-press-crawler.git
cd k-party-press-crawler
```

(No Git? On the GitHub page, click **Code → Download ZIP**, unzip it, and open a terminal in that folder.)

**Step 3. Create an isolated environment and install the requirements.**

```bash
python -m venv .venv
source .venv/bin/activate          # Windows (PowerShell): .venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

**Step 4. Run a first collection.** The `--since` option limits the run to recent posts, so it finishes in a minute or two:

```bash
python crawlers/crawl_sdp.py --out ./out --since 2026-09-01
```

**Step 5. Look at the result.**

```text
out/sdp/사회민주당_브리핑.csv      <- the data
out/sdp/raw/                       <- raw HTML (compressed)
out/sdp/summary.json               <- counts and a status summary
```

Open the CSV in Python:

```python
import pandas as pd
df = pd.read_csv("out/sdp/사회민주당_브리핑.csv")
print(df[["date", "title", "status"]].head())
```

or in Excel (the files are UTF-8 with a byte-order mark, so Korean text displays correctly).

That is the whole workflow. Every other crawler works the same way; only the script name and a few options change (see [Section 4](#4-the-crawlers-party-by-party)).

## 3. Installation in detail

### 3.1 Python packages

```bash
pip install -r requirements.txt
```

This installs `requests`, `beautifulsoup4`, `pandas`, and `pyhwp`.

### 3.2 Optional programs (only needed for some posts)

A few posts contain their text **only in an attached file or in an image**. To read those, the crawlers call external programs. If a program is missing, the crawler does not fail; it records the post with empty text and explains why in the `fail_reason` column.

| Used for | Program | Windows | macOS | Ubuntu / Debian |
|---|---|---|---|---|
| PDF attachments | Poppler (`pdftotext`) | Download Poppler for Windows and add its `bin` folder to `PATH` | `brew install poppler` | `sudo apt install poppler-utils` |
| HWP attachments (Hancom) | `pyhwp` (`hwp5txt`) | included in `requirements.txt` | included | included |
| Text inside images (OCR) | Tesseract with Korean data | UB-Mannheim installer, select the Korean language data | `brew install tesseract tesseract-lang` | `sudo apt install tesseract-ocr tesseract-ocr-kor` |

Check that they work:

```bash
pdftotext -v
tesseract --list-langs     # the list should include "kor"
```

### 3.3 Network

You need ordinary internet access to each party's website. No login and no API key is required.

## 4. The crawlers, party by party

All crawlers share these options:

| Option | Meaning |
|---|---|
| `--out DIR` | **Required.** Output folder. It is created if needed. |
| `--since YYYY-MM-DD` | Only posts on or after this date (inclusive). |
| `--until YYYY-MM-DD` | Only posts on or before this date (inclusive). |
| `--no-details` | Collect only the list of posts (titles, dates, URLs); do not open each post. Useful for a quick test. |

Several crawlers also accept `--exclude-csv existing.csv` (skip posts that you already have; see [Section 5.3](#53-update-an-existing-collection-incremental-collection)).

| Party | Script | Boards collected | Output location |
|---|---|---|---|
| Democratic Party of Korea | `crawlers/crawl_dp.py` | press releases (`188`), commentaries and briefings (`11`), opening remarks (`230`), policy briefings (`13`) | `out/brd{N}_{name}/{name}.csv` |
| People Power Party | `crawlers/crawl_ppp.py` | spokesperson commentaries and press releases (`BBSDD0001`), floor-leadership press releases (`BBSDD0002`), Policy Committee (`BBSDD0003`), media committee (`BBSDD0042`, formerly "Special Committee on Media") | `out/ppp_{code}/{name}.csv` |
| Rebuilding Korea Party | `crawlers/crawl_cj.py` | press-conference statements, commentaries and briefings, policy briefings, press releases | `out/조국혁신당_보도자료_{start}_{end}.csv` |
| Reform Party | `crawlers/crawl_reform.py` | commentaries and briefings (`briefing`), press releases (`press`) | `out/reform_{key}/{name}.csv` |
| Basic Income Party | `crawlers/crawl_basicincome.py` | commentaries and press releases (one board) | `out/basicincome/기본소득당보도자료.csv` |
| Social Democratic Party | `crawlers/crawl_sdp.py` | briefings | `out/sdp/사회민주당_브리핑.csv` |
| Progressive Party | `crawlers/crawl_jinbo.py` | press releases, commentaries, opening remarks, policy commentaries | `out/jinbo/진보당보도자료.csv` |

Examples (run one at a time; each prints its progress):

```bash
# Democratic Party: --board is required and takes the site's board number
python crawlers/crawl_dp.py --out ./out --board 188      # press releases
python crawlers/crawl_dp.py --out ./out --board 11       # commentaries and briefings
python crawlers/crawl_dp.py --out ./out --board 230      # opening remarks
python crawlers/crawl_dp.py --out ./out --board 13       # policy briefings

# People Power Party: one board, or all of them
python crawlers/crawl_ppp.py --out ./out --board BBSDD0001
python crawlers/crawl_ppp.py --out ./out --board all

# Rebuilding Korea Party: choose boards by name (quote names that contain a space)
python crawlers/crawl_cj.py --out ./out --boards 논평브리핑 "정책 브리핑" --since 2026-01-01

# Reform Party
python crawlers/crawl_reform.py --out ./out --board all

# Basic Income, Social Democratic, Progressive Parties
python crawlers/crawl_basicincome.py --out ./out
python crawlers/crawl_sdp.py --out ./out
python crawlers/crawl_jinbo.py --out ./out --boards 정책논평 논평
```

Party-specific notes:

- **Democratic Party.** Many old posts show the same placeholder date `2003-11-11` on the website. Do not rely on `--since` for very old periods. Some posts consist only of attached PDFs that the website refuses to serve (`status = site_denied`).
- **People Power Party.** The server sometimes resets connections when it receives many requests at once. Use `--workers 1 --delay 1.5` (see [Section 7](#7-troubleshooting)). The old "Special Committee on Media" board is the same board as the current media committee (`BBSDD0042`).
- **Rebuilding Korea Party.** Data are read from the site's public board API. `--since` and `--until` are also available under the older names `--start` and `--end`.
- **Social Democratic Party.** Items labelled as social-media posts are skipped unless you add `--include-sns`.
- **Progressive Party.** The website does not declare a character encoding; the crawler decodes pages as UTF-8 explicitly. The commentary board includes some posts titled as party-schedule announcements.

## 5. Common recipes

### 5.1 Collect a specific period

```bash
python crawlers/crawl_jinbo.py --out ./out --since 2025-01-01 --until 2025-12-31
```

The crawler reads the full list of posts, keeps only those in the period, and opens only those. Posts without a date in the list are excluded because their period cannot be verified. The full list is still saved in `list.json`.

### 5.2 Resume an interrupted run

Just run the same command again. Each downloaded post is saved to `details.jsonl` as it arrives, and the crawler skips posts it already has.

### 5.3 Update an existing collection (incremental collection)

If you already have a CSV and want only the posts that are new (or that were empty last time):

```bash
python crawlers/crawl_ppp.py --out ./new --board all --exclude-csv ppp_old.csv
```

- Posts that already have body text in the old CSV are skipped.
- New posts and posts whose text was empty are downloaded.
- The result contains **only the newly downloaded rows**; combine it with your old data yourself (see `tools/merge_parties.py` for an example).
- The old CSV must have `url` and `text` columns (for the Democratic Party also `board_name`).

Supported by: `crawl_ppp.py`, `crawl_basicincome.py`, `crawl_sdp.py`, `crawl_jinbo.py`, `crawl_dp.py`.

For the Democratic Party there is also `--refetch-empty old.csv`, which re-downloads only the posts whose text was empty in the old file.

### 5.4 Be gentle with a server that resets connections

```bash
python crawlers/crawl_ppp.py --out ./out --board BBSDD0003 --workers 1 --delay 1.5
```

- `--workers N`: number of simultaneous requests (default 3).
- `--delay SECONDS`: wait between requests (default 0.2–0.8 s; with `--delay 1.5` the wait is 1.5–2.25 s).
- `--reuse-list`: when resuming, reuse the saved `list.json` instead of walking all list pages again.

### 5.5 Quick test without downloading everything

```bash
python crawlers/crawl_sdp.py --out ./test --no-details
python crawlers/crawl_dp.py --out ./test --board 188 --limit-pages 3 --no-details
```

## 6. Understanding the output

### 6.1 Folder contents

For each board you get a folder like this:

```text
out/ppp_BBSDD0003/
├── 정책위_보도자료.csv      the result
├── list.json                every post found in the list pages
├── details.jsonl            checkpoint: one JSON record per downloaded post
├── summary.json             counts by status, time of collection
└── raw/
    ├── list/                raw list pages (gzip)
    └── detail/              raw post pages (gzip)
```

The raw files are never modified by later steps. If you change how a page is parsed, you can re-run on the saved HTML.

### 6.2 CSV columns

| Column | Meaning |
|---|---|
| `post_id` | Identifier of the post on the party's website. |
| `board_name` | Board the post came from. |
| `title` | Title as displayed. |
| `category` (or `section`) | Sub-category, when the site provides one. |
| `date` | Publication date, `YYYY-MM-DD`. |
| `pub_date` | Publication date as printed inside the post page. |
| `date_mismatch` | `True` if `date` (list page) and `pub_date` (post page) disagree. |
| `writer` | Author or publishing unit, when available. |
| `text` | Body text, as published (markup removed, otherwise untouched). |
| `text_source` | `body` (from the page), `attachment` (from an attached PDF/HWP), `ocr` (from images), or `none`. |
| `status` | Result of the download, see below. |
| `fail_reason` | Human-readable reason (in Korean) when text could not be obtained. |
| `url` | Address of the original post. |
| `fetched_at` | When the post was downloaded. |

(Column sets differ slightly between parties, because the websites expose different information. The Rebuilding Korea Party crawler writes a shorter set: `id, board_name, title, category, date, writer, text, url`.)

### 6.3 Status values

| `status` | Meaning |
|---|---|
| `ok` | Body text was read from the page. |
| `attachment_ok` | Body text was extracted from an attached PDF or HWP file. |
| `image_ocr` | The body consisted of images; text was recognized by OCR. |
| `image_only_ocr_empty` | The body consists of images, and OCR found no usable text. |
| `image_download_failed` | The body images could not be downloaded (for example, their server no longer exists). |
| `site_denied` | The website refused to serve the attachment. |
| `no_body` | The post is empty on the website. |
| `fetch_failed` | The page could not be downloaded even after retries. Run again to retry. |

Posts with no usable text are **kept** in the CSV (with an explanation) rather than silently dropped.

### 6.4 Excel limits

Excel cannot hold more than 32,767 characters in a cell, so very long posts may be cut or corrupt a row if you open and re-save the CSV in Excel. Use Python (`pandas`) or R for analysis, or open the file read-only.

## 7. Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| `ConnectionResetError` or many retries | The server is limiting request rates. Use `--workers 1 --delay 1.5` (People Power Party crawler) and run again; progress is saved. |
| The script stops at the very first request | Same cause. The crawler retries the first connection up to six times; if it still fails, wait a few minutes and run again. |
| Korean text looks like `ì¢…...` | The page was decoded with the wrong encoding. The crawlers set UTF-8 where needed; if you parse raw HTML yourself, decode it as UTF-8. |
| `pdftotext: command not found` / `tesseract: command not found` | Optional program not installed ([Section 3.2](#32-optional-programs-only-needed-for-some-posts)). Collection continues; affected posts have empty text. |
| A post has empty `text` | Look at `status` and `fail_reason`. Many are genuinely empty or image-only posts, or attachments that the site does not serve. Running again does not change this. |
| Run takes hours | Normal for full histories (tens of thousands of posts). Use `--since` to limit the period. |
| Dates such as `2003-11-11` | Placeholder date shown by the Democratic Party website for old posts. |
| Windows: `Activate.ps1` is blocked | Run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` in PowerShell once, or use Command Prompt and `.venv\Scripts\activate.bat`. |

## 8. Collection criteria

To keep the parties comparable, the same rule is applied to every site:

1. Collect the **News** (소식) section of each party's official website.
2. **Always include** commentaries and briefings (논평·브리핑). Include press releases, opening remarks, and policy-related briefing or commentary boards where the party has them.
3. **Exclude** notice boards (공지사항), schedule pages, galleries, newsletters, and reference archives.
4. For each included board, walk **all** list pages and keep every post.

If a party changes its website, a crawler may stop working. Please open an issue with the command you ran and the error message.

## 9. Automatic weekly updates (GitHub Actions)

The workflow `.github/workflows/weekly-update.yml` keeps the Hugging Face dataset up to date. Every **Sunday at 21:17 UTC (Monday 06:17 in Korea)** it:

1. collects the posts of the **last 21 days** from all seven party websites (the overlap of three weeks prevents gaps if a run is missed),
2. downloads each party's current CSV from the dataset,
3. keeps only the posts that are **not in the CSV yet** (matched by URL),
4. **appends** those rows at the end of the CSV and uploads the file as one commit named `Weekly update: ...`.

Existing rows are never edited or removed. Before uploading, the script checks by hash that every old byte of the file is unchanged, and it refuses to upload a party whose number of new rows is implausible (more than 3,000), which would indicate a parsing problem. Hugging Face keeps the full history of the dataset, so any update can be reverted from the repository's *Files and versions → History* page.

The code lives in `tools/hf_update.py` (it reuses the crawlers in `crawlers/`).

### 9.1 One-time setup (for the repository owner)

1. **Create a Hugging Face token.** Go to https://huggingface.co/settings/tokens → *Create new token* → type *Fine-grained* → give it **write** access to the dataset `hanjunlee/KoreanPartyPressRelease` only. Copy the token (it starts with `hf_`).
2. **Store it as a GitHub secret.** In this repository: *Settings → Secrets and variables → Actions → New repository secret*. Name: `HF_TOKEN`. Value: the token. Never write a token into a file or a commit.
3. **(Optional but recommended) Set up the alert e-mail.** See Section 9.4. Without it GitHub still sends its own "workflow failed" e-mail, but not the detailed message.
4. **Enable Actions** (*Actions* tab → enable workflows, if asked).
5. **Run a test first.** *Actions → Weekly update to Hugging Face → Run workflow*. Leave *dry run* checked. It collects and checks everything but uploads nothing. Open the run and read the summary at the bottom: the number of new posts per party should be small and plausible.
6. **Run it for real once**, with *dry run* unchecked, and look at the new commit on the dataset page. After that the weekly schedule takes over.

### 9.2 Running it by hand (locally)

```bash
export HF_TOKEN=hf_xxx                              # PowerShell: $env:HF_TOKEN="hf_xxx"
python tools/hf_update.py --dry-run --parties 사회민주당 기본소득당     # test, no upload
python tools/hf_update.py --days 21                                    # all parties, uploads
```

Useful options: `--days N` (period), `--parties ...` (subset), `--dry-run` (no upload), `--workdir DIR` (scratch folder). The token is required even for a dry run, because the dataset is gated.

### 9.3 Things to know

- **A failing party does not stop the others.** The run is marked as failed and an alert e-mail is sent (Section 9.4); fix the crawler and run the workflow again (already-uploaded parties are skipped because their posts are no longer new).
- **Website changes** are the usual cause of failures. If a party redesigns its site, its crawler needs an update.
- **Scheduled workflows are paused** by GitHub after 60 days without any repository activity. Re-enable them in the *Actions* tab if that happens.
- **Network location.** GitHub's runners are outside Korea. If a party website blocks or throttles them, that party fails with connection errors; run the updater from your own computer instead (Section 9.2).
- **Posts that are later edited** or whose attachments become available later are not refreshed, because the updater only appends posts it has not seen. Use the crawlers' `--exclude-csv` option (Section 5.3) for a manual repair.
- The dataset card (README on Hugging Face) describes the first release of 2026-11-11; later rows are marked in the `source` column as `auto_update_YYYY-MM-DD`.

### 9.4 Alert e-mail when a website changes

The updater does not only look for crashes; it also detects *silent* breakage (a redesigned page that still loads but yields nothing useful). A party is flagged when:

- its list of posts is empty (blocking: nothing is uploaded for that party),
- no post at all was found in the period (not applied to 사회민주당, which publishes rarely),
- more than 10% of the new posts have no title or no date (blocking),
- more than 40% of the new posts have an empty body (warning only),
- rows with `status = fetch_failed` are never uploaded; they are retried at the next run.

If any party is flagged or fails, the job ends as *failed* and the step `E-mail an alert if something went wrong` sends a plain-text mail (problem list, new rows per party, link to the run log) through Gmail. Healthy parties are still updated.

Setup (once):

1. Turn on 2-step verification for the sending Google account, then create an **app password** at https://myaccount.google.com/apppasswords.
2. Add repository secrets (*Settings → Secrets and variables → Actions*): `MAIL_USERNAME` (the Gmail address that sends), `MAIL_PASSWORD` (the app password), and optionally `MAIL_TO` (the receiving address; default is `MAIL_USERNAME`). Never write them into a file, an issue, or a chat.
3. Test the mail text locally without sending: `python tools/send_alert_mail.py --print`.

If the secrets are missing the step is skipped with a message; the original failure is not hidden.

> **Material from another corpus.** 19 People Power Party documents that our crawl missed, and 513 publication dates of Democratic Party posts, were taken from Lim (2025) and are marked in the data. See [docs/APPENDIX_lim2025.md](docs/APPENDIX_lim2025.md).

## 10. Responsible use

- **Be polite to the servers.** The defaults wait between requests; do not remove these waits or run many copies in parallel.
- **Check the terms of each website** and the copyright rules that apply to your use. The texts are public communications of political organizations, but the websites' terms apply to how you collect and redistribute them.
- Posts name public figures in their official roles. Do not use the data to target private individuals.
- Raw data should be preserved. Do cleaning, de-duplication, and merging in a **new folder**, never by overwriting the raw files.

## 11. Repository layout

```text
crawlers/            one crawler per party (crawl_dp.py, crawl_ppp.py, ...)
tools/hf_update.py       weekly updater used by the GitHub Action (appends new posts to the dataset)
tools/send_alert_mail.py alert e-mail sent by the workflow when a problem is detected
docs/APPENDIX_lim2025.md which rows and dates were supplemented from Lim (2025), and how they are marked
.github/workflows/      GitHub Actions workflow (weekly-update.yml)
tools/merge_parties.py   maintainer script that merged earlier and later collections
                         (paths are specific to the maintainer's working folders; read it as an example)
notebooks/reinforce_dp.ipynb   earlier notebook for repairing Democratic Party CSV text
docs/diagnosis.md    record of the problems found when the dataset was first updated
requirements.txt     Python packages
```

## 12. Citation and contact

If you use the dataset or this code in research, please cite:

> Lee, H. J. (2026). *Korean Parliamentary Party Press Release Dataset* [Data set]. Hugging Face. https://huggingface.co/datasets/hanjunlee/KoreanPartyPressRelease

Contact: hanjunleekr@hufs.ac.kr (Hankuk University of Foreign Studies). Bug reports and suggestions are welcome as GitHub issues.

## Acknowledgements
This notebook was developed with the assistance of Claude Opus 5.5 (Anthropic).
