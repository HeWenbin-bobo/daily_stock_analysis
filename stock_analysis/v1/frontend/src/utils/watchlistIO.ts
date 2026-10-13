/**
 * 自选股导入/导出工具。
 *
 * ## 导入规则 (importWatchlistFile)
 * - 文件大小上限: 1 MiB (MAX_FILE_BYTES)
 * - 单文件最多条目数: 500 (MAX_ITEMS)
 * - 股票代码长度: 1~32 字符 (MAX_CODE_LENGTH)
 * - 股票名称长度: 1~64 字符 (MAX_NAME_LENGTH)
 * - 市场必须为 'cn' | 'hk' | 'us' (VALID_MARKETS)
 *
 * ## JSON 格式支持
 * 支持两种格式:
 * 1. `{ items: [...] }` — 含 items 字段的对象
 * 2. `[...]` — 直接为数组
 *
 * ## 错误码 (WatchlistImportError.code)
 * | code              | 含义                          |
 * | ----------------- | ----------------------------- |
 * | FILE_TOO_LARGE    | 文件超过 1 MiB                |
 * | FILE_EMPTY        | 文件为空                      |
 * | INVALID_JSON      | 不是合法 JSON                 |
 * | INVALID_STRUCTURE | JSON 结构不合法                |
 * | TOO_MANY_ITEMS    | 条目数超过 500                |
 * | NO_VALID_ITEMS    | 无有效条目 (含无效/重复过滤后) |
 *
 * ## 导出格式 (exportWatchlist)
 * JSON 结构: `{ exported_at: ISO8601, count: number, items: StockSearchItem[] }`
 * 文件名格式: `watchlist_YYYYMMDD_HHMMSS.json`
 */
import type { StockSearchItem } from '../types';
import { downloadBlob } from './downloadBlob';

// 限制常量集中在此, 便于审阅/调整
const MAX_FILE_BYTES = 1 * 1024 * 1024;       // 1 MiB 上限
const MAX_ITEMS = 500;                        // 单文件最多条目数
const MAX_CODE_LENGTH = 32;                   // 股票代码最大长度
const MAX_NAME_LENGTH = 64;                   // 股票名称最大长度
const VALID_MARKETS = ['cn', 'hk', 'us'] as const;
type ValidMarket = typeof VALID_MARKETS[number];

// iter 107: 错误码 discriminator — 之前 importWatchlistFile 抛普通 Error, Watchlist
// 统一回 "文件格式不符合要求"。现在带 code, 上层可以给特定提示 (e.g. "文件超过 1 MiB")。
// 内部 detail 字段仍保留完整信息供 console.error (DEV 守卫) 排查。
export type WatchlistImportErrorCode =
  | "FILE_TOO_LARGE"
  | "FILE_EMPTY"
  | "INVALID_JSON"
  | "INVALID_STRUCTURE"
  | "TOO_MANY_ITEMS"
  | "NO_VALID_ITEMS";

export class WatchlistImportError extends Error {
  code: WatchlistImportErrorCode;
  constructor(code: WatchlistImportErrorCode, detail: string) {
    super(detail);
    this.name = "WatchlistImportError";
    this.code = code;
  }
}

/** 导出当前自选股为 JSON 文件 */
export function exportWatchlist(items: StockSearchItem[]): void {
  // iter 92: 单一 now — 之前 exported_at 和文件名 timestamp 各取一次,
  // 跨秒边界时 JSON 内的 ISO 时间与文件名时间戳会不一致。
  const now = new Date();
  const payload = {
    exported_at: now.toISOString(),
    count: items.length,
    items,
  };
  const blob = new Blob([JSON.stringify(payload, null, 2)], {
    type: "application/json;charset=utf-8",
  });
  const ts = now
    .toISOString()
    .replace(/[-:]/g, "")
    .replace(/\..+/, "")
    .replace("T", "_");
  downloadBlob(blob, `watchlist_${ts}.json`);
}

function isValidItem(item: unknown): item is StockSearchItem {
  if (!item || typeof item !== "object") return false;
  const it = item as Partial<StockSearchItem>;
  if (typeof it.code !== "string" || typeof it.name !== "string") return false;
  if (!(VALID_MARKETS as readonly string[]).includes(it.market as string)) return false;
  if (it.code.length === 0 || it.code.length > MAX_CODE_LENGTH) return false;
  if (it.name.length === 0 || it.name.length > MAX_NAME_LENGTH) return false;
  return true;
}

/** 解析用户上传的文件, 返回自选股数组 (去重 + 限额) */
export async function importWatchlistFile(file: File): Promise<StockSearchItem[]> {
  // 1. 文件大小防护
  if (file.size > MAX_FILE_BYTES) {
    throw new WatchlistImportError(
      "FILE_TOO_LARGE",
      `文件过大 (${(file.size / 1024 / 1024).toFixed(2)} MiB > 上限 ${(MAX_FILE_BYTES / 1024 / 1024).toFixed(0)} MiB)`,
    );
  }
  if (file.size === 0) {
    throw new WatchlistImportError("FILE_EMPTY", "文件为空");
  }

  const text = await file.text();

  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    throw new WatchlistImportError("INVALID_JSON", "文件不是合法的 JSON");
  }

  // 多种格式支持:
  // 1. { items: [...] }
  // 2. 直接是 [...]
  let rawItems: unknown[];
  if (Array.isArray(parsed)) {
    rawItems = parsed;
  } else if (
    parsed &&
    typeof parsed === "object" &&
    "items" in parsed &&
    Array.isArray((parsed as { items: unknown }).items)
  ) {
    rawItems = (parsed as { items: unknown[] }).items;
  } else {
    throw new WatchlistImportError(
      "INVALID_STRUCTURE",
      "JSON 结构不合法 (需要 {items:[...]} 或 [...])",
    );
  }

  // 2. 条目数量防护
  if (rawItems.length > MAX_ITEMS) {
    throw new WatchlistImportError(
      "TOO_MANY_ITEMS",
      `条目数超出限制 (${rawItems.length} > 上限 ${MAX_ITEMS})`,
    );
  }

  // 校验每个条目 + 3. 去重 (按 code + market)
  const seen = new Set<string>();
  const valid: StockSearchItem[] = [];
  let invalidCount = 0;
  let duplicateCount = 0;
  for (const item of rawItems) {
    if (!isValidItem(item)) {
      invalidCount++;
      continue;
    }
    const key = `${item.market}:${item.code}`;
    if (seen.has(key)) {
      duplicateCount++;
      continue;
    }
    seen.add(key);
    valid.push({ code: item.code, name: item.name, market: item.market as ValidMarket });
  }

  if (valid.length === 0) {
    throw new WatchlistImportError(
      "NO_VALID_ITEMS",
      `文件中无有效条目 (无效项: ${invalidCount}${duplicateCount > 0 ? `, 重复项: ${duplicateCount}` : ""})`,
    );
  }

  return valid;
}