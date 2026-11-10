# party-press-crawler

한국 원내 정당의 보도자료·성명을 수집하는 크롤러 모음입니다. 결과 데이터셋은 Hugging Face에 있으며 이 저장소에는 코드와 문서만 둡니다.

- 데이터셋: https://huggingface.co/datasets/hanjunlee/KoreanPartyPressRelease (gated)

## 구성

| 경로 | 내용 |
|---|---|
| `crawlers/crawl_ppp.py` | 국민의힘 (대변인 논평, 원내 보도자료, 미디어특위) |
| `crawlers/crawl_dp.py` | 더불어민주당 (보도자료, 논평·브리핑, 모두발언) |
| `crawlers/crawl_cj.py` | 조국혁신당 (기자회견문, 논평브리핑, 보도자료) |
| `notebooks/reinforce_dp.ipynb` | 민주당 CSV 본문 보완 및 정제 노트북 |
| `docs/diagnosis.md` | 2026-11 업데이트 후 누락 진단과 해결 계획 |

개혁신당·기본소득당 크롤러는 아직 이 저장소에 없습니다.

## 실행 전 확인

- 크롤러의 출력 경로(`/Users/hanjunlee/Downloads/...`)는 본인 환경에 맞게 바꿔 주세요.
- 데이터를 HF에서 받을 때는 토큰을 환경변수로 넣습니다: `export HF_TOKEN=...` (코드에 토큰을 쓰지 마세요).
- 의존성: `pandas tqdm requests beautifulsoup4 openpyxl` (노트북은 `newspaper3k` 추가).

## 알려진 문제

자세한 원인과 수정 계획은 [docs/diagnosis.md](docs/diagnosis.md)를 보세요. 요약하면 조국혁신당 목록 수집이 현재 막혀 있고, 민주당은 날짜 속성 버그와 쿠키 리다이렉트 처리가 필요하며, 모든 크롤러에서 실패가 조용히 누락되는 구조를 고쳐야 합니다.

## 데이터 취급

- 원자료(raw)는 항상 보존하고, 정제·병합은 새 폴더에서 진행합니다.
- 공개 저장소로 전환하기 전에 수집 대상 사이트의 이용 약관과 데이터셋 접근 조건을 확인하세요.
