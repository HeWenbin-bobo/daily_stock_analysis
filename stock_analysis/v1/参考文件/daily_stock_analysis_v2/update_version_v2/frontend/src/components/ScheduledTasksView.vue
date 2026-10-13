<script setup lang="ts">
/**
 * 定时任务视图
 */
import { ref, onMounted } from 'vue'
import {
  NCard, NButton, NSpace, NText, NTag, NDataTable, NInput, NEmpty,
  NPopconfirm, NSpin, NModal, NSwitch, NAlert,
} from 'naive-ui'
import {
  getScheduledTasks, saveScheduledTask, deleteScheduledTask,
  runScheduledTask, type ScheduledTaskItem,
} from '../api/hotAnalysis'
import { extractErrorMessage } from '../utils/formatters'
import { h } from 'vue'

const emit = defineEmits<{ (e: 'back'): void }>()

const loading = ref(false)
const items = ref<ScheduledTaskItem[]>([])
const showModal = ref(false)
const saving = ref(false)
const triggering = ref<number | null>(null)
const error = ref<string | null>(null)

// 表单
const formName = ref('')
const formCron = ref('0 15 * * 1-5')
const formEnabled = ref(true)
const formMaxIntersection = ref(10)

const CRON_PRESETS = [
  { label: '每天15:00（收盘）', value: '0 15 * * 1-5' },
  { label: '每天09:30（开盘）', value: '30 9 * * 1-5' },
  { label: '每周一09:00', value: '0 9 * * 1' },
  { label: '自定义', value: '' },
]

async function load() {
  loading.value = true
  try {
    const data = await getScheduledTasks()
    items.value = data.items
  } catch (e: unknown) {
    // ★ round 6 audit：用户可见而非 console
    error.value = extractErrorMessage(e, '加载定时任务失败')
  } finally {
    loading.value = false
  }
}

async function handleSave() {
  if (!formName.value.trim() || !formCron.value.trim()) return
  saving.value = true
  error.value = null
  try {
    await saveScheduledTask({
      name: formName.value.trim(),
      cron_expr: formCron.value.trim(),
      enabled: formEnabled.value,
      config: { max_intersection: formMaxIntersection.value },
    })
    showModal.value = false
    formName.value = ''
    await load()
  } catch (e: unknown) {
    // ★ round 6 audit：用户可见
    error.value = extractErrorMessage(e, '保存失败')
  } finally {
    saving.value = false
  }
}

async function handleDelete(id: number) {
  try {
    await deleteScheduledTask(id)
    await load()
  } catch (e: unknown) {
    error.value = extractErrorMessage(e, '删除失败')
  }
}

async function handleTrigger(id: number) {
  triggering.value = id
  try {
    await runScheduledTask(id)
  } catch (e: unknown) {
    error.value = extractErrorMessage(e, '触发任务失败')
  } finally {
    triggering.value = null
  }
}

onMounted(load)

const columns = [
  { title: '名称', key: 'name', width: 150, render: (r: ScheduledTaskItem) => h('strong', {}, r.name) },
  {
    title: 'Cron', key: 'cron_expr', width: 140,
    render: (r: ScheduledTaskItem) => h(NTag, { size: 'small', bordered: false }, () => r.cron_expr),
  },
  {
    title: '状态', key: 'enabled', width: 70,
    render: (r: ScheduledTaskItem) => h(NTag, { type: r.enabled ? 'success' : 'default', size: 'small', bordered: false }, () => r.enabled ? '启用' : '禁用'),
  },
  {
    title: '上次执行', key: 'last_run_at', width: 170,
    render: (r: ScheduledTaskItem) => r.last_run_at || '从未执行',
  },
  {
    title: '结果', key: 'last_status', width: 80,
    render: (r: ScheduledTaskItem) => r.last_status ? h(NTag, { type: r.last_status === 'ok' ? 'success' : 'error', size: 'small', bordered: false }, () => r.last_status) : '-',
  },
  {
    title: '操作', key: 'actions', width: 200,
    render: (r: ScheduledTaskItem) => h(NSpace, { size: 6 }, () => [
      h(NButton, { size: 'tiny', class: 'btn-run', 'aria-label': `立即执行任务 ${r.name}`, loading: triggering.value === r.id, onClick: () => handleTrigger(r.id), style: 'border-radius: 6px' }, () => '▶ 执行'),
      h(NPopconfirm, { onPositiveClick: () => handleDelete(r.id) },
        { trigger: () => h(NButton, { size: 'tiny', class: 'btn-danger', 'aria-label': `删除定时任务 ${r.name}`, style: 'border-radius: 6px' }, () => '🗑 删除'), default: () => '确定删除？' },
      ),
    ]),
  },
]
</script>

<template>
  <n-card :bordered="false" style="border-radius: 14px; box-shadow: 0 1px 3px rgba(0,0,0,0.06)">
    <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 20px">
      <n-space align="center">
        <n-button class="btn-back" aria-label="返回分析主页" @click="$emit('back')" style="border-radius: 8px">← 返回</n-button>
        <n-text style="font-size: 16px; font-weight: 600">⏰ 定时任务</n-text>
        <n-text depth="3">定时自动执行分析</n-text>
      </n-space>
      <n-button class="btn-create" aria-label="新建定时任务" @click="showModal = true" style="border-radius: 8px">+ 新建任务</n-button>
    </div>

    <n-alert type="info" style="margin-bottom: 16px" :show-icon="true">
      定时任务依赖后端调度器。当前为手动触发模式，点击"执行"立即运行一次分析。
    </n-alert>
    <n-alert v-if="error" type="error" :show-icon="true" style="margin-bottom: 12px" closable @close="error = null" role="alert">{{ error }}</n-alert>

    <n-spin :show="loading" role="status" aria-live="polite">
      <n-empty v-if="!loading && items.length === 0" description="暂无定时任务" />
      <n-data-table v-else :columns="columns" :data="items" :row-key="(r: ScheduledTaskItem) => r.id" :pagination="false" size="small" />
    </n-spin>

    <!-- 新建弹窗 -->
    <n-modal v-model:show="showModal" preset="card" title="新建定时任务" style="width: 480px">
      <n-space vertical style="gap: 14px">
        <n-input v-model:value="formName" placeholder="任务名称（如：每日收盘分析）" />
        <div>
          <n-text>执行时间：</n-text>
          <n-space style="margin-top: 6px">
            <n-button v-for="p in CRON_PRESETS" :key="p.value" size="small" :type="formCron === p.value ? 'primary' : 'default'" @click="formCron = p.value">
              {{ p.label }}
            </n-button>
          </n-space>
          <n-input v-model:value="formCron" placeholder="cron 表达式" style="margin-top: 8px" />
        </div>
        <div style="display: flex; align-items: center; gap: 10px">
          <n-text>启用：</n-text>
          <n-switch v-model:value="formEnabled" />
        </div>
      </n-space>
      <template #action>
        <n-space justify="end">
          <n-button class="btn-cancel" @click="showModal = false" style="border-radius: 8px">取消</n-button>
          <n-button class="btn-confirm" :loading="saving" :disabled="!formName.trim() || !formCron.trim()" @click="handleSave" style="border-radius: 8px">💾 保存任务</n-button>
        </n-space>
      </template>
    </n-modal>
  </n-card>
</template>
