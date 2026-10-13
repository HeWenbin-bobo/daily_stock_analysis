<script setup lang="ts">
/**
 * 自选股跟踪视图
 */
import { ref, onMounted, h } from 'vue'
import { NCard, NButton, NSpace, NText, NTag, NDataTable, NInput, NEmpty, NPopconfirm, NSpin, NAlert } from 'naive-ui'
import { getWatchlist, addToWatchlist, removeFromWatchlist, type WatchlistItem } from '../api/hotAnalysis'
import { extractErrorMessage } from '../utils/formatters'

const emit = defineEmits<{ (e: 'back'): void }>()

const loading = ref(false)
const items = ref<WatchlistItem[]>([])
const newCode = ref('')
const newName = ref('')
const adding = ref(false)
const error = ref<string | null>(null)

async function load() {
  loading.value = true
  try {
    const data = await getWatchlist()
    items.value = data.items
  } catch (e) {
    // ★ round 6 audit：暴露给用户（已有 error ref + 模板 n-alert）
    error.value = extractErrorMessage(e, '加载自选股失败')
  } finally {
    loading.value = false
  }
}

async function handleAdd() {
  if (!newCode.value.trim()) return
  adding.value = true
  error.value = null
  try {
    await addToWatchlist(newCode.value.trim(), newName.value.trim())
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

const columns = [
  { title: '代码', key: 'code', width: 100, render: (r: WatchlistItem) => r.code },
  { title: '名称', key: 'name', width: 120 },
  { title: '备注', key: 'notes', render: (r: WatchlistItem) => r.notes || '-' },
  { title: '添加时间', key: 'created_at', width: 170 },
  {
    title: '操作', key: 'actions', width: 100,
    render: (r: WatchlistItem) => h(
      NPopconfirm,
      { onPositiveClick: () => handleRemove(r.code) },
      { trigger: () => h(NButton, { size: 'tiny', class: 'btn-danger', style: 'border-radius: 6px' }, () => '🗑 移除'), default: () => `确定移除 ${r.code}？` },
    ),
  },
]

// h is imported at the top of the script
</script>

<template>
  <n-card :bordered="false" style="border-radius: 14px; box-shadow: 0 1px 3px rgba(0,0,0,0.06)">
    <div style="display: flex; align-items: center; gap: 12px; margin-bottom: 20px">
      <n-button class="btn-back" @click="$emit('back')" style="border-radius: 8px">← 返回</n-button>
      <n-text style="font-size: 16px; font-weight: 600">⭐ 自选股跟踪</n-text>
      <n-text depth="3">共 {{ items.length }} 只</n-text>
    </div>

    <!-- 添加 -->
    <n-space style="margin-bottom: 16px">
      <n-input v-model:value="newCode" placeholder="股票代码" style="width: 140px" :disabled="adding" @keyup.enter="handleAdd" />
      <n-input v-model:value="newName" placeholder="名称（可选）" style="width: 160px" :disabled="adding" @keyup.enter="handleAdd" />
      <n-button class="btn-create" :loading="adding" :disabled="!newCode.trim()" @click="handleAdd" style="border-radius: 8px">⭐ 添加自选</n-button>
    </n-space>

    <n-alert v-if="error" type="error" :show-icon="true" style="margin-bottom: 12px" closable @close="error = null">{{ error }}</n-alert>

    <n-spin :show="loading">
      <n-empty v-if="!loading && items.length === 0" description="暂无自选股" />
      <n-data-table v-else :columns="columns" :data="items" :row-key="(r: WatchlistItem) => r.code" :pagination="false" size="small" />
    </n-spin>
  </n-card>
</template>
