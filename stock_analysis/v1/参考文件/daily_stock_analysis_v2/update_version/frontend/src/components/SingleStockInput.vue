<script setup lang="ts">
/**
 * 单股分析输入组件 —— 多股票并发版
 *
 * 支持：
 * - 多股票输入（逗号/空格/换行分隔）
 * - 并发执行（最多 3 只同时），其他排队
 * - 每只股票独立卡片：pending / running / done / failed / cancelled
 * - 全部取消 / 单只取消 / 移除已完成
 * - 已支持代码或中文名称（后端解析）
 */
import { ref, computed } from 'vue'
import {
  NCard,
  NInput,
  NButton,
  NSpace,
  NText,
  NTag,
  NDataTable,
  NAlert,
  NSpin,
  NIcon,
  NEmpty,
} from 'naive-ui'
import type { AnalysisRunItem, SingleStockResponse } from '../api/hotAnalysis'
import { scoreColor, adviceType } from '../utils/formatters'
import type { SingleTask } from '../composables/useHotAnalysis'

const props = defineProps<{
  tasks: SingleTask[]
  concurrency: number
}>()

const emit = defineEmits<{
  (e: 'submit', codes: string[]): void
  (e: 'cancel', id: string): void
  (e: 'cancelAll'): void
  (e: 'remove', id: string): void
  (e: 'clearFinished'): void
}>()

const inputText = ref('')

const queuedCount = computed(() => props.tasks.filter((t) => t.status === 'pending').length)
const runningCount = computed(() => props.tasks.filter((t) => t.status === 'running').length)
const doneCount = computed(() => props.tasks.filter((t) => t.status === 'done').length)
const failedCount = computed(() => props.tasks.filter((t) => t.status === 'failed').length)
const cancelledCount = computed(() => props.tasks.filter((t) => t.status === 'cancelled').length)
const hasActive = computed(() => queuedCount.value + runningCount.value > 0)

function onSubmit() {
  const codes = inputText.value
    .split(/[\s,，;；\n\r\t]+/)
    .map((s) => s.trim())
    .filter(Boolean)
  if (codes.length === 0) return
  emit('submit', codes)
  inputText.value = ''
}

const analysisColumns = [
  {
    title: '轮次',
    key: 'round',
    width: 60,
    render: (r: AnalysisRunItem) => `第 ${r.round} 轮`,
  },
  {
    title: '评分',
    key: 'sentiment_score',
    width: 80,
    render: (r: AnalysisRunItem) => (r.error ? '-' : String(r.sentiment_score ?? '-')),
  },
  {
    title: '建议',
    key: 'operation_advice',
    width: 100,
    render: (r: AnalysisRunItem) => r.operation_advice ?? '-',
  },
  {
    title: '趋势',
    key: 'trend_prediction',
    render: (r: AnalysisRunItem) => r.trend_prediction ?? '-',
  },
  {
    title: '模型',
    key: 'model_used',
    width: 120,
    render: (r: AnalysisRunItem) => r.model_used ?? '-',
  },
  {
    title: '状态',
    key: 'error',
    width: 80,
    render: (r: AnalysisRunItem) => (r.error ? '❌ 失败' : '✓ 成功'),
  },
]

function statusTagType(status: SingleTask['status']) {
  if (status === 'done') return 'success'
  if (status === 'failed') return 'error'
  if (status === 'cancelled') return 'warning'
  if (status === 'running') return 'info'
  return 'default'
}

function statusLabel(status: SingleTask['status']) {
  if (status === 'pending') return '排队'
  if (status === 'running') return '分析中'
  if (status === 'done') return '完成'
  if (status === 'failed') return '失败'
  if (status === 'cancelled') return '已取消'
  return status
}
</script>

<template>
  <div style="display: flex; flex-direction: column; gap: 16px">
    <!-- 输入区 -->
    <n-card :bordered="false" style="border-radius: 14px; box-shadow: 0 1px 3px rgba(0, 0, 0, 0.06)">
      <div style="margin-bottom: 14px">
        <n-text style="font-size: 16px; font-weight: 600">🔍 单股分析（并发）</n-text>
        <n-text depth="3" style="margin-left: 12px; font-size: 13px">
          输入 1 只或多只股票（代码或名称），自动并发（最多 {{ concurrency }} 只同时）。多种分隔符：逗号 / 空格 / 换行
        </n-text>
      </div>

      <n-space align="flex-start" :wrap="true" :size="[12, 12]">
        <n-input
          v-model:value="inputText"
          type="textarea"
          :autosize="{ minRows: 2, maxRows: 6 }"
          placeholder="例如：600519, 000858, 002156（每行一只 / 逗号 / 空格分隔）"
          style="min-width: 360px; flex: 1 1 360px"
          :disabled="false"
          @keydown.ctrl.enter="onSubmit"
          @keydown.meta.enter="onSubmit"
        />
        <n-space vertical :size="[8, 8]">
          <n-button
            class="btn-primary-action"
            :disabled="!inputText.trim()"
            @click="onSubmit"
            style="height: 38px; border-radius: 10px; min-width: 140px"
          >
            🚀 并发分析
          </n-button>
          <n-button
            v-if="hasActive"
            class="btn-danger"
            :disabled="!hasActive"
            @click="emit('cancelAll')"
            style="height: 32px; border-radius: 8px; min-width: 140px"
          >
            ✕ 全部取消
          </n-button>
          <n-button
            v-if="doneCount + failedCount + cancelledCount > 0"
            quaternary
            @click="emit('clearFinished')"
            style="height: 32px; border-radius: 8px; min-width: 140px; font-size: 12px"
          >
            🧹 清空已完成
          </n-button>
        </n-space>
      </n-space>

      <!-- 概览计数 -->
      <div v-if="tasks.length > 0" style="margin-top: 14px; display: flex; gap: 10px; flex-wrap: wrap; align-items: center">
        <n-text depth="3" style="font-size: 12px">共 {{ tasks.length }} 只：</n-text>
        <n-tag :type="'default'" size="small" :bordered="false">排队 {{ queuedCount }}</n-tag>
        <n-tag v-if="runningCount" :type="'info'" size="small" :bordered="false">
          <NSpin size="small" style="margin-right: 4px" />
          分析中 {{ runningCount }}
        </n-tag>
        <n-tag v-if="doneCount" :type="'success'" size="small" :bordered="false">完成 {{ doneCount }}</n-tag>
        <n-tag v-if="failedCount" :type="'error'" size="small" :bordered="false">失败 {{ failedCount }}</n-tag>
        <n-tag v-if="cancelledCount" :type="'warning'" size="small" :bordered="false">已取消 {{ cancelledCount }}</n-tag>
      </div>
    </n-card>

    <!-- 任务列表 -->
    <div v-if="tasks.length === 0">
      <n-empty description="尚未添加单股分析任务。输入代码或名称后点击「🚀 并发分析」" />
    </div>

    <n-card
      v-for="task in tasks"
      :key="task.id"
      :bordered="false"
      :style="`border-radius: 14px; box-shadow: 0 1px 3px rgba(0,0,0,0.06); ${
        task.status === 'running' ? 'border: 1px solid rgba(59,130,246,0.25);' : ''
      }`"
    >
      <!-- 标题行：代码 / 名称 / 状态 / 取消按钮 -->
      <div
        style="
          display: flex; align-items: center; gap: 12px;
          margin-bottom: 12px; padding-bottom: 12px;
          border-bottom: 1px dashed rgba(148,163,184,0.25);
        "
      >
        <n-text style="font-size: 18px; font-weight: 600; min-width: 130px">
          {{ task.result?.name || task.code }}
        </n-text>
        <n-text v-if="task.result && task.result.code !== task.code" depth="3" style="font-family: monospace; font-size: 12px">
          {{ task.result.code }}
        </n-text>
        <n-tag :type="statusTagType(task.status)" :bordered="false" round size="medium">
          <template v-if="task.status === 'running'">
            <NSpin size="small" style="margin-right: 4px" />
            {{ statusLabel(task.status) }}
          </template>
          <template v-else>
            {{ statusLabel(task.status) }}
          </template>
        </n-tag>
        <n-tag v-if="task.status === 'done' && task.result" :type="adviceType(task.result.final_advice)" :bordered="false" size="small">
          {{ task.result.final_advice || '-' }}
        </n-tag>
        <n-text
          v-if="task.status === 'done' && task.result"
          :style="`color: ${scoreColor(task.result.avg_score)}; font-weight: 600; font-size: 16px`"
        >
          {{ task.result.avg_score }} 分
        </n-text>
        <div style="margin-left: auto; display: flex; gap: 6px">
          <n-button
            v-if="task.status === 'pending' || task.status === 'running'"
            size="tiny"
            class="btn-danger"
            @click="emit('cancel', task.id)"
          >
            取消
          </n-button>
          <n-button
            v-else
            size="tiny"
            quaternary
            @click="emit('remove', task.id)"
          >
            移除
          </n-button>
        </div>
      </div>

      <!-- 失败 / 取消原因 -->
      <n-alert
        v-if="task.status === 'failed' && task.error"
        type="error"
        :title="'分析失败'"
        :show-icon="true"
        style="margin-bottom: 12px"
      >
        {{ task.error }}
      </n-alert>

      <!-- 跑完了展示多轮 -->
      <div v-else-if="task.status === 'done' && task.result">
        <n-data-table
          :columns="analysisColumns"
          :data="task.result.analyses"
          :row-key="(r: AnalysisRunItem) => r.round"
          :pagination="false"
          size="small"
        />
      </div>

      <!-- 跑中占位 -->
      <div v-else-if="task.status === 'running' || task.status === 'pending'" style="padding: 12px 0; color: var(--color-label-secondary); font-size: 13px">
        <NSpin size="small" style="margin-right: 8px" />
        {{ task.status === 'pending' ? '等待空闲并发槽…' : '正在调用 LLM，每只股票 3 轮…' }}
      </div>
    </n-card>
  </div>
</template>
