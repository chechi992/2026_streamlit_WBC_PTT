# -*- coding: utf-8 -*-
"""
2026 WBC PTT Baseball 板 社群輿論互動式儀表板（進階版）
========================================================
工具展示論文用 Streamlit App

新增功能（相對於 v1 骨架）：
1. 爬蟲時間範圍鎖定 WBC 台灣隊預賽期間（3/4-3/10），並加上 WBC 關鍵字過濾
2. 球員名單更新為 2026 WBC 中華隊實際出賽名單
3. 載入外部球員基本資料（players_info.csv）與賽事事件（wbc_events.csv）
4. 五個 Tab 分頁：
   - 總覽 Dashboard
   - 場次分析（選一場比賽看當天討論）
   - 球員個人頁（輿論 vs 實際打擊數據）
   - 球員對比（兩位並排 PK，加入動態泡泡縮放避免破版）
   - 賽事時間軸（比賽結果疊在情緒走勢上）

執行方式：
    pip install streamlit pandas matplotlib beautifulsoup4 requests
    streamlit run wbc_dashboard_v2.py
"""

import ast
import os
import random
import time
from datetime import datetime, timedelta

import pandas as pd
import streamlit as st
import matplotlib.pyplot as plt
import matplotlib as mpl

# ──────────────────────────────────────────────
# 0. 全域設定
# ──────────────────────────────────────────────
st.set_page_config(page_title="WBC PTT 輿論儀表板", layout="wide")

# 2026 WBC 中華隊實際出賽名單（依 WBC 官方統計頁面 + 運動視界 + 報導者）
PLAYERS = [
    # 打者(全部 13 位有官方打擊數據的)
    "陳傑憲", "張育成", "林安可", "江坤宇", "鄭宗哲",
    "吉力吉撈", "林家正", "陳晨威", "宋晟睿", "費爾柴德",
    "吳念庭", "蔣少宏", "林子偉",
    # 主要投手(被討論的)
    "古林睿煬", "徐若熙", "林維恩", "林昱珉", "陳冠宇",
]

# 名稱別名表（標題裡可能用簡稱、英文名）
PLAYER_ALIASES = {
    "陳傑憲": ["陳傑憲", "傑憲"],
    "張育成": ["張育成", "育成"],
    "費爾柴德": ["費爾柴德", "費仔", "Fairchild"],
    "吉力吉撈": ["吉力吉撈", "鞏冠", "吉力"],
    "江坤宇": ["江坤宇", "坤宇"],
    "古林睿煬": ["古林睿煬", "古林"],
    "徐若熙": ["徐若熙", "若熙"],
    "陳冠宇": ["陳冠宇", "冠宇"],
}

# WBC 時間範圍與中華隊賽程
# 【設定區間】精準鎖定台灣隊賽事期間 3/4 到 3/10
WBC_START = datetime(2026, 3, 4)
WBC_END = datetime(2026, 3, 10)

# 中華隊賽程（用於場次分析）
GAMES = [
    {"date": "2026-03-05", "opponent": "澳洲", "score": "0-3", "tw_win": False},
    {"date": "2026-03-06", "opponent": "日本", "score": "0-13", "tw_win": False},
    {"date": "2026-03-07", "opponent": "捷克", "score": "14-0", "tw_win": True},
    {"date": "2026-03-08", "opponent": "韓國", "score": "5-4", "tw_win": True},
]

# WBC 相關關鍵字（用於過濾文章標題）
WBC_KEYWORDS = ["WBC", "經典賽", "中華隊", "台灣隊", "Team Taiwan",
                "中華", "澳洲", "捷克", "韓國", "日本"]

# 情緒詞典（可擴充）
POSITIVE_WORDS = ["爽", "強", "猛", "穩", "神", "讚", "厲害", "好棒", "精彩",
                  "加油", "感動", "屌", "讚讚", "GG好球", "霸氣"]
NEGATIVE_WORDS = ["爛", "雷", "廢", "炸", "崩", "差", "可惜", "失誤", "丟臉",
                  "難看", "心痛", "唉", "嘆", "傻眼", "笑死"]

CSV_PATH = "ptt_baseball_wbc_raw.csv"
PLAYERS_INFO_PATH = "players_info.csv"
EVENTS_PATH = "wbc_events.csv"


# ──────────────────────────────────────────────
# 1. 中文字體設定
# ──────────────────────────────────────────────
def setup_chinese_font():
    import matplotlib.font_manager as fm

    candidates = [
        "Noto Sans CJK TC", "Noto Sans CJK JP", "Noto Sans CJK SC",
        "Microsoft JhengHei", "PingFang TC", "Heiti TC", "SimHei",
        "WenQuanYi Zen Hei", "Arial Unicode MS",
    ]
    available = {f.name for f in fm.fontManager.ttflist}
    for name in candidates:
        if name in available:
            mpl.rc("font", family=name)
            mpl.rcParams["axes.unicode_minus"] = False
            return name
    for root, _, files in os.walk("/usr/share/fonts"):
        for f in files:
            if "NotoSansCJK" in f and f.endswith((".ttc", ".ttf", ".otf")):
                path = os.path.join(root, f)
                fm.fontManager.addfont(path)
                prop = fm.FontProperties(fname=path)
                mpl.rc("font", family=prop.get_name())
                mpl.rcParams["axes.unicode_minus"] = False
                return prop.get_name()
    mpl.rcParams["axes.unicode_minus"] = False
    return None


FONT_NAME = setup_chinese_font()


# ──────────────────────────────────────────────
# 2. PTT 爬蟲
# ──────────────────────────────────────────────
import requests
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor
from requests.adapters import HTTPAdapter

try:
    from urllib3.util.retry import Retry
except ImportError:
    from requests.packages.urllib3.util.retry import Retry


class PTTScraper:
    base_url = "https://www.ptt.cc"
    MAX_WORKERS = 6           # 並行數,避免被 PTT 限流
    PER_REQUEST_SLEEP = 0.15  # 每次抓取後微停

    def __init__(self, board):
        self.url = self.base_url + f"/bbs/{board}/index.html"
        self.session = self._build_session()

    @staticmethod
    def _build_session():
        """建立有 retry 機制的 Session,自動處理瞬斷的 SSL/連線錯誤。"""
        s = requests.Session()
        retry = Retry(
            total=5, connect=5, read=5,
            backoff_factor=0.8,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
            raise_on_status=False,
        )
        adapter = HTTPAdapter(
            max_retries=retry, pool_connections=10, pool_maxsize=10)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        s.headers.update({
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/120.0.0.0 Safari/537.36"),
        })
        s.cookies.set("over18", "1", domain="www.ptt.cc")
        return s

    def get_push(self, push):
        try:
            if push.find("span", class_="push-tag") is None:
                return dict()
            return {
                "Tag": push.find("span", class_="push-tag").text.strip(),
                "Userid": push.find("span", class_="push-userid").text.strip(),
                "Content": push.find("span", class_="push-content").text.strip().lstrip(":"),
                "Ipdatetime": push.find("span", class_="push-ipdatetime").text.strip(),
            }
        except Exception:
            return dict()

    def get_soup(self, url):
        """有手動 retry,徹底失敗才回 None。"""
        for attempt in range(3):
            try:
                resp = self.session.get(url, timeout=15)
                if resp.status_code == 200:
                    time.sleep(self.PER_REQUEST_SLEEP)
                    return BeautifulSoup(resp.text, "html.parser")
            except (requests.exceptions.SSLError,
                    requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout,
                    requests.exceptions.ChunkedEncodingError):
                if attempt < 2:
                    time.sleep(1.5 * (attempt + 1))
                    continue
            except Exception:
                pass
        return None

    def fetch_post(self, url):
        """抓單篇,失敗回 None,不會拖垮整批。"""
        try:
            soup = self.get_soup(self.base_url + url)
            if soup is None:
                return None
            author = title = date = content = None
            if soup.find(id="main-content"):
                content = soup.find(id="main-content").text.split("※ 發信站")[0]
            metas = soup.find_all(class_="article-meta-value")
            if metas:
                author = metas[0].text if len(metas) > 0 else None
                title = metas[-2].text if len(metas) >= 2 else None
                try:
                    date = datetime.strptime(metas[-1].text,
                                             "%a %b %d %H:%M:%S %Y")
                except Exception:
                    date = None
            pushes = soup.find_all("div", class_="push")
            # 推文解析改為單執行緒,避免內外巢狀並行爆量
            push_list = [self.get_push(p) for p in pushes]
            return {"Title": title, "Author": author, "Date": date,
                    "Content": content, "Link": url, "Pushes": push_list}
        except Exception:
            return None

    def find_latest_index(self):
        """抓 index.html,從『‹ 上頁』連結解析出最新 index 編號。失敗回 None。"""
        import re
        soup = self.get_soup(f"{self.base_url}/bbs/Baseball/index.html")
        if soup is None:
            return None
        try:
            prev_link = soup.find("a", string="‹ 上頁")["href"]
            m = re.search(r"index(\d+)\.html", prev_link)
            if m:
                return int(m.group(1)) + 1
        except Exception:
            pass
        return None

    def _get_page_newest_date(self, index_num, year):
        """抓 indexN.html,回傳該頁最新一篇文章的日期。"""
        url = f"{self.base_url}/bbs/Baseball/index{index_num}.html"
        soup = self.get_soup(url)
        if soup is None:
            return None
        entries = list(soup.select(".r-ent"))
        for entry in reversed(entries):  # 該頁由舊到新排序,reversed 從最新看起
            try:
                if entry.find("div", "title").a is None:
                    continue
                date_str = entry.select(".date")[0].text.strip()
                return datetime.strptime(f"{year}/{date_str}", "%Y/%m/%d")
            except Exception:
                continue
        return None

    def smart_find_start_page(self, start_date, end_date, progress_callback=None):
        """智能尋找適合的起始 index 編號,確保抓到 [start_date, end_date] 範圍。"""
        latest = self.find_latest_index()
        if latest is None:
            return None

        # 初始估算:每天 8 頁,從 end_date 往新偏 30 頁緩衝
        days_back = (datetime.now() - end_date).days
        estimated = max(1, latest - days_back * 8 - 30)

        for attempt in range(6):
            page_date = self._get_page_newest_date(estimated, end_date.year)
            if progress_callback:
                progress_callback(attempt + 1, estimated, page_date)
            if page_date is None:
                return estimated

            # 該頁最新文章 < start_date → 整頁太舊,往新跳
            if page_date < start_date:
                diff_days = (start_date - page_date).days
                estimated += max(diff_days * 8 + 20, 50)
            # 該頁最新 > end_date + 30 天 → 太新太多,往舊跳
            elif page_date > end_date + timedelta(days=30):
                diff_days = (page_date - end_date).days
                estimated = max(1, estimated - diff_days * 8)
            else:
                # 在 [start_date, end_date + 30] 之間,從這裡爬剛好
                return estimated

            estimated = min(estimated, latest)  # 不超過最新

        return estimated

    def get_data_in_range(self, start_date, end_date, max_posts=300,
                          keyword_filter=None, progress=None):
        """從最新往回爬,抓 [start_date, end_date] 之間且匹配關鍵字的文章。"""
        data = []
        start_date = start_date.replace(hour=0, minute=0, second=0, microsecond=0)
        end_date_excl = end_date.replace(hour=23, minute=59, second=59)
        consecutive_fails = 0
        pages_visited = 0
        current_page_date = None

        while True:
            soup = self.get_soup(self.url)
            if soup is None:
                consecutive_fails += 1
                if consecutive_fails >= 3:
                    return data
                time.sleep(2)
                continue
            consecutive_fails = 0
            pages_visited += 1

            div_element = soup.find("div", {"class": "r-list-sep"})
            entries = soup.select(".r-ent") if div_element is None else [
                e for e in div_element.previous_siblings
                if e.name == "div" and "r-ent" in e.get("class", [])
            ]
            links_to_fetch = []
            reached_start = False
            for entry in reversed(entries):
                try:
                    if entry.find("div", "title").a is None:
                        continue
                    date_str = entry.select(".date")[0].text.strip()
                    # 顯式拼上年份,避免 Python 3.14+ 對缺年份的 DeprecationWarning
                    post_date = datetime.strptime(
                        f"{start_date.year}/{date_str}", "%Y/%m/%d")
                    current_page_date = post_date  # 記錄當前頁日期
                    if post_date < start_date:
                        reached_start = True
                        break
                    if post_date > end_date_excl:
                        continue
                    title_text = entry.select(".title a")[0].text
                    if keyword_filter and not any(k in title_text for k in keyword_filter):
                        continue
                    links_to_fetch.append(entry.select(".title a")[0]["href"])
                except Exception:
                    pass

            if links_to_fetch:
                with ThreadPoolExecutor(max_workers=self.MAX_WORKERS) as executor:
                    page_data = list(executor.map(self.fetch_post, links_to_fetch))
                for item in page_data:
                    if item is None or item.get("Date") is None:
                        continue
                    if start_date <= item["Date"] <= end_date_excl:
                        data.append(item)

            if progress:
                progress(len(data), max_posts,
                         pages=pages_visited,
                         current_date=current_page_date)
            if reached_start or len(data) >= max_posts:
                return data[:max_posts]
            try:
                prev_link = soup.find("a", string="‹ 上頁")["href"]
                self.url = self.base_url + prev_link
            except (TypeError, KeyError):
                return data


# ──────────────────────────────────────────────
# 3. 假資料產生器（demo / 後備方案）
# ──────────────────────────────────────────────
def generate_fake_data(n_posts=200, seed=42):
    random.seed(seed)
    rows = []
    title_templates = [
        "[討論] {p} 今天表現如何", "[分享] WBC {p} 關鍵一打",
        "[新聞] {p} 入選國家隊名單", "[心得] 看完 {p} 守備有感",
        "[問題] {p} 狀態是不是回來了", "Re: [討論] {p} vs {p2} 誰比較穩",
        "[轉播] 中華隊 vs {opp}", "[討論] {opp}場 {p} 表現",
        "[爆卦] {p} 受傷了嗎", "[Live] WBC C組 中華 vs {opp}",
    ]
    opponents = ["澳洲", "日本", "捷克", "韓國"]
    for _ in range(n_posts):
        p = random.choice(PLAYERS)
        p2 = random.choice([x for x in PLAYERS if x != p])
        opp = random.choice(opponents)
        tmpl = random.choice(title_templates)
        title = tmpl.format(p=p, p2=p2, opp=opp)
        # 日期集中在 3/5-3/10,3/14, 3/17 給少量
        if random.random() < 0.8:
            day = random.randint(5, 10)
        else:
            day = random.choice([14, 15, 16, 17])
        date = datetime(2026, 3, day, random.randint(0, 23), random.randint(0, 59))
        n_push = random.randint(5, 80)
        pushes = []
        # 結合對戰結果調整情緒分布
        win_game = opp in ["捷克", "韓國"]
        for _ in range(n_push):
            r = random.random()
            tag = "推" if r < (0.7 if win_game else 0.45) else \
                  ("噓" if r < (0.85 if win_game else 0.75) else "→")
            content = ""
            if random.random() < 0.4:
                pool = POSITIVE_WORDS if tag == "推" else NEGATIVE_WORDS
                content = random.choice(pool)
            content += "".join(random.choices("好啊喔哈這就是有夠真的", k=random.randint(2, 6)))
            pushes.append({"Tag": tag, "Userid": f"user{random.randint(1, 999)}",
                           "Content": content, "Ipdatetime": ""})
        rows.append({"Title": title, "Author": f"poster{random.randint(1, 99)}",
                     "Date": date, "Content": "（內文略）",
                     "Link": "/bbs/Baseball/M.fake.html", "Pushes": pushes})
    return pd.DataFrame(rows)


# ──────────────────────────────────────────────
# 4. 資料處理
# ──────────────────────────────────────────────
@st.cache_data
def load_raw_csv(path):
    try:
        df = pd.read_csv(path)
        if df.empty or "Pushes" not in df.columns:
            return pd.DataFrame(columns=["Title", "Author", "Date", "Content", "Link", "Pushes"])
        df["Pushes"] = df["Pushes"].apply(
            lambda v: ast.literal_eval(v) if isinstance(v, str) else [])
        df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
        return df
    except (pd.errors.EmptyDataError, pd.errors.ParserError):
        return pd.DataFrame(columns=["Title", "Author", "Date", "Content", "Link", "Pushes"])


@st.cache_data
def load_players_info(path):
    if not os.path.exists(path):
        return pd.DataFrame({"姓名": PLAYERS})
    try:
        return pd.read_csv(path)
    except Exception as e:
        try:
            df = pd.read_csv(path, engine="python", on_bad_lines="skip")
            st.warning(f"⚠️ {path} 有格式問題,已跳過壞掉的行。錯誤:{e}")
            return df
        except Exception:
            st.warning(f"⚠️ 無法讀取 {path},改用內建球員名單。")
            return pd.DataFrame({"姓名": PLAYERS})


@st.cache_data
def load_events(path):
    if not os.path.exists(path):
        return pd.DataFrame(columns=["日期", "類型", "標題", "情緒傾向", "描述"])
    try:
        df = pd.read_csv(path)
        df["日期"] = pd.to_datetime(df["日期"], errors="coerce")
        return df
    except Exception as e:
        # 嘗試用 python engine + 寬鬆模式再讀一次
        try:
            df = pd.read_csv(path, engine="python", on_bad_lines="skip")
            if "日期" in df.columns:
                df["日期"] = pd.to_datetime(df["日期"], errors="coerce")
            st.warning(f"⚠️ {path} 有格式問題,已跳過壞掉的行。錯誤:{e}")
            return df
        except Exception:
            st.warning(f"⚠️ 無法讀取 {path},事件清單將為空。請檢查 CSV 格式。")
            return pd.DataFrame(columns=["日期", "類型", "標題", "情緒傾向", "描述"])


def find_players_in_text(text):
    """用別名表搜尋文本內提到的球員。"""
    if not isinstance(text, str):
        return []
    found = set()
    for canonical, aliases in {**{p: [p] for p in PLAYERS}, **PLAYER_ALIASES}.items():
        for a in aliases:
            if a in text:
                found.add(canonical if canonical in PLAYERS else canonical)
                break
    # 確保只回傳在 PLAYERS 名單中的
    return [p for p in found if p in PLAYERS]


def count_tags(pushes):
    up = sum(1 for p in pushes if isinstance(p, dict) and p.get("Tag", "").strip() == "推")
    down = sum(1 for p in pushes if isinstance(p, dict) and p.get("Tag", "").strip() == "噓")
    neu = sum(1 for p in pushes if isinstance(p, dict) and p.get("Tag", "").strip() == "→")
    return up, down, neu


def count_sentiment(pushes, words):
    return sum(1 for p in pushes if isinstance(p, dict)
               for w in words if w in p.get("Content", ""))


def enrich(df):
    tags = df["Pushes"].apply(count_tags)
    df["推"] = tags.apply(lambda x: x[0])
    df["噓"] = tags.apply(lambda x: x[1])
    df["→"] = tags.apply(lambda x: x[2])
    df["正面詞數"] = df["Pushes"].apply(lambda p: count_sentiment(p, POSITIVE_WORDS))
    df["負面詞數"] = df["Pushes"].apply(lambda p: count_sentiment(p, NEGATIVE_WORDS))
    df["提到球員"] = df["Title"].apply(find_players_in_text)
    # 用內文補強(若有)
    if "Content" in df.columns:
        df["提到球員"] = df.apply(
            lambda r: list(set(r["提到球員"]) | set(find_players_in_text(r.get("Content", "")))),
            axis=1)
    return df


def tag_game(date):
    """根據日期判斷該文章屬於哪場比賽(或無)。"""
    if pd.isna(date):
        return None
    d = date.date() if hasattr(date, "date") else date
    for g in GAMES:
        gd = datetime.strptime(g["date"], "%Y-%m-%d").date()
        if d == gd:
            return f"vs {g['opponent']}"
    return None


def explode_players(df):
    records = []
    for _, row in df.iterrows():
        for player in row["提到球員"]:
            records.append({
                "球員": player, "Title": row["Title"], "Date": row["Date"],
                "推": row["推"], "噓": row["噓"], "→": row["→"],
                "正面詞數": row["正面詞數"], "負面詞數": row["負面詞數"],
                "場次": tag_game(row["Date"]),
            })
    return pd.DataFrame(records)


def summarize(df_player):
    if df_player.empty:
        return pd.DataFrame()
    summary = df_player.groupby("球員").agg(
        文章數=("Title", "count"),
        總推=("推", "sum"),
        總噓=("噓", "sum"),
        總中性=("→", "sum"),
        推文正面=("正面詞數", "sum"),
        推文負面=("負面詞數", "sum"),
    ).reset_index()
    summary["加權熱度"] = summary["文章數"] + (summary["總推"] + summary["總噓"]) // 10
    summary["正負比"] = (summary["推文正面"] /
                          summary["推文負面"].replace(0, 1)).round(2)
    # 情緒分數: (推-噓)/(推+噓+1),範圍約 -1 ~ 1
    summary["情緒分數"] = ((summary["總推"] - summary["總噓"]) /
                           (summary["總推"] + summary["總噓"] + 1)).round(3)
    return summary.sort_values("文章數", ascending=False)


# ──────────────────────────────────────────────
# 5. 標題與資料來源控制
# ──────────────────────────────────────────────
st.title("⚾ 2026 WBC ｜ PTT Baseball 板 社群輿論儀表板")
st.caption("透過篩選球員、場次、時間區間與情緒維度,觀察社群討論熱度與情緒傾向變化")

if FONT_NAME is None:
    st.warning("找不到中文字體,圖表中文可能顯示為方框。請安裝 fonts-noto-cjk。")

with st.sidebar:
    st.header("📥 資料來源")
    source = st.radio(
        "選擇資料來源",
        ["讀取現有 CSV", "重新爬取 PTT", "使用假資料(demo)"],
        help="課堂展示建議用『讀取現有 CSV』或『假資料』。",
    )

    if source == "重新爬取 PTT":
        # 【修改處】調高上限至 5000，預設抓 2500，確保包含整個預賽期間的文章
        max_posts = st.number_input("最多爬取文章數", 100, 5000, 2500, step=100,
                                    help="WBC 台灣隊賽程期間（3/4-3/10）文章爆量，建議至少設為 2000-3000 篇才能完整涵蓋 3/5 對澳洲的首戰")
        use_keyword = st.checkbox("只抓 WBC 相關標題", value=True)
        st.caption("⚠️ WBC 期間平均每天討論量極大，要涵蓋 3/5-3/10 全段建議抓取 2500 篇以上")

        if st.button("🚀 開始爬取"):
            bar = st.progress(0.0, text="智能尋找 3 月對應頁面...")
            status_box = st.empty()
            scraper = PTTScraper("Baseball")

            # 智能尋找起始頁
            def _smart_progress(attempt, idx, page_date):
                date_str = page_date.strftime("%m/%d") if page_date else "?"
                status_box.info(
                    f"🔍 智能定位中(第 {attempt}/6 次)｜"
                    f"測試 index{idx}｜該頁日期 {date_str}"
                )

            start_index = scraper.smart_find_start_page(
                WBC_START, WBC_END, progress_callback=_smart_progress)

            if start_index is None:
                st.error("無法定位 PTT 起始頁,請檢查網路")
                st.stop()

            scraper.url = f"{scraper.base_url}/bbs/Baseball/index{start_index}.html"
            st.info(f"✅ 定位完成,從 index{start_index} 開始爬取")

            def _progress(cur, total, pages=0, current_date=None):
                bar.progress(min(cur / total, 1.0),
                             text=f"已抓取 {cur}/{total} 篇")
                date_str = current_date.strftime("%m/%d") if current_date else "?"
                status_box.info(
                    f"📄 已翻頁:**{pages}** ｜ "
                    f"當前日期:**{date_str}** ｜ "
                    f"累積:**{cur}** 篇"
                )

            kw = WBC_KEYWORDS if use_keyword else None
            try:
                raw = scraper.get_data_in_range(
                    WBC_START, WBC_END, max_posts=max_posts,
                    keyword_filter=kw, progress=_progress)
                if len(raw) == 0:
                    st.warning("爬完了但沒抓到符合條件的文章。舊 CSV 已保留。")
                else:
                    pd.DataFrame(raw).to_csv(CSV_PATH, index=False, encoding="utf-8-sig")
                    st.cache_data.clear()
                    st.success(f"✅ 完成！共 {len(raw)} 篇,已存成 {CSV_PATH}")
            except Exception as e:
                st.error(f"爬取過程發生錯誤:{e}")


# ──────────────────────────────────────────────
# 6. 載入資料
# ──────────────────────────────────────────────
if source == "使用假資料(demo)":
    df = generate_fake_data()
elif os.path.exists(CSV_PATH):
    df = load_raw_csv(CSV_PATH)
else:
    st.info("尚未有 CSV 檔。請先在側邊欄爬取資料,或改用『假資料(demo)』。")
    st.stop()

df = enrich(df)
players_info = load_players_info(PLAYERS_INFO_PATH)
events = load_events(EVENTS_PATH)


# ──────────────────────────────────────────────
# 7. 共用篩選器(全域)
# ──────────────────────────────────────────────
GAME_OPTIONS = {
    "全部場次": None,
    "3/5 vs 澳洲 (0-3 敗)": "2026-03-05",
    "3/6 vs 日本 (0-13 敗)": "2026-03-06",
    "3/7 vs 捷克 (14-0 勝)": "2026-03-07",
    "3/8 vs 韓國 (5-4 勝)": "2026-03-08",
}

STATS_METRICS = ["AVG (打擊率)", "OPS", "H (安打)", "HR (全壘打)",
                 "RBI (打點)", "SB (盜壘)"]
STATS_COL_MAP = {
    "AVG (打擊率)": "AVG", "OPS": "OPS", "H (安打)": "H",
    "HR (全壘打)": "HR", "RBI (打點)": "RBI", "SB (盜壘)": "SB",
}

with st.sidebar:
    st.header("🔍 全域篩選")

    selected_game = st.selectbox(
        "⚾ 場次選擇", list(GAME_OPTIONS.keys()),
        help="選擇特定場次,所有 Tab 的圖表會同步切換")

    selected_players = st.multiselect(
        "選擇球員(可複選)", PLAYERS, default=PLAYERS)

    valid_dates = df["Date"].dropna()
    if not valid_dates.empty:
        dmin, dmax = valid_dates.min().date(), valid_dates.max().date()
        if dmin < dmax:
            date_range = st.slider("日期區間", min_value=dmin, max_value=dmax,
                                    value=(dmin, dmax), format="MM/DD")
        else:
            date_range = (dmin, dmax)
    else:
        date_range = None

    min_articles = st.slider("最少文章數門檻", 0, 10, 0)

    st.divider()
    st.header("📊 成績指標(供散點圖切換)")
    stats_metric_label = st.selectbox(
        "選擇 X 軸指標", STATS_METRICS,
        help="散點圖會用此指標當 X 軸,Y 軸為社群情緒分數")
    stats_metric = STATS_COL_MAP[stats_metric_label]


# 套用篩選
mask = df["提到球員"].apply(lambda lst: any(p in selected_players for p in lst))
if date_range and df["Date"].notna().any():
    start_d = pd.Timestamp(date_range[0])
    end_d = pd.Timestamp(date_range[1]) + pd.Timedelta(days=1)
    mask &= df["Date"].between(start_d, end_d)

# 全域場次篩選
game_date_str = GAME_OPTIONS[selected_game]
if game_date_str:
    game_date = pd.Timestamp(game_date_str)
    mask &= df["Date"].dt.date == game_date.date()

df_filtered = df[mask].copy()
df_filtered["提到球員"] = df_filtered["提到球員"].apply(
    lambda lst: [p for p in lst if p in selected_players])

df_player_all = explode_players(df_filtered)
summary_all = summarize(df_player_all)
if not summary_all.empty and min_articles > 0:
    summary_all = summary_all[summary_all["文章數"] >= min_articles]


# ──────────────────────────────────────────────
# 8. KPI 區塊(5 個指標)
# ──────────────────────────────────────────────
# 計算當前篩選下涉及球員的平均打擊率
def calc_avg_ba(summary, players_info):
    if summary.empty:
        return "—"
    involved = set(summary["球員"])
    info_subset = players_info[players_info["姓名"].isin(involved)].copy()
    avg_col = info_subset["AVG"].astype(str).str.strip()
    valid = avg_col[avg_col.str.match(r"^\.?\d+")]
    if valid.empty:
        return "—"
    nums = valid.apply(lambda x: float(x) if x.startswith(".") else float(x))
    return f".{int(nums.mean() * 1000):03d}"


c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("文章總數", len(df_filtered))
c2.metric("涵蓋球員", summary_all["球員"].nunique() if not summary_all.empty else 0)
c3.metric("推文總數", int(df_filtered[["推", "噓", "→"]].sum().sum()))
total_pos = int(summary_all["推文正面"].sum()) if not summary_all.empty else 0
total_neg = int(summary_all["推文負面"].sum()) if not summary_all.empty else 0
c4.metric("正面詞/負面詞", f"{total_pos} / {total_neg}")
c5.metric("涉及球員平均打擊率", calc_avg_ba(summary_all, players_info))

# 顯示目前場次選擇
if selected_game != "全部場次":
    st.info(f"⚾ 目前場次篩選:**{selected_game}**(所有 Tab 已同步)")

if summary_all.empty:
    st.warning("目前篩選下沒有資料,請放寬條件或更換資料來源。")
    st.stop()


# ──────────────────────────────────────────────
# 9. 五個 Tab
# ──────────────────────────────────────────────
tab1, tab2, tab3, tab4, tab5 = st.tabs([
    "🏆 總覽", "⚾ 場次分析", "👤 球員個人頁", "📊 成績 vs 輿論", "📰 賽事時間軸"])


# ── Tab 1：總覽 ───────────────────────────────
with tab1:
    st.subheader("全體球員聲量與情緒總覽")

    col_l, col_r = st.columns(2)
    with col_l:
        st.markdown("**球員加權熱度排行**")
        fig, ax = plt.subplots(figsize=(6, max(3, len(summary_all) * 0.35)))
        ax.barh(summary_all["球員"], summary_all["加權熱度"], color="steelblue")
        ax.invert_yaxis()
        ax.set_xlabel("加權熱度")
        st.pyplot(fig)

    with col_r:
        st.markdown("**推噓堆疊**")
        fig, ax = plt.subplots(figsize=(6, max(3, len(summary_all) * 0.35)))
        x = range(len(summary_all))
        ax.bar(x, summary_all["總推"], label="推", color="tomato", width=0.5)
        ax.bar(x, summary_all["總噓"], bottom=summary_all["總推"],
               label="噓", color="steelblue", width=0.5)
        ax.bar(x, summary_all["總中性"],
               bottom=summary_all["總推"] + summary_all["總噓"],
               label="→", color="lightgray", width=0.5)
        ax.set_xticks(list(x))
        ax.set_xticklabels(summary_all["球員"], rotation=45, ha="right")
        ax.legend()
        st.pyplot(fig)

    st.markdown("**情緒詞分布(正面 vs 負面)**")
    fig, ax = plt.subplots(figsize=(12, 4))
    bw = 0.35
    x = range(len(summary_all))
    ax.bar([i - bw / 2 for i in x], summary_all["推文正面"], width=bw,
           label="正面詞", color="tomato")
    ax.bar([i + bw / 2 for i in x], summary_all["推文負面"], width=bw,
           label="負面詞", color="steelblue")
    ax.set_xticks(list(x))
    ax.set_xticklabels(summary_all["球員"], rotation=45, ha="right")
    ax.legend()
    st.pyplot(fig)

    with st.expander("📋 彙整資料表"):
        st.dataframe(summary_all, width="stretch")


# ── Tab 2：場次分析 ──────────────────────────
with tab2:
    st.subheader("各場次討論分析")
    st.caption("👈 從左側『場次選擇』切換比賽,本 Tab 會顯示該場詳細討論。"
               "若選『全部場次』,則同時顯示四場對戰。")

    # 根據全域場次選擇決定要顯示的場次
    if selected_game == "全部場次":
        games_to_show = GAMES
    else:
        # 從 GAMES 找出對應的單場
        sel_date = GAME_OPTIONS[selected_game]
        games_to_show = [g for g in GAMES if g["date"] == sel_date]

    for game in games_to_show:
        game_date = datetime.strptime(game["date"], "%Y-%m-%d").date()
        df_game = df_filtered[df_filtered["Date"].dt.date == game_date].copy()
        dp_game = explode_players(df_game)
        sm_game = summarize(dp_game)

        win_emoji = "🏆 勝" if game["tw_win"] else "💔 敗"
        st.markdown(f"### {win_emoji} {game['date']} ｜ 中華 {game['score']} {game['opponent']}")
        st.caption(f"當日相關文章 **{len(df_game)}** 篇")

        if sm_game.empty:
            st.info("當日無討論資料(可能該場篩選後無文章)。")
            st.markdown("---")
            continue

        c1, c2 = st.columns(2)
        with c1:
            fig, ax = plt.subplots(figsize=(6, max(3, len(sm_game) * 0.4)))
            ax.barh(sm_game["球員"], sm_game["文章數"], color="steelblue")
            ax.invert_yaxis()
            ax.set_xlabel("文章數")
            ax.set_title("當日被討論的球員", fontsize=11)
            st.pyplot(fig)

        with c2:
            fig, ax = plt.subplots(figsize=(6, max(3, len(sm_game) * 0.4)))
            bw = 0.35
            x = range(len(sm_game))
            ax.bar([i - bw / 2 for i in x], sm_game["推文正面"], width=bw,
                   label="正面詞", color="tomato")
            ax.bar([i + bw / 2 for i in x], sm_game["推文負面"], width=bw,
                   label="負面詞", color="steelblue")
            ax.set_xticks(list(x))
            ax.set_xticklabels(sm_game["球員"], rotation=45, ha="right")
            ax.set_title("當日情緒分布", fontsize=11)
            ax.legend()
            st.pyplot(fig)

        with st.expander(f"📄 {game['date']} 當日全部文章"):
            st.dataframe(df_game[["Title", "Date", "推", "噓", "→",
                                  "正面詞數", "負面詞數"]], width="stretch")
        st.markdown("---")


# ── Tab 3：球員個人頁 ────────────────────────
with tab3:
    st.subheader("球員個人頁:輿論 vs 實際表現")

    pick = st.selectbox("選擇球員", PLAYERS, key="single_player")

    # 基本資料卡片
    info_row = players_info[players_info["姓名"] == pick]
    if not info_row.empty:
        info = info_row.iloc[0]
        # 第一排:基本資訊
        r1 = st.columns(3)
        r1[0].metric("守備位置", info.get("守備位置", "—"))
        r1[1].metric("所屬球隊", info.get("所屬球隊", "—"))
        r1[2].metric("類別", info.get("類別", "—"))

        # 第二排:WBC 官方打擊數據
        st.markdown("**📊 WBC 官方賽事數據**")
        r2 = st.columns(6)
        for col, label, key in [
            (r2[0], "AB(打數)", "AB"),
            (r2[1], "H(安打)", "H"),
            (r2[2], "HR", "HR"),
            (r2[3], "RBI", "RBI"),
            (r2[4], "SB(盜壘)", "SB"),
            (r2[5], "AVG", "AVG"),
        ]:
            val = str(info.get(key, "-")).strip()
            col.metric(label, val if val and val != "-" else "—")

        r3 = st.columns(3)
        for col, label, key in [
            (r3[0], "OBP(上壘率)", "OBP"),
            (r3[1], "SLG(長打率)", "SLG"),
            (r3[2], "OPS", "OPS"),
        ]:
            val = str(info.get(key, "-")).strip()
            col.metric(label, val if val and val != "-" else "—")

        if pd.notna(info.get("備註", None)) and str(info.get("備註", "")).strip():
            st.caption(f"📝 {info['備註']}")
        st.caption("資料來源:WBC 官方統計頁面(mlb.com/world-baseball-classic/stats)、報導者、運動視界")

    # 該球員的輿論資料
    dp_player = df_player_all[df_player_all["球員"] == pick]

    if dp_player.empty:
        st.warning(f"目前篩選範圍內沒有 {pick} 的討論資料。")
    else:
        sm_player = summarize(dp_player).iloc[0]
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("文章數", int(sm_player["文章數"]))
        c2.metric("推/噓", f"{int(sm_player['總推'])} / {int(sm_player['總噓'])}")
        c3.metric("正/負面詞", f"{int(sm_player['推文正面'])} / {int(sm_player['推文負面'])}")
        c4.metric("正負比", f"{sm_player['正負比']:.2f}")

        st.markdown(f"**{pick} 每日討論趨勢**")
        ts = dp_player.dropna(subset=["Date"]).copy()
        if not ts.empty:
            ts["日期"] = ts["Date"].dt.date
            daily = ts.groupby("日期").agg(
                文章數=("Title", "count"),
                正面詞=("正面詞數", "sum"),
                負面詞=("負面詞數", "sum"),
                推=("推", "sum"),
                噓=("噓", "sum"),
            ).reset_index()

            fig, axes = plt.subplots(2, 1, figsize=(12, 6), sharex=True)
            axes[0].bar(daily["日期"], daily["文章數"], color="steelblue")
            axes[0].set_ylabel("文章數")
            axes[0].set_title(f"{pick} ── 每日討論量 & 情緒")
            # 標記比賽日
            for g in GAMES:
                gd = datetime.strptime(g["date"], "%Y-%m-%d").date()
                if daily["日期"].min() <= gd <= daily["日期"].max():
                    color = "green" if g["tw_win"] else "red"
                    axes[0].axvline(gd, color=color, linestyle="--", alpha=0.4)
                    axes[0].text(gd, axes[0].get_ylim()[1] * 0.9,
                                 f"vs{g['opponent']}", rotation=90,
                                 fontsize=8, color=color)

            axes[1].plot(daily["日期"], daily["正面詞"], marker="o",
                         label="正面詞", color="tomato")
            axes[1].plot(daily["日期"], daily["負面詞"], marker="o",
                         label="負面詞", color="steelblue")
            axes[1].set_ylabel("詞頻")
            axes[1].set_xlabel("日期")
            axes[1].legend()
            plt.xticks(rotation=45)
            plt.tight_layout()
            st.pyplot(fig)

        st.markdown(f"**提及 {pick} 的全部文章**")
        st.dataframe(dp_player[["Title", "Date", "場次", "推", "噓",
                                "正面詞數", "負面詞數"]], width="stretch")


# ── Tab 4:成績 vs 輿論散點圖 ─────────────────
with tab4:
    st.subheader(f"球員 {stats_metric_label} vs 社群情緒分數")
    st.caption("X 軸:WBC 官方打擊數據 ｜ Y 軸:社群情緒分數 = (推 − 噓) ÷ (推 + 噓 + 1) ｜ 圓圈大小:加權熱度")

    def parse_stat(val):
        """把 '.400' / '1.038' / '6' / '-' 等字串轉成 float,失敗回 None。"""
        if val is None or pd.isna(val):
            return None
        s = str(val).strip()
        if s == "" or s == "-":
            return None
        try:
            return float(s)
        except ValueError:
            return None

    # 合併輿論摘要與打擊數據
    info_subset = players_info[players_info["姓名"].isin(summary_all["球員"])].copy()
    info_subset["指標值"] = info_subset[stats_metric].apply(parse_stat)
    info_subset = info_subset.dropna(subset=["指標值"])

    merged = summary_all.merge(
        info_subset[["姓名", "指標值", "類別"]],
        left_on="球員", right_on="姓名", how="inner",
    )

    if merged.empty:
        st.warning(f"沒有球員具備 {stats_metric_label} 的數值資料(可能是投手,或當前篩選範圍內球員無資料)。")
    else:
        fig, ax = plt.subplots(figsize=(11, 6))

        # 【修改處】計算當前資料中的最大加權熱度，用於動態縮放泡泡大小
        max_heat = merged["加權熱度"].max() if not merged.empty else 1

        # 區分中職 vs 旅外用不同 marker
        for category, marker in [("中職", "o"), ("旅外", "^")]:
            sub = merged[merged["類別"] == category]
            if sub.empty:
                continue
            scatter = ax.scatter(
                sub["指標值"], sub["情緒分數"],
                # 【修改處】利用動態比例縮放：最大泡泡固定為面積 1200，最小為 50
                s=(sub["加權熱度"] / max_heat) * 1200 + 50,
                c=sub["情緒分數"], cmap="RdYlGn",
                vmin=-1, vmax=1, alpha=0.85,
                marker=marker, edgecolors="gray", linewidths=1,
                label=f"{category}(n={len(sub)})",
            )

        # 標註球員姓名
        for _, row in merged.iterrows():
            ax.annotate(row["球員"], (row["指標值"], row["情緒分數"]),
                        textcoords="offset points", xytext=(7, 4), fontsize=10)

        ax.axhline(0, color="gray", linestyle="--", alpha=0.5,
                   label="情緒分數 = 0(中性)")
        ax.set_xlabel(stats_metric_label, fontsize=12)
        ax.set_ylabel("社群情緒分數(-1=全噓 / +1=全推)", fontsize=12)
        ax.set_title(f"{stats_metric_label} × 社群情緒(圓圈大小 = 加權熱度)",
                     fontsize=13)
        ax.legend(loc="best", fontsize=9)
        ax.grid(True, alpha=0.3)
        plt.tight_layout()
        st.pyplot(fig)

        # 解讀提示
        st.markdown("**💡 如何解讀這張圖**")
        st.markdown(
            f"- **右上角象限**:{stats_metric_label}高 + 鄉民正評 → 實至名歸的英雄\n"
            f"- **左下角象限**:{stats_metric_label}低 + 鄉民負評 → 印象與表現一致的失望\n"
            f"- **左上角象限**:{stats_metric_label}低 + 鄉民竟給正評 → 可能因傷或其他原因被同情/期待\n"
            f"- **右下角象限**:{stats_metric_label}高 + 鄉民竟給負評 → 高反差!可能是反串、隱形貢獻被忽略\n"
            f"- **圓形 = 中職球員,三角形 = 旅外球員**:對照鄉民對兩類球員的態度差異"
        )

        # 詳細資料表
        with st.expander("📋 散點圖原始資料"):
            show = merged[["球員", "類別", "指標值", "情緒分數",
                          "加權熱度", "文章數", "總推", "總噓"]].copy()
            show.columns = ["球員", "類別", stats_metric_label,
                            "情緒分數", "加權熱度", "文章數", "推", "噓"]
            st.dataframe(show.sort_values("加權熱度", ascending=False),
                         width="stretch", hide_index=True)


# ── Tab 5:賽事時間軸 ────────────────────────
with tab5:
    st.subheader("賽事時間軸:比賽結果 × 社群情緒")
    st.caption("把每場比賽結果疊在情緒走勢上,觀察『實戰表現 vs 鄉民反應』的對應關係。")

    if df_player_all["Date"].notna().any():
        ts = df_player_all.dropna(subset=["Date"]).copy()
        ts["日期"] = ts["Date"].dt.date
        daily = ts.groupby("日期").agg(
            文章數=("Title", "count"),
            正面詞=("正面詞數", "sum"),
            負面詞=("負面詞數", "sum"),
            推=("推", "sum"),
            噓=("噓", "sum"),
        ).reset_index()

        fig, axes = plt.subplots(2, 1, figsize=(14, 7), sharex=True)

        # 上圖：每日文章量,並標記比賽日
        axes[0].bar(daily["日期"], daily["文章數"], color="lightsteelblue",
                    label="每日文章數")
        for g in GAMES:
            gd = datetime.strptime(g["date"], "%Y-%m-%d").date()
            if daily["日期"].min() <= gd <= daily["日期"].max():
                color = "green" if g["tw_win"] else "red"
                axes[0].axvline(gd, color=color, linestyle="--", alpha=0.6)
                label = f"vs {g['opponent']}\n{g['score']}"
                axes[0].text(gd, axes[0].get_ylim()[1] * 0.85, label,
                             rotation=0, fontsize=9, color=color,
                             ha="center",
                             bbox=dict(boxstyle="round,pad=0.3",
                                       facecolor="white",
                                       edgecolor=color, alpha=0.8))
        axes[0].set_ylabel("文章數")
        axes[0].set_title("每日討論量(綠=台灣勝,紅=台灣敗)")

        # 下圖:情緒走勢
        axes[1].plot(daily["日期"], daily["正面詞"], marker="o",
                     label="正面詞", color="tomato", linewidth=2)
        axes[1].plot(daily["日期"], daily["負面詞"], marker="o",
                     label="負面詞", color="steelblue", linewidth=2)
        for g in GAMES:
            gd = datetime.strptime(g["date"], "%Y-%m-%d").date()
            if daily["日期"].min() <= gd <= daily["日期"].max():
                color = "green" if g["tw_win"] else "red"
                axes[1].axvline(gd, color=color, linestyle="--", alpha=0.4)
        axes[1].set_ylabel("情緒詞頻")
        axes[1].set_xlabel("日期")
        axes[1].legend()
        plt.xticks(rotation=45)
        plt.tight_layout()
        st.pyplot(fig)

        st.markdown("**💡 解讀提示**")
        st.markdown(
            "- 若某天台灣輸球但**正面詞反而上升**,可能是球員個人有亮眼表現、或鄉民集體鼓勵\n"
            "- 若某天台灣贏球但**負面詞仍高**,可能是反串、戰術質疑、或對個別失誤的批評\n"
            "- 對比『中華隊本屆打擊率僅 1 成 86』的整體疲弱數據,觀察情緒是否真的與此一致"
        )

        # 賽事事件列表
        if not events.empty:
            st.markdown("**📋 賽事關鍵事件**")
            events_disp = events.copy()
            events_disp["日期"] = events_disp["日期"].dt.strftime("%Y-%m-%d")
            st.dataframe(events_disp, width="stretch")

        # ── 整隊官方統計(中職 vs 旅外)─────────────
        st.markdown("---")
        st.markdown("### 🏟️ 整隊官方統計:中職 vs 旅外球員的鮮明落差")
        st.caption("資料來源:運動視界(2026/3/9)引用 Statcast 與 WBC 官方數據")

        team_stats_path = "team_stats.csv"
        team_pitch_path = "team_pitching.csv"

        col_bat, col_pit = st.columns(2)
        if os.path.exists(team_stats_path):
            with col_bat:
                st.markdown("**打擊統計**")
                tb = pd.read_csv(team_stats_path)
                st.dataframe(tb, width="stretch", hide_index=True)
                st.caption("💡 旅外球員 OPS 1.082 vs 中職 0.451,差距高達 2.4 倍。"
                           "可對照鄉民對旅外/本土球員的討論態度。")
        if os.path.exists(team_pitch_path):
            with col_pit:
                st.markdown("**投手統計**")
                tp = pd.read_csv(team_pitch_path)
                st.dataframe(tp, width="stretch", hide_index=True)
                st.caption("💡 中職投手 ERA 7.82 vs 旅外 4.19。"
                           "全投手 ERA 5.63 若扣掉日本場 7 局 13 分,降至 2.52。")
    else:
        st.info("無有效日期資料,無法繪製時間軸。")


# ──────────────────────────────────────────────
# 10. 頁尾
# ──────────────────────────────────────────────
st.markdown("---")
st.caption(
    "**資料來源:** "
    "PTT Baseball 板(社群輿論)｜"
    "報導者 *Data Reporter*〈台灣2026 WBC投打分析〉(球員個人事件與打擊數據)｜"
    "運動視界〈2026年經典賽中華隊預賽數據漫談〉(整隊 Statcast 與 WBC 官方數據)｜"
    "WBC 官方賽程比分。"
    "情緒分析採關鍵詞頻率法,存在反諷誤判等限制,詳見論文『資料品質與限制』章節。"
)