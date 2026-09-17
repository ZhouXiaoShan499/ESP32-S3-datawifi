/*
 * ESP32-S3 IMU 实时监控面板
 *
 * 定时轮询查询接口：
 *   GET /api/v1/devices              -> 设备下拉
 *   GET /api/v1/latest?device_id=..  -> 选中设备的最新数据
 * 能力：设备下拉选择、最新数据展示、无数据/无设备提示、更新状态（更新中/未更新）。
 */

'use strict';

const POLL_MS = 800;           // 轮询周期（原 2000，压到 800ms 提升实时感）
const STALE_S = 30;            // 超过该秒数视为「未更新」（与服务端 / 保持一致）
const NUM = 5;                 // 展示头部/尾部样本条数
const CHART_WINDOW_S = 30;     // 波形窗口秒数（与 /api/v1/window?seconds= 一致）
const CHART_AXES = ['ax', 'ay', 'az'];
const CHART_COLORS = { ax: '#cf222e', ay: '#1a7f37', az: '#2563eb' };

const el = (id) => document.getElementById(id);
const $ = {
  deviceSel: el('deviceSel'),
  refreshBtn: el('refreshBtn'),
  lastPoll: el('lastPoll'),
  noDeviceMsg: el('noDeviceMsg'),
  statusBar: el('statusBar'),
  noDataMsg: el('noDataMsg'),
  dataCard: el('dataCard'),
  cardTitle: el('cardTitle'),
  stateBadge: el('stateBadge'),
  vDevice: el('vDevice'),
  vSourceUnit: el('vSourceUnit'),
  vCount: el('vCount'),
  vTsMs: el('vTsMs'),
  vReceived: el('vReceived'),
  vIp: el('vIp'),
  tbHead: el('tbHead'),
  tbTail: el('tbTail'),
  chart: el('chart'),
  chartWin: el('chartWin'),
  chartUnit: el('chartUnit'),
  chartCount: el('chartCount'),
};

const state = { busy: false, devices: [], current: '', lastPoints: [], unit: '' };

/* ------------------------------------------------------------------ *
 * 工具
 * ------------------------------------------------------------------ */

/** 容错 fetch：网络/非 2xx/解析失败都抛出带可读信息的 Error */
async function apiGet(path) {
  let res;
  try {
    res = await fetch(path, { cache: 'no-store' });
  } catch (e) {
    throw new Error('无法连接服务器，请确认服务端已启动：' + path);
  }
  if (!res.ok) {
    throw new Error('接口返回 ' + res.status + '：' + path);
  }
  let data;
  try {
    data = await res.json();
  } catch (e) {
    throw new Error('响应不是合法 JSON：' + path);
  }
  return data;
}

function fmtTsMs(tsMs) {
  if (tsMs === null || tsMs === undefined) return '—';
  const d = new Date(tsMs);
  if (isNaN(d.getTime())) return String(tsMs);
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ` +
         `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function fmtNum(v) {
  return (v === null || v === undefined || isNaN(v)) ? '—' : Number(v).toFixed(4);
}

/* ------------------------------------------------------------------ *
 * 渲染
 * ------------------------------------------------------------------ */

function renderBadge(stateCode, text) {
  return `<span id="stateBadge" class="badge ${stateCode}">${text}</span>`;
}

/** 依据服务端接收时间距当前的秒数判定 更新中/未更新；无法判定则 NO_DATA */
function judgeAge(receivedAt) {
  if (receivedAt === null || receivedAt === undefined) return null; // 未知
  const now = Date.now() / 1000;
  const age = Math.max(0, now - receivedAt);
  return { age, fresh: age <= STALE_S };
}

function renderStatus(msg, kind) {
  if (kind === 'hidden') {
    $.statusBar.classList.add('hidden');
    return;
  }
  $.statusBar.classList.remove('hidden');
  $.statusBar.innerHTML =
    `<div class="card"><span class="${kind === 'error' ? 'err-msg' : ''}">${msg}</span></div>`;
}

function renderSamples(tbody, rows) {
  tbody.innerHTML = '';
  if (!rows || rows.length === 0) {
    tbody.innerHTML = '<tr><td colspan="4">—</td></tr>';
    return;
  }
  const frag = document.createDocumentFragment();
  for (const s of rows) {
    const tr = document.createElement('tr');
    tr.innerHTML =
      `<td>${s.t_ms}</td><td>${fmtNum(s.ax)}</td>` +
      `<td>${fmtNum(s.ay)}</td><td>${fmtNum(s.az)}</td>`;
    frag.appendChild(tr);
  }
  tbody.appendChild(frag);
}

function showNoData(hasDevices) {
  $.noDeviceMsg.classList.toggle('hidden', hasDevices);
  $.noDataMsg.classList.toggle('hidden', !hasDevices);
  $.dataCard.classList.add('hidden');
  state.lastPoints = [];      // 无数据时清空波形，避免残留上一设备的曲线
  drawChart([]);
}

function renderLatest(data) {
  if (!data || !data.found) {
    // 设备列表里存在该设备，但尚无该设备的采样记录 -> NO DATA
    showNoData(state.devices.length > 0);
    return;
  }
  $.noDeviceMsg.classList.add('hidden');
  $.noDataMsg.classList.add('hidden');
  $.dataCard.classList.remove('hidden');

  const up = data.upload || {};
  const unit = up.unit || '';
  const source = up.source || '';
  const ip = up.ip || '';

  state.unit = unit;
  if ($.chartUnit) $.chartUnit.textContent = unit || 'm/s^2';

  $.vDevice.textContent = up.device_id || '—';
  $.vSourceUnit.textContent = (source || '—') + (unit ? ' (' + unit + ')' : '');
  $.vCount.textContent = (up.sample_count ?? '—') + ' 点';
  $.vTsMs.textContent = fmtTsMs(up.ts_ms) + '  (' + (up.ts_ms ?? '—') + ')';
  $.vReceived.textContent = up.received_at_str || fmtTsMs(up.received_at * 1000);
  $.vIp.textContent = ip || '—';

  const st = judgeAge(up.received_at);
  let badge = '';
  if (st === null) {
    badge = renderBadge('err', '无数据');
  } else if (st.fresh) {
    badge = renderBadge('ok', '更新中 · ' + st.age.toFixed(0) + 's 前');
  } else {
    badge = renderBadge('warn', '未更新 · ' + st.age.toFixed(0) + 's 前');
  }
  $.stateBadge.outerHTML = badge;
  $.stateBadge = el('stateBadge'); // 重新绑定（外层 HTML 已被替换）

  // 样本头/尾（最多 NUM 条）
  const head = (data.sample_head || []).slice(0, NUM);
  const tail = (data.sample_tail || []).slice(-NUM);
  renderSamples($.tbHead, head);
  renderSamples($.tbTail, tail);
}

/* ------------------------------------------------------------------ *
 *  实时波形（Canvas 2D，零第三方依赖）
 * ------------------------------------------------------------------ */

/** 按 devicePixelRatio 重置绘制缓冲区，保证高清屏不糊 */
function fitCanvas(cv, ctx) {
  const dpr = window.devicePixelRatio || 1;
  const cssW = cv.clientWidth || 900;
  const cssH = cv.clientHeight || 240;
  const bufW = Math.round(cssW * dpr);
  const bufH = Math.round(cssH * dpr);
  if (cv.width !== bufW || cv.height !== bufH) {
    cv.width = bufW;
    cv.height = bufH;
  }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { w: cssW, h: cssH };
}

function drawChartEmpty(ctx, w, h, box, text) {
  ctx.fillStyle = '#6b7280';
  ctx.font = '13px "Segoe UI", "Microsoft YaHei", sans-serif';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.fillText(text, box.x + box.w / 2, box.y + box.h / 2);
}

/**
 * 画 ax/ay/az 三条滚动曲线。
 * points: [{t: 绝对 epoch ms, ax, ay, az}, ...]（按 t 升序）
 * X 轴固定为 [tEnd - CHART_WINDOW_S, tEnd] 的滚动窗口，tEnd = 最后一个点的时间。
 */
function drawChart(points) {
  const cv = $.chart;
  if (!cv || !cv.getContext) return;
  const ctx = cv.getContext('2d');
  const { w, h } = fitCanvas(cv, ctx);
  ctx.clearRect(0, 0, w, h);

  const box = { x: 52, y: 14, w: w - 52 - 12, h: h - 14 - 24 };
  if (box.w <= 0 || box.h <= 0) return;

  // 绘图区背景 + 边框
  ctx.fillStyle = '#ffffff';
  ctx.fillRect(box.x, box.y, box.w, box.h);
  ctx.strokeStyle = '#dfe3e8';
  ctx.lineWidth = 1;
  ctx.strokeRect(box.x + 0.5, box.y + 0.5, box.w - 1, box.h - 1);

  if ($.chartCount) {
    $.chartCount.textContent = (points ? points.length : 0) + ' 点';
  }

  if (!points || points.length === 0) {
    drawChartEmpty(ctx, w, h, box, '等待数据…（板端开始采集后自动出现波形）');
    return;
  }

  const spanMs = CHART_WINDOW_S * 1000;
  const tEnd = points[points.length - 1].t;
  const tStart = tEnd - spanMs;

  // Y 轴：只对窗口内可见点求幅值，避免历史极值压扁当前波形
  let maxAbs = 1;
  for (const p of points) {
    if (p.t < tStart) continue;
    for (const a of CHART_AXES) {
      const v = Math.abs(p[a]);
      if (isFinite(v) && v > maxAbs) maxAbs = v;
    }
  }
  const yMax = maxAbs * 1.1;
  const yMin = -yMax;

  const xOf = (t) => box.x + ((t - tStart) / spanMs) * box.w;
  const yOf = (v) => box.y + box.h - ((v - yMin) / (yMax - yMin)) * box.h;

  // 横向网格 + Y 刻度（5 等分，中间那条即 0 线，画深一点）
  ctx.font = '11px "Segoe UI", "Microsoft YaHei", sans-serif';
  ctx.textBaseline = 'middle';
  ctx.textAlign = 'right';
  for (let i = 0; i <= 4; i++) {
    const v = yMin + ((yMax - yMin) * i) / 4;
    const y = yOf(v);
    ctx.strokeStyle = (i === 2) ? '#c8ccd2' : '#eef1f5';
    ctx.beginPath();
    ctx.moveTo(box.x, y + 0.5);
    ctx.lineTo(box.x + box.w, y + 0.5);
    ctx.stroke();
    ctx.fillStyle = '#6b7280';
    ctx.fillText(v.toFixed(1), box.x - 6, y);
  }

  // 纵向网格 + X 刻度（每 5 s 一格，最右为 now）
  ctx.textAlign = 'center';
  ctx.textBaseline = 'top';
  for (let s = 0; s <= CHART_WINDOW_S; s += 5) {
    const x = xOf(tEnd - s * 1000);
    ctx.strokeStyle = '#eef1f5';
    ctx.beginPath();
    ctx.moveTo(x + 0.5, box.y);
    ctx.lineTo(x + 0.5, box.y + box.h);
    ctx.stroke();
    ctx.fillStyle = '#6b7280';
    ctx.fillText(s === 0 ? 'now' : '-' + s + 's', x, box.y + box.h + 6);
  }

  // 三条曲线（裁剪到绘图区，防止越界绘制）
  ctx.save();
  ctx.beginPath();
  ctx.rect(box.x, box.y, box.w, box.h);
  ctx.clip();
  ctx.lineJoin = 'round';
  for (const a of CHART_AXES) {
    ctx.strokeStyle = CHART_COLORS[a];
    ctx.lineWidth = 1.3;
    ctx.beginPath();
    let started = false;
    for (const p of points) {
      if (p.t < tStart) continue;          // 窗口外的旧点跳过
      const v = p[a];
      if (!isFinite(v)) { started = false; continue; }
      const x = xOf(p.t);
      const y = yOf(v);
      if (!started) { ctx.moveTo(x, y); started = true; }
      else { ctx.lineTo(x, y); }
    }
    ctx.stroke();
  }
  ctx.restore();

  // 图例
  ctx.font = '11px "Segoe UI", "Microsoft YaHei", sans-serif';
  ctx.textAlign = 'left';
  ctx.textBaseline = 'middle';
  let lx = box.x + 8;
  const ly = box.y + 10;
  for (const a of CHART_AXES) {
    ctx.strokeStyle = CHART_COLORS[a];
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.moveTo(lx, ly);
    ctx.lineTo(lx + 16, ly);
    ctx.stroke();
    ctx.fillStyle = '#1f2933';
    ctx.fillText(a, lx + 20, ly);
    lx += 48;
  }
}

/* ------------------------------------------------------------------ *
 * 轮询主流程
 * ------------------------------------------------------------------ */

async function poll() {
  if (state.busy) return; // 上一次未完成则跳过本轮
  state.busy = true;
  $.refreshBtn.disabled = true;
  try {
    // 1) 设备列表
    let data;
    try {
      data = await apiGet('/api/v1/devices');
    } catch (e) {
      renderStatus('⚠ ' + e.message, 'error');
      showNoData(false);
      return;
    }
    renderStatus('', 'hidden'); // 清空状态条

    const devs = (data && data.devices) || [];
    const ids = devs.map((d) => d.device_id);
    state.devices = ids;

    // 保留当前选择；若其已消失则回退第一个
    if (!(state.current && ids.includes(state.current))) {
      state.current = ids.length ? ids[0] : '';
    }
    rebuildSelect(devs);

    if (!ids.length) {
      showNoData(false);
      return;
    }
    if (!state.current) return;

    // 2) 最新数据
    const path = '/api/v1/latest?device_id=' + encodeURIComponent(state.current);
    const latest = await apiGet(path);
    renderLatest(latest);

    // 3) 实时波形：跨批次取连续样本（失败不影响上方数据卡片）
    const winPath = '/api/v1/window?device_id=' +
                    encodeURIComponent(state.current) +
                    '&seconds=' + CHART_WINDOW_S;
    try {
      const win = await apiGet(winPath);
      state.lastPoints = (win && win.points) || [];
    } catch (e) {
      state.lastPoints = [];
    }
    drawChart(state.lastPoints);
  } catch (e) {
    renderStatus('⚠ ' + e.message, 'error');
  } finally {
    state.busy = false;
    $.refreshBtn.disabled = false;
    $.lastPoll.textContent =
      '最近刷新 ' + new Date().toLocaleTimeString('zh-CN', { hour12: false });
  }
}

function rebuildSelect(devs) {
  const sel = $.deviceSel;
  sel.innerHTML = '';
  for (const d of devs) {
    const opt = document.createElement('option');
    opt.value = d.device_id;
    opt.textContent = d.device_id;
    if (d.device_id === state.current) opt.selected = true;
    sel.appendChild(opt);
  }
  const empty = !devs.length;
  sel.disabled = empty;
  $.refreshBtn.disabled = empty;
  sel.title = empty ? '暂无设备' : '';
}

/* ------------------------------------------------------------------ *
 * 事件 & 启动
 * ------------------------------------------------------------------ */

$.deviceSel.addEventListener('change', () => {
  state.current = $.deviceSel.value;
  poll();
});

$.refreshBtn.addEventListener('click', () => poll());

// 窗口尺寸变化时按新宽度重绘（用最近一次的点，避免额外请求）
window.addEventListener('resize', () => drawChart(state.lastPoints || []));

// 标题里的窗口秒数与常量保持同步
if ($.chartWin) $.chartWin.textContent = String(CHART_WINDOW_S);

// 立即轮询一次，再周期轮询
poll();
setInterval(poll, POLL_MS);

// 页面加载时先画一次空波形，避免 canvas 区域空白
drawChart([]);


