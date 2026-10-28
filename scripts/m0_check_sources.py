"""M0 사전 점검 스크립트 (일회성, 점검 후 버려도 되는 코드).

설계 문서의 가정이 실제 데이터 소스와 맞는지 확인하고 보고서를 만든다.
  1. 종목 목록: 시장 컬럼, 시가총액 컬럼, 상장일, ETF/상장폐지 목록
  2. 지수·환율 심볼과 조회 가능 기간
  3. 수정주가 여부(삼성전자 2018-05 액면분할 구간)
  4. 호출 간격과 실패율
  5. 거래소 달력(한국·미국)과 지수 날짜 비교, 미국 날짜 정렬 규칙

실행:
  pip install finance-datareader pandas exchange_calendars
  python m0_check_sources.py --stocks 30 --sleep 0.3

결과는 화면에 출력되고 m0_report.txt, m0_report.json으로 저장된다.
상태 표시: PASS(통과) / FAIL(가정과 다름) / CHECK(직접 확인 필요) / SKIP(건너뜀)
"""
import argparse
import inspect
import json
import time
from datetime import date, timedelta

import pandas as pd

RESULTS = []


def record(item, status, detail=""):
    RESULTS.append({"item": item, "status": status, "detail": detail})
    print(f"[{status:5}] {item}: {detail}")


def first_col(df, candidates):
    """후보 이름 중 데이터프레임에 있는 첫 컬럼 이름을 돌려준다."""
    for c in candidates:
        if c in df.columns:
            return c
    return None


# ---------------------------------------------------------------- 1. 종목 목록
def check_listing(fdr):
    listing = None
    try:
        listing = fdr.StockListing("KRX")
    except Exception as e:  # 네트워크·소스 오류
        record("종목 목록 조회", "FAIL", f"StockListing('KRX') 실패: {e!r}")
        return None

    cols = list(listing.columns)
    record("종목 목록 조회", "PASS", f"{len(listing)}행, 컬럼 {cols}")

    code_col = first_col(listing, ["Code", "Symbol"])
    name_col = first_col(listing, ["Name"])
    market_col = first_col(listing, ["Market"])
    marcap_col = first_col(listing, ["Marcap", "MarketCap"])

    if not (code_col and name_col):
        record("코드·이름 컬럼", "FAIL", f"Code/Name 컬럼을 찾지 못함: {cols}")
        return None
    record("코드·이름 컬럼", "PASS", f"{code_col}, {name_col}")

    if market_col is None:
        record("시장 컬럼", "FAIL", "Market 컬럼 없음 → 시장 구분 방법을 따로 찾아야 함")
        return listing
    counts = listing[market_col].fillna("(비어 있음)").value_counts().to_dict()
    empty = int(listing[market_col].isna().sum())
    status = "PASS" if empty == 0 else "CHECK"
    record("시장 컬럼", status, f"값 분포 {counts}, 비어 있는 행 {empty}개")

    if marcap_col is None:
        record("시가총액 컬럼", "FAIL", "시가총액 컬럼 없음 → 시드 100종목 선정 방법을 따로 찾아야 함")
    else:
        marcap = pd.to_numeric(listing[marcap_col], errors="coerce")
        record("시가총액 컬럼", "PASS", f"{marcap_col}, 값이 있는 행 {int(marcap.notna().sum())}개")
        record_seed(listing, code_col, name_col, market_col, marcap_col)

    codes = listing[code_col].astype(str)
    not_zero = listing[~codes.str.endswith("0")]
    sample = not_zero[name_col].head(8).tolist()
    record(
        "우선주 판별(코드 끝자리)",
        "CHECK",
        f"코드가 0으로 끝나지 않는 종목 {len(not_zero)}개, 예: {sample} → 우선주 외 종목이 섞였는지 눈으로 확인",
    )
    return listing


def record_seed(listing, code_col, name_col, market_col, marcap_col):
    """시드 종목(KOSPI 70 + KOSDAQ 30) 후보를 뽑아 보고, 파일로 저장한다."""
    df = listing.copy()
    df["_marcap"] = pd.to_numeric(df[marcap_col], errors="coerce")
    market = df[market_col].astype(str)
    kospi = df[market.str.upper().eq("KOSPI")]
    kosdaq = df[market.str.upper().str.startswith("KOSDAQ")]
    # 보통주만(코드 끝자리 0)으로 제한
    kospi = kospi[kospi[code_col].astype(str).str.endswith("0")].nlargest(70, "_marcap")
    kosdaq = kosdaq[kosdaq[code_col].astype(str).str.endswith("0")].nlargest(30, "_marcap")
    seed = pd.concat([kospi.assign(seed_market="KOSPI"), kosdaq.assign(seed_market="KOSDAQ")])
    seed = seed[[code_col, name_col, "seed_market", "_marcap"]].rename(
        columns={code_col: "code", name_col: "name", "_marcap": "marcap"}
    )
    seed.to_csv("m0_seed_candidates.csv", index=False, encoding="utf-8-sig")
    status = "PASS" if len(kospi) == 70 and len(kosdaq) == 30 else "CHECK"
    record(
        "시드 후보(KOSPI 70 + KOSDAQ 30)",
        status,
        f"KOSPI {len(kospi)}개, KOSDAQ {len(kosdaq)}개 → m0_seed_candidates.csv 저장",
    )


def check_listing_extras(fdr):
    for key, label in [("KRX-DESC", "상장일"), ("ETF/KR", "ETF 목록"), ("KRX-DELISTING", "상장폐지 목록")]:
        try:
            df = fdr.StockListing(key)
        except Exception as e:
            record(f"{label} 조회", "FAIL", f"StockListing('{key}') 실패: {e!r}")
            continue
        extra = ""
        if key == "KRX-DESC":
            ld = first_col(df, ["ListingDate"])
            if ld is None:
                record(f"{label} 조회", "FAIL", f"ListingDate 컬럼 없음: {list(df.columns)}")
                continue
            extra = f", ListingDate 비어 있는 행 {int(df[ld].isna().sum())}개(우선주 등)"
        record(f"{label} 조회", "PASS", f"{len(df)}행, 컬럼 {list(df.columns)[:8]}{extra}")


# ------------------------------------------------- 2. 지수·환율 심볼과 기간
INDEX_SYMBOLS = ["KS11", "KQ11", "US500", "IXIC", "USD/KRW"]


def check_symbols(fdr, start):
    frames = {}
    for sym in INDEX_SYMBOLS:
        try:
            df = fdr.DataReader(sym, start)
        except Exception as e:
            record(f"심볼 {sym}", "FAIL", f"조회 실패: {e!r}")
            continue
        if df is None or len(df) == 0:
            record(f"심볼 {sym}", "FAIL", "데이터가 비어 있음")
            continue
        status = "PASS" if len(df) >= 870 else "CHECK"
        record(
            f"심볼 {sym}",
            status,
            f"{len(df)}행, {df.index.min().date()} ~ {df.index.max().date()}, 컬럼 {list(df.columns)}",
        )
        frames[sym] = df
    return frames


# ---------------------------------------------------- 3. 수정주가 여부
def check_adjusted(fdr):
    """삼성전자는 2018-05-04에 50:1 액면분할. 수정주가라면 일간 변동이 30%(상하한)를 넘지 않는다."""
    try:
        df = fdr.DataReader("005930", "2018-04-20", "2018-05-10")
    except Exception as e:
        record("수정주가(삼성전자 액면분할)", "FAIL", f"조회 실패: {e!r}")
        return
    if "Close" not in df.columns or len(df) < 5:
        record("수정주가(삼성전자 액면분할)", "FAIL", f"Close 컬럼이 없거나 행이 부족: {list(df.columns)}, {len(df)}행")
        return
    worst = df["Close"].pct_change().abs().max()
    first, last = float(df["Close"].iloc[0]), float(df["Close"].iloc[-1])
    if worst > 0.31:
        record("수정주가(삼성전자 액면분할)", "FAIL", f"하루 최대 변동 {worst:.1%} → 비수정주가로 보임 (종가 {first:,.0f} → {last:,.0f})")
    else:
        record("수정주가(삼성전자 액면분할)", "PASS", f"하루 최대 변동 {worst:.1%} → 수정주가로 보임 (종가 {first:,.0f} → {last:,.0f})")
    try:
        sig = str(inspect.signature(fdr.DataReader))
        record("DataReader 시그니처", "CHECK", f"{sig} → 소스를 지정하는 인자가 있으면 문서에서 확인")
    except (TypeError, ValueError):
        pass


# -------------------------------------------------- 4. 호출 간격과 실패율
def check_rate(fdr, codes, sleep_sec, start):
    if not codes:
        record("호출 간격 시험", "SKIP", "시험할 종목 코드가 없음")
        return
    ok, failed, rows, elapsed = 0, [], [], []
    for code in codes:
        t0 = time.time()
        done = False
        for _ in range(3):  # 최대 3회 재시도
            try:
                df = fdr.DataReader(code, start)
                rows.append(len(df))
                ok += 1
                done = True
                break
            except Exception:
                time.sleep(1.0)
        if not done:
            failed.append(code)
        elapsed.append(time.time() - t0)
        time.sleep(sleep_sec)
    avg = sum(elapsed) / len(elapsed)
    status = "PASS" if not failed else "CHECK"
    record(
        "호출 간격 시험",
        status,
        f"종목 {len(codes)}개, 간격 {sleep_sec}초, 성공 {ok}개, 실패 {len(failed)}개 {failed[:5]}, "
        f"종목당 평균 {avg:.2f}초, 행 수 최소 {min(rows) if rows else 0}",
    )
    if failed:
        record("호출 간격 권고", "CHECK", "실패가 있으면 --sleep 값을 늘려 다시 시험")


# ------------------------------------- 5. 거래소 달력과 미국 날짜 정렬
def latest_us_session_before(us_cal, target):
    """target(한국 날짜)보다 앞선 가장 최근 미국 거래일."""
    sessions = us_cal.sessions_in_range(target - timedelta(days=10), target - timedelta(days=1))
    return pd.Timestamp(sessions[-1]).date() if len(sessions) else None


def check_calendar(frames):
    try:
        import exchange_calendars as xcals
    except ImportError:
        record("달력 라이브러리", "SKIP", "exchange_calendars가 설치되지 않음 (pip install exchange_calendars)")
        return
    try:
        kr = xcals.get_calendar("XKRX")
        us = xcals.get_calendar("XNYS")
    except Exception as e:
        record("달력 라이브러리", "FAIL", f"XKRX/XNYS 달력을 불러오지 못함: {e!r}")
        return
    record("달력 라이브러리", "PASS", f"exchange_calendars {getattr(xcals, '__version__', '?')}, 지원 범위 {kr.first_session.date()} ~ {kr.last_session.date()}")

    # 알려진 휴장일 확인(직접 눈으로 확인)
    probes = [date(2026, 10, 9), date(2026, 5, 1), date(2026, 12, 31)]
    out = []
    for d in probes:
        out.append(f"{d}: {'거래일' if kr.is_session(pd.Timestamp(d)) else '휴장'}")
    record("한국 휴장일 샘플", "CHECK", "; ".join(out) + " → 한글날(10/9)은 휴장이어야 함. 5/1·12/31은 실제 공지와 비교")

    # 한국 달력 vs KOSPI 지수 날짜
    kospi = frames.get("KS11")
    if kospi is not None:
        idx_dates = sorted(pd.Timestamp(x).date() for x in kospi.index)
        cal_dates = [pd.Timestamp(x).date() for x in kr.sessions_in_range(idx_dates[0], idx_dates[-1])]
        only_idx = sorted(set(idx_dates) - set(cal_dates))
        only_cal = sorted(set(cal_dates) - set(idx_dates))
        if not only_idx and not only_cal:
            record("달력 vs KOSPI 지수 날짜", "PASS", f"{len(idx_dates)}개 날짜가 모두 일치")
        else:
            record(
                "달력 vs KOSPI 지수 날짜",
                "CHECK",
                f"지수에만 있는 날 {len(only_idx)}개 {only_idx[:5]}, 달력에만 있는 날 {len(only_cal)}개 {only_cal[:5]}",
            )

    # 미국 날짜 정렬: merge_asof(이전 날짜만) vs 달력 기반 함수
    us_df = frames.get("US500")
    if kospi is not None and us_df is not None:
        kr_dates = sorted(pd.Timestamp(x).date() for x in kospi.index)[-60:]
        left = pd.DataFrame({"T": pd.to_datetime(kr_dates)})
        right = pd.DataFrame({"U": pd.to_datetime([pd.Timestamp(x).date() for x in us_df.index])}).sort_values("U")
        merged = pd.merge_asof(left, right, left_on="T", right_on="U", allow_exact_matches=False)
        bad = []
        for t, u in zip(merged["T"], merged["U"]):
            expected = latest_us_session_before(us, t.date())
            got = None if pd.isna(u) else u.date()
            if expected != got:
                bad.append((t.date(), got, expected))
        if not bad:
            record("미국 날짜 정렬 규칙", "PASS", "최근 60개 한국 거래일에서 merge_asof 결과와 달력 계산이 일치")
        else:
            record("미국 날짜 정렬 규칙", "CHECK", f"불일치 {len(bad)}건 (한국일, merge_asof, 달력): {bad[:3]}")


# --------------------------------------------------------------------- 실행
def main():
    ap = argparse.ArgumentParser(description="M0 사전 점검")
    ap.add_argument("--stocks", type=int, default=30, help="호출 간격 시험에 쓸 종목 수")
    ap.add_argument("--sleep", type=float, default=0.3, help="종목 사이 대기 시간(초)")
    ap.add_argument("--calendar-days", type=int, default=1300, help="조회 시작일까지의 달력 일수(약 870거래일 이상)")
    args = ap.parse_args()

    try:
        import FinanceDataReader as fdr
    except ImportError:
        print("FinanceDataReader가 설치되어 있지 않습니다: pip install finance-datareader")
        return
    record("라이브러리 버전", "CHECK", f"FinanceDataReader {getattr(fdr, '__version__', '?')}, pandas {pd.__version__} → 버전을 기록해 두고 고정")

    start = (date.today() - timedelta(days=args.calendar_days)).isoformat()

    listing = check_listing(fdr)
    check_listing_extras(fdr)
    frames = check_symbols(fdr, start)
    check_adjusted(fdr)

    codes = []
    if listing is not None:
        code_col = first_col(listing, ["Code", "Symbol"])
        marcap_col = first_col(listing, ["Marcap", "MarketCap"])
        if code_col and marcap_col:
            top = listing.assign(_m=pd.to_numeric(listing[marcap_col], errors="coerce")).nlargest(args.stocks, "_m")
            codes = top[code_col].astype(str).tolist()
        elif code_col:
            codes = listing[code_col].astype(str).head(args.stocks).tolist()
    check_rate(fdr, codes, args.sleep, start)
    check_calendar(frames)

    # 보고서 저장
    counts = {}
    for r in RESULTS:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    summary = "  ".join(f"{k} {v}" for k, v in sorted(counts.items()))
    print("\n요약:", summary)
    with open("m0_report.txt", "w", encoding="utf-8") as f:
        f.write(f"M0 사전 점검 보고서 ({date.today()})\n요약: {summary}\n\n")
        for r in RESULTS:
            f.write(f"[{r['status']:5}] {r['item']}: {r['detail']}\n")
    with open("m0_report.json", "w", encoding="utf-8") as f:
        json.dump(RESULTS, f, ensure_ascii=False, indent=2)
    print("저장: m0_report.txt, m0_report.json")


if __name__ == "__main__":
    main()
