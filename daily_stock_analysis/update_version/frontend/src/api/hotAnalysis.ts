/**
 * 三榜交集分析 API 客户端
 * 文档：POST /api/v1/hot-analysis/run
 */
import axios, { type AxiosInstance } from 'axios'

// API 基础 URL：开发期使用 Vite 代理（/api → 127.0.0.1:8000）
// 生产部署可通过环境变量覆盖
const API_BASE = (import.meta.env.VITE_API_BASE as string) || '/api/v1'

const http: AxiosInstance = axios.create({
  baseURL: API_BASE,
  timeout: 0, // 不超时（长任务）
})

export interface AnalysisRunItem {
  round: number
  sentiment_score: number | null
  operation_advice: string | null
  decision_type: string | null
  trend_prediction: string | null
  model_used: string | null
  timestamp: string | null
  error: string | null
}

export interface HotAnalysisStockResult {
  code: string
  name: string
  in_sources: string[]
  analyses: AnalysisRunItem[]
  pass_reason: string
  avg_score: number
  final_advice: string
}

export interface HotAnalysisRunResponse {
  status: 'ok' | 'failed' | 'timeout'
  started_at: string
  finished_at: string
  duration_seconds: number
  hot_lists_summary: Record<string, number>
  /** 每个榜单的状态: 'ok' | 'failed' | 'fallback' */
  hot_lists_status?: Record<string, string>
  /** 每个榜单的实现说明（仅兜底/异常时填） */
  hot_lists_notes?: Record<string, string>
  raw_intersection_count: number
  filtered_intersection_count: number
  analyzed_stock_count: number
  passed_stock_count: number
  results: HotAnalysisStockResult[]
  error: string | null
}

export interface HotAnalysisRunRequest {
  hot_list_n?: number
  max_intersection?: number
  exclude_kechuang?: boolean
  rounds?: number
  max_runtime_seconds?: number
  inter_round_sleep?: number
  simple_report?: boolean
}

/** 触发一次完整流程 */
export async function runHotAnalysis(
  req: HotAnalysisRunRequest = {},
): Promise<HotAnalysisRunResponse> {
  const { data } = await http.post<HotAnalysisRunResponse>('/hot-analysis/run', {
    hot_list_n: req.hot_list_n ?? 200,
    max_intersection: req.max_intersection ?? 10,
    exclude_kechuang: req.exclude_kechuang ?? true,
    rounds: req.rounds ?? 3,
    max_runtime_seconds: req.max_runtime_seconds ?? 600,
    inter_round_sleep: req.inter_round_sleep ?? 0.6,
    simple_report: req.simple_report ?? true,
  })
  return data
}

/* ----- 阶段 1：预览（仅抓榜 + 计算交集，不调 LLM） ----- */

export interface HotAnalysisPreviewRequest {
  hot_list_n?: number
  max_intersection?: number
  exclude_kechuang?: boolean
  rounds?: number
  inter_round_sleep?: number
  simple_report?: boolean
}

export interface HotAnalysisPreviewResponse {
  status: 'ok' | 'failed'
  started_at: string
  finished_at: string
  duration_seconds: number
  hot_lists_summary: Record<string, number>
  hot_lists_status?: Record<string, string>
  hot_lists_notes?: Record<string, string>
  raw_intersection_count: number
  filtered_intersection_count: number
  target_count: number
  estimated_seconds: number
  estimated_human: string
  rounds: number
  error: string | null
}

/** 阶段 1：仅抓榜 + 求交集 + 预估时间（~5-15s） */
export async function previewHotAnalysis(
  req: HotAnalysisPreviewRequest = {},
): Promise<HotAnalysisPreviewResponse> {
  const { data } = await http.post<HotAnalysisPreviewResponse>('/hot-analysis/preview', {
    hot_list_n: req.hot_list_n ?? 200,
    max_intersection: req.max_intersection ?? 10,
    exclude_kechuang: req.exclude_kechuang ?? true,
    rounds: req.rounds ?? 3,
    inter_round_sleep: req.inter_round_sleep ?? 0.6,
    simple_report: req.simple_report ?? true,
  })
  return data
}

/** 健康检查 */
export async function checkHealth(): Promise<{ status: string; akshare_available: boolean }> {
  const { data } = await http.get('/hot-analysis/health')
  return data
}
