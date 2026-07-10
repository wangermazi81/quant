# API 清单（自动生成，勿手改）

> 由 `tools/gen_api_doc.py` 从 `app.url_map` 导出，CI 校验与代码同步。
> 共 221 条路由，按蓝图分组。

## admin — 管理后台（49 条）

| 路径 | 方法 | 端点 | 说明 |
|---|---|---|---|
| `/api/admin/ai-trade-decision/auto/run` | POST | admin.admin_ai_trade_auto_run |  |
| `/api/admin/ai-trade-decision/auto/status` | GET | admin.admin_ai_trade_auto_status |  |
| `/api/admin/ai-trade-decision/latest` | GET | admin.admin_ai_trade_decision_latest |  |
| `/api/admin/ai-trade-decision/run` | POST | admin.admin_ai_trade_decision_run |  |
| `/api/admin/announcements` | GET | admin.admin_list_announcements |  |
| `/api/admin/announcements` | POST | admin.admin_create_announcement |  |
| `/api/admin/announcements/<int:ann_id>` | DELETE | admin.admin_delete_announcement |  |
| `/api/admin/announcements/<int:ann_id>` | PUT | admin.admin_update_announcement |  |
| `/api/admin/changelog` | GET | admin.admin_list_changelog | 管理员列出全部更新日志（含未发布）。 |
| `/api/admin/changelog` | POST | admin.admin_create_changelog | 管理员创建更新日志。 |
| `/api/admin/changelog/<int:cid>` | DELETE | admin.admin_delete_changelog | 管理员删除更新日志。 |
| `/api/admin/changelog/<int:cid>` | PUT | admin.admin_update_changelog | 管理员更新日志。 |
| `/api/admin/datasource/config` | GET | admin.admin_datasource_config_get |  |
| `/api/admin/datasource/config` | POST | admin.admin_datasource_config_save |  |
| `/api/admin/datasource/test` | POST | admin.admin_datasource_test |  |
| `/api/admin/factor-mining/latest` | GET | admin.admin_factor_mining_latest |  |
| `/api/admin/factor-mining/rules` | GET | admin.admin_factor_mining_rules |  |
| `/api/admin/factor-mining/run` | POST | admin.admin_factor_mining_run |  |
| `/api/admin/factor-watchlist` | GET | admin.admin_factor_watchlist_get |  |
| `/api/admin/factor-watchlist/<item_id>` | DELETE | admin.admin_factor_watchlist_delete |  |
| `/api/admin/factor-watchlist/<item_id>` | PATCH | admin.admin_factor_watchlist_update |  |
| `/api/admin/factor-watchlist/import` | POST | admin.admin_factor_watchlist_import |  |
| `/api/admin/invite_codes` | GET | admin.admin_list_invites | 列出全部邀请码（含生成者、名额、已用、奖励、状态）。 |
| `/api/admin/invite_codes` | POST | admin.admin_create_invite | 生成邀请码：max_uses 名额 / reward 每邀请奖励积分 / note 备注 / expires_at 可选过期。 |
| `/api/admin/invite_codes/<int:cid>` | DELETE | admin.admin_revoke_invite | 作废邀请码（停止可用，不删历史）。 |
| `/api/admin/logs` | GET | admin.admin_get_logs |  |
| `/api/admin/permissions/catalog` | GET | admin.admin_permissions_catalog |  |
| `/api/admin/point_costs` | GET | admin.admin_get_point_costs | 各功能调用积分消耗（0=免费）。 |
| `/api/admin/point_costs` | POST | admin.admin_set_point_costs | 保存积分消耗配置：内存即时生效 + data/point_costs.json 持久化。 |
| `/api/admin/reset_requests` | GET | admin.admin_list_reset_requests | 列出找回密码申请（默认仅 pending）。 |
| `/api/admin/reset_requests/<int:req_id>/reject` | POST | admin.admin_reject_reset_request | 驳回申请（不重置密码）。 |
| `/api/admin/reset_requests/<int:req_id>/resolve` | POST | admin.admin_resolve_reset_request | 审批通过：生成临时密码并重置，返回临时密码供管理员转告用户。 |
| `/api/admin/security/events` | GET | admin.admin_security_events |  |
| `/api/admin/security/ip_blocks` | GET | admin.admin_security_ip_blocks |  |
| `/api/admin/security/ip_blocks` | POST | admin.admin_security_block_ip |  |
| `/api/admin/security/ip_blocks/<path:ip>` | DELETE | admin.admin_security_release_ip |  |
| `/api/admin/stats` | GET | admin.admin_stats |  |
| `/api/admin/system/status` | GET | admin.admin_system_status |  |
| `/api/admin/transactions` | GET | admin.admin_transactions |  |
| `/api/admin/users` | GET | admin.admin_users |  |
| `/api/admin/users/<int:uid>` | DELETE | admin.admin_delete_user | 删除用户（不可删自己）。 |
| `/api/admin/users/<int:uid>/active` | POST | admin.admin_set_active |  |
| `/api/admin/users/<int:uid>/admin` | POST | admin.admin_set_admin |  |
| `/api/admin/users/<int:uid>/permissions` | GET | admin.admin_get_user_permissions |  |
| `/api/admin/users/<int:uid>/permissions` | POST | admin.admin_save_user_permissions |  |
| `/api/admin/users/<int:uid>/points` | POST | admin.admin_add_points |  |
| `/api/admin/users/<int:uid>/reset_password` | POST | admin.admin_reset_password |  |
| `/api/admin/users/<int:uid>/set_points` | POST | admin.admin_set_points | 把用户积分设为指定值（可增可减），区别于 /points 的增量充值。 |
| `/api/admin/users/admins` | GET | admin.admin_list_admin_users |  |

## ai — AI 分析（11 条）

| 路径 | 方法 | 端点 | 说明 |
|---|---|---|---|
| `/api/ai/analyze` | POST | ai.ai_analyze |  |
| `/api/ai/analyze_async` | POST | ai.ai_analyze_async | 异步 AI 分析：立即返回 task_id，前端轮询进度和结果。 |
| `/api/ai/analyze_progress/<task_id>` | GET | ai.ai_analyze_progress | 查询异步 AI 分析状态（前端可 1-2s 轮询） |
| `/api/ai/analyze_result/<task_id>` | GET | ai.ai_analyze_result | 获取异步 AI 分析结果 |
| `/api/ai/config` | GET | ai.ai_get_config | 全局 AI 配置（仅管理员可查看/修改；普通用户的 AI 功能由后端用全局配置代答）。 |
| `/api/ai/config` | POST | ai.ai_save_config |  |
| `/api/ai/detect_models` | POST | ai.ai_detect_models | 探测 Base URL + Key 下可用的全部模型（中转站/官方 OpenAI 兼容端点通用）。 |
| `/api/ai/generate_strategy` | POST | ai.ai_gen_strategy |  |
| `/api/ai/test` | POST | ai.ai_test_conn |  |
| `/api/stocks/score` | POST | ai.api_stock_score |  |
| `/api/strategy_templates` | GET | ai.api_strategy_templates | 获取预设策略模板列表 |

## ai_trader — AI 自主交易（22 条）

| 路径 | 方法 | 端点 | 说明 |
|---|---|---|---|
| `/api/ai_trader/account` | GET | ai_trader.account |  |
| `/api/ai_trader/account/reset` | POST | ai_trader.account_reset |  |
| `/api/ai_trader/config` | GET | ai_trader.get_config |  |
| `/api/ai_trader/config` | POST | ai_trader.set_config |  |
| `/api/ai_trader/config/test` | POST | ai_trader.test_config |  |
| `/api/ai_trader/confirm` | POST | ai_trader.confirm |  |
| `/api/ai_trader/detect_models` | POST | ai_trader.detect_models | 填 Key 后探测可用模型列表（与后台「AI接口设置」同款体验）。 |
| `/api/ai_trader/emergency_stop` | POST | ai_trader.emergency_stop |  |
| `/api/ai_trader/meta` | GET | ai_trader.meta |  |
| `/api/ai_trader/research/backtest` | POST | ai_trader.research_backtest |  |
| `/api/ai_trader/research/backtest/<job_id>` | GET | ai_trader.research_backtest_status |  |
| `/api/ai_trader/research/memory` | GET | ai_trader.research_memory |  |
| `/api/ai_trader/run_once` | POST | ai_trader.run_once |  |
| `/api/ai_trader/settings` | GET | ai_trader.get_settings |  |
| `/api/ai_trader/settings` | POST | ai_trader.set_settings |  |
| `/api/ai_trader/snapshot` | GET | ai_trader.snapshot |  |
| `/api/ai_trader/start` | POST | ai_trader.start |  |
| `/api/ai_trader/status` | GET | ai_trader.status |  |
| `/api/ai_trader/stop` | POST | ai_trader.stop |  |
| `/api/ai_trader/stop_once` | POST | ai_trader.stop_once |  |
| `/api/ai_trader/stream` | GET | ai_trader.stream |  |
| `/api/ai_trader/watchlist` | POST | ai_trader.watchlist |  |

## auth — 认证（7 条）

| 路径 | 方法 | 端点 | 说明 |
|---|---|---|---|
| `/api/auth/forgot` | POST | auth.auth_forgot | 申请重置：按邮箱/用户名找到用户→生成 token→发邮件。 |
| `/api/auth/login` | POST | auth.auth_login |  |
| `/api/auth/logout` | POST | auth.auth_logout |  |
| `/api/auth/me` | GET | auth.auth_me |  |
| `/api/auth/register` | POST | auth.auth_register |  |
| `/api/auth/reset` | POST | auth.auth_reset | 凭 token 设置新密码。 |
| `/api/auth/reset/validate` | GET | auth.auth_reset_validate | 前端打开重置页时先校验 token 是否有效（决定显示表单还是失效提示）。 |

## bridge — 实盘桥接（6 条）

| 路径 | 方法 | 端点 | 说明 |
|---|---|---|---|
| `/api/bridge/live_enabled` | GET | bridge.get_live_enabled |  |
| `/api/bridge/live_enabled` | POST | bridge.set_live_enabled |  |
| `/api/bridge/signals` | GET | bridge.bridge_get_signals |  |
| `/api/bridge/signals/<int:signal_id>/report` | POST | bridge.bridge_report_signal |  |
| `/api/bridge/signals/<int:signal_id>/status` | GET | bridge.bridge_signal_status | 前端轮询单条信号当前状态（pending/sent/executed/failed/cancelled） |
| `/api/bridge/today_orders` | GET | bridge.bridge_today_orders | 读 xiadan 当日委托表，返回每笔精确状态（已成/已报/部成/已撤/废单） |

## data — 行情数据（10 条）

| 路径 | 方法 | 端点 | 说明 |
|---|---|---|---|
| `/api/data/delete` | POST | data.api_data_delete |  |
| `/api/data/download` | POST | data.api_data_download |  |
| `/api/data/progress/<task_id>` | GET | data.api_data_progress |  |
| `/api/data/refresh_names` | POST | data.api_refresh_names |  |
| `/api/data/sector/<code>` | GET | data.api_stock_sector |  |
| `/api/data/stocks` | GET | data.api_data_list |  |
| `/api/kline/<code>` | GET | data.api_kline | K 线数据：本地已是最新交易日才走本地快路径，否则优先 TDX 实时日K， |
| `/api/kline/<code>/minute` | GET | data.api_minute_kline | Minute K-line data for the stock chart modal. |
| `/api/kline/<code>/seconds` | GET | data.api_second_kline | Second-level tick data for the stock chart modal. |
| `/api/quote/<code>/order_book` | GET | data.api_order_book | Realtime five-level bid/ask book for the stock chart modal. |

## factors — 因子库/因子动物园（14 条）

| 路径 | 方法 | 端点 | 说明 |
|---|---|---|---|
| `/api/factors` | GET | factors.api_factor_list |  |
| `/api/factors/<filename>` | GET | factors.api_factor_get |  |
| `/api/factors/ai_generate` | POST | factors.api_factor_ai_generate |  |
| `/api/factors/delete` | POST | factors.api_factor_delete |  |
| `/api/factors/evaluate` | POST | factors.api_factor_evaluate | 因子评价：计算 IC / ICIR / RankIC / t-stat。 |
| `/api/factors/run` | POST | factors.api_factor_run |  |
| `/api/factors/save` | POST | factors.api_factor_save |  |
| `/api/factors/template` | GET | factors.api_factor_template |  |
| `/api/factors_zoo/backtest` | POST | factors.api_factors_zoo_backtest | 因子回测（同步，前端等结果；大池子用 codes 限制规模） |
| `/api/factors_zoo/compute` | POST | factors.api_factors_zoo_compute | 对一组股票算指定 alpha 的分数 |
| `/api/factors_zoo/compute_async` | POST | factors.api_factors_zoo_compute_async | 启动异步因子计算（适合全市场扫描） |
| `/api/factors_zoo/list` | GET | factors.api_factors_zoo_list | 列出 Vibe Alpha 因子库的所有可用因子 |
| `/api/factors_zoo/progress/<task_id>` | GET | factors.api_factors_zoo_progress | 查询异步任务进度（轻量级，前端可 1s 轮询） |
| `/api/factors_zoo/result/<task_id>` | GET | factors.api_factors_zoo_result | 取异步任务结果（仅 status=done 时返回完整 ranked） |

## feedback — 问题反馈（4 条）

| 路径 | 方法 | 端点 | 说明 |
|---|---|---|---|
| `/api/feedback` | GET | feedback.admin_list_feedback | 管理员列表（可 ?status=open 过滤）。 |
| `/api/feedback` | POST | feedback.submit_feedback | 提交反馈（登录用户）。 |
| `/api/feedback/<int:fid>/status` | POST | feedback.admin_update_feedback | 管理员更新状态 / 备注。 |
| `/api/feedback/mine` | GET | feedback.my_feedback | 查看自己的反馈与处理状态。 |

## lhb — 龙虎榜（2 条）

| 路径 | 方法 | 端点 | 说明 |
|---|---|---|---|
| `/api/lhb/data` | GET | lhb.api_lhb_data |  |
| `/api/lhb/dates` | GET | lhb.api_lhb_dates |  |

## market — 市场概览（3 条）

| 路径 | 方法 | 端点 | 说明 |
|---|---|---|---|
| `/api/market/context` | GET | market.api_market_context |  |
| `/api/market/regime` | POST | market.api_market_regime | 市场环境检测（牛市/熊市/震荡/急跌）+ 建议仓位系数。 |
| `/api/market/review` | GET | market.api_market_review | 大盘复盘（公开接口） |

## misc — 健康/回测/组合优化/Vibe激活（8 条）

| 路径 | 方法 | 端点 | 说明 |
|---|---|---|---|
| `/api/backtest` | POST | misc.api_backtest |  |
| `/api/backtest/cancel/<task_id>` | POST | misc.api_backtest_cancel |  |
| `/api/backtest/task/<task_id>` | GET | misc.api_backtest_task |  |
| `/api/health` | GET | misc.api_health |  |
| `/api/portfolio/optimize` | POST | misc.api_portfolio_optimize | 投资组合优化。 |
| `/api/vibe_activations/activate` | POST | misc.api_vibe_activation_activate |  |
| `/api/vibe_activations/deactivate` | POST | misc.api_vibe_activation_deactivate |  |
| `/api/vibe_activations/me` | GET | misc.api_vibe_activation_me |  |

## notify — 通知（3 条）

| 路径 | 方法 | 端点 | 说明 |
|---|---|---|---|
| `/api/notify/test` | POST | notify.api_notify_test | 测试推送（发送测试消息到所有启用渠道） |
| `/api/user/notify_config` | GET | notify.user_get_notify_config |  |
| `/api/user/notify_config` | POST | notify.user_save_notify_config |  |

## pages — 页面/静态（6 条）

| 路径 | 方法 | 端点 | 说明 |
|---|---|---|---|
| `/` | GET | pages.index |  |
| `/admin` | GET | pages.admin_page |  |
| `/api/changelog` | GET | pages.public_changelog | 获取已发布的更新日志（登录/未登录均可看）。 |
| `/legacy` | GET | pages.index_legacy | 旧版单文件 UI 兜底入口（切换期保留）。 |
| `/logs/screenshots/<path:filename>` | GET | pages.serve_screenshot | 暴露桥接器截图给前端浏览器（仅登录用户可看） |
| `/v2` | GET | pages.index_v2 | 新版前端（frontend/ 经 Vite 构建到 static/dist/）。登录后逐页验收功能对等。 |

## paper — 模拟盘（12 条）

| 路径 | 方法 | 端点 | 说明 |
|---|---|---|---|
| `/api/paper/account` | GET | paper.paper_account |  |
| `/api/paper/buy` | POST | paper.paper_manual_buy |  |
| `/api/paper/config` | GET | paper.paper_get_config |  |
| `/api/paper/config` | POST | paper.paper_set_config |  |
| `/api/paper/equity` | GET | paper.paper_equity |  |
| `/api/paper/orders` | GET | paper.paper_orders |  |
| `/api/paper/positions` | GET | paper.paper_positions |  |
| `/api/paper/reset` | POST | paper.paper_reset |  |
| `/api/paper/sell` | POST | paper.paper_manual_sell |  |
| `/api/paper/stats` | GET | paper.paper_stats |  |
| `/api/paper/stream` | GET | paper.paper_stream | SSE 实时推送：日志、持仓更新、扫描完成事件。 |
| `/api/paper/trades` | GET | paper.paper_trades |  |

## research — 产业链投研（6 条）

| 路径 | 方法 | 端点 | 说明 |
|---|---|---|---|
| `/api/chain/concepts` | GET | research.api_chain_concepts |  |
| `/api/chain/graph` | GET | research.api_chain_graph_get |  |
| `/api/chain/graph` | POST | research.api_chain_graph_gen |  |
| `/api/chain/stocks` | GET | research.api_chain_stocks |  |
| `/api/reports` | GET | research.api_reports |  |
| `/api/reports/latest` | GET | research.api_reports_latest | 研报中心：最新研报流。qtype 0=个股 1=行业 3=策略；q=代码/名称/行业/关键词。 |

## review — 复盘/新闻（6 条）

| 路径 | 方法 | 端点 | 说明 |
|---|---|---|---|
| `/api/news/feed` | GET | review.api_news_feed |  |
| `/api/news/sources` | GET | review.api_news_sources |  |
| `/api/review/daily` | GET | review.api_review_daily |  |
| `/api/review/dates` | GET | review.api_review_dates |  |
| `/api/review/generate` | POST | review.api_review_generate | 异步启动生成（采集+AI约1-2分钟，HTTP立即返回），前端轮询 /api/review/status。 |
| `/api/review/status` | GET | review.api_review_status |  |

## search — 搜索/资讯（11 条）

| 路径 | 方法 | 端点 | 说明 |
|---|---|---|---|
| `/api/search/config` | GET | search.api_search_config_get | 获取搜索服务配置（管理员） |
| `/api/search/config` | POST | search.api_search_config_save | 保存搜索服务配置（管理员） |
| `/api/search/news/<code>` | GET | search.api_search_news | 个股新闻搜索（Tavily→SerpAPI→直接抓取） |
| `/api/search/stats` | GET | search.api_search_stats | 搜索服务用量统计 |
| `/api/search/status` | GET | search.api_search_status | 搜索服务状态（是否有可用额度） |
| `/api/search/stock_info/<code>` | GET | search.api_search_stock_info | 综合股票信息（新闻+网页+TwelveData报价） |
| `/api/search/symbol_search` | GET | search.api_symbol_search | TwelveData 股票代码/名称搜索 |
| `/api/search/twelvedata/kline/<code>` | GET | search.api_td_kline | TwelveData K线数据（A股） |
| `/api/search/twelvedata/quote/<code>` | GET | search.api_td_quote | TwelveData 实时报价（A股） |
| `/api/search/web` | POST | search.api_search_web | 通用网页搜索（Tavily→SerpAPI） |
| `/api/stock/news/<code>` | GET | search.api_stock_news | 获取个股新闻 + 情绪分析（search_manager 五级降级） |

## strategies — 策略中心（10 条）

| 路径 | 方法 | 端点 | 说明 |
|---|---|---|---|
| `/api/strategies` | GET | strategies.api_list |  |
| `/api/strategies/<filename>` | GET | strategies.api_get |  |
| `/api/strategies/activate` | POST | strategies.api_activate |  |
| `/api/strategies/delete` | POST | strategies.api_delete |  |
| `/api/strategies/progress/<task_id>` | GET | strategies.api_strat_progress | 查询异步选股任务状态（前端可 1-2s 轮询） |
| `/api/strategies/rename` | POST | strategies.api_rename |  |
| `/api/strategies/result/<task_id>` | GET | strategies.api_strat_result | 获取异步选股结果（任务完成后调用） |
| `/api/strategies/run` | POST | strategies.api_run |  |
| `/api/strategies/run_async` | POST | strategies.api_run_async | 异步选股：立即返回 task_id，前端轮询 /strategies/progress 和 /strategies/result。 |
| `/api/strategies/save` | POST | strategies.api_save |  |

## trade — 监控/交易/账户（16 条）

| 路径 | 方法 | 端点 | 说明 |
|---|---|---|---|
| `/api/account` | GET | trade.get_account |  |
| `/api/account` | POST | trade.set_account |  |
| `/api/account/funds` | GET | trade.account_funds | 统一「可用资金」查询，方便下单前知道还能买多少。 |
| `/api/account/snapshot` | GET | trade.account_snapshot | 实盘资金 + 持仓快照（一次性返回 可用资金 / 总资产 / 持仓列表）。 |
| `/api/account/trade_settings` | GET,POST | trade.account_trade_settings | 读/写 实盘交易自动化开关（运行时切换，免改 config）： |
| `/api/auto_execute` | POST | trade.set_auto_execute |  |
| `/api/execute` | POST | trade.execute |  |
| `/api/positions` | GET | trade.get_positions_api |  |
| `/api/positions/close` | POST | trade.close_position |  |
| `/api/positions/sell` | POST | trade.sell_position |  |
| `/api/queue/add` | POST | trade.queue_add |  |
| `/api/queue/clear` | POST | trade.clear_queue |  |
| `/api/seal_tracker` | GET | trade.seal_tracker_dump | SealTracker 状态查询（调试/前端展示用）。 |
| `/api/start` | POST | trade.start |  |
| `/api/status` | GET | trade.get_status |  |
| `/api/stop` | POST | trade.stop |  |

## user — 用户自助（15 条）

| 路径 | 方法 | 端点 | 说明 |
|---|---|---|---|
| `/api/announcements` | GET | user.get_announcements |  |
| `/api/announcements/<int:ann_id>/read` | POST | user.mark_announcement_read |  |
| `/api/user/ai_config` | GET | user.user_get_ai_config |  |
| `/api/user/ai_config` | POST | user.user_save_ai_config |  |
| `/api/user/broker_config` | GET | user.user_get_broker_config |  |
| `/api/user/broker_config` | POST | user.user_save_broker_config |  |
| `/api/user/change_password` | POST | user.user_change_password |  |
| `/api/user/invite_codes` | GET | user.my_invite_codes | 我生成的邀请码列表 + 邀请概览（累计邀请人数/奖励积分/每邀请奖励）。 |
| `/api/user/invite_codes` | POST | user.create_my_invite_code | 生成我的邀请码（名额可设 1~20；奖励为平台固定值，不可自定义防刷分）。 |
| `/api/user/invite_codes/<int:cid>` | DELETE | user.revoke_my_invite_code | 作废我自己的邀请码。 |
| `/api/user/logs` | GET | user.user_get_logs |  |
| `/api/user/points` | GET | user.user_points |  |
| `/api/user/points/history` | GET | user.user_points_history |  |
| `/api/user/settings` | GET | user.user_get_settings |  |
| `/api/user/settings` | POST | user.user_save_settings |  |

