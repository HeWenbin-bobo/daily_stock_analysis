<script setup lang="ts">
/**
 * 自选股跟踪视图
 */
import { ref, computed, onMounted, h } from 'vue'
import { NCard, NButton, NSpace, NText, NTag, NDataTable, NInput, NEmpty, NPopconfirm, NSpin, NAlert } from 'naive-ui'
import { getWatchlist, addToWatchlist, removeFromWatchlist, getMarketData, type WatchlistItem, type MarketDataItem } from '../api/hotAnalysis'
import { extractErrorMessage } from '../utils/formatters'

const emit = defineEmits<{ (e: 'back'): void }>()

const loading = ref(false)
const items = ref<WatchlistItem[]>([])
const newCode = ref('')
const newName = ref('')
const adding = ref(false)
const error = ref<string | null>(null)
// ★ 功能升级(L1)：自选股行情 map（code -> 行情），接入此前定义但零调用的 getMarketData
const quotesMap = ref<Record<string, MarketDataItem>>({})
const quotesLoading = ref(false)

async function loadQuotes() {
  if (items.value.length === 0) {
    quotesMap.value = {}
    return
  }
  quotesLoading.value = true
  try {
    // /hot-analysis/market-data 限最多 50 只，自选股一般不会超
    const codes = items.value.map((it) => it.code).slice(0, 50)
    const data = await getMarketData(codes)
    // ★ 修复：后端返回的 code 带市场前缀（如 sh600519/sz000858），
    //   但自选股列表用纯数字代码（600519）。用后缀 6 位数字匹配，
    //   否则 quotesMap 查找永远 miss，行情列恒显示 '-'。
    const map: Record<string, MarketDataItem> = {}
    for (const it of data.items) {
      const pureCode = it.code.replace(/^[a-zA-Z]+/, '')
      map[pureCode] = it
    }
    quotesMap.value = map
  } catch {
    // 行情拉取失败不阻塞自选股列表展示，静默降级（表格该列显示 '-'）
    quotesMap.value = {}
  } finally {
    quotesLoading.value = false
  }
}

async function load() {
  loading.value = true
  try {
    const data = await getWatchlist()
    items.value = data.items
    await loadQuotes()
  } catch (e) {
    // ★ round 6 audit：暴露给用户（已有 error ref + 模板 n-alert）
    error.value = extractErrorMessage(e, '加载自选股失败')
  } finally {
    loading.value = false
  }
}

// ★ Round 13：6 位 A 股代码校验
const STOCK_CODE_RE = /^[0-9]{6}$/

async function handleAdd() {
  const code = newCode.value.trim()
  if (!code) return
  if (!STOCK_CODE_RE.test(code)) {
    error.value = '股票代码格式错误：需为 6 位数字'
    return
  }
  adding.value = true
  error.value = null
  try {
    await addToWatchlist(code, newName.value.trim())
    newCode.value = ''
    newName.value = ''
    await load()
  } catch (e: unknown) {
    error.value = extractErrorMessage(e, '添加失败')
  } finally {
    adding.value = false
  }
}

async function handleRemove(code: string) {
  try {
    await removeFromWatchlist(code)
    await load()
  } catch (e: unknown) {
    error.value = extractErrorMessage(e, '移除失败')
  }
}

onMounted(load)

// ★ 修复：columns 若是普通 const 数组，其 render 闭包读取的 quotesMap.value
//   变化不会触发 NDataTable 重渲染（columns 本身不是响应式依赖，Naive UI 不知道
//   要重新渲染单元格）。改为 computed，quotesMap 变化时 columns 引用变化，
//   NDataTable 的 :columns 绑定重新求值触发渲染。
const columns = computed(() => [
  { title: '代码', key: 'code', width: 100, render: (r: WatchlistItem) => r.code },
  { title: '名称', key: 'name', width: 120 },
  // ★ 功能升级(L1)：价格/涨跌幅列，来自 quotesMap（getMarketData 接入）
  {
    title: '现价', key: 'price', width: 90,
    render: (r: WatchlistItem) => {
      const q = quotesMap.value[r.code]
      return q ? `¥${q.price.toFixed(2)}` : '-'
    },
  },
  {
    title: '涨跌幅', key: 'change_pct', width: 90,
    render: (r: WatchlistItem) => {
      const q = quotesMap.value[r.code]
      if (!q) return '-'
      const color = q.change_pct >= 0 ? 'var(--up)' : 'var(--down)'
      return h('span', { style: `color: ${color}; font-weight: 600` }, `${q.change_pct >= 0 ? '+' : ''}${q.change_pct.toFixed(2)}%`)
    },
  },
  { title: '备注', key: 'notes', render: (r: WatchlistItem) => r.notes || '-' },
  { title: '添加时间', key: 'created_at', width: 170 },
  {
    title: '操作', key: 'actions', width: 100,
    render: (r: WatchlistItem) => h(
      NPopconfirm,
      { onPositiveClick: () => handleRemove(r.code) },
      {
        trigger: () => h(NButton, { size: 'tiny', class: 'btn-danger', 'aria-label': `移除自选股 ${r.code}`, style: 'border-radius: 6px' }, () => '🗑 移除'),
        default: () => `确定移除 ${r.code}？`,
      },
    ),
  },
])

// h is imported at the top of the script
</script>

<template>
  <n-card :bordered="false" style="border-radius: 14px; box-shadow: 0 1px 3px rgba(0,0,0,0.06)">
    <div style="display: flex; align-items: center; gap: 12px; margin-bottom: 20px">
      <n-button class="btn-back" aria-label="返回分析主页" @click="$emit('back')" style="border-radius: 8px">← 返回</n-button>
      <n-text style="font-size: 16px; font-weight: 600">⭐ 自选股跟踪</n-text>
      <n-text depth="3">共 {{ items.length }} 只</n-text>
      <!-- ★ 功能升级(L1)：手动刷新行情 -->
      <n-button size="small" quaternary :loading="quotesLoading" :disabled="items.length === 0" @click="loadQuotes" aria-label="刷新自选股行情" style="margin-left: auto">🔄 刷新行情</n-button>
    </div>

    <!-- 添加 -->
    <n-space style="margin-bottom: 16px">
      <n-input v-model:value="newCode" placeholder="股票代码" aria-label="股票代码输入框" style="width: 140px" :disabled="adding" @keyup.enter="handleAdd" />
      <n-input v-model:value="newName" placeholder="名称（可选）" aria-label="股票名称输入框（可选）" style="width: 160px" :disabled="adding" @keyup.enter="handleAdd" />
      <n-button class="btn-create" :loading="adding" :disabled="!newCode.trim()" @click="handleAdd" aria-label="添加自选股" style="border-radius: 8px">⭐ 添加自选</n-button>
    </n-space>

    <n-alert v-if="error" type="error" :show-icon="true" style="margin-bottom: 12px" closable @close="error = null" role="alert">{{ error }}</n-alert>

    <n-spin :show="loading" role="status" aria-live="polite">
      <n-empty v-if="!loading && items.length === 0" description="暂无自选股" />
      <n-data-table v-else :columns="columns" :data="items" :row-key="(r: WatchlistItem) => r.code" :pagination="false" size="small" />
    </n-spin>
  </n-card>
</template>
