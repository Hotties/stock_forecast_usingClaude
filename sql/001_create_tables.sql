-- M0: MariaDB 테이블 생성 (설계 문서 8절 기준)
-- 실행: mysql -u <user> -p <db> < 001_create_tables.sql
-- 같은 파일을 다시 실행해도 안전하도록 IF NOT EXISTS를 사용합니다.
-- 모든 컬럼과 테이블에 COMMENT를 달았습니다. DB 안에서도 SHOW FULL COLUMNS FROM 테이블명; 으로 볼 수 있습니다.

SET NAMES utf8mb4;

-- ---------------------------------------------------------------------------
-- stocks: 종목 마스터. 자동완성의 검색 대상이자 모든 테이블의 기준
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS stocks (
  code VARCHAR(6) NOT NULL
    COMMENT '종목코드. 6자리 문자열(앞자리 0 보존). 예: 005930',
  name VARCHAR(100) NOT NULL
    COMMENT '회사명. 자동완성 검색용 인덱스 있음. 예: 삼성전자',
  market ENUM('KOSPI','KOSDAQ') NOT NULL
    COMMENT '소속 시장. 점수 계산에 어떤 시장 지수를 쓸지 결정',
  is_active TINYINT(1) NOT NULL DEFAULT 1
    COMMENT '지원 대상 여부. 1=지원, 0=제외(신규상장, 거래정지, 상장폐지, 우선주, ETF 등). 0이면 자동완성에서 빠짐',
  is_tracked TINYINT(1) NOT NULL DEFAULT 0
    COMMENT '편입 여부. 검색되어 적재가 끝났거나 학습용 기준 종목이면 1. 1인 종목만 실행 시 갱신 대상',
  is_seed TINYINT(1) NOT NULL DEFAULT 0
    COMMENT '학습용 기준 종목 여부. 1=처음에 미리 편입한 시드 100종목(KOSPI 70 + KOSDAQ 30)',
  last_loaded_date DATE NULL
    COMMENT '일봉을 마지막으로 적재한 거래일. 증분 적재의 시작점',
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
    COMMENT '이 행이 마지막으로 갱신된 시각(자동 기록)',
  PRIMARY KEY (code),
  KEY idx_stocks_name (name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
  COMMENT='종목 마스터(코드, 회사명, 시장, 지원·편입·시드 상태)';

-- ---------------------------------------------------------------------------
-- prices_daily: 종목별 일봉(수정주가). 예측의 원재료
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS prices_daily (
  code VARCHAR(6) NOT NULL
    COMMENT '종목코드. stocks.code 참조',
  trade_date DATE NOT NULL
    COMMENT '거래일. 휴장일은 행이 없음',
  open DECIMAL(14,2)
    COMMENT '시가. 원 단위, 수정주가',
  high DECIMAL(14,2)
    COMMENT '고가. 원 단위, 수정주가',
  low DECIMAL(14,2)
    COMMENT '저가. 원 단위, 수정주가',
  close DECIMAL(14,2) NOT NULL
    COMMENT '종가. 이동평균과 등락률 계산의 기준, 수정주가',
  volume BIGINT
    COMMENT '거래량(주 단위)',
  change_pct DECIMAL(8,4)
    COMMENT '전일 대비 등락률(%). (close / 전 거래일 close - 1) x 100. 적재할 때 계산해 저장',
  PRIMARY KEY (code, trade_date),
  CONSTRAINT fk_prices_stock FOREIGN KEY (code) REFERENCES stocks (code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
  COMMENT='종목별 일봉(수정주가)과 거래량';

-- ---------------------------------------------------------------------------
-- index_daily: 시장 지수, 미국 지수, 환율의 일별 데이터
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS index_daily (
  symbol VARCHAR(12) NOT NULL
    COMMENT '지표 이름. KOSPI, KOSDAQ, US500(S&P500), IXIC(나스닥), USDKRW(원/달러)',
  trade_date DATE NOT NULL
    COMMENT '해당 시장의 거래일. 미국 지표는 미국 현지 날짜 기준',
  close DECIMAL(14,4) NOT NULL
    COMMENT '종가. 환율은 마감 환율',
  change_pct DECIMAL(8,4)
    COMMENT '전일 대비 등락률(%). market_score, us_score 계산에 사용',
  PRIMARY KEY (symbol, trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
  COMMENT='시장 지수·미국 지수·환율의 일별 종가와 등락률';

-- ---------------------------------------------------------------------------
-- features_daily: 기준일별 항목 점수. 모든 점수는 -1(하락 신호)에서 +1(상승 신호) 사이
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS features_daily (
  code VARCHAR(6) NOT NULL
    COMMENT '종목코드. stocks.code 참조',
  base_date DATE NOT NULL
    COMMENT '기준일. 마지막 확정 종가일(이 날 장 마감까지의 정보만 사용)',
  target_date DATE NOT NULL
    COMMENT '예측 대상일. base_date 다음 거래일',
  ma7_score DECIMAL(6,4)
    COMMENT '7일 이평선 점수. 이격도를 변동성 x 루트7로 나눠 -1~+1로 자른 값',
  ma14_score DECIMAL(6,4)
    COMMENT '14일 이평선 점수. 같은 방식(루트14)',
  ma30_score DECIMAL(6,4)
    COMMENT '30일 이평선 점수. 같은 방식(루트30)',
  ma120_score DECIMAL(6,4)
    COMMENT '120일 이평선 점수. 같은 방식(루트120)',
  market_score DECIMAL(6,4)
    COMMENT '시장 지수 당일 점수. 종목이 속한 시장 지수의 기준일 등락률을 변동성과 2로 나눠 -1~+1로 자른 값',
  market5_score DECIMAL(6,4)
    COMMENT '시장 지수 최근 5일 점수. 5일 누적 수익률을 변동성 x 루트5와 2로 나눠 -1~+1로 자른 값',
  us_score DECIMAL(6,4)
    COMMENT '미국 증시 점수. 대상일보다 앞선 가장 최근 미국 거래일의 S&P500 등락률을 같은 방식으로 환산',
  volume_score DECIMAL(6,4)
    COMMENT '거래량 점수. 가격 방향 부호 x ln(거래량 / 20일 평균) / ln 3 을 -1~+1로 자른 값',
  PRIMARY KEY (code, base_date),
  CONSTRAINT fk_features_stock FOREIGN KEY (code) REFERENCES stocks (code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
  COMMENT='기준일별 항목 점수(-1~+1). 가중치 재학습 때 다시 계산하지 않도록 저장';

-- ---------------------------------------------------------------------------
-- model_versions: 모델 버전. 어떤 예측이 어떤 모델에서 나왔는지 추적
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS model_versions (
  version VARCHAR(20) NOT NULL
    COMMENT '버전 이름. 예: v0-formula, v1-ridge',
  method VARCHAR(20) NOT NULL
    COMMENT '가중치를 정한 방식. formula(수식), ridge, logistic',
  train_start DATE
    COMMENT '학습 구간의 시작일',
  train_end DATE
    COMMENT '학습 구간의 종료일',
  n_days SMALLINT NOT NULL DEFAULT 500
    COMMENT '학습에 쓴 거래일 수. 기본 500',
  direction_acc DECIMAL(5,4)
    COMMENT '방향 적중률(walk-forward 검증 결과). 0.54면 54%',
  baseline_acc DECIMAL(5,4)
    COMMENT '기준선 적중률(항상 상승, 어제와 동일 중 높은 쪽). direction_acc가 이보다 높아야 의미가 있음',
  mae DECIMAL(8,4)
    COMMENT '평균 절대 오차(%p). 예측 등락률과 실제 등락률 차이의 평균',
  is_active TINYINT(1) NOT NULL DEFAULT 0
    COMMENT '서비스 사용 여부. 1인 버전이 화면 예측에 쓰임(하나만 1로 유지)',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
    COMMENT '버전 생성 시각(자동 기록)',
  PRIMARY KEY (version)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
  COMMENT='모델 버전, 학습 방식, 학습 구간, 검증 지표';

-- ---------------------------------------------------------------------------
-- model_weights: 버전별 항목 가중치. 예측 등락률 = 절편 + 합계(가중치 x 점수)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS model_weights (
  version VARCHAR(20) NOT NULL
    COMMENT '모델 버전. model_versions.version 참조',
  component VARCHAR(20) NOT NULL
    COMMENT '항목 이름. ma7, ma14, ma30, ma120, market, market5, us, volume, intercept(절편)',
  weight DECIMAL(10,6) NOT NULL
    COMMENT '가중치. 점수 1.0당 반영되는 등락률(%p). 음수도 가능',
  PRIMARY KEY (version, component),
  CONSTRAINT fk_weights_version FOREIGN KEY (version) REFERENCES model_versions (version)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
  COMMENT='버전별 항목 가중치';

-- ---------------------------------------------------------------------------
-- predictions: 예측과 실제 결과. 화면의 예측 결과와 과거 적중 기록의 원천
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS predictions (
  id BIGINT NOT NULL AUTO_INCREMENT
    COMMENT '예측 번호(자동 증가)',
  code VARCHAR(6) NOT NULL
    COMMENT '종목코드. stocks.code 참조',
  base_date DATE NOT NULL
    COMMENT '기준일. 마지막 확정 종가일',
  target_date DATE NOT NULL
    COMMENT '예측 대상일. 기준일 다음 거래일',
  version VARCHAR(20) NOT NULL
    COMMENT '예측에 사용한 모델 버전. model_versions.version 참조',
  base_close DECIMAL(14,2) NOT NULL
    COMMENT '기준일 종가. 예측 가격 범위 계산의 출발점',
  pred_return_pct DECIMAL(8,4) NOT NULL
    COMMENT '예상 등락률(%). 절편 + 합계(가중치 x 점수)',
  up_prob DECIMAL(5,4)
    COMMENT '상승 확률(0~1). 수식 단계는 정규분포 근사, 회귀 단계는 로지스틱 회귀 확률',
  price_low DECIMAL(14,2)
    COMMENT '예상 가격 범위의 하단. 기준일 종가 x (1 + 예상 등락률/100 - 변동폭)',
  price_high DECIMAL(14,2)
    COMMENT '예상 가격 범위의 상단. 기준일 종가 x (1 + 예상 등락률/100 + 변동폭)',
  actual_close DECIMAL(14,2)
    COMMENT '대상일 실제 종가. 확정 전에는 NULL',
  actual_return_pct DECIMAL(8,4)
    COMMENT '실제 등락률(%). (actual_close / base_close - 1) x 100. 확정 전에는 NULL',
  hit TINYINT(1)
    COMMENT '방향 적중 여부. 예상과 실제 등락률의 부호가 같으면 1, 다르면 0. 확정 전에는 NULL',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
    COMMENT '예측 생성 시각(자동 기록)',
  PRIMARY KEY (id),
  UNIQUE KEY uq_pred (code, base_date, version),
  KEY idx_pred_target (code, target_date),
  CONSTRAINT fk_pred_stock FOREIGN KEY (code) REFERENCES stocks (code),
  CONSTRAINT fk_pred_version FOREIGN KEY (version) REFERENCES model_versions (version)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
  COMMENT='예측값과 실제값, 방향 적중 여부(적중 기록 화면의 원천)';

-- ---------------------------------------------------------------------------
-- pipeline_runs: 실행 기록. 갱신이나 재학습이 잘 돌았는지 확인
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pipeline_runs (
  id BIGINT NOT NULL AUTO_INCREMENT
    COMMENT '실행 번호(자동 증가)',
  job VARCHAR(30) NOT NULL
    COMMENT '작업 이름. refresh, retrain 등',
  started_at DATETIME NOT NULL
    COMMENT '작업 시작 시각',
  finished_at DATETIME
    COMMENT '작업 종료 시각. 실행 중이면 NULL',
  status ENUM('running','success','failed') NOT NULL
    COMMENT '상태. running=실행 중, success=성공, failed=실패. running이면 새 실행을 시작하지 않음',
  processed_rows INT
    COMMENT '처리 건수. 적재하거나 계산한 행 수',
  message TEXT
    COMMENT '오류 메시지 또는 메모. 실패 원인',
  PRIMARY KEY (id),
  KEY idx_runs_job (job, started_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
  COMMENT='배치·학습 작업의 실행 기록';
