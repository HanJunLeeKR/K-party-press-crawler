#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
더불어민주당 크롤러 - 완전판
- requests 기반 (빠름)
- 500페이지마다 세션 재생성 (메모리 정리)
- 재시도 로직 (3회)
- 실패한 페이지 건너뛰기
- 빈 페이지 3회 연속 감지 → 자동 종료
"""

import os
import time
import re
import random
from datetime import datetime
import pandas as pd
from tqdm import tqdm
import requests
from bs4 import BeautifulSoup

def parse_date(date_str):
    """날짜 파싱 (여러 형식 지원)"""
    try:
        # 2026-02-03 형식
        return datetime.strptime(date_str.strip(), '%Y-%m-%d')
    except:
        try:
            # 2026-02-03 14:24:41 형식
            return datetime.strptime(date_str.strip().split()[0], '%Y-%m-%d')
        except:
            return None

def create_session():
    """세션 생성 (연결 재사용으로 속도 향상)"""
    session = requests.Session()
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'ko-KR,ko;q=0.9',
    })
    return session

def get_with_retry(session, url, max_retries=3, timeout=10):
    """재시도 로직"""
    for attempt in range(max_retries):
        try:
            response = session.get(url, timeout=timeout)
            response.raise_for_status()
            return response
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(1)
            else:
                return None
    return None

def collect_press_list(base_url, board_id, board_name,
                       start_date_str, end_date_str, max_pages=10000):

    start_date = datetime.strptime(start_date_str, '%Y-%m-%d')
    end_date = datetime.strptime(end_date_str, '%Y-%m-%d')

    data = []
    page_num = 0  # sno는 0부터 시작 (0, 20, 40, ...)
    session = None
    consecutive_errors = 0
    max_consecutive_errors = 10

    # 빈 페이지 감지
    empty_pages = 0
    max_empty_pages = 3

    print(f"\n▶ [{board_name}] 목록 수집 시작...")
    pbar = tqdm(desc=f"[{board_name}]", unit="페이지")

    while page_num <= max_pages * 20:  # sno는 20씩 증가
        # 500페이지마다 세션 재생성
        if (page_num // 20) % 500 == 0:
            if session:
                session.close()
                pbar.write(f"  🔄 sno={page_num}: 세션 재생성 (메모리 정리)")
            session = create_session()

        pbar.update(1)
        pbar.set_postfix({"수집": len(data), "sno": page_num})

        # 더불어민주당 URL 패턴
        # 첫 페이지는 sno 없이, 2페이지부터는 sno=20&par=&& 형식
        if page_num == 0:
            url = f"{base_url}/news/list.php?brd={board_id}"
        else:
            url = f"{base_url}/news/list.php?sno={page_num}&par=&&brd={board_id}"

        # 랜덤 대기 (0.5-1.5초)
        time.sleep(random.uniform(0.5, 1.5))

        # 재시도 로직으로 요청
        response = get_with_retry(session, url, max_retries=3, timeout=10)

        if not response:
            consecutive_errors += 1
            pbar.write(f"  ✗ sno={page_num} 실패 (연속 {consecutive_errors}회)")

            if consecutive_errors >= max_consecutive_errors:
                pbar.write(f"  ⚠ 연속 {consecutive_errors}회 실패. 수집 종료.")
                break

            page_num += 20
            continue

        # 성공하면 연속 오류 카운터 리셋
        consecutive_errors = 0

        soup = BeautifulSoup(response.text, 'html.parser')

        # board-item 찾기
        board_items = soup.find_all('div', {'class': 'board-item'})

        if not board_items or len(board_items) == 0:
            empty_pages += 1
            pbar.write(f"  ⚠ board-item 없음 (sno={page_num}) [빈 페이지 {empty_pages}/3]")

            if empty_pages >= max_empty_pages:
                pbar.write(f"  📭 연속 {empty_pages}페이지 비어있음 → 데이터 끝으로 판단, 수집 종료")
                break

            page_num += 20
            continue

        # 데이터가 있으면 빈 페이지 카운터 리셋
        empty_pages = 0

        # 데이터 추출
        stop_flag = False

        for item in board_items:
            try:
                # 제목과 URL 추출
                link_tag = item.find('a')
                if not link_tag:
                    continue

                title_span = link_tag.find('span')
                if not title_span:
                    continue

                title = title_span.get_text(strip=True).replace('\n', ' ')

                # URL 처리 (상대 경로를 절대 경로로)
                article_url = link_tag.get('href', '')
                if article_url.startswith('./'):
                    article_url = base_url + '/news/' + article_url[2:]
                elif not article_url.startswith('http'):
                    article_url = base_url + article_url

                # 카테고리 추출
                category_tag = item.find('p', {'class': 'category'})
                category = ""
                if category_tag:
                    category_span = category_tag.find('span')
                    if category_span:
                        category = category_span.get_text(strip=True)

                # 날짜 추출
                time_tag = item.find('time')
                if not time_tag:
                    continue

                date_str = time_tag.get('datetime', '')
                if not date_str:
                    date_str = time_tag.get_text(strip=True)

                article_date = parse_date(date_str)

                if not article_date:
                    continue
                if article_date < start_date:
                    stop_flag = True
                    break
                if article_date > end_date:
                    continue

                data.append({
                    'no': '',  # 민주당 사이트는 no가 명시되지 않음
                    'category': category,
                    'title': title,
                    'date': date_str.split()[0] if ' ' in date_str else date_str,
                    'url': article_url,
                    'board_name': board_name
                })
            except Exception as e:
                continue

        if stop_flag:
            pbar.write(f"  📅 시작 날짜({start_date_str}) 이전 도달 → 수집 종료")
            break

        page_num += 20

    if session:
        session.close()
    pbar.close()

    print(f"✓ [{board_name}] 총 {len(data)}개 수집 (마지막 sno: {page_num})")
    return data

def clean_text(text):
    text = text.replace('\xa0', '').replace('\u200b', '').replace('​', '')
    return text.strip()

def collect_press_detail(session, url):
    try:
        response = get_with_retry(session, url, max_retries=2, timeout=10)
        if not response:
            return {'text': "본문 조회 실패", 'writer': "", 'published_date': ""}

        soup = BeautifulSoup(response.text, 'html.parser')

        text_parts = []
        writer = ""
        published_date = ""

        # 게시일 추출
        date_li = soup.find('li', {'class': 'date'})
        if date_li:
            date_text = date_li.get_text(strip=True)
            # "게시일 : 2026-02-03 14:24:41" 형식에서 날짜 추출
            match = re.search(r'(\d{4}-\d{2}-\d{2})', date_text)
            if match:
                published_date = match.group(1)

        # 본문 추출
        contents_div = soup.find('div', {'class': 'board-view__contents'})

        if contents_div:
            # 모든 텍스트 요소 추출
            for element in contents_div.descendants:
                if element.name == 'p':
                    text = element.get_text(strip=True)
                    cleaned = clean_text(text)
                    if cleaned:
                        text_parts.append(cleaned)
                elif element.name == 'b':
                    # 굵은 글씨는 작성자일 가능성
                    text = element.get_text(strip=True)
                    cleaned = clean_text(text)
                    # 본문 마지막의 굵은 글씨를 작성자로 간주
                    if cleaned and not writer:
                        # "더불어민주당", "공보국" 등이 포함된 경우 작성자로 저장
                        if '민주당' in cleaned or '공보국' in cleaned or '대변인' in cleaned:
                            writer = cleaned

        return {
            'text': '\n'.join(text_parts),
            'writer': writer,
            'published_date': published_date
        }
    except Exception as e:
        return {'text': "본문 조회 오류", 'writer': "", 'published_date': ""}

def main():
    print("\n" + "="*60)
    print("더불어민주당 보도자료 수집 - 완전판")
    print("- requests 기반 (빠른 속도)")
    print("- 500페이지마다 세션 재생성")
    print("- 재시도 로직 (3회)")
    print("- 실패 페이지 건너뛰기")
    print("- ★ 빈 페이지 3회 연속 → 자동 종료")
    print("="*60)

    party_name = "더불어민주당"
    base_url = "https://theminjoo.kr/main/sub"

    party_boards = {
        "보도자료": "188",
        "논평_브리핑": "11",
        "모두발언": "230",
    }

    start_date = "2003-11-11"
    end_date = "2026-02-04"
    output_path = "/Users/hanjunlee/Downloads/더불어민주당_완전판"

    all_dfs = []

    for board_name, board_id in party_boards.items():
        # 목록 수집
        press_list = collect_press_list(
            base_url, board_id, board_name,
            start_date, end_date, max_pages=10000
        )

        if not press_list:
            continue

        # 상세 정보 수집
        print(f"\n▶ [{board_name}] 본문 수집 중... ({len(press_list)}개)")

        session = create_session()

        for i, item in enumerate(tqdm(press_list, desc=f"[{board_name}]", unit="건"), 1):
            # 500건마다 세션 재생성
            if i % 500 == 0:
                session.close()
                session = create_session()

            time.sleep(random.uniform(0.3, 0.8))
            details = collect_press_detail(session, item['url'])
            item['text'] = details['text']
            item['writer'] = details['writer']
            item['published_date'] = details['published_date']

        session.close()
        print(f"✓ [{board_name}] 완료")

        df = pd.DataFrame(press_list)
        df = df[['board_name', 'no', 'title', 'category', 'published_date', 'writer', 'text', 'url']]
        all_dfs.append(df)

    if not all_dfs:
        print("\n⚠️ 수집 데이터 없음")
        return

    # 저장
    combined_df = pd.concat(all_dfs, ignore_index=True)
    valid_df = combined_df[(combined_df['title'] != "") & (combined_df['text'] != "")]

    # published_date를 datetime 형식으로 변환 & 컬럼명 변경
    valid_df['published_date'] = pd.to_datetime(valid_df['published_date'], errors='coerce')
    valid_df = valid_df.rename(columns={'published_date': 'date'})

    os.makedirs(output_path, exist_ok=True)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')

    # CSV 저장
    filename_csv = f"{party_name}_보도자료_{start_date}_{end_date}_{timestamp}.csv"
    output_file_csv = os.path.join(output_path, filename_csv)
    valid_df.to_csv(output_file_csv, index=False, encoding='utf-8-sig')

    # Excel 저장 (날짜 형식 유지)
    filename_xlsx = f"{party_name}_보도자료_{start_date}_{end_date}_{timestamp}.xlsx"
    output_file_xlsx = os.path.join(output_path, filename_xlsx)
    valid_df.to_excel(output_file_xlsx, index=False, engine='openpyxl')

    print("\n" + "="*60)
    print(f"✓ 총 {len(valid_df)}개 저장")
    for board_name in party_boards.keys():
        count = len(valid_df[valid_df['board_name'] == board_name])
        print(f"  • {board_name}: {count}개")
    print(f"✓ CSV 저장: {output_file_csv}")
    print(f"✓ Excel 저장: {output_file_xlsx} (날짜 형식 유지)")
    print("="*60)
    print("\n★ 완료! ★\n")

if __name__ == "__main__":
    main()
