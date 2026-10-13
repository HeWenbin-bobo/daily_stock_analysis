/** 触发浏览器下载一个 Blob 到本地文件系统。
 *
 * iter 88: 共享实现 — exportData.ts 和 watchlistIO.ts 之前各有自己的副本, 维护时
 * 必须同步更新 (如 SSR 守卫、URL 释放时机)。合并到一处, 单一事实来源。
 *
 * 实现: 创建 <a download> 元素, 临时附加到 DOM (部分浏览器要求元素在 DOM 中
 * 才能触发 click), 立即触发 click, 异步 setTimeout 释放 ObjectURL。
 *
 * 容错: jsdom / 测试环境会 mock createElement 返回非 Node 的代理对象, 此时
 * appendChild 抛 TypeError。catch 降级为直接 a.click() — 部分浏览器 / 测试
 * 环境下元素不必在 DOM 中也能触发下载。
 */
export function downloadBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  try {
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
  } catch (err) {
    // 降级: jsdom 测试 / 某些受限环境 appendChild 失败, 直接 click
    // iter 104: 加 DEV 警告 — 之前完全静默吞错, 生产构建中连 console 都看不到,
    // 排查下载失败很费时。DEV 守卫避免生产噪音。
    if (import.meta.env.DEV) {
      console.warn("downloadBlob: appendChild failed, attempting direct click:", err);
    }
    a.click();
  }
  // 释放 URL(异步, 不阻塞)
  // iter 120: 从 setTimeout(0) 改为 1000ms 兜底 — 之前 0ms 在事件循环饱和或
  // 测试环境 (jsdom 不真正异步执行 setTimeout 0) 下可能延迟/不触发, 导致 blob
  // URL 内存泄漏。1000ms 覆盖了浏览器下载开始的窗口, 不依赖 addEventListener
  // (jsdom 测试 mock createElement 返回的 anchor 代理没有 addEventListener, 之前
  // 试图用 click listener 反而让测试 TypeError)。
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
