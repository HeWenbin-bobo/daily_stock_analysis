<script setup lang="ts">
/**
 * 大盘复盘视图
 *
 * - 触发每日大盘复盘（akshare 抓指数 + 宽度 + 板块）
 * - 展示最新报告：盘面信号 / 指数结构 / 行业板块
 * - 查看历史报告列表
 * - round 6 新增：导出 .md 报告 / 对比两个日期 / 批量生成近 N 天
 */
import { ref, computed, onMounted, onBeforeUnmount, h } from 'vue'
import {
  NCard, NButton, NSpace, NText, NTag, NDataTable, NEmpty, NSpin, NAlert, NCollapse, NCollapseItem,
  NModal, NInputNumber, NInput, NPopconfirm,
} from 'naive-ui'
import {
  generateMarketReview,
  listMarketReviews,
  getLatestMarketReview,
  deleteMarketReview,
  getMarketReviewByDate,
  getMarketReviewExportUrl,
  getMarketReviewPdfUrl,
  compareMarketReviews,
  batchGenerateMarketReviews,
  type MarketReviewItem,
  type MarketReviewComparison,
  type MarketReviewBatchResponse,
} from '../api/hotAnalysis'
import { extractErrorMessage } from '../utils/formatters'

const emit = defineEmits<{ (e: 'back'): void }>()

const loading = ref(false)
const generating = ref(false)
const error = ref<string | null>(null)
const latest = ref<MarketReviewItem | null>(null)
const history = ref<MarketReviewItem[]>([])

// round 6: 对比 / 批量 UI 状态
const compareModalOpen = ref(false)
const compareDateA = ref('')
const compareDateB = ref('')
const compareResult = ref<MarketReviewComparison | null>(null)
const comparing = ref(false)

const batchModalOpen = ref(false)
const batchDays = ref(5)
const batchResult = ref<MarketReviewBatchResponse | null>(null)
const batching = ref(false)

// ★ Round 4 修复：unmount 后停止轮询，防止路由切走时仍在写 ref + 内存泄漏
let pollingCancelled = false
let pollingTimer: ReturnType<typeof setTimeout> | null = null

async function loadAll() {
  loading.value = true
  error.value = null
  try {
    try {
      latest.value = await getLatestMarketReview()
    } catch (e: unknown) {
      const anyE = e as { response?: { data?: { detail?: string } } }
      const detail = anyE.response?.data?.detail || ''
      // ★ 修复(H3)：getLatestMarketReview 失败（限流 429/网络抖动）时不要清空已有 latest ——
      //   旧实现无条件 latest.value = null，用户刚"生成成功"的复盘被随后 loadAll 清掉，
      //   页面回到空态，用户以为生成失败。仅当确实没有旧数据时才置 null。
      if (!latest.value) latest.value = null
      if (detail.includes('已自动触发')) {
        // 保留兼容：若未来后端改为后台生成，走轮询等待
        const start = Date.now()
        while (Date.now() - start < 60000) {
          if (pollingCancelled) return
          await new Promise<void>((r) => {
            pollingTimer = setTimeout(r, 3000)
          })
          pollingTimer = null
          if (pollingCancelled) return
          try {
            latest.value = await getLatestMarketReview()
            error.value = null
            return  // 成功拿到，结束
          } catch {
            // 继续轮询
          }
        }
        error.value = '后台生成超时，请稍后手动点击"生成今日复盘"'
      }
    }
    // ★ 修复(M5)：请求 page_size=50 但表格硬编码 pageSize:10 → 第 5 页后无数据。
    //   统一为按后端最大分页 100 拉取，表格分页显示全部。
    const list = await listMarketReviews(1, 100)
    history.value = list.items
  } catch (e: unknown) {
    error.value = extractErrorMessage(e, '加载大盘复盘失败')
  } finally {
    loading.value = false
  }
}

onBeforeUnmount(() => {
  pollingCancelled = true
  if (pollingTimer) {
    clearTimeout(pollingTimer)
    pollingTimer = null
  }
})

async function handleGenerate() {
  // ★ round 9：防双击 — generating 状态时直接拒掉
  if (generating.value) return
  generating.value = true
  error.value = null
  try {
    const item = await generateMarketReview()
    latest.value = item
    await loadAll()
  } catch (e: unknown) {
    error.value = extractErrorMessage(e, '生成大盘复盘失败')
  } finally {
    generating.value = false
  }
}

async function handleDelete(review_date: string) {
  try {
    await deleteMarketReview(review_date)
    if (latest.value?.review_date === review_date) {
      latest.value = null
    }
    await loadAll()
  } catch (e: unknown) {
    error.value = extractErrorMessage(e, '删除失败')
  }
}

async function viewItem(review_date: string) {
  const existing = history.value.find((x) => x.review_date === review_date)
  if (existing) {
    latest.value = existing
    return
  }
  try {
    latest.value = await getMarketReviewByDate(review_date)
  } catch (e: unknown) {
    error.value = extractErrorMessage(e, '获取复盘失败')
  }
}

// round 6: 导出 .md 报告
function handleExport(review_date: string) {
  // 浏览器原生下载，axios 无需走；Content-Disposition 由后端设置
  window.open(getMarketReviewExportUrl(review_date), '_blank')
}

// round 8: 导出 PDF 报告（后端用 reportlab 生成）
function handleExportPdf(review_date: string) {
  window.open(getMarketReviewPdfUrl(review_date), '_blank')
}

// round 6: 对比
function openCompare() {
  compareDateA.value = history.value[1]?.review_date ?? ''
  compareDateB.value = history.value[0]?.review_date ?? ''
  compareResult.value = null
  compareModalOpen.value = true
}
async function runCompare() {
  if (!compareDateA.value || !compareDateB.value) return
  // ★ 修复(M4)：手动输入日期需校验 YYYY-MM-DD 格式（否则后端 404"其中一日不存在"）
  const dateRe = /^\d{4}-\d{2}-\d{2}$/
  if (!dateRe.test(compareDateA.value) || !dateRe.test(compareDateB.value)) {
    error.value = '对比日期格式必须是 YYYY-MM-DD'
    return
  }
  if (compareDateA.value === compareDateB.value) {
    error.value = '对比的两个日期不能相同'
    return
  }
  comparing.value = true
  error.value = null
  try {
    compareResult.value = await compareMarketReviews(compareDateA.value, compareDateB.value)
  } catch (e: unknown) {
    error.value = extractErrorMessage(e, '对比失败')
  } finally {
    comparing.value = false
  }
}

// round 6: 批量生成
function openBatch() {
  batchDays.value = 5
  batchResult.value = null
  batchModalOpen.value = true
}
async function runBatch() {
  batching.value = true
  error.value = null
  try {
    batchResult.value = await batchGenerateMarketReviews(batchDays.value)
    await loadAll()
  } catch (e: unknown) {
    error.value = extractErrorMessage(e, '批量生成失败')
  } finally {
    batching.value = false
  }
}

const indexOrder = ['上证指数', '深证成指', '创业板指', '科创50', '上证50', '沪深300']
const indices = computed(() => {
  if (!latest.value) return [] as Array<{ name: string; data: Record<string, unknown> }>
  const src = latest.value.indices as Record<string, Record<string, unknown>>
  const out: Array<{ name: string; data: Record<string, unknown> }> = []
  for (const name of indexOrder) {
    if (src[name]) out.push({ name, data: src[name] })
  }
  for (const [name, data] of Object.entries(src)) {
    if (!indexOrder.includes(name)) out.push({ name, data })
  }
  return out
})

function fmtPct(p: unknown): string {
  if (p == null || p === '') return '—'
  const n = Number(p)
  if (Number.isNaN(n)) return '—'
  const sign = n > 0 ? '+' : ''
  return `${sign}${n.toFixed(2)}%`
}

function fmtNum(v: unknown, digits = 2): string {
  if (v == null) return '—'
  const n = Number(v)
  if (Number.isNaN(n)) return '—'
  return n.toFixed(digits)
}

function fmtYi(v: unknown): string {
  if (v == null) return '—'
  const n = Number(v)
  if (Number.isNaN(n)) return '—'
  return n.toLocaleString('zh-CN', { maximumFractionDigits: 0 })
}

const signalScore = computed(() => {
  const s = (latest.value?.signals as { score?: number } | undefined)?.score ?? 0
  return Number(s)
})
const signalTag = computed(() => (latest.value?.signals as { tag?: string } | undefined)?.tag || '—')
const signalAdvice = computed(() => (latest.value?.signals as { advice?: string } | undefined)?.advice || '')
const signalReason = computed(() => (latest.value?.signals as { reason?: string } | undefined)?.reason || '')

const breadth = computed(() => (latest.value?.breadth as Record<string, unknown>) || {})

interface SectorGroup {
  top_gainers?: Array<Record<string, unknown>>
  top_losers?: Array<Record<string, unknown>>
  count?: number
  error?: string
}
interface SectorsShape {
  industries?: SectorGroup
  concepts?: SectorGroup
  // legacy fallback (round-3 records persisted with flat top_gainers/top_losers)
  top_gainers?: Array<Record<string, unknown>>
  top_losers?: Array<Record<string, unknown>>
}

const sectors = computed(() => {
  const s = (latest.value?.sectors as SectorsShape | undefined) || {}
  // New shape: { industries: {top_gainers, top_losers}, concepts: {...} }
  if (s.industries || s.concepts) {
    return {
      industries: {
        gainers: s.industries?.top_gainers ?? [],
        losers: s.industries?.top_losers ?? [],
        count: s.industries?.count ?? 0,
        error: s.industries?.error ?? '',
      },
      concepts: {
        gainers: s.concepts?.top_gainers ?? [],
        losers: s.concepts?.top_losers ?? [],
        count: s.concepts?.count ?? 0,
        error: s.concepts?.error ?? '',
      },
    }
  }
  // Legacy: { top_gainers, top_losers } at top level
  return {
    industries: { gainers: s.top_gainers ?? [], losers: s.top_losers ?? [], count: 0, error: '' },
    concepts: { gainers: [], losers: [], count: 0, error: 'legacy record' },
  }
})

const warnings = computed(() => (latest.value?.warnings as string[]) || [])

onMounted(loadAll)

// 历史列表列（模板外定义，避免 in-template TS 注解报错）
interface IndexRow { name: string; data: Record<string, unknown> }
function buildHistoryColumns() {
  return [
    { title: '日期', key: 'review_date', width: 130 },
    { title: '状态', key: 'status', width: 80 },
    { title: '摘要', key: 'summary', ellipsis: { tooltip: true } },
    {
      title: '操作',
      key: 'actions',
      width: 180,
      render(row: MarketReviewItem) {
        // ★ 修复(H4)：删除无确认 → 包 NPopconfirm，误删一天复盘不可恢复
        return h(NSpace, { size: 'small' }, () => [
          h(NButton, { size: 'tiny', secondary: true, onClick: () => viewItem(row.review_date) }, () => '查看'),
          h(NPopconfirm, { onPositiveClick: () => handleDelete(row.review_date) }, {
            trigger: () => h(NButton, { size: 'tiny', type: 'error', secondary: true }, () => '删除'),
            default: () => `确定删除 ${row.review_date} 复盘？此操作不可恢复。`,
          }),
        ])
      },
    },
  ]
}

function buildIndexColumns() {
  return [
    { title: '指数', key: 'name', width: 100 },
    {
      title: '最新',
      key: 'price',
      width: 100,
      render(row: IndexRow) {
        return fmtNum(row.data.price, 2)
      },
    },
    {
      title: '涨跌幅',
      key: 'change_pct',
      width: 110,
      render(row: IndexRow) {
        return fmtPct(row.data.change_pct)
      },
    },
    {
      title: '开盘',
      key: 'open',
      render(row: IndexRow) {
        return fmtNum(row.data.open, 2)
      },
    },
    {
      title: '最高',
      key: 'high',
      render(row: IndexRow) {
        return fmtNum(row.data.high, 2)
      },
    },
    {
      title: '最低',
      key: 'low',
      render(row: IndexRow) {
        return fmtNum(row.data.low, 2)
      },
    },
    {
      title: '成交额(亿)',
      key: 'turnover',
      render(row: IndexRow) {
        return fmtYi(row.data.turnover)
      },
    },
  ]
}

function buildSectorColumns(positive: boolean, label: string) {
  // 数据字段固定为 name / change_pct（与后端 market_review_service._compute_sectors() 一致）；
  // 名称列必须 key='name'，否则 n-data-table 找不到行属性 → 名称栏空白
  return [
    {
      title: label,
      key: 'name',
      width: 110,
      ellipsis: { tooltip: true },
    },
    {
      title: '涨跌幅',
      key: 'change_pct',
      width: 90,
      render(row: Record<string, unknown>) {
        const v = Number(row.change_pct ?? row.avg_change_pct ?? 0)
        const sign = v > 0 ? '+' : ''
        return `${sign}${v.toFixed(2)}%`
      },
    },
  ]
}

const historyColumns = buildHistoryColumns()
const indexColumns = buildIndexColumns()
const industryGainColumns = buildSectorColumns(true, '行业')
const industryLoseColumns = buildSectorColumns(false, '行业')
const conceptGainColumns = buildSectorColumns(true, '概念')
const conceptLoseColumns = buildSectorColumns(false, '概念')
</script>

<template>
  <div>
    <n-space justify="space-between" align="center" style="margin-bottom: 16px">
      <n-space>
        <n-text strong style="font-size: 18px">📊 大盘复盘</n-text>
        <n-tag :bordered="false" type="info" size="small">每日报告</n-tag>
      </n-space>
      <n-space>
        <n-button
          type="primary"
          :loading="generating"
          @click="handleGenerate"
          aria-label="生成今日大盘复盘"
        >
          🚀 生成今日复盘
        </n-button>
        <n-button
          quaternary
          :disabled="history.length < 2"
          @click="openCompare"
          aria-label="对比两个日期的复盘"
        >
          🔄 对比
        </n-button>
        <n-button
          quaternary
          @click="openBatch"
          aria-label="批量生成近 N 天复盘"
        >
          📦 批量
        </n-button>
        <n-button
          quaternary
          :disabled="!latest"
          @click="handleExport(latest!.review_date)"
          aria-label="导出当前复盘为 Markdown"
        >
          📥 .md
        </n-button>
        <n-button
          quaternary
          :disabled="!latest"
          @click="handleExportPdf(latest!.review_date)"
          aria-label="导出当前复盘为 PDF"
        >
          📄 PDF
        </n-button>
        <n-button quaternary @click="emit('back')" aria-label="返回分析主页">← 返回</n-button>
      </n-space>
    </n-space>

    <n-alert v-if="error" type="error" :show-icon="true" style="margin-bottom: 16px" role="alert" aria-live="assertive">
      {{ error }}
    </n-alert>

    <n-alert
      v-if="warnings.length"
      type="warning"
      :show-icon="true"
      style="margin-bottom: 16px"
      title="部分数据源获取失败（已用降级数据填充）"
    >
      <ul style="margin: 0; padding-left: 20px">
        <li v-for="(w, i) in warnings" :key="i">{{ w }}</li>
      </ul>
    </n-alert>

    <n-spin :show="loading" role="status" aria-live="polite">
      <!-- 详情卡 -->
      <n-card v-if="latest" size="small" style="border-radius: 12px; margin-bottom: 16px" :bordered="false">
        <template #header>
          <n-space align="center">
            <span style="font-weight: bold; font-size: 16px">📅 {{ latest.review_date }} 大盘复盘</span>
            <n-tag :type="latest.status === 'ok' ? 'success' : 'warning'" size="small">
              {{ latest.status }}
            </n-tag>
          </n-space>
        </template>
        <template #header-extra>
          <n-text depth="3" style="font-size: 12px">耗时 {{ latest.duration_seconds }}s</n-text>
        </template>

        <!-- 信号摘要 -->
        <div style="display: flex; gap: 16px; align-items: center; padding: 12px; border-radius: 10px; background: var(--accent-soft); border: 1px solid var(--accent-border); margin-bottom: 16px">
          <span style="font-size: 36px">🎯</span>
          <div style="flex: 1">
            <div style="font-size: 12px; color: var(--text-secondary)">盘面信号</div>
            <div style="font-size: 22px; font-weight: bold; color: var(--accent)">
              {{ signalScore }} / 100（{{ signalTag }}）
            </div>
            <div style="font-size: 13px; color: #475569; margin-top: 4px">
              <strong>建议：</strong>{{ signalAdvice }}
            </div>
            <div style="font-size: 12px; color: #94a3b8; margin-top: 2px">
              {{ signalReason }}
            </div>
          </div>
          <div style="text-align: right">
            <div style="font-size: 12px; color: #64748b">上涨 / 下跌</div>
            <div style="font-size: 18px; font-weight: bold; color: #059669">
              <span style="color: #16a34a">{{ breadth.advance || 0 }}</span>
              /
              <span style="color: #dc2626">{{ breadth.decline || 0 }}</span>
            </div>
            <div style="font-size: 12px; color: #94a3b8; margin-top: 2px">
              涨跌停差 {{ breadth.limit_diff || 0 }}
            </div>
            <div style="font-size: 12px; color: #94a3b8">
              成交额 {{ fmtYi(breadth.total_turnover_yi) }} 亿
            </div>
          </div>
        </div>

        <!-- 指数结构 -->
        <div style="margin-top: 16px; font-weight: bold; font-size: 14px; color: #475569">📈 指数结构</div>
        <n-data-table
          :columns="indexColumns"
          :data="indices"
          :bordered="false"
          :single-line="false"
          size="small"
          style="margin-top: 8px"
        />

        <!-- 行业板块 -->
        <div style="margin-top: 16px; font-weight: bold; font-size: 14px; color: #475569">
          🔥 行业板块
          <span style="font-size: 11px; color: #94a3b8; font-weight: normal; margin-left: 8px">
            ({{ sectors.industries.count }} 个)
          </span>
        </div>
        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-top: 8px">
          <div>
            <div style="font-size: 12px; color: #16a34a; margin-bottom: 6px">📈 领涨 Top 5</div>
            <n-data-table
              :columns="industryGainColumns"
              :data="sectors.industries.gainers"
              :bordered="false"
              size="small"
            />
          </div>
          <div>
            <div style="font-size: 12px; color: #dc2626; margin-bottom: 6px">📉 领跌 Top 5</div>
            <n-data-table
              :columns="industryLoseColumns"
              :data="sectors.industries.losers"
              :bordered="false"
              size="small"
            />
          </div>
        </div>

        <!-- 概念板块 -->
        <div
          v-if="sectors.concepts.gainers.length || sectors.concepts.losers.length"
          style="margin-top: 16px; font-weight: bold; font-size: 14px; color: #475569"
        >
          💡 概念板块
          <span style="font-size: 11px; color: #94a3b8; font-weight: normal; margin-left: 8px">
            ({{ sectors.concepts.count }} 个)
          </span>
        </div>
        <div
          v-if="sectors.concepts.gainers.length || sectors.concepts.losers.length"
          style="display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-top: 8px"
        >
          <div>
            <div style="font-size: 12px; color: #16a34a; margin-bottom: 6px">📈 领涨 Top 5</div>
            <n-data-table
              :columns="conceptGainColumns"
              :data="sectors.concepts.gainers"
              :bordered="false"
              size="small"
            />
          </div>
          <div>
            <div style="font-size: 12px; color: #dc2626; margin-bottom: 6px">📉 领跌 Top 5</div>
            <n-data-table
              :columns="conceptLoseColumns"
              :data="sectors.concepts.losers"
              :bordered="false"
              size="small"
            />
          </div>
        </div>

        <!-- Markdown 全文 -->
        <n-collapse style="margin-top: 16px">
          <n-collapse-item title="📄 完整 Markdown 报告" name="md">
            <pre style="white-space: pre-wrap; word-break: break-word; background: #f8fafc; padding: 12px; border-radius: 8px; font-size: 12px; line-height: 1.6; max-height: 400px; overflow: auto">{{ latest.report_markdown }}</pre>
          </n-collapse-item>
        </n-collapse>
      </n-card>

      <n-empty v-else description="尚无大盘复盘记录，点击右上角「生成今日复盘」开始" style="margin: 24px 0" />

      <!-- 历史列表 -->
      <n-card title="📚 历史复盘记录" size="small" style="border-radius: 12px" :bordered="false">
        <n-data-table :columns="historyColumns" :data="history" :bordered="false" size="small" :pagination="{ pageSize: 10 }" />
      </n-card>
    </n-spin>

    <!-- round 6: 对比两个日期 modal -->
    <n-modal
      v-model:show="compareModalOpen"
      preset="card"
      title="🔄 对比两个日期"
      style="max-width: 720px"
      :bordered="false"
    >
      <n-space vertical :size="[12, 12]">
        <n-space>
          <n-text>基期</n-text>
          <n-input v-model:value="compareDateA" placeholder="YYYY-MM-DD" aria-label="基期日期" style="width: 160px" />
          <n-text>目标期</n-text>
          <n-input v-model:value="compareDateB" placeholder="YYYY-MM-DD" aria-label="目标期日期" style="width: 160px" />
          <n-button type="primary" :loading="comparing" @click="runCompare" aria-label="执行对比分析">开始对比</n-button>
        </n-space>
        <div v-if="compareResult">
          <n-alert type="info" :show-icon="true" style="margin-bottom: 12px">
            <strong>{{ compareResult.date_a }}</strong> → <strong>{{ compareResult.date_b }}</strong>
            ；共 <strong>{{ compareResult.diff.length }}</strong> 个字段差异
          </n-alert>
          <n-data-table
            :columns="[
              { title: '指标', key: 'path', width: 200, ellipsis: { tooltip: true } },
              { title: '基期', key: 'from', width: 100 },
              { title: '目标', key: 'to', width: 100 },
              { title: '变化', key: 'delta', width: 100,
                render: (r: Record<string, unknown>) => r.delta == null ? '-' : (Number(r.delta) > 0 ? `+${r.delta}` : String(r.delta)) },
            ]"
            :data="Object.entries(compareResult.summary).map(([path, v]) => ({ path, from: v.from, to: v.to, delta: v.delta }))"
            :bordered="false"
            size="small"
          />
        </div>
      </n-space>
    </n-modal>

    <!-- round 6: 批量生成 modal -->
    <n-modal
      v-model:show="batchModalOpen"
      preset="card"
      title="📦 批量生成近 N 天"
      style="max-width: 720px"
      :bordered="false"
    >
      <n-space vertical :size="[12, 12]">
        <n-space align="center">
          <n-text>回溯天数（不含今天）</n-text>
          <n-input-number v-model:value="batchDays" :min="1" :max="30" aria-label="回溯天数" style="width: 120px" />
          <n-button type="primary" :loading="batching" @click="runBatch" aria-label="开始批量生成复盘">开始生成</n-button>
        </n-space>
        <n-text depth="3" style="font-size: 12px">
          提示：每天约 15-25s；5 天 ≈ 2 分钟。已有记录自动跳过。
        </n-text>
        <div v-if="batchResult && batchResult.length">
          <n-alert :type="batchResult.some(r => r.status === 'failed') ? 'warning' : 'success'" :show-icon="true" style="margin-bottom: 12px">
            请求 {{ batchResult.length }} 天：完成 {{ batchResult.filter(r => r.status === 'ok').length }} 天，失败 {{ batchResult.filter(r => r.status === 'failed').length }} 天
          </n-alert>
          <n-data-table
            :columns="[
              { title: '日期', key: 'review_date', width: 140 },
              { title: '状态', key: 'status', width: 100,
                render: (r: Record<string, unknown>) => {
                  const colors: Record<string, string> = { ok: 'success', skipped: 'default', failed: 'error' }
                  return h(NTag, { type: (colors[r.status as string] || 'default') as 'default', size: 'small', bordered: false }, () => r.status as string)
                }
              },
              { title: '摘要', key: 'summary' },
            ]"
            :data="batchResult"
            :bordered="false"
            size="small"
          />
        </div>
      </n-space>
    </n-modal>
  </div>
</template>

<style scoped>
:deep(.n-data-table .n-data-table-th),
:deep(.n-data-table .n-data-table-td) {
  font-size: 13px;
}
</style>
