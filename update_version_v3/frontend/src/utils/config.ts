/**
 * 配置持久化工具 —— localStorage 读写
 *
 * ★ round 8：引入 config_version 字段做 schema migration。
 *   - 老数据无 version 视为 v0 → 整体丢弃，回到 DEFAULT_CONFIG（防 stale 字段污染）
 *   - v1 之后只增不删字段；字段类型不兼容时手动 bump version 并写迁移函数
 */

const STORAGE_KEY = 'hot-analysis-config'
const CURRENT_VERSION = 1

export interface AppConfig {
  config_version: number
  maxRuntime: number
  maxIntersection: number
  minScore: number
  allowedAdvice: string[]
  excludeKeywords: string[]
  requireAllRounds: boolean
  llmTimeout: number
  rounds: number
  interRoundSleep: number
  singleConcurrency: number
  /** ★ Round 16：市场分析并发分析股票数（1=串行） */
  maxConcurrency: number
}

const DEFAULT_CONFIG: AppConfig = {
  config_version: CURRENT_VERSION,
  maxRuntime: 180,
  maxIntersection: 10,
  minScore: 60,
  allowedAdvice: ['买入', '加仓', '持有', 'BUY', 'HOLD', 'ADD'],
  excludeKeywords: ['卖出', '减仓', 'SELL', 'REDUCE'],
  requireAllRounds: true,
  llmTimeout: 180,
  rounds: 3,
  interRoundSleep: 0.6,
  singleConcurrency: 3,
  maxConcurrency: 2,
}

/**
 * 迁移函数：每个 version 跳变时由迁移链负责把老数据搬到新结构。
 * 当前没有需要迁移的字段，但保留 hook 防止将来直接 bump 漏迁移。
 */
function migrate(parsed: Record<string, unknown>): AppConfig | null {
  const v = (parsed.config_version as number) ?? 0
  if (v < 1) {
    // 老数据：丢弃，回到默认（避免 stale 字段如 llm_timeout=10 触发 422）
    return null
  }
  // 后续 v1 → v2 迁移写在这里
  return { ...DEFAULT_CONFIG, ...parsed, config_version: CURRENT_VERSION } as AppConfig
}

/** 从 localStorage 加载配置（自动迁移/版本校验） */
export function loadConfig(): AppConfig {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (raw) {
      const parsed = JSON.parse(raw) as Record<string, unknown>
      const migrated = migrate(parsed)
      if (migrated) return migrated
    }
  } catch { /* ignore parse error */ }
  return { ...DEFAULT_CONFIG }
}

/** 保存配置到 localStorage（自动写入 version） */
export function saveConfig(config: Partial<AppConfig>): void {
  try {
    const current = loadConfig()
    const merged = { ...current, ...config, config_version: CURRENT_VERSION }
    localStorage.setItem(STORAGE_KEY, JSON.stringify(merged))
  } catch { /* ignore quota exceeded etc. */ }
}

/** 测试/调试用：清空配置（force reload defaults） */
export function resetConfig(): void {
  try {
    localStorage.removeItem(STORAGE_KEY)
  } catch { /* ignore */ }
}
