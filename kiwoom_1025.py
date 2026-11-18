# -*- coding: utf-8 -*-
"""
10·25일선 매매 자동창 (키움 REST API)

- 오른쪽: 관심종목 5일선 후보 (영웅문 [0130] 관심종목에서 복사/내보낸 목록, 또는 조건식)
- 왼쪽: 후보마다 10일선 / 25일선 매매 계획을 자동 계산
  매매선, 이격률, 등급, 1~4차 매수가와 비중, 평단, 익절가 2개, 손절가, 판정
- 실시간 시세로 판정(대기 / 1차 근접 / n차 도달 / 손절 이탈)을 바꾸고 알림음

규칙 근거: 5·10·25일선 매매 규칙서 (프로젝트 Claude Doc)
주문 기능은 없습니다. 시세 확인과 알림만 합니다.
필요: Python 3, pip install pyqt6 requests websockets
앱키는 kiwoom_watch5_rest.py와 같은 kiwoom_config.ini를 씁니다.
"""
import os
import re
import sys
import json
import time
import queue
import datetime
import threading
import webbrowser
import urllib.parse
import configparser

try:
    import requests
    from websockets.sync.client import connect as ws_connect

    from PyQt6.QtCore import QThread, pyqtSignal, Qt, QTimer, QRect, QEvent
    from PyQt6.QtGui import QBrush, QColor, QFont, QPalette
    from PyQt6.QtWidgets import (
        QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QComboBox,
        QPushButton, QLabel, QTableWidget, QTableWidgetItem, QSpinBox,
        QHeaderView, QCheckBox, QMessageBox, QSplitter, QFrame, QMenu, QInputDialog,
        QStyledItemDelegate, QStyleOptionViewItem, QStyle,
    )
except ImportError as e:
    print(f"필요한 프로그램이 설치되지 않았습니다: {e}")
    print("cmd에서 다음을 실행한 뒤 다시 시도하세요:  python -m pip install pyqt6 requests websockets")
    input("Enter를 누르면 닫힙니다.")
    sys.exit(1)

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "kiwoom_config.ini")
LOGFILE = os.path.join(HERE, "kiwoom_1025.log")
LISTFILE = os.path.join(HERE, "관심종목_5일선.txt")

HOSTS = {
    "real": ("https://api.kiwoom.com", "wss://api.kiwoom.com:10000/api/dostk/websocket"),
    "mock": ("https://mockapi.kiwoom.com", "wss://mockapi.kiwoom.com:10000/api/dostk/websocket"),
}

# ---------------- 규칙서 숫자 (바꾸고 싶으면 여기만 고치면 됩니다) ----------------
MIN_GAP = 10            # 이격률 10% 이상만 매매
BIG_GAP = 20            # 큰 이격: 비중 20%, 익절 +5%/+7%
A_GAP = 30              # A급
STEP_10 = {0: 4.0, 20: 6.0}             # 10일선 추가매수 간격(%) : 이격 10~20 → 4, 20 이상 → 6
STEP_25 = {0: 4.0, 20: 6.0, 30: 8.5}    # 25일선 : 10~20 → 4, 20~30 → 6, 30 이상 → 8.5 (확정된 -3~5 / -5~7 / -7~10의 중간)
W_BASIC = [3, 3, 4]                     # 기본 비중 10%
W_BIG = [5, 5, 5, 5]                    # 큰 이격 20%
W_HIGH = [2.5, 2.5, 2.5, 2.5]           # 고가권(바닥 대비 +500% 이상) 큰 이격은 10%로 제한
BIG_BOUNCE = 10         # 앞 이평선에서 10% 이상 반등했으면 다음 선은 패스 / 한 템포 늦춤
HIGH_RISE = 500         # 바닥 대비 +500% 이상이면 고가권
NEAR = 3                # 1차 매수가 위 3% 이내면 '근접'
# 5일선: 상한가(+29% 이상) 또는 전고 돌파 +20% 이상 양봉 다음날 눌림 매수
SIG_UP = 20             # 신호 양봉 최소 등락률
LIMIT_UP = 29           # 상한가로 보는 등락률
STEP_5 = {0: 4.0, 1: 4.0, 2: 5.0, 3: 5.0, 4: 7.5}   # 상한가 횟수별 추가매수 간격(%) (0 = 20% 양봉)
FRAC_5 = {0: 2 / 3, 1: None, 2: 2 / 3, 3: 1 / 2, 4: 1 / 3}  # 신호봉 몸통의 몇 지점에서 1차 (None = 종가 부근)
LINE_NAME = {5: "5일선 매매", 10: "10일선 매매", 25: "25일선 매매", "out": "이탈", None: "제외 · 조회중"}

COLS = ["판정", "종목", "현재가", "등락률", "매매선", "이격률", "등급",
        "1차", "2차", "3차", "손절", "손절까지", "비중", "평단", "익절①", "익절②", "메모"]
C = {n: i for i, n in enumerate(COLS)}
HEAD = {"1차": "1차\n검색식", "2차": "2차\n검색식", "3차": "3차\n검색식", "손절": "손절\n검색식"}  # 위줄 매수가, 아랫줄 검색식 값
LINE_FG = {5: QColor("#e0218a"), 10: QColor("#1f5fd1"), 25: QColor("#e6b800")}   # 매매선 글씨: 분홍 / 파랑 / 옅은 노랑
MEMO_CUT = 0.2          # 메모 칸에서 덜어 다른 칸에 나눠 줄 비율
ROW_H2 = 66             # 종목 줄 높이 (위줄 매수가 + 아랫줄 검색식 값)
RCOLS = ["#", "종목", "등락률", "이격5·10", "이격10·25", "다음"]

RED = QColor("#d6202a")
BLUE = QColor("#1f5fd1")
GRAY = QColor("#8a8a8a")
PINK = QColor("#ffdbe6")           # 근접 · 매수가능(1~4차 도달)
WAIT_BG = (QColor("#ffffff"), QColor("#f5f5f5"))   # 대기: 흰색 / 아주 연한 회색 번갈아
MEMO_BG = QColor("#fffde7")
BLINK = QColor("#ff8fb8")                          # 검색식 값 1% 이내 깜빡임 분홍                        # 오른쪽 목록의 메모 줄
BG = {
    "1차": QColor("#ffe3c4"), "2차": QColor("#ffcf9e"), "3차": QColor("#ffb994"),
    "4차": QColor("#ffa3a3"), "근접": QColor("#eaf6e4"), "이탈": QColor("#c9d6f2"),
}


def log(text):
    try:
        with open(LOGFILE, "a", encoding="utf-8") as f:
            f.write(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {text}\n")
    except OSError:
        pass


def to_int(s):
    s = str(s).strip().replace("+", "").replace("-", "").replace(",", "")
    try:
        return int(float(s))
    except ValueError:
        return 0


def to_float(s):
    try:
        return float(str(s).strip().replace(",", ""))
    except ValueError:
        return 0.0


def clean_code(c):
    c = str(c).strip()
    return c[1:] if c[:1] in ("A", "J", "Q") and len(c) == 7 else c


# ---------------- 라운드피겨 ----------------
def unit(p):
    """딱 떨어지는 가격 단위. 강의 예: 10일선 3,265 → 3,250, 3,780 → 3,750, 6,229 → 6,200, 25일선 2,020 → 2,000"""
    if p < 1000:
        return 5
    if p < 5000:
        return 50
    if p < 10000:
        return 100
    if p < 50000:
        return 500
    if p < 200000:
        return 1000
    return 5000


def round_near(p):
    """매수가: 가장 가까운 라운드피겨"""
    if p <= 0:
        return 0
    u = unit(p)
    return int(round(p / u) * u)


def stop_below(p):
    """손절가: 이평선 바로 아래 라운드피겨 (이평이 3,780이면 3,750)"""
    if p <= 0:
        return 0
    u = unit(p)
    return int((p - 0.01) // u * u)


def tick_up(p):
    """익절가: 호가 단위로 올림"""
    t = 1 if p < 2000 else 5 if p < 5000 else 10 if p < 20000 else 50 if p < 50000 else \
        100 if p < 200000 else 500 if p < 500000 else 1000
    return int(-(-p // t) * t)


def step_for(table, gap):
    s = table[0]
    for k in sorted(table):
        if gap >= k:
            s = table[k]
    return s / 100


# ---------------- 계산 (규칙서 10·25일선) ----------------
def plan(bars, cur=0, day_high=0, day_low=0, overseas_drop=False, today=None):
    """bars: 일봉 [{dt, open, high, low, close}] 최신이 앞. 결과 dict 하나를 돌려줍니다."""
    today = today or datetime.date.today().strftime("%Y%m%d")
    if len(bars) < 27:
        return {"verdict": "제외", "memo": "일봉 부족(신규주 등)"}
    closes = [b["close"] for b in bars]
    amts = [b.get("amt", 0) for b in bars]
    opens = [b["open"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    if cur:
        if bars[0]["dt"] == today:
            closes[0] = cur
            highs[0] = max(highs[0], day_high or cur)
            lows[0] = min(lows[0] or cur, day_low or cur)
        elif datetime.datetime.now().strftime("%H%M") >= "0900":
            closes.insert(0, cur)
            amts.insert(0, 0)
            opens.insert(0, cur)
            highs.insert(0, day_high or cur)
            lows.insert(0, day_low or cur)
    cur = cur or closes[0]

    def ma(n, i=0):
        """n일 이평. 7.5일선·17.5일선처럼 소수면 마지막 날을 절반만 넣음"""
        whole = int(n)
        total = sum(closes[i:i + whole])
        if n > whole:
            total += closes[i + whole] * (n - whole)
        return total / n

    def bounce(n):
        """최근 7일 안에 n일선을 찍고 나서 몇 % 반등했나"""
        best = 0.0
        for i in range(1, 8):
            if i + n > len(closes) or lows[i] <= 0:
                continue
            pullback = closes[i] < max(closes[i + 1:i + 4])     # 오르다 눌린 날만 (급등 첫날 제외)
            if pullback and lows[i] <= ma(n, i) * 1.01:
                best = max(best, (max(highs[:i]) / lows[i] - 1) * 100)
        return best

    m5, m7, m10, m25 = ma(5), ma(7.5), ma(10), ma(25)     # m7 = 7.5일선 (규칙서 2026-10-15 수정)
    m17 = ma(17.5)                                         # 17.5일선 = 25일선 매매 1차
    g510 = (m5 - m10) / cur * 100
    g1025 = (m10 - m25) / cur * 100
    kijun = (max(highs[:26]) + min(lows[:26])) / 2
    bottom = min(x for x in lows[:120] if x > 0)
    rise = (cur / bottom - 1) * 100
    b5, b10 = bounce(5), bounce(10)
    bear = 0
    for i in range(len(closes)):
        if closes[i] < opens[i]:
            bear += 1
        else:
            break

    res = {"m5": m5, "m10": m10, "m25": m25, "g510": g510, "g1025": g1025, "cur": cur}
    memo = []

    # 5일선 신호: 오늘 또는 어제 상한가 / 전고 돌파 20% 이상 양봉
    sig = None
    for k in (0, 1):
        if not closes[k + 1]:
            continue
        chg = (closes[k] / closes[k + 1] - 1) * 100
        if chg < SIG_UP:
            continue
        cnt, j = 0, k
        while j + 1 < len(closes) and closes[j + 1] and (closes[j] / closes[j + 1] - 1) * 100 >= LIMIT_UP:
            cnt += 1
            j += 1
        brk = highs[k] >= max(highs[k + 1:k + 61])
        if cnt or brk:
            sig = (k, min(cnt, 4), brk)
            break
    stop5 = stop_below(m5)

    if sig and cur >= stop5:
        line, gap = 5, g510
        k, cnt, brk = sig
        label = {0: "20%↑양봉", 1: "1상", 2: "2상", 3: "3상", 4: "4상+"}[cnt]
        memo.append(("오늘 신호 → 내일 눌림 매수" if k == 0 else "어제 신호 → 오늘 눌림 매수"))
        if cnt and not brk:
            memo.append("전고 미돌파")
        if opens[k] == highs[k] == lows[k]:
            memo.append("점상: 아주 보수적으로 또는 패스")
        if cnt >= 4:
            memo.append("4연상 이상: 비중 5% 권장")
        if 2 <= cnt <= 3 and brk:
            memo.append("A급 후보(테마·거래대금 확인)")
        if amts[k] and amts[k] < 50000:
            memo.append("거래대금 500억 미만")
    elif g510 >= MIN_GAP and cur > m10 and b5 < BIG_BOUNCE:
        line, gap = 10, g510
        if sig:
            memo.append("5일선 이탈 → 10일선")
    elif g1025 >= MIN_GAP:
        line, gap = 25, g1025
        if g510 >= MIN_GAP and b5 >= BIG_BOUNCE:
            memo.append(f"5선 반등 {b5:.0f}% → 10선 패스")
    else:
        res.update(verdict="제외", line=None, next=next_label(g510, g1025),
                   memo=(f"5선 반등 {b5:.0f}%, 25선 이격 부족" if g510 >= MIN_GAP else "이격 10% 미만"))
        return res

    shift = overseas_drop
    if line == 5:
        top, bot = closes[k], min(opens[k], closes[k])
        frac = FRAC_5[cnt]
        base = top if frac is None else bot + (top - bot) * frac
        stop = stop5
        step = STEP_5[cnt] / 100
        if rise >= 300:
            shift = True
            memo.append(f"바닥 대비 +{rise:.0f}% → 2차가에서 1차")
    elif line == 10:
        base = m7
        if m10 > kijun and (m10 - kijun) / m10 <= 0.03:
            stop = stop_below(kijun)
            memo.append("기준선 손절")
        else:
            stop = stop_below(m10)
        if m5 > kijun > m10:
            memo.append(f"기준선 {kijun:,.0f} 부근도 1차 가능")
        step = step_for(STEP_10, gap)
    else:
        base = m17
        if m10 > kijun > m25:
            base = kijun
            memo.append("기준선 1차")
        stop = stop_below(m25)
        step = step_for(STEP_25, gap)
        if b10 >= BIG_BOUNCE:
            shift = True
            memo.append(f"10선 반등 {b10:.0f}% → 한 템포 늦춤")
        if bear >= 2 and cur < m5 and cur < m10:
            memo.append(f"음봉 {bear}개 하락(A급 모양)")
        if cur > m10:
            memo.append("아직 10일선 위")

    big = gap >= BIG_GAP and line != 5
    high = rise >= HIGH_RISE
    weights = W_BIG if big else W_BASIC
    if high and line != 5:
        weights = W_HIGH if big else W_BASIC
        memo.append(f"바닥 대비 +{rise:.0f}% 고가권, 비중 10% 이하" + (" · 칼손절" if line == 25 else ""))
    if overseas_drop:
        memo.append("해외 -2% → 2차가에서 1차")

    p = round_near(base)
    if shift:
        p = round_near(p * (1 - step))
    buys = []
    for w in weights:
        if p <= stop:
            break
        buys.append((p, w))
        p = round_near(p * (1 - step))
    if len(buys) < len(weights) and buys:
        memo.append(f"손절가가 가까워 {len(buys)}차까지만")

    if line in (10, 25):            # SMINT 검색식 (2026-10-17): B1 = (짧은 이평 + 긴 이평)/2, B2·B3 = B1에서 긴 이평 쪽 33%·66%
        short, long_ = (m5, m10) if line == 10 else (m10, m25)
        b1 = (short + long_) / 2
        r10 = lambda x: int(round(x / 10) * 10)       # 끝자리 0으로 반올림
        res.update(fbuys=[r10(b1), r10(b1 - (b1 - long_) * 0.33), r10(b1 - (b1 - long_) * 0.66)], fstop=r10(long_))

    grade = label if line == 5 else "A급" if gap >= A_GAP else "큰 이격" if big else "일반"
    res.update(line=line, gap=gap, grade=grade, buys=buys, stop=stop, step=step * 100,
               next=f"{line}선", high=high, verdict="")
    if not buys:
        res.update(verdict="제외", memo="매수가가 손절가 아래 · " + " · ".join(memo))
        return res
    avg = sum(p * w for p, w in buys) / sum(w for _, w in buys)
    t1, t2 = (1.05, 1.07) if big else (1.03, 1.05)
    res.update(avg=avg, tp1=tick_up(avg * t1), tp2=tick_up(avg * t2),
               tp_text=("+5% 절반 · +7%" if big else "+3% 절반 · +5%"))

    if cur < stop:
        v = "이탈"
    else:
        n = sum(1 for p, _ in buys if cur <= p)
        if n:
            v = f"{n}차"
        elif cur <= buys[0][0] * (1 + NEAR / 100):
            v = "근접"
        else:
            v = "대기"
    res.update(verdict=v, memo=" · ".join(memo))
    return res


def next_label(g510, g1025):
    if g510 >= MIN_GAP:
        return "10선"
    if g1025 >= MIN_GAP:
        return "25선"
    return "-"


MANUAL_V = ["대기", "근접", "1차", "2차", "3차", "이탈", "제외"]   # 우클릭으로 고를 수 있는 판정
VERDICT_ORDER = {"4차": 0, "3차": 1, "2차": 2, "1차": 3, "근접": 4, "대기": 5, "이탈": 6, "제외": 7, "조회중": 8}


# ---------------- 설정 ----------------
def load_config():
    cp = configparser.ConfigParser()
    if not os.path.exists(CONFIG):
        cp["kiwoom"] = {"appkey": "여기에_앱키", "secretkey": "여기에_시크릿키", "mode": "real"}
        with open(CONFIG, "w", encoding="utf-8") as f:
            f.write("; 키움 REST API 앱키/시크릿키를 넣으세요. mode = real(실전) 또는 mock(모의투자)\n")
            cp.write(f)
        return None
    cp.read(CONFIG, encoding="utf-8")
    k = cp["kiwoom"]
    if "여기에" in k.get("appkey", "") or "여기에" in k.get("secretkey", ""):
        return None
    return {"appkey": k["appkey"].strip(), "secretkey": k["secretkey"].strip(),
            "mode": k.get("mode", "real").strip()}


def load_prefs():
    cp = configparser.ConfigParser()
    cp.read(CONFIG, encoding="utf-8")
    p = cp["w1025"] if cp.has_section("w1025") else {}
    return {"principal": int(p.get("principal", 1000)), "refresh": int(p.get("refresh", 5)),
            "ontop": p.get("ontop", "1") == "1", "sound": p.get("sound", "1") == "1",
            "cond": p.get("cond", ""), "rowh": p.get("rowh", "")}


def save_prefs(prefs):
    cp = configparser.ConfigParser()
    cp.read(CONFIG, encoding="utf-8")
    cp["w1025"] = {k: (("1" if v else "0") if isinstance(v, bool) else str(v)) for k, v in prefs.items()}
    with open(CONFIG, "w", encoding="utf-8") as f:
        cp.write(f)


# ---------------- REST ----------------
class Rest:
    def __init__(self, cfg):
        self.base = HOSTS[cfg["mode"]][0]
        self.cfg = cfg
        self.token = None
        self.lock = threading.Lock()
        self.last = 0.0

    def get_token(self):
        r = requests.post(f"{self.base}/oauth2/token", timeout=10,
                          headers={"Content-Type": "application/json;charset=UTF-8"},
                          json={"grant_type": "client_credentials",
                                "appkey": self.cfg["appkey"], "secretkey": self.cfg["secretkey"]})
        data = r.json()
        log(f"token 응답 return_code={data.get('return_code')} msg={data.get('return_msg')}")
        self.token = data.get("token")
        if not self.token:
            raise RuntimeError(data.get("return_msg") or f"토큰 발급 실패 (HTTP {r.status_code})")
        return self.token

    def post(self, api_id, path, body, cont=""):
        with self.lock:                      # 초당 조회 제한 대응
            wait = 0.3 - (time.time() - self.last)
            if wait > 0:
                time.sleep(wait)
            self.last = time.time()
        headers = {"Content-Type": "application/json;charset=UTF-8",
                   "authorization": f"Bearer {self.token}", "api-id": api_id}
        if cont:
            headers.update({"cont-yn": "Y", "next-key": cont})
        r = requests.post(f"{self.base}{path}", timeout=15, json=body, headers=headers)
        data = r.json()
        if data.get("return_code") not in (0, "0", None):
            log(f"{api_id} 오류: {data.get('return_msg')}")
        nxt = r.headers.get("next-key", "") if r.headers.get("cont-yn") == "Y" else ""
        return data, nxt


class TrWorker(QThread):
    """종목 마스터(이름↔코드)와 일봉 조회"""
    master = pyqtSignal(dict)            # name -> code
    bars = pyqtSignal(str, str, list)    # code, name, bars

    def __init__(self, rest):
        super().__init__()
        self.rest = rest
        self.q = queue.Queue()

    def add(self, code, name=""):
        self.q.put((code, name))

    def run(self):
        names = {}
        for mk in ("0", "10"):                       # 코스피, 코스닥
            try:
                nxt = ""
                for _ in range(20):
                    d, nxt = self.rest.post("ka10099", "/api/dostk/stkinfo", {"mrkt_tp": mk}, nxt)
                    for x in d.get("list") or []:
                        if x.get("code") and x.get("name"):
                            names[x["name"].strip()] = x["code"].strip()
                    if not nxt:
                        break
            except Exception as e:
                log(f"종목 목록 조회 실패 {mk}: {e!r}")
        log(f"종목 마스터 {len(names)}개")
        self.master.emit(names)
        while True:
            code, name = self.q.get()
            try:
                if not name:
                    info, _ = self.rest.post("ka10001", "/api/dostk/stkinfo", {"stk_cd": code})
                    name = info.get("stk_nm", "")
                d, _ = self.rest.post("ka10081", "/api/dostk/chart", {
                    "stk_cd": code, "base_dt": datetime.date.today().strftime("%Y%m%d"), "upd_stkpc_tp": "1"})
                rows = d.get("stk_dt_pole_chart_qry") or []
                out = [{"dt": x.get("dt", ""), "open": to_int(x.get("open_pric")), "high": to_int(x.get("high_pric")),
                        "low": to_int(x.get("low_pric")), "close": to_int(x.get("cur_prc")),
                        "amt": to_int(x.get("trde_prica"))} for x in rows[:130]]
                self.bars.emit(code, name, out)
            except Exception as e:
                log(f"일봉 조회 실패 {code}: {e!r}")
                self.bars.emit(code, name, [])


class WsWorker(QThread):
    """웹소켓: 로그인, 조건식, 실시간 시세, 지수"""
    status = pyqtSignal(str)
    conditions = pyqtSignal(list)
    cond_result = pyqtSignal(list)
    cond_real = pyqtSignal(str, str)
    tick = pyqtSignal(str, dict)
    index = pyqtSignal(str, dict)

    def __init__(self, rest):
        super().__init__()
        self.rest = rest
        self.url = HOSTS[rest.cfg["mode"]][1]
        self.out = queue.Queue()
        self.running = True
        self.registered = set()

    def send(self, obj):
        self.out.put(obj)

    def run(self):
        while self.running:
            try:
                self.session()
            except Exception as e:
                log(f"웹소켓 끊김: {e!r}")
                self.status.emit("서버 연결이 끊겼습니다. 5초 뒤 다시 연결합니다.")
                time.sleep(5)

    def session(self):
        with ws_connect(self.url, open_timeout=10) as ws:
            ws.send(json.dumps({"trnm": "LOGIN", "token": self.rest.token}))
            while self.running:
                while not self.out.empty():
                    msg = self.out.get()
                    ws.send(json.dumps(msg, ensure_ascii=False))
                    log(f"보냄 {str(msg)[:300]}")
                try:
                    raw = ws.recv(timeout=0.2)
                except TimeoutError:
                    continue
                msg = json.loads(raw)
                trnm = msg.get("trnm")
                if trnm == "PING":
                    ws.send(raw)
                    continue
                if trnm != "REAL":
                    log(f"받음 {raw[:500]}")
                self.handle(msg)

    def handle(self, msg):
        trnm = msg.get("trnm")
        rc = msg.get("return_code")
        if trnm == "LOGIN":
            if rc not in (0, "0"):
                self.status.emit(f"로그인 실패: {msg.get('return_msg')}")
                return
            self.status.emit("로그인 완료")
            self.send({"trnm": "CNSRLST"})
            self.send({"trnm": "REG", "grp_no": "2", "refresh": "1",
                       "data": [{"item": ["001", "101"], "type": ["0J"]}]})
            if self.registered:                 # 다시 연결됐을 때 시세 재등록
                self.send({"trnm": "REG", "grp_no": "1", "refresh": "1",
                           "data": [{"item": sorted(self.registered), "type": ["0B"]}]})
        elif trnm == "CNSRLST":
            items = []
            for d in msg.get("data") or []:
                if isinstance(d, (list, tuple)) and len(d) >= 2:
                    items.append((str(d[0]), str(d[1])))
            self.conditions.emit(items)
        elif trnm == "CNSRREQ":
            if rc not in (0, "0", None):
                self.status.emit(f"조건검색 실패: {msg.get('return_msg')}")
                return
            out = [clean_code(d.get("jmcode") or d.get("9001") or "") for d in msg.get("data") or []]
            self.cond_result.emit([c for c in out if c])
        elif trnm == "REAL":
            for d in msg.get("data") or []:
                v = d.get("values") or {}
                typ = d.get("type")
                if typ == "02":
                    self.cond_real.emit(clean_code(v.get("9001") or d.get("item") or ""), v.get("843", "I"))
                elif typ == "0B":
                    self.tick.emit(clean_code(d.get("item", "")), v)
                elif typ == "0J":
                    self.index.emit(str(d.get("item", "")), v)

    def unwatch(self, codes):
        codes = [c for c in codes if c in self.registered]
        if codes:
            self.send({"trnm": "REMOVE", "grp_no": "1", "refresh": "",
                       "data": [{"item": codes, "type": ["0B"]}]})
            self.registered.difference_update(codes)

    def watch(self, codes):
        new = [c for c in codes if c not in self.registered]
        for i in range(0, len(new), 100):
            self.send({"trnm": "REG", "grp_no": "1", "refresh": "1",
                       "data": [{"item": new[i:i + 100], "type": ["0B"]}]})
        self.registered.update(new)


# ---------------- 화면 ----------------
class KTable(QTableWidget):
    """Delete 키로 삭제, (오른쪽 표) 끌어서 순서 바꾸기"""
    moved = pyqtSignal(list, int)       # 옮길 줄들, 놓은 위치
    delete_pressed = pyqtSignal()

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.delete_pressed.emit()
        else:
            super().keyPressEvent(e)

    def enable_drag(self):
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.setDragDropOverwriteMode(False)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QTableWidget.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)

    def dropEvent(self, e):
        if e.source() is not self:
            e.ignore()
            return
        pos = e.position().toPoint()
        idx = self.indexAt(pos)
        if idx.isValid():
            dst = idx.row() + (1 if pos.y() > self.visualRect(idx).center().y() else 0)
        else:
            dst = self.rowCount()
        rows = sorted({i.row() for i in self.selectedIndexes()})
        e.setDropAction(Qt.DropAction.IgnoreAction)     # 표가 직접 옮기지 않고 목록 순서만 바꿈
        e.accept()
        if rows:
            self.moved.emit(rows, dst)


JADE = QColor("#a8dcc8")


class RowPaint(QStyledItemDelegate):
    """표 기본 그리기: 선택 표시·점선 포커스(칸마다 생기는 짧은 세로 줄)는 없애고, 마우스를 누르고 있는 줄만 비취색"""
    def paint(self, p, opt, idx):
        o = QStyleOptionViewItem(opt)
        self.initStyleOption(o, idx)
        o.state &= ~(QStyle.StateFlag.State_Selected | QStyle.StateFlag.State_HasFocus | QStyle.StateFlag.State_MouseOver)
        if getattr(o.widget, "press_row", None) == idx.row():
            o.backgroundBrush = QBrush(JADE)
        style = o.widget.style() if o.widget else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, o, p, o.widget)


class TwoLine(QStyledItemDelegate):
    """1·2·3차, 손절 칸: 10·25일선은 매수가를 칸 맨 위, 검색식 값을 맨 아래 / 5일선은 가운데"""
    def paint(self, p, opt, idx):
        o = QStyleOptionViewItem(opt)
        self.initStyleOption(o, idx)
        top, _, bot = o.text.replace("\u2028", "\n").partition("\n")   # Qt가 줄바꿈을 \u2028로 바꿔 넘김
        o.text = ""
        o.state &= ~(QStyle.StateFlag.State_Selected | QStyle.StateFlag.State_HasFocus | QStyle.StateFlag.State_MouseOver)
        if getattr(o.widget, "press_row", None) == idx.row():
            o.backgroundBrush = QBrush(JADE)
        style = o.widget.style() if o.widget else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, o, p, o.widget)
        sel = False
        p.save()
        p.setFont(o.font)
        p.setPen(o.palette.color(QPalette.ColorRole.HighlightedText if sel else QPalette.ColorRole.Text))
        rc = o.rect.adjusted(4, 0, -4, 0)
        al = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        if not bot:                         # 검색식 값 없음(5일선 매매): 칸 가운데
            p.drawText(rc, al, top)
        else:                               # 10·25일선: 칸 높이를 3등분해 매수가는 위 1/3, 검색식 값은 아래 1/3
            third = rc.height() / 3
            p.drawText(QRect(rc.x(), rc.y(), rc.width(), int(third)), al, top)
            p.drawText(QRect(rc.x(), int(rc.y() + third * 2), rc.width(), rc.height() - int(third * 2)), al, bot)
        p.restore()


class Stock:
    def __init__(self, code, name=""):
        self.code, self.name = code, name
        self.cur = self.high = self.low = 0
        self.rate = 0.0
        self.bars = []
        self.loaded = False
        self.res = {"verdict": "조회중", "memo": ""}
        self.note = ""                  # 내가 쓴 메모
        self.alerted = ""
        self.cond_out = False           # 조건식에서 빠짐 (조건이탈)
        self.manual = ""                # 수동으로 고른 판정 ("" = 자동)
        self.manual_line = None         # 끌어서 옮긴 매매 구간 (5/10/25)
        self.last_line = None           # 마지막으로 매매 구간에 있던 선 (5/10/25)


class Window(QMainWindow):
    def __init__(self, rest):
        super().__init__()
        self.setWindowTitle("5·10·25일선 매매 자동창 (구간 정렬판 · 11-18 수정판)")
        self.resize(1700, 820)
        self.rest = rest
        self.prefs = load_prefs()
        self.stocks = {}
        self.order = []                 # 관심종목 순서
        self.names = {}
        self.cur_seq = None
        self.list_mtime = 0
        self.dirty = True
        self.removed = set()            # 삭제한 종목 (조건식이 다시 넣지 않게)
        self.memos = {}                 # 오른쪽 목록의 메모 줄: 키 -> 글
        self.memo_seq = 0
        rh = self.prefs.get("rowh", "")
        self.row_h = int(rh) if rh.isdigit() else ROW_H2   # 모든 종목 줄 높이 (마우스로 끌어서 바꾼 값)
        self.row_codes = []             # 왼쪽 표 각 줄의 종목 코드
        self.row_sections = []
        self.sizing = False
        self.build_ui()

        self.tr = TrWorker(rest) if rest else None
        self.ws = WsWorker(rest) if rest else None
        if rest:
            self.tr.master.connect(self.on_master)
            self.tr.bars.connect(self.on_bars)
            self.tr.start()
            self.ws.status.connect(self.status)
            self.ws.conditions.connect(self.on_conditions)
            self.ws.cond_result.connect(self.on_cond_result)
            self.ws.cond_real.connect(self.on_cond_real)
            self.ws.tick.connect(self.on_tick)
            self.ws.index.connect(self.on_index)
            self.ws.start()
            self.status("키움 서버에 연결하는 중…")

        self.ui_timer = QTimer(self)
        self.ui_timer.timeout.connect(self.redraw)
        self.ui_timer.start(1000)
        self.blinks = []                # 검색식 값 1% 이내 칸: (줄, 칸, 원래 바탕)
        self.blink_on = False
        self.blink_timer = QTimer(self)
        self.blink_timer.timeout.connect(self.blink)
        self.blink_timer.start(500)
        self.file_timer = QTimer(self)
        self.file_timer.timeout.connect(self.check_file)
        self.file_timer.start(5000)
        self.refresh_timer = QTimer(self)
        self.refresh_timer.timeout.connect(self.refetch)
        self.set_refresh()

    # ----- 화면 만들기 -----
    def build_ui(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(6, 6, 6, 4)

        top = QHBoxLayout()
        b = QPushButton("선별")
        b.setMinimumWidth(80)
        b.clicked.connect(self.refetch)
        top.addWidget(b)
        top.addSpacing(10)
        top.addWidget(QLabel("자동 갱신"))
        self.refresh_spin = QSpinBox()
        self.refresh_spin.setRange(1, 60)
        self.refresh_spin.setValue(self.prefs["refresh"])
        self.refresh_spin.valueChanged.connect(self.set_refresh)
        top.addWidget(self.refresh_spin)
        top.addWidget(QLabel("분"))
        self.ontop = QCheckBox("항상 위")
        self.ontop.setChecked(self.prefs["ontop"])
        self.ontop.toggled.connect(self.set_ontop)
        top.addWidget(self.ontop)
        self.sound = QCheckBox("알림음")
        self.sound.setChecked(self.prefs["sound"])
        self.sound.toggled.connect(self.save)
        top.addWidget(self.sound)
        self.overseas = QCheckBox("전일 미국지수 -2% 이상 하락")
        self.overseas.toggled.connect(self.recalc_all)
        top.addWidget(self.overseas)
        top.addSpacing(10)
        b = QPushButton("관심종목 붙여넣기")
        b.setToolTip("[0130]에서 복사한 종목(코드나 이름)을 붙여넣습니다. 목록이 이것으로 바뀝니다.")
        b.clicked.connect(self.paste_list)
        top.addWidget(b)
        b = QPushButton("선택 종목 삭제 (Del)")
        b.setToolTip("표에서 고른 종목을 후보에서 뺍니다. Delete 키도 됩니다. Ctrl/Shift로 여러 개 선택 가능")
        b.clicked.connect(self.delete_selected)
        top.addWidget(b)
        b = QPushButton("목록 파일 열기")
        b.clicked.connect(self.open_list_file)
        top.addWidget(b)
        top.addSpacing(10)
        self.cond_box = QComboBox()
        self.cond_box.setMinimumWidth(160)
        self.cond_box.setPlaceholderText("조건식 (선택)")
        top.addWidget(self.cond_box)
        self.cond_box.currentIndexChanged.connect(self.on_cond_changed)
        self.cond_btn = QPushButton("조건식 다시 받기")
        self.cond_btn.setEnabled(False)
        self.cond_btn.clicked.connect(self.start_condition)
        top.addWidget(self.cond_btn)
        top.addStretch(1)
        self.done_lbl = QLabel("")
        top.addWidget(self.done_lbl)
        lay.addLayout(top)

        self.index_lbl = QLabel("코스피 -   |   코스닥 -")
        f = QFont()
        f.setBold(True)
        self.index_lbl.setFont(f)
        self.index_lbl.setTextFormat(Qt.TextFormat.RichText)
        lay.addWidget(self.index_lbl)

        split = QSplitter(Qt.Orientation.Horizontal)
        self.table = self.make_table(COLS)
        self.table.setHorizontalHeaderLabels([HEAD.get(c, c) for c in COLS])
        self.two_line = TwoLine(self.table)
        for c in HEAD:
            self.table.setItemDelegateForColumn(C[c], self.two_line)
        self.table.cellDoubleClicked.connect(lambda r, c: self.open_news(self.table, r))
        self.table.delete_pressed.connect(self.delete_selected)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        vh = self.table.verticalHeader()            # 종목 줄 맨 아랫선을 마우스로 끌어서 높이 조절
        vh.setMinimumSectionSize(20)
        vh.sectionResized.connect(self.on_row_resized)
        self.drag_row = None
        self.table.viewport().setMouseTracking(True)
        self.table.viewport().installEventFilter(self)
        self.table.press_row = None                 # 마우스를 누르고 있는 줄만 비취색 (RowPaint), 떼면 원래 바탕색
        self.table.setItemDelegate(RowPaint(self.table))
        self.table.setColumnWidth(0, 66)        # 판정 칸 너비 ("조건이탈"까지 보이게)
        split.addWidget(self.table)
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        t = QLabel("관심종목 5일선 후보  (끌어서 순서 변경 · Del 삭제 · 우클릭 빈칸/메모)")
        t.setFont(f)
        rl.addWidget(t)
        self.rtable = self.make_table(RCOLS)
        self.rtable.cellDoubleClicked.connect(lambda r, c: self.open_news(self.rtable, r))
        self.rtable.delete_pressed.connect(self.delete_selected)
        self.table.itemSelectionChanged.connect(lambda: self.table.selectedIndexes() and setattr(self, "last_table", self.table))
        self.rtable.itemSelectionChanged.connect(lambda: self.rtable.selectedIndexes() and setattr(self, "last_table", self.rtable))
        self.rtable.enable_drag()
        self.rtable.moved.connect(self.move_rows)
        self.rtable.setEditTriggers(QTableWidget.EditTrigger.DoubleClicked | QTableWidget.EditTrigger.EditKeyPressed)
        self.rtable.itemChanged.connect(self.on_ritem_changed)
        for tb in (self.table, self.rtable):
            tb.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            tb.customContextMenuRequested.connect(lambda pos, tb=tb: self.context_menu(tb, pos))
        rl.addWidget(self.rtable)
        split.addWidget(right)
        right.hide()                                # 오른쪽 관심종목 표는 없앰 (왼쪽 표에서 우클릭·끌기로 관리)
        self.table.enable_drag()                    # 종목 줄을 끌어서 순서 이동
        self.table.moved.connect(self.move_left)
        split.setSizes([1320, 380])
        lay.addWidget(split, 1)

        self.status_lbl = QLabel()
        self.status_lbl.setFrameShape(QFrame.Shape.StyledPanel)
        lay.addWidget(self.status_lbl)
        hint = QLabel("종목 더블클릭 → 네이버 뉴스(최신순) · Del 키 → 종목 삭제 · 우클릭 → 종목 추가·삭제·판정(자동/수동)·메모 · 종목 끌어서 순서 이동 · 악재 뉴스, CB·유증·권리락 일정은 직접 확인 · 조회 전용(주문 없음) · "
                      "판정 색: 근접·매수가능 연분홍, 대기 흰색/연회색, 이탈 파란 글씨 · 현재가가 검색식 값 1% 이내면 그 칸 분홍 깜빡임, 1~2%면 분홍")
        hint.setStyleSheet("color:#777")
        lay.addWidget(hint)
        self.setCentralWidget(w)
        self.set_ontop(self.ontop.isChecked())

    def make_table(self, cols):
        t = KTable(0, len(cols))
        t.setHorizontalHeaderLabels(cols)
        cf = t.font()
        cf.setWeight(QFont.Weight.DemiBold)             # 내용 글씨: 조금 더 진하게
        t.setFont(cf)
        t.verticalHeader().setVisible(False)
        t.verticalHeader().setDefaultSectionSize(33)     # 줄 높이 (기존 22의 1.5배)
        t.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        t.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        t.horizontalHeader().setStretchLastSection(True)
        t.horizontalHeader().setStyleSheet(          # 머리글: 진한 글씨
            "QHeaderView::section{font-weight:900; color:#000; background:#fff59d;"
            " border:1px solid #b8bec9; padding:2px 4px;}")
        return t

    def status(self, text):
        self.status_lbl.setText(f"[{datetime.datetime.now():%H:%M:%S}] {text}")
        log(f"상태 {text}")

    def save(self):
        self.prefs.update(refresh=self.refresh_spin.value(),
                          ontop=self.ontop.isChecked(), sound=self.sound.isChecked())
        try:
            save_prefs(self.prefs)
        except OSError as e:
            log(f"설정 저장 실패 {e!r}")

    def set_ontop(self, on):
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, on)
        self.show()
        self.save()

    def set_refresh(self):
        self.refresh_timer.start(self.refresh_spin.value() * 60 * 1000)
        self.save()

    def on_principal(self):
        self.dirty = True
        self.save()

    # ----- 관심종목 목록 -----
    def on_master(self, names):
        self.names = names
        self.status(f"종목 목록 {len(names):,}개 준비. 관심종목 목록을 읽습니다.")
        self.list_mtime = 0
        self.check_file()

    def parse_codes(self, text):
        codes = []
        for line in text.splitlines():
            found = re.findall(r"(?<!\d)[AJQ]?(\d{6})(?!\d)", line)
            if not found:
                for tok in re.split(r"[\t,;|]+", line):
                    tok = tok.strip().lstrip("*").strip()
                    if tok in self.names:
                        found = [self.names[tok]]
                        break
            for c in found[:1]:
                if c not in codes:
                    codes.append(c)
        return codes

    def check_file(self):
        if not os.path.exists(LISTFILE):
            with open(LISTFILE, "w", encoding="utf-8") as f:
                f.write("# 관심종목 5일선 후보. 한 줄에 한 종목 (코드나 이름). '관심종목 붙여넣기' 버튼을 쓰면 자동으로 채워집니다.\n")
        m = os.path.getmtime(LISTFILE)
        if m == self.list_mtime or (self.rest and not self.names):
            return
        self.list_mtime = m
        with open(LISTFILE, encoding="utf-8", errors="ignore") as f:
            lines = f.read().splitlines()
        self.memos.clear()
        entries, notes, manual = [], {}, {}
        for line in lines:
            if line.startswith("#"):
                continue
            if line.startswith("@"):                       # 메모 줄
                entries.append(self.new_memo(line[1:].strip()))
                continue
            codes = self.parse_codes(line)
            if codes and codes[0] not in entries:
                entries.append(codes[0])
                parts = line.split("\t")
                if len(parts) >= 3 and parts[2].strip():
                    notes[codes[0]] = parts[2].strip()
                if len(parts) >= 4 and parts[3].strip().split("@")[0] in MANUAL_V:
                    manual[codes[0]] = parts[3].strip()
        codes = [e for e in entries if e not in self.memos]
        self.set_list(codes, source="목록 파일")
        self.order = entries + [c for c in self.order if c not in entries]
        for c in self.stocks:
            man, _, ml = manual.get(c, "").partition("@")
            self.stocks[c].manual = man
            self.stocks[c].manual_line = int(ml) if ml.isdigit() else None
        for c, n in notes.items():
            if c in self.stocks:
                self.stocks[c].note = n
                if self.stocks[c].loaded:
                    self.recalc(self.stocks[c])

    def new_memo(self, text=""):
        self.memo_seq += 1
        key = f"m:{self.memo_seq}"
        self.memos[key] = text
        return key

    def rlist(self):
        """오른쪽 목록에 보이는 줄 (종목 코드와 메모 키) 순서대로"""
        return [x for x in self.order if x in self.stocks or x in self.memos]

    def paste_list(self):
        text = QApplication.clipboard().text()
        codes = self.parse_codes(text)
        if not codes:
            QMessageBox.information(self, "붙여넣기", "복사한 내용에서 종목을 찾지 못했습니다.\n"
                                    "[0130]에서 종목을 선택해 복사(Ctrl+C)하거나, 엑셀로 내보낸 뒤 종목 열을 복사하세요.")
            return
        self.removed -= set(codes)
        self.memos.clear()
        self.set_list(codes, source="붙여넣기")
        self.save_order()

    def write_list(self, entries):
        lines = []
        for c in entries:
            if c in self.memos:
                lines.append("@" + self.memos[c].replace("\n", " "))
            else:
                st = self.stocks.get(c)
                name = (st.name if st else "") or self.name_of(c)
                note = (st.note if st else "").replace("\t", " ")
                man = (st.manual + (f"@{st.manual_line}" if st.manual_line else "")) if st and st.manual else ""
                lines.append(f"{c}\t{name}" + (f"\t{note}" if note or man else "") + (f"\t{man}" if man else ""))
        with open(LISTFILE, "w", encoding="utf-8") as f:
            f.write("# 관심종목 5일선 후보 (저장 " + datetime.datetime.now().strftime("%Y-%m-%d %H:%M") + ")\n")
            f.write("\n".join(lines) + ("\n" if lines else ""))
        self.list_mtime = os.path.getmtime(LISTFILE)

    def save_order(self):
        self.write_list([c for c in self.order
                         if c in self.memos or (c in self.stocks and not self.stocks[c].from_cond)])

    def selected_codes(self):
        t = getattr(self, "last_table", None) or self.table
        codes = []
        for r in sorted({i.row() for i in t.selectedIndexes()}):
            it = t.item(r, 0)
            c = it.data(Qt.ItemDataRole.UserRole) if it else None
            if c and c not in codes:
                codes.append(c)
        return codes

    def delete_selected(self):
        codes = self.selected_codes()
        if not codes:
            self.status("삭제할 종목을 먼저 표에서 고르세요.")
            return
        names = [self.stocks[c].name or c for c in codes if c in self.stocks]
        names += ["메모 줄" for c in codes if c in self.memos]
        for c in codes:
            if c in self.memos:
                self.memos.pop(c)
            else:
                self.stocks.pop(c, None)
                self.removed.add(c)
            if c in self.order:
                self.order.remove(c)
        if self.ws:
            self.ws.unwatch([c for c in codes if not c.startswith("m:")])
        self.save_order()
        self.table.clearSelection()
        self.rtable.clearSelection()
        self.status(f"삭제: {', '.join(names)}")
        self.dirty = True
        self.redraw()

    def move_rows(self, rows, dst):
        vis = self.rlist()
        codes = [vis[r] for r in rows if r < len(vis)]
        rest = [c for c in vis if c not in codes]
        i = dst - sum(1 for r in rows if r < dst)
        i = max(0, min(i, len(rest)))
        self.order = rest[:i] + codes + rest[i:] + [c for c in self.order if c not in vis]
        self.save_order()
        self.dirty = True
        self.redraw()
        self.rtable.clearSelection()
        for k in range(i, i + len(codes)):
            self.rtable.selectRow(k)

    def open_list_file(self):
        self.check_file()
        os.startfile(LISTFILE) if hasattr(os, "startfile") else None

    def name_of(self, code):
        for n, c in self.names.items():
            if c == code:
                return n
        return ""

    def set_list(self, codes, source=""):
        keep = set(codes) | {c for c, s in self.stocks.items() if getattr(s, "from_cond", False)}
        for c in list(self.stocks):
            if c not in keep:
                del self.stocks[c]
        self.order = [c for c in codes] + [c for c in self.order if c in keep and c not in codes]
        for c in codes:
            self.add_stock(c)
        self.status(f"{source}에서 {len(codes)}종목을 읽었습니다. 일봉을 조회합니다.")
        self.dirty = True

    def add_stock(self, code, from_cond=False):
        if code in self.stocks:
            return
        st = Stock(code, self.name_of(code))
        st.from_cond = from_cond
        self.stocks[code] = st
        if code not in self.order:
            self.order.append(code)
        if self.tr:
            self.tr.add(code, st.name)
        if self.ws:
            self.ws.watch([code])

    def refetch(self):
        for c, st in self.stocks.items():
            if self.tr:
                self.tr.add(c, st.name)
        self.status(f"{len(self.stocks)}종목 일봉 다시 조회")

    # ----- 조건식 (선택) -----
    def on_conditions(self, items):
        self.cond_box.blockSignals(True)
        self.cond_box.clear()
        for seq, name in items:
            self.cond_box.addItem(name, seq)
        saved = self.prefs.get("cond", "")
        pick = next((i for i, (_, n) in enumerate(items) if saved and n == saved), None)
        if pick is None:
            pick = next((i for i, (_, n) in enumerate(items) if "5일선" in n), None)
        if pick is not None:
            self.cond_box.setCurrentIndex(pick)
        self.cond_box.blockSignals(False)
        self.cond_btn.setEnabled(bool(items))
        self.cur_seq = None                 # 새로 접속하면 서버 쪽 조건검색은 비어 있음
        if pick is not None:                # 조건식 받기를 누르지 않아도 자동으로 편입 시작
            self.start_condition()

    def on_cond_changed(self, _i):
        if self.ws and self.cond_box.currentData() is not None:
            self.start_condition()

    def start_condition(self):
        seq = self.cond_box.currentData()
        if self.cur_seq is not None:
            self.ws.send({"trnm": "CNSRCLR", "seq": self.cur_seq})
        self.ws.send({"trnm": "CNSRREQ", "seq": seq, "search_type": "1", "stex_tp": "K"})
        self.cur_seq = seq
        self.prefs["cond"] = self.cond_box.currentText()
        self.save()
        self.status(f"'{self.cond_box.currentText()}' 조건식 자동 편입 중 (새로 들어오는 종목도 자동 추가)")

    def on_cond_result(self, codes):
        for c in codes:
            if c not in self.removed:
                self.add_stock(c, from_cond=True)
        self.dirty = True
        self.status(f"조건식 결과 {len(codes)}종목 추가")

    def on_cond_real(self, code, kind):
        st = self.stocks.get(code)
        if kind == "I" and code and code not in self.removed:
            if st:
                if st.cond_out:
                    st.cond_out = False
                    self.dirty = True
                return
            self.add_stock(code, from_cond=True)
            self.status(f"조건식 신규 편입: {code}")
            self.dirty = True
        elif kind == "D" and st and not st.cond_out:      # 조건 이탈 → 이탈 칸으로 자동 이동
            st.cond_out = True
            self.status(f"조건 이탈: {st.name or code}")
            self.dirty = True

    # ----- 데이터 -----
    def on_bars(self, code, name, bars):
        st = self.stocks.get(code)
        if not st:
            return
        if name:
            st.name = name
        st.bars = bars
        st.loaded = True
        if not st.cur and len(bars) >= 2 and bars[1]["close"]:   # 장 마감 후·주말: 마지막 일봉으로 등락률
            st.rate = (bars[0]["close"] / bars[1]["close"] - 1) * 100
        self.recalc(st)
        if all(s.loaded for s in self.stocks.values()):
            self.done_lbl.setText(f"완료 {datetime.datetime.now():%H:%M:%S} · {len(self.stocks)}종목")

    def on_tick(self, code, v):
        st = self.stocks.get(code)
        if not st:
            return
        st.cur = to_int(v.get("10", 0))
        st.rate = to_float(v.get("12", 0))
        st.high = to_int(v.get("17", 0)) or st.high
        st.low = to_int(v.get("18", 0)) or st.low
        if st.loaded:
            self.recalc(st)

    def on_index(self, item, v):
        name = {"001": "코스피", "101": "코스닥"}.get(item)
        if not name:
            return
        if not hasattr(self, "idx"):
            self.idx = {}
        self.idx[name] = (abs(to_float(v.get("10", 0))), to_float(v.get("12", 0)))
        parts = []
        for n in ("코스피", "코스닥"):
            if n in self.idx:
                p, r = self.idx[n]
                col = "#d6202a" if r > 0 else "#1f5fd1" if r < 0 else "#333"
                arrow = "▲" if r > 0 else "▼" if r < 0 else ""
                parts.append(f"{n} <span style='color:{col}'>{p:,.2f} {arrow}{r:+.2f}%</span>")
        self.index_lbl.setText("&nbsp;&nbsp;|&nbsp;&nbsp;".join(parts))

    def recalc_all(self):
        for st in self.stocks.values():
            if st.loaded:
                self.recalc(st)

    def recalc(self, st):
        if not st.bars:
            st.res = {"verdict": "제외", "memo": "일봉 조회 실패"}
        else:
            st.res = plan(st.bars, st.cur, st.high, st.low, self.overseas.isChecked())
        if st.res.get("line"):
            st.last_line = st.res["line"]
        v = st.res.get("verdict", "")
        if v in ("1차", "2차", "3차", "4차", "이탈", "근접") and v != st.alerted:
            if v != "근접" or not st.alerted:
                self.status(f"{st.name or st.code} {v} (현재가 {st.cur:,}, {st.res.get('line')}일선 매매)")
                if self.sound.isChecked():
                    QApplication.beep()
            st.alerted = v
        self.dirty = True

    # ----- 표 그리기 -----
    def redraw(self):
        if not self.dirty or self.drag_row is not None:     # 줄 높이를 끄는 중에는 다시 그리지 않음
            return
        self.dirty = False
        groups = {}
        for x in self.stocks.values():
            v = x.res.get("verdict")
            if x.manual:                                 # 수동 판정이 우선
                ln = "out" if x.manual == "이탈" else None if x.manual == "제외" else \
                    (x.manual_line or x.res.get("line") or x.last_line)
            else:
                out = x.cond_out or v == "이탈" or (v == "제외" and x.last_line)   # 매매 구간에 있다가 이탈·제외·조건이탈
                ln = "out" if out else x.res.get("line")
            groups.setdefault(ln, []).append(x)
        entries = []
        self.row_sections = []                           # 각 줄이 속한 구간
        pos = {c: i for i, c in enumerate(self.order)}
        for ln in (5, 10, 25, "out", None):             # 구간 순서로 줄 세우기 (구간 제목 줄은 없음), 구간 안에서는 내가 끌어 놓은 순서
            lst = sorted(groups.get(ln, []), key=lambda s: pos.get(s.code, 1e9))
            self.row_sections += [ln] * len(lst)
            entries += [("st", x) for x in lst]
        t = self.table
        sel = self.selected_code(t)
        self.blinks = []
        t.clearSpans()
        self.sizing = True
        t.setRowCount(len(entries))
        t.setVerticalHeaderLabels([""] * len(entries))
        self.row_codes = [st.code if kind == "st" else None for kind, st in entries]
        normal = QFont()
        normal.setWeight(QFont.Weight.DemiBold)    # 내용 글씨: 조금 더 진하게
        wait_i = 0
        for r, (kind, st) in enumerate(entries):
            if r and self.row_sections[r] != self.row_sections[r - 1]:
                wait_i = 0                               # 구간이 바뀌면 대기 줄 번갈이 색 처음부터
            res = st.res
            v = res.get("verdict", "")
            if st.cond_out:
                v = "조건이탈"
            if st.manual:
                v = st.manual
            line = res.get("line") or (st.last_line if (st.cond_out or v in ("이탈", "제외")) else None)
            if st.manual and st.manual_line:
                line = st.manual_line
            cur = st.cur or res.get("cur") or 0
            buys = res.get("buys") or []
            fb = res.get("fbuys") or []
            vals = {
                "판정": v + ("*" if st.manual else ""), "종목": st.name or st.code,
                "현재가": f"{cur:,}" if cur else "",
                "등락률": f"{st.rate:+.2f}%" if (st.cur or st.rate) else "",
                "매매선": f"{line}일선" if line else res.get("next", ""),
                "이격률": f"{res['gap']:.1f}%" if res.get("gap") and line != 5 else "",   # 5일선 매매는 이격도와 무관
                "등급": res.get("grade", ""),
                "비중": ("/".join(f"{w:g}" for _, w in buys) + f" = {sum(w for _, w in buys):g}%") if buys else "",
                "평단": f"{res['avg']:,.0f}" if res.get("avg") else "",
                "익절①": f"{res['tp1']:,}" if res.get("tp1") else "",
                "익절②": f"{res['tp2']:,}" if res.get("tp2") else "",
                "손절": (f"{res['stop']:,}" if res.get("stop") else "")
                        + (f"\n{res['fstop']:,.0f}" if res.get("fstop") else ""),
                "손절까지": f"{(res['stop'] / cur - 1) * 100:+.1f}%" if res.get("stop") and cur else "",
                "메모": " · ".join(x for x in (f"[내 메모] {st.note}" if st.note else "", res.get("memo", "")) if x),
            }
            if len(buys) > 3:                                  # 4차 칸은 없앴으니 메모에 적음
                vals["메모"] = f"4차 {buys[3][0]:,} ({buys[3][1]:g}%)" + (" · " + vals["메모"] if vals["메모"] else "")
            for i in range(3):
                vals[f"{i + 1}차"] = f"{buys[i][0]:,} ({buys[i][1]:g}%)" if i < len(buys) else ""
                if i < 3 and i < len(fb):                         # 아랫줄: 검색식 값
                    vals[f"{i + 1}차"] += f"\n{fb[i]:,.0f}"
            bg = None
            if v == "근접" or v[:1] in "1234":
                bg = PINK
            elif v == "대기":
                bg = WAIT_BG[wait_i % 2]
                wait_i += 1
            elif v in ("이탈", "조건이탈"):
                bg = QColor("#ffffff")                     # 이탈 칸 종목: 흰 바탕 (판정 글씨만 파랑)
            t.setRowHeight(r, self.row_h)
            for name, text in vals.items():
                it = t.item(r, C[name])
                if it is None:
                    it = QTableWidgetItem()
                    t.setItem(r, C[name], it)
                it.setText(text)
                it.setFont(normal)
                it.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsDragEnabled)
                it.setData(Qt.ItemDataRole.UserRole, st.code)
                if name not in ("종목", "메모", "판정", "매매선", "등급"):
                    it.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                if name == "메모":                                  # 메모는 칸 맨 위에서부터
                    it.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
                fg = None
                if name == "등락률":
                    fg = RED if st.rate > 0 else BLUE if st.rate < 0 else None
                if name == "매매선":
                    fg = LINE_FG.get(line)
                if name == "이격률" and line != 5 and (res.get("gap") or 0) >= BIG_GAP:
                    fg = RED
                if name == "판정" and v == "제외":
                    fg = GRAY
                if name == "판정" and v in ("이탈", "조건이탈"):
                    fg = BLUE
                it.setForeground(fg or QColor("#000"))
                it.setBackground(bg or QColor(0, 0, 0, 0))
                it.setToolTip("")
            for i, f in enumerate(fb[:3]):                  # 현재가가 검색식 값 1% 이내 → 깜빡임, 1~2% → 분홍 고정
                if cur and f and abs(cur - f) <= f * 0.01:
                    self.blinks.append((r, C[f"{i + 1}차"], bg or QColor(0, 0, 0, 0)))
                elif cur and f and abs(cur - f) <= f * 0.02:
                    t.item(r, C[f"{i + 1}차"]).setBackground(BLINK)
            if st.manual:
                t.item(r, C["판정"]).setToolTip("수동 판정 (우클릭 → 판정 → 자동으로 되돌리기)")
            if buys:
                t.item(r, C["비중"]).setToolTip(f"추가매수 간격 -{res.get('step', 0):g}%")
                t.item(r, C["익절①"]).setToolTip(res.get("tp_text", ""))
            if st.code == sel:
                t.selectRow(r)
        self.sizing = False
        self.fit_columns()

        rt = self.rtable
        if rt.isHidden() or rt.parentWidget().isHidden():      # 오른쪽 표를 숨겼으면 그리지 않음
            return
        if rt.state() == QTableWidget.State.EditingState:     # 입력 중이면 오른쪽은 건드리지 않음
            return
        rt.blockSignals(True)
        rt.clearSpans()
        keys = self.rlist()
        rt.setRowCount(len(keys))
        num = 0
        for r, code in enumerate(keys):
            if code in self.memos:
                for c in range(rt.columnCount()):
                    it = QTableWidgetItem("" if c != 1 else self.memos[code])
                    it.setData(Qt.ItemDataRole.UserRole, code)
                    fl = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsDragEnabled
                    if c == 1:
                        fl |= Qt.ItemFlag.ItemIsEditable
                        it.setToolTip("더블클릭해서 종목(코드나 이름) 또는 메모를 입력하세요")
                    it.setFlags(fl)
                    it.setBackground(MEMO_BG)
                    it.setForeground(QColor("#6b5400"))
                    rt.setItem(r, c, it)
                rt.setSpan(r, 1, 1, rt.columnCount() - 1)
                continue
            num += 1
            st = self.stocks[code]
            res = st.res
            vals = [str(num), st.name or code, f"{st.rate:+.2f}%" if (st.cur or st.rate) else "",
                    f"{res['g510']:.1f}%" if "g510" in res else "",
                    f"{res['g1025']:.1f}%" if "g1025" in res else "",
                    (f"{res['line']}선" if res.get("line") else res.get("next", "")) or ""]
            for c, text in enumerate(vals):
                it = rt.item(r, c)
                if it is None:
                    it = QTableWidgetItem()
                    rt.setItem(r, c, it)
                it.setText(text)
                it.setData(Qt.ItemDataRole.UserRole, code)
                it.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsDragEnabled)
                it.setBackground(QColor(0, 0, 0, 0))
                it.setToolTip(f"[내 메모] {st.note}" if st.note else "")
                if c in (0, 2, 3, 4):
                    it.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                fg = None
                if c == 2:
                    fg = RED if st.rate > 0 else BLUE if st.rate < 0 else None
                if c in (3, 4) and text and to_float(text.rstrip("%")) >= MIN_GAP:
                    fg = RED
                it.setForeground(fg or QColor("#000"))
        rt.blockSignals(False)

    # ----- 우클릭 메뉴: 빈칸 추가, 종목 추가, 메모 -----
    def context_menu(self, tb, pos):
        idx = tb.indexAt(pos)
        key = None
        if idx.isValid():
            it = tb.item(idx.row(), 0)
            key = it.data(Qt.ItemDataRole.UserRole) if it else None
            if key:
                tb.selectRow(idx.row())
                self.last_table = tb
        m = QMenu(self)
        a_blank = m.addAction("빈칸 추가 (종목이나 메모 입력)") if tb is self.rtable else None
        a_add = m.addAction("종목 추가…")
        a_note = a_del = None
        a_man = {}
        if key in self.stocks:
            m.addSeparator()
            st = self.stocks[key]
            sub = m.addMenu("판정 (자동/수동)")
            for name in ["자동"] + MANUAL_V:
                a = sub.addAction(name)
                a.setCheckable(True)
                a.setChecked((st.manual or "자동") == name)
                a_man[a] = "" if name == "자동" else name
            a_note = m.addAction("메모 고치기…" if st.note else "메모 쓰기…")
            a_del = m.addAction("이 종목 삭제")
        elif key in self.memos:
            m.addSeparator()
            a_del = m.addAction("이 메모 줄 삭제")
        act = m.exec(tb.viewport().mapToGlobal(pos))
        if act is None:
            return
        if act in a_man:
            st = self.stocks[key]
            st.manual, st.manual_line = a_man[act], None
            self.save_order()
            self.status(f"{st.name or key} 판정: {st.manual or '자동'}")
            self.dirty = True
            self.redraw()
        elif act == a_blank:
            new = self.new_memo("")
            self.insert_after(key, new)
            self.save_order()
            self.dirty = True
            self.redraw()
            r = self.rlist().index(new)
            self.rtable.setCurrentCell(r, 1)
            self.rtable.editItem(self.rtable.item(r, 1))
        elif act == a_add:
            text, ok = QInputDialog.getText(self, "종목 추가", "종목코드 또는 종목명:")
            if ok and text.strip():
                self.add_by_text(text.strip(), after=key)
        elif act == a_note:
            st = self.stocks[key]
            text, ok = QInputDialog.getText(self, "메모", f"{st.name or key} 메모 (비우면 삭제):", text=st.note)
            if ok:
                st.note = text.strip()
                self.save_order()
                self.dirty = True
                self.redraw()
        elif act == a_del:
            self.delete_selected()

    def move_left(self, rows, dst):
        """왼쪽 표에서 끌어 놓은 종목을 놓은 자리로 (구간은 판정에 따라 자동)"""
        codes = [self.row_codes[r] for r in rows if r < len(self.row_codes) and self.row_codes[r]]
        if not codes:
            return
        sec = self.row_sections[dst] if dst < len(self.row_sections) else None   # 놓은 구간
        for c in codes:                                   # 다른 구간에 놓으면 판정을 그 구간에 맞게 수동으로
            st = self.stocks[c]
            if self.row_sections[self.row_codes.index(c)] == sec:
                continue
            if sec == "out":
                st.manual, st.manual_line = "이탈", None
            elif sec is None:
                st.manual, st.manual_line = "제외", None
            else:
                st.manual, st.manual_line = "대기", sec
            self.status(f"{st.name or c} → {LINE_NAME[sec]} (판정 수동: {st.manual})")
        target = next((c for c in self.row_codes[dst:] if c and c not in codes), None)
        self.order = [c for c in self.order if c not in codes]
        i = self.order.index(target) if target in self.order else len(self.order)
        self.order[i:i] = codes
        self.save_order()
        self.dirty = True
        self.redraw()

    def insert_after(self, anchor, key):
        if key in self.order:
            self.order.remove(key)
        i = self.order.index(anchor) + 1 if anchor in self.order else len(self.order)
        self.order.insert(i, key)

    def resolve(self, text):
        m = re.fullmatch(r"[AJQ]?(\d{6})(\s+.*)?", text)
        if m:
            return m.group(1)
        return self.names.get(text.lstrip("*").strip())

    def add_by_text(self, text, after=None, replace=None):
        code = self.resolve(text)
        if not code:
            QMessageBox.information(self, "종목 추가", f"'{text}' 종목을 찾지 못했습니다. 종목코드 6자리로 넣어 보세요.")
            return False
        self.removed.discard(code)
        if replace in self.order:
            if code in self.stocks:
                self.order.remove(replace)
            else:
                self.order[self.order.index(replace)] = code     # 빈칸 자리에 그대로
            self.memos.pop(replace, None)
        if code in self.stocks:
            self.status(f"{self.stocks[code].name or code}은(는) 이미 목록에 있습니다.")
        else:
            if code not in self.order:
                self.insert_after(after, code)
            self.add_stock(code)
            self.status(f"종목 추가: {self.stocks[code].name or code}")
        self.save_order()
        self.dirty = True
        self.redraw()
        return True

    def on_ritem_changed(self, it):
        key = it.data(Qt.ItemDataRole.UserRole)
        if key not in self.memos or it.column() != 1:
            return
        text = it.text().strip()
        if text and self.resolve(text):               # 종목이면 종목으로 바꿈
            QTimer.singleShot(0, lambda: self.add_by_text(text, replace=key))
            return
        self.memos[key] = text                        # 아니면 메모로 저장
        self.save_order()
        self.dirty = True

    def row_edge(self, y):
        """마우스가 종목 줄 맨 아랫선(±4px) 위에 있으면 그 줄 번호"""
        t = self.table
        for r in (t.rowAt(y), t.rowAt(y - 5)):
            if 0 <= r < len(self.row_codes) and self.row_codes[r]:
                bottom = t.rowViewportPosition(r) + t.rowHeight(r)
                if abs(y - bottom) <= 4:
                    return r
        return None

    def eventFilter(self, obj, e):
        if obj is not self.table.viewport():
            return super().eventFilter(obj, e)
        et = e.type()
        if et == QEvent.Type.MouseButtonPress and e.button() == Qt.MouseButton.LeftButton:
            self.table.press_row = self.table.rowAt(int(e.position().y()))
            obj.update()
        elif et == QEvent.Type.MouseButtonRelease and self.table.press_row is not None:
            self.table.press_row = None
            obj.update()
        if et == QEvent.Type.MouseMove:
            y = int(e.position().y())
            if self.drag_row is not None:               # 끈 거리를 위쪽 종목 줄 수로 나눠 경계선이 마우스를 그대로 따라오게
                r, y0, h0 = self.drag_row, self.drag_y0, self.drag_h0
                k = sum(1 for c in self.row_codes[:r + 1] if c)
                self.set_all_heights(max(20, round(h0 + (y - y0) / max(k, 1))))
                return True
            obj.setCursor(Qt.CursorShape.SizeVerCursor if self.row_edge(y) is not None else Qt.CursorShape.ArrowCursor)
        elif et == QEvent.Type.MouseButtonPress and e.button() == Qt.MouseButton.LeftButton:
            r = self.row_edge(int(e.position().y()))
            if r is not None:
                self.drag_row = r
                self.drag_y0 = int(e.position().y())
                self.drag_h0 = self.table.rowHeight(r)
                return True
        elif et == QEvent.Type.MouseButtonRelease and self.drag_row is not None:
            self.drag_row = None
            self.save_rowh()                        # 놓을 때 한 번만 저장
            return True
        return super().eventFilter(obj, e)

    def fit_columns(self):
        """메모 칸을 남는 폭의 80%로 줄이고, 줄인 20%를 다른 칸에 고르게 나눔"""
        t = self.table
        hh = t.horizontalHeader()
        memo = C["메모"]
        need = []
        for c in range(len(COLS)):
            if c == memo:
                need.append(0)
                continue
            w = max(t.sizeHintForColumn(c), hh.sectionSizeHint(c))
            need.append(66 if c == 0 else w)
        free = t.viewport().width() - sum(need)
        extra = int(free * MEMO_CUT / (len(COLS) - 1)) if free > 0 else 0
        for c, w in enumerate(need):
            if c != memo:
                hh.setSectionResizeMode(c, QHeaderView.ResizeMode.Interactive)
                t.setColumnWidth(c, w + extra)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if hasattr(self, "table"):
            QTimer.singleShot(0, self.fit_columns)

    def set_all_heights(self, h):
        if h == self.row_h:
            return
        t = self.table
        t.setUpdatesEnabled(False)
        self.sizing = True
        for i, code in enumerate(self.row_codes):
            if code:
                t.setRowHeight(i, h)
        self.sizing = False
        t.setUpdatesEnabled(True)
        self.row_h = h

    def on_row_resized(self, r, _old, new):
        if self.sizing or r >= len(self.row_codes) or not self.row_codes[r]:
            return
        self.row_h = new
        self.sizing = True                          # 한 줄을 끌면 모든 종목 줄이 같은 높이로
        for i, code in enumerate(self.row_codes):
            if code and i != r:
                self.table.setRowHeight(i, new)
        self.sizing = False
        if self.drag_row is None:
            self.save_rowh()

    def save_rowh(self):
        self.prefs["rowh"] = str(self.row_h)
        self.save()

    def blink(self):
        self.blink_on = not self.blink_on
        for r, c, base in self.blinks:
            it = self.table.item(r, c)
            if it:
                it.setBackground(BLINK if self.blink_on else base)

    def selected_code(self, t):
        r = t.currentRow()
        it = t.item(r, 0) if r >= 0 else None
        return it.data(Qt.ItemDataRole.UserRole) if it else None

    def open_news(self, t, r):
        it = t.item(r, 0)
        code = it.data(Qt.ItemDataRole.UserRole) if it else None
        if code in self.stocks:                     # 네이버 뉴스 검색, 최신순(sort=1)
            q = urllib.parse.quote(self.stocks[code].name or code)
            webbrowser.open(f"https://search.naver.com/search.naver?ssc=tab.news.all&where=news&sort=1&query={q}")


def title_bar_color(win):
    """윈도우 11: 창 맨 위 제목 표시줄도 남색 바탕·흰 글씨로 (윈도우 10은 그대로)"""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        hwnd = int(win.winId())
        for attr, bgr in ((35, 0x5C2F1F), (36, 0xFFFFFF)):   # 35 제목줄 색, 36 글자 색 (0xBBGGRR)
            v = ctypes.c_int(bgr)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(v), ctypes.sizeof(v))
    except Exception as e:
        log(f"제목줄 색 변경 실패 {e}")


def main():
    app = QApplication(sys.argv)
    cfg = load_config()
    if not cfg:
        QMessageBox.information(None, "설정 필요",
                                f"아래 파일을 메모장으로 열어 앱키와 시크릿키를 넣고 저장한 뒤 다시 실행하세요.\n\n{CONFIG}")
        return
    rest = Rest(cfg)
    try:
        rest.get_token()
    except Exception as e:
        QMessageBox.critical(None, "토큰 발급 실패",
                             f"{e}\n\n앱키·시크릿키, 실전/모의(mode), 등록한 IP를 확인하세요.\n자세한 내용: {LOGFILE}")
        return
    win = Window(rest)
    win.show()
    title_bar_color(win)
    sys.exit(app.exec())


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        traceback.print_exc()
        log(traceback.format_exc())
        input("\n오류가 났습니다. 위 내용을 캡처해 보내주세요. Enter를 누르면 닫힙니다.")
