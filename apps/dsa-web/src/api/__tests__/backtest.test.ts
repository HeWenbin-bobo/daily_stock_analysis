import { beforeEach, describe, expect, it, vi } from 'vitest';
import { backtestApi } from '../backtest';

const { post } = vi.hoisted(() => ({ post: vi.fn() }));

vi.mock('../index', () => ({
  default: { post },
}));

describe('backtestApi.run', () => {
  beforeEach(() => {
    post.mockReset();
    post.mockResolvedValue({ data: {} });
  });

  it('allows a default run to outlive the shared 30s axios budget', async () => {
    // A default run evaluates up to 200 stored analyses and re-fetches daily
    // bars per candidate (~590s measured). With the shared 30s default the
    // request was reported as failed while the server kept working.
    await backtestApi.run();

    const [url, , config] = post.mock.calls[0];
    expect(url).toBe('/api/v1/backtest/run');
    expect(config?.timeout).toBeGreaterThan(590_000);
  });

  it('keeps every filter field on the request body', async () => {
    await backtestApi.run({
      code: ' 600519 ',
      force: true,
      evalWindowDays: 10,
      minAgeDays: 0,
      limit: 50,
    });

    const [, body] = post.mock.calls[0];
    expect(body).toEqual({
      code: '600519',
      force: true,
      eval_window_days: 10,
      min_age_days: 0,
      limit: 50,
    });
  });
});
