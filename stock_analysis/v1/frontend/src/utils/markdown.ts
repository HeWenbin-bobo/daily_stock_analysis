/** 极简 Markdown 渲染器, 用于 LLM 回复。

支持的语法(最小子集):
  - 段落(空行分隔)
  - **粗体** / *斜体*
  - `代码`
  - # / ## / ### 标题
  - - / * / 数字. 无序/有序列表
  - > 引用

不依赖任何外部包, 体积小。
*/

function escapeHtml(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

/** 行内格式化: 处理 bold/italic/code/links */
function inline(s: string): string {
  let out = escapeHtml(s);
  // 代码(优先于其他)
  out = out.replace(/`([^`]+)`/g, '<code class="px-1 py-0.5 bg-slate-700/60 rounded text-sm font-mono">$1</code>');
  // 粗体
  out = out.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  // 斜体
  out = out.replace(/(^|[^*])\*([^*]+)\*(?!\*)/g, '$1<em>$2</em>');
  // 链接 [text](url)
  out = out.replace(
    /\[([^\]]+)\]\((https?:\/\/[^\)]+)\)/g,
    (_match, text, url) => {
      // 只允许 http/https  scheme (已由正则保证), 并转义引号防止 href 属性被截断
      const safeUrl = String(url)
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
      return `<a href="${safeUrl}" target="_blank" rel="noopener noreferrer" class="text-blue-400 hover:underline">${text}</a>`;
    }
  );
  return out;
}

export function renderMarkdown(text: string): string {
  if (!text) return "";
  const lines = text.split(/\r?\n/);
  const out: string[] = [];

  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (line === undefined) break; // iter 71: noUncheckedIndexedAccess 兜底

    // 跳过空行
    if (!line.trim()) {
      i++;
      continue;
    }

    // 标题
    const h = /^(#{1,3})\s+(.+)$/.exec(line);
    if (h && h[1] && h[2]) {
      const level = h[1].length;
      const cls =
        level === 1
          ? "text-xl font-bold mt-3 mb-2"
          : level === 2
            ? "text-lg font-semibold mt-3 mb-1.5"
            : "text-base font-semibold mt-2 mb-1";
      out.push(`<h${level} class="${cls}">${inline(h[2])}</h${level}>`);
      i++;
      continue;
    }

    // 引用
    if (line.startsWith("> ")) {
      const block: string[] = [];
      while (i < lines.length) {
        const ln = lines[i];
        if (ln === undefined || !ln.startsWith("> ")) break;
        block.push(ln.slice(2));
        i++;
      }
      out.push(
        `<blockquote class="border-l-4 border-blue-500/50 pl-3 my-2 text-slate-300 italic">${inline(block.join(" "))}</blockquote>`
      );
      continue;
    }

    // 无序列表
    if (/^[-*]\s+/.test(line)) {
      const items: string[] = [];
      while (i < lines.length) {
        const ln = lines[i];
        if (ln === undefined || !/^[-*]\s+/.test(ln)) break;
        items.push(ln.replace(/^[-*]\s+/, ""));
        i++;
      }
      out.push(
        `<ul class="list-disc list-inside my-2 space-y-1">${items
          .map((x) => `<li>${inline(x)}</li>`)
          .join("")}</ul>`
      );
      continue;
    }

    // 有序列表
    if (/^\d+\.\s+/.test(line)) {
      const items: string[] = [];
      while (i < lines.length) {
        const ln = lines[i];
        if (ln === undefined || !/^\d+\.\s+/.test(ln)) break;
        items.push(ln.replace(/^\d+\.\s+/, ""));
        i++;
      }
      out.push(
        `<ol class="list-decimal list-inside my-2 space-y-1">${items
          .map((x) => `<li>${inline(x)}</li>`)
          .join("")}</ol>`
      );
      continue;
    }

    // 普通段落(合并连续非空行)
    const para: string[] = [];
    while (i < lines.length) {
      const ln = lines[i];
      if (ln === undefined || !ln.trim() || /^(#{1,3}\s|[-*]\s|\d+\.\s|>\s)/.test(ln)) break;
      para.push(ln);
      i++;
    }
    if (para.length > 0) {
      out.push(`<p class="my-2 leading-relaxed">${inline(para.join(" "))}</p>`);
    }
  }

  return out.join("");
}
