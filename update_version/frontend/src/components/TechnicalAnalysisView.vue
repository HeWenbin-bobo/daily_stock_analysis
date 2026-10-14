<script setup lang="ts">
/**
 * 技术分析视图 —— 从 stock_analysis/v1 移植
 *
 * 功能：K 线 + 波峰波谷标记 + 技术指标（MACD/BOLL/KDJ）+ AI 解读 + CSV/JSON 导出
 * 数据来源：/api/v2/technical/*（后端复用 data_source 的 akshare → baostock → mock）
 */
import { ref, onMounted, onBeforeUnmount } from 'vue'
import {
  NCard, NInput, NButton, NSpace, NText, NTag, NAlert, NSpin, NSelect,
  NDataTable, NGrid, NGi, NEmpty, NInputNumber,
} from 'naive-ui'
import axios, { AxiosError } from 'axios'
import {
  analyzePeakTrough,
  getIndicator,
  getLLMInsight,
  v2Http,
  type PeakTroughResponse,
  type PeakTroughSegment,
  type IndicatorResponse,
} from '../api/hotAnalysis'

interface KlinePoint {
  date: string
  open: number
  close: number
  low: number
  high: number
  volume: number
  amount?: number
}

interface EChartInstance {
  setOption: (opt: unknown) => void
  resize: () => void
  dispose: () => void
  clear: () => void
}

const PERIOD_OPTIONS = [
  { label: '日 K', value: 'daily' },
  { label: '周 K', value: 'weekly' },
  { label: '月 K', value: 'monthly' },
  { label: '季 K', value: 'quarterly' },
  { label: '年 K', value: 'yearly' },
]

const INDICATOR_OPTIONS = [
  { label: 'MACD', value: 'MACD' },
  { label: 'BOLL', value: 'BOLL' },
  { label: 'KDJ', value: 'KDJ' },
]

const code = ref('600519')
const period = ref<'daily' | 'weekly' | 'monthly' | 'quarterly' | 'yearly'>('daily')
const days = ref(365)
const windowSize = ref(3)
const indicatorType = ref<'MACD' | 'BOLL' | 'KDJ'>('MACD')

const loading = ref(false)
const indicatorLoading = ref(false)
const insightLoading = ref(false)
const errorMsg = ref<string | null>(null)
const indicatorError = ref<string | null>(null)

const kline = ref<KlinePoint[]>([])
const peakTrough = ref<PeakTroughResponse | null>(null)
const indicator = ref<IndicatorResponse | null>(null)
const insight = ref<string | null>(null)

const isMock = ref(false)

// ECharts 实例
const chartContainer = ref<HTMLDivElement | null>(null)
const indicatorContainer = ref<HTMLDivElement | null>(null)
let echartsMod: unknown = null
let chart: EChartInstance | null = null
let indicatorChart: EChartInstance | null = null
let resizeHandler: (() => void) | null = null

const isUnmounted = ref(false)
let requestSeq = 0
function _isStale(seq: number): boolean {
  return seq !== requestSeq || isUnmounted.value
}

async function loadEcharts(): Promise<unknown> {
  if (!echartsMod) echartsMod = await import('echarts')
  return echartsMod
}

async function ensureChart(): Promise<EChartInstance | null> {
  const mod = await loadEcharts()
  if (!chart && chartContainer.value) {
    // @ts-expect-error echarts 默认导出 init
    chart = mod.init(chartContainer.value) as EChartInstance
  }
  return chart
}

async function ensureIndicatorChart(): Promise<EChartInstance | null> {
  const mod = await loadEcharts()
  if (!indicatorChart && indicatorContainer.value) {
    // @ts-expect-error echarts 默认导出 init
    indicatorChart = mod.init(indicatorContainer.value) as EChartInstance
  }
  return indicatorChart
}

/** 主分析：拉 K 线 + 波峰波谷 + 指标，并渲染 */
async function runAnalysis(): Promise<void> {
  const seq = ++requestSeq
  loading.value = true
  errorMsg.value = null
  indicatorError.value = null
  insight.value = null
  peakTrough.value = null
  indicator.value = null
  isMock.value = false

  try {
    // 1) K 线
    const kresp = await v2Http.get('/data/kline', {
      params: { code: code.value, days: days.value, period: period.value },
    })
    const k: KlinePoint[] = kresp.data.kline || []
    if (_isStale(seq)) return
    if (!k.length) {
      errorMsg.value = `${code.value} 无 K 线数据`
      return
    }
    kline.value = k
    if (kresp.data.is_mock) isMock.value = true

    // 2) 波峰波谷
    const pt = await analyzePeakTrough({
      code: code.value,
      period: period.value,
      days: days.value,
      window: windowSize.value,
    })
    if (_isStale(seq)) return
    peakTrough.value = pt
    if (pt.is_mock) isMock.value = true
    renderKline()

    // 3) 指标
    await loadIndicator()
  } catch (e: unknown) {
    if (_isStale(seq)) return
    const msg = e instanceof AxiosError ? e.message : String(e)
    errorMsg.value = `分析失败：${msg}`
  } finally {
    loading.value = false
  }
}

function renderKline(): void {
  void ensureChart().then((c) => {
    if (!c || !kline.value.length) return
    const dates = kline.value.map((k) => k.date)
    const ohlc = kline.value.map((k) => [k.open, k.close, k.low, k.high])
    const volumes = kline.value.map((k) => ({
      value: k.volume,
      itemStyle: { color: k.close >= k.open ? '#dc2626' : '#16a34a' },
    }))

    const pt = peakTrough.value
    const marks: Array<{ coord: [string, number]; name: string; value: string; itemStyle: { color: string } }> = []
    const markLines: Array<{ yAxis: number; name: string; lineStyle: { color: string; type: string } }> = []

    if (pt) {
      markLines.push({
        yAxis: pt.year_high.price,
        name: '年度高点',
        lineStyle: { color: '#d97706', type: 'dashed' },
      })
      const seen = new Set<string>()
      for (const seg of pt.segments) {
        for (const [date, price, kind] of [
          [seg.from_date, seg.start_price, 'start'],
          [seg.to_date, seg.end_price, 'end'],
        ] as Array<[string, number, string]>) {
          const key = `${date}-${kind}`
          if (seen.has(key)) continue
          seen.add(key)
          const isHigh = kind === 'start' ? seg.type === 'decline' : seg.type === 'rebound'
          marks.push({
            coord: [date, price],
            name: seg.type === 'decline' ? '波峰' : '波谷',
            value: seg.type,
            itemStyle: { color: isHigh ? '#dc2626' : '#16a34a' },
          })
        }
      }
    }

    c.setOption({
      title: { text: `${code.value} ${pt?.stock_name || ''} K线 · 波峰波谷`, left: 'center', textStyle: { fontSize: 14 } },
      tooltip: { trigger: 'axis' },
      legend: { data: ['K线', '成交量'], top: 28 },
      xAxis: [
        { type: 'category', data: dates, scale: true, boundaryGap: false },
        { type: 'category', data: dates, gridIndex: 1 },
      ],
      yAxis: [
        { scale: true, splitArea: { show: true } },
        { scale: true, gridIndex: 1, splitNumber: 2 },
      ],
      grid: [
        { left: 60, right: 20, top: 70, height: '58%' },
        { left: 60, right: 20, top: '80%', height: '14%' },
      ],
      dataZoom: [
        { type: 'inside', xAxisIndex: [0, 1] },
        { type: 'slider', xAxisIndex: [0, 1] },
      ],
      series: [
        {
          name: 'K线',
          type: 'candlestick',
          data: ohlc,
          markLine: { symbol: 'none', data: markLines, label: { formatter: '{b}' } },
          markPoint: { symbol: 'pin', symbolSize: 36, data: marks, label: { fontSize: 9 } },
        },
        { name: '成交量', type: 'bar', data: volumes, xAxisIndex: 1, yAxisIndex: 1 },
      ],
    })
  })
}

async function loadIndicator(): Promise<void> {
  // ★ 修复(C2)：旧实现 const seq = requestSeq（不递增）→ 与 runAnalysis 内的指标请求
  //   共享同一个 seq，两个并发请求都认为"新鲜"，后完成的覆盖先完成的（stale 指标可展示）。
  //   改为 ++requestSeq，与 runAnalysis 一致，每次指标请求独占代数。
  const seq = ++requestSeq
  indicatorLoading.value = true
  indicatorError.value = null
  try {
    const ind = await getIndicator(code.value, indicatorType.value, period.value, days.value)
    if (_isStale(seq)) return
    indicator.value = ind
    renderIndicator()
  } catch (e: unknown) {
    if (_isStale(seq)) return
    const msg = e instanceof AxiosError ? e.message : String(e)
    indicatorError.value = `指标加载失败：${msg}`
  } finally {
    indicatorLoading.value = false
  }
}

function renderIndicator(): void {
  void ensureIndicatorChart().then((c) => {
    if (!c || !indicator.value) return
    const ind = indicator.value
    const seriesData = ind.series

    if (ind.indicator === 'MACD') {
      const dif = seriesData.dif || []
      const dea = seriesData.dea || []
      const hist = seriesData.hist || []
      const times = dif.map((p) => p.time)
      c.setOption({
        title: { text: 'MACD', left: 'center', textStyle: { fontSize: 13 } },
        tooltip: { trigger: 'axis' },
        legend: { data: ['DIF', 'DEA', 'HIST'], top: 24 },
        xAxis: { type: 'category', data: times },
        yAxis: { scale: true },
        dataZoom: [{ type: 'inside' }, { type: 'slider' }],
        series: [
          { name: 'DIF', type: 'line', data: dif.map((p) => p.value), showSymbol: false },
          { name: 'DEA', type: 'line', data: dea.map((p) => p.value), showSymbol: false },
          {
            name: 'HIST', type: 'bar',
            data: hist.map((p) => ({ value: p.value, itemStyle: { color: p.value >= 0 ? '#dc2626' : '#16a34a' } })),
          },
        ],
      })
    } else if (ind.indicator === 'BOLL') {
      const mid = seriesData.mid || []
      const upper = seriesData.upper || []
      const lower = seriesData.lower || []
      const times = mid.map((p) => p.time)
      c.setOption({
        title: { text: 'BOLL', left: 'center', textStyle: { fontSize: 13 } },
        tooltip: { trigger: 'axis' },
        legend: { data: ['MID', 'UPPER', 'LOWER'], top: 24 },
        xAxis: { type: 'category', data: times },
        yAxis: { scale: true },
        dataZoom: [{ type: 'inside' }, { type: 'slider' }],
        series: [
          { name: 'MID', type: 'line', data: mid.map((p) => p.value), showSymbol: false },
          { name: 'UPPER', type: 'line', data: upper.map((p) => p.value), showSymbol: false, lineStyle: { type: 'dashed' } },
          { name: 'LOWER', type: 'line', data: lower.map((p) => p.value), showSymbol: false, lineStyle: { type: 'dashed' } },
        ],
      })
    } else {
      const k = seriesData.k || []
      const d = seriesData.d || []
      const j = seriesData.j || []
      const times = k.map((p) => p.time)
      c.setOption({
        title: { text: 'KDJ', left: 'center', textStyle: { fontSize: 13 } },
        tooltip: { trigger: 'axis' },
        legend: { data: ['K', 'D', 'J'], top: 24 },
        xAxis: { type: 'category', data: times },
        yAxis: { scale: true },
        dataZoom: [{ type: 'inside' }, { type: 'slider' }],
        series: [
          { name: 'K', type: 'line', data: k.map((p) => p.value), showSymbol: false },
          { name: 'D', type: 'line', data: d.map((p) => p.value), showSymbol: false },
          { name: 'J', type: 'line', data: j.map((p) => p.value), showSymbol: false },
        ],
      })
    }
  })
}

async function runInsight(): Promise<void> {
  // ★ 修复(C3)：旧实现无卸载守卫 + 无竞态守卫——双击/切代码后旧请求响应返回会
  //   写入已卸载的 ref（内存泄漏 + 显示过期股票解读）。加 seq 代数守卫。
  const seq = ++requestSeq
  if (!peakTrough.value) {
    await runAnalysis()
    if (_isStale(seq)) return
    if (!peakTrough.value) return
  }
  insightLoading.value = true
  insight.value = null
  try {
    const r = await getLLMInsight({
      code: code.value,
      period: period.value,
      days: days.value,
      window: windowSize.value,
    })
    if (_isStale(seq)) return
    insight.value = r.text
  } catch (e: unknown) {
    if (_isStale(seq)) return
    const msg = e instanceof AxiosError ? e.message : String(e)
    insight.value = `AI 解读失败：${msg}`
  } finally {
    if (!_isStale(seq)) insightLoading.value = false
  }
}

function download(content: string, filename: string, mime: string): void {
  const blob = new Blob(['﻿' + content], { type: mime })
  const url = URL.createObjectURL(blob)
  try {
    const a = document.createElement('a')
    a.href = url
    a.download = filename
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
  } finally {
    URL.revokeObjectURL(url)
  }
}

function exportCSV(): void {
  if (!peakTrough.value) return
  const pt = peakTrough.value
  const s = pt.summary
  const lines: string[] = [
    '类型,起始日期,结束日期,起始价,结束价,涨跌幅(%)',
    ...pt.segments.map((seg) =>
      [seg.type, seg.from_date, seg.to_date, seg.start_price, seg.end_price, seg.change_pct].join(','),
    ),
    '',
    `年度高点,${pt.year_high.date},${pt.year_high.price}`,
    `当前价,${s.current_price}`,
    `最大回撤(%),${s.max_decline_pct}`,
    `最大回弹(%),${s.max_rebound_pct}`,
    `距年度高点(%),${s.current_vs_high_pct}`,
  ]
  download(lines.join('\r\n'), `peak-trough-${code.value}-${period.value}.csv`, 'text/csv;charset=utf-8')
}

function exportJSON(): void {
  if (!peakTrough.value) return
  download(JSON.stringify(peakTrough.value, null, 2), `peak-trough-${code.value}.json`, 'application/json')
}

const segmentColumns = [
  { title: '类型', key: 'type', width: 80, render: (s: PeakTroughSegment) => (s.type === 'decline' ? '📉 回撤' : '📈 回弹') },
  { title: '起始日期', key: 'from_date', width: 110 },
  { title: '结束日期', key: 'to_date', width: 110 },
  { title: '起始价', key: 'start_price', width: 90 },
  { title: '结束价', key: 'end_price', width: 90 },
  {
    title: '涨跌幅', key: 'change_pct', width: 90,
    render: (s: PeakTroughSegment) => {
      const c = s.change_pct >= 0 ? 'var(--up)' : 'var(--down)'
      return `${s.change_pct >= 0 ? '+' : ''}${s.change_pct.toFixed(2)}%`
    },
  },
]

function fmtPct(v: number): string {
  return `${v >= 0 ? '+' : ''}${v.toFixed(2)}%`
}

onMounted(() => {
  void runAnalysis()
  resizeHandler = () => {
    chart?.resize()
    indicatorChart?.resize()
  }
  window.addEventListener('resize', resizeHandler)
})

onBeforeUnmount(() => {
  isUnmounted.value = true
  if (resizeHandler) {
    window.removeEventListener('resize', resizeHandler)
    resizeHandler = null
  }
  chart?.dispose()
  chart = null
  indicatorChart?.dispose()
  indicatorChart = null
})
</script>

<template>
  <div style="display: flex; flex-direction: column; gap: 16px">
    <!-- 输入区 -->
    <n-card :bordered="false" style="border-radius: 14px; box-shadow: 0 1px 3px rgba(0,0,0,0.06)">
      <n-space align="center" wrap :size="[12, 12]">
        <n-text strong style="font-size: 16px">📐 技术分析</n-text>
        <n-input v-model:value="code" placeholder="股票代码" aria-label="股票代码" style="width: 120px" />
        <n-select v-model:value="period" :options="PERIOD_OPTIONS" aria-label="周期选择" style="width: 100px" />
        <n-input-number v-model:value="days" :min="30" :max="2000" :step="30" aria-label="数据天数" style="width: 120px" />
        <n-input-number v-model:value="windowSize" :min="1" :max="10" :step="1" aria-label="波峰波谷窗口" style="width: 90px" />
        <n-button class="btn-primary-action" :loading="loading" @click="runAnalysis" aria-label="开始技术分析" style="height: 38px">
          🔍 分析
        </n-button>
        <n-button class="btn-export" :disabled="!peakTrough" @click="runInsight" :loading="insightLoading" aria-label="AI 解读" style="height: 38px">
          🤖 AI 解读
        </n-button>
        <n-button class="btn-export" :disabled="!peakTrough" @click="exportCSV" aria-label="导出 CSV" size="small">📊 CSV</n-button>
        <n-button class="btn-export" :disabled="!peakTrough" @click="exportJSON" aria-label="导出 JSON" size="small">📄 JSON</n-button>
      </n-space>
      <n-alert v-if="isMock" type="warning" :show-icon="true" style="margin-top: 12px" closable @close="isMock = false">
        当前为沙箱/离线环境，展示的是合成数据（数据源不可用时的 mock 回退）。
      </n-alert>
    </n-card>

    <n-alert v-if="errorMsg" type="error" :show-icon="true" closable @close="errorMsg = null" role="alert">
      {{ errorMsg }}
    </n-alert>

    <!-- 摘要统计 -->
    <n-grid v-if="peakTrough" :cols="4" :x-gap="14" :y-gap="14" responsive="screen" style="margin-top: 0">
      <n-gi>
        <n-card size="small" style="border-radius: 12px; background: var(--color-elevated); border: 1px solid var(--color-border)">
          <n-text depth="3" style="font-size: 11px; display: block">年度高点</n-text>
          <n-text strong style="font-size: 16px">¥{{ peakTrough.year_high.price.toFixed(2) }}</n-text>
          <n-text depth="3" style="font-size: 11px">{{ peakTrough.year_high.date }}</n-text>
        </n-card>
      </n-gi>
      <n-gi>
        <n-card size="small" style="border-radius: 12px; background: #fef2f2; border: 1px solid rgba(220,38,38,0.15)">
          <n-text depth="3" style="font-size: 11px; display: block">最大回撤</n-text>
          <n-text strong style="font-size: 16px; color: var(--down)">{{ fmtPct(peakTrough.summary.max_decline_pct) }}</n-text>
        </n-card>
      </n-gi>
      <n-gi>
        <n-card size="small" style="border-radius: 12px; background: #f0fdf4; border: 1px solid rgba(22,163,74,0.15)">
          <n-text depth="3" style="font-size: 11px; display: block">最大回弹</n-text>
          <n-text strong style="font-size: 16px; color: var(--up)">{{ fmtPct(peakTrough.summary.max_rebound_pct) }}</n-text>
        </n-card>
      </n-gi>
      <n-gi>
        <n-card size="small" style="border-radius: 12px; background: var(--color-elevated); border: 1px solid var(--color-border)">
          <n-text depth="3" style="font-size: 11px; display: block">当前价 / 距高点</n-text>
          <n-text strong style="font-size: 16px">¥{{ peakTrough.summary.current_price.toFixed(2) }}</n-text>
          <n-text strong style="font-size: 12px; color: var(--warning)">{{ fmtPct(peakTrough.summary.current_vs_high_pct) }}</n-text>
        </n-card>
      </n-gi>
    </n-grid>

    <!-- K 线图 -->
    <n-card v-if="kline.length" :bordered="false" style="border-radius: 14px; box-shadow: 0 1px 3px rgba(0,0,0,0.06)">
      <div ref="chartContainer" style="width: 100%; height: 420px" role="img" aria-label="K线图与波峰波谷标记" />
    </n-card>

    <!-- 指标图 -->
    <n-card :bordered="false" style="border-radius: 14px; box-shadow: 0 1px 3px rgba(0,0,0,0.06)">
      <n-space align="center" wrap style="margin-bottom: 12px">
        <n-text strong style="font-size: 14px">📊 技术指标</n-text>
        <n-select v-model:value="indicatorType" :options="INDICATOR_OPTIONS" aria-label="指标选择" style="width: 110px" @update:value="loadIndicator" />
        <n-spin v-if="indicatorLoading" size="small" />
      </n-space>
      <n-alert v-if="indicatorError" type="warning" :show-icon="true" closable @close="indicatorError = null" style="margin-bottom: 8px">
        {{ indicatorError }}
      </n-alert>
      <div v-if="kline.length" ref="indicatorContainer" style="width: 100%; height: 280px" role="img" aria-label="技术指标图表" />
      <n-empty v-else description="请先分析股票" style="padding: 24px" />
    </n-card>

    <!-- 波峰波谷段 + AI 解读 -->
    <div v-if="peakTrough">
      <n-card :bordered="false" style="border-radius: 14px; box-shadow: 0 1px 3px rgba(0,0,0,0.06); margin-bottom: 16px">
        <template #header>📈 波峰波谷段（{{ peakTrough.segments.length }} 段）</template>
        <n-data-table v-if="peakTrough.segments.length" :columns="segmentColumns" :data="peakTrough.segments" :row-key="(s: PeakTroughSegment) => `${s.from_date}-${s.to_date}`" :pagination="false" size="small" />
        <n-empty v-else description="无波峰波谷段" style="padding: 20px" />
      </n-card>

      <n-card v-if="insight" :bordered="false" style="border-radius: 14px; box-shadow: 0 1px 3px rgba(0,0,0,0.06)">
        <template #header>🤖 AI 解读</template>
        <n-text style="white-space: pre-wrap; line-height: 1.7">{{ insight }}</n-text>
      </n-card>
    </div>
  </div>
</template>
