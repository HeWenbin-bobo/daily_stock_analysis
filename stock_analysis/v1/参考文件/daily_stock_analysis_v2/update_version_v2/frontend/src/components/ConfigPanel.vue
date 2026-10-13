<script setup lang="ts">
/**
 * 配置面板 — 最长时间 + 最大分析数量 + 过滤规则
 *
 * Phase 1: 新增过滤规则配置（最低评分、允许建议、排除关键词）
 * 配置自动保存到 localStorage
 */
import { watch } from 'vue'
import { debounce } from '../utils/debounce'
import {
  NText,
  NRadioGroup,
  NRadio,
  NCollapse,
  NCollapseItem,
  NInputNumber,
  NCheckboxGroup,
  NCheckbox,
  NSwitch,
  NSpace,
  NTag,
} from 'naive-ui'
import { saveConfig } from '../utils/config'

const props = defineProps<{
  selectedMaxRuntime: number
  selectedMaxIntersection: number
  rounds: number
  interRoundSleep: number
  loading: boolean
  // 过滤规则
  minScore: number
  allowedAdvice: string[]
  excludeKeywords: string[]
  requireAllRounds: boolean
  // LLM 超时
  llmTimeout: number
  // 单股并发
  singleConcurrency: number
}>()

const emit = defineEmits<{
  (e: 'update:selectedMaxRuntime', v: number): void
  (e: 'update:selectedMaxIntersection', v: number): void
  (e: 'update:rounds', v: number): void
  (e: 'update:interRoundSleep', v: number): void
  (e: 'update:minScore', v: number | null): void
  (e: 'update:allowedAdvice', v: string[]): void
  (e: 'update:excludeKeywords', v: string[]): void
  (e: 'update:requireAllRounds', v: boolean): void
  (e: 'update:llmTimeout', v: number): void
  (e: 'update:singleConcurrency', v: number): void
}>()

const MAX_RUNTIME_OPTIONS: number[] = [60, 120, 180, 240, 300, 600, 1200, 1800]
const INTERSECTION_OPTIONS: number[] = [1, 5, 10, 30, 50, 100, 200, 300]
const LLM_TIMEOUT_OPTIONS: number[] = [60, 120, 180, 300, 600]
const ROUNDS_OPTIONS: number[] = [1, 2, 3, 4, 5]
const INTER_ROUND_SLEEP_OPTIONS: number[] = [0, 0.3, 0.6, 1.0, 2.0, 5.0]
const SINGLE_CONCURRENCY_OPTIONS: number[] = [1, 2, 3, 5, 8]

const ADVICE_OPTIONS = [
  { label: '买入', value: '买入' },
  { label: '加仓', value: '加仓' },
  { label: '持有', value: '持有' },
  { label: 'BUY', value: 'BUY' },
  { label: 'HOLD', value: 'HOLD' },
  { label: 'ADD', value: 'ADD' },
]

const KEYWORD_OPTIONS = [
  { label: '卖出', value: '卖出' },
  { label: '减仓', value: '减仓' },
  { label: 'SELL', value: 'SELL' },
  { label: 'REDUCE', value: 'REDUCE' },
]

// 监听所有配置变化自动保存到 localStorage（debounce 300ms 避免连续写）
const _saveConfigDebounced = debounce(() => saveConfig({
  maxRuntime: props.selectedMaxRuntime,
  maxIntersection: props.selectedMaxIntersection,
  rounds: props.rounds,
  interRoundSleep: props.interRoundSleep,
  minScore: props.minScore,
  allowedAdvice: props.allowedAdvice,
  excludeKeywords: props.excludeKeywords,
  requireAllRounds: props.requireAllRounds,
  llmTimeout: props.llmTimeout,
  singleConcurrency: props.singleConcurrency,
}), 300)
watch(
  () => [
    props.selectedMaxRuntime,
    props.selectedMaxIntersection,
    props.rounds,
    props.interRoundSleep,
    props.minScore,
    props.allowedAdvice,
    props.excludeKeywords,
    props.requireAllRounds,
    props.llmTimeout,
    props.singleConcurrency,
  ],
  _saveConfigDebounced,
  { deep: true },
)
</script>

<template>
  <!-- 基础配置 -->
  <div
    style="
      display: flex;
      align-items: center;
      gap: 12px;
      padding-bottom: 14px;
      margin-bottom: 14px;
      border-bottom: 1px dashed rgba(148,163,184,0.25);
      flex-wrap: wrap;
    "
  >
    <div style="display: flex; align-items: center; gap: 8px; min-width: 140px">
      <span style="font-size: 18px">⏱</span>
      <n-text strong style="font-size: 14px; color: var(--color-label)">最长任务时间</n-text>
    </div>
    <n-radio-group
      :value="selectedMaxRuntime"
      @update:value="$emit('update:selectedMaxRuntime', $event)"
      :disabled="loading"
      size="medium"
    >
      <n-radio
        v-for="opt in MAX_RUNTIME_OPTIONS"
        :key="opt"
        :value="opt"
        :label="`${opt}分钟`"
      />
    </n-radio-group>
    <n-text depth="3" style="font-size: 11px; margin-left: auto; opacity: 0.7">
      LLM 单次 ~30-60s
    </n-text>
  </div>

  <div
    style="
      display: flex;
      align-items: center;
      gap: 12px;
      padding-bottom: 16px;
      margin-bottom: 16px;
      border-bottom: 1px solid rgba(148,163,184,0.2);
      flex-wrap: wrap;
    "
  >
    <div style="display: flex; align-items: center; gap: 8px; min-width: 140px">
      <span style="font-size: 18px">📊</span>
      <n-text strong style="font-size: 14px; color: var(--color-label)">分析数量</n-text>
    </div>
    <n-radio-group
      :value="selectedMaxIntersection"
      @update:value="$emit('update:selectedMaxIntersection', $event)"
      :disabled="loading"
      size="medium"
    >
      <n-radio
        v-for="opt in INTERSECTION_OPTIONS"
        :key="opt"
        :value="opt"
        :label="`${opt}只`"
      />
    </n-radio-group>
    <n-text depth="3" style="font-size: 11px; margin-left: auto; opacity: 0.7">
      10只 ≈ 5min / 100只 ≈ 1.5h / 300只 ≈ 5h
    </n-text>
  </div>

  <!-- 分析轮数 + 轮间间隔 -->
  <div
    style="
      display: flex;
      align-items: center;
      gap: 12px;
      padding-bottom: 14px;
      margin-bottom: 14px;
      border-bottom: 1px dashed rgba(148,163,184,0.25);
      flex-wrap: wrap;
    "
  >
    <div style="display: flex; align-items: center; gap: 8px; min-width: 140px">
      <span style="font-size: 18px">🔁</span>
      <n-text strong style="font-size: 14px; color: var(--color-label)">轮数 / 间隔</n-text>
    </div>
    <n-radio-group
      :value="rounds"
      @update:value="$emit('update:rounds', $event)"
      :disabled="loading"
      size="medium"
    >
      <n-radio v-for="opt in ROUNDS_OPTIONS" :key="opt" :value="opt" :label="`${opt}轮`" />
    </n-radio-group>
    <n-radio-group
      :value="interRoundSleep"
      @update:value="$emit('update:interRoundSleep', $event)"
      :disabled="loading"
      size="medium"
      style="margin-left: 12px"
    >
      <n-radio v-for="opt in INTER_ROUND_SLEEP_OPTIONS" :key="opt" :value="opt" :label="`${opt}s`" />
    </n-radio-group>
    <n-text depth="3" style="font-size: 11px; margin-left: auto; opacity: 0.7">
      每只股票的 LLM 轮询轮数 × 间隔
    </n-text>
  </div>

  <!-- LLM 单次超时 -->
  <div
    style="
      display: flex;
      align-items: center;
      gap: 12px;
      padding-bottom: 14px;
      margin-bottom: 14px;
      border-bottom: 1px dashed rgba(148,163,184,0.25);
      flex-wrap: wrap;
    "
  >
    <div style="display: flex; align-items: center; gap: 8px; min-width: 140px">
      <span style="font-size: 18px">⏳</span>
      <n-text strong style="font-size: 14px; color: var(--color-label)">LLM 单次超时</n-text>
    </div>
    <n-radio-group
      :value="llmTimeout"
      @update:value="$emit('update:llmTimeout', $event)"
      :disabled="loading"
      size="medium"
    >
      <n-radio
        v-for="opt in LLM_TIMEOUT_OPTIONS"
        :key="opt"
        :value="opt"
        :label="`${opt}s`"
      />
    </n-radio-group>
    <n-text depth="3" style="font-size: 11px; margin-left: auto; opacity: 0.7">
      每次 LLM 调用的最大等待时间
    </n-text>
  </div>

  <!-- 单股分析并发数 -->
  <div
    style="
      display: flex;
      align-items: center;
      gap: 12px;
      padding-bottom: 14px;
      margin-bottom: 14px;
      border-bottom: 1px dashed rgba(148,163,184,0.25);
      flex-wrap: wrap;
    "
  >
    <div style="display: flex; align-items: center; gap: 8px; min-width: 140px">
      <span style="font-size: 18px">🚀</span>
      <n-text strong style="font-size: 14px; color: var(--color-label)">单股并发</n-text>
    </div>
    <n-radio-group
      :value="singleConcurrency"
      @update:value="$emit('update:singleConcurrency', $event)"
      :disabled="loading"
      size="medium"
    >
      <n-radio
        v-for="opt in SINGLE_CONCURRENCY_OPTIONS"
        :key="opt"
        :value="opt"
        :label="`${opt}路`"
      />
    </n-radio-group>
    <n-text depth="3" style="font-size: 11px; margin-left: auto; opacity: 0.7">
      同时最多 N 只股票调用 LLM；修改后对下一次提交生效
    </n-text>
  </div>

  <!-- 过滤规则（可折叠） -->
  <n-collapse>
    <n-collapse-item name="filter">
      <template #header>
        <div style="display: flex; align-items: center; gap: 8px">
          <span style="font-size: 16px">🔧</span>
          <n-text strong style="font-size: 14px">过滤规则</n-text>
          <n-tag size="small" :bordered="false" style="background: rgba(99,102,241,0.08); color: #6366f1; font-size: 11px">
            高级
          </n-tag>
        </div>
      </template>
      <div style="padding: 8px 0; display: flex; flex-direction: column; gap: 20px">
        <!-- 最低评分 -->
        <div style="display: flex; align-items: center; gap: 12px; flex-wrap: wrap">
          <n-text style="font-size: 14px; color: var(--color-label-secondary); min-width: 100px; font-weight: 500">
            🎯 最低评分
          </n-text>
          <n-input-number
            :value="minScore"
            @update:value="$emit('update:minScore', $event)"
            :min="0"
            :max="100"
            :step="5"
            :disabled="loading"
            style="width: 120px"
            size="small"
          />
          <n-text depth="3" style="font-size: 12px">
            低于此分数的股票将被过滤
          </n-text>
        </div>

        <!-- 允许的建议 -->
        <div style="display: flex; align-items: flex-start; gap: 12px; flex-wrap: wrap">
          <n-text style="font-size: 14px; color: var(--color-label-secondary); min-width: 100px; padding-top: 2px; font-weight: 500">
            ✅ 允许建议
          </n-text>
          <n-checkbox-group
            :value="allowedAdvice"
            @update:value="$emit('update:allowedAdvice', $event as string[])"
            :disabled="loading"
          >
            <n-space>
              <n-checkbox
                v-for="opt in ADVICE_OPTIONS"
                :key="opt.value"
                :value="opt.value"
                :label="opt.label"
              />
            </n-space>
          </n-checkbox-group>
        </div>

        <!-- 排除关键词 -->
        <div style="display: flex; align-items: flex-start; gap: 12px; flex-wrap: wrap">
          <n-text style="font-size: 14px; color: var(--color-label-secondary); min-width: 100px; padding-top: 2px; font-weight: 500">
            🚫 排除关键词
          </n-text>
          <n-checkbox-group
            :value="excludeKeywords"
            @update:value="$emit('update:excludeKeywords', $event as string[])"
            :disabled="loading"
          >
            <n-space>
              <n-checkbox
                v-for="opt in KEYWORD_OPTIONS"
                :key="opt.value"
                :value="opt.value"
                :label="opt.label"
              />
            </n-space>
          </n-checkbox-group>
          <n-text depth="3" style="font-size: 12px">
            含这些关键词的建议将被直接淘汰
          </n-text>
        </div>

        <!-- 要求所有轮通过 -->
        <div style="display: flex; align-items: center; gap: 12px">
          <n-text style="font-size: 14px; color: var(--color-label-secondary); font-weight: 500">
            🔄 要求所有轮通过
          </n-text>
          <n-switch
            :value="requireAllRounds"
            @update:value="$emit('update:requireAllRounds', $event)"
            :disabled="loading"
          />
          <n-tag size="small" :bordered="false" :type="requireAllRounds ? 'info' : 'warning'">
            {{ requireAllRounds ? '每轮都需满足' : '多数轮满足即可' }}
          </n-tag>
        </div>
      </div>
    </n-collapse-item>
  </n-collapse>
</template>
