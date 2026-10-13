/**
 * 集中式 API 客户端, 封装所有 /api/v1/* 端点。通过 axios 实例统一 timeout/interceptor,
 * withRetry 包装提供 1s/2s/4s 退避重试 (iter 80), X-Request-ID interceptor
 * 在 DEV 模式下打印请求 ID 便于前后端日志关联 (iter 134)。
 */
import axios, { AxiosError } from 'axios';
import type {
  IndicatorResult,
  LLMInsightResponse,
  LLMStatus,
  PeakTroughResponse,
  Period,
  StockHistoryResponse,
  StockSearchItem,
} from '../types';

const api = axios.create({
  baseURL: '/api/v1',
  timeout: 30000,
});

// iter 134: 读 X-Request-ID 响应头 — 后端 _RequestIDMiddleware 给每个请求分配 rid=
// 并通过响应头返回。response interceptor 把 rid 挂在 error 上, 让 errors.ts / 调用方
// 在 DEV 控制台能直接关联前端 console 日志和后端 server 日志。
// 仅在 DEV 打印 (production 安静, 避免泄露内部诊断细节)。
api.interceptors.response.use(
  (response) => {
    if (import.meta.env.DEV) {
      const rid = response.headers['x-request-id'];
      if (rid) {
        // eslint-disable-next-line no-console
        console.debug(`[API] ${response.config.method?.toUpperCase()} ${response.config.url} rid=${rid}`);
      }
    }
    return response;
  },
  (error: AxiosError) => {
    if (import.meta.env.DEV) {
      const rid = error.response?.headers['x-request-id'];
      if (rid) {
        // eslint-disable-next-line no-console
        console.warn(`[API] ${error.config?.method?.toUpperCase()} ${error.config?.url} failed rid=${rid}: ${error.message}`);
      }
    }
    return Promise.reject(error);
  },
);

/**
 * 搜索股票。
 *
 * @param q - 搜索关键词，支持股票代码或名称
 * @param signal - AbortSignal，用于主动取消请求
 * @returns 匹配股票列表，关键词为空或仅空白时直接返回空数组 []
 *
 * @example
 * const stocks = await searchStocks('贵州茅台');
 */
export async function searchStocks(q: string, signal?: AbortSignal): Promise<StockSearchItem[]> {
  if (!q.trim()) return [];
  const { data } = await api.get<{ items: StockSearchItem[] }>('/stocks/search', {
    params: { q },
    signal,
  });
  return data.items;
}

export async function fetchHistory(
  code: string,
  period: Period,
  days = 365,
  signal?: AbortSignal
): Promise<StockHistoryResponse> {
  return withRetry(
    (s) =>
      api
        .get<StockHistoryResponse>(`/stocks/${code}/history`, {
          params: { period, days },
          signal: s,
        })
        .then((r) => r.data),
    { signal },
  );
}

export async function analyzePeakTrough(
  code: string,
  period: Period,
  days = 365,
  signal?: AbortSignal
): Promise<PeakTroughResponse> {
  return withRetry(
    (s) =>
      api
        .post<PeakTroughResponse>(
          '/analysis/peak-trough',
          { stock_code: code, period, days },
          { signal: s },
        )
        .then((r) => r.data),
    { signal },
  );
}

export async function fetchLLMInsight(
  code: string,
  period: Period,
  days = 365,
  extraPrompt?: string,
  signal?: AbortSignal
): Promise<LLMInsightResponse> {
  // iter 96: 包裹 withRetry — 临时网络抖动 (RST / 5xx) 让用户手动重试体验差。
  // LLM 调用慢 (~1-10s) 更容易碰到瞬时故障, 与其它端点保持一致。
  return withRetry(
    (s) =>
      api
        .post<LLMInsightResponse>(
          '/analysis/llm-insight',
          { stock_code: code, period, days, extra_prompt: extraPrompt },
          { signal: s },
        )
        .then((r) => r.data),
    { signal },
  );
}

export async function fetchLLMStatus(signal?: AbortSignal): Promise<LLMStatus> {
  // iter 96: 同上 — 包裹 withRetry 防止偶发网络抖动导致状态显示 "未配置"。
  return withRetry(
    (s) =>
      api.get<LLMStatus>('/analysis/llm-status', { signal: s }).then((r) => r.data),
    { signal },
  );
}

// iter 84: IndicatorPoint 只在 types.ts 定义, 删除此处的重复实现。
// (types.ts:79-82 是唯一权威定义, 各组件都从这里 import。)

/**
 * 获取技术指标数据 (BOLL / MACD / KDJ)。
 *
 * @param code - 股票代码
 * @param indicator - 指标类型，字面量联合类型 'MACD' | 'BOLL' | 'KDJ'
 * @param period - K线周期，默认 'daily' (日线)
 * @param days - 回溯天数，默认 365
 * @param signal - AbortSignal，用于主动取消请求
 * @returns IndicatorResult，包含 series 字段 (各指标的具体数据数组)
 *
 * 注意: 此函数不包裹 withRetry — 指标是可选叠加数据，失败时前端会降级，
 * 重试反而增加用户等待时间且通常会再次失败。
 */
export async function fetchIndicator(
  code: string,
  indicator: 'MACD' | 'BOLL' | 'KDJ',
  period: Period = 'daily',
  days = 365,
  signal?: AbortSignal
): Promise<IndicatorResult> {
  const { data } = await api.get<IndicatorResult>(
    `/stocks/${code}/indicators/${indicator}`,
    { params: { period, days }, signal }
  );
  return data;
}

export function isCancelError(err: unknown): boolean {
  // iter 82: 同时识别 DOMException AbortError — withRetry() 在 sleep 中被 abort 时
  // 抛 DOMException('Aborted', 'AbortError'), 旧实现没覆盖会让 abort 错误显示给用户。
  return (
    axios.isCancel(err) ||
    (err instanceof AxiosError && err.code === 'ERR_CANCELED') ||
    (err instanceof DOMException && err.name === 'AbortError')
  );
}

/**
 * 带退避的重试包装 — 仅在请求未被主动取消时重试。
 *
 * iter 80: 临时网络故障 (RST、负载均衡 5xx) 不应让用户手动重试。
 * 默认 3 次, 退避 1s/2s/4s — 与大多数云端 LB 的"reset after 3 retries"节奏对齐。
 *
 * 取消语义: 一旦 signal 被 abort, 立即退出循环并把 AbortError 抛出。
 */
export async function withRetry<T>(
  fn: (signal?: AbortSignal) => Promise<T>,
  options: { retries?: number; backoffMs?: readonly number[]; signal?: AbortSignal } = {},
): Promise<T> {
  const { retries = 3, backoffMs = [1000, 2000, 4000] as const, signal } = options;
  let lastErr: unknown;
  for (let attempt = 0; attempt <= retries; attempt++) {
    if (signal?.aborted) {
      throw new DOMException('Aborted', 'AbortError');
    }
    try {
      return await fn(signal);
    } catch (err: unknown) {
      // 用户主动取消 — 不重试, 立即抛
      if (isCancelError(err) || (err instanceof DOMException && err.name === 'AbortError')) {
        throw err;
      }
      lastErr = err;
      if (attempt >= retries) break;
      const wait = backoffMs[attempt] ?? backoffMs[backoffMs.length - 1] ?? 1000;
      await new Promise<void>((resolve, reject) => {
        const timer = setTimeout(resolve, wait);
        signal?.addEventListener(
          'abort',
          () => {
            clearTimeout(timer);
            reject(new DOMException('Aborted', 'AbortError'));
          },
          { once: true },
        );
      });
    }
  }
  throw lastErr;
}