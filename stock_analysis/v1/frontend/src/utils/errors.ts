/** 前端错误 -> 用户友好提示。
 *
 * 设计原则:
 *   - 不直接泄露后端原始错误细节(堆栈、SQL、路径、token 等)
 *   - 但保留 axios 提供的 status / code 语义, 便于排错
 *   - 输出短句, 适合直接渲染到 UI
 */

import { AxiosError } from 'axios';

export function toUserMessage(err: unknown, fallback = '操作失败, 请稍后重试'): string {
  // axios 取消: 不当作错误上报
  if (err instanceof AxiosError && (err.name === 'CanceledError' || err.code === 'ERR_CANCELED')) {
    return '';
  }
  if (err instanceof AxiosError) {
    const status = err.response?.status;
    // 不要把后端的原始 data / message 直接吐给用户, 避免信息泄露
    if (status === undefined) {
      return '网络异常, 请检查连接后重试';
    }
    if (status >= 500) return '服务暂不可用, 请稍后重试';
    if (status === 404) return '资源不存在或已下架';
    if (status === 403) return '无权访问该资源';
    if (status === 401) return '未授权, 请重新登录';
    if (status === 429) return '请求过于频繁, 请稍后再试';
    if (status >= 400) return '请求被拒绝, 请检查参数';
    return fallback;
  }

  // 普通 JS 错误: 严格走 fallback, 不暴露任何 raw message.
// 之前会把 Error.message 直接 slice 输出, 会泄漏 stack/路径/URL 等技术细节给用户。
// iter 84: 收紧安全策略 — 非 axios 错误一律用通用 fallback。
// iter 86: 删除 unused `msg` 变量, 之前 `const msg = rawMessage(err); if (!msg) return fallback;`
// 后面立即 `return fallback`, 让 msg 检查完全 dead — 收紧策略后已经不需要区分 msg 是否存在。
return fallback;
}