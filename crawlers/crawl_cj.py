#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
조국혁신당 크롤러 - 완전판
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

def collect_press_list(base_url, board_path, board_name,
                       start_date_str, end_date_str, max_pages=10000):

    start_date = datetime.strptime(start_date_str, '%Y-%m-%d')
    end_date = datetime.strptime(end_date_str, '%Y-%m-%d')

    data = []
    page_num = 1
    session = None
    consecutive_errors = 0
    max_consecutive_errors = 10

    # 빈 페이지 감지
    empty_pages = 0
    max_empty_pages = 3

    # 중복 감지 (게시글 번호 추적)
    seen_article_numbers = set()
    duplicate_pages = 0
    max_duplicate_pages = 2

    print(f"\n▶ [{board_name}] 목록 수집 시작...")
    pbar = tqdm(desc=f"[{board_name}]", unit="페이지")

    while page_num <= max_pages:
        # 500페이지마다 세션 재생성
        if page_num % 500 == 0:
            if session:
                session.close()
                pbar.write(f"  🔄 page={page_num}: 세션 재생성 (메모리 정리)")
            session = create_session()

        if not session:
            session = create_session()

        pbar.update(1)
        pbar.set_postfix({"수집": len(data), "page": page_num})

        # 조국혁신당 URL 패턴
        # 1페이지는 page 파라미터 없음, 2페이지부터는 ?page=2 형식
        if page_num == 1:
            url = f"{base_url}/{board_path}"
        else:
            url = f"{base_url}/{board_path}?page={page_num}"

        # 랜덤 대기 (0.5-1.5초)
        time.sleep(random.uniform(0.5, 1.5))

        # 재시도 로직으로 요청
        response = get_with_retry(session, url, max_retries=3, timeout=10)

        if not response:
            consecutive_errors += 1
            pbar.write(f"  ✗ page={page_num} 실패 (연속 {consecutive_errors}회)")

            if consecutive_errors >= max_consecutive_errors:
                pbar.write(f"  ⚠ 연속 {consecutive_errors}회 실패. 수집 종료.")
                break

            page_num += 1
            continue

        # 성공하면 연속 오류 카운터 리셋
        consecutive_errors = 0

        soup = BeautifulSoup(response.text, 'html.parser')

        # <li class="td date"> 찾기 (각 게시글이 li 구조)
        # 실제로는 전체 구조를 확인해야 하지만, date li를 기준으로 찾음
        date_items = soup.find_all('li', {'class': 'td date'})

        if not date_items or len(date_items) == 0:
            empty_pages += 1
            pbar.write(f"  ⚠ 게시글 없음 (page={page_num}) [빈 페이지 {empty_pages}/3]")

            if empty_pages >= max_empty_pages:
                pbar.write(f"  📭 연속 {empty_pages}페이지 비어있음 → 데이터 끝으로 판단, 수집 종료")
                break

            page_num += 1
            continue

        # 데이터가 있으면 빈 페이지 카운터 리셋
        empty_pages = 0

        # 각 게시글 항목 추출
        # 구조: <ul> 안에 <li class="td category">, <li class="td"><a>, <li class="td date"> 순서
        # 하지만 정확한 구조를 모르므로, 모든 게시글 항목을 찾아야 함

        # 더 정확하게 찾기 위해 <a href="/news/..."> 패턴으로 찾기
        article_links = soup.find_all('a', href=re.compile(f'^/news/{board_path}/'))

        if not article_links:
            empty_pages += 1
            pbar.write(f"  ⚠ 게시글 링크 없음 (page={page_num}) [빈 페이지 {empty_pages}/3]")

            if empty_pages >= max_empty_pages:
                pbar.write(f"  📭 연속 {empty_pages}페이지 비어있음 → 데이터 끝으로 판단, 수집 종료")
                break

            page_num += 1
            continue

        # 데이터가 있으면 빈 페이지 카운터 리셋
        empty_pages = 0

        # 이 페이지에서 새로운 게시글이 있는지 체크
        new_articles_in_page = 0

        stop_flag = False

        for link in article_links:
            try:
                # 제목과 URL 추출
                title = link.get_text(strip=True).replace('\n', ' ')
                article_url = link.get('href', '')

                # 상대 경로를 절대 경로로
                if article_url.startswith('/'):
                    article_url = base_url + article_url

                # 중복 체크 (URL 기반)
                if article_url in seen_article_numbers:
                    continue  # 이미 수집한 게시글은 건너뛰기

                # 같은 부모 요소에서 날짜와 카테고리 찾기
                # link의 부모를 찾아 올라가며 date와 category 찾기
                parent = link.find_parent('ul')
                if not parent:
                    parent = link.find_parent('li')
                    if parent:
                        parent = parent.find_parent('ul')

                date_str = ""
                category = ""

                if parent:
                    # 같은 ul 내에서 date와 category 찾기
                    date_li = parent.find('li', {'class': 'td date'})
                    if date_li:
                        date_str = date_li.get_text(strip=True)

                    category_li = parent.find('li', {'class': 'td category'})
                    if category_li:
                        category = category_li.get_text(strip=True)

                if not date_str:
                    continue

                article_date = parse_date(date_str)

                if not article_date:
                    continue
                if article_date < start_date:
                    stop_flag = True
                    break
                if article_date > end_date:
                    continue

                data.append({
                    'no': '',  # 조국혁신당은 no가 없음 (URL에서 ID 추출 가능)
                    'category': category,
                    'title': title,
                    'date': date_str,
                    'url': article_url,
                    'board_name': board_name
                })

                # 새 게시글로 추적 (URL 기반)
                seen_article_numbers.add(article_url)
                new_articles_in_page += 1

            except Exception as e:
                continue

        if stop_flag:
            pbar.write(f"  📅 시작 날짜({start_date_str}) 이전 도달 → 수집 종료")
            break

        # 중복 페이지 감지 (새 게시글이 하나도 없으면 중복)
        if new_articles_in_page == 0:
            duplicate_pages += 1
            pbar.write(f"  ⚠ 중복 페이지 감지 (page={page_num}) [연속 {duplicate_pages}회]")

            if duplicate_pages >= max_duplicate_pages:
                pbar.write(f"  🔁 연속 {duplicate_pages}페이지 중복 → 마지막 페이지로 판단, 수집 종료")
                break
        else:
            duplicate_pages = 0  # 새 게시글이 있으면 카운터 리셋

        page_num += 1

    if session:
        session.close()
    pbar.close()

    print(f"✓ [{board_name}] 총 {len(data)}개 수집 (마지막 page: {page_num})")
    return data

def clean_text(text):
    text = text.replace('\xa0', '').replace('\u200b', '').replace('​', '')
    return text.strip()

def collect_press_detail(session, url):
    try:
        response = get_with_retry(session, url, max_retries=2, timeout=10)
        if not response:
            return {'text': "본문 조회 실패", 'writer': ""}

        soup = BeautifulSoup(response.text, 'html.parser')

        text_parts = []
        writer = ""

        # 본문 추출: <div class="editor ck-content">
        contents_div = soup.find('div', {'class': 'editor ck-content'})

        if contents_div:
            # 모든 <p> 태그 추출
            paragraphs = contents_div.find_all('p')

            for p in paragraphs:
                text = p.get_text(strip=True)
                cleaned = clean_text(text)
                if cleaned:
                    text_parts.append(cleaned)

            # 작성자 추출: 마지막 <p> 태그에서 "조국혁신당"이 포함된 경우
            if paragraphs:
                for p in reversed(paragraphs):
                    text = p.get_text(strip=True)
                    cleaned = clean_text(text)
                    if '조국혁신당' in cleaned or '대변인' in cleaned or '위원회' in cleaned:
                        writer = cleaned
                        break

        return {
            'text': '\n'.join(text_parts),
            'writer': writer
        }
    except Exception as e:
        return {'text': "본문 조회 오류", 'writer': ""}

def main():
    print("\n" + "="*60)
    print("조국혁신당 보도자료 수집 - 완전판")
    print("- requests 기반 (빠른 속도)")
    print("- 500페이지마다 세션 재생성")
    print("- 재시도 로직 (3회)")
    print("- 실패 페이지 건너뛰기")
    print("- ★ 빈 페이지 3회 연속 → 자동 종료")
    print("="*60)

    party_name = "조국혁신당"
    base_url = "https://rebuildingkoreaparty.kr"

    party_boards = {
        "기자회견문": "news/press-conference",
        "논평브리핑": "news/commentary-briefing",
        "보도자료": "news/press-release",
    }

    start_date = "2024-03-04"  # 조국혁신당 창당일
    end_date = "2026-02-05"
    output_path = "/Users/hanjunlee/Downloads/조국혁신당_완전판"

    all_dfs = []

    for board_name, board_path in party_boards.items():
        # 목록 수집
        press_list = collect_press_list(
            base_url, board_path, board_name,
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

        session.close()
        print(f"✓ [{board_name}] 완료")

        df = pd.DataFrame(press_list)
        df = df[['board_name', 'no', 'title', 'category', 'date', 'writer', 'text', 'url']]
        all_dfs.append(df)

    if not all_dfs:
        print("\n⚠️ 수집 데이터 없음")
        return

    # 저장
    combined_df = pd.concat(all_dfs, ignore_index=True)
    valid_df = combined_df[(combined_df['title'] != "") & (combined_df['text'] != "")]

    os.makedirs(output_path, exist_ok=True)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    filename = f"{party_name}_보도자료_{start_date}_{end_date}_{timestamp}.csv"
    output_file = os.path.join(output_path, filename)

    valid_df.to_csv(output_file, index=False, encoding='utf-8-sig')

    print("\n" + "="*60)
    print(f"✓ 총 {len(valid_df)}개 저장")
    for board_name in party_boards.keys():
        count = len(valid_df[valid_df['board_name'] == board_name])
        print(f"  • {board_name}: {count}개")
    print(f"✓ 저장: {output_file}")
    print("="*60)
    print("\n★ 완료! ★\n")

if __name__ == "__main__":
    main()
