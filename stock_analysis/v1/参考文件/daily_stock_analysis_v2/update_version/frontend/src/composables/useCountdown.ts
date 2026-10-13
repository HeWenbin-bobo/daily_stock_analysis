/**
 * 计时器 composable — 管理实时时钟和分析耗时计时器
 */
import { ref, onMounted, onBeforeUnmount } from 'vue'

export function useCountdown() {
  // 实时时钟
  const now = ref<Date>(new Date())
  let clockTimer: number | null = null

  // 分析耗时
  const elapsedSec = ref(0)
  let elapsedTimer: number | null = null

  onMounted(() => {
    clockTimer = window.setInterval(() => {
      now.value = new Date()
    }, 1000)
  })

  onBeforeUnmount(() => {
    if (clockTimer != null) {
      clearInterval(clockTimer)
      clockTimer = null
    }
    stopElapsed()
  })

  function startElapsed() {
    const start = Date.now()
    elapsedSec.value = 0
    elapsedTimer = window.setInterval(() => {
      elapsedSec.value = Math.floor((Date.now() - start) / 1000)
    }, 1000)
  }

  function stopElapsed() {
    if (elapsedTimer) {
      clearInterval(elapsedTimer)
      elapsedTimer = null
    }
  }

  return {
    now,
    elapsedSec,
    startElapsed,
    stopElapsed,
  }
}
