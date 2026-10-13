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
import { NCard, NSelect, NButton, NSpace, NSpin, NTag, NInput, NInputNumber, NAlert } from 'naive-ui'
import { AxiosError } from 'axios'
// ★ 修复(H2)：复用 hotAnalysis.ts 导出的 v2Http（带统一错误拦截器），
//   旧实现本地 axios.create({baseURL:'/api/v2'}) 无拦截器 → 401/429/5xx 显示原文。
import { v2Http } from '../api/hotAnalysis'

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
// ★ 功能升级(L8)：mock 数据提示 —— 后端 /data/kline 返回 is_mock 时展示"合成数据"标签
const isMockData = ref(false)
const chartContainer = ref<HTMLDivElement | null>(null)

let echartsMod: unknown = null
// ECharts 实例类型（松散类型，因为 ECharts 类型导出较繁琐）
interface EChartInstance {
  setOption: (opt: unknown) => void
  clear: () => void
  resize: () => void
  dispose: () => void
}
let chart: EChartInstance | null = null
let resizeHandler: (() => void) | null = null

// ★ Round 19 修复(M8)：请求序号守卫——快速切换图表类型时丢弃过期响应，
//   避免先启动但后完成的请求覆盖当前选中类型的图表
let requestSeq = 0
function _isStale(seq: number): boolean {
  return seq !== requestSeq || isUnmounted.value
}

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
  const seq = ++requestSeq
  await loadEcharts()
  loading.value = true
  errorMsg.value = null
  try {
    const r = await v2Http.get('/data/kline', {
      params: { code: symbol.value, days: days.value, period: period.value },
    })
    const kline: KlinePoint[] = r.data.kline || []
    if (_isStale(seq)) return
    // ★ 功能升级(L8)：mock 数据提示（后端 /data/kline 返回 is_mock 时展示"合成数据"标签）
    //   放在 stale 检查之后，避免过期请求覆盖新图表状态的 mock 标记。
    isMockData.value = r.data.is_mock === true
    if (!kline.length) {
      errorMsg.value = `${symbol.value} 无 K 线数据`
      return
    }
    if (!chart) return
    // ★ 迭代1 修复(ECharts merge 残留)：切换图表类型（K线→热力图→资金流向）时，
    //   setOption 默认是 merge 模式，旧的 series（如 K线/成交量 的 legend 与 candlestick）
    //   会残留在新图上，导致热力图只显示标题、矩形区域被旧配置覆盖而空白。
    //   每次加载前 clear() 清除旧 option，再 setOption 全新配置。
    chart.clear()
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
  const seq = ++requestSeq
  await loadEcharts()
  loading.value = true
  errorMsg.value = null
  try {
    const r = await v2Http.get('/community/hot-list', {
      params: { limit: 30 },
    })
    const items: HotItem[] = r.data.items || []
    if (_isStale(seq)) return
    if (!items.length) {
      errorMsg.value = '热力图暂无数据（每日榜单需 cron 刷新）'
      return
    }
    if (!chart) return
    // ★ 迭代1 修复(ECharts merge 残留)：切换图表类型前清除旧 option（见 loadKline 注释）
    chart.clear()
    // ★ 迭代1 修复(ECharts 6 treemap 破坏性变更)：
    //   ECharts 6 中 treemap 的 levels[].itemStyle.color 函数回调不再生效，
    //   实测 levels 方式渲染矩形全空白（只有标题）。改为在 data 项上直接内联
    //   itemStyle.color（红涨 #ef232a / 绿跌 #14b143，符合 A 股习惯）。
    const data = items.map((it) => ({
      name: it.symbol,
      value: Math.abs(it.change_pct || 0) || 0.01, // 0 值给极小值兜底，避免 treemap 无法计算面积
      change_pct: it.change_pct,
      itemStyle: {
        color: (it.change_pct || 0) >= 0 ? '#ef232a' : '#14b143',
      },
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
  const seq = ++requestSeq
  await loadEcharts()
  loading.value = true
  errorMsg.value = null
  try {
    const r = await v2Http.get('/data/quote', {
      params: { code: symbol.value },
    })
    const quote: Quote = r.data
    if (_isStale(seq)) return
    if (!quote || typeof quote.price !== 'number') {
      errorMsg.value = `${symbol.value} 无行情数据`
      return
    }
    if (!chart) return
    // ★ 迭代1 修复(ECharts merge 残留)：切换图表类型前清除旧 option（见 loadKline 注释）
    chart.clear()
    // ★ Round 19 修复(M10)：真实资金流数据源不可用，明确标注为「估算」，
    //   避免把成交额按 3/7 硬拆的假数据展示成真实主力/散户流向误导用户
    const main = Math.max(0, (quote.amount || 0) * 0.3)
    const retail = Math.max(0, (quote.amount || 0) * 0.7)
    // ★ 迭代1 修复(ECharts 6 donut 布局)：环形图默认 center 偏下且无数值标签，
    //   实测图表被容器底部截断。显式设置 center 上移 + label 显示百分比金额。
    chart.setOption({
      title: {
        text: `${quote.name || symbol.value} 资金流向（估算）`,
        subtext: '⚠ 数据源不含主力/散户净流入，本图为按成交额估算的示意图',
        left: 'center',
        top: 10,
        subtextStyle: { color: '#f59e0b', fontSize: 11 },
      },
      tooltip: {
        trigger: 'item',
        formatter: (p: { name: string; value: number }) =>
          `${p.name}<br/>${(p.value / 100000000).toFixed(2)} 亿`,
      },
      legend: { orient: 'vertical', left: 'left', top: 'middle' },
      series: [
        {
          type: 'pie',
          radius: ['38%', '62%'],
          center: ['55%', '52%'],
          avoidLabelOverlap: true,
          itemStyle: { borderRadius: 6, borderColor: '#fff', borderWidth: 2 },
          label: {
            show: true,
            formatter: (p: { name: string; percent: number; value: number }) =>
              `${p.name}\n${(p.value / 100000000).toFixed(2)} 亿 (${p.percent.toFixed(0)}%)`,
          },
          data: [
            { name: '主力（估算）', value: main },
            { name: '散户（估算）', value: retail },
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
        <!-- ★ 功能升级(L8)：K 线模式显示天数输入 + 回车触发刷新 -->
        <NInputNumber
          v-if="vizType === 'kline'"
          v-model:value="days"
          :min="10"
          :max="1000"
          :step="30"
          aria-label="K 线天数"
          style="width: 100px"
          @keydown.enter="loadViz"
        />
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
        <!-- ★ 功能升级(L8)：mock 数据提示（后端 /data/kline 会返回 is_mock） -->
        <NTag v-if="isMockData" type="warning" :bordered="false" aria-label="当前为合成数据">⚠ 合成数据</NTag>
      </NSpace>
      <NAlert v-if="errorMsg" type="warning" :show-icon="true" closable @close="errorMsg = null" role="alert">
        {{ errorMsg }}
      </NAlert>
      <div ref="chartContainer" style="width: 100%; height: 500px" role="img" aria-label="数据图表区域" />
    </NSpace>
  </NCard>
</template>
