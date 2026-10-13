/// <reference types="vitest/globals" />
import { describe, expect, it } from 'vitest';
import { AxiosError } from 'axios';
import { toUserMessage } from './errors';

describe('toUserMessage', () => {
  it('maps 5xx to user-friendly message', () => {
    const err = new AxiosError('boom', 'ERR_BAD_RESPONSE', undefined, undefined, {
      status: 502, statusText: 'Bad Gateway', data: { detail: 'pg password leaked' }, headers: {},
    } as never);
    expect(toUserMessage(err)).toBe('服务暂不可用, 请稍后重试');
  });

  it('maps 404 to friendly message', () => {
    const err = new AxiosError('not found', 'ERR_NOT_FOUND', undefined, undefined, {
      status: 404, statusText: 'Not Found', data: 'internal stack', headers: {},
    } as never);
    expect(toUserMessage(err)).toBe('资源不存在或已下架');
  });

  it('maps 429 to friendly message', () => {
    const err = new AxiosError('rate', 'ERR_RATE', undefined, undefined, {
      status: 429, statusText: 'Too Many Requests', data: {}, headers: {},
    } as never);
    expect(toUserMessage(err)).toBe('请求过于频繁, 请稍后再试');
  });

  it('handles network error (no response)', () => {
    const err = new AxiosError('Network Error', 'ERR_NETWORK');
    expect(toUserMessage(err)).toBe('网络异常, 请检查连接后重试');
  });

  it('returns empty for cancel errors', () => {
    const err = new AxiosError('canceled', 'ERR_CANCELED');
    err.name = 'CanceledError';
    expect(toUserMessage(err)).toBe('');
  });

  it('truncates long regular Error messages', () => {
    const long = 'x'.repeat(500);
    const out = toUserMessage(new Error(long));
    expect(out.length).toBeLessThanOrEqual(120);
  });

  it('returns fallback for non-Error values', () => {
    expect(toUserMessage(null)).toBe('操作失败, 请稍后重试');
    expect(toUserMessage(undefined)).toBe('操作失败, 请稍后重试');
    expect(toUserMessage(42)).toBe('操作失败, 请稍后重试');
  });

  it('does not leak raw backend detail string into message', () => {
    const err = new AxiosError('whatever', 'X', undefined, undefined, {
      status: 400, statusText: 'Bad Request', data: 'SQLSTATE: password=secret123', headers: {},
    } as never);
    const msg = toUserMessage(err);
    expect(msg).not.toContain('SQLSTATE');
    expect(msg).not.toContain('secret123');
    expect(msg).toBe('请求被拒绝, 请检查参数');
  });

  // iter 125: 补测 401/403/418 status code 映射 — review 报告标注为 MEDIUM,
  // errors.ts:24-25 的 401/403 分支从未被任何测试覆盖, 回归风险高。
  it('maps 401 to login prompt', () => {
    const err = new AxiosError('unauthorized', 'ERR_UNAUTHORIZED', undefined, undefined, {
      status: 401, statusText: 'Unauthorized', data: { detail: 'token expired' }, headers: {},
    } as never);
    expect(toUserMessage(err)).toBe('未授权, 请重新登录');
    // 原始 detail 不应泄露
    expect(toUserMessage(err)).not.toContain('token expired');
  });

  it('maps 403 to forbidden message', () => {
    const err = new AxiosError('forbidden', 'ERR_FORBIDDEN', undefined, undefined, {
      status: 403, statusText: 'Forbidden', data: { detail: 'admin only' }, headers: {},
    } as never);
    expect(toUserMessage(err)).toBe('无权访问该资源');
    expect(toUserMessage(err)).not.toContain('admin only');
  });

  it('maps generic 4xx (e.g. 418) to rejection message', () => {
    const err = new AxiosError('teapot', 'X', undefined, undefined, {
      status: 418, statusText: "I'm a teapot", data: {}, headers: {},
    } as never);
    expect(toUserMessage(err)).toBe('请求被拒绝, 请检查参数');
  });

  // iter 125: 补测非 AxiosError 严格 fallback 路径 — review 报告标注为 LOW。
  // 旧实现会把 Error.message 直接 slice, 新实现一律 fallback。
  it('returns provided fallback for non-AxiosError Error', () => {
    expect(toUserMessage(new SyntaxError('boom'), 'custom fallback')).toBe('custom fallback');
    expect(toUserMessage(new TypeError('whatever'), '操作已取消')).toBe('操作已取消');
  });
});