/**
 * 通用 debounce 工具 — Round 5 a11y: ConfigPanel watch 连写 localStorage 时防抖
 *
 * 为不引入 lodash 依赖，单函数实现（trailing edge 300ms）
 */
export function debounce<T extends (...args: never[]) => void>(
  fn: T,
  delay = 300,
): (...args: Parameters<T>) => void {
  let timer: ReturnType<typeof setTimeout> | null = null
  return (...args: Parameters<T>) => {
    if (timer) clearTimeout(timer)
    timer = setTimeout(() => {
      timer = null
      fn(...args)
    }, delay)
  }
}
