CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT,
    action TEXT NOT NULL,
    resource_type TEXT,
    resource_id TEXT,
    ip_address TEXT,
    user_agent TEXT,
    request_body TEXT,
    status TEXT DEFAULT 'success',
    error_message TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_log(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action, created_at);

CREATE TABLE IF NOT EXISTS notification_channel (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    channel_type TEXT NOT NULL,
    target TEXT NOT NULL,
    target_encrypted TEXT,
    enabled INTEGER DEFAULT 1,
    quiet_hours_start TEXT,
    quiet_hours_end TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(user_id, channel_type)
);
-- ★ Round 4 索引：alerts_engine 按 (user_id, enabled) 批量查推送通道
CREATE INDEX IF NOT EXISTS idx_notif_channel_user_enabled ON notification_channel(user_id, enabled);

CREATE TABLE IF NOT EXISTS notification_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER,
    user_id TEXT,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    template_name TEXT,
    related_resource_type TEXT,
    related_resource_id TEXT,
    status TEXT DEFAULT 'pending',
    retry_count INTEGER DEFAULT 0,
    error_message TEXT,
    sent_at REAL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notif_user ON notification_log(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_notif_status ON notification_log(status, created_at);

CREATE TABLE IF NOT EXISTS alert_rule (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    name TEXT NOT NULL,
    rule_type TEXT NOT NULL,
    symbol_filter TEXT,
    params_json TEXT NOT NULL,
    enabled INTEGER DEFAULT 1,
    cooldown_minutes INTEGER DEFAULT 60,
    last_triggered_at REAL,
    last_notified_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alert_rule_user ON alert_rule(user_id, enabled);
CREATE INDEX IF NOT EXISTS idx_alert_rule_enabled ON alert_rule(enabled);  -- ★ Round 4: scan_all_alerts 全表扫

CREATE TABLE IF NOT EXISTS alert_trigger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id INTEGER NOT NULL,
    user_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    triggered_at REAL NOT NULL,
    metric_value REAL,
    threshold_value REAL,
    context_json TEXT,
    notification_log_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_trigger_rule ON alert_trigger(rule_id, triggered_at);
CREATE INDEX IF NOT EXISTS idx_trigger_user ON alert_trigger(user_id, triggered_at);

CREATE TABLE IF NOT EXISTS backtest_strategy (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT,
    natural_language TEXT NOT NULL,
    compiled_dsl TEXT,
    compiled_code TEXT,
    llm_model TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_strategy_user ON backtest_strategy(user_id, created_at);

CREATE TABLE IF NOT EXISTS backtest_result (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id INTEGER NOT NULL,
    user_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    initial_capital REAL DEFAULT 1000000.0,
    final_capital REAL,
    total_return_pct REAL,
    annualized_return_pct REAL,
    sharpe_ratio REAL,
    max_drawdown_pct REAL,
    win_rate REAL,
    total_trades INTEGER,
    trades_json TEXT,
    equity_curve_json TEXT,
    status TEXT DEFAULT 'pending',
    error_message TEXT,
    created_at REAL NOT NULL,
    completed_at REAL
);
CREATE INDEX IF NOT EXISTS idx_backtest_strategy ON backtest_result(strategy_id, created_at);
CREATE INDEX IF NOT EXISTS idx_backtest_user ON backtest_result(user_id, created_at);

CREATE TABLE IF NOT EXISTS paper_account (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    name TEXT NOT NULL,
    initial_capital REAL DEFAULT 1000000.0,
    cash REAL DEFAULT 1000000.0,
    created_at REAL NOT NULL,
    UNIQUE(user_id, name)
);
-- ★ Round 4 索引：list_accounts(user_id) 常用
CREATE INDEX IF NOT EXISTS idx_paper_account_user ON paper_account(user_id);

CREATE TABLE IF NOT EXISTS paper_trade (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    user_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    price REAL NOT NULL,
    quantity INTEGER NOT NULL,
    amount REAL NOT NULL,
    fee REAL DEFAULT 0.0,
    reason TEXT,
    related_alert_id INTEGER,
    related_strategy_id INTEGER,
    trade_date TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_paper_account ON paper_trade(account_id, trade_date);
-- ★ Round 4 索引：portfolio.list_holdings WHERE user_id=? AND reason LIKE 'portfolio_holding%'
CREATE INDEX IF NOT EXISTS idx_paper_trade_user_reason ON paper_trade(user_id, reason);

CREATE TABLE IF NOT EXISTS paper_snapshot (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    snapshot_date TEXT NOT NULL,
    total_equity REAL NOT NULL,
    cash REAL NOT NULL,
    positions_value REAL NOT NULL,
    positions_json TEXT,
    benchmark_equity REAL,
    created_at REAL NOT NULL,
    UNIQUE(account_id, snapshot_date)
);

CREATE TABLE IF NOT EXISTS community_hot_list (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rank_date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    rank INTEGER NOT NULL,
    score REAL,
    change_pct REAL,
    mention_count INTEGER,
    UNIQUE(rank_date, symbol)
);

CREATE TABLE IF NOT EXISTS shared_report (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    share_token TEXT UNIQUE NOT NULL,
    user_id TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    title TEXT,
    view_count INTEGER DEFAULT 0,
    is_public INTEGER DEFAULT 1,
    expires_at REAL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_share_user ON shared_report(user_id, created_at);
-- ★ Round 4 索引：_load_resource 反向查找 + 列表过滤 (resource_type, resource_id)
CREATE INDEX IF NOT EXISTS idx_share_resource ON shared_report(resource_type, resource_id);

CREATE TABLE IF NOT EXISTS user_follow (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    follower_id TEXT NOT NULL,
    followee_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE(follower_id, followee_id)
);

CREATE TABLE IF NOT EXISTS data_source_health (
    source_name TEXT PRIMARY KEY,
    success_count INTEGER DEFAULT 0,
    failure_count INTEGER DEFAULT 0,
    last_success_at REAL,
    last_failure_at REAL,
    last_error TEXT,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS rate_limit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT,
    ip_address TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rl_ip ON rate_limit_log(ip_address, created_at);
CREATE INDEX IF NOT EXISTS idx_rl_user ON rate_limit_log(user_id, created_at);

CREATE TABLE IF NOT EXISTS llm_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    total_tokens INTEGER,
    cost_cny REAL,
    task_type TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_llm_user ON llm_usage(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_llm_provider ON llm_usage(provider, created_at);

-- 用户自定义 cron 任务元数据（持久化 APScheduler job 之外的展示用）
CREATE TABLE IF NOT EXISTS cron_user_jobs (
    job_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    func_name TEXT NOT NULL,
    cron_expr TEXT NOT NULL,
    args_json TEXT,
    description TEXT,
    enabled INTEGER DEFAULT 1,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_user_jobs_user ON cron_user_jobs(user_id, created_at);
-- ★ Round 4 索引：list_user_jobs() 全表按 created_at 排序；scheduler 重启时按 enabled 过滤
CREATE INDEX IF NOT EXISTS idx_user_jobs_enabled ON cron_user_jobs(enabled);

-- cron 任务执行日志
CREATE TABLE IF NOT EXISTS cron_run_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    started_at REAL NOT NULL,
    finished_at REAL,
    status TEXT,
    error_message TEXT,
    duration_seconds REAL
);
CREATE INDEX IF NOT EXISTS idx_run_log_job ON cron_run_log(job_id, started_at);
CREATE INDEX IF NOT EXISTS idx_run_log_status ON cron_run_log(status, started_at);
-- ============================================================================
-- Hyperopt（参数优化，借鉴 freqtrade Hyperopt 的最小版）
-- ============================================================================
CREATE TABLE IF NOT EXISTS hyperopt_run (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id INTEGER NOT NULL,
    user_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    epochs INTEGER,
    loss_fn TEXT,
    train_start TEXT,
    train_end TEXT,
    test_start TEXT,
    test_end TEXT,
    best_params_json TEXT,
    best_loss REAL,
    best_train_json TEXT,
    oos_metrics_json TEXT,
    oos_loss REAL,
    status TEXT DEFAULT 'running',
    error_message TEXT,
    created_at REAL NOT NULL,
    completed_at REAL
);
CREATE INDEX IF NOT EXISTS idx_hyperopt_run_strategy ON hyperopt_run(strategy_id, created_at);

CREATE TABLE IF NOT EXISTS hyperopt_epoch (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    epoch INTEGER NOT NULL,
    params_json TEXT,
    loss REAL,
    metrics_json TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_hyperopt_epoch_run ON hyperopt_epoch(run_id, epoch);
