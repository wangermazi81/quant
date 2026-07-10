from tests._module_stubs import stub_project_module
import sys
import types
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from analytics import ai_analyst


def _sample_kline(rows=80):
    dates = pd.date_range('2026-03-01', periods=rows, freq='D')
    close = [10 + i * 0.03 for i in range(rows)]
    return pd.DataFrame(
        {
            'date': dates,
            'open': [v - 0.04 for v in close],
            'close': close,
            'high': [v + 0.08 for v in close],
            'low': [v - 0.10 for v in close],
            'volume': [1000000 + (i % 7) * 25000 for i in range(rows)],
        }
    )


def test_run_analysis_returns_executable_decision_plan_without_llm(monkeypatch):
    stub_project_module(
        monkeypatch,
        'market_context',
        types.SimpleNamespace(
            get_market_regime=lambda: {'trend': '强势震荡', 'strength': 72},
            get_market_summary_text=lambda: '【市场背景】强势震荡，允许正常仓位精选个股。',
        ),
    )

    result = ai_analyst.run_analysis(
        '000001',
        '平安银行',
        _sample_kline(),
        config_override={'enabled': False, 'api_key': ''},
    )

    assert result['ok'] is True
    assert result['ai_enabled'] is False

    plan = result['decision_plan']
    assert plan['action'] in {'强烈买入', '买入', '观望', '减仓', '卖出'}
    assert 0 <= plan['confidence'] <= 100
    assert 0 <= plan['position_pct'] <= 0.5
    assert plan['entry_zone']['low'] <= plan['entry_zone']['high']
    assert plan['stop_loss'] > 0
    assert plan['take_profit']['t1'] > 0
    assert isinstance(plan['hold_days'], str) and plan['hold_days']
    assert plan['cancel_conditions']
    assert 'notes' in plan

    evidence = result['evidence']
    assert set(evidence) >= {'technical', 'market', 'sentiment', 'fundamental', 'risk'}
    assert evidence['technical']['score'] == result['score']['total']
    assert evidence['market']['strength'] == 72
    assert isinstance(evidence['risk']['flags'], list)
