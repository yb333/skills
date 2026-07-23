/**
 * Analyzer Agent 运营埋点接收服务端。
 *
 * 单文件实现：HTTP 服务 + SQLite 存储 + 聚合统计 + ECharts 看板。
 * 唯一外部依赖：better-sqlite3。
 *
 * 接口：
 *   POST /api/usage      接收单条或数组记录（INSERT OR IGNORE 幂等）
 *   GET  /api/stats      聚合统计 JSON
 *   GET  /api/health     健康检查
 *   GET  /               ECharts 看板
 *
 * 启动：
 *   npm install && npm start
 *   PORT=3000 node server.js
 */

const http = require('http');
const path = require('path');
const fs = require('fs');
// 用 Node 内置 node:sqlite（v22.5+ 自带，零外部依赖，无需编译）
// 替代 better-sqlite3 —— 后者是原生 C++ 模块，Windows 上常因缺编译工具链失败
const { DatabaseSync } = require('node:sqlite');

const PORT = parseInt(process.env.PORT || '3000', 10);

// 数据库路径：独立于代码目录，避免代码更新（git pull / 全量同步）覆盖用户数据。
// 默认放 ~/.analyzer-agent/（与客户端 usage.csv 同级），可用环境变量 USAGE_DB_PATH 覆盖。
const os = require('os');
const DEFAULT_DATA_DIR = path.join(os.homedir(), '.analyzer-agent');
const DATA_DIR = process.env.USAGE_DATA_DIR
  ? path.resolve(process.env.USAGE_DATA_DIR)
  : DEFAULT_DATA_DIR;
const DB_PATH = path.join(DATA_DIR, 'usage.db');

// ── 数据库初始化 ──────────────────────────────────────────────────────────

if (!fs.existsSync(DATA_DIR)) fs.mkdirSync(DATA_DIR, { recursive: true });

const db = new DatabaseSync(DB_PATH);
db.exec('PRAGMA journal_mode = WAL');
db.exec('PRAGMA busy_timeout = 5000');

// 固定列 schema（替代参考项目的 event+properties JSON 模式）
// user 是 SQL 保留字，列名用 user_name
db.exec(`
  CREATE TABLE IF NOT EXISTS usage_records (
    run_id TEXT PRIMARY KEY,
    timestamp TEXT NOT NULL,
    install_id TEXT NOT NULL,
    user_name TEXT,
    command TEXT NOT NULL,
    input_type TEXT,
    asset TEXT,
    target_table TEXT,
    batch_id TEXT,
    rule_count INTEGER,
    field_count INTEGER,
    source_count INTEGER,
    elapsed_sec REAL,
    elapsed_detail TEXT,
    status TEXT NOT NULL,
    error_type TEXT,
    quality_issues INTEGER,
    agent_version TEXT,
    python_version TEXT,
    os TEXT,
    received_at INTEGER NOT NULL
  );

  CREATE INDEX IF NOT EXISTS idx_usage_install_id ON usage_records(install_id);
  CREATE INDEX IF NOT EXISTS idx_usage_command ON usage_records(command);
  CREATE INDEX IF NOT EXISTS idx_usage_user ON usage_records(user_name);
  CREATE INDEX IF NOT EXISTS idx_usage_timestamp ON usage_records(timestamp);
  CREATE INDEX IF NOT EXISTS idx_usage_target ON usage_records(target_table);
  CREATE INDEX IF NOT EXISTS idx_usage_status ON usage_records(status);
`);

// ── 自动迁移：旧库升级时补 elapsed_detail 列（幂等，已有则跳过）──
try {
  const cols = db.prepare("PRAGMA table_info(usage_records)").all();
  if (!cols.some(c => c.name === 'elapsed_detail')) {
    db.exec('ALTER TABLE usage_records ADD COLUMN elapsed_detail TEXT');
    console.log('[Migration] Added column elapsed_detail');
  }
} catch (e) { /* 首次建表无此列检测可忽略 */ }

const INSERT_COLS = [
  'run_id', 'timestamp', 'install_id', 'user_name', 'command', 'input_type',
  'asset', 'target_table', 'batch_id', 'rule_count', 'field_count',
  'source_count', 'elapsed_sec', 'elapsed_detail', 'status', 'error_type',
  'quality_issues', 'agent_version', 'python_version', 'os'
];

const insertStmt = db.prepare(
  `INSERT OR IGNORE INTO usage_records (${INSERT_COLS.join(', ')}, received_at)
   VALUES (${INSERT_COLS.map(() => '?').join(', ')}, ?)`
);

// ── HTTP 工具 ────────────────────────────────────────────────────────────

function corsHeaders() {
  return {
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type',
  };
}

function jsonReply(res, code, data) {
  const body = JSON.stringify(data);
  res.writeHead(code, { 'Content-Type': 'application/json; charset=utf-8', ...corsHeaders() });
  res.end(body);
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    let chunks = [];
    req.on('data', (c) => chunks.push(c));
    req.on('end', () => resolve(Buffer.concat(chunks).toString('utf-8')));
    req.on('error', reject);
  });
}

function toInt(v) {
  const n = parseInt(v, 10);
  return Number.isFinite(n) ? n : null;
}
function toFloat(v) {
  const n = parseFloat(v);
  return Number.isFinite(n) ? n : null;
}
function toStr(v) {
  return v === undefined || v === null ? null : String(v);
}

// ── POST /api/usage ──────────────────────────────────────────────────────

function handlePostUsage(payload) {
  // 支持单条对象或数组
  let records;
  if (Array.isArray(payload)) {
    records = payload;
  } else if (payload && typeof payload === 'object') {
    records = [payload];
  } else {
    return { statusCode: 400, body: { ok: false, error: 'Invalid payload' } };
  }
  if (records.length === 0) {
    return { statusCode: 400, body: { ok: false, error: 'Empty records' } };
  }

  const now = Date.now();
  let received = 0;

  // node:sqlite 无 db.transaction() helper，手动 BEGIN/COMMIT/ROLLBACK
  try {
    db.exec('BEGIN');
    for (const r of records) {
      if (!r || !r.run_id || !r.command) continue;
      const result = insertStmt.run(
        toStr(r.run_id), toStr(r.timestamp), toStr(r.install_id), toStr(r.user_name),
        toStr(r.command), toStr(r.input_type), toStr(r.asset), toStr(r.target_table),
        toStr(r.batch_id), toInt(r.rule_count), toInt(r.field_count), toInt(r.source_count),
        toFloat(r.elapsed_sec), toStr(r.elapsed_detail), toStr(r.status) || 'unknown',
        toStr(r.error_type), toInt(r.quality_issues), toStr(r.agent_version),
        toStr(r.python_version), toStr(r.os), now
      );
      if (result.changes > 0) received++;
    }
    db.exec('COMMIT');
  } catch (err) {
    try { db.exec('ROLLBACK'); } catch (e) { /* ignore */ }
    return { statusCode: 500, body: { ok: false, error: 'Database error: ' + err.message } };
  }

  const ts = new Date().toISOString();
  console.log(`[${ts}] Received ${received}/${records.length} records`);
  return { statusCode: 200, body: { ok: true, received } };
}

// ── GET /api/health ──────────────────────────────────────────────────────

function handleHealth() {
  let size = 0;
  try {
    size = fs.statSync(DB_PATH).size;
    // 加上 WAL 文件
    const walPath = DB_PATH + '-wal';
    if (fs.existsSync(walPath)) size += fs.statSync(walPath).size;
  } catch (e) { /* ignore */ }
  return { status: 'ok', uptime: Math.floor(process.uptime()), db_size_mb: Math.round(size / 1048576 * 100) / 100 };
}

// ── GET /api/stats ───────────────────────────────────────────────────────

function handleStats() {
  const result = {};

  // 1. 概览
  result.overview = db.prepare(`
    SELECT
      COUNT(DISTINCT install_id) AS total_users,
      COUNT(DISTINCT user_name) AS total_user_names,
      COUNT(*) AS total_records,
      COUNT(DISTINCT CASE WHEN status='ok' THEN run_id END) AS total_ok,
      COUNT(DISTINCT CASE WHEN status='error' THEN run_id END) AS total_error,
      ROUND(AVG(elapsed_sec), 2) AS avg_elapsed_sec,
      MIN(timestamp) AS first_event,
      MAX(timestamp) AS last_event
    FROM usage_records
  `).get();

  // 2. 日活趋势（近 30 天，按天）
  const thirtyDaysAgo = new Date(Date.now() - 30 * 24 * 3600 * 1000).toISOString();
  result.daily_active = db.prepare(`
    SELECT
      substr(timestamp, 1, 10) AS date,
      COUNT(DISTINCT install_id) AS users,
      COUNT(*) AS records
    FROM usage_records
    WHERE timestamp >= ?
    GROUP BY date
    ORDER BY date DESC
  `).all(thirtyDaysAgo);

  // 3. 命令热度
  result.command_usage = db.prepare(`
    SELECT command, COUNT(*) AS count, MAX(timestamp) AS last_used
    FROM usage_records GROUP BY command ORDER BY count DESC
  `).all();

  // 4. 用户活跃度
  result.user_activity = db.prepare(`
    SELECT
      COALESCE(user_name, '(unknown)') AS user_name,
      COUNT(*) AS count,
      MAX(timestamp) AS last_active
    FROM usage_records GROUP BY user_name ORDER BY count DESC LIMIT 50
  `).all();

  // 5. 资产分析 Top10
  result.top_assets = db.prepare(`
    SELECT
      COALESCE(target_table, asset, '(unknown)') AS asset,
      COUNT(*) AS count,
      MAX(timestamp) AS last_analyzed
    FROM usage_records
    WHERE target_table IS NOT NULL OR asset IS NOT NULL
    GROUP BY asset ORDER BY count DESC LIMIT 10
  `).all();

  // 6. 错误类型分布
  result.error_types = db.prepare(`
    SELECT
      COALESCE(error_type, '(none)') AS error_type,
      COUNT(*) AS count,
      MAX(timestamp) AS last_seen
    FROM usage_records
    WHERE status = 'error'
    GROUP BY error_type ORDER BY count DESC
  `).all();

  // 7. 输入类型占比
  result.input_types = db.prepare(`
    SELECT
      COALESCE(input_type, '(unknown)') AS input_type,
      COUNT(*) AS count
    FROM usage_records GROUP BY input_type ORDER BY count DESC
  `).all();

  // 8. 版本分布
  result.versions = db.prepare(`
    SELECT
      COALESCE(agent_version, '(unknown)') AS agent_version,
      COUNT(DISTINCT install_id) AS users,
      COUNT(*) AS count
    FROM usage_records GROUP BY agent_version ORDER BY count DESC
  `).all();

  // 9. OS 分布
  result.os_distribution = db.prepare(`
    SELECT
      COALESCE(os, '(unknown)') AS os,
      COUNT(DISTINCT install_id) AS users,
      COUNT(*) AS count
    FROM usage_records GROUP BY os ORDER BY count DESC
  `).all();

  return result;
}

// ── GET / 看板 ────────────────────────────────────────────────────────────

function handleDashboard() {
  const html = `<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Analyzer Agent 运营数据</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"><\/script>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif; background: #f5f7fa; color: #333; padding: 20px; }
  h1 { font-size: 22px; margin-bottom: 4px; }
  .subtitle { color: #888; font-size: 13px; margin-bottom: 20px; }
  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 16px; margin-bottom: 24px; }
  .card { background: #fff; border-radius: 10px; padding: 18px; box-shadow: 0 1px 4px rgba(0,0,0,0.06); }
  .card .label { font-size: 12px; color: #888; margin-bottom: 6px; }
  .card .value { font-size: 28px; font-weight: 600; color: #1a73e8; }
  .card .sub { font-size: 11px; color: #aaa; margin-top: 4px; }
  .section { background: #fff; border-radius: 10px; padding: 18px; margin-bottom: 20px; box-shadow: 0 1px 4px rgba(0,0,0,0.06); }
  .section h2 { font-size: 15px; margin-bottom: 12px; border-left: 3px solid #1a73e8; padding-left: 8px; }
  .chart { width: 100%; height: 320px; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { padding: 8px 10px; text-align: left; border-bottom: 1px solid #eee; }
  th { color: #888; font-weight: 500; background: #fafbfc; }
  tr:hover { background: #fafbfc; }
  .bar-cell { position: relative; min-width: 120px; }
  .bar-fill { background: #1a73e8; height: 6px; border-radius: 3px; margin-top: 4px; }
  .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
  @media (max-width: 768px) { .grid-2 { grid-template-columns: 1fr; } }
  #loading { text-align: center; padding: 60px; color: #888; }
</style>
</head>
<body>
<h1>Analyzer Agent 运营数据</h1>
<div class="subtitle" id="lastUpdate">加载中...</div>

<div id="loading">正在加载...</div>
<div id="content" style="display:none">

  <div class="cards" id="overviewCards"></div>

  <div class="section">
    <h2>日活趋势（近 30 天）</h2>
    <div id="dailyChart" class="chart"></div>
  </div>

  <div class="grid-2">
    <div class="section">
      <h2>命令热度</h2>
      <table><thead><tr><th>命令</th><th>次数</th><th>最后使用</th></tr></thead><tbody id="cmdTable"></tbody></table>
    </div>
    <div class="section">
      <h2>输入类型占比</h2>
      <div id="inputChart" class="chart" style="height:260px"></div>
    </div>
  </div>

  <div class="grid-2">
    <div class="section">
      <h2>用户活跃度</h2>
      <table><thead><tr><th>用户</th><th>次数</th><th>最后活跃</th></tr></thead><tbody id="userTable"></tbody></table>
    </div>
    <div class="section">
      <h2>资产分析 Top10</h2>
      <table><thead><tr><th>资产</th><th>分析次数</th><th>最后分析</th></tr></thead><tbody id="assetTable"></tbody></table>
    </div>
  </div>

  <div class="grid-2">
    <div class="section">
      <h2>错误类型分布</h2>
      <div id="errorChart" class="chart" style="height:260px"></div>
    </div>
    <div class="section">
      <h2>环境分布</h2>
      <table><thead><tr><th>OS</th><th>用户数</th><th>记录数</th></tr></thead><tbody id="osTable"></tbody></table>
    </div>
  </div>

</div>

<script>
var COMMAND_NAMES = {
  'analyze': '资产文档化',
  'analyze-chain': '多规则组链路分析',
  'analyze-batch': '批量文档化',
  'impact-analysis': '关联影响分析',
  'impact-analysis-cross': '跨资产影响分析',
  'field-search': '字段使用检索'
};
var INPUT_NAMES = {
  'xlsx': '术加制品包Excel',
  'yml_dir': '代码仓yml目录',
  'table_name': '仅表名',
  'impact_excel': '变更清单Excel',
  'field_list': '字段列表'
};

function fmtTime(s) {
  if (!s) return '-';
  return s.replace('T', ' ').substring(0, 19);
}
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"]/g, function(c) {
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];
  });
}
function cmdLabel(c) { return COMMAND_NAMES[c] || c || '(unknown)'; }
function inputLabel(i) { return INPUT_NAMES[i] || i || '(unknown)'; }

function renderOverview(o) {
  var html = '';
  html += card('总用户数', o.total_users || 0, '按 install_id 去重');
  html += card('总记录数', o.total_records || 0,
    '成功 ' + (o.total_ok||0) + ' / 失败 ' + (o.total_error||0));
  html += card('平均耗时', (o.avg_elapsed_sec || 0) + 's', '每条平均');
  html += card('最后活跃', fmtTime(o.last_event).substring(0, 16), '');
  document.getElementById('overviewCards').innerHTML = html;
}
function card(label, value, sub) {
  return '<div class="card"><div class="label">' + label + '</div>' +
    '<div class="value">' + value + '</div>' +
    (sub ? '<div class="sub">' + sub + '</div>' : '') + '</div>';
}

function renderDaily(da) {
  var el = document.getElementById('dailyChart');
  var chart = echarts.init(el);
  var dates = da.map(function(r){return r.date;}).reverse();
  var users = da.map(function(r){return r.users;}).reverse();
  var records = da.map(function(r){return r.records;}).reverse();
  chart.setOption({
    tooltip: { trigger: 'axis' },
    legend: { data: ['用户数', '记录数'] },
    grid: { left: 40, right: 40, bottom: 40, top: 40 },
    xAxis: { type: 'category', data: dates, axisLabel: { rotate: 45, fontSize: 10 } },
    yAxis: [
      { type: 'value', name: '用户数', position: 'left' },
      { type: 'value', name: '记录数', position: 'right' }
    ],
    series: [
      { name: '用户数', type: 'line', smooth: true, data: users,
        itemStyle: { color: '#1a73e8' }, areaStyle: { opacity: 0.1 } },
      { name: '记录数', type: 'line', smooth: true, yAxisIndex: 1, data: records,
        itemStyle: { color: '#34a853' }, areaStyle: { opacity: 0.1 } }
    ]
  });
  window.addEventListener('resize', function(){ chart.resize(); });
}

function renderTable(id, rows, mapper) {
  var html = rows.map(function(r) {
    return '<tr>' + mapper(r).map(function(c){return '<td>'+esc(c)+'</td>';}).join('') + '</tr>';
  }).join('');
  document.getElementById(id).innerHTML = html;
}

function renderPie(id, data, nameMapper) {
  var el = document.getElementById(id);
  if (!el || !data.length) { if(el) el.innerHTML='<div style="color:#aaa;padding:20px;text-align:center">暂无数据</div>'; return; }
  var chart = echarts.init(el);
  var pieData = data.map(function(d) {
    return { name: nameMapper(d[Object.keys(d)[0]]), value: d.count };
  });
  chart.setOption({
    tooltip: { trigger: 'item', formatter: '{b}: {c} ({d}%)' },
    series: [{
      type: 'pie', radius: ['40%', '70%'], data: pieData,
      label: { fontSize: 11 }
    }]
  });
  window.addEventListener('resize', function(){ chart.resize(); });
}

function renderErrorBar(errors) {
  var el = document.getElementById('errorChart');
  if (!errors.length) { el.innerHTML = '<div style="color:#34a853;padding:20px;text-align:center">🎉 无错误记录</div>'; return; }
  var chart = echarts.init(el);
  chart.setOption({
    tooltip: { trigger: 'axis' },
    grid: { left: 100, right: 20, top: 10, bottom: 20 },
    xAxis: { type: 'value' },
    yAxis: { type: 'category', data: errors.map(function(e){return e.error_type;}) },
    series: [{
      type: 'bar', data: errors.map(function(e){return e.count;}),
      itemStyle: { color: '#ea4335' }
    }]
  });
  window.addEventListener('resize', function(){ chart.resize(); });
}

function render(data) {
  renderOverview(data.overview || {});
  renderDaily(data.daily_active || []);
  renderTable('cmdTable', data.command_usage || [], function(r) {
    return [cmdLabel(r.command), r.count, fmtTime(r.last_used)];
  });
  renderTable('userTable', data.user_activity || [], function(r) {
    return [r.user_name, r.count, fmtTime(r.last_active)];
  });
  renderTable('assetTable', data.top_assets || [], function(r) {
    return [r.asset, r.count, fmtTime(r.last_analyzed)];
  });
  renderTable('osTable', data.os_distribution || [], function(r) {
    return [r.os, r.users, r.count];
  });
  renderPie('inputChart', data.input_types || [], inputLabel);
  renderErrorBar(data.error_types || []);
}

function refresh() {
  fetch('/api/stats').then(function(r){return r.json();}).then(function(data){
    document.getElementById('loading').style.display = 'none';
    document.getElementById('content').style.display = 'block';
    document.getElementById('lastUpdate').textContent = '更新于 ' + new Date().toLocaleTimeString('zh-CN');
    setTimeout(function(){ render(data); }, 0);
  }).catch(function(err){
    document.getElementById('loading').textContent = '加载失败: ' + err.message;
  });
}
refresh();
setInterval(refresh, 60000);
<\/script>
</body>
</html>`;
  return html;
}

// ── 路由分发 ──────────────────────────────────────────────────────────────

const server = http.createServer(async (req, res) => {
  // CORS preflight
  if (req.method === 'OPTIONS') {
    res.writeHead(204, corsHeaders());
    res.end();
    return;
  }

  const url = new URL(req.url, 'http://localhost');
  const pathname = url.pathname;

  if (req.method === 'POST' && pathname === '/api/usage') {
    const raw = await readBody(req);
    let payload;
    try {
      payload = JSON.parse(raw);
    } catch (e) {
      jsonReply(res, 400, { ok: false, error: 'Invalid JSON: ' + e.message });
      return;
    }
    const result = handlePostUsage(payload);
    jsonReply(res, result.statusCode, result.body);
    return;
  }

  if (req.method === 'GET' && pathname === '/api/stats') {
    try {
      jsonReply(res, 200, handleStats());
    } catch (e) {
      jsonReply(res, 500, { ok: false, error: e.message });
    }
    return;
  }

  if (req.method === 'GET' && pathname === '/api/health') {
    jsonReply(res, 200, handleHealth());
    return;
  }

  if (req.method === 'GET' && (pathname === '/' || pathname === '/index.html')) {
    const html = handleDashboard();
    res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
    res.end(html);
    return;
  }

  jsonReply(res, 404, { error: 'Not found: ' + pathname });
});

server.listen(PORT, '0.0.0.0', () => {
  console.log(`[Analyzer Agent Telemetry] 监听 http://0.0.0.0:${PORT}`);
  console.log(`  看板:    http://localhost:${PORT}/`);
  console.log(`  上报:    POST http://<server-ip>:${PORT}/api/usage`);
  console.log(`  统计:    GET  http://localhost:${PORT}/api/stats`);
  console.log(`  数据库:  ${DB_PATH}`);
});
