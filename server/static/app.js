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
  captureBtn: el('captureBtn'),
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
  vTrigger: el('vTrigger'),
  vRequestId: el('vRequestId'),
  tbHead: el('tbHead'),
  tbTail: el('tbTail'),
  chart: el('chart'),
  chartWin: el('chartWin'),
  chartUnit: el('chartUnit'),
  chartCount: el('chartCount'),
  taskBadge: el('taskBadge'),
  taskRid: el('taskRid'),
  taskStatus: el('taskStatus'),
  taskSpec: el('taskSpec'),
  taskExpires: el('taskExpires'),
  scene3d: el('scene3d'),
  sceneInfo: el('sceneInfo'),
  sceneWin: el('sceneWin'),
  autoRotate: el('autoRotate'),
  resetView: el('resetView'),
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

/** 容错 POST JSON：网络/非 2xx 都抛出带可读信息的 Error（优先用服务端 error 字段） */
async function apiPost(path, body) {
  let res;
  try {
    res = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
      cache: 'no-store',
    });
  } catch (e) {
    throw new Error('无法连接服务器，请确认服务端已启动：' + path);
  }
  let data = null;
  try {
    data = await res.json();
  } catch (e) {
    data = null;
  }
  if (!res.ok) {
    const msg = (data && data.error) ? data.error : ('接口返回 ' + res.status);
    throw new Error(msg + '：' + path);
  }
  if (!data) throw new Error('响应不是合法 JSON：' + path);
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
    renderTrace(latest);

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
    drawScene3D(state.lastPoints);
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

/* ------------------------------------------------------------------ *
 *  按需采集任务（manual capture task）
 *
 *  「仅刷新」只重新拉查询接口；「采集一次最新数据」先 POST 建任务，板端轮询领取后
 *  暂停周期上报、按任务节拍采一批数据并带 request_id 回传，服务端在写库的同一
 *  事务里把任务置 completed。
 * ------------------------------------------------------------------ */

const TASK = {
  samples: 100,      // 单次采集样本数（100 Hz × 1 s）
  rateHz: 100,       // 采样率，与板端 QMA6100P 一致
  timeoutS: 60,      // 任务有效期（服务端过期 → timeout）
  pollMs: 1000,      // 任务状态查询间隔
};

const TASK_STATUS_META = {
  submitted: { cls: 'warn', text: '已提交', hint: '等待板端轮询领取（板端每 3 s 轮询一次）' },
  dispatched: { cls: 'warn', text: '已下发', hint: '板端已领取任务，正在采集' },
  acked: { cls: 'warn', text: '设备接收', hint: '板端已回执，采集/上传进行中' },
  completed: { cls: 'ok', text: '完成', hint: '数据已入库，样本来自本组设备' },
  failed: { cls: 'err', text: '失败', hint: '被板端或服务端校验拒绝' },
  timeout: { cls: 'err', text: '超时', hint: '有效期内未完成，不会被再次下发' },
};

const taskState = { rid: '', timer: null, last: null };

function renderTaskBadge(status) {
  const meta = TASK_STATUS_META[status] || { cls: 'off', text: status || '空闲', hint: '' };
  if ($.taskBadge) {
    $.taskBadge.className = 'badge ' + meta.cls;
    $.taskBadge.textContent = meta.text;
  }
  return meta;
}

function renderTask(task, note) {
  if (!task) return;
  const meta = renderTaskBadge(task.status);
  taskState.last = task;
  taskState.rid = task.request_id || taskState.rid;

  if ($.taskRid) $.taskRid.textContent = task.request_id || '—';
  if ($.taskSpec) {
    $.taskSpec.textContent = (task.sample_count ?? '—') + ' 点 @ ' +
                             (task.sample_rate_hz ?? '—') + ' Hz';
  }
  if ($.taskStatus) {
    let text = meta.text + '（状态码 ' + task.status + '）· ' + meta.hint;
    if (task.error) text += ' · error: ' + task.error;
    if (note) text += ' · ' + note;
    $.taskStatus.textContent = text;
  }
  if ($.taskExpires) {
    if (task.terminal) {
      $.taskExpires.textContent = task.completed_at_str
        ? '已结束 ' + task.completed_at_str
        : '已结束';
    } else {
      const left = Number(task.expires_in_s);
      $.taskExpires.textContent = isFinite(left)
        ? (left > 0 ? left.toFixed(0) + ' s 后超时' : '已过期')
        : '—';
    }
  }
}

function stopTaskWatch() {
  if (taskState.timer) {
    clearInterval(taskState.timer);
    taskState.timer = null;
  }
}

/** 以 1 s 间隔查询 /api/v1/tasks/{request_id}，终态后停止并补拉一次数据 */
function watchTask(requestId) {
  stopTaskWatch();
  const tick = async () => {
    try {
      const data = await apiGet('/api/v1/tasks/' + encodeURIComponent(requestId));
      renderTask(data.task);
      if (data.task && data.task.terminal) {
        stopTaskWatch();
        poll();          // 任务完成后立刻刷新数据面板，不用等下一次轮询
      }
    } catch (e) {
      stopTaskWatch();
      renderStatus('⚠ 任务状态查询失败：' + e.message, 'error');
    }
  };
  taskState.timer = setInterval(tick, TASK.pollMs);
  tick();
}

/** 打开页面/切换设备时回填该设备最近的任务，避免刷新后卡片空白 */
async function refreshLatestTask() {
  if (!state.current) return;
  try {
    const data = await apiGet('/api/v1/tasks?device_id=' +
                              encodeURIComponent(state.current) + '&limit=1');
    const task = (data.tasks || [])[0];
    if (!task) return;
    renderTask(task, task.terminal ? '历史任务（已结束）' : '继续跟踪');
    if (!task.terminal) watchTask(task.request_id);
  } catch (e) {
    /* 任务接口不可用不应影响主面板 */
  }
}

/** 点击「采集一次最新数据」：建任务 → 跟踪到终态 */
async function captureOnce() {
  if (!state.current) {
    renderStatus('⚠ 请先选择设备：下拉来自 /api/v1/devices，板端至少上传过一次才会出现', 'error');
    return;
  }
  $.captureBtn.disabled = true;
  try {
    const data = await apiPost('/api/v1/tasks', {
      device_id: state.current,
      sample_count: TASK.samples,
      sample_rate_hz: TASK.rateHz,
      timeout_s: TASK.timeoutS,
    });
    renderTask(data.task, data.duplicate ? '已复用未完成任务，未产生并行任务' : '任务已创建');
    watchTask(data.task.request_id);
    renderStatus('', 'hidden');
  } catch (e) {
    renderStatus('⚠ 创建采集任务失败：' + e.message, 'error');
  } finally {
    $.captureBtn.disabled = false;
  }
}

/** /api/v1/latest 现在带回 trigger / request_id，回填到数据卡片 */
function renderTrace(latest) {
  const up = (latest && latest.upload) || {};
  if ($.vTrigger) {
    $.vTrigger.textContent = up.trigger
      ? (up.trigger === 'manual' ? 'manual（按需采集）' : 'periodic（周期上报）')
      : '—';
  }
  if ($.vRequestId) {
    $.vRequestId.textContent = up.request_id || '—（周期批次不带任务号）';
  }
}

$.captureBtn.addEventListener('click', () => captureOnce());

// 切换设备后回填该设备最近的任务（等待 poll() 完成设备切换）
$.deviceSel.addEventListener('change', () => {
  stopTaskWatch();
  setTimeout(refreshLatestTask, 900);
});

// 设备列表首次加载后按钮才可用；这里先放开，点击时会校验是否已选设备
$.captureBtn.disabled = false;
refreshLatestTask();

// 便于自动化验证：用 /ui/?autocapture=1 打开页面即触发一次采集（等价于点按钮），
// 无需人工点击也能核对「建任务 → 板端领取 → 带 request_id 回传」这条链路。
try {
  const params = new URLSearchParams(window.location.search);
  if (params.get('autocapture') === '1') {
    setTimeout(captureOnce, 1200);
  }
} catch (e) {
  /* 不支持 URLSearchParams 的环境忽略即可 */
}

/* ------------------------------------------------------------------ *
 *  三维姿态视图：纯 Canvas 2D 手写正交投影（无第三方库 / 无 CDN）
 *
 *  屏幕约定：ax → 右，ay → 上，az → 朝向观察者；绕 Y 轴偏航、绕屏幕水平轴俯仰，
 *  正交投影（不做透视除法，读数稳定，CPU 开销小）。地面网格画在 y = -L 平面，
 *  当前向量在 XZ 平面上的投影用虚线落到网格上。
 * ------------------------------------------------------------------ */

const SCENE = {
  yaw: -0.65,       // 偏航角（弧度）
  pitch: 0.42,      // 俯仰角
  zoom: 1,
  auto: true,
  dragging: false,
  lastX: 0,
  lastY: 0,
};

const SCENE_AXIS_LEN = 12;        // 轴长（m/s²，覆盖 1 g ≈ 9.81）
const SCENE_G = 9.80665;          // 重力参考值
const SCENE_TRAJ_MAX = 600;       // 轨迹最多绘制点数（超出则抽样）
const SCENE_COLORS = {
  ax: '#cf222e', ay: '#1a7f37', az: '#2563eb',
  vector: '#111827', gravity: '#8b95a1', traj: 'rgba(37, 99, 235, .45)',
  grid: '#eef1f5', ground: '#c8ccd2',
};

/** 世界坐标 (x, y, z) → 屏幕坐标；depth 供将来做远近排序 */
function project3D(x, y, z, w, h, scale) {
  const cy = Math.cos(SCENE.yaw), sy = Math.sin(SCENE.yaw);
  const cp = Math.cos(SCENE.pitch), sp = Math.sin(SCENE.pitch);
  const x1 = x * cy - z * sy;      // 绕 Y 轴偏航
  const z1 = x * sy + z * cy;
  const y2 = y * cp - z1 * sp;     // 绕屏幕水平轴俯仰
  const z2 = y * sp + z1 * cp;
  const s = scale * SCENE.zoom;
  return { x: w / 2 + x1 * s, y: h / 2 - y2 * s, depth: z2 };
}

/** 带箭头的线段（箭头在屏幕空间计算，保证各视角下大小一致） */
function drawArrow(ctx, from, to, color, width, dashed) {
  const dx = to.x - from.x, dy = to.y - from.y;
  const len = Math.hypot(dx, dy);
  if (len < 2) return;
  const ux = dx / len, uy = dy / len;
  const head = Math.min(10, Math.max(5, len * 0.16));
  const bx = to.x - ux * head, by = to.y - uy * head;
  ctx.save();
  ctx.strokeStyle = color;
  ctx.fillStyle = color;
  ctx.lineWidth = width;
  if (dashed) ctx.setLineDash([5, 4]);
  ctx.beginPath();
  ctx.moveTo(from.x, from.y);
  ctx.lineTo(bx, by);
  ctx.stroke();
  ctx.setLineDash([]);
  ctx.beginPath();
  ctx.moveTo(to.x, to.y);
  ctx.lineTo(bx - uy * head * 0.45, by + ux * head * 0.45);
  ctx.lineTo(bx + uy * head * 0.45, by - ux * head * 0.45);
  ctx.closePath();
  ctx.fill();
  ctx.restore();
}

/** 地面网格（y = -L 平面）+ 网格原点十字 */
function drawGround3D(ctx, w, h, scale, L) {
  const steps = 4;
  ctx.save();
  ctx.lineWidth = 1;
  for (let i = 0; i <= steps; i++) {
    const t = -L + (2 * L * i) / steps;
    ctx.strokeStyle = (i * 2 === steps) ? SCENE_COLORS.ground : SCENE_COLORS.grid;
    for (const seg of [[t, -L, t, L], [-L, t, L, t]]) {
      const a = project3D(seg[0], -L, seg[1], w, h, scale);
      const b = project3D(seg[2], -L, seg[3], w, h, scale);
      ctx.beginPath();
      ctx.moveTo(a.x, a.y);
      ctx.lineTo(b.x, b.y);
      ctx.stroke();
    }
  }
  ctx.restore();
}

/** 三轴箭头 + 轴标签，返回屏幕原点 */
function drawAxes3D(ctx, w, h, scale, L) {
  const o = project3D(0, 0, 0, w, h, scale);
  const tips = {
    ax: project3D(L, 0, 0, w, h, scale),
    ay: project3D(0, L, 0, w, h, scale),
    az: project3D(0, 0, L, w, h, scale),
  };
  ctx.font = '12px "Segoe UI", "Microsoft YaHei", sans-serif';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  for (const key of ['ax', 'ay', 'az']) {
    drawArrow(ctx, o, tips[key], SCENE_COLORS[key], 1.6, false);
    ctx.fillStyle = SCENE_COLORS[key];
    ctx.fillText(key, tips[key].x, tips[key].y);
  }
  ctx.fillStyle = '#6b7280';
  ctx.font = '11px "Segoe UI", "Microsoft YaHei", sans-serif';
  ctx.textAlign = 'left';
  ctx.textBaseline = 'bottom';
  ctx.fillText('轴长 ' + L + ' m/s²', 10, h - 8);
  return o;
}

/** 轨迹折线（抽样到 SCENE_TRAJ_MAX 点；起点灰、终点蓝） */
function drawTrajectory3D(ctx, w, h, scale, points) {
  if (!points || points.length < 2) return;
  const step = Math.max(1, Math.floor(points.length / SCENE_TRAJ_MAX));
  ctx.save();
  ctx.strokeStyle = SCENE_COLORS.traj;
  ctx.lineWidth = 1.2;
  ctx.beginPath();
  let started = false;
  for (let i = 0; i < points.length; i += step) {
    const p = points[i];
    if (!isFinite(p.ax) || !isFinite(p.ay) || !isFinite(p.az)) {
      started = false;
      continue;
    }
    const s = project3D(p.ax, p.ay, p.az, w, h, scale);
    if (!started) {
      ctx.moveTo(s.x, s.y);
      started = true;
    } else {
      ctx.lineTo(s.x, s.y);
    }
  }
  ctx.stroke();

  const first = points[0];
  const last = points[points.length - 1];
  const a = project3D(first.ax, first.ay, first.az, w, h, scale);
  const b = project3D(last.ax, last.ay, last.az, w, h, scale);
  ctx.fillStyle = '#9aa4b2';
  ctx.beginPath();
  ctx.arc(a.x, a.y, 2.5, 0, Math.PI * 2);
  ctx.fill();
  ctx.fillStyle = SCENE_COLORS.az;
  ctx.beginPath();
  ctx.arc(b.x, b.y, 3, 0, Math.PI * 2);
  ctx.fill();
  ctx.restore();
}

function sceneEmptyText(ctx, w, h, text) {
  ctx.fillStyle = '#6b7280';
  ctx.font = '13px "Segoe UI", "Microsoft YaHei", sans-serif';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.fillText(text, w / 2, h / 2);
}

/**
 * 主绘制：地面网格 + 三轴 + 轨迹 + 当前向量（含分量/投影虚线）+ 重力参考。
 * points: [{t, ax, ay, az}, …]，按 t 升序（即 /api/v1/window 的返回值）
 */
function drawScene3D(points) {
  const cv = $.scene3d;
  if (!cv || !cv.getContext) return;
  const ctx = cv.getContext('2d');
  const { w, h } = fitCanvas(cv, ctx);
  ctx.clearRect(0, 0, w, h);

  const L = SCENE_AXIS_LEN;
  const scale = Math.min(w, h) / (L * 3.6);     // 让 ±L 落在画布中部

  drawGround3D(ctx, w, h, scale, L);
  const o = drawAxes3D(ctx, w, h, scale, L);
  drawTrajectory3D(ctx, w, h, scale, points);

  const last = (points && points.length) ? points[points.length - 1] : null;
  if (!last || !isFinite(last.ax) || !isFinite(last.ay) || !isFinite(last.az)) {
    sceneEmptyText(ctx, w, h, '等待数据……（板端上传后此处实时刷新）');
    updateSceneInfo(null, points);
    return;
  }

  const v = project3D(last.ax, last.ay, last.az, w, h, scale);
  const comps = [
    [project3D(last.ax, 0, 0, w, h, scale), SCENE_COLORS.ax],
    [project3D(0, last.ay, 0, w, h, scale), SCENE_COLORS.ay],
    [project3D(0, 0, last.az, w, h, scale), SCENE_COLORS.az],
  ];
  const shadow = project3D(last.ax, -L, last.az, w, h, scale);

  ctx.save();                     // 分量 + 地面投影（虚线，画在下层）
  ctx.setLineDash([4, 4]);
  ctx.lineWidth = 1.2;
  for (const [tip, color] of comps) {
    ctx.strokeStyle = color;
    ctx.beginPath();
    ctx.moveTo(o.x, o.y);
    ctx.lineTo(tip.x, tip.y);
    ctx.stroke();
  }
  ctx.strokeStyle = SCENE_COLORS.ground;
  ctx.beginPath();
  ctx.moveTo(v.x, v.y);
  ctx.lineTo(shadow.x, shadow.y);
  ctx.stroke();
  ctx.restore();

  const gz = (last.az >= 0 ? 1 : -1) * SCENE_G;   // 重力参考：与 az 同向取 +g
  const g = project3D(0, 0, gz, w, h, scale);
  drawArrow(ctx, o, g, SCENE_COLORS.gravity, 1.4, true);
  ctx.fillStyle = '#6b7280';
  ctx.font = '11px "Segoe UI", "Microsoft YaHei", sans-serif';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'bottom';
  ctx.fillText('g', g.x, g.y - 4);

  drawArrow(ctx, o, v, SCENE_COLORS.vector, 2.6, false);   // 当前向量（最上层）
  ctx.fillStyle = SCENE_COLORS.vector;
  ctx.beginPath();
  ctx.arc(v.x, v.y, 3.5, 0, Math.PI * 2);
  ctx.fill();

  drawSceneLegend(ctx, last);
  updateSceneInfo(last, points);
}

function drawSceneLegend(ctx, p) {
  const mag = Math.sqrt(p.ax * p.ax + p.ay * p.ay + p.az * p.az);
  const deg = (r) => (r * 180 / Math.PI).toFixed(0);
  const lines = [
    'a = (' + p.ax.toFixed(2) + ', ' + p.ay.toFixed(2) + ', ' + p.az.toFixed(2) + ') m/s²',
    '|a| = ' + mag.toFixed(2) + ' m/s²    g = ' + SCENE_G.toFixed(2),
    '偏航 ' + deg(SCENE.yaw) + '° · 俯仰 ' + deg(SCENE.pitch) +
      '° · 缩放 ' + SCENE.zoom.toFixed(2) + '×',
  ];
  ctx.save();
  ctx.font = '12px "Segoe UI", "Microsoft YaHei", sans-serif';
  ctx.textAlign = 'left';
  ctx.textBaseline = 'top';
  ctx.fillStyle = '#1f2933';
  lines.forEach((t, i) => ctx.fillText(t, 10, 10 + i * 16));
  ctx.restore();
}

function updateSceneInfo(last, points) {
  if (!$.sceneInfo) return;
  if (!last) {
    $.sceneInfo.textContent = '暂无数据';
    return;
  }
  const mag = Math.sqrt(last.ax * last.ax + last.ay * last.ay + last.az * last.az);
  $.sceneInfo.textContent = '|a| = ' + mag.toFixed(2) + ' m/s² · 轨迹 ' +
                            ((points || []).length) + ' 点';
}

/* ---- 交互：拖拽旋转 / 滚轮缩放 / 双击或按钮复位 / 自动旋转 ---- */

function resetScene() {
  SCENE.yaw = -0.65;
  SCENE.pitch = 0.42;
  SCENE.zoom = 1;
  drawScene3D(state.lastPoints || []);
}

let sceneLastFrame = 0;

/** 自动旋转：rAF 驱动并限到约 30 fps，避免无谓重绘 */
function sceneRotateLoop(ts) {
  const now = (typeof ts === 'number') ? ts : performance.now();
  if (SCENE.auto && !SCENE.dragging && (now - sceneLastFrame) > 33) {
    sceneLastFrame = now;
    SCENE.yaw += 0.006;
    drawScene3D(state.lastPoints || []);
  }
  requestAnimationFrame(sceneRotateLoop);
}

function sceneInit() {
  const cv = $.scene3d;
  if (!cv) return;
  if ($.sceneWin) $.sceneWin.textContent = String(CHART_WINDOW_S);
  if ($.autoRotate) SCENE.auto = $.autoRotate.checked;

  const onDown = (x, y) => {
    SCENE.dragging = true;
    SCENE.lastX = x;
    SCENE.lastY = y;
  };
  const onMove = (x, y) => {
    if (!SCENE.dragging) return;
    const dx = x - SCENE.lastX;
    const dy = y - SCENE.lastY;
    SCENE.lastX = x;
    SCENE.lastY = y;
    SCENE.yaw += dx * 0.01;
    SCENE.pitch = Math.max(-1.45, Math.min(1.45, SCENE.pitch + dy * 0.01));
    drawScene3D(state.lastPoints || []);
  };
  const onUp = () => { SCENE.dragging = false; };

  cv.addEventListener('mousedown', (ev) => onDown(ev.clientX, ev.clientY));
  window.addEventListener('mousemove', (ev) => onMove(ev.clientX, ev.clientY));
  window.addEventListener('mouseup', onUp);

  cv.addEventListener('touchstart', (ev) => {
    const t = ev.touches[0];
    if (t) onDown(t.clientX, t.clientY);
  }, { passive: true });
  cv.addEventListener('touchmove', (ev) => {
    const t = ev.touches[0];
    if (!t) return;
    ev.preventDefault();
    onMove(t.clientX, t.clientY);
  }, { passive: false });
  cv.addEventListener('touchend', onUp);

  cv.addEventListener('wheel', (ev) => {
    ev.preventDefault();
    const factor = ev.deltaY > 0 ? 0.92 : 1.08;
    SCENE.zoom = Math.max(0.4, Math.min(3, SCENE.zoom * factor));
    drawScene3D(state.lastPoints || []);
  }, { passive: false });

  cv.addEventListener('dblclick', () => resetScene());
  if ($.resetView) $.resetView.addEventListener('click', () => resetScene());
  if ($.autoRotate) {
    $.autoRotate.addEventListener('change', () => { SCENE.auto = $.autoRotate.checked; });
  }
  // 窗口尺寸变化时按新尺寸重绘（正交投影，只需重算 scale）
  window.addEventListener('resize', () => drawScene3D(state.lastPoints || []));

  requestAnimationFrame(sceneRotateLoop);
  drawScene3D(state.lastPoints || []);
}

sceneInit();


