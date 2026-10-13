<script setup lang="ts">
/**
 * 四榜交集分析 - 主页面
 *
 * 功能：
 * 1. 顶部：一个醒目的"分析"按钮
 * 2. 中部：加载状态（带进度信息：抓榜单 N / 交集中 / 分析 1/N）
 * 3. 底部：可排序、可展开的表格（每只股票三轮分析详情）
 * 4. 顶部辅助：复制/导出/排序说明
 * 5. 空态：友好的"暂未发现符合条件的股票"
 *
 * 数据流：
 *   点击按钮 -> previewHotAnalysis() -> 阶段1显示抓榜+交集+预估
 *   用户确认 -> runHotAnalysis() -> 阶段2 真实 LLM 分析
 */
import { ref, computed, h, onMounted, onBeforeUnmount } from 'vue'
import {
  NConfigProvider,
  NCard,
  NButton,
  NSpin,
  NAlert,
  NSpace,
  NTag,
  NDataTable,
  NText,
  NEmpty,
  NStatistic,
  NGrid,
  NGi,
  NTooltip,
  NRadioGroup,
  NRadio,
  type DataTableColumns,
} from 'naive-ui'
import { zhCN, dateZhCN } from 'naive-ui'
import {
  runHotAnalysis,
  previewHotAnalysis,
  type HotAnalysisRunResponse,
  type HotAnalysisPreviewResponse,
  type HotAnalysisStockResult,
  type AnalysisRunItem,
} from './api/hotAnalysis'

// ---------------------------------------------------------------------------
// 状态
// ---------------------------------------------------------------------------
// ★ 两阶段流程：'idle' → 'previewing' → 'previewed' → 'analyzing' → 'done'
type Phase = 'idle' | 'previewing' | 'previewed' | 'analyzing' | 'done'
const phase = ref<Phase>('idle')

const loading = ref(false)  // 综合 loading，按钮 disabled
const error = ref<string | null>(null)
const result = ref<HotAnalysisRunResponse | null>(null)
const preview = ref<HotAnalysisPreviewResponse | null>(null)
const progressTip = ref('点击下方按钮开始全流程分析')
const elapsedSec = ref(0)
let timer: number | null = null

// 排序
type SortKey = 'code' | 'name' | 'avg_score' | 'final_advice'
const sortKey = ref<SortKey>('avg_score')
const sortOrder = ref<'asc' | 'desc'>('desc')

// 复制/导出反馈
const copyOk = ref(false)
const exportOk = ref(false)

// 最大任务时间选择（分钟），默认 180
const MAX_RUNTIME_OPTIONS: number[] = [60, 120, 180, 240, 300, 600, 1200]
const selectedMaxRuntime = ref<number>(180)

// 最大分析股票数选择（防止 LLM 调用爆炸），默认 10
const INTERSECTION_OPTIONS: number[] = [1, 5, 10, 30, 50, 100, 200]
const selectedMaxIntersection = ref<number>(10)

// Real-time date/clock for homepage banner
const WEEK_LABELS = ['周日', '周一', '周二', '周三', '周四', '周五', '周六']
const now = ref<Date>(new Date())
let clockTimer: number | null = null

function pad2(n: number): string {
  return n < 10 ? `0${n}` : `${n}`
}
function formatDate(d: Date): string {
  return `${d.getFullYear()} 年 ${d.getMonth() + 1} 月 ${d.getDate()} 日`
}
function formatTime(d: Date): string {
  return `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`
}

onMounted(() => {
  clockTimer = window.setInterval(() => { now.value = new Date() }, 1000)
})
onBeforeUnmount(() => {
  if (clockTimer != null) {
    clearInterval(clockTimer)
    clockTimer = null
  }
})

// ---------------------------------------------------------------------------
// 排序后的结果
// ---------------------------------------------------------------------------
const sortedResults = computed(() => {
  if (!result.value) return []
  const list = [...result.value.results]
  const dir = sortOrder.value === 'asc' ? 1 : -1
  return list.sort((a, b) => {
    let av: number | string = 0
    let bv: number | string = 0
    if (sortKey.value === 'code') {
      av = a.code
      bv = b.code
    } else if (sortKey.value === 'name') {
      av = a.name
      bv = b.name
    } else if (sortKey.value === 'avg_score') {
      av = a.avg_score
      bv = b.avg_score
    } else if (sortKey.value === 'final_advice') {
      av = a.final_advice
      bv = b.final_advice
    }
    if (typeof av === 'string' && typeof bv === 'string') {
      return av.localeCompare(bv) * dir
    }
    return ((av as number) - (bv as number)) * dir
  })
})

// ---------------------------------------------------------------------------
// 阶段 1：触发预览（抓榜 + 求交集 + 预估时间，不调 LLM）
// ---------------------------------------------------------------------------
async function onRun() {
  if (loading.value) return
  phase.value = 'previewing'
  preview.value = null
  result.value = null
  error.value = null
  elapsedSec.value = 0
  progressTip.value = '正在抓取四榜（东方财富 / 雪球 / 同花顺 / 全市场活跃）…'

  try {
    const data = await previewHotAnalysis({
      hot_list_n: 200,
      max_intersection: selectedMaxIntersection.value,
      rounds: 3,
      inter_round_sleep: 0.6,
      simple_report: true,
    })
    if (data.status === 'failed') {
      error.value = data.error || '预览失败'
      progressTip.value = '抓榜失败'
      phase.value = 'idle'
      return
    }
    preview.value = data
    phase.value = 'previewed'
    progressTip.value = `准备分析 ${data.target_count} 只股票`
  } catch (e: unknown) {
    const err = e as { message?: string; response?: { data?: { detail?: string } } }
    error.value =
      err?.response?.data?.detail ||
      err?.message ||
      '请求失败，请确认后端已启动（端口 8000）'
    progressTip.value = '抓榜失败'
    phase.value = 'idle'
  }
}

// ---------------------------------------------------------------------------
// 阶段 2：用户确认后真正跑 LLM 分析
// ---------------------------------------------------------------------------
async function onConfirmAnalysis() {
  if (loading.value || !preview.value) return
  phase.value = 'analyzing'
  result.value = null
  loading.value = true
  startTimer()
  progressTip.value = `正在对 ${preview.value.target_count} 只股票做三轮 LLM 分析…`

  try {
    const data = await runHotAnalysis({
      hot_list_n: 200,
      max_intersection: selectedMaxIntersection.value,
      rounds: 3,
      max_runtime_seconds: selectedMaxRuntime.value * 60,
      inter_round_sleep: 0.6,
      simple_report: true,
    })
    result.value = data
    if (data.status === 'failed') {
      error.value = data.error || '后端返回 failed 状态'
    } else {
      progressTip.value =
        data.passed_stock_count > 0
          ? `分析完成：共 ${data.passed_stock_count} 只股票通过过滤`
          : '分析完成，但本轮未发现符合条件的股票'
    }
    phase.value = 'done'
  } catch (e: unknown) {
    const err = e as { message?: string; response?: { data?: { detail?: string } } }
    error.value =
      err?.response?.data?.detail ||
      err?.message ||
      '请求失败，请确认后端已启动（端口 8000）'
    progressTip.value = '分析失败'
  } finally {
    loading.value = false
    stopTimer()
  }
}

function onCancelPreview() {
  preview.value = null
  phase.value = 'idle'
  progressTip.value = '点击下方按钮开始全流程分析'
}

function startTimer() {
  const start = Date.now()
  timer = window.setInterval(() => {
    elapsedSec.value = Math.floor((Date.now() - start) / 1000)
  }, 1000)
}
function stopTimer() {
  if (timer) {
    clearInterval(timer)
    timer = null
  }
}

// ---------------------------------------------------------------------------
// 排序处理
// ---------------------------------------------------------------------------
function toggleSort(key: SortKey) {
  if (sortKey.value === key) {
    sortOrder.value = sortOrder.value === 'asc' ? 'desc' : 'asc'
  } else {
    sortKey.value = key
    sortOrder.value = key === 'avg_score' ? 'desc' : 'asc'
  }
}

// ---------------------------------------------------------------------------
// 复制 / 导出
// ---------------------------------------------------------------------------
function buildPlainText(): string {
  if (!result.value) return ''
  const lines: string[] = []
  lines.push('# 四榜交集分析结果')
  lines.push(`# 抓取: ${JSON.stringify(result.value.hot_lists_summary)}`)
  lines.push(
    `# 交集: raw=${result.value.raw_intersection_count} filtered=${result.value.filtered_intersection_count} 通过=${result.value.passed_stock_count}`,
  )
  lines.push(
    `# 用时: ${result.value.duration_seconds}s | 状态: ${result.value.status}`,
  )
  lines.push('')
  for (const s of sortedResults.value) {
    lines.push(
      `${s.code} ${s.name} | 均分=${s.avg_score} | 建议=${s.final_advice} | 命中榜=${s.in_sources.join('+')}`,
    )
    for (const a of s.analyses) {
      const err = a.error ? ` | ERR: ${a.error}` : ''
      lines.push(
        `  - 第${a.round}轮: score=${a.sentiment_score ?? '-'} advice=${a.operation_advice ?? '-'}${err}`,
      )
    }
    lines.push('')
  }
  return lines.join('\n')
}

async function onCopy() {
  const text = buildPlainText()
  if (!text) return
  try {
    await navigator.clipboard.writeText(text)
    copyOk.value = true
    setTimeout(() => (copyOk.value = false), 1800)
  } catch {
    // 退路：临时 textarea
    const ta = document.createElement('textarea')
    ta.value = text
    document.body.appendChild(ta)
    ta.select()
    document.execCommand('copy')
    document.body.removeChild(ta)
    copyOk.value = true
    setTimeout(() => (copyOk.value = false), 1800)
  }
}

function onExport() {
  const text = buildPlainText()
  if (!text) return
  const blob = new Blob([text], { type: 'text/plain;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `hot-analysis-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-')}.txt`
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  URL.revokeObjectURL(url)
  exportOk.value = true
  setTimeout(() => (exportOk.value = false), 1800)
}

// ---------------------------------------------------------------------------
// 工具：评分颜色 / 建议颜色
// ---------------------------------------------------------------------------
function scoreColor(score: number | null | undefined): string {
  if (score == null) return '#94a3b8'
  if (score >= 80) return '#16a34a'
  if (score >= 60) return '#22c55e'
  if (score >= 40) return '#f59e0b'
  return '#ef4444'
}
function adviceType(advice: string | null | undefined): 'success' | 'warning' | 'error' | 'default' {
  if (!advice) return 'default'
  if (advice.includes('买入') || advice.includes('加仓')) return 'success'
  if (advice.includes('持有') || advice.toLowerCase() === 'hold') return 'success'
  if (advice.includes('卖出') || advice.includes('减仓')) return 'error'
  if (advice.includes('观望') || advice.includes('等待')) return 'warning'
  return 'default'
}
function passReasonText(reason: string): string {
  const map: Record<string, string> = {
    score: '评分均≥60',
    advice: '建议均为买/持',
    'score+advice': '评分+建议',
    incomplete: '数据不全',
    rejected_sell: '出现卖出',
    rejected_filter: '未达条件',
  }
  return map[reason] || reason
}
function passReasonType(reason: string): 'success' | 'default' | 'warning' {
  if (['score', 'advice', 'score+advice'].includes(reason)) return 'success'
  return 'warning'
}

/** ★ 状态徽章：把后端返回的 hot_lists_status 转换成 UI 元数据 */
function statusType(
  status: string | undefined,
): { color: string; label: string; icon: string } {
  if (status === 'ok') return { color: '#16a34a', label: '正常', icon: '✓' }
  if (status === 'fallback')
    return { color: '#f59e0b', label: '兜底', icon: '⚠' }
  if (status === 'failed') return { color: '#ef4444', label: '失败', icon: '✗' }
  return { color: '#94a3b8', label: '未知', icon: '?' }
}

/** 哪些榜单本次 failed（用于顶部警告条） */
const failedSources = computed(() => {
  const list = result.value?.hot_lists_status ?? {}
  return Object.entries(list)
    .filter(([, s]) => s === 'failed')
    .map(([k]) => k)
})

// ---------------------------------------------------------------------------
// 工具：构建 R{n} 提示
// ---------------------------------------------------------------------------
function buildAnalysisTip(a: AnalysisRunItem): string {
  if (a.error) return `第 ${a.round} 轮失败：${a.error}`
  return [
    `第 ${a.round} 轮`,
    `评分: ${a.sentiment_score}`,
    `建议: ${a.operation_advice}`,
    `趋势: ${a.trend_prediction || '-'}`,
    `模型: ${a.model_used || '-'}`,
    `时间: ${a.timestamp || '-'}`,
  ].join('\n')
}

// ---------------------------------------------------------------------------
// 表格列定义
// ---------------------------------------------------------------------------
const columns = computed<DataTableColumns<HotAnalysisStockResult>>(() => [
  {
    title: () =>
      h(
        'span',
        { onClick: () => toggleSort('code'), style: 'cursor:pointer' },
        `代码 ${sortKey.value === 'code' ? (sortOrder.value === 'asc' ? '↑' : '↓') : ''}`,
      ),
    key: 'code',
    width: 90,
    render: (r) => h('strong', { style: 'font-family: monospace' }, r.code),
  },
  {
    title: () =>
      h(
        'span',
        { onClick: () => toggleSort('name'), style: 'cursor:pointer' },
        `名称 ${sortKey.value === 'name' ? (sortOrder.value === 'asc' ? '↑' : '↓') : ''}`,
      ),
    key: 'name',
    width: 110,
  },
  {
    title: () =>
      h(
        'span',
        { onClick: () => toggleSort('avg_score'), style: 'cursor:pointer' },
        `平均分 ${sortKey.value === 'avg_score' ? (sortOrder.value === 'asc' ? '↑' : '↓') : ''}`,
      ),
    key: 'avg_score',
    width: 100,
    sorter: (a, b) => a.avg_score - b.avg_score,
    render: (r) =>
      h(
        'span',
        {
          style: `color:${scoreColor(r.avg_score)};font-weight:600;font-size:15px`,
        },
        String(r.avg_score),
      ),
  },
  {
    title: () =>
      h(
        'span',
        { onClick: () => toggleSort('final_advice'), style: 'cursor:pointer' },
        `最终建议 ${sortKey.value === 'final_advice' ? (sortOrder.value === 'asc' ? '↑' : '↓') : ''}`,
      ),
    key: 'final_advice',
    width: 110,
    render: (r) =>
      h(
        NTag,
        { type: adviceType(r.final_advice), bordered: false, round: true },
        { default: () => r.final_advice || '-' },
      ),
  },
  {
    title: '命中榜',
    key: 'in_sources',
    width: 200,
    render: (r) =>
      h(
        NSpace,
        { size: 4 },
        () =>
          r.in_sources.map((s) =>
            h(NTag, { size: 'small', type: 'info', bordered: false }, () => s),
          ),
      ),
  },
  {
    title: '通过原因',
    key: 'pass_reason',
    width: 110,
    render: (r) =>
      h(
        NTag,
        { type: passReasonType(r.pass_reason), size: 'small', bordered: false },
        { default: () => passReasonText(r.pass_reason) },
      ),
  },
  {
    title: '轮详情',
    key: 'analyses',
    render: (r) =>
      h(
        NSpace,
        { size: 6 },
        () =>
          r.analyses.map((a) =>
            h(
              NTooltip,
              {
                trigger: 'hover',
                placement: 'top',
              },
              {
                trigger: () =>
                  h(
                    NTag,
                    {
                      size: 'small',
                      type: a.error
                        ? 'error'
                        : adviceType(a.operation_advice),
                      bordered: false,
                      round: true,
                    },
                    () => `R${a.round}: ${a.error ? 'ERR' : (a.sentiment_score ?? '-')}`,
                  ),
                default: () => buildAnalysisTip(a),
              },
            ),
          ),
      ),
  },
])
</script>

<template>
  <n-config-provider :locale="zhCN" :date-locale="dateZhCN">
    <div style="max-width: 1200px; margin: 0 auto; padding: 32px 24px">
      <!-- 标题 + 实时日期/钟 -->
      <header style="text-align: center; margin-bottom: 28px">
        <h1 style="margin: 0; font-size: 28px; color: #0f172a">
          📈 四榜交集分析
        </h1>
        <p style="color: #64748b; margin-top: 8px; font-size: 14px">
          东方财富 · 雪球 · 同花顺 · 全市场活跃 四榜求交，剔科创板，三轮 LLM 分析过滤
        </p>
        <!-- ★ 新增：当前日期 + 实时钟（每秒更新） -->
        <div style="
          margin-top: 14px;
          display: inline-flex;
          align-items: center;
          gap: 14px;
          padding: 8px 18px;
          border-radius: 999px;
          background: linear-gradient(135deg, #f1f5f9 0%, #e2e8f0 100%);
          box-shadow: inset 0 0 0 1px rgba(148, 163, 184, 0.18);
          font-size: 13px;
          color: #475569;
        ">
          <span style="display: inline-flex; align-items: center; gap: 6px">
            <span style="font-size: 14px">📅</span>
            {{ formatDate(now) }} · {{ WEEK_LABELS[now.getDay()] }}
          </span>
          <span style="
            width: 1px;
            height: 14px;
            background: linear-gradient(to bottom, transparent, #cbd5e1, transparent);
          "></span>
          <span style="
            display: inline-flex;
            align-items: center;
            gap: 6px;
            font-family: 'SF Mono', Menlo, Consolas, monospace;
            font-variant-numeric: tabular-nums;
            font-weight: 600;
            color: #0f172a;
            letter-spacing: 0.5px;
          ">
            <span style="font-size: 14px">🕐</span>
            {{ formatTime(now) }}
          </span>
        </div>
      </header>

      <!-- 操作栏：核心按钮 + 辅助按钮 -->
      <n-card
        :bordered="false"
        style="border-radius: 14px; box-shadow: 0 1px 3px rgba(0, 0, 0, 0.06)"
      >
        <!-- ★ 新增：最长任务时间选择（按钮上方，配置区） -->
        <div
          style="
            display: flex;
            align-items: center;
            gap: 12px;
            padding-bottom: 12px;
            margin-bottom: 12px;
            border-bottom: 1px dashed #e2e8f0;
            flex-wrap: wrap;
          "
        >
          <n-text style="font-size: 14px; color: #334155">
            ⏱ 最长任务时间：
          </n-text>
          <n-radio-group
            v-model:value="selectedMaxRuntime"
            :disabled="loading"
            size="medium"
          >
            <n-radio
              v-for="opt in MAX_RUNTIME_OPTIONS"
              :key="opt"
              :value="opt"
              :label="`${opt} 分钟`"
            />
          </n-radio-group>
          <n-text depth="3" style="font-size: 12px; margin-left: auto">
            （LLM 单次 ~5-15s；越多只越慢）
          </n-text>
        </div>

        <!-- ★ 新增：最大分析股票数（控制 LLM 调用次数） -->
        <div
          style="
            display: flex;
            align-items: center;
            gap: 12px;
            padding-bottom: 14px;
            margin-bottom: 14px;
            border-bottom: 1px solid #e2e8f0;
            flex-wrap: wrap;
          "
        >
          <n-text style="font-size: 14px; color: #334155">
            📊 分析数量：
          </n-text>
          <n-radio-group
            v-model:value="selectedMaxIntersection"
            :disabled="loading"
            size="medium"
          >
            <n-radio
              v-for="opt in INTERSECTION_OPTIONS"
              :key="opt"
              :value="opt"
              :label="`最多 ${opt} 只`"
            />
          </n-radio-group>
          <n-text depth="3" style="font-size: 12px; margin-left: auto">
            （10 只 ≈ 30 次 LLM / 100 只 ≈ 300 次 / ~1.5 小时上限）
          </n-text>
        </div>

        <n-space justify="space-between" align="center" wrap>
          <n-space align="center">
            <n-button
              type="primary"
              size="large"
              :loading="phase === 'previewing' || phase === 'analyzing'"
              :disabled="loading || phase === 'previewing' || phase === 'analyzing'"
              @click="onRun"
              style="
                min-width: 180px;
                height: 52px;
                font-size: 18px;
                font-weight: 600;
                border-radius: 12px;
              "
            >
              <template #icon>🚀</template>
              {{
                phase === 'previewing' ? '抓榜中…' :
                phase === 'previewed' ? '已预览' :
                phase === 'analyzing' ? '分析中…' :
                phase === 'done' ? '重新分析' :
                '开始分析'
              }}
            </n-button>
            <n-text depth="3">{{ progressTip }}</n-text>
          </n-space>

          <n-space v-if="result">
            <n-tooltip>
              <template #trigger>
                <n-button @click="onCopy" :type="copyOk ? 'success' : 'default'">
                  {{ copyOk ? '✓ 已复制' : '复制文本' }}
                </n-button>
              </template>
              复制为可粘贴的纯文本（含每只股票的三轮详情）
            </n-tooltip>
            <n-tooltip>
              <template #trigger>
                <n-button @click="onExport" :type="exportOk ? 'success' : 'default'">
                  {{ exportOk ? '✓ 已导出' : '导出 .txt' }}
                </n-button>
              </template>
              下载为 UTF-8 文本文件
            </n-tooltip>
          </n-space>
        </n-space>

        <!-- 加载中显示进度 -->
        <div v-if="loading" style="margin-top: 18px; text-align: center">
          <n-spin size="large" />
          <div style="margin-top: 10px; color: #475569; font-size: 13px">
            已等待 {{ elapsedSec }} 秒 · 长任务（最多 {{ selectedMaxRuntime }} 分钟），请勿关闭页面
          </div>
        </div>
      </n-card>

      <!-- 错误提示 -->
      <n-alert
        v-if="error"
        type="error"
        :title="'分析失败'"
        style="margin-top: 18px"
        :show-icon="true"
      >
        {{ error }}
      </n-alert>

      <!-- ★ 新增：阶段 1 预览结果（让用户看到数量和预估时间，再确认开始） -->
      <n-card
        v-if="phase === 'previewed' && preview"
        style="
          margin-top: 18px;
          border-radius: 14px;
          background: linear-gradient(135deg, #fef3c7 0%, #fde68a 100%);
          box-shadow: 0 1px 3px rgba(0, 0, 0, 0.08);
        "
      >
        <div style="display: flex; align-items: center; flex-wrap: wrap; gap: 16px">
          <div style="font-size: 28px">📋</div>
          <div style="flex: 1; min-width: 240px">
            <div style="font-size: 16px; font-weight: 600; color: #78350f">
              阶段 1 完成：抓取 + 交集计算
            </div>
            <div style="margin-top: 8px; display: flex; flex-wrap: wrap; gap: 18px; font-size: 14px; color: #422006">
              <span>
                <strong style="color: #b45309">{{ preview.raw_intersection_count }}</strong>
                只股票在四榜共同出现
              </span>
              <span>
                剔除 688* 后剩
                <strong style="color: #b45309">{{ preview.filtered_intersection_count }}</strong>
                只
              </span>
              <span>
                实际将分析
                <strong style="color: #b91c1c; font-size: 16px">{{ preview.target_count }}</strong>
                只
              </span>
            </div>
            <div style="margin-top: 10px; font-size: 14px; color: #422006">
              ⏱ <strong>预估耗时 {{ preview.estimated_human }}</strong>
              （基于 LLM 平均 ~15s/调用 × {{ preview.target_count }} 只 × {{ preview.rounds }} 轮）
            </div>
          </div>
          <n-space vertical>
            <n-button
              type="primary"
              size="large"
              @click="onConfirmAnalysis"
              style="min-width: 130px"
            >
              ✓ 确认开始
            </n-button>
            <n-button
              size="small"
              @click="onCancelPreview"
            >
              取消
            </n-button>
          </n-space>
        </div>
      </n-card>

      <!-- ★ 新增：榜单抓取状态行（结果存在时） -->
      <n-card
        v-if="result"
        size="small"
        style="margin-top: 18px; border-radius: 12px; background: #f8fafc"
      >
        <div style="display: flex; align-items: center; flex-wrap: wrap; gap: 14px">
          <n-text depth="2" style="font-size: 13px">本次抓取:</n-text>
          <n-space>
            <template
              v-for="src in ['东方财富', '雪球', '同花顺']"
              :key="src"
            >
              <n-tooltip
                v-if="result.hot_lists_notes?.[src]"
                placement="top"
              >
                <template #trigger>
                  <n-tag
                    :style="{
                      background: statusType(result.hot_lists_status?.[src]).color,
                      color: 'white',
                      border: 'none',
                      cursor: 'help',
                    }"
                    size="small"
                    round
                  >
                    {{ statusType(result.hot_lists_status?.[src]).icon }}
                    {{ src }}
                    ({{ result.hot_lists_summary[src] ?? 0 }})
                  </n-tag>
                </template>
                {{ result.hot_lists_notes[src] }}
              </n-tooltip>
              <n-tag
                v-else
                :style="{
                  background: statusType(result.hot_lists_status?.[src]).color,
                  color: 'white',
                  border: 'none',
                }"
                size="small"
                round
              >
                {{ statusType(result.hot_lists_status?.[src]).icon }}
                {{ src }}
                ({{ result.hot_lists_summary[src] ?? 0 }})
              </n-tag>
            </template>
          </n-space>
          <n-text v-if="failedSources.length" depth="3" style="font-size: 12px">
            · 实际有效榜单 {{ Object.values(result.hot_lists_status ?? {}).filter(s => s !== 'failed').length }} 个
          </n-text>
        </div>
        <n-alert
          v-if="failedSources.length"
          type="warning"
          :show-icon="true"
          style="margin-top: 12px; margin-bottom: 0"
          size="small"
        >
          以下榜单本次获取失败: <strong>{{ failedSources.join('、') }}</strong>。
          已自动降级到剩余 <strong>{{ Object.values(result.hot_lists_status ?? {}).filter(s => s !== 'failed').length }}</strong> 个榜单求交集。
        </n-alert>
      </n-card>

      <!-- 摘要统计（结果存在时） -->
      <n-grid
        v-if="result && result.status !== 'failed'"
        :cols="4"
        :x-gap="14"
        :y-gap="14"
        responsive="screen"
        style="margin-top: 20px"
      >
        <n-gi>
          <n-card size="small">
            <n-statistic label="东方财富 / 雪球 / 同花顺">
              {{ (result.hot_lists_summary['东方财富'] ?? 0) }} /
              {{ (result.hot_lists_summary['雪球'] ?? 0) }} /
              {{ (result.hot_lists_summary['同花顺'] ?? 0) }}
            </n-statistic>
          </n-card>
        </n-gi>
        <n-gi>
          <n-card size="small">
            <n-statistic label="四榜交集 / 剔688后">
              {{ result.raw_intersection_count }} / {{ result.filtered_intersection_count }}
            </n-statistic>
          </n-card>
        </n-gi>
        <n-gi>
          <n-card size="small">
            <n-statistic label="已分析 / 通过过滤">
              {{ result.analyzed_stock_count }} / {{ result.passed_stock_count }}
            </n-statistic>
          </n-card>
        </n-gi>
        <n-gi>
          <n-card size="small">
            <n-statistic label="总耗时" :precision="2">
              {{ result.duration_seconds }}s
            </n-statistic>
          </n-card>
        </n-gi>
      </n-grid>

      <!-- 结果区域 -->
      <n-card
        v-if="result"
        :bordered="false"
        style="
          margin-top: 20px;
          border-radius: 14px;
          box-shadow: 0 1px 3px rgba(0, 0, 0, 0.06);
        "
      >
        <!-- 空态 -->
        <n-empty
          v-if="result.passed_stock_count === 0"
          description="暂未发现符合条件的股票"
          size="large"
          style="padding: 48px 0"
        >
          <template #extra>
            <n-text depth="3">
              建议调整：增大 max_intersection、关闭剔除科创板、提高轮间间隔
            </n-text>
          </template>
        </n-empty>

        <!-- 表格 -->
        <n-data-table
          v-else
          :columns="columns"
          :data="sortedResults"
          :row-key="(r) => r.code"
          :pagination="false"
          size="medium"
          style="margin-top: 4px"
        />
      </n-card>
    </div>
  </n-config-provider>
</template>

<style scoped>
header h1 {
  letter-spacing: 0.5px;
}
</style>
