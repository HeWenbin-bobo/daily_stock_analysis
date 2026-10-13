# -*- coding: utf-8 -*-
"""
通知推送 - 多通道 + 模板 + 重试
- 通道：企业微信 / 钉钉 / 邮件 / Server 酱
- 模板：内置 (market_review_daily / alert_triggered / backtest_done / paper_settle)
- 重试：tenacity 指数退避 3 次
- 推送历史：notification_log 表
"""
import time
import json
import logging
import smtplib
import asyncio
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Optional, Dict, Any, List
from dataclasses import dataclass
from enum import Enum

import urllib.request
import urllib.error
import urllib.parse

from db.migrate import get_conn
from core.crypto import encrypt_str, decrypt_str

logger = logging.getLogger(__name__)


class ChannelType(Enum):
    WECHAT_WORK = "wechat_work"   # 企业微信 webhook
    DINGTALK = "dingtalk"         # 钉钉 webhook
    EMAIL = "email"               # SMTP 邮件
    SERVERCHAN = "serverchan"     # Server 酱（推送到微信）


@dataclass
class NotifyResult:
    success: bool
    channel_type: str
    error_message: Optional[str] = None
    http_status: Optional[int] = None
    retry_count: int = 0


# ============================================================================
# 模板系统
# ============================================================================
TEMPLATES = {
    "market_review_daily": {
        "title": "📊 今日大盘复盘（{date}）",
        "content": """## 📊 今日大盘复盘（{date}）

**情绪评分**：{signal_score}/100（{signal_label}）

**市场宽度**：
- 上涨 {up_count} 家 / 下跌 {down_count} 家
- 涨停 {limit_up} 家 / 跌停 {limit_down} 家
- 成交额 {amount} 亿

**领涨行业 TOP 5**：
{top_industries}

**核心结论**：
{summary}

---
⚠️ 本报告由 AI 生成，仅供研究参考，不构成投资建议。
""",
    },
    "alert_triggered": {
        "title": "🚨 异动提醒：{symbol} {rule_name}",
        "content": """## 🚨 异动提醒

**股票**：{symbol} {name}
**规则**：{rule_name}
**触发值**：{metric_value}（阈值 {threshold_value}）
**触发时间**：{trigger_time}

**简评**：{commentary}

---
⚠️ 本提醒由系统自动生成，仅供参考，不构成投资建议。
""",
    },
    "backtest_done": {
        "title": "📈 回测完成：{strategy_name}",
        "content": """## 📈 回测完成

**策略**：{strategy_name}
**标的**：{symbol}
**区间**：{start_date} ~ {end_date}

**核心指标**：
- 总收益：{total_return_pct}%
- 年化：{annualized_return_pct}%
- 夏普：{sharpe_ratio}
- 最大回撤：{max_drawdown_pct}%
- 胜率：{win_rate}%
- 交易次数：{total_trades}

---
⚠️ 历史回测不代表未来收益。
""",
    },
    "paper_settle": {
        "title": "💰 模拟盘日终（{date}）",
        "content": """## 💰 模拟盘日终

**账户**：{account_name}
**总资产**：¥{total_equity:,.0f}
**持仓市值**：¥{positions_value:,.0f}
**可用现金**：¥{cash:,.0f}
**当日盈亏**：{day_pnl_pct}%

---
⚠️ 模拟交易，仅供参考。
""",
    },
}


def render_template(template_name: str, context: Dict[str, Any]) -> Dict[str, str]:
    """渲染模板 → {title, content}"""
    tpl = TEMPLATES.get(template_name)
    if not tpl:
        return {"title": context.get("title", template_name), "content": context.get("content", "")}
    # title 和 content 分别 try，互不影响
    try:
        title = tpl["title"].format(**context)
    except KeyError as e:
        logger.warning("Template %s title missing key: %s", template_name, e)
        title = tpl["title"] + f" [缺失: {e}]"
    try:
        content = tpl["content"].format(**context)
    except KeyError as e:
        logger.warning("Template %s content missing key: %s", template_name, e)
        content = tpl["content"] + f"\n\n[未填字段: {e}]"
    return {"title": title, "content": content}


# ============================================================================
# 各通道实现
# ============================================================================
def _send_wechat_work(webhook_url: str, title: str, content: str) -> NotifyResult:
    """企业微信 markdown"""
    payload = {
        "msgtype": "markdown",
        "markdown": {
            "content": f"## {title}\n\n{content}",
        },
    }
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            webhook_url,
            data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8")
            status = resp.status
            try:
                result = json.loads(body)
                if result.get("errcode", 0) != 0:
                    return NotifyResult(False, "wechat_work", error_message=body[:300], http_status=status)
            except json.JSONDecodeError:
                pass
        return NotifyResult(True, "wechat_work", http_status=status)
    except urllib.error.HTTPError as e:
        return NotifyResult(False, "wechat_work", error_message=str(e)[:300], http_status=e.code)
    except Exception as e:
        return NotifyResult(False, "wechat_work", error_message=str(e)[:300])


def _send_dingtalk(webhook_url: str, title: str, content: str) -> NotifyResult:
    """钉钉 markdown"""
    payload = {
        "msgtype": "markdown",
        "markdown": {
            "title": title,
            "text": f"## {title}\n\n{content}",
        },
    }
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            webhook_url,
            data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8")
            status = resp.status
            try:
                result = json.loads(body)
                if result.get("errcode", 0) != 0:
                    return NotifyResult(False, "dingtalk", error_message=body[:300], http_status=status)
            except json.JSONDecodeError:
                pass
        return NotifyResult(True, "dingtalk", http_status=status)
    except urllib.error.HTTPError as e:
        return NotifyResult(False, "dingtalk", error_message=str(e)[:300], http_status=e.code)
    except Exception as e:
        return NotifyResult(False, "dingtalk", error_message=str(e)[:300])


def _send_email(smtp_target: str, title: str, content: str) -> NotifyResult:
    """邮件: smtp_target = 'smtp.gmail.com:587:user:password:to@example.com'"""
    try:
        parts = smtp_target.split(":")
        if len(parts) != 5:
            return NotifyResult(False, "email", error_message="smtp_target 格式错误（需要 host:port:user:pass:to）")
        host, port, user, password, to_addr = parts
        port = int(port)
        # ★ Round 4 安全加固：拒绝指向内网/loopback 的 SMTP host，避免 SSRF
        if host.lower() in ("localhost", "127.0.0.1", "0.0.0.0", "::1") or host.startswith(("10.", "192.168.", "169.254.", "172.16.", "172.17.", "172.18.", "172.19.", "172.20.", "172.21.", "172.22.", "172.23.", "172.24.", "172.25.", "172.26.", "172.27.", "172.28.", "172.29.", "172.30.", "172.31.")):
            return NotifyResult(False, "email", error_message=f"smtp host {host} 不允许（内网地址）")
        msg = MIMEMultipart("alternative")
        msg["Subject"] = title
        msg["From"] = user
        msg["To"] = to_addr
        # 防止 HTML 注入：转义 content 中的 < > &
        safe_html = (
            content.replace("&", "&amp;")
                    .replace("<", "&lt;")
                    .replace(">", "&gt;")
        )
        html_content = f"<html><body><pre>{safe_html}</pre></body></html>"
        msg.attach(MIMEText(html_content, "html", "utf-8"))
        msg.attach(MIMEText(content, "plain", "utf-8"))

        if port == 465:
            server = smtplib.SMTP_SSL(host, port, timeout=15)
        else:
            server = smtplib.SMTP(host, port, timeout=15)
        server.ehlo()
        if port != 465:
            server.starttls()
            server.ehlo()
        server.login(user, password)
        server.sendmail(user, [to_addr], msg.as_string())
        server.quit()
        return NotifyResult(True, "email", http_status=250)
    except Exception as e:
        return NotifyResult(False, "email", error_message=str(e)[:300])


def _send_serverchan(send_key: str, title: str, content: str) -> NotifyResult:
    """Server 酱（推送到微信）"""
    url = f"https://sctapi.ftqq.com/{send_key}.send"
    payload = {"title": title, "desp": content}
    try:
        data = urllib.parse.urlencode(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8")
            status = resp.status
            try:
                result = json.loads(body)
                if result.get("code", 0) != 0:
                    return NotifyResult(False, "serverchan", error_message=body[:300], http_status=status)
            except json.JSONDecodeError:
                pass
        return NotifyResult(True, "serverchan", http_status=status)
    except urllib.error.HTTPError as e:
        return NotifyResult(False, "serverchan", error_message=str(e)[:300], http_status=e.code)
    except Exception as e:
        return NotifyResult(False, "serverchan", error_message=str(e)[:300])


# ============================================================================
# 推送调度（含重试）
# ============================================================================
def send(
    channel_id: int,
    *,
    template_name: Optional[str] = None,
    title: Optional[str] = None,
    content: Optional[str] = None,
    context: Optional[Dict[str, Any]] = None,
    max_retries: int = 3,
) -> NotifyResult:
    """
    推送单条消息
    - channel_id: 来自 notification_channel 表
    - template_name 或直接 title+content
    """
    channel = _get_channel(channel_id)
    if not channel:
        return NotifyResult(False, "unknown", error_message=f"channel {channel_id} 不存在")

    if not channel["enabled"]:
        return NotifyResult(False, channel["channel_type"], error_message="channel disabled")

    # 渲染模板
    if template_name:
        ctx = context or {}
        rendered = render_template(template_name, ctx)
        title = rendered["title"]
        content = rendered["content"]

    if not title or not content:
        return NotifyResult(False, channel["channel_type"], error_message="title/content 缺失")

    # 检查 quiet hours
    if _in_quiet_hours(channel):
        return _record_and_return(channel, title, content, template_name, NotifyResult(
            False, channel["channel_type"], error_message="quiet hours"
        ))

    # ★ Round 4 修复：强制走 target_encrypted，不再 fallback 到明文 target 列
    #   （如果未来迁移老数据：明文列可能仍有内容，临时兼容一下）
    target = decrypt_str(channel.get("target_encrypted", ""))
    if not target:
        target = channel.get("target", "")  # 兼容老数据（迁移完后可移除）
    if not target:
        return NotifyResult(False, channel["channel_type"], error_message="target 缺失（未解密成功）")

    # 重试 3 次（指数退避 1s, 2s, 4s）
    last_result = None
    for attempt in range(max_retries):
        last_result = _dispatch(channel["channel_type"], target, title, content)
        last_result.retry_count = attempt
        if last_result.success:
            break
        if attempt < max_retries - 1:
            time.sleep(2 ** attempt)

    return _record_and_return(channel, title, content, template_name, last_result)


def _dispatch(channel_type: str, target: str, title: str, content: str) -> NotifyResult:
    """分发到具体通道"""
    if channel_type == ChannelType.WECHAT_WORK.value:
        return _send_wechat_work(target, title, content)
    elif channel_type == ChannelType.DINGTALK.value:
        return _send_dingtalk(target, title, content)
    elif channel_type == ChannelType.EMAIL.value:
        return _send_email(target, title, content)
    elif channel_type == ChannelType.SERVERCHAN.value:
        return _send_serverchan(target, title, content)
    else:
        return NotifyResult(False, channel_type, error_message=f"unknown channel type: {channel_type}")


def _record_and_return(channel, title, content, template_name, result: NotifyResult) -> NotifyResult:
    """写推送历史 + 返回结果"""
    try:
        conn = get_conn()
        conn.execute(
            """INSERT INTO notification_log
               (channel_id, user_id, title, content, template_name,
                related_resource_type, related_resource_id,
                status, retry_count, error_message, sent_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                channel["id"], channel["user_id"], title, content, template_name,
                None, None,
                "success" if result.success else ("skipped" if "quiet" in (result.error_message or "") else "failed"),
                result.retry_count,
                result.error_message,
                time.time() if result.success else None,
                time.time(),
            ),
        )
    except Exception as e:
        logger.warning("Failed to record notification log: %s", e)
    return result


def _get_channel(channel_id: int) -> Optional[Dict[str, Any]]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM notification_channel WHERE id=?", (channel_id,))
    row = cur.fetchone()
    return dict(row) if row else None


def _in_quiet_hours(channel: Dict[str, Any]) -> bool:
    """检查是否在静默时段"""
    start = channel.get("quiet_hours_start")
    end = channel.get("quiet_hours_end")
    if not start or not end:
        return False
    from datetime import datetime
    now = datetime.now().strftime("%H:%M")
    # 支持跨天（如 22:00 → 08:00）
    if start <= end:
        return start <= now <= end
    else:
        return now >= start or now <= end


# ============================================================================
# 通道管理 API
# ============================================================================
def _mask_channel_target(channel_type: str, raw: str) -> str:
    """针对不同通道类型做合理掩码。

    ★ Round 4 修复：原实现对所有通道都用 "***" + 后 4 位，但对 SMTP
    'host:port:user:pass:to@example.com' 仍然暴露前段（host/user/port）。
    现按通道类型分别掩码：
    - email：保留 '***@example.com'
    - wechat_work / dingtalk / serverchan：保留 URL 前缀 + "***" + 后 6 位
    """
    if not raw:
        return ""
    if channel_type == ChannelType.EMAIL.value:
        # 找最后一个 '@'（如果 user@host 形式）；否则整体 ***
        if "@" in raw:
            return "***@" + raw.split("@")[-1]
        return "***"
    # webhook URL：保留 scheme + "://" + "***" + 后 6 位
    for sep in ("://",):
        idx = raw.find(sep)
        if idx > 0:
            head = raw[: idx + len(sep)]
            tail = raw[-6:] if len(raw) > 6 + len(head) + 3 else ""
            return f"{head}***{tail}"
    return "***" + raw[-4:] if len(raw) > 4 else "***"


def add_channel(
    user_id: str,
    channel_type: str,
    target: str,
    *,
    enabled: bool = True,
    quiet_hours_start: Optional[str] = None,
    quiet_hours_end: Optional[str] = None,
) -> int:
    """添加推送通道（target 加密存储，不留明文列）。"""
    if not target:
        raise ValueError("target 不能为空")
    valid_types = {c.value for c in ChannelType}
    if channel_type not in valid_types:
        raise ValueError(f"channel_type 必须是 {valid_types} 之一")
    conn = get_conn()
    encrypted = encrypt_str(target)
    cur = conn.cursor()
    now = time.time()
    try:
        # ★ Round 4 安全修复：不再写入 plaintext target 列，避免 DB dump / 备份泄露 webhook URL / SMTP 密码
        cur.execute(
            """INSERT INTO notification_channel
               (user_id, channel_type, target, target_encrypted, enabled,
                quiet_hours_start, quiet_hours_end, created_at, updated_at)
               VALUES (?, ?, '', ?, ?, ?, ?, ?, ?)""",
            (user_id, channel_type, encrypted, 1 if enabled else 0,
             quiet_hours_start, quiet_hours_end, now, now),
        )
        return cur.lastrowid or 0
    except Exception as e:
        # unique 冲突 → update
        if "UNIQUE" in str(e):
            cur.execute(
                """UPDATE notification_channel SET target_encrypted=?, enabled=?,
                   quiet_hours_start=?, quiet_hours_end=?, updated_at=?
                   WHERE user_id=? AND channel_type=?""",
                (encrypted, 1 if enabled else 0,
                 quiet_hours_start, quiet_hours_end, now, user_id, channel_type),
            )
            cur.execute("SELECT id FROM notification_channel WHERE user_id=? AND channel_type=?",
                        (user_id, channel_type))
            return cur.fetchone()["id"]
        raise


def list_channels(user_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """列出所有通道 — ★ Round 4 修复：从已存在的明文 target 列做合理掩码。"""
    conn = get_conn()
    cur = conn.cursor()
    if user_id:
        cur.execute("SELECT * FROM notification_channel WHERE user_id=?", (user_id,))
    else:
        cur.execute("SELECT * FROM notification_channel")
    rows = []
    for r in cur.fetchall():
        d = dict(r)
        d["target"] = _mask_channel_target(d.get("channel_type", ""), d.get("target", ""))
        d["target_encrypted"] = "[ENCRYPTED]"
        rows.append(d)
    return rows


def delete_channel(channel_id: int) -> bool:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM notification_channel WHERE id=?", (channel_id,))
    return cur.rowcount > 0


def list_logs(
    user_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    conn = get_conn()
    cur = conn.cursor()
    sql = "SELECT * FROM notification_log WHERE 1=1"
    params: list = []
    if user_id:
        sql += " AND user_id=?"
        params.append(user_id)
    if status:
        sql += " AND status=?"
        params.append(status)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    cur.execute(sql, params)
    return [dict(r) for r in cur.fetchall()]