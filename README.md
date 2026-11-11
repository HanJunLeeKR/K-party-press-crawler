# party-press-crawler

한국 정당(더불어민주당, 국민의힘, 조국혁신당, 개혁신당, 기본소득당, 사회민주당, 진보당)의 보도자료·논평·성명을 수집하는 크롤러 모음입니다. 결과 데이터셋은 Hugging Face에 있으며, 이 저장소에는 코드와 문서만 둡니다.

- 데이터셋: https://huggingface.co/datasets/hanjunlee/KoreanPartyPressRelease (gated)

## 크롤러

정당마다 파일이 하나씩 있고, 모두 `--out 출력폴더`가 필요합니다.

| 정당 | 파일 | 게시판 |
|---|---|---|
| 더불어민주당 | `crawlers/crawl_dp.py` | 보도자료(188), 논평·브리핑(11), 모두발언(230) |
| 국민의힘 | `crawlers/crawl_ppp.py` | 대변인 논평·보도자료(BBSDD0001), 원내 보도자료(BBSDD0002), 미디어특위(BBSDD0042) |
| 조국혁신당 | `crawlers/crawl_cj.py` | 기자회견문, 논평브리핑, 보도자료 |
| 개혁신당 | `crawlers/crawl_reform.py` | 논평·브리핑(briefing), 보도자료(press) |
| 기본소득당 | `crawlers/crawl_basicincome.py` | 논평·보도자료 |
| 사회민주당 | `crawlers/crawl_sdp.py` | 브리핑 (논평성명·브리핑·보도자료, SNS는 기본 제외) |
| 진보당 | `crawlers/crawl_jinbo.py` | 보도자료, 논평, 모두발언 |

```bash
python crawlers/crawl_sdp.py --out ./out
python crawlers/crawl_dp.py --out ./out --board 188        # 게시판 번호를 지정
python crawlers/crawl_ppp.py --out ./out --board all
python crawlers/crawl_jinbo.py --out ./out --since 2025-01-01 --until 2025-12-31   # 기간 지정
python crawlers/crawl_cj.py --out ./out --since 2025-01-01 --until 2025-12-31
```

### 기간 지정과 빈 곳 채우기

- **기간 지정:** 모든 수집기가 `--since YYYY-MM-DD`와 `--until YYYY-MM-DD`(양끝 포함)를 지원합니다. 목록에서 날짜로 거른 뒤 해당 글만 상세 수집하므로 시간이 크게 줄고, `list.json`에는 전체 목록이 그대로 남습니다. 목록에 날짜가 없는 글은 기간을 확인할 수 없어 제외됩니다. (조국혁신당은 기존 `--start`/`--end`도 계속 쓸 수 있습니다.) 더불어민주당 옛 글 중에는 날짜가 `2003-11-11`로 일괄 표시된 것이 많으니, 옛 기간을 지정할 때 유의하세요.
- **빈 곳 채우기·증분 수집:** 국민의힘, 기본소득당, 사회민주당, 진보당, 더불어민주당은 `--exclude-csv 기존.csv`를 지원합니다. 기존 CSV에 본문이 있는 글은 건너뛰고, 새 글과 본문이 비어 있던 글만 받습니다. 기존 CSV에는 `url`과 `text` 컬럼(더불어민주당은 `board_name`도)이 있어야 합니다. 결과에는 새로 받은 행만 들어 있습니다.
- 더불어민주당은 기존 CSV에서 본문이 비어 있던 글만 다시 받는 `--refetch-empty 기존.csv`도 있습니다.
- 첨부파일이나 이미지에 글자가 없는 글은 다시 받아도 비어 있습니다. 이유는 `fail_reason`에 기록됩니다.

## 설치

```bash
pip install -r requirements.txt
```

본문이 첨부파일이나 이미지에만 있는 글을 읽으려면 아래 프로그램이 필요합니다. 없으면 해당 글은 본문 없음으로 기록됩니다.

| 용도 | 프로그램 |
|---|---|
| PDF 첨부 | poppler (`pdftotext`) |
| HWP 첨부 | pyhwp (`hwp5txt`, `requirements.txt`에 포함) |
| 이미지 본문 OCR | tesseract 와 한국어 데이터 (`kor`) |

## 출력 형식

새 수집기의 CSV는 공통 컬럼을 씁니다: `post_id, board_name, title, category, date(YYYY-MM-DD), pub_date, date_mismatch, writer, text, text_source, status, fail_reason, url, fetched_at`. `status`와 `fail_reason`으로 본문 수집 성공 여부와 실패 사유(한글)를 확인할 수 있습니다. 상세 HTML 원본은 `raw/`에 gzip으로 보존됩니다.

## 병합 도구

`tools/merge_parties.py`는 기존 CSV와 새 수집분을 정당별 파일로 합치는 관리용 스크립트입니다. 입력 파일 위치가 작업 폴더 구조에 맞춰져 있어 그대로 쓰기보다는 참고용입니다. 규칙: `date` 정규화(시각은 `datetime`), 본문이 있는 쪽 우선, 본문 가공 없음.

## 참고 문서

- [notebooks/reinforce_dp.ipynb](notebooks/reinforce_dp.ipynb): 더불어민주당 CSV 본문 보완·정제 노트북 (이전 작업)
- [docs/diagnosis.md](docs/diagnosis.md): 2026-11 업데이트 시점의 누락 진단 (과거 기록)

## 데이터 취급

- 원자료(raw)는 항상 보존하고, 정제·병합은 새 폴더에서 진행합니다.
- 공개 저장소로 전환하기 전에 수집 대상 사이트의 이용 약관과 데이터셋 접근 조건을 확인하세요.
