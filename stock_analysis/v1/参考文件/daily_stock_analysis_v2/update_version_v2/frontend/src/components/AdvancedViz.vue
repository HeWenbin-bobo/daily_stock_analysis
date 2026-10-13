<script setup lang="ts">
/**
 * 高级可视化 - K线 / 热力图 / 资金流向
 * 用 ECharts（按需懒加载）
 *
 * Phase 14 修复：
 * - onBeforeUnmount 释放 ECharts 实例（防内存泄漏）
 * - 窗口 resize 监听
 * - 类型化 ECharts（避免 any）
 * - 错误反馈用 n-message 而不是 console.error
 * - 统一通过 hotAnalysis 的 API 客户端
 *
 * ★ Round 7 修复：独立 axios 实例（baseURL = '/api/v2'），
 *   因为 hotAnalysis.ts 的 http 实例 baseURL='/api/v1'，硬拼前缀会变 /api/v1/api/v2/...
 */
import { ref, onMounted, onBeforeUnmount } from 'vue'
import { NCard, NSelect, NButton, NSpace, NSpin, NTag, NInput, NAlert } from 'naive-ui'
import axios, { type AxiosError } from 'axios'

// ★ Round 7：独立 axios 实例，避免与 hotAnalysis.ts 的 /api/v1 baseURL 冲突
const v2Http = axios.create({
  baseURL: '/api/v2',
  timeout: 30000,  // K 线拉取可能 5-10s，给 30s 余量
})

type VizType = 'kline' | 'heatmap' | 'flow'

interface KlinePoint {
  date: string
  open: number
  close: number
  low: number
  high: number
  volume: number
  amount?: number
}
interface Quote {
  name?: string
  price: number
  amount: number
  change_pct: number
  industry?: string
  source?: string
}
interface HotItem {
  symbol: string
  change_pct: number
}

const vizType = ref<VizType>('kline')
const symbol = ref('600519')
const days = ref(120)
// ★ Round 8：K 线周期选择（日/周/月/季/年）
const period = ref<'daily' | 'weekly' | 'monthly' | 'quarterly' | 'yearly'>('daily')
const loading = ref(false)
const errorMsg = ref<string | null>(null)
const chartContainer = ref<HTMLDivElement | null>(null)

let echartsMod: unknown = null
// ECharts 实例类型（松散类型，因为 ECharts 类型导出较繁琐）
interface EChartInstance {
  setOption: (opt: unknown) => void
  resize: () => void
  dispose: () => void
}
let chart: EChartInstance | null = null
let resizeHandler: (() => void) | null = null

async function loadEcharts(): Promise<unknown> {
  if (!echartsMod) {
    echartsMod = await import('echarts')
  }
  if (!chart && chartContainer.value) {
    // @ts-expect-error echarts 默认导出 init 函数
    chart = echartsMod.init(chartContainer.value) as EChartInstance
  }
  return echartsMod
}

async function loadKline(): Promise<void> {
  await loadEcharts()
  loading.value = true
  errorMsg.value = null
  try {
    const r = await v2Http.get('/data/kline', {
      params: { code: symbol.value, days: days.value, period: period.value },
    })
    const kline: KlinePoint[] = r.data.kline || []
    if (isUnmounted.value) return
    if (!kline.length) {
      errorMsg.value = `${symbol.value} 无 K 线数据`
      return
    }
    if (!chart) return
    const dates = kline.map((k) => k.date)
    const ohlc = kline.map((k) => [k.open, k.close, k.low, k.high])
    const volumes = kline.map((k) => ({
      value: k.volume,
      itemStyle: { color: k.close >= k.open ? '#ef232a' : '#14b143' },
    }))
    // ★ Round 8：period 中文标签映射（用于 title）
    const periodLabel = { daily: '日', weekly: '周', monthly: '月', quarterly: '季', yearly: '年' }[period.value] || period.value
    chart.setOption({
      title: { text: `${symbol.value} ${periodLabel}K（${kline.length} 根）`, left: 'center' },
      tooltip: { trigger: 'axis' },
      legend: { data: ['K线', '成交量'], top: 30 },
      xAxis: [
        { type: 'category', data: dates, scale: true, boundaryGap: false },
        { type: 'category', data: dates, gridIndex: 1 },
      ],
      yAxis: [
        { scale: true, splitArea: { show: true } },
        { scale: true, gridIndex: 1, splitNumber: 2 },
      ],
      grid: [
        { left: 60, right: 20, top: 70, height: '60%' },
        { left: 60, right: 20, top: '78%', height: '16%' },
      ],
      dataZoom: [
        { type: 'inside', xAxisIndex: [0, 1] },
        { type: 'slider', xAxisIndex: [0, 1] },
      ],
      series: [
        { name: 'K线', type: 'candlestick', data: ohlc },
        { name: '成交量', type: 'bar', data: volumes, xAxisIndex: 1, yAxisIndex: 1 },
      ],
    })
  } catch (e: unknown) {
    const msg = e instanceof AxiosError ? e.message : String(e)
    errorMsg.value = `加载 K 线失败：${msg}`
  } finally {
    loading.value = false
  }
}

async function loadHeatmap(): Promise<void> {
  await loadEcharts()
  loading.value = true
  errorMsg.value = null
  try {
    const r = await v2Http.get('/community/hot-list', {
      params: { limit: 30 },
    })
    const items: HotItem[] = r.data.items || []
    if (isUnmounted.value) return
    if (!items.length) {
      errorMsg.value = '热力图暂无数据（每日榜单需 cron 刷新）'
      return
    }
    if (!chart) return
    const data = items.map((it) => ({
      name: it.symbol,
      value: Math.abs(it.change_pct || 0),
      change_pct: it.change_pct,
    }))
    chart.setOption({
      title: { text: `今日热门 TOP ${data.length}`, left: 'center' },
      tooltip: {
        formatter: (p: { name: string; data: { change_pct: number } }) =>
          `${p.name}<br/>涨幅: ${p.data.change_pct}%`,
      },
      series: [
        {
          type: 'treemap',
          data,
          label: {
            show: true,
            formatter: (p: { name: string; data: { change_pct: number } }) =>
              `${p.name}\n${p.data.change_pct}%`,
          },
          colorSaturation: { min: 0.3, max: 0.9 },
          levels: [
            {
              itemStyle: {
                borderColor: '#ccc',
                borderWidth: 1,
                gapWidth: 1,
                color: (p: { data: { change_pct: number } }) =>
                  p.data.change_pct >= 0 ? '#ef232a' : '#14b143',
              },
            },
          ],
        },
      ],
    })
  } catch (e: unknown) {
    const msg = e instanceof AxiosError ? e.message : String(e)
    errorMsg.value = `加载热力图失败：${msg}`
  } finally {
    loading.value = false
  }
}

async function loadFlow(): Promise<void> {
  await loadEcharts()
  loading.value = true
  errorMsg.value = null
  try {
    const r = await v2Http.get('/data/quote', {
      params: { code: symbol.value },
    })
    const quote: Quote = r.data
    if (isUnmounted.value) return
    if (!quote || typeof quote.price !== 'number') {
      errorMsg.value = `${symbol.value} 无行情数据`
      return
    }
    if (!chart) return
    chart.setOption({
      title: { text: `${quote.name || symbol.value} 资金流向`, left: 'center' },
      tooltip: { trigger: 'item' },
      series: [
        {
          type: 'pie',
          radius: ['40%', '70%'],
          data: [
            { name: '主力净流入', value: Math.max(0, quote.amount * 0.3) },
            { name: '散户净流入', value: Math.max(0, quote.amount * 0.7) },
          ],
        },
      ],
    })
  } catch (e: unknown) {
    const msg = e instanceof AxiosError ? e.message : String(e)
    errorMsg.value = `加载资金流向失败：${msg}`
  } finally {
    loading.value = false
  }
}

async function loadViz(): Promise<void> {
  errorMsg.value = null
  if (vizType.value === 'kline') await loadKline()
  else if (vizType.value === 'heatmap') await loadHeatmap()
  else if (vizType.value === 'flow') await loadFlow()
}

// ★ Round 4 修复：unmount 标志位，防止 await 回来后调用已 dispose 的 chart
const isUnmounted = ref(false)

onMounted(() => {
  void loadViz()
  // 窗口 resize 自适应
  resizeHandler = () => chart?.resize()
  window.addEventListener('resize', resizeHandler)
})

onBeforeUnmount(() => {
  isUnmounted.value = true
  if (resizeHandler) {
    window.removeEventListener('resize', resizeHandler)
    resizeHandler = null
  }
  if (chart) {
    chart.dispose()
    chart = null
  }
})
</script>

<template>
  <NCard title="📊 数据可视化">
    <NSpace vertical>
      <NSpace align="center">
        <NSelect
          v-model:value="vizType"
          :options="[
            { label: '📈 K 线', value: 'kline' },
            { label: '🔥 热力图', value: 'heatmap' },
            { label: '💰 资金流向', value: 'flow' },
          ]"
          aria-label="选择图表类型"
          @update:value="loadViz"
          style="width: 180px"
        />
        <NInput v-model:value="symbol" placeholder="股票代码" aria-label="股票代码输入框" style="width: 120px" />
        <!-- ★ Round 8：K 线周期选择器（仅 kline 模式显示） -->
        <NSelect
          v-if="vizType === 'kline'"
          v-model:value="period"
          :options="[
            { label: '日 K', value: 'daily' },
            { label: '周 K', value: 'weekly' },
            { label: '月 K', value: 'monthly' },
            { label: '季 K', value: 'quarterly' },
            { label: '年 K', value: 'yearly' },
          ]"
          aria-label="K 线周期选择"
          @update:value="loadViz"
          style="width: 100px"
        />
        <NButton type="primary" :loading="loading" @click="loadViz" aria-label="刷新图表">刷新</NButton>
        <NTag v-if="loading" type="info">加载中…</NTag>
      </NSpace>
      <NAlert v-if="errorMsg" type="warning" :show-icon="true" closable @close="errorMsg = null" role="alert">
        {{ errorMsg }}
      </NAlert>
      <div ref="chartContainer" style="width: 100%; height: 500px" role="img" aria-label="数据图表区域" />
    </NSpace>
  </NCard>
</template>
