"""M0 점수 분포 확인 스크립트 (일회성).

시드 100종목(KOSPI 70 + KOSDAQ 30)의 약 870거래일 데이터로 설계 문서 4절의 점수를 계산하고,
산식 상수가 적당한지 분포로 확인한다.

판정 기준(설계 문서): 점수가 -1 또는 +1로 잘린 비율이 5% 미만이고,
점수의 5~95% 구간이 대략 -0.8 ~ +0.8 안(허용 ±0.85)이면 통과.
(참고: 정규분포 데이터에서 지수형 점수의 5~95% 구간은 약 ±0.82라 기준에 거의 맞닿아 있다. 실제 주가 수익률은 꼬리가 두꺼워 더 넓게 나올 수 있다.)

점수 (모두 -1 ~ +1로 자름, 기준일 D)
  ma{N}_score   = clip( gap(N) / (σ60 x √N x MA_K) ),  gap(N) = (종가 - MA(N)) / MA(N)
  market_score  = clip( r_지수(D) / σ60_지수 / MKT_DIV )
  market5_score = clip( R5_지수 / (σ60_지수 x √5) / MKT_DIV )
  us_score      = clip( r_S&P500 / σ60_S&P500 / US_DIV )      (대상일 T보다 앞선 가장 최근 미국 거래일)
  volume_score  = clip( 부호(r(D)) x ln(V(D) / V20) / ln(VOL_BASE) )

실행:
  pip install finance-datareader pandas numpy
  python m0_score_distribution.py                      # 기본 상수로 확인
  python m0_score_distribution.py --market-div 3       # 상수를 바꿔 다시 확인
같은 폴더에 m0_seed_candidates.csv(m0_check_sources.py가 만든 파일)가 있으면 그 종목을 쓴다.
결과: m0_score_report.txt, m0_score_stats.csv, m0_score_corr.csv
"""
import argparse
import math
import os
import time
from datetime import datetime

import numpy as np
import pandas as pd

SCORES = ["ma7", "ma14", "ma30", "ma120", "market", "market5", "us", "volume"]
MA_WINDOWS = (7, 14, 30, 120)

# ---- 점수 상수 (설계 문서 4절의 초기값). 명령줄 옵션으로 바꿔 볼 수 있다.
DEFAULTS = {"ma_k": 1.0, "mkt_div": 2.0, "us_div": 2.0, "vol_base": 3.0}


# ------------------------------------------------------------------ 점수 계산
def clip_score(x):
    return x.clip(-1.0, 1.0)


def sigma60(returns):
    """기준일까지 최근 60거래일 수익률의 표준편차."""
    return returns.rolling(60).std()


def us_series(us_close, kr_dates, us_div):
    """한국 기준일 D마다 '대상일 T보다 앞선 가장 최근 미국 거래일'의 점수(미정규화 값)를 붙인다.

    T는 한국 거래일 달력에서 D의 다음 거래일이다. 마지막 D는 T를 알 수 없어 비워 둔다.
    """
    r = us_close.pct_change()
    z = r / sigma60(r) / us_div
    right = pd.DataFrame({"U": pd.to_datetime(z.index), "z": z.values}).sort_values("U")
    kr = pd.DatetimeIndex(kr_dates).sort_values()
    left = pd.DataFrame({"D": kr[:-1], "T": kr[1:]})
    merged = pd.merge_asof(left, right, left_on="T", right_on="U", allow_exact_matches=False)
    return pd.Series(merged["z"].values, index=merged["D"])


def raw_scores(prices, index_close, us_raw, params):
    """종목 하나의 점수(자르기 전 값)를 만든다. prices: Close, Volume 컬럼, 날짜 인덱스."""
    close = prices["Close"].astype(float)
    vol = prices["Volume"].astype(float)
    r = close.pct_change()
    sig = sigma60(r)
    out = pd.DataFrame(index=close.index)

    for n in MA_WINDOWS:
        ma = close.rolling(n).mean()
        gap = (close - ma) / ma
        out[f"ma{n}"] = gap / (sig * math.sqrt(n) * params["ma_k"])

    idx = index_close.reindex(close.index)
    r_i = index_close.pct_change()
    sig_i = sigma60(r_i)
    out["market"] = (r_i / sig_i / params["mkt_div"]).reindex(close.index)
    r5 = index_close.pct_change(5)
    out["market5"] = (r5 / (sig_i * math.sqrt(5)) / params["mkt_div"]).reindex(close.index)

    out["us"] = us_raw.reindex(close.index)

    v20 = vol.shift(1).rolling(20).mean()  # D 직전 20거래일 평균(D 제외)
    ratio = vol / v20
    ratio = ratio.where(ratio > 0)
    out["volume"] = np.sign(r) * np.log(ratio) / math.log(params["vol_base"])

    out = out.replace([np.inf, -np.inf], np.nan)
    out["next_ret"] = close.shift(-1) / close - 1  # 참고용(다음 거래일 등락률)
    return out[SCORES + ["next_ret"]]


# ------------------------------------------------------------------ 분포 통계
def describe(raw):
    """자르기 전 값(raw)으로 포화 비율과 분위수를 계산한다."""
    x = raw.dropna()
    if len(x) == 0:
        return None
    clipped = x.clip(-1, 1)
    q95_abs = float(x.abs().quantile(0.95))
    return {
        "n": int(len(x)),
        "sat_pct": float((x.abs() >= 1).mean() * 100),
        "p05": float(clipped.quantile(0.05)),
        "p50": float(clipped.quantile(0.50)),
        "p95": float(clipped.quantile(0.95)),
        "std": float(clipped.std()),
        "q95_abs": q95_abs,
        "multiplier": (0.8 / q95_abs) if q95_abs > 0 else float("nan"),
    }


def verdict(s):
    if s is None:
        return "SKIP"
    ok_sat = s["sat_pct"] < 5.0
    ok_range = s["p05"] >= -0.85 and s["p95"] <= 0.85  # 설계의 "대략 ±0.8"에 약간의 허용을 둠
    if ok_sat and ok_range:
        return "PASS" if s["q95_abs"] >= 0.4 else "CHECK(분포가 좁음)"
    return "CHECK(포화·범위 초과)"


# ------------------------------------------------------------------ 데이터 읽기
def load_seed(path, fdr):
    if path and os.path.exists(path):
        df = pd.read_csv(path, dtype={"code": str})
        return df[["code", "name", "seed_market"]]
    listing = fdr.StockListing("KRX")
    listing = listing.assign(_m=pd.to_numeric(listing["Marcap"], errors="coerce"))
    base = listing[listing["Code"].astype(str).str.endswith("0")]
    market = base["Market"].astype(str).str.upper()
    kospi = base[market.eq("KOSPI")].nlargest(70, "_m").assign(seed_market="KOSPI")
    kosdaq = base[market.str.startswith("KOSDAQ")].nlargest(30, "_m").assign(seed_market="KOSDAQ")
    seed = pd.concat([kospi, kosdaq])
    return seed[["Code", "Name", "seed_market"]].rename(columns={"Code": "code", "Name": "name"})


def fetch(fdr, symbol, start, cache_dir, use_cache, sleep_sec):
    safe = symbol.replace("/", "_")
    path = os.path.join(cache_dir, f"{safe}.pkl")
    if use_cache and os.path.exists(path):
        return pd.read_pickle(path)
    df = None
    for _ in range(3):
        try:
            df = fdr.DataReader(symbol, start)
            break
        except Exception:
            time.sleep(1.0)
    if df is None:
        return None
    os.makedirs(cache_dir, exist_ok=True)
    df.to_pickle(path)
    time.sleep(sleep_sec)
    return df


# ------------------------------------------------------------------ 실행
def main():
    ap = argparse.ArgumentParser(description="M0 점수 분포 확인")
    ap.add_argument("--days", type=int, default=870, help="사용할 한국 거래일 수(적재 길이)")
    ap.add_argument("--calendar-days", type=int, default=1500, help="조회 시작일까지의 달력 일수(870거래일보다 넉넉하게)")
    ap.add_argument("--seed-csv", default="m0_seed_candidates.csv")
    ap.add_argument("--sleep", type=float, default=0.3)
    ap.add_argument("--no-cache", action="store_true", help="저장해 둔 데이터를 쓰지 않고 다시 받기")
    ap.add_argument("--cache-dir", default="m0_cache")
    ap.add_argument("--ma-k", type=float, default=DEFAULTS["ma_k"], help="이평선 점수 나누는 값에 곱할 배율")
    ap.add_argument("--market-div", type=float, default=DEFAULTS["mkt_div"], help="시장 지수 점수 나누는 값")
    ap.add_argument("--us-div", type=float, default=DEFAULTS["us_div"], help="미국 점수 나누는 값")
    ap.add_argument("--vol-base", type=float, default=DEFAULTS["vol_base"], help="거래량 점수 포화 배수(ln 밑)")
    args = ap.parse_args()
    params = {"ma_k": args.ma_k, "mkt_div": args.market_div, "us_div": args.us_div, "vol_base": args.vol_base}

    try:
        import FinanceDataReader as fdr
    except ImportError:
        print("FinanceDataReader가 설치되어 있지 않습니다: pip install finance-datareader")
        return

    start = (pd.Timestamp.today() - pd.Timedelta(days=args.calendar_days)).strftime("%Y-%m-%d")
    use_cache = not args.no_cache

    # 지수와 미국 지수
    kospi = fetch(fdr, "KS11", start, args.cache_dir, use_cache, args.sleep)
    kosdaq = fetch(fdr, "KQ11", start, args.cache_dir, use_cache, args.sleep)
    us = fetch(fdr, "US500", start, args.cache_dir, use_cache, args.sleep)
    if kospi is None or kosdaq is None or us is None:
        print("지수 데이터를 받지 못했습니다. 네트워크와 심볼을 확인하세요.")
        return

    # 마지막 행이 오늘이고 장 마감 확정(16:30) 전이면 장중 값일 수 있어 뺀다
    now = datetime.now()
    notes = []
    if pd.Timestamp(kospi.index[-1]).date() == now.date() and (now.hour, now.minute) < (16, 30):
        kospi, kosdaq = kospi.iloc[:-1], kosdaq.iloc[:-1]
        notes.append("16:30 전이라 오늘 행(장중 값일 수 있음)을 제외했습니다.")

    kr_dates = pd.DatetimeIndex(kospi.index)[-args.days:]
    first_day = kr_dates[0]
    idx_close = {
        "KOSPI": kospi["Close"].astype(float).loc[first_day:],
        "KOSDAQ": kosdaq["Close"].astype(float).loc[first_day:],
    }
    us_close = us["Close"].astype(float)
    us_raw = us_series(us_close, kr_dates, params["us_div"])

    seed = load_seed(args.seed_csv, fdr)
    frames, skipped = [], []
    for _, row in seed.iterrows():
        code, market = str(row["code"]), str(row["seed_market"])
        df = fetch(fdr, code, start, args.cache_dir, use_cache, args.sleep)
        if df is None or "Close" not in df.columns or "Volume" not in df.columns:
            skipped.append((code, "조회 실패"))
            continue
        df = df.loc[first_day:]
        if len(df) < 200:
            skipped.append((code, f"{len(df)}행(너무 짧음)"))
            continue
        sc = raw_scores(df, idx_close[market], us_raw, params)
        sc["code"], sc["seed_market"] = code, market
        frames.append(sc)
    if not frames:
        print("계산할 종목이 없습니다.")
        return
    pooled = pd.concat(frames)

    # ---- 통계
    rows = []
    for name in SCORES:
        for scope, sub in [("전체", pooled), ("KOSPI", pooled[pooled.seed_market == "KOSPI"]), ("KOSDAQ", pooled[pooled.seed_market == "KOSDAQ"])]:
            s = describe(sub[name])
            if s:
                rows.append({"score": name, "scope": scope, "verdict": verdict(s), **s})
    stats = pd.DataFrame(rows)
    stats.to_csv("m0_score_stats.csv", index=False, encoding="utf-8-sig")

    clipped = pooled[SCORES].clip(-1, 1)
    corr = clipped.corr().round(2)
    corr.to_csv("m0_score_corr.csv", encoding="utf-8-sig")
    with_ret = clipped.join(pooled["next_ret"]).corr()["next_ret"].drop("next_ret").round(3)

    # ---- 보고서
    lines = []
    lines.append(f"M0 점수 분포 보고서 ({datetime.now():%Y-%m-%d %H:%M})")
    lines.append(f"종목 {pooled['code'].nunique()}개 (건너뜀 {len(skipped)}개 {skipped[:5]}), 행 {len(pooled)}개, 기간 {first_day.date()} ~ {kr_dates[-1].date()}")
    lines.append(f"상수: {params}")
    lines.extend(notes)
    lines.append("")
    lines.append("[전체 분포]  sat%=-1/+1로 잘린 비율, p05/p95=5·95% 분위수(자른 뒤), q95|x|=자르기 전 절댓값의 95% 분위수,")
    lines.append("             배율=곱하면 q95가 0.8이 되는 값(1보다 작으면 더 크게 나눠야 하고, 크면 더 작게 나눠야 함)")
    lines.append(f"{'점수':9}{'판정':20}{'n':>8}{'sat%':>7}{'p05':>7}{'p50':>7}{'p95':>7}{'std':>7}{'q95|x|':>8}{'배율':>7}")
    for _, r in stats[stats.scope == "전체"].iterrows():
        lines.append(f"{r['score']:9}{r['verdict']:20}{r['n']:>8}{r['sat_pct']:>7.1f}{r['p05']:>7.2f}{r['p50']:>7.2f}{r['p95']:>7.2f}{r['std']:>7.2f}{r['q95_abs']:>8.2f}{r['multiplier']:>7.2f}")
    lines.append("")
    lines.append("[시장별 비교]  sat% / p05 / p95")
    for name in SCORES:
        parts = []
        for scope in ("KOSPI", "KOSDAQ"):
            r = stats[(stats.score == name) & (stats.scope == scope)]
            if len(r):
                r = r.iloc[0]
                parts.append(f"{scope} {r['sat_pct']:.1f}% / {r['p05']:.2f} / {r['p95']:.2f}")
        lines.append(f"{name:9}" + "   ".join(parts))
    lines.append("")
    lines.append("[점수끼리의 상관]  값이 0.9 이상이면 사실상 같은 신호라 하나만 써도 됨")
    lines.append(corr.to_string())
    lines.append("")
    lines.append("[다음 거래일 등락률과의 상관 - 참고용, 모델 검증이 아님]")
    lines.append(with_ret.to_string())
    text = "\n".join(lines)
    print(text)
    with open("m0_score_report.txt", "w", encoding="utf-8") as f:
        f.write(text + "\n")
    print("\n저장: m0_score_report.txt, m0_score_stats.csv, m0_score_corr.csv")


if __name__ == "__main__":
    main()
