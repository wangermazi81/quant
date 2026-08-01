from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
ADMIN_HTML = ROOT / "admin.html"
ADMIN_CSS = ROOT / "static" / "app" / "css" / "admin-base.css"
ADMIN_JS = ROOT / "static" / "app" / "js" / "admin-app.js"


def test_short_factor_discovery_admin_ui_contract():
    html = ADMIN_HTML.read_text(encoding="utf-8")
    js = ADMIN_JS.read_text(encoding="utf-8")

    assert 'id="sfd-run-btn"' in html
    assert 'id="sfd-candidates"' in html
    assert 'id="sfd-protocol"' in html
    assert "极致短线新因子发现" in html
    assert "/admin/short-factor-discovery/latest" in js
    assert "/admin/short-factor-discovery/status" in js
    assert "/admin/short-factor-discovery/run" in js
    assert "renderShortFactorDiscovery" in js
    assert "research_only" in js
    assert "deployment_eligible" in js


def _render_model_meta(report: dict) -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the admin UI behavior test")
    harness = r"""
const fs = require('fs');
const vm = require('vm');
const elements = {};
function element(id) {
  if (!elements[id]) {
    elements[id] = {
      id,
      style: {},
      dataset: {},
      textContent: '',
      innerHTML: '',
      value: '',
      classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
      setAttribute() {},
      getAttribute() { return null; },
      querySelector() { return null; },
      querySelectorAll() { return []; },
    };
  }
  return elements[id];
}
const context = {
  console,
  elements,
  setTimeout,
  clearTimeout,
  setInterval,
  clearInterval,
  confirm() { return false; },
  location: { hash: '' },
  localStorage: { getItem() { return null; }, setItem() {}, removeItem() {} },
  window: { addEventListener() {} },
  document: {
    addEventListener() {},
    getElementById: element,
    querySelector() { return null; },
    querySelectorAll() { return []; },
  },
};
context.window.location = context.location;
vm.createContext(context);
vm.runInContext(
  fs.readFileSync(process.argv[1], 'utf8') + '\nthis.__renderModelDiagnostics = fmRenderModelDiagnostics;',
  context,
);
context.__renderModelDiagnostics(JSON.parse(process.argv[2]));
process.stdout.write(elements['fm-model-meta'].innerHTML || '');
"""
    completed = subprocess.run(
        [node, "-e", harness, str(ADMIN_JS), json.dumps(report, ensure_ascii=False)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


def _render_model_lifecycle(report: dict, overview: dict, run_id: str) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the admin UI behavior test")
    harness = r"""
const fs = require('fs');
const vm = require('vm');
const elements = {};
function element(id) {
  if (!elements[id]) {
    elements[id] = {
      id,
      style: {},
      dataset: {},
      textContent: '',
      innerHTML: '',
      value: '',
      disabled: false,
      title: '',
      classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
      setAttribute() {},
      getAttribute() { return null; },
      querySelector() { return null; },
      querySelectorAll() { return []; },
    };
  }
  return elements[id];
}
const context = {
  console,
  elements,
  setTimeout,
  clearTimeout,
  setInterval,
  clearInterval,
  confirm() { return false; },
  location: { hash: '' },
  localStorage: { getItem() { return null; }, setItem() {}, removeItem() {} },
  window: { addEventListener() {} },
  document: {
    addEventListener() {},
    getElementById: element,
    querySelector() { return null; },
    querySelectorAll() { return []; },
  },
};
context.window.location = context.location;
vm.createContext(context);
vm.runInContext(
  fs.readFileSync(process.argv[1], 'utf8') + `
this.__renderModelLifecycle = function(report, overview, runId) {
  FM_LAST_FACTOR_RUN_ID = runId;
  FM_EXPERIMENT_OVERVIEW = overview;
  fmRenderModelDiagnostics(report);
  return {
    state: elements['fm-model-state'].textContent,
    meta: elements['fm-model-meta'].innerHTML,
  };
};`,
  context,
);
const output = context.__renderModelLifecycle(
  JSON.parse(process.argv[2]),
  JSON.parse(process.argv[3]),
  process.argv[4],
);
process.stdout.write(JSON.stringify(output));
"""
    completed = subprocess.run(
        [
            node,
            "-e",
            harness,
            str(ADMIN_JS),
            json.dumps(report, ensure_ascii=False),
            json.dumps(overview, ensure_ascii=False),
            run_id,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def _render_experiment_overview(
    overview: dict,
    report: dict,
    run_id: str,
) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the admin UI behavior test")
    harness = r"""
const fs = require('fs');
const vm = require('vm');
const elements = {};
function element(id) {
  if (!elements[id]) {
    elements[id] = {
      id,
      style: {},
      dataset: {},
      textContent: '',
      innerHTML: '',
      value: '',
      disabled: false,
      title: '',
      classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
      setAttribute() {},
      getAttribute() { return null; },
      querySelector() { return null; },
      querySelectorAll() { return []; },
    };
  }
  return elements[id];
}
const context = {
  console,
  elements,
  setTimeout,
  clearTimeout,
  setInterval,
  clearInterval,
  confirm() { return false; },
  location: { hash: '' },
  localStorage: { getItem() { return null; }, setItem() {}, removeItem() {} },
  window: { addEventListener() {} },
  document: {
    addEventListener() {},
    getElementById: element,
    querySelector() { return null; },
    querySelectorAll() { return []; },
  },
};
context.window.location = context.location;
vm.createContext(context);
vm.runInContext(
  fs.readFileSync(process.argv[1], 'utf8') + `
this.__renderExperimentOverview = function(overview, report, runId) {
  FM_LAST_FACTOR_REPORT = report;
  FM_LAST_FACTOR_RUN_ID = runId;
  fmRenderExperimentOverview(overview);
  return {
    state: elements['fm-experiment-state'].textContent,
    progress: elements['fm-experiment-progress'].innerHTML,
    meta: elements['fm-experiment-meta'].innerHTML,
    metrics: elements['fm-experiment-metrics'].innerHTML,
    history: elements['fm-experiment-history'].innerHTML,
    lockDisabled: elements['fm-lock-btn'].disabled,
    lockTitle: elements['fm-lock-btn'].title,
    cancelDisplay: elements['fm-cancel-btn'].style.display || '',
  };
};`,
  context,
);
const output = context.__renderExperimentOverview(
  JSON.parse(process.argv[2]),
  JSON.parse(process.argv[3]),
  process.argv[4],
);
process.stdout.write(JSON.stringify(output));
"""
    completed = subprocess.run(
        [
            node,
            "-e",
            harness,
            str(ADMIN_JS),
            json.dumps(overview, ensure_ascii=False),
            json.dumps(report, ensure_ascii=False),
            run_id,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def _run_factor_refresh_race() -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the admin UI behavior test")
    harness = r"""
const fs = require('fs');
const vm = require('vm');
const elements = {};
function element(id) {
  if (!elements[id]) {
    elements[id] = {
      id,
      style: {},
      dataset: {},
      textContent: '',
      innerHTML: '',
      value: '',
      checked: false,
      disabled: false,
      title: '',
      className: '',
      classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
      setAttribute() {},
      getAttribute() { return null; },
      querySelector() { return null; },
      querySelectorAll() { return []; },
    };
  }
  return elements[id];
}
const context = {
  console,
  elements,
  setTimeout,
  clearTimeout,
  setInterval,
  clearInterval,
  confirm() { return false; },
  location: { hash: '' },
  localStorage: { getItem() { return null; }, setItem() {}, removeItem() {} },
  window: { addEventListener() {} },
  document: {
    addEventListener() {},
    getElementById: element,
    querySelector() { return null; },
    querySelectorAll() { return []; },
  },
};
context.window.location = context.location;
vm.createContext(context);
vm.runInContext(
  fs.readFileSync(process.argv[1], 'utf8') + `
this.__runFactorRefreshRace = async function() {
  function deferred() {
    let resolve;
    const promise = new Promise(done => { resolve = done; });
    return {promise, resolve};
  }
  const urls = [
    '/admin/factor-mining/rules',
    '/admin/factor-mining/latest',
    '/admin/factor-mining/experiments/active',
    '/admin/factor-mining/status',
  ];
  const first = Object.fromEntries(urls.map(url => [url, deferred()]));
  const second = Object.fromEntries(urls.map(url => [url, deferred()]));
  const calls = Object.fromEntries(urls.map(url => [url, 0]));
  _annFetch = function(method, url) {
    if (url.startsWith('/admin/factor-watchlist')) {
      return Promise.resolve({
        ok: true,
        updated_at: '',
        latest_scope: null,
        items: [],
        trade_dates: [],
        selected_date: '',
        total_all: 0,
        counts_by_date: {},
        counts_by_status: {},
        counts_by_readiness: {},
      });
    }
    const round = calls[url]++;
    return (round === 0 ? first[url] : second[url]).promise;
  };
  const older = loadFactorMining();
  const newer = loadFactorMining();
  second['/admin/factor-mining/rules'].resolve({ok:true, mining_rules:FM_DEFAULT_RULES});
  second['/admin/factor-mining/latest'].resolve({
    ok:true,
    found:true,
    run_id:'run-new',
    report:{
      status:'ok',
      data_status:'ok',
      research_status:'exploratory',
      generated_at:'2026-07-30 17:20:00',
      config:{},
      summary:{},
      latest_candidates:[],
      windows:[],
      research_assessment:{lock_eligible:false},
    },
  });
  second['/admin/factor-mining/experiments/active'].resolve({
    ok:true,
    found:false,
    experiment:null,
    progress:null,
    latest_validated:null,
    experiments:[],
  });
  second['/admin/factor-mining/status'].resolve({ok:true, found:false});
  await newer;

  first['/admin/factor-mining/rules'].resolve({ok:true, mining_rules:FM_DEFAULT_RULES});
  first['/admin/factor-mining/latest'].resolve({
    ok:true,
    found:true,
    run_id:'run-old',
    report:{
      status:'degraded',
      data_status:'degraded',
      research_status:'failed',
      generated_at:'2026-07-29 17:20:00',
      config:{},
      summary:{},
      latest_candidates:[],
      windows:[],
      research_assessment:{lock_eligible:false},
    },
  });
  first['/admin/factor-mining/experiments/active'].resolve({
    ok:true,
    found:true,
    experiment:{experiment_id:'exp-old', source_run_id:'run-old', status:'locked_shadow'},
    progress:{required_sessions:20, observed_sessions:1, settled_sessions:0},
    latest_validated:null,
    experiments:[],
  });
  first['/admin/factor-mining/status'].resolve({ok:true, found:false});
  await older;

  const fullLoadResult = {
    runId: FM_LAST_FACTOR_RUN_ID,
    experimentState: elements['fm-experiment-state'].textContent,
  };

  const oldOverview = deferred();
  const newOverview = deferred();
  let overviewCall = 0;
  _annFetch = function() {
    return (overviewCall++ === 0 ? oldOverview : newOverview).promise;
  };
  const olderOverviewRequest = fmLoadExperimentOverview();
  const newerOverviewRequest = fmLoadExperimentOverview();
  newOverview.resolve({
    ok:true,
    found:false,
    experiment:null,
    progress:null,
    latest_validated:null,
    experiments:[],
  });
  await newerOverviewRequest;
  oldOverview.resolve({
    ok:true,
    found:true,
    experiment:{experiment_id:'exp-stale', source_run_id:'run-stale', status:'locked_shadow'},
    progress:{required_sessions:20, observed_sessions:1, settled_sessions:0},
    latest_validated:null,
    experiments:[],
  });
  await olderOverviewRequest;

  const oldWatchlist = deferred();
  const newWatchlist = deferred();
  let watchlistCall = 0;
  _annFetch = function() {
    return (watchlistCall++ === 0 ? oldWatchlist : newWatchlist).promise;
  };
  FM_WATCH_DATE = '';
  const olderWatchlistRequest = fmLoadWatchlist();
  const newerWatchlistRequest = fmLoadWatchlist();
  newWatchlist.resolve({
    ok:true,
    updated_at:'2026-07-30 17:10:00',
    latest_scope:{trade_date:'2026-07-30', candidate_count:0},
    items:[],
    trade_dates:['2026-07-30'],
    selected_date:'2026-07-30',
    total_all:0,
    counts_by_date:{'2026-07-30':0},
    counts_by_status:{},
    counts_by_readiness:{},
  });
  await newerWatchlistRequest;
  oldWatchlist.resolve({
    ok:true,
    updated_at:'2026-07-29 17:10:00',
    latest_scope:{trade_date:'2026-07-29', candidate_count:0},
    items:[],
    trade_dates:['2026-07-29'],
    selected_date:'2026-07-29',
    total_all:0,
    counts_by_date:{'2026-07-29':0},
    counts_by_status:{},
    counts_by_readiness:{},
  });
  await olderWatchlistRequest;

  return {
    ...fullLoadResult,
    standaloneExperimentState: elements['fm-experiment-state'].textContent,
    watchDate: FM_WATCH_DATE,
  };
};`,
  context,
);
context.__runFactorRefreshRace()
  .then(output => process.stdout.write(JSON.stringify(output)))
  .catch(error => {
    console.error(error);
    process.exitCode = 1;
  });
"""
    completed = subprocess.run(
        [node, "-e", harness, str(ADMIN_JS)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=20,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def test_factor_model_diagnostics_separates_data_and_research_status():
    html = _render_model_meta(
        {
            "status": "ok",
            "data_status": "ok",
            "research_status": "failed",
            "reason": "ok",
            "research_assessment": {
                "deployment_eligible": False,
                "failure_reasons": ["bootstrap_lower_bound_not_positive"],
                "bootstrap_total_return_ci_pct": {
                    "lower": -2.5,
                    "median": 1.25,
                    "upper": 4.0,
                },
            },
            "model": {"fallback": False, "model_version": "relay_factor_mining_v3_nested_ridge"},
            "summary": {},
        }
    )

    assert "relay_factor_mining_v3_nested_ridge" in html
    assert "failed" in html
    assert "bootstrap_lower_bound_not_positive" in html
    assert "-2.50%" in html
    assert "不可自动部署" in html


def test_factor_model_diagnostics_render_st_coverage_and_research_status():
    html = _render_model_meta(
        {
            "status": "degraded",
            "reason": "ST status coverage too low",
            "model": {"fallback": True, "fallback_reason": "insufficient samples"},
            "summary": {
                "st_status_coverage": 0.75,
                "latest_st_status_coverage": 0.5,
                "amount_data_real": True,
                "amount_data_coverage": 0.99,
                "latest_amount_data_coverage": 0.995,
                "panel_stock_count": 800,
                "panel_max_abs_close_return": 0.1,
            },
        }
    )

    assert "历史 75.0%" in html
    assert "最新截面 50.0%" in html
    assert "研究状态 降级" in html
    assert "ST status coverage too low" in html
    assert "历史 99.0%；最新截面 99.5%" in html


@pytest.mark.parametrize(
    ("overview", "expected_state"),
    [
        ({"ok": True, "found": False, "experiments": []}, "历史通过，可锁定"),
        (
            {
                "ok": True,
                "found": True,
                "experiment": {
                    "experiment_id": "exp-active",
                    "source_run_id": "run-current",
                    "status": "locked_shadow",
                },
                "progress": {
                    "required_sessions": 20,
                    "observed_sessions": 7,
                    "settled_sessions": 5,
                },
                "experiments": [],
            },
            "前瞻验证中",
        ),
        (
            {
                "ok": True,
                "found": False,
                "latest_validated": {
                    "experiment_id": "exp-validated",
                    "source_run_id": "run-current",
                    "status": "validated",
                },
                "experiments": [],
            },
            "已验证，可部署",
        ),
        (
            {
                "ok": True,
                "found": False,
                "experiments": [
                    {
                        "experiment_id": "exp-failed",
                        "source_run_id": "run-current",
                        "status": "failed",
                        "evaluation": {
                            "failure_reasons": [
                                "minimum_total_return_pct_not_met"
                            ]
                        },
                    }
                ],
            },
            "前瞻验证失败",
        ),
    ],
)
def test_factor_model_diagnostics_render_full_research_lifecycle(
    overview,
    expected_state,
):
    rendered = _render_model_lifecycle(
        {
            "status": "ok",
            "data_status": "ok",
            "research_status": "exploratory",
            "research_assessment": {
                "lock_eligible": True,
                "deployment_eligible": False,
                "failure_reasons": [],
            },
            "model": {
                "fallback": False,
                "model_version": "relay_factor_mining_v3_nested_ridge",
            },
            "summary": {},
        },
        overview,
        "run-current",
    )

    assert rendered["state"] == expected_state


def test_factor_experiment_overview_renders_progress_incumbent_and_history():
    rendered = _render_experiment_overview(
        {
            "ok": True,
            "found": True,
            "experiment": {
                "experiment_id": "exp-challenger",
                "source_run_id": "run-current",
                "model_instance_id": "model-challenger",
                "status": "locked_shadow",
                "locked_at": "2026-07-30T09:10:00+00:00",
            },
            "progress": {
                "required_sessions": 20,
                "observed_sessions": 7,
                "settled_sessions": 5,
                "ready_to_evaluate": False,
            },
            "latest_validated": {
                "experiment_id": "exp-incumbent",
                "source_run_id": "run-incumbent",
                "model_instance_id": "model-incumbent",
                "status": "validated",
                "evaluation": {
                    "metrics": {
                        "total_return_pct": 6.25,
                        "max_drawdown_pct": -3.5,
                        "executed_trade_count": 18,
                    },
                    "failure_reasons": [],
                },
            },
            "experiments": [
                {
                    "experiment_id": "exp-challenger",
                    "source_run_id": "run-current",
                    "model_instance_id": "model-challenger",
                    "status": "locked_shadow",
                    "progress": {
                        "required_sessions": 20,
                        "observed_sessions": 7,
                        "settled_sessions": 5,
                    },
                },
                {
                    "experiment_id": "exp-incumbent",
                    "source_run_id": "run-incumbent",
                    "model_instance_id": "model-incumbent",
                    "status": "validated",
                    "progress": {
                        "required_sessions": 20,
                        "observed_sessions": 20,
                        "settled_sessions": 20,
                    },
                    "evaluation": {
                        "metrics": {
                            "total_return_pct": 6.25,
                            "max_drawdown_pct": -3.5,
                            "executed_trade_count": 18,
                        },
                        "failure_reasons": [],
                    },
                },
            ],
        },
        {
            "status": "ok",
            "data_status": "ok",
            "research_status": "exploratory",
            "research_assessment": {
                "lock_eligible": True,
                "deployment_eligible": False,
            },
        },
        "run-current",
    )

    assert rendered["state"] == "前瞻验证中"
    assert "已观察 7 / 20" in rendered["progress"]
    assert "已结算 5 / 20" in rendered["progress"]
    assert "exp-challenger" in rendered["meta"]
    assert "model-challenger" in rendered["meta"]
    assert "当前生产模型" in rendered["metrics"]
    assert "6.25%" in rendered["metrics"]
    assert "已验证" in rendered["history"]
    assert rendered["lockDisabled"] is True
    assert rendered["cancelDisplay"] != "none"


def test_factor_mining_page_exposes_experiment_controls_without_manual_import():
    html = ADMIN_HTML.read_text(encoding="utf-8")
    js = ADMIN_JS.read_text(encoding="utf-8")

    for element_id in (
        "fm-experiment-state",
        "fm-experiment-progress",
        "fm-experiment-meta",
        "fm-experiment-metrics",
        "fm-experiment-history",
        "fm-lock-btn",
        "fm-cancel-btn",
    ):
        assert f'id="{element_id}"' in html

    assert 'data-ui-click="fmLockExperiment()"' in html
    assert 'data-ui-click="fmCancelExperiment()"' in html
    assert 'data-ui-click="fmImportWatchlist()"' not in html
    assert "function fmImportWatchlist" not in js


def test_factor_mining_menu_matches_root_only_backend_contract():
    js = ADMIN_JS.read_text(encoding="utf-8")

    assert (
        "const ROOT_ONLY_PAGES = new Set(['factor-mining','ai-trade-decision']);"
        in js
    )


def test_factor_mining_refresh_ignores_older_response_that_finishes_last():
    result = _run_factor_refresh_race()

    assert result["runId"] == "run-new"
    assert result["experimentState"] == "当前无挑战者"
    assert result["standaloneExperimentState"] == "当前无挑战者"
    assert result["watchDate"] == "2026-07-30"
