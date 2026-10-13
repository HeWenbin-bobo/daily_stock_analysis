export type Market = 'cn' | 'hk' | 'us';
export type Period = 'daily' | 'weekly' | 'monthly';

/** 数据源标签 — iter 157/158/162 与 backend services/data_provider ALL_SOURCES 对齐。
 *  - akshare: A股/港股 实时数据 (东财)
 *  - yfinance: 美股 实时数据 (主)
 *  - tencent: A股/港股 实时数据 (腾讯直连兜底, akshare 不可达时)
 *  - finnhub: 美股 实时数据 (Finnhub 兜底, 需 FINNHUB_API_KEY)
 *  - alphavantage: 美股 实时数据 (AlphaVantage 兜底, 需 ALPHAVANTAGE_API_KEY)
 *  - mock: 用户主动 FORCE_MOCK=1, 明确知道是演示数据
 *  - mock_fallback: 所有上游失败, 静默 fallback 到 mock — 用户应该看到 banner */
export type DataSource =
  | 'akshare'
  | 'yfinance'
  | 'tencent'
  | 'finnhub'
  | 'alphavantage'
  | 'mock'
  | 'mock_fallback';

export interface KLineData {
  date: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  amount: number;
  change_percent: number;
}

export interface StockHistoryResponse {
  stock_code: string;
  stock_name: string;
  market: Market;
  period: Period;
  data: KLineData[];
  source: DataSource | string;
}

export interface StockSearchItem {
  code: string;
  name: string;
  market: Market;
}

export interface PeakTroughSegment {
  type: 'decline' | 'rebound';
  from_date: string;
  to_date: string;
  start_price: number;
  end_price: number;
  change_pct: number;
  from_peak: boolean;
}

export interface PeakTroughResponse {
  stock_code: string;
  stock_name: string;
  period: Period;
  year_high: { date: string; price: number };
  year_start_date: string;
  analysis_date: string;
  segments: PeakTroughSegment[];
  summary: {
    max_decline_pct: number;
    max_rebound_pct: number;
    current_vs_high_pct: number;
    current_vs_year_start_pct: number;
    current_price: number;
    year_start_price: number;
    segment_count: number;
    decline_count: number;
    rebound_count: number;
  };
  // iter 128: 加 effective_window 字段 — iter 120 后端在响应里加了这个字段,
  // 但前端类型没声明, 调用方访问 result.effective_window 会得到 undefined。
  // 后端 PeakTroughResponse schema 已声明 (backend/schemas/stocks.py:164),
  // 客户端类型应当与之对齐 — user 请求 window=5 实际跑 window=4 量化,
  // UI 可据此显示"实际使用窗口 N"提高透明度。
  effective_window?: number;
}

export interface LLMStatus {
  available: boolean;
  source: string;
  provider: string | null;
  model: string | null;
  base_url: string | null;
  app_type: string | null;
  // iter 150: 移除 website_url — 与 backend iter 108 配套, 该字段是供应商营销/
  // 文档链接, 不应在 llm-status 响应中暴露 (即便利 oop-admin 也不防 admin 探测)。
  // 同时将 all_providers 内部项精确匹配 backend LLMProviderInfo (id/name/is_current)。
  all_providers: Array<{ id: string; name: string; is_current?: number }>;
  message?: string;
}

export interface LLMInsightResponse {
  provider: string;
  model: string;
  text: string;
  source: string;
}

export interface IndicatorPoint {
  time: string;
  value: number;
}

/** /stocks/{code}/indicators/{indicator} 返回结构。
 *  series 字段名随 indicator 变化: BOLL=mid/upper/lower; MACD=dif/dea/hist; KDJ=k/d/j
 *  (iter 64: 从 api/client.ts 提升到此处供外部模块 import, 避免 typing 漂移) */
export interface IndicatorResult {
  indicator: string;
  params: Record<string, number | string>;
  series: Record<string, IndicatorPoint[]>;
}