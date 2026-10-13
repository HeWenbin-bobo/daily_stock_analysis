import { describe, expect, it } from 'vitest';
import { renderMarkdown } from '../utils/markdown';

describe('renderMarkdown', () => {
  it('renders empty string', () => {
    expect(renderMarkdown('')).toBe('');
  });

  it('renders plain text as paragraph', () => {
    const html = renderMarkdown('hello world');
    expect(html).toContain('<p');
    expect(html).toContain('hello world');
  });

  it('renders bold **text**', () => {
    const html = renderMarkdown('这是 **重要** 的内容');
    expect(html).toContain('<strong>重要</strong>');
    expect(html).not.toContain('**');  // ** 应被替换
  });

  it('renders H1 / H2 / H3 headings', () => {
    const html = renderMarkdown('# 一级\n## 二级\n### 三级');
    expect(html).toContain('<h1');
    expect(html).toContain('一级');
    expect(html).toContain('<h2');
    expect(html).toContain('二级');
    expect(html).toContain('<h3');
    expect(html).toContain('三级');
  });

  it('renders unordered lists', () => {
    const html = renderMarkdown('- 项目 1\n- 项目 2');
    expect(html).toContain('<ul');
    expect(html).toContain('<li>项目 1</li>');
    expect(html).toContain('<li>项目 2</li>');
  });

  it('renders ordered lists', () => {
    const html = renderMarkdown('1. 第一\n2. 第二');
    expect(html).toContain('<ol');
    expect(html).toContain('<li>第一</li>');
    expect(html).toContain('<li>第二</li>');
  });

  it('renders blockquotes', () => {
    const html = renderMarkdown('> 引用文字');
    expect(html).toContain('<blockquote');
    expect(html).toContain('引用文字');
  });

  it('renders inline `code`', () => {
    const html = renderMarkdown('执行 `npm install` 命令');
    expect(html).toContain('<code');
    expect(html).toContain('npm install');
  });

  it('escapes HTML to prevent XSS', () => {
    const html = renderMarkdown('<script>alert("xss")</script>');
    expect(html).not.toContain('<script>');  // 不应该是真正的 script 标签
    expect(html).toContain('&lt;script&gt;');
  });

  it('renders links as anchors', () => {
    const html = renderMarkdown('访问 [官网](https://example.com)');
    expect(html).toContain('<a');
    expect(html).toContain('href="https://example.com"');
    expect(html).toContain('target="_blank"');
  });

  it('escapes double quotes in link URLs to prevent XSS', () => {
    const html = renderMarkdown('[点击](https://example.com" onclick="alert(1))');
    expect(html).toContain('href="https://example.com&quot; onclick=&quot;alert(1"');
    expect(html).not.toContain('onclick="alert(1)"');
  });

  it('escapes single quotes in link URLs to prevent XSS', () => {
    const html = renderMarkdown('[悬停](https://example.com\' onmouseover=\'alert(1))');
    expect(html).toContain('href="https://example.com&#39; onmouseover=&#39;alert(1"');
    expect(html).not.toContain("onmouseover='alert(1)'");
  });

  it('does not render non-http schemes as links', () => {
    const html = renderMarkdown('[恶意](javascript:alert(1))');
    expect(html).not.toContain('<a');
    expect(html).not.toContain('href=');
    expect(html).toContain('javascript:alert(1)');
  });

  it('renders a realistic LLM response', () => {
    const md = `该股当前处于 **下跌趋势**, 主要风险:

- 高位回调幅度较大
- 成交量未放大

> 建议: 等待止跌信号

当前估值处于历史 30% 分位, 适合中长期布局。`;

    const html = renderMarkdown(md);
    expect(html).toContain('<strong>下跌趋势</strong>');
    expect(html).toContain('<ul');
    expect(html).toContain('<li>高位回调幅度较大</li>');
    expect(html).toContain('<blockquote');
    expect(html).toContain('建议: 等待止跌信号');
    expect(html).toContain('<p');
  });
});
