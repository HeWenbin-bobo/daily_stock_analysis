"""Playwright-based browser E2E smoke test for the stock analysis app.

Simulates a human user:
  1. Open http://127.0.0.1:5179
  2. Search for 600519 via the search bar
  3. Click the suggestion
  4. Verify K-line chart renders
  5. Switch period daily -> weekly -> monthly
  6. Toggle BOLL/MACD/KDJ indicators
  7. Click AI interpretation
  8. Add to watchlist
  9. Take screenshots at each step

Run: python scripts/e2e_smoke.py
"""
import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright, expect, TimeoutError as PWTimeout


FRONTEND = "http://127.0.0.1:5179"
SHOT_DIR = Path("F:/stock_analysis/v1/_e2e_screenshots")
SHOT_DIR.mkdir(parents=True, exist_ok=True)


def step(label):
    print(f"\n--- {label} ---")


def main():
    results = {"steps": [], "pass": 0, "fail": 0, "screenshots": []}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1400, "height": 900})
        page = ctx.new_page()

        page.on("console", lambda msg: print(f"  [browser:{msg.type}] {msg.text}") if msg.type in ("error", "warning") else None)

        try:
            step("1. Open frontend")
            page.goto(FRONTEND, wait_until="networkidle", timeout=30000)
            shot = SHOT_DIR / "01_home.png"
            page.screenshot(path=str(shot), full_page=True)
            results["screenshots"].append(str(shot))
            print(f"  page title: {page.title()}")
            results["steps"].append("home loaded")
            results["pass"] += 1

            step("2. Search 600519")
            search = page.get_by_role("combobox", name="搜索股票代码或名称")
            search.fill("600519")
            # wait for results
            page.wait_for_selector('[role="option"]', timeout=10000)
            shot = SHOT_DIR / "02_search.png"
            page.screenshot(path=str(shot), full_page=True)
            results["screenshots"].append(str(shot))
            results["steps"].append("search results shown")
            results["pass"] += 1

            step("3. Click suggested stock")
            page.locator('[role="option"]').first.click()
            # wait for K-line chart to render
            # iter 96: aria-label 改为动态 "K线图, 共 N 根 K 线", 用 starts-with 匹配。
            page.wait_for_selector('[aria-label^="K线图"]', timeout=15000)
            time.sleep(1.5)
            shot = SHOT_DIR / "03_kline.png"
            page.screenshot(path=str(shot), full_page=True)
            results["screenshots"].append(str(shot))
            results["steps"].append("K-line chart rendered")
            results["pass"] += 1

            step("4. Switch period weekly")
            page.get_by_role("button", name="周 K").click()
            time.sleep(1.5)
            shot = SHOT_DIR / "04_weekly.png"
            page.screenshot(path=str(shot), full_page=True)
            results["screenshots"].append(str(shot))
            results["steps"].append("weekly period")
            results["pass"] += 1

            step("5. Switch period monthly")
            page.get_by_role("button", name="月 K").click()
            time.sleep(1.5)
            shot = SHOT_DIR / "05_monthly.png"
            page.screenshot(path=str(shot), full_page=True)
            results["screenshots"].append(str(shot))
            results["steps"].append("monthly period")
            results["pass"] += 1

            step("6. Switch back to daily")
            page.get_by_role("button", name="日 K").click()
            time.sleep(1.5)
            shot = SHOT_DIR / "06_daily.png"
            page.screenshot(path=str(shot), full_page=True)
            results["screenshots"].append(str(shot))
            results["steps"].append("back to daily")
            results["pass"] += 1

            step("7. Toggle BOLL indicator")
            boll_btn = page.locator("button", has_text="BOLL").first
            boll_btn.click()
            time.sleep(1.0)
            results["steps"].append("BOLL toggled")
            results["pass"] += 1

            step("8. Toggle MACD indicator")
            macd_btn = page.locator("button", has_text="MACD").first
            macd_btn.click()
            time.sleep(1.0)
            shot = SHOT_DIR / "07_indicators.png"
            page.screenshot(path=str(shot), full_page=True)
            results["screenshots"].append(str(shot))
            results["steps"].append("MACD toggled, KDJ/BOLL visible")
            results["pass"] += 1

            step("9. Toggle KDJ indicator")
            kdj_btn = page.locator("button", has_text="KDJ").first
            kdj_btn.click()
            time.sleep(1.0)
            shot = SHOT_DIR / "08_kdj.png"
            page.screenshot(path=str(shot), full_page=True)
            results["screenshots"].append(str(shot))
            results["steps"].append("KDJ toggled")
            results["pass"] += 1

            step("10. Verify peak-trough panel visible")
            page.wait_for_selector("text=波峰波谷", timeout=5000)
            shot = SHOT_DIR / "09_peak_trough.png"
            page.screenshot(path=str(shot), full_page=True)
            results["screenshots"].append(str(shot))
            results["steps"].append("peak-trough panel rendered")
            results["pass"] += 1

            step("11. Add to watchlist")
            watchlist_btn = page.locator("button[aria-label='切换自选股状态']").first
            watchlist_btn.click()
            time.sleep(0.5)
            shot = SHOT_DIR / "10_watchlist.png"
            page.screenshot(path=str(shot), full_page=True)
            results["screenshots"].append(str(shot))
            results["steps"].append("watchlist toggled")
            results["pass"] += 1

            step("12. Theme toggle")
            theme_btn = page.locator("button[aria-label='切换主题']").first
            theme_btn.click()
            time.sleep(0.5)
            shot = SHOT_DIR / "11_theme_light.png"
            page.screenshot(path=str(shot), full_page=True)
            results["screenshots"].append(str(shot))
            results["steps"].append("theme toggle")
            results["pass"] += 1

            print("\n[OK] All steps passed")
        except PWTimeout as e:
            print(f"\n[X] TIMEOUT: {e}")
            try:
                page.screenshot(path=str(SHOT_DIR / "FAIL.png"), full_page=True)
            except Exception:
                pass
            results["fail"] += 1
            results["steps"].append(f"FAIL: {e}")
        except Exception as e:
            print(f"\n[X] ERROR: {e}")
            try:
                page.screenshot(path=str(SHOT_DIR / "FAIL.png"), full_page=True)
            except Exception:
                pass
            results["fail"] += 1
            results["steps"].append(f"FAIL: {e}")
        finally:
            browser.close()

    Path("F:/stock_analysis/v1/e2e_results.json").write_text(json.dumps(results, indent=2, ensure_ascii=False))
    print(f"\nResults written: {results['pass']} pass, {results['fail']} fail")
    print(f"Screenshots: {len(results['screenshots'])}")
    return 0 if results["fail"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
