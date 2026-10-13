<script setup lang="ts">
/**
 * 对比分析视图
 *
 * Phase 3: 选择两次分析结果进行对比
 */
import { ref, computed } from 'vue'
import {
  NCard,
  NButton,
  NSpace,
  NText,
  NTag,
  NSelect,
  NDataTable,
  NAlert,
  NSpin,
  type DataTableColumns,
} from 'naive-ui'
import {
  getAnalysisHistory,
  compareRuns,
  type AnalysisHistoryItem,
  type CompareResult,
} from '../api/hotAnalysis'
import { scoreColor, adviceType, extractErrorMessage } from '../utils/formatters'
import { h } from 'vue'

const emit = defineEmits<{
  (e: 'back'): void
}>()

const loading = ref(false)
const comparing = ref(false)
const error = ref<string | null>(null)

// 历史记录列表（用于下拉选择）
const historyItems = ref<AnalysisHistoryItem[]>([])
const selectedRun1 = ref<number | null>(null)
const selectedRun2 = ref<number | null>(null)

// 对比结果
const compareResult = ref<CompareResult | null>(null)

async function loadHistory() {
  loading.value = true
  try {
    const data = await getAnalysisHistory(1, 100)
    historyItems.value = data.items
  } catch (e: unknown) {
    error.value = extractErrorMessage(e, '加载历史记录失败')
  } finally {
    loading.value = false
  }
}

async function doCompare() {
  if (!selectedRun1.value || !selectedRun2.value) return
  comparing.value = true
  error.value = null
  compareResult.value = null

  try {
    const result = await compareRuns(selectedRun1.value, selectedRun2.value)
    compareResult.value = result
  } catch (e: unknown) {
    error.value = extractErrorMessage(e, '对比失败')
  } finally {
    comparing.value = false
  }
}

// 下拉选项
const historyOptions = computed(() =>
  historyItems.value.map((item) => ({
    label: `#${item.id} ${item.started_at.slice(0, 16)} | 通过${item.passed_stock_count}只`,
    value: item.id,
  })),
)

// 评分变化项类型
interface ScoreChangeItem {
  code: string
  name: string
  old_score: number
  new_score: number
  old_advice: string
  new_advice: string
}

// 评分变化表格列
const changeColumns: DataTableColumns<ScoreChangeItem> = [
  { title: '代码', key: 'code', width: 90 },
  { title: '名称', key: 'name', width: 110 },
  {
    title: '旧评分',
    key: 'old_score',
    width: 80,
    render: (r) =>
      h('span', { style: `color: ${scoreColor(r.old_score)}; font-weight: 600` }, String(r.old_score)),
  },
  {
    title: '新评分',
    key: 'new_score',
    width: 80,
    render: (r) =>
      h('span', { style: `color: ${scoreColor(r.new_score)}; font-weight: 600` }, String(r.new_score)),
  },
  {
    title: '变化',
    key: 'diff',
    width: 80,
    render: (r) => {
      const diff = r.new_score - r.old_score
      const color = diff > 0 ? '#16a34a' : diff < 0 ? '#ef4444' : '#64748b'
      const prefix = diff > 0 ? '+' : ''
      return h('span', { style: `color: ${color}; font-weight: 600` }, `${prefix}${diff}`)
    },
  },
  {
    title: '旧建议',
    key: 'old_advice',
    width: 100,
    render: (r) =>
      h(NTag, { type: adviceType(r.old_advice), bordered: false, size: 'small' }, () => r.old_advice || '-'),
  },
  {
    title: '新建议',
    key: 'new_advice',
    width: 100,
    render: (r) =>
      h(NTag, { type: adviceType(r.new_advice), bordered: false, size: 'small' }, () => r.new_advice || '-'),
  },
]

// 初始化
loadHistory()
</script>

<template>
  <n-card
    :bordered="false"
    style="border-radius: 14px; box-shadow: 0 1px 3px rgba(0, 0, 0, 0.06)"
  >
    <div style="display: flex; align-items: center; gap: 12px; margin-bottom: 20px">
      <n-button class="btn-back" aria-label="返回分析主页" @click="$emit('back')" style="border-radius: 8px">← 返回</n-button>
      <n-text style="font-size: 16px; font-weight: 600">🔄 对比分析</n-text>
    </div>

    <!-- 选择两次分析 -->
    <n-space vertical style="margin-bottom: 20px">
      <n-space align="center" wrap>
        <n-text>第一次分析：</n-text>
        <n-select
          v-model:value="selectedRun1"
          :options="historyOptions"
          placeholder="选择分析记录"
          aria-label="选择第一次分析记录"
          style="width: 360px"
          :loading="loading"
          clearable
        />
      </n-space>
      <n-space align="center" wrap>
        <n-text>第二次分析：</n-text>
        <n-select
          v-model:value="selectedRun2"
          :options="historyOptions"
          placeholder="选择分析记录"
          aria-label="选择第二次分析记录"
          style="width: 360px"
          :loading="loading"
          clearable
        />
      </n-space>
      <n-button
        class="btn-compare"
        :loading="comparing"
        :disabled="!selectedRun1 || !selectedRun2 || selectedRun1 === selectedRun2"
        @click="doCompare"
        aria-label="开始对比两次分析记录"
        style="height: 38px; border-radius: 10px"
      >
        🔄 开始对比
      </n-button>
    </n-space>

    <n-alert v-if="error" type="error" :show-icon="true" style="margin-bottom: 16px" role="alert">
      {{ error }}
    </n-alert>

    <n-spin :show="comparing" role="status" aria-live="polite">
      <div v-if="compareResult">
        <!-- 摘要 -->
        <n-space style="margin-bottom: 16px" wrap>
          <n-tag type="success" :bordered="false">
            新增: {{ compareResult.new_stocks.length }} 只
          </n-tag>
          <n-tag type="error" :bordered="false">
            退出: {{ compareResult.removed_stocks.length }} 只
          </n-tag>
          <n-tag type="info" :bordered="false">
            评分变化: {{ compareResult.score_changes.length }} 只
          </n-tag>
        </n-space>

        <!-- 新增股票 -->
        <div v-if="compareResult.new_stocks.length" style="margin-bottom: 16px">
          <n-text style="font-weight: 600">🆕 新增股票：</n-text>
          <n-space style="margin-top: 8px">
            <n-tag v-for="code in compareResult.new_stocks" :key="code" type="success" :bordered="false">
              {{ code }}
            </n-tag>
          </n-space>
        </div>

        <!-- 退出股票 -->
        <div v-if="compareResult.removed_stocks.length" style="margin-bottom: 16px">
          <n-text style="font-weight: 600">📉 退出股票：</n-text>
          <n-space style="margin-top: 8px">
            <n-tag v-for="code in compareResult.removed_stocks" :key="code" type="error" :bordered="false">
              {{ code }}
            </n-tag>
          </n-space>
        </div>

        <!-- 评分变化表格 -->
        <div v-if="compareResult.score_changes.length">
          <n-text style="font-weight: 600; margin-bottom: 8px; display: block">📊 评分变化：</n-text>
          <n-data-table
            :columns="changeColumns"
            :data="compareResult.score_changes"
            :row-key="(r: ScoreChangeItem) => r.code"
            :pagination="false"
            size="small"
          />
        </div>
      </div>
    </n-spin>
  </n-card>
</template>
