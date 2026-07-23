import os
import sys
from pathlib import Path

from flask import Flask

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ['QUANT_TEST_IMPORT'] = '1'
os.environ['THS_NO_SCHEDULER'] = '1'


def test_mobile_account_endpoint_queues_background_task(monkeypatch):
    from web import bridge

    app = Flask(__name__)
    app.register_blueprint(bridge.bp)

    calls = []

    def fake_start(action, target, worker):
        calls.append((action, target, worker))
        return {'ok': True, 'accepted': True, 'action': action, 'target': target, 'msg': 'queued'}

    class FakeOperator:
        def query_account_snapshot(self):
            return {'ok': True, 'available': 123.45, 'positions': []}

    profile = {'trade_mode': 'simulation', 'confirm_mode': 'auto', 'connection_mode': 'usb'}
    monkeypatch.setattr(bridge, '_load_bridge_config', lambda: {})
    monkeypatch.setattr(bridge, '_load_android_profile', lambda cfg=None: dict(profile))
    monkeypatch.setattr(bridge, '_save_android_profile', lambda *args, **kwargs: None)
    monkeypatch.setattr(bridge, '_start_mobile_legacy_task', fake_start)
    monkeypatch.setattr(bridge, '_build_android_operator_for_web', lambda: FakeOperator())

    response = app.test_client().get('/api/mobile_ths/account')

    assert response.status_code == 200
    assert response.get_json()['action'] == 'account'
    assert calls and calls[0][0] == 'account'
    assert calls[0][2]()['available'] == 123.45


def test_mobile_legacy_busy_is_accepted(monkeypatch):
    from web import bridge

    bridge._mobile_legacy_state_update(
        running=True,
        task_id='switch_mode-old',
        action='switch_mode',
        target='simulation',
        ok=None,
        msg='running',
        started_at=bridge.time.time(),
    )

    result = bridge._start_mobile_legacy_task('open_ths', '', lambda: {'ok': True})

    assert result['ok'] is True
    assert result['accepted'] is True
    assert result['busy'] is True

    bridge._mobile_legacy_state_update(running=False, action='', target='', ok=None, msg='', started_at=0.0)


def test_mobile_connection_and_confirm_clear_stale_failed_task(monkeypatch):
    from web import bridge

    app = Flask(__name__)
    app.register_blueprint(bridge.bp)

    cfg = {'connection_mode': 'wifi', 'confirm_mode': 'manual'}
    profile = {'connection_mode': 'wifi', 'confirm_mode': 'manual'}

    monkeypatch.setattr(bridge, '_load_bridge_config', lambda: dict(cfg))
    monkeypatch.setattr(bridge, '_load_android_profile', lambda cfg=None: dict(profile))
    monkeypatch.setattr(bridge, '_save_bridge_config', lambda new_cfg: cfg.update(new_cfg))
    monkeypatch.setattr(bridge, '_save_android_profile', lambda new_profile, cfg=None: profile.update(new_profile))
    monkeypatch.setattr(bridge, '_ensure_usb_reverse', lambda cfg, profile: {'ok': True, 'msg': 'usb ready', 'device_serial': 'device-1'})

    bridge._mobile_legacy_state_update(
        running=False,
        task_id='switch_mode-old',
        action='switch_mode',
        target='simulation',
        ok=False,
        msg='old failed',
        started_at=bridge.time.time(),
    )

    client = app.test_client()
    conn = client.get('/api/mobile_ths/connection?mode=usb').get_json()
    state_after_conn = bridge._mobile_legacy_state_snapshot()

    confirm = client.get('/api/mobile_ths/confirm_mode?mode=auto').get_json()
    state_after_confirm = bridge._mobile_legacy_state_snapshot()

    assert conn['ok'] is True
    assert state_after_conn['action'] == 'connection'
    assert state_after_conn['ok'] is True
    assert confirm['ok'] is True
    assert confirm['confirm_mode'] == 'auto'
    assert state_after_confirm['action'] == 'confirm_mode'
    assert state_after_confirm['ok'] is True
