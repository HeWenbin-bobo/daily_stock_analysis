import { describe, expect, it } from 'vitest';
import { parseApiError } from '../error';

// HTTP 503 is shared by two very different situations: the backend cannot reach
// an upstream dependency, and the backend is healthy but lacks a required local
// capability (e.g. the share-image renderer is not installed). The backend names
// the real cause in its payload, so the classifier must not overwrite it with a
// proxy/DNS guess that sends users chasing a network problem that does not exist.
function serviceUnavailable(payload: Record<string, unknown>) {
  return {
    isAxiosError: true,
    message: 'Request failed with status code 503',
    response: { status: 503, statusText: 'Service Unavailable', data: payload },
  };
}

describe('parseApiError 503 classification', () => {
  it('surfaces the backend capability failure instead of guessing proxy or DNS', () => {
    const parsed = parseApiError(
      serviceUnavailable({
        error: 'share_image_unavailable',
        message: '分享图片生成失败，请检查 wkhtmltoimage 转图工具是否已安装并可用',
      }),
    );

    expect(parsed.category).toBe('server_capability');
    expect(parsed.message).toContain('wkhtmltoimage');
    expect(parsed.message).not.toContain('代理');
    expect(parsed.message).not.toContain('DNS');
  });

  it('still reports a genuine upstream 503 as a network problem', () => {
    const parsed = parseApiError(
      serviceUnavailable({ error: 'upstream_unavailable', message: 'upstream dependency down' }),
    );

    expect(parsed.category).toBe('upstream_network');
    expect(parsed.message).toContain('代理');
  });

  it('treats an unlabelled 503 as an upstream network problem', () => {
    const parsed = parseApiError(serviceUnavailable({}));

    expect(parsed.category).toBe('upstream_network');
  });
});
