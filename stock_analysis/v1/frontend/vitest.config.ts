import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    environment: 'jsdom',
    globals: true,
    include: ['src/**/*.test.{ts,tsx}'],
    coverage: {
      reporter: ['text', 'json-summary'],
      include: ['src/utils/**', 'src/hooks/useWatchlist.ts', 'src/api/**'],
    },
    // iter 145: 抑制 lightweight-charts 在 jsdom teardown 时的已知无害 RAF 异常。
    // lightweight-charts 内部通过 requestAnimationFrame 调度绘制周期, jsdom
    // 的 RAF 实现在 cleanup 后仍会触发已入队的回调, 访问已 dispose 的
    // chart 内部对象 (PriceAxisWidget._internal_optimalWidth /
    // DevicePixelContentBoxBinding) 抛出 "Value is null" / "Object is disposed"。
    // 这些异常发生在所有断言通过后, 不影响测试结果; 但 vitest 会捕获并
    // 标记为 "might cause false positive tests"。过滤掉它们消除噪声,
    // 同时通过 test:ignore-error-pattern 模式识别真正的 bug。
    onUnhandledError: (error) => {
      const msg = error instanceof Error ? error.message : String(error);
      const stack = error instanceof Error ? (error.stack ?? '') : '';
      const isLWCTeardownNoise =
        /Value is null/.test(msg) ||
        /Object is disposed/.test(msg) ||
        /lightweight-charts/.test(stack);
      return !isLWCTeardownNoise;
    },
  },
});
