#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
국민의힘 크롤러 - 완전판 v2
- 원래 requests 기반 (빠름)
- 500페이지마다 세션 재생성 (메모리 정리)
- 재시도 로직 (3회)
- 실패한 페이지 건너뛰기
- ★ 빈 페이지 3회 연속 감지 → 자동 종료 (NEW!)
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
    try:
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

def collect_press_list(base_url, board_id, board_name,
                       start_date_str, end_date_str, max_pages=10000):

    start_date = datetime.strptime(start_date_str, '%Y-%m-%d')
    end_date = datetime.strptime(end_date_str, '%Y-%m-%d')

    data = []
    page_num = 1
    session = None
    consecutive_errors = 0
    max_consecutive_errors = 10

    # ★ NEW: 빈 페이지 감지
    empty_pages = 0
    max_empty_pages = 3  # 연속 3페이지 비어있으면 종료

    print(f"\n▶ [{board_name}] 목록 수집 시작...")
    pbar = tqdm(desc=f"[{board_name}]", unit="페이지")

    while page_num <= max_pages:
        # 500페이지마다 세션 재생성
        if (page_num - 1) % 500 == 0:
            if session:
                session.close()
                pbar.write(f"  🔄 {page_num}페이지: 세션 재생성 (메모리 정리)")
            session = create_session()

        pbar.update(1)
        pbar.set_postfix({"수집": len(data), "페이지": page_num})

        url = f"{base_url}/news/comment/{board_id}?page={page_num}"

        # 랜덤 대기 (0.5-1.5초)
        time.sleep(random.uniform(0.5, 1.5))

        # 재시도 로직으로 요청
        response = get_with_retry(session, url, max_retries=3, timeout=10)

        if not response:
            consecutive_errors += 1
            pbar.write(f"  ✗ 페이지 {page_num} 실패 (연속 {consecutive_errors}회)")

            if consecutive_errors >= max_consecutive_errors:
                pbar.write(f"  ⚠ 연속 {consecutive_errors}회 실패. 수집 종료.")
                break

            page_num += 1
            continue

        # 성공하면 연속 오류 카운터 리셋
        consecutive_errors = 0

        soup = BeautifulSoup(response.text, 'html.parser')

        table_div = soup.find('div', {'class': 'board-tbl'})
        if not table_div:
            empty_pages += 1
            pbar.write(f"  ⚠ board-tbl 없음 (페이지 {page_num}) [빈 페이지 {empty_pages}/3]")

            if empty_pages >= max_empty_pages:
                pbar.write(f"  📭 연속 {empty_pages}페이지 비어있음 → 데이터 끝으로 판단, 수집 종료")
                break

            page_num += 1
            continue

        tbody = table_div.find('tbody')
        if not tbody:
            empty_pages += 1
            pbar.write(f"  ⚠ tbody 없음 (페이지 {page_num}) [빈 페이지 {empty_pages}/3]")

            if empty_pages >= max_empty_pages:
                pbar.write(f"  📭 연속 {empty_pages}페이지 비어있음 → 데이터 끝으로 판단, 수집 종료")
                break

            page_num += 1
            continue

        rows = tbody.find_all('tr')
        if not rows or len(rows) == 0:
            empty_pages += 1
            pbar.write(f"  ⚠ 게시글 없음 (페이지 {page_num}) [빈 페이지 {empty_pages}/3]")

            if empty_pages >= max_empty_pages:
                pbar.write(f"  📭 연속 {empty_pages}페이지 비어있음 → 데이터 끝으로 판단, 수집 종료")
                break

            page_num += 1
            continue

        # ★ 데이터가 있으면 빈 페이지 카운터 리셋
        empty_pages = 0

        # 데이터 추출
        stop_flag = False

        for row in rows:
            cols = row.find_all('td')
            if len(cols) < 3:
                continue

            try:
                no_td = row.find('td', {'class': 'no'})
                class_td = row.find('td', {'class': 'class'})

                no = no_td.get_text(strip=True) if no_td else cols[0].get_text(strip=True)
                section = class_td.get_text(strip=True) if class_td else cols[1].get_text(strip=True)

                link_tag = row.find('a')
                if not link_tag:
                    continue

                title = link_tag.get_text(strip=True).replace('\n', ' ')
                article_url = base_url + link_tag['href']

                date_str = ""
                if len(cols) >= 4:
                    date_str = cols[3].get_text(strip=True)

                if not date_str or not re.match(r'\d{4}-\d{2}-\d{2}', date_str):
                    dd_date = row.find('dd', {'class': 'date'})
                    if dd_date:
                        span = dd_date.find('span')
                        if span:
                            span.decompose()
                        date_str = dd_date.get_text(strip=True)

                article_date = parse_date(date_str)

                if not article_date:
                    continue
                if article_date < start_date:
                    stop_flag = True
                    break
                if article_date > end_date:
                    continue

                data.append({
                    'no': no,
                    'section': section,
                    'title': title,
                    'date': date_str,
                    'url': article_url,
                    'board_name': board_name
                })
            except:
                continue

        if stop_flag:
            pbar.write(f"  📅 시작 날짜({start_date_str}) 이전 도달 → 수집 종료")
            break

        page_num += 1

    if session:
        session.close()
    pbar.close()

    print(f"✓ [{board_name}] 총 {len(data)}개 수집 (마지막 페이지: {page_num - 1})")
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

        conts_tag = soup.select_one('dd.conts')

        if conts_tag:
            hwp_div = conts_tag.find('div', {'id': 'hwpEditorBoardContent'})
            if hwp_div:
                hwp_div.decompose()

            p_tags = conts_tag.find_all('p')

            for p in p_tags:
                style = p.get('style', '')
                is_center = 'text-align:center' in style.replace(' ', '').lower()

                raw_text = p.get_text(strip=True)
                cleaned_text = clean_text(raw_text)

                if not cleaned_text:
                    continue

                if is_center:
                    if not re.match(r'\d{4}\.\s*\d{1,2}\.\s*\d{1,2}', cleaned_text):
                        writer = cleaned_text
                else:
                    text_parts.append(cleaned_text)

        return {'text': '\n'.join(text_parts), 'writer': writer}
    except:
        return {'text': "본문 조회 오류", 'writer': ""}

def main():
    print("\n" + "="*60)
    print("국민의힘 보도자료 수집 - 완전판 v2")
    print("- requests 기반 (빠른 속도)")
    print("- 500페이지마다 세션 재생성")
    print("- 재시도 로직 (3회)")
    print("- 실패 페이지 건너뛰기")
    print("- ★ 빈 페이지 3회 연속 → 자동 종료")
    print("="*60)

    party_name = "국민의힘"
    base_url = "https://www.peoplepowerparty.kr"

    party_boards = {
        "대변인_논평보도자료": "BBSDD0001",
        "원내_보도자료": "BBSDD0002",
        "미디어특위_보도자료": "BBSDD0042",
    }

    start_date = "2000-03-10"
    end_date = "2026-02-03"
    output_path = "/Users/hanjunlee/Downloads/국민의힘_완전판_v2"

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

        session.close()
        print(f"✓ [{board_name}] 완료")

        df = pd.DataFrame(press_list)
        df = df[['board_name', 'no', 'title', 'section', 'date', 'writer', 'text', 'url']]
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
