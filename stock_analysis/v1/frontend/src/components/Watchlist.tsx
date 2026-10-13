/**
 * 自选股管理面板。数据通过 zustand store 持久化到 localStorage (iter 94 Set-backed has()),
 * 导入/导出使用 WatchlistImportError 类型化错误 (iter 107)。星标图标
 * 高亮当前选中股票, role=group 标识列表 (iter 92), focus-visible:ring-2 键盘焦点样式 (iter 117)。
 */
import { memo, useEffect, useRef, useState, type ChangeEvent } from 'react';
import type { Market, StockSearchItem } from '../types';
import { marketColor, marketLabel } from '../utils/market';
import { exportWatchlist, importWatchlistFile, WatchlistImportError } from '../utils/watchlistIO';

interface Props {
  items: StockSearchItem[];
  onSelect: (item: StockSearchItem) => void;
  // iter 110: market 收紧到 Market 联合类型 — 与 useWatchlist.remove/has 对齐,
  // 让 onRemove 调用方拿到的就是已校验类型, 不需要二次 narrowing。
  onRemove: (code: string, market: Market) => void;
  onImport?: (items: StockSearchItem[]) => void;
  currentCode?: string;
  // iter 133: currentMarket 收紧到 Market 联合类型 — 与 useWatchlist / App.tsx 调用点对齐,
  // 之前 `string` 接受任何字符串, 即使 store 永远是 Market 也让 prop contract 比必要更宽。
  currentMarket?: Market;
}

/**
 * 把导入错误映射为用户友好提示 — 不暴露实现细节 (e.g. "MiB 上限" / "JSON 结构"),
 * 只告诉用户"是什么问题 + 怎么解决"。具体技术原因仍可在 console.error (DEV 守卫) 看。
 */
function importErrorMessage(err: unknown): string {
  if (err instanceof WatchlistImportError) {
    switch (err.code) {
      case "FILE_TOO_LARGE":
        return "导入失败: 文件超过 1 MiB 上限, 请精简后再试";
      case "FILE_EMPTY":
        return "导入失败: 文件为空";
      case "INVALID_JSON":
        return "导入失败: 文件不是合法的 JSON";
      case "INVALID_STRUCTURE":
        return "导入失败: JSON 结构不符合要求, 需要 {items: [...]} 或 [...]";
      case "TOO_MANY_ITEMS":
        return "导入失败: 条目数超过 500 上限";
      case "NO_VALID_ITEMS":
        return "导入失败: 文件中无有效条目, 请检查 code/name/market 字段";
      default:
        return "导入失败: 文件格式不符合要求, 请检查 JSON 结构";
    }
  }
  return "导入失败: 文件格式不符合要求, 请检查 JSON 结构";
}

export const Watchlist = memo(function Watchlist({ items, onSelect, onRemove, onImport, currentCode, currentMarket }: Props) {
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [importStatus, setImportStatus] = useState<{
    kind: 'ok' | 'err';
    msg: string;
  } | null>(null);

  // iter 87: 4s 自动关闭 import 提示 — 之前 banner 永久挂载, 用户切走再回来还能看到
  // 过时的"导入成功"提示。错误提示 给 6s (用户要看清错误信息), 成功 给 4s。
  useEffect(() => {
    if (!importStatus) return;
    const delay = importStatus.kind === 'err' ? 6000 : 4000;
    const t = window.setTimeout(() => setImportStatus(null), delay);
    return () => window.clearTimeout(t);
  }, [importStatus]);

  const handleImportClick = () => {
    fileInputRef.current?.click();
  };

  // iter 79: 导入请求 generation token — 防止快速连续选两次文件时,
  // 旧 import 完成回写 setImportStatus 覆盖新文件的结果。
  const importTokenRef = useRef(0);
  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const handleFileChange = async (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    // iter 93: 即使 onImport 缺失或 file 为空, 也要清空 input — 否则下一次选同文件
    // 不会触发 change 事件, 用户得手动 cancel 才能重新选。
    if (!file) {
      if (fileInputRef.current) fileInputRef.current.value = "";
      return;
    }
    if (!onImport) {
      if (fileInputRef.current) fileInputRef.current.value = "";
      return;
    }
    const token = ++importTokenRef.current;
    try {
      const imported = await importWatchlistFile(file);
      if (token !== importTokenRef.current || !mountedRef.current) return;
      onImport(imported);
      setImportStatus({ kind: 'ok', msg: `成功导入 ${imported.length} 支股票` });
    } catch (err: unknown) {
      if (token !== importTokenRef.current || !mountedRef.current) return;
      // iter 107: 之前统一回 "文件格式不符合要求", 所有错误混在一起,
      // 用户看到 "格式不对" 但实际是文件过大 / 条目超限 / JSON 不合法 等不同问题。
      // 用 WatchlistImportError.code 区分, 给特定提示。
      // 开发者在 DevTools 仍可见完整堆栈 (DEV 守卫)。
      if (import.meta.env.DEV) {
        console.error("watchlist import failed:", err);
      }
      setImportStatus({ kind: 'err', msg: importErrorMessage(err) });
    } finally {
      // 清空 input, 否则重复选同一文件不会触发 change
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  };
  // 空状态: 提示用户如何添加自选, 而不是悄悄什么都不渲染
  // iter 120: 加 tabIndex={0} — 让键盘用户 Tab 能聚焦空状态容器, 屏幕阅读器
  // 也能直接进入 status 区域读出"自选股为空"提示。autofocus 不可用 (组件挂载时
  // 用户不一定在 watchlist 区域, 强行聚焦会打扰), tabIndex={0} 是更克制的可访问性。
  if (items.length === 0) {
    return (
      <div
        className="mt-8 w-full max-w-2xl bg-panel border border-dashed border-border rounded-lg p-6 text-center"
        role="status"
        tabIndex={0}
      >
        <svg className="w-8 h-8 text-slate-500 mx-auto mb-3" fill="currentColor" viewBox="0 0 24 24" aria-hidden="true">
          <path d="M11.049 2.927c.3-.921 1.603-.921 1.902 0l1.519 4.674a1 1 0 00.95.69h4.915c.969 0 1.371 1.24.588 1.81l-3.976 2.888a1 1 0 00-.363 1.118l1.518 4.674c.3.922-.755 1.688-1.538 1.118l-3.976-2.888a1 1 0 00-1.176 0l-3.976 2.888c-.783.57-1.838-.197-1.538-1.118l1.518-4.674a1 1 0 00-.363-1.118l-3.976-2.888c-.784-.57-.38-1.81.588-1.81h4.914a1 1 0 00.951-.69l1.519-4.674z" />
        </svg>
        <h3 className="text-sm font-semibold text-slate-300 mb-1">自选股为空</h3>
        <p className="text-xs text-slate-400">
          在上方搜索股票, 选中后点击卡片右上角的星标即可加入自选
        </p>
        {onImport && (
          <button
            type="button"
            onClick={handleImportClick}
            className="mt-3 text-xs px-3 py-1 bg-slate-700/50 hover:bg-slate-700 text-slate-300 rounded transition-colors
                       focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500"
            aria-label="从 JSON 文件导入自选股"
          >
            或从 JSON 文件导入
          </button>
        )}
      </div>
    );
  }

  return (
    <div className="mt-8 w-full max-w-2xl">
      <h3 className="text-sm font-semibold text-slate-400 mb-3 flex items-center gap-2">
        <svg className="w-4 h-4 text-yellow-400" fill="currentColor" viewBox="0 0 24 24">
          <path d="M11.049 2.927c.3-.921 1.603-.921 1.902 0l1.519 4.674a1 1 0 00.95.69h4.915c.969 0 1.371 1.24.588 1.81l-3.976 2.888a1 1 0 00-.363 1.118l1.518 4.674c.3.922-.755 1.688-1.538 1.118l-3.976-2.888a1 1 0 00-1.176 0l-3.976 2.888c-.783.57-1.838-.197-1.538-1.118l1.518-4.674a1 1 0 00-.363-1.118l-3.976-2.888c-.784-.57-.38-1.81.588-1.81h4.914a1 1 0 00.951-.69l1.519-4.674z" />
        </svg>
        我的自选 ({items.length})
        <div className="ml-auto flex gap-1">
          {onImport && (
            <>
              <button
                type="button"
                onClick={handleImportClick}
                className="text-xs px-2 py-0.5 bg-slate-700/50 hover:bg-slate-700 text-slate-300 rounded transition-colors flex items-center gap-1"
                title="导入 JSON"
                aria-label="导入自选股"
              >
                <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                    d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-8l-4-4m0 0L8 8m4-4v12" />
                </svg>
                导入
              </button>
              <input
                ref={fileInputRef}
                type="file"
                accept="application/json,.json"
                className="hidden"
                onChange={handleFileChange}
                aria-hidden="true"
              />
            </>
          )}
          {items.length > 0 && (
            <button
              type="button"
              onClick={() => exportWatchlist(items)}
              className="text-xs px-2 py-0.5 bg-slate-700/50 hover:bg-slate-700 text-slate-300 rounded transition-colors flex items-center gap-1"
              title="导出为 JSON"
              // iter 101: 动态 aria-label — 与 PeakTroughPanel 导出按钮保持一致 (iter 100),
              // 让屏幕阅读器知道会导出多少支股票。
              aria-label={`导出自选股 (${items.length} 支) 为 JSON`}
            >
              <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                  d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" />
              </svg>
              导出
            </button>
          )}
        </div>
      </h3>
      {importStatus && (
        <div
          role={importStatus.kind === 'err' ? 'alert' : 'status'}
          aria-live={importStatus.kind === 'err' ? 'assertive' : 'polite'}
          className={`mb-2 text-xs px-2 py-1 rounded flex items-center justify-between gap-2 ${
            importStatus.kind === 'ok'
              ? 'bg-green-900/30 text-green-300 border border-green-800/50'
              : 'bg-red-900/30 text-red-300 border border-red-800/50'
          }`}
        >
          <span>{importStatus.msg}</span>
          {/* iter 102: 关闭按钮对成功/错误 banner 都显示 — 之前只有错误 banner 能手动关闭,
          4s 自动关闭对部分用户太快。让用户随时主动关闭, 与 App.tsx 主错误 banner 模式一致。 */}
          <button
            type="button"
            onClick={() => setImportStatus(null)}
            className="text-slate-400 hover:text-slate-200 rounded
                       focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500"
            aria-label="关闭提示"
          >
            <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>
      )}
      <div className="flex gap-2 flex-wrap justify-center" role="group" aria-label="自选股列表">
        {items.map((item) => {
          const isCurrent = item.code === currentCode && item.market === currentMarket;
          return (
            <div
              key={`${item.market}-${item.code}`}
              className={`group flex items-center gap-2 pl-3 pr-1 py-1.5 rounded-lg border transition-colors ${
                isCurrent
                  ? 'bg-blue-900/30 border-blue-500/60'
                  : 'bg-panel border-border hover:border-blue-500/50'
              }`}
            >
              <button
                type="button"
                onClick={() => onSelect(item)}
                className="flex items-center gap-2 hover:text-blue-300 focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 rounded"
                title={`切换到 ${item.name}`}
              >
                <span className={`${marketColor(item.market)} text-white text-[10px] px-1.5 py-0.5 rounded`}>
                  {marketLabel(item.market)}
                </span>
                <span className="text-sm text-slate-200 font-medium">{item.name}</span>
                <span className="text-xs text-slate-400 font-mono">{item.code}</span>
              </button>
              <button
                type="button"
                onClick={() => onRemove(item.code, item.market)}
                className="p-2 rounded text-slate-400 hover:text-red-300 hover:bg-slate-700/50 transition-colors
                           focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500"
                title="移除"
                // iter 112: 用 marketLabel (A股/港股/美股) 而非原始 market 字符串 (cn/hk/us) —
                // 屏幕阅读器报"贵州茅台 (A股)"比"贵州茅台 (cn)"更符合视觉 badge 文案。
                aria-label={`移除 ${item.name} (${marketLabel(item.market)})`}
              >
                <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                    d="M6 18L18 6M6 6l12 12" />
                </svg>
              </button>
            </div>
          );
        })}
      </div>
    </div>
  );
});
