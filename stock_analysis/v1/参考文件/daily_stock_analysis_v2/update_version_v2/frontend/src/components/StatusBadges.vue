<script setup lang="ts">
/**
 * 榜单抓取状态行
 */
import { computed } from 'vue'
import { statusType } from '../utils/formatters'

const props = defineProps<{
  hotListsSummary: Record<string, number>
  hotListsStatus?: Record<string, string>
  hotListsNotes?: Record<string, string>
}>()

const SOURCES = ['东方财富', '雪球', '同花顺']

const failedSources = computed(() =>
  Object.entries(props.hotListsStatus ?? {})
    .filter(([, s]) => s === 'failed')
    .map(([k]) => k),
)

const activeCount = computed(
  () =>
    Object.values(props.hotListsStatus ?? {}).filter((s) => s !== 'failed').length,
)

/** 把 statusType 计算收敛到一个方法里，避免模板里多次重复调用 */
function badgeStyle(src: string) {
  return statusType(props.hotListsStatus?.[src])
}
</script>

<template>
  <n-card
    size="small"
    style="
      margin-top: 18px;
      border-radius: 12px;
      background: linear-gradient(135deg, #f8fafc 0%, #f1f5f9 100%);
      border: 1px solid rgba(226,232,240,0.5);
    "
  >
    <div style="display: flex; align-items: center; flex-wrap: wrap; gap: 14px">
      <div style="display: flex; align-items: center; gap: 6px">
        <span style="font-size: 16px">📡</span>
        <n-text strong style="font-size: 13px; color: #1e293b">本次抓取</n-text>
      </div>
      <n-space>
        <template v-for="src in SOURCES" :key="src">
          <template v-if="hotListsNotes?.[src]">
            <n-tooltip placement="top">
              <template #trigger>
                <n-tag
                  :style="{
                    background: badgeStyle(src).color,
                    color: 'white',
                    border: 'none',
                    cursor: 'help',
                    boxShadow: `0 2px 8px ${badgeStyle(src).color}40`,
                  }"
                  size="small"
                  round
                >
                  {{ badgeStyle(src).icon }}
                  {{ src }}
                  ({{ hotListsSummary[src] ?? 0 }})
                </n-tag>
              </template>
              {{ hotListsNotes[src] }}
            </n-tooltip>
          </template>
          <template v-else>
            <n-tag
              :style="{
                background: badgeStyle(src).color,
                color: 'white',
                border: 'none',
                boxShadow: `0 2px 8px ${badgeStyle(src).color}40`,
              }"
              size="small"
              round
            >
              {{ badgeStyle(src).icon }}
              {{ src }}
              ({{ hotListsSummary[src] ?? 0 }})
            </n-tag>
          </template>
        </template>
      </n-space>
      <n-text v-if="failedSources.length" depth="3" style="font-size: 12px">
        · 实际有效榜单 {{ activeCount }} 个
      </n-text>
    </div>
    <n-alert
      v-if="failedSources.length"
      type="warning"
      :show-icon="true"
      style="margin-top: 12px; margin-bottom: 0"
      size="small"
    >
      以下榜单本次获取失败:
      <strong>{{ failedSources.join('、') }}</strong>。 已自动降级到剩余
      <strong>{{ activeCount }}</strong> 个榜单求交集。
    </n-alert>
  </n-card>
</template>
