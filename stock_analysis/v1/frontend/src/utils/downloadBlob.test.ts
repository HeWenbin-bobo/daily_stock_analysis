/// <reference types="vitest/globals" />
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { downloadBlob } from './downloadBlob';

// iter 125: 补测 downloadBlob catch 分支 (utils/downloadBlob.ts:26) — review 报告
// 标注为 MEDIUM。appendChild 失败时的 fallback click + DEV console.warn 路径从未被
// 直接测试, 只能通过 exportData.test.ts 间接覆盖 — 但那个 mock 让 fallback click
// 也成功, 所以 catch block 内的 console.warn 永远不触发。
describe('downloadBlob', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('appends anchor to body, clicks, and removes when DOM available', () => {
    // 正常路径: spy 不抛错
    const appendSpy = vi.spyOn(document.body, 'appendChild');
    const removeSpy = vi.spyOn(document.body, 'removeChild');
    const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, 'click');

    const blob = new Blob(['test'], { type: 'text/plain' });
    downloadBlob(blob, 'test.txt');

    // 正常路径: 走 try 分支, 不进 catch
    expect(appendSpy).toHaveBeenCalledTimes(1);
    expect(clickSpy).toHaveBeenCalledTimes(1);
    expect(removeSpy).toHaveBeenCalledTimes(1);
  });

  it('falls back to direct click when appendChild throws (DEV warning emitted)', () => {
    // iter 125: 验证 catch block 真的能跑 — appendChild 抛 TypeError, fallback
    // 直接 a.click(), 且 DEV 守卫的 console.warn 被触发。
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
    vi.spyOn(document.body, 'appendChild').mockImplementation(() => {
      throw new TypeError('HierarchyRequestError');
    });
    const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, 'click');

    const blob = new Blob(['test'], { type: 'text/plain' });
    expect(() => downloadBlob(blob, 'test.txt')).not.toThrow();
    // fallback click 应被调用
    expect(clickSpy).toHaveBeenCalledTimes(1);
    // DEV 守卫的 console.warn 应记录降级
    expect(warnSpy).toHaveBeenCalled();
    expect(warnSpy.mock.calls[0][0]).toContain('downloadBlob');
  });
});