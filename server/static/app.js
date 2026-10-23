/*
 * ESP32-S3 IMU 实时监控面板
 *
 * 定时轮询查询接口：
 *   GET /api/v1/devices                    -> 设备下拉
 *   GET /api/v1/latest?device_id=..        -> 选中设备的最新数据
 *   GET /api/v1/latest?device_id=..&trigger=manual|periodic  -> 对照区各取一类批次
 *   GET /api/v1/window?device_id=..&seconds=30               -> 波形与三维轨迹
 *   GET /api/v1/tasks?device_id=..&limit=20                  -> 任务历史
 *   GET /api/v1/control?device_id=..                         -> 周期上报暂停状态
 *   GET /api/v1/photos?device_id=..&limit=12                 -> 照片画廊（实时拍照）
 *   GET /api/v1/photos/{id}                                  -> 照片 JPEG 原图
 *   GET /api/v1/live?device_id=..                            -> 直播状态（板端长按 Button A 推流）
 *   GET /api/v1/live/frame?device_id=..&seq=..               -> 直播最新一帧 JPEG（内存态）
 * 能力：设备下拉选择、最新数据展示、无数据/无设备提示、更新状态（更新中/未更新）、
 *      按需采集任务（参数表单 → 创建 → 跟踪 → 时间戳/upload_id 回显 → 无回执提示）、
 *      实时拍照（拍一张照片 → kind=camera 任务 → 板端单帧 JPEG 上传 → 画廊缩略图/
 *      原图/逐张删除）、
 *      摄像头实时画面（板端长按 Button A 推流 → POST /api/v1/live → 本页独立 250 ms
 *      节拍按 seq 轮询最新一帧 <img>，约 2-4 fps；超时无新帧自动显示「未推流」）、
 *      周期上报控制（暂停 N 秒 / 提前恢复 → kind=pause|resume 任务 → 板端 applied 生效）、
 *      手动批次与周期批次对照、三维姿态视图。
 */

'use strict';

const POLL_MS = 800;           // 轮询周期（原 2000，压到 800ms 提升实时感）
const STALE_S = 30;            // 超过该秒数视为「未更新」（与服务端 / 保持一致）
const NUM = 5;                 // 展示头部/尾部样本条数
const CHART_WINDOW_S = 30;     // 波形窗口秒数（与 /api/v1/window?seconds= 一致）
const CHART_AXES = ['ax', 'ay', 'az'];
const CHART_COLORS = { ax: '#cf222e', ay: '#1a7f37', az: '#2563eb' };
/* 照片画廊：一次拉最近 N 张（对应 GET /api/v1/photos?limit=），点删除走 DELETE */
const PHOTO_LIMIT = 12;
/* 摄像头实时画面（直播）：板端长按 Button A 推流，服务端只在内存里保留最新一帧。
   本页用**独立节拍**（liveTickLoop）轮询 GET /api/v1/live：新帧一到就尽快换
   <img src>，不再被 800 ms 的主轮询拖带（板端约 250 ms/帧，见 main.c
   LIVE_FRAME_INTERVAL_MS，摄像头移动时这一条决定了画面跟手程度）。
   LIVE_POLL_MIN_MS 是两次取图的最小间隔，防止图片请求堆积。 */
const LIVE_POLL_MIN_MS = 250;
const LIVE_TICK_MS = 250;       // 推流中：独立节拍周期（与板端帧间隔对齐）
const LIVE_IDLE_TICK_MS = 1000; // 未推流：退到 1 s，避免空转时白白高频打 /api/v1/live

/* 闭环事件（板端按键触发）：这些常量必须和上面的常量放在一起。
   注意 poll() 在本文件靠前的位置（约 570 行）就被立刻调用了一次，而事件相关的
   函数体在后面——const 有暂时性死区，如果把这些常量留在后面的小节里，
   首轮 poll() 里的 refreshEvents() 会直接抛 "Cannot access before initialization"。 */
const EVENT_LIMIT = 8;
const EVENT_ACTION_CN = { accept: '回应', cancel: '取消', confirm: '确认完成' };
const EVENT_BY_CN = { device: '板端按键', web: '网页' };

const el = (id) => document.getElementById(id);
const $ = {
  deviceSel: el('deviceSel'),
  refreshBtn: el('refreshBtn'),
  captureBtn: el('captureBtn'),
  pauseInput: el('pauseInput'),
  pauseBtn: el('pauseBtn'),
  resumeBtn: el('resumeBtn'),
  sourceSel: el('sourceSel'),
  countInput: el('countInput'),
  rateInput: el('rateInput'),
  timeoutInput: el('timeoutInput'),
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
  taskDispatched: el('taskDispatched'),
  taskAcked: el('taskAcked'),
  taskCompleted: el('taskCompleted'),
  taskUploadId: el('taskUploadId'),
  taskAckHint: el('taskAckHint'),
  taskHistoryBody: el('taskHistoryBody'),
  taskHistoryInfo: el('taskHistoryInfo'),
  ctrlBadge: el('ctrlBadge'),
  ctrlState: el('ctrlState'),
  ctrlRemaining: el('ctrlRemaining'),
  ctrlUntil: el('ctrlUntil'),
  ctrlRid: el('ctrlRid'),
  ctrlUpdated: el('ctrlUpdated'),
  cmpBody: el('cmpBody'),
  cmpInfo: el('cmpInfo'),
  scene3d: el('scene3d'),
  sceneInfo: el('sceneInfo'),
  sceneWin: el('sceneWin'),
  autoRotate: el('autoRotate'),
  resetView: el('resetView'),
  photoBtn: el('photoBtn'),
  previewBtn: el('previewBtn'),
  photoBadge: el('photoBadge'),
  photoInfo: el('photoInfo'),
  photoStatus: el('photoStatus'),
  photoGrid: el('photoGrid'),
  liveBadge: el('liveBadge'),
  liveInfo: el('liveInfo'),
  liveMeta: el('liveMeta'),
  liveImg: el('liveImg'),
  liveOff: el('liveOff'),
  liveTimeoutS: el('liveTimeoutS'),
  eventBadge: el('eventBadge'),
  eventRid: el('eventRid'),
  eventStatus: el('eventStatus'),
  eventSource: el('eventSource'),
  eventDeviceTs: el('eventDeviceTs'),
  eventCreated: el('eventCreated'),
  eventResponded: el('eventResponded'),
  eventTtl: el('eventTtl'),
  eventAcceptBtn: el('eventAcceptBtn'),
  eventCancelBtn: el('eventCancelBtn'),
  eventConfirmBtn: el('eventConfirmBtn'),
  eventStatusLine: el('eventStatusLine'),
  eventHistoryBody: el('eventHistoryBody'),
  histTrigger: el('histTrigger'),
  histLimit: el('histLimit'),
  histPrevBtn: el('histPrevBtn'),
  histNextBtn: el('histNextBtn'),
  histRefreshBtn: el('histRefreshBtn'),
  histInfo: el('histInfo'),
  histBadge: el('histBadge'),
  histBody: el('histBody'),
  histDetailCard: el('histDetailCard'),
  histDetailTitle: el('histDetailTitle'),
  histDetailGrid: el('histDetailGrid'),
  histCsvBtn: el('histCsvBtn'),
  histCloseBtn: el('histCloseBtn'),
  histChart: el('histChart'),
  histChartUnit: el('histChartUnit'),
  histChartCount: el('histChartCount'),
  histSamplePrevBtn: el('histSamplePrevBtn'),
  histSampleNextBtn: el('histSampleNextBtn'),
  histSampleInfo: el('histSampleInfo'),
  histSampleBody: el('histSampleBody'),
  voiceBadge: el('voiceBadge'),
  voiceStatus: el('voiceStatus'),
  voiceThread: el('voiceThread'),
  voiceInput: el('voiceInput'),
  voiceRecordBtn: el('voiceRecordBtn'),
  voiceSendBtn: el('voiceSendBtn'),
  voiceStopBtn: el('voiceStopBtn'),
  voiceClearBtn: el('voiceClearBtn'),
  voiceOptions: el('voiceOptions'),
  voiceStatusLine: el('voiceStatusLine'),
  voiceStage: el('voiceStage'),
  voiceRequestId: el('voiceRequestId'),
  voiceTaskRid: el('voiceTaskRid'),
  voiceLatency: el('voiceLatency'),
  voiceRecognizer: el('voiceRecognizer'),
  voiceSpeechState: el('voiceSpeechState'),
  voiceAutoSpeak: el('voiceAutoSpeak'),
  voiceSpeakStopBtn: el('voiceSpeakStopBtn'),
  voiceSupport: el('voiceSupport'),
  voiceLogInfo: el('voiceLogInfo'),
  voiceLogBody: el('voiceLogBody'),
};

const state = { busy: false, devices: [], current: '', lastPoints: [], unit: '',
                eventRid: '' };

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

/** 容错 DELETE：网络/非 2xx 都抛出带可读信息的 Error（优先用服务端 error 字段） */
async function apiDelete(path) {
  let res;
  try {
    res = await fetch(path, { method: 'DELETE', cache: 'no-store' });
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
  return data || { ok: true };
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
 * X 轴为 [tEnd - spanS, tEnd] 的窗口，tEnd = 最后一个点的时间。
 *
 * opts（可选，全给默认值就是原来的「最近 30 s 实时波形」）：
 *   canvas   目标画布（默认 #chart）
 *   countEl  右上角「N 点」元素（默认 #chartCount）
 *   spanS    窗口秒数（默认 CHART_WINDOW_S = 30）
 *   tickS    X 轴刻度间隔秒数（默认 5）
 *   xLabel   (秒偏移) => 刻度文案，默认 "now" / "-5s" 这种
 * 「历史记录」里看单批数据时复用本函数（独立画布 + 按该批时长铺满 X 轴）。
 */
function drawChart(points, opts) {
  opts = opts || {};
  const cv = opts.canvas || $.chart;
  if (!cv || !cv.getContext) return;
  const countEl = (opts.countEl !== undefined) ? opts.countEl : $.chartCount;
  const spanS = opts.spanS || CHART_WINDOW_S;
  const tickS = opts.tickS || 5;
  const xLabel = opts.xLabel || ((s) => (s === 0 ? 'now' : '-' + s + 's'));
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

  if (countEl) {
    countEl.textContent = (points ? points.length : 0) + ' 点';
  }

  if (!points || points.length === 0) {
    drawChartEmpty(ctx, w, h, box, '等待数据…（板端开始采集后自动出现波形）');
    return;
  }

  const spanMs = spanS * 1000;
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

  // 纵向网格 + X 刻度（默认每 5 s 一格，最右为 now；单批视图按该批时长给刻度）
  ctx.textAlign = 'center';
  ctx.textBaseline = 'top';
  for (let s = 0; s <= spanS + 1e-9; s += tickS) {
    const x = xOf(tEnd - s * 1000);
    ctx.strokeStyle = '#eef1f5';
    ctx.beginPath();
    ctx.moveTo(x + 0.5, box.y);
    ctx.lineTo(x + 0.5, box.y + box.h);
    ctx.stroke();
    ctx.fillStyle = '#6b7280';
    ctx.fillText(xLabel(s), x, box.y + box.h + 6);
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

    // 3') 摄像头实时画面：主轮询里也顺手试一次（内部有 LIVE_POLL_MIN_MS 最小
    //     间隔 + busy 保护，与下面的独立定时器重复调用无副作用，还顺带覆盖了
    //     切设备 / 点「刷新」后的即时反应）。真正的高频取图由文件末尾的
    //     setInterval(refreshLive, LIVE_TICK_MS) 负责。不 await —— 图片/状态请求
    //     再慢也不该拖住 800 ms 的主轮询和数据面板。
    refreshLive();

    // 3'') 闭环事件：板端按键触发的事件要"立刻"在页面上看到，
    //      所以放在主轮询里（不 await 会漏掉首轮的卡片渲染，这里 await 一次
    //      轻量 JSON；侧栏/历史仍走节流，避免每 800 ms 重画表格）。
    await refreshEvents(false);

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

    // 侧栏（对照区 / 任务历史）按节流刷新：首轮必刷，之后每 SIDE_REFRESH_EVERY 轮一次。
    // 不 await：侧栏慢或失败都不该拖住 800 ms 的主轮询。
    pollTicks++;
    if (pollTicks === 1 || pollTicks % SIDE_REFRESH_EVERY === 0) refreshSidePanels();

    // 「历史记录」分区可见时顺手跟一次列表（新批次自动出现）；不可见就不打这个接口。
    // activeTab 定义在本文件末尾，但这行在 await 之后才执行，脚本求值早已结束，不会命中 TDZ。
    if (activeTab === 'history' && pollTicks % SIDE_REFRESH_EVERY === 0) refreshHistory();
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
  if ($.pauseBtn) $.pauseBtn.disabled = empty;
  if ($.resumeBtn) $.resumeBtn.disabled = empty;
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

// 直播画面独立节拍：与 800 ms 主轮询解耦 —— 摄像头移动时，板端一有新帧
// （seq 变化）就能在 ≤ LIVE_TICK_MS 内换 <img src>，不用干等下一轮主轮询。
// 推流中按 LIVE_TICK_MS(250 ms) 跑；没推流时退到 LIVE_IDLE_TICK_MS(1 s)，
// 避免空闲时白白 4 次/秒打 /api/v1/live。refreshLive 内部另有 busy + 最小间隔
// 双重保护，不会重入或堆积请求。
// 注意：这里只用 setTimeout 排一拍、不在脚本求值阶段读 LIVE —— const LIVE 在
// 本文件靠后的直播小节里定义，同步读会命中 TDZ（与文件顶部那段警告同一个坑）。
function liveTickLoop() {
  Promise.resolve(refreshLive()).finally(() => {
    setTimeout(liveTickLoop, LIVE.active ? LIVE_TICK_MS : LIVE_IDLE_TICK_MS);
  });
}
setTimeout(liveTickLoop, LIVE_TICK_MS);

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

/* 参数表单的可调范围：取「服务端校验」与「板端能力」的交集，避免页面造出必然失败的任务。
 *  服务端 validate_task_request：rate 10-200、count 1-2000、timeout 5-600
 *  板端 main.c：sample_count ≤ TASK_MAX_SAMPLES(600)、rate ≤ TARGET_SAMPLE_FREQ_HZ(100)
 * source 必须是板端上报的 qma6100p、unit 必须是 m/s^2，否则 _check_task_match 判失败，
 * 因此这两项在页面上固定、不开放编辑。 */
const TASK_LIMITS = {
  source: 'qma6100p',
  countMin: 1, countMax: 600,
  rateMin: 10, rateMax: 100,
  timeoutMin: 5, timeoutMax: 600,
  pauseMin: 5, pauseMax: 600, pauseDefault: 120,   // 与服务端 PAUSE_MIN_S/MAX_S 对齐
};

/* 任务类型：capture/camera 由带 request_id 的上传收尾；pause/resume 靠板端 /applied 收尾 */
const TASK_KIND = {
  capture: { text: '按需采集', short: 'capture', spec: (t) => (t.sample_count ?? '—') + ' 点 @ ' + (t.sample_rate_hz ?? '—') + ' Hz' },
  camera: { text: '实时拍照', short: 'camera', spec: () => '拍一帧 640×480 JPEG 并上传（不采样本）' },
  preview: { text: '本地预览', short: 'preview', spec: () => '切换板端 240×240 LCD 相机预览（开/关）' },
  pause: { text: '暂停周期', short: 'pause', spec: (t) => '暂停 ' + (t.duration_s ?? TASK_LIMITS.pauseDefault) + ' s（到期自动恢复）' },
  resume: { text: '恢复周期', short: 'resume', spec: () => '立即恢复周期上报' },
};

function taskKindMeta(kind) {
  return TASK_KIND[kind] || TASK_KIND.capture;
}

/* 已下发超过该秒数仍无 acked_at → 页面给出「回执未收到」提示（纯前端推断，服务端状态不变） */
const NO_ACK_WARN_S = 5;

/* 侧栏（对照区 / 任务历史）刷新节流：主轮询每 800 ms 一次，侧栏每 5 次刷新一次 */
const SIDE_REFRESH_EVERY = 5;
let pollTicks = 0;

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

function renderTask(task, note, upload, photo) {
  if (!task) return;
  const meta = renderTaskBadge(task.status);
  taskState.last = task;
  taskState.rid = task.request_id || taskState.rid;

  if ($.taskRid) $.taskRid.textContent = task.request_id || '—';
  if ($.taskSpec) {
    // 控制任务（pause/resume）没有采样点数，按类型渲染各自的说明
    $.taskSpec.textContent = taskKindMeta(task.kind).spec(task);
  }
  if ($.taskStatus) {
    let text = meta.text + '（状态码 ' + task.status + '）· ' + meta.hint;
    if (task.error) text += ' · error: ' + task.error;
    if (note) text += ' · ' + note;
    $.taskStatus.textContent = text;
  }
  // 状态机时间戳：服务端返回 *_at_str，未到达的阶段显示占位文案
  if ($.taskDispatched) {
    $.taskDispatched.textContent = task.dispatched_at_str || '—（尚未下发）';
  }
  if ($.taskAcked) {
    $.taskAcked.textContent = task.acked_at_str || '—（尚未回执）';
  }
  if ($.taskCompleted) {
    $.taskCompleted.textContent = task.completed_at_str || '—（未结束）';
  }
  // 关联数据：任务详情接口带回 uploads 摘要（upload），列表接口只有 upload_id
  const up = upload || task.upload || null;
  const kindMeta = taskKindMeta(task.kind);
  if ($.taskUploadId) {
    if (task.kind === 'camera') {
      // 拍照任务不产生样本批次：任务详情接口带回关联照片（photo），直接显示照片信息
      const ph = photo || task.photo || null;
      $.taskUploadId.textContent = ph
        ? ('照片 #' + ph.id + '（' + (ph.size_kb ?? '—') + ' KB · ' +
           (ph.width ?? '?') + '×' + (ph.height ?? '?') + ' · ' +
           (ph.received_at_str || '—') + '）')
        : '—（尚无照片：板端拍好并 POST /api/v1/photos 后才有关联照片）';
    } else if (up && up.id) {
      $.taskUploadId.textContent = up.id + '（' + (up.trigger || 'manual') + ' · ' +
        (up.sample_count ?? '—') + ' 点 · ' + (up.received_at_str || '—') + '）';
    } else if (task.upload_id) {
      $.taskUploadId.textContent = String(task.upload_id);
    } else if (task.kind && task.kind !== 'capture') {
      // 暂停/恢复不产生观测数据，靠板端 /applied 收尾，永远没有关联批次
      $.taskUploadId.textContent = '—（' + kindMeta.text + '任务不产生批次，靠板端 /applied 收尾）';
    } else {
      $.taskUploadId.textContent = '—（任务未完成，暂无关联批次）';
    }
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
  renderNoAckHint(task);
}

/** 「回执未收到」提示：板端 ack 失败只在设备日志里可见，服务端会一直停在 dispatched */
function renderNoAckHint(task) {
  if (!$.taskAckHint) return;
  const dispatched = Number(task && task.dispatched_at);
  const waiting = !!(task && task.status === 'dispatched' &&
                     isFinite(dispatched) && dispatched > 0);
  if (!waiting) {
    $.taskAckHint.classList.add('hidden');
    $.taskAckHint.textContent = '';
    return;
  }
  const waited = Math.max(0, Date.now() / 1000 - dispatched);
  $.taskAckHint.classList.remove('hidden');
  $.taskAckHint.textContent = waited >= NO_ACK_WARN_S
    ? '⚠ 已下发 ' + waited.toFixed(0) + ' s 仍未收到设备回执（acked_at）：板端回执失败只在设备'
      + '串口日志里可见，服务端状态会保持 dispatched 直到有效期结束。请查板端 [task] 日志；'
      + '任务仍可能在有效期内靠数据回传（带 request_id 的上报）直接完成。'
    : '已下发 ' + waited.toFixed(1) + ' s，等待设备回执（超过 ' + NO_ACK_WARN_S + ' s 会提示）。';
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
      renderTask(data.task, undefined, data.upload, data.photo);
      if (data.task && data.task.terminal) {
        stopTaskWatch();
        // 控制任务（pause/resume）的终态就是 device_control 已被写入的时刻，立刻回读
        refreshControl();
        // 拍照任务的终态 = 照片已入库，立刻刷新画廊（不用等下一次侧栏刷新）
        if (data.task.kind === 'camera') refreshGallery();
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

/** 打开页面/切换设备时回填该设备最近的任务与任务历史（列表按 created_at 倒序） */
async function refreshLatestTask() {
  if (!state.current) return;
  try {
    const data = await apiGet('/api/v1/tasks?device_id=' +
                              encodeURIComponent(state.current) + '&limit=20');
    const tasks = data.tasks || [];
    renderTaskHistory(tasks);
    const task = tasks[0];
    if (!task) return;
    renderTask(task, task.terminal ? '历史任务（已结束）' : '继续跟踪');
    if (!task.terminal) {
      watchTask(task.request_id);
    } else if (task.kind === 'camera') {
      // 历史拍照任务：关联照片只在任务详情接口里（列表接口只有 upload_id），
      // 补一次详情请求，任务卡片才能显示「照片 #id（尺寸/大小/时间）」。
      try {
        const detail = await apiGet('/api/v1/tasks/' + encodeURIComponent(task.request_id));
        renderTask(detail.task, '历史任务（已结束）', detail.upload, detail.photo);
      } catch (e) {
        /* 详情接口不可用：保留列表渲染结果（不显示照片信息，不影响主面板） */
      }
    }
  } catch (e) {
    /* 任务接口不可用不应影响主面板 */
  }
}

/** request_id 是 32 位十六进制，表格里只显示头尾（完整值放在 title 属性里） */
function shortRid(rid) {
  if (!rid) return '—';
  return rid.length > 10 ? rid.slice(0, 6) + '…' + rid.slice(-4) : rid;
}

/** 任务耗时：created_at → completed_at（未结束则显示已过时间） */
function taskDuration(t) {
  const start = Number(t && t.created_at);
  if (!isFinite(start) || start <= 0) return '—';
  const end = Number(t.completed_at) || (Date.now() / 1000);
  return (t.completed_at ? '' : '进行中 ') + Math.max(0, end - start).toFixed(1) + ' s';
}

/** 任务历史表：最近 20 条任务（类型/状态/参数/关联批次 upload_id/耗时） */
function renderTaskHistory(tasks) {
  const list = tasks || [];
  if ($.taskHistoryInfo) {
    $.taskHistoryInfo.textContent = list.length
      ? '· ' + (state.current || '') + ' · 已完成 ' +
        list.filter((t) => t.status === 'completed').length + ' 条'
      : '· 暂无任务记录';
  }
  const body = $.taskHistoryBody;
  if (!body) return;
  if (!list.length) {
    body.innerHTML = '<tr><td colspan="7">—（本设备还没有任务）</td></tr>';
    return;
  }
  const frag = document.createDocumentFragment();
  for (const t of list) {
    const meta = TASK_STATUS_META[t.status] || { cls: 'off', text: t.status };
    const kind = taskKindMeta(t.kind);
    const tr = document.createElement('tr');
    tr.innerHTML =
      '<td>' + (t.created_at_str || '—') + '</td>' +
      '<td>' + kind.text + '</td>' +
      '<td title="' + (t.request_id || '') + '">' + shortRid(t.request_id) + '</td>' +
      '<td>' + taskSpecShort(t) + '</td>' +
      '<td>' + meta.text + '（' + (t.status || '—') + '）</td>' +
      '<td>' + (t.upload_id ?? '—') + '</td>' +
      '<td>' + taskDuration(t) + '</td>';
    frag.appendChild(tr);
  }
  body.innerHTML = '';
  body.appendChild(frag);
}

/** 历史表里的参数列：采集任务显示「点数 @ Hz」，控制任务显示自己的语义（板端不采数据） */
function taskSpecShort(t) {
  const kind = taskKindMeta(t.kind);
  if ((t.kind || 'capture') === 'capture') {
    return (t.sample_count ?? '—') + ' @ ' + (t.sample_rate_hz ?? '—');
  }
  if (t.kind === 'pause') {
    return '暂停 ' + (t.duration_s ?? TASK_LIMITS.pauseDefault) + ' s';
  }
  return kind.spec(t);
}

/** 对照区：同设备「最近一批 manual」vs「最近一批 periodic」（后端只多了一个 trigger 过滤） */
async function renderCompare() {
  const body = $.cmpBody;
  if (!body || !state.current) return;
  const base = '/api/v1/latest?device_id=' + encodeURIComponent(state.current);
  let manual = null, periodic = null;
  try {
    const [m, p] = await Promise.all([
      apiGet(base + '&trigger=manual'),
      apiGet(base + '&trigger=periodic'),
    ]);
    manual = (m && m.found) ? m.upload : null;
    periodic = (p && p.found) ? p.upload : null;
  } catch (e) {
    body.innerHTML = '<tr><td colspan="6">—（对照数据查询失败：' + e.message + '）</td></tr>';
    return;
  }
  const row = (label, up) => {
    if (!up) {
      return '<tr><td>' + label + '</td><td colspan="5">—（该设备还没有这类批次）</td></tr>';
    }
    return '<tr><td>' + label + '</td>' +
      '<td>' + (up.id ?? '—') + '</td>' +
      '<td>' + (up.sample_count ?? '—') + '</td>' +
      '<td>' + fmtTsMs(up.ts_ms) + '　(' + (up.ts_ms ?? '—') + ')</td>' +
      '<td>' + (up.received_at_str || '—') + '</td>' +
      '<td>' + shortRid(up.request_id) + '</td></tr>';
  };
  body.innerHTML = row('manual（按需采集）', manual) + row('periodic（周期上报）', periodic);
  if ($.cmpInfo) {
    $.cmpInfo.textContent = manual && periodic
      ? '· 两类批次各有记录 · ' + (state.current || '')
      : '· 缺一类批次（板端可能在暂停周期上报或尚未手动采集）';
  }
}

/** 侧栏刷新：对照区 + 任务历史 + 周期上报控制 + 照片画廊
 *  （切设备 / 建任务 / 终态 / 主轮询节流都会调用） */
function refreshSidePanels() {
  renderCompare();
  refreshLatestTask();
  refreshControl();
  refreshGallery();
  refreshEvents(true);    // 闭环事件：详细卡片每轮已刷新，这里补齐流水表
}

/** URL 参数（?source=&samples=&rate=&timeout=&pause=）覆盖表单初值，便于自动化构造任务参数 */
function syncFormFromUrl() {
  let params;
  try {
    params = new URLSearchParams(window.location.search);
  } catch (e) {
    return;      // 不支持 URLSearchParams 的环境忽略
  }
  // 属性与属性值一起写：属性值让无头浏览器 dump-dom 也能看到实际生效的参数
  const set = (elem, key) => {
    const v = params.get(key);
    if (!elem || !v) return;
    elem.value = v;
    elem.setAttribute('value', v);
  };
  set($.sourceSel, 'source');
  set($.countInput, 'samples');
  set($.rateInput, 'rate');
  set($.timeoutInput, 'timeout');
  set($.pauseInput, 'pause');
}

/** 读取参数表单；越界/非整数抛错（不发请求，错误信息直接显示在状态条） */
function readTaskParams() {
  const num = (label, raw, low, high) => {
    const n = Number(raw);
    if (!Number.isInteger(n) || n < low || n > high) {
      throw new Error(label + ' 必须是 ' + low + '-' + high +
                      ' 的整数（当前 ' + raw + '）');
    }
    return n;
  };
  const source = String(($.sourceSel && $.sourceSel.value) || TASK_LIMITS.source).trim();
  return {
    source: source || TASK_LIMITS.source,
    sample_count: num('样本数', ($.countInput && $.countInput.value) || TASK.samples,
                      TASK_LIMITS.countMin, TASK_LIMITS.countMax),
    sample_rate_hz: num('采样率 Hz', ($.rateInput && $.rateInput.value) || TASK.rateHz,
                        TASK_LIMITS.rateMin, TASK_LIMITS.rateMax),
    timeout_s: num('有效期 s', ($.timeoutInput && $.timeoutInput.value) || TASK.timeoutS,
                   TASK_LIMITS.timeoutMin, TASK_LIMITS.timeoutMax),
  };
}

/** 点击「采集一次最新数据」：按表单参数建任务 → 跟踪到终态 */
async function captureOnce() {
  return createTask('capture');
}

/** 点击「暂停周期」：建 kind=pause 任务（带 duration_s），板端 applied 后即生效 */
async function pausePeriodic() {
  return createTask('pause');
}

/** 点击「恢复周期」：建 kind=resume 任务，提前结束暂停窗口 */
async function resumePeriodic() {
  return createTask('resume');
}

/** 读取「暂停时长」输入（服务端允许 5-600 s 整数，默认 120） */
function readPauseDuration() {
  const raw = ($.pauseInput && $.pauseInput.value) || TASK_LIMITS.pauseDefault;
  const n = Number(raw);
  if (!Number.isInteger(n) || n < TASK_LIMITS.pauseMin || n > TASK_LIMITS.pauseMax) {
    throw new Error('暂停时长必须是 ' + TASK_LIMITS.pauseMin + '-' +
                    TASK_LIMITS.pauseMax + ' 的整数（当前 ' + raw + '）');
  }
  return n;
}

/** 建一条任务（capture / pause / resume 共用同一条链路，只有 kind 与收尾方式不同） */
async function createTask(kind, extra) {
  if (!state.current) {
    renderStatus('⚠ 请先选择设备：下拉来自 /api/v1/devices，板端至少上传过一次才会出现', 'error');
    return null;
  }
  const body = { device_id: state.current, kind: kind, timeout_s: TASK.timeoutS };
  try {
    if (kind === 'capture') {
      const params = readTaskParams();
      body.source = params.source;
      body.sample_count = params.sample_count;
      body.sample_rate_hz = params.sample_rate_hz;
      body.timeout_s = params.timeout_s;
    } else {
      // 控制任务不采数据，但仍需合法的 source/unit/sample_*（服务端 NOT NULL 字段）
      body.source = ($.sourceSel && $.sourceSel.value) || TASK_LIMITS.source;
      if (kind === 'pause') {
        body.duration_s = (extra && extra.duration_s) ?? readPauseDuration();
      }
    }
  } catch (e) {
    renderStatus('⚠ 任务参数不合法：' + e.message, 'error');
    return null;
  }

  const buttons = kind === 'capture' ? [$.captureBtn]
                : (kind === 'camera' ? [$.photoBtn]
                : (kind === 'preview' ? [$.previewBtn] : [$.pauseBtn, $.resumeBtn]));
  for (const b of buttons) if (b) b.disabled = true;
  try {
    const data = await apiPost('/api/v1/tasks', body);
    renderTask(data.task,
                data.duplicate ? '已复用未完成任务，未产生并行任务'
                               : (data.superseded ? '任务已创建（旧的反方向控制任务被取代）'
                                                  : '任务已创建'));
    watchTask(data.task.request_id);
    renderStatus('', 'hidden');
    refreshControl();       // 控制任务：板端 applied 前仍显示旧状态，倒计时会实时刷新
    refreshSidePanels();
    return data.task;
  } catch (e) {
    renderStatus('⚠ 创建任务失败（' + kind + '）：' + e.message, 'error');
    return null;
  } finally {
    for (const b of buttons) if (b) b.disabled = false;
  }
}

/* ------------------------------------------------------------------ *
 *  周期上报控制卡片（暂停 / 恢复）
 *
 *  真相源是服务端的 device_control 表（由板端 POST /applied 写入），页面通过
 *  GET /api/v1/control 读取；paused_until 到点后服务端惰性归零，所以这里显示
 *  的「剩余」与板端自动恢复时刻一致，不需要前端自己猜。
 * ------------------------------------------------------------------ */

/** 渲染暂停状态：徽标 + 状态/剩余/自动恢复时刻/最近控制任务/更新时间 */
function renderPeriodicControl(control) {
  const c = control || {};
  const paused = !!c.periodic_paused;
  if ($.ctrlBadge) {
    $.ctrlBadge.className = 'badge ' + (paused ? 'warn' : 'ok');
    $.ctrlBadge.textContent = paused ? '停止中' : '上报中';
  }
  if ($.ctrlState) {
    $.ctrlState.textContent = paused
      ? '已暂停（板端不再上传周期批次）'
      : '正常周期上报（每 1 s 一批 100 点）';
  }
  if ($.ctrlRemaining) {
    const left = Number(c.remaining_s);
    $.ctrlRemaining.textContent = paused
      ? (isFinite(left) ? Math.max(0, left).toFixed(0) + ' s 后自动恢复' : '—')
      : '—（未暂停）';
  }
  if ($.ctrlUntil) {
    $.ctrlUntil.textContent = paused
      ? (c.paused_until_str || '—') + '（板端自愈 + 服务端惰性归零）'
      : '—（未暂停）';
  }
  if ($.ctrlRid) {
    $.ctrlRid.textContent = c.request_id || '—（还没有控制任务）';
  }
  if ($.ctrlUpdated) {
    $.ctrlUpdated.textContent = c.updated_at_str || '—';
  }
}

/** 拉一次 /api/v1/control（主轮询节流触发 + 建控制任务后立即调用） */
async function refreshControl() {
  if (!state.current || !$.ctrlBadge) return;
  try {
    const data = await apiGet('/api/v1/control?device_id=' +
                              encodeURIComponent(state.current));
    renderPeriodicControl(data.control);
  } catch (e) {
    /* 控制接口不可用不应影响主面板 */
  }
}

/* ------------------------------------------------------------------ *
 *  摄像头实时画面（直播：板端长按 Button A 推流）
 *
 *  链路：板端长按 Button A（2 s）→ live_stream_task 每 ~250 ms 拍一帧
 *  640×480 JPEG 并 POST /api/v1/live?device_id=…&ts_ms=…&w=…&h=… →
 *  服务端只把最新一帧留在内存（不落盘、不入库、不建任务）→ 本页由独立节拍
 *  liveTickLoop() 调 refreshLive()（推流中 250 ms、空闲 1 s）：GET /api/v1/live
 *  看状态，seq 变了才去 GET /api/v1/live/frame 刷新 <img>（额外带 &seq= 防缓存）。
 *  active=false（超过 timeout_s 没有新帧）= 板端已停止推流 / 断网 / 掉电：
 *  画面保留最后一帧并标记「已停止」，而不是报错。
 * ------------------------------------------------------------------ */

const LIVE = { busy: false, lastAt: 0, lastSeq: -1, device: '', active: false };

/** 未推流 / 查询失败 / 无帧时统一走这里：占位提示 + 隐藏 <img> + 徽章与元信息 */
function renderLiveOff(text, meta, badgeText) {
  if ($.liveImg) $.liveImg.classList.add('hidden');
  if ($.liveOff) {
    $.liveOff.classList.remove('hidden');
    $.liveOff.textContent = text ||
      '—（板端长按 Button A 2 s 开启推流后，此处自动显示实时画面）';
  }
  if ($.liveMeta) $.liveMeta.textContent = meta || '—';
  if ($.liveBadge) {
    $.liveBadge.className = 'badge off';
    $.liveBadge.textContent = badgeText || '未推流';
  }
}

/** 直播状态 → 元信息文本（多行，CSS 用 white-space: pre-line 渲染） */
function liveMetaText(st, dev) {
  const age = (st.age_ms == null) ? '—' : (st.age_ms / 1000).toFixed(1) + ' s 前';
  return '设备：' + dev +
    '\n帧序号 seq：' + (st.seq ?? '—') +
    '\n分辨率：' + (st.width ?? '?') + '×' + (st.height ?? '?') +
    '\n单帧大小：' + (st.bytes ? (st.bytes / 1024).toFixed(1) + ' KB' : '—') +
    '\n最近一帧：' + (st.received_at_str || '—') + '（' + age + '）' +
    '\n板端拍摄时刻：' + (st.ts_ms_str || '—') +
    '\n来源 IP：' + (st.ip || '—') +
    '\n名义帧率：约 ' + (st.nominal_fps ?? '—') + ' fps（阈值 ' + st.timeout_s + ' s 无帧即视为停止）';
}

/** 独立节拍 liveTickLoop() 与主轮询都会调用（不 await）：只取最新一帧，
 *  没有新帧就不动 <img>；同时把 LIVE.active 回写，供节拍在 250 ms / 1 s 间切换。 */
async function refreshLive() {
  const img = $.liveImg;
  if (!img) return;
  const now = Date.now();
  if (LIVE.busy || now - LIVE.lastAt < LIVE_POLL_MIN_MS) return;
  LIVE.busy = true;
  LIVE.lastAt = now;
  try {
    // 未选设备时也查一次：板端可能正在推流、却还没上传过 IMU 周期数据（下拉里暂时没有它）
    const q = state.current ? '?device_id=' + encodeURIComponent(state.current) : '';
    let st = await apiGet('/api/v1/live' + q);
    if (LIVE.device !== (st.device_id || '')) {
      LIVE.lastSeq = -1;
      LIVE.device = st.device_id || '';
    }

    let dev = st.device_id || '';
    if (!dev) {
      const hot = (st.devices || []).find((d) => d.active) ||
                  (st.devices || [])[0] || null;
      if (!hot) {
        LIVE.active = false;
        renderLiveOff();
        if ($.liveInfo) $.liveInfo.textContent = '· 暂无设备推流';
        return;
      }
      dev = hot.device_id;
      st = await apiGet('/api/v1/live?device_id=' + encodeURIComponent(dev));
      if (LIVE.device !== dev) {
        LIVE.lastSeq = -1;
        LIVE.device = dev;
      }
    }

    if ($.liveTimeoutS && st.timeout_s) $.liveTimeoutS.textContent = String(st.timeout_s);
    if ($.liveInfo) $.liveInfo.textContent = '· ' + dev;
    LIVE.active = !!st.active;   // 决定独立节拍用 250 ms（推流中）还是 1 s（空闲）

    const meta = liveMetaText(st, dev);
    if (!st.seq) {      // 该设备从未推过流（服务端内存里没有它的帧）
      renderLiveOff('—（' + dev + ' 尚未推流：请在板端长按 Button A 2 s 开启）',
                    meta, '未推流');
      return;
    }

    if (st.seq !== LIVE.lastSeq) {
      LIVE.lastSeq = st.seq;
      // 服务端已带 Cache-Control: no-store；seq 兜底避免浏览器复用上一帧
      img.src = '/api/v1/live/frame?device_id=' + encodeURIComponent(dev) +
                '&seq=' + st.seq + '&t=' + now;
    }
    img.classList.remove('hidden');
    if ($.liveOff) $.liveOff.classList.add('hidden');
    if ($.liveMeta) $.liveMeta.textContent = meta;
    if ($.liveBadge) {
      $.liveBadge.className = 'badge ' + (st.active ? 'ok' : 'warn');
      $.liveBadge.textContent = st.active
        ? ('推流中 · ' + (st.width ?? '?') + '×' + (st.height ?? '?'))
        : '已停止（保留最后一帧）';
    }
  } catch (e) {
    LIVE.active = false;
    renderLiveOff('—（直播状态查询失败：' + e.message + '）', '—', '查询失败');
  } finally {
    LIVE.busy = false;
  }
}

/* ------------------------------------------------------------------ *
 *  闭环事件（板端按键触发 → 本地反馈 → 远端显示 → 回应/取消）
 *
 *  方向与页面上其它卡片相反：**板端发起、Web 接收**。
 *    板端按触发键 → POST /api/v1/events/trigger（request_id 由板端生成）
 *    → 本卡片从 GET /api/v1/events 读到它，显示「待处理」
 *    → 本页点「回应 / 取消 / 确认完成」走 POST /api/v1/events/respond
 *    → 板端每 1 s 轮询 GET /api/v1/events/status，看到终态就闪灯 + 显示 DONE
 *  刻意不做 Web 端「下发触发」按钮：触发只能来自板子上的物理按键，
 *  否则「本地反馈先于网络」这条设计（trigger 前灯已闪）就无从验证。
 * ------------------------------------------------------------------ */

/** 板端触发时刻：优先用板端 NTP 时间戳（epoch ms），缺失时退回服务端接收时间 */
function fmtEventDeviceTs(ev) {
  if (!ev || !ev.device_ts_ms) return '—（板端未带时间）';
  return fmtTsMs(ev.device_ts_ms);
}

/** 禁用/启用三个响应按钮（终态事件不允许再响应） */
function setEventButtons(enabled) {
  const can = !!enabled;
  for (const b of [$.eventAcceptBtn, $.eventCancelBtn, $.eventConfirmBtn]) {
    if (b) b.disabled = !can;
  }
}

function renderEventDetail(ev) {
  if (!ev) {
    $.eventRid.textContent = '—';
    $.eventStatus.textContent = '暂无事件（板端按触发键后会出现在这里）';
    $.eventSource.textContent = '—';
    $.eventDeviceTs.textContent = '—';
    $.eventCreated.textContent = '—';
    $.eventResponded.textContent = '—';
    $.eventTtl.textContent = '—';
    setEventButtons(false);
    state.eventRid = '';
    return;
  }

  state.eventRid = ev.request_id;
  $.eventRid.textContent = ev.request_id;
  $.eventStatus.textContent = ev.status_cn + '（' + ev.status + '）'
    + (ev.terminal ? ' · 已终结' : '');
  $.eventSource.textContent = ev.device_id + ' / ' + ev.kind;
  $.eventDeviceTs.textContent = fmtEventDeviceTs(ev);
  $.eventCreated.textContent = ev.created_at_str || fmtTsMs(ev.created_at * 1000);
  $.eventResponded.textContent = ev.responded_at_str
    ? (EVENT_ACTION_CN[ev.response] || ev.response || '—')
      + ' · ' + (EVENT_BY_CN[ev.responded_by] || ev.responded_by || '—')
      + ' · ' + ev.responded_at_str
    : '—（尚无响应）';
  $.eventTtl.textContent = ev.terminal
    ? '已结束'
    : (ev.ttl_left_s > 0 ? ev.ttl_left_s.toFixed(0) + ' s 后过期' : '已过期');

  // 终态事件没有可响应动作；待处理/已回应才能点
  setEventButtons(!ev.terminal);
}

function renderEventHistory(list) {
  if (!$.eventHistoryBody) return;
  if (!list.length) {
    $.eventHistoryBody.innerHTML =
      '<tr><td colspan="7">—（该设备还没有闭环事件）</td></tr>';
    return;
  }
  $.eventHistoryBody.innerHTML = '';
  for (const ev of list) {
    const tr = document.createElement('tr');
    const cells = [
      ev.created_at_str || '—',
      ev.request_id.slice(0, 12) + '…',
      ev.device_id,
      ev.status_cn,
      EVENT_ACTION_CN[ev.response] || ev.response || '—',
      EVENT_BY_CN[ev.responded_by] || ev.responded_by || '—',
      ev.closed_at_str || '—',
    ];
    for (const c of cells) {
      const td = document.createElement('td');
      td.textContent = c;
      tr.appendChild(td);
    }
    $.eventHistoryBody.appendChild(tr);
  }
}

/** 拉取闭环事件：刷新卡片 + 流水表。静默失败，不影响主面板。 */
async function refreshEvents(withHistory) {
  if (!state.current) return;
  try {
    const data = await apiGet('/api/v1/events?device_id=' +
                              encodeURIComponent(state.current) +
                              '&limit=' + EVENT_LIMIT);
    const list = (data && data.events) || [];
    renderEventDetail(list[0] || null);
    /* 流水表只在节流轮次（或响应动作后）重画：它和"待处理"提示不同，
     * 不需要 800 ms 刷新一次，避免每轮都重建 8 行 DOM。 */
    if (withHistory) {
      renderEventHistory(list);
    }

    const pending = (data && data.pending) || 0;
    const cls = pending > 0 ? 'err' : (list.length ? 'ok' : 'off');
    const txt = pending > 0 ? pending + ' 条待处理'
                            : (list.length ? '最近 ' + list.length + ' 条' : '无事件');
    /* 不能复用 renderBadge()：它把 id 写死成 stateBadge（数据卡片专用），
     * 这里必须用自己的 id，否则下一轮 el('eventBadge') 会拿到 null。 */
    $.eventBadge.outerHTML = '<span id="eventBadge" class="badge ' + cls + '">'
                             + txt + '</span>';
    $.eventBadge = el('eventBadge');
  } catch (e) {
    if (withHistory) {
      $.eventStatusLine.textContent = '—（事件查询失败：' + e.message + '）';
    }
  }
}

/** 对当前事件发送响应动作 */
async function respondEvent(action) {
  const rid = state.eventRid;
  if (!rid) return;
  if (action === 'cancel' &&
      !window.confirm('确认取消闭环事件 ' + rid.slice(0, 12) + '… ？')) {
    return;
  }
  setEventButtons(false);
  try {
    await apiPost('/api/v1/events/respond',
                  { request_id: rid, action: action, by: 'web' });
    $.eventStatusLine.textContent = '已发送「' + (EVENT_ACTION_CN[action] || action)
      + '」：' + new Date().toLocaleTimeString('zh-CN', { hour12: false });
    await refreshEvents(true);
  } catch (e) {
    $.eventStatusLine.textContent = '⚠ 响应失败：' + e.message;
    setEventButtons(true);
  }
}

if ($.eventAcceptBtn) {
  $.eventAcceptBtn.addEventListener('click', () => respondEvent('accept'));
  $.eventCancelBtn.addEventListener('click', () => respondEvent('cancel'));
  $.eventConfirmBtn.addEventListener('click', () => respondEvent('confirm'));
}

/* ------------------------------------------------------------------ *
 *  实时拍照（kind=camera 任务 + 照片画廊）
 *
 *  链路：点「拍一张照片」→ POST /api/v1/tasks {kind:"camera"} →
 *  板端 ≤3 s 轮询领取 → 拍一帧 640×480 JPEG 原样 POST /api/v1/photos →
 *  服务端落盘 + 入库并在同一事务里把任务置 completed。
 *  画廊数据来自 GET /api/v1/photos?device_id=…&limit=12（新的在前），
 *  每张照片的缩略图直接用 GET /api/v1/photos/{id}，删除走
 *  DELETE /api/v1/photos/{id}（服务端同时删库行与磁盘文件）。
 * ------------------------------------------------------------------ */

/** 点击「拍一张照片」：建 kind=camera 任务 → 跟踪任务 → 终态后画廊自动刷新 */
async function capturePhoto() {
  if (!state.current) {
    renderStatus('⚠ 请先选择设备：下拉来自 /api/v1/devices，板端至少上传过一次才会出现', 'error');
    return null;
  }
  if ($.photoStatus) $.photoStatus.textContent = '已下发拍照任务，等待板端领取…';
  if ($.photoBadge) {
    $.photoBadge.className = 'badge warn';
    $.photoBadge.textContent = '等待板端';
  }
  const task = await createTask('camera');
  if (!task) {
    if ($.photoStatus) $.photoStatus.textContent = '—';
    if ($.photoBadge) {
      $.photoBadge.className = 'badge off';
      $.photoBadge.textContent = '空闲';
    }
    return null;
  }
  if ($.photoStatus) {
    $.photoStatus.textContent = '任务 ' + shortRid(task.request_id) +
      ' 已下发（板端 ≤3 s 轮询领取，拍好即上传）';
  }
  return task;
}

/** 点击「切换本地预览」：建 kind=preview 任务（toggle），板端 applied 后即生效 */
async function togglePreview() {
  if (!state.current) {
    renderStatus('⚠ 请先选择设备：下拉来自 /api/v1/devices，板端至少上传过一次才会出现', 'error');
    return null;
  }
  if ($.photoStatus) $.photoStatus.textContent = '已下发本地预览切换任务，等待板端领取…';
  const task = await createTask('preview');
  if (!task) {
    if ($.photoStatus) $.photoStatus.textContent = '—';
    return null;
  }
  if ($.photoStatus) {
    $.photoStatus.textContent = '任务 ' + shortRid(task.request_id) +
      ' 已下发（板端 ≤3 s 轮询领取，切换 LCD 预览）';
  }
  return task;
}

/** 画廊：拉该设备最近 PHOTO_LIMIT 张照片并渲染（失败只在卡片内提示，不影响主面板） */
async function refreshGallery() {
  const grid = $.photoGrid;
  if (!grid) return;
  if (!state.current) {
    grid.innerHTML = '<div class="photo-empty">—（请先选择设备）</div>';
    if ($.photoInfo) $.photoInfo.textContent = '· 未选择设备';
    return;
  }
  let data;
  try {
    data = await apiGet('/api/v1/photos?device_id=' + encodeURIComponent(state.current) +
                        '&limit=' + PHOTO_LIMIT);
  } catch (e) {
    grid.innerHTML = '<div class="photo-empty">—（照片列表查询失败：' + e.message + '）</div>';
    return;
  }
  const list = (data && data.photos) || [];
  if ($.photoInfo) {
    $.photoInfo.textContent = '· ' + state.current + ' · 共 ' + (data.total ?? 0) +
      ' 张（显示最近 ' + list.length + ' 张）';
  }
  if ($.photoBadge) {
    $.photoBadge.className = 'badge ' + (list.length ? 'ok' : 'off');
    $.photoBadge.textContent = list.length ? (list.length + ' 张') : '暂无照片';
  }
  if (!list.length) {
    grid.innerHTML = '<div class="photo-empty">—（该设备还没有照片，点「拍一张照片」触发板端拍摄）</div>';
    return;
  }
  const frag = document.createDocumentFragment();
  for (const p of list) frag.appendChild(photoCard(p));
  grid.innerHTML = '';
  grid.appendChild(frag);
}

/** 单张照片卡片：缩略图（点击看原图）+ 元数据 + 原图/删除按钮 */
function photoCard(p) {
  const url = p.url || ('/api/v1/photos/' + p.id);
  const wrap = document.createElement('div');
  wrap.className = 'photo-card';
  wrap.dataset.photoId = p.id;

  const img = document.createElement('img');
  // 服务端已带 Cache-Control: no-store；加时间戳兜底，避免删后重建同 id 时命中浏览器缓存
  img.src = url + '?t=' + Date.now();
  img.alt = 'photo #' + p.id;
  img.title = '点击查看原图';
  img.addEventListener('click', () => window.open(url, '_blank', 'noopener'));

  const meta = document.createElement('div');
  meta.className = 'photo-meta';
  meta.innerHTML =
    '<b>#' + p.id + '</b> · ' + (p.size_kb ?? '—') + ' KB · ' +
    (p.width ?? '?') + '×' + (p.height ?? '?') + '<br>' +
    '接收 ' + (p.received_at_str || '—') + '<br>' +
    (p.request_id ? '任务 ' + shortRid(p.request_id) : '来源：直接上传（无任务号）');
  // note 是**上报方可控的自由文本**（板端 / 任意能访问服务端的客户端都能写入），
  // 不能拼进 innerHTML —— 一个 "备注" 里塞 <img onerror=…> 就能在页面上执行脚本。
  // 上面已拼好的部分只会插入数字与 request_id（服务端白名单字符集），可继续用
  // innerHTML；note 单独用 createTextNode 交给浏览器转义，不手写转义函数。
  if (p.note) {
    meta.appendChild(document.createElement('br'));
    meta.appendChild(document.createTextNode('备注：' + p.note));
  }

  const actions = document.createElement('div');
  actions.className = 'photo-actions';
  const openBtn = document.createElement('button');
  openBtn.type = 'button';
  openBtn.textContent = '原图';
  openBtn.addEventListener('click', () => window.open(url, '_blank', 'noopener'));
  const delBtn = document.createElement('button');
  delBtn.type = 'button';
  delBtn.textContent = '删除';
  delBtn.title = 'DELETE /api/v1/photos/' + p.id + '（同时删除数据库记录与磁盘 JPEG）';
  delBtn.addEventListener('click', () => deletePhoto(p.id));
  actions.appendChild(openBtn);
  actions.appendChild(delBtn);

  wrap.appendChild(img);
  wrap.appendChild(meta);
  wrap.appendChild(actions);
  return wrap;
}

/** 删除一张照片：确认 → DELETE → 重新拉画廊（无论成功失败都重拉，避免界面与库不一致） */
async function deletePhoto(photoId) {
  const ok = window.confirm('确定删除照片 #' + photoId +
                            '？服务端会同时删除数据库记录与磁盘 JPEG 文件，不可恢复。');
  if (!ok) return;
  try {
    const res = await apiDelete('/api/v1/photos/' + photoId);
    renderStatus('已删除照片 #' + photoId +
                 (res && res.file_deleted === false ? '（磁盘文件此前已不存在）' : ''),
                 'info');
  } catch (e) {
    renderStatus('⚠ 删除照片 #' + photoId + ' 失败：' + e.message, 'error');
  }
  refreshGallery();
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
$.pauseBtn.addEventListener('click', () => pausePeriodic());
$.resumeBtn.addEventListener('click', () => resumePeriodic());
$.photoBtn.addEventListener('click', () => capturePhoto());
$.previewBtn.addEventListener('click', () => togglePreview());

// 切换设备后回填该设备最近的任务、任务历史与对照区（等待 poll() 完成设备切换）
$.deviceSel.addEventListener('change', () => {
  stopTaskWatch();
  HIST.offset = 0;                 // 历史记录回到第一页（换设备了）
  setTimeout(refreshSidePanels, 900);
  setTimeout(refreshHistory, 900);
});

// 设备列表首次加载后按钮才可用；这里先放开，点击时会校验是否已选设备
$.captureBtn.disabled = false;
$.pauseBtn.disabled = false;
$.resumeBtn.disabled = false;
$.photoBtn.disabled = false;
$.previewBtn.disabled = false;
syncFormFromUrl();       // 先让 ?samples=/&rate=/&timeout=/&source=/&pause= 覆盖表单初值
refreshSidePanels();

// 便于自动化验证：用 /ui/?autocapture=1 打开页面即触发一次采集（等价于点按钮），
// 无需人工点击也能核对「建任务 → 板端领取 → 带 request_id 回传」这条链路。
// 这几个自动参数同时会把分区切到对应那一页，否则动作发生在隐藏面板里，人看不到结果。
// （switchTab 用 setTimeout 推迟到脚本求值结束：TABS/activeTab 定义在文件末尾，
//   这行同步执行会命中 TDZ；initTabs 在求值阶段跑完，0 ms 回调随后覆盖它。）
try {
  const params = new URLSearchParams(window.location.search);
  if (params.get('autocapture') === '1') {
    setTimeout(() => switchTab('task'), 0);
    setTimeout(captureOnce, 1200);
  }
  // ?autophoto=1：打开页面即触发一次拍照（等价于点「拍一张照片」），
  // 用于自动核对「建 camera 任务 → 板端拍照上传 → 画廊出现新照片」这条链路。
  if (params.get('autophoto') === '1') {
    setTimeout(() => switchTab('camera'), 0);
    setTimeout(capturePhoto, 1200);
  }
  // ?autopause=1[&pause=90]：打开页面即触发一次「暂停周期」（等价于点按钮），
  // 用于自动核对「建 pause 任务 → 板端 applied → device_control 置停止中」这条链路。
  if (params.get('autopause') === '1') {
    setTimeout(() => switchTab('control'), 0);
    const dur = Number(params.get('pause') || TASK_LIMITS.pauseDefault);
    setTimeout(() => createTask('pause', { duration_s: dur }), 1200);
  }
  // ?autoresume=1：打开页面即触发一次「恢复周期」（提前结束暂停窗口）
  if (params.get('autoresume') === '1') {
    setTimeout(() => switchTab('control'), 0);
    setTimeout(() => createTask('resume'), 1200);
  }
  // ?autovoice=1&voice_text=查看系统状态：自动走一次文字版语音助手链路，
  // 用于无头浏览器验证输入 → Agent → 结果气泡/时序日志（不伪造麦克风录音）。
  if (params.get('autovoice') === '1') {
    setTimeout(() => switchTab('voice'), 0);
    setTimeout(() => {
      if (!$.voiceInput) return;
      $.voiceInput.value = params.get('voice_text') || '查看系统状态';
      voiceSubmit($.voiceInput.value, { inputSource: '自动验收文字输入' });
    }, 1200);
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

/* ------------------------------------------------------------------ *
 *  数据采集历史（一行 = uploads 表里的一批数据；点开看这一批的原始采样点）
 *
 *  与「采集任务」分区的区别：任务表记的是「你让板端做的事」（下发/回执/完成），
 *  这里记的是「板端真的传回来的数据批次」—— 点数、批次起点、触发方式、来源 IP，
 *  以及每批的原始采样点和 CSV 导出。数据源就是 /api/v1/upload 写进去的
 *  uploads / samples 两张表，不额外落盘、不新增存储。
 *
 *  接口：GET /api/v1/uploads（列表，分页 + trigger 过滤）
 *        GET /api/v1/uploads/{id}（单批元数据 + 该批样本，带 t_abs_ms）
 *  样本量级：板端单批 ≤600 点（TASK_MAX_SAMPLES），详情一次取回（上限 5000），
 *  前端按 50 条分页显示，不再为翻页多开接口。
 * ------------------------------------------------------------------ */

const HIST = {
  limit: 20,        // 列表每页批次数（与 histLimit 下拉同步）
  offset: 0,        // 列表偏移（分页游标）
  total: 0,         // 满足条件的总批次数
  trigger: '',      // '' = 全部 / manual / periodic
  busy: false,      // 防重入（主轮询与手动刷新可能同时触发）
  openId: '',       // 当前打开的批次号（列表里高亮该行）
  detail: null,     // { upload, samples, truncated }
  page: 0,          // 详情里样本表的页码
  pageSize: 50,     // 详情里每页显示多少条样本
};

const HIST_SAMPLE_LIMIT = 5000;    // 单批样本取回上限（与服务端 UPLOAD_SAMPLE_MAX_LIMIT 同值）
const HIST_TRIGGER_CN = { manual: 'manual（按需采集）', periodic: 'periodic（周期上报）' };

/** 列表查询串：设备跟随顶部设备下拉；未选设备时列全部设备的批次 */
function histQuery() {
  const q = [];
  if (state.current) q.push('device_id=' + encodeURIComponent(state.current));
  if (HIST.trigger) q.push('trigger=' + encodeURIComponent(HIST.trigger));
  q.push('limit=' + HIST.limit);
  q.push('offset=' + Math.max(0, HIST.offset));
  return '/api/v1/uploads?' + q.join('&');
}

/** 拉列表并渲染（失败也要说清楚原因，而不是留一个空表） */
async function refreshHistory() {
  if (HIST.busy) return;
  HIST.busy = true;
  try {
    const data = await apiGet(histQuery());
    HIST.total = data.total || 0;
    HIST.offset = data.offset || 0;
    HIST.limit = data.limit || HIST.limit;
    renderHistoryList(data.uploads || []);
  } catch (e) {
    histSetBodyText('—（历史数据查询失败：' + e.message + '）');
    if ($.histInfo) $.histInfo.textContent = '· 查询失败';
    if ($.histBadge) { $.histBadge.className = 'badge err'; $.histBadge.textContent = '读取失败'; }
  } finally {
    HIST.busy = false;
  }
}

/** 列表空表 / 出错时的占位行 */
function histSetBodyText(text) {
  const tr = document.createElement('tr');
  const td = document.createElement('td');
  td.colSpan = 8;
  td.textContent = text;
  tr.appendChild(td);
  $.histBody.innerHTML = '';
  $.histBody.appendChild(tr);
}

/** 列表渲染：批号 / 接收时间 / 批次起点 / 点数 / 触发方式 / request_id / 来源 / 操作。
 *  所有文本走 textContent —— source / ip 等字段是上报方可控的自由文本。 */
function renderHistoryList(list) {
  if (!list.length) {
    histSetBodyText(state.current
      ? '—（该设备还没有采集记录；板端上传一批数据后这里就会出现）'
      : '—（还没有任何采集记录；设备开始上报后这里就会出现）');
  } else {
    const body = $.histBody;
    body.innerHTML = '';
    const frag = document.createDocumentFragment();
    for (const u of list) {
      const open = String(u.id) === String(HIST.openId);
      const tr = document.createElement('tr');
      if (open) tr.className = 'hist-open';
      const cells = [
        ['#' + u.id, 'upload_id=' + u.id],
        [u.received_at_str || '—', 'received_at=' + (u.received_at ?? '')],
        [u.ts_ms_str || '—', 'ts_ms=' + (u.ts_ms ?? '')],
        [(u.sample_count ?? '—') + ' 点', ''],
        [HIST_TRIGGER_CN[u.trigger] || (u.trigger || '—'), ''],
        [shortRid(u.request_id), u.request_id || '（周期批次不带任务号）'],
        [(u.source || '—') + (u.ip ? ' · ' + u.ip : ''),
         'source=' + (u.source || '—') + ' unit=' + (u.unit || '—') + ' ip=' + (u.ip || '—')],
      ];
      for (const [text, title] of cells) {
        const td = document.createElement('td');
        td.textContent = text;
        if (title) td.title = title;
        tr.appendChild(td);
      }
      const tdBtn = document.createElement('td');
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.dataset.uploadId = String(u.id);
      btn.textContent = open ? '刷新样本' : '查看样本';
      tdBtn.appendChild(btn);
      tr.appendChild(tdBtn);
      frag.appendChild(tr);
    }
    body.appendChild(frag);
  }

  if ($.histInfo) {
    $.histInfo.textContent = '· ' + (state.current || '全部设备') +
      ' · 共 ' + HIST.total + ' 批' +
      (HIST.total ? ' · 当前显示第 ' + (HIST.offset + 1) + '-' +
                    (HIST.offset + list.length) + ' 批' : '') +
      (HIST.trigger ? ' · 只看 ' + HIST.trigger : '');
  }
  if ($.histBadge) {
    $.histBadge.className = 'badge ' + (HIST.total ? 'ok' : 'off');
    $.histBadge.textContent = HIST.total + ' 批';
  }
  $.histPrevBtn.disabled = HIST.offset <= 0;
  $.histNextBtn.disabled = (HIST.offset + HIST.limit) >= HIST.total;
}

/** 打开某一批：元数据 + 原始波形 + 样本表（前端分页） */
async function openUploadDetail(id) {
  const sid = String(id || '');
  if (!/^\d+$/.test(sid)) return;
  HIST.openId = sid;
  HIST.page = 0;
  HIST.detail = null;
  $.histDetailCard.classList.remove('hidden');
  $.histDetailTitle.textContent = '· 批次 #' + sid + '（加载中…）';
  $.histDetailGrid.innerHTML = '';
  $.histCsvBtn.disabled = true;
  $.histSampleBody.innerHTML = '<tr><td colspan="6">加载中…</td></tr>';
  $.histSampleInfo.textContent = '';
  $.histSamplePrevBtn.disabled = true;
  $.histSampleNextBtn.disabled = true;
  try {
    const data = await apiGet('/api/v1/uploads/' + encodeURIComponent(sid) +
                              '?limit=' + HIST_SAMPLE_LIMIT);
    HIST.detail = {
      upload: data.upload || {},
      samples: data.samples || [],
      truncated: !!data.truncated,
    };
    renderUploadDetail();
  } catch (e) {
    $.histDetailTitle.textContent = '· 批次 #' + sid;
    $.histSampleBody.innerHTML = '';
    const tr = document.createElement('tr');
    const td = document.createElement('td');
    td.colSpan = 6;
    td.textContent = '—（读取失败：' + e.message + '）';
    tr.appendChild(td);
    $.histSampleBody.appendChild(tr);
    renderStatus('⚠ 读取历史批次 #' + sid + ' 失败：' + e.message, 'error');
  }
  refreshHistory();     // 让列表跟上（行高亮 / 按钮文案）
}

/** 详情里的一个「k / v」单元格（与页面上其它卡片同一套样式） */
function histKv(label, value, title) {
  const wrap = document.createElement('div');
  wrap.className = 'kv';
  const k = document.createElement('span');
  k.className = 'k';
  k.textContent = label;
  const v = document.createElement('span');
  v.className = 'v';
  v.textContent = value;
  if (title) v.title = title;
  wrap.appendChild(k);
  wrap.appendChild(v);
  return wrap;
}

/** 单批 X 轴的刻度步长：目标 6 格，取「好看的」数值 */
function histTickStep(target) {
  const steps = [0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 30, 60, 120, 300, 600];
  for (const s of steps) {
    if (s >= target) return s;
  }
  return 600;
}

function renderUploadDetail() {
  const d = HIST.detail;
  if (!d) return;
  const up = d.upload;
  const samples = d.samples;

  $.histDetailTitle.textContent = '· 批次 #' + up.id + ' · ' + (up.device_id || '—') +
    ' · 共 ' + samples.length + ' 点';

  const grid = $.histDetailGrid;
  grid.innerHTML = '';
  grid.appendChild(histKv('设备 ID', up.device_id || '—'));
  grid.appendChild(histKv('数据源 / 单位', (up.source || '—') + ' (' + (up.unit || '—') + ')'));
  grid.appendChild(histKv('本批样本数', (up.sample_count ?? '—') + ' 点'));
  grid.appendChild(histKv('批次起点 ts_ms', up.ts_ms_str || '—', 'ts_ms=' + (up.ts_ms ?? '')));
  grid.appendChild(histKv('服务端接收时间', up.received_at_str || '—',
                          'received_at=' + (up.received_at ?? '')));
  grid.appendChild(histKv('触发方式 trigger', HIST_TRIGGER_CN[up.trigger] || (up.trigger || '—')));
  grid.appendChild(histKv('任务号 request_id', up.request_id || '—（周期批次不带任务号）'));
  grid.appendChild(histKv('来源', (up.ip || '—') + ' · upload_id=' + up.id));

  // 波形用这一批的时长铺满 X 轴（实时波形那套固定 30 s 窗口不适合看单批）
  if ($.histChartUnit) $.histChartUnit.textContent = up.unit || 'm/s^2';
  const points = samples.map((s) => ({ t: s.t_abs_ms, ax: s.ax, ay: s.ay, az: s.az }));
  if (points.length >= 2) {
    const spanS = Math.max(0.05, (points[points.length - 1].t - points[0].t) / 1000);
    drawChart(points, {
      canvas: $.histChart,
      countEl: $.histChartCount,
      spanS: spanS,
      tickS: histTickStep(spanS / 6),
      xLabel: (s) => Math.max(0, spanS - s).toFixed(1) + 's',
    });
  } else {
    drawChart(points, { canvas: $.histChart, countEl: $.histChartCount });
  }

  $.histCsvBtn.disabled = !samples.length;
  renderUploadSamples();
}

/** 详情里的样本表：前端按 pageSize 分页（单批通常 ≤600 条） */
function renderUploadSamples() {
  const d = HIST.detail;
  if (!d) return;
  const total = d.samples.length;
  const pages = Math.max(1, Math.ceil(total / HIST.pageSize));
  HIST.page = Math.min(Math.max(0, HIST.page), pages - 1);
  const start = HIST.page * HIST.pageSize;
  const rows = d.samples.slice(start, start + HIST.pageSize);
  const body = $.histSampleBody;
  body.innerHTML = '';

  if (!total) {
    const tr = document.createElement('tr');
    const td = document.createElement('td');
    td.colSpan = 6;
    td.textContent = '—（这一批没有样本点）';
    tr.appendChild(td);
    body.appendChild(tr);
  } else {
    const frag = document.createDocumentFragment();
    for (const s of rows) {
      const tr = document.createElement('tr');
      for (const v of [s.seq, s.t_ms, s.t_abs_ms, fmtNum(s.ax), fmtNum(s.ay), fmtNum(s.az)]) {
        const td = document.createElement('td');
        td.textContent = v;
        tr.appendChild(td);
      }
      frag.appendChild(tr);
    }
    body.appendChild(frag);
  }
  $.histSampleInfo.textContent = total
    ? '· 第 ' + (start + 1) + '-' + (start + rows.length) + ' 条 / 共 ' + total + ' 条' +
      (d.truncated ? '（服务端只返回了前 ' + total + ' 条）' : '')
    : '';
  $.histSamplePrevBtn.disabled = HIST.page <= 0;
  $.histSampleNextBtn.disabled = HIST.page >= pages - 1;
}

function closeUploadDetail() {
  HIST.detail = null;
  HIST.openId = '';
  $.histDetailCard.classList.add('hidden');
  refreshHistory();     // 清掉行高亮与「刷新样本」文案
}

/** 导出这一批为 CSV：列名与板端 SD 卡 CSV 对齐，label 填触发方式 */
function histDownloadCsv() {
  const d = HIST.detail;
  if (!d || !d.samples.length) return;
  const up = d.upload || {};
  const lines = [
    '# device_id=' + (up.device_id || '') + ' upload_id=' + (up.id || '') +
      ' source=' + (up.source || '') + ' unit=' + (up.unit || '') +
      ' trigger=' + (up.trigger || ''),
    '# ts_ms=' + (up.ts_ms || '') + ' received_at=' + (up.received_at_str || ''),
    'label,timestamp_ms,accel_x,accel_y,accel_z',
  ];
  for (const s of d.samples) {
    lines.push([up.trigger || '', s.t_abs_ms, s.ax, s.ay, s.az].join(','));
  }
  // BOM：Windows 双击打开 CSV 时 Excel 才不会把中文/数字搞乱
  const blob = new Blob(['\ufeff' + lines.join('\r\n')], { type: 'text/csv;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = 'imu_' + String(up.device_id || 'device').replace(/[^\w.-]+/g, '_') +
               '_batch' + (up.id || '') + '_' + String(up.ts_ms || '') + '.csv';
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}

// ---- 历史记录分区的控件 ----
$.histTrigger.addEventListener('change', () => {
  HIST.trigger = $.histTrigger.value;
  HIST.offset = 0;
  refreshHistory();
});
$.histLimit.addEventListener('change', () => {
  HIST.limit = Number($.histLimit.value) || 20;
  HIST.offset = 0;
  refreshHistory();
});
$.histPrevBtn.addEventListener('click', () => {
  HIST.offset = Math.max(0, HIST.offset - HIST.limit);
  refreshHistory();
});
$.histNextBtn.addEventListener('click', () => {
  HIST.offset += HIST.limit;
  refreshHistory();
});
$.histRefreshBtn.addEventListener('click', () => refreshHistory());
$.histCloseBtn.addEventListener('click', () => closeUploadDetail());
$.histCsvBtn.addEventListener('click', () => histDownloadCsv());
$.histSamplePrevBtn.addEventListener('click', () => { HIST.page--; renderUploadSamples(); });
$.histSampleNextBtn.addEventListener('click', () => { HIST.page++; renderUploadSamples(); });

// 「查看样本」按钮是每行动态生成的，用事件委托接（避免每次渲染绑 N 个监听）
$.histBody.addEventListener('click', (ev) => {
  const btn = (ev.target && ev.target.closest) ? ev.target.closest('button[data-upload-id]') : null;
  if (btn) openUploadDetail(btn.dataset.uploadId);
});

/* ------------------------------------------------------------------ *
 *  语音任务助手
 *
 *  文字和录音不是两条业务链路：录音只负责把浏览器识别结果填进输入框，
 *  最终都走 POST /api/v1/agent/chat。每个请求带独立的 Agent request_id；
 *  「停止」会 Abort 当前 fetch、停止识别/播报，并请求服务端取消仍在轮询的
 *  设备任务。generation 单调递增，用来丢弃取消后才返回的迟到响应。
 * ------------------------------------------------------------------ */

const VOICE_SESSION_KEY = 'imu_voice_agent_session';
const VOICE_LOG_MAX = 40;
const SpeechRecognitionCtor = window.SpeechRecognition ||
                              window.webkitSpeechRecognition || null;

const VOICE = {
  generation: 0,
  busy: false,
  stopping: false,
  requestId: '',
  taskRequestId: '',
  sessionId: '',
  startedAt: 0,
  firstFeedbackMs: null,
  agentResponseMs: null,
  abortController: null,
  recognition: null,
  listening: false,
  recognitionToken: 0,
  recognitionError: '',
  captureRequestId: '',
  transcript: '',
  interimLogged: false,
  pendingOptions: [],
  logs: [],
  cancellationWarning: '',
};

try {
  VOICE.sessionId = window.sessionStorage.getItem(VOICE_SESSION_KEY) || '';
} catch (e) {
  VOICE.sessionId = '';
}

function voiceNewRequestId(prefix) {
  let token = '';
  try {
    token = window.crypto && window.crypto.randomUUID
      ? window.crypto.randomUUID()
      : (Date.now().toString(36) + '-' + Math.random().toString(36).slice(2));
  } catch (e) {
    token = Date.now().toString(36) + '-' + Math.random().toString(36).slice(2);
  }
  token = String(token).replace(/[^A-Za-z0-9_-]/g, '').slice(0, 52);
  return (prefix || 'voice') + '-' + token;
}

function voiceClock(epochMs) {
  const d = new Date(epochMs || Date.now());
  const p = (n, width) => String(n).padStart(width || 2, '0');
  return p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds()) +
         '.' + p(d.getMilliseconds(), 3);
}

function voiceElapsedMs() {
  return VOICE.startedAt ? Math.max(0, performance.now() - VOICE.startedAt) : null;
}

function voiceRefreshButtons() {
  const synthBusy = !!(window.speechSynthesis && window.speechSynthesis.speaking);
  if ($.voiceRecordBtn) {
    $.voiceRecordBtn.disabled = VOICE.busy || VOICE.stopping;
    $.voiceRecordBtn.classList.toggle('recording', VOICE.listening);
    $.voiceRecordBtn.setAttribute('aria-pressed', VOICE.listening ? 'true' : 'false');
    $.voiceRecordBtn.textContent = VOICE.listening ? '■ 停止录音' : '🎙 开始录音';
  }
  if ($.voiceSendBtn) {
    $.voiceSendBtn.disabled = VOICE.busy || VOICE.stopping || !$.voiceInput ||
                              !$.voiceInput.value.trim();
  }
  if ($.voiceStopBtn) {
    $.voiceStopBtn.disabled = VOICE.stopping ||
      (!VOICE.busy && !VOICE.listening && !synthBusy);
  }
  if ($.voiceSpeakStopBtn) {
    $.voiceSpeakStopBtn.disabled = !synthBusy;
  }
  if ($.voiceClearBtn) {
    $.voiceClearBtn.disabled = VOICE.stopping;
  }
}

function voiceSetStage(label, cls, statusText) {
  if ($.voiceBadge) {
    $.voiceBadge.className = 'badge ' + (cls || 'off');
    $.voiceBadge.textContent = label || '空闲';
  }
  if ($.voiceStage) $.voiceStage.textContent = label || '空闲';
  if ($.voiceStatus && statusText !== undefined) $.voiceStatus.textContent = statusText;
  if ($.voiceStatusLine && statusText !== undefined) {
    $.voiceStatusLine.textContent = statusText || '';
  }
  voiceRefreshButtons();
}

function voiceSetLatency() {
  if (!$.voiceLatency) return;
  const parts = [];
  if (VOICE.firstFeedbackMs !== null) {
    parts.push('本地反馈 ' + Math.round(VOICE.firstFeedbackMs) + ' ms');
  }
  if (VOICE.agentResponseMs !== null) {
    parts.push('Agent 完成 ' + Math.round(VOICE.agentResponseMs) + ' ms');
  } else if (VOICE.startedAt) {
    parts.push('已等待 ' + Math.round(voiceElapsedMs()) + ' ms');
  }
  $.voiceLatency.textContent = parts.length ? parts.join(' · ') : '—';
}

function voiceAddBubble(role, text) {
  if (!$.voiceThread || !text) return;
  const bubble = document.createElement('div');
  bubble.className = 'voice-bubble ' + role;
  bubble.textContent = text;
  $.voiceThread.appendChild(bubble);
  $.voiceThread.scrollTop = $.voiceThread.scrollHeight;
}

function voiceSetOptions(options) {
  VOICE.pendingOptions = Array.isArray(options) ? options.slice() : [];
  if (!$.voiceOptions) return;
  $.voiceOptions.innerHTML = '';
  VOICE.pendingOptions.forEach((choice) => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.textContent = choice;
    btn.addEventListener('click', () => voiceSubmit(choice, {
      clarify: true,
      inputSource: '澄清选项',
    }));
    $.voiceOptions.appendChild(btn);
  });
}

function voiceRenderLog() {
  if (!$.voiceLogBody) return;
  if (!VOICE.logs.length) {
    $.voiceLogBody.innerHTML = '<tr><td colspan="5">—（尚未发起语音或文字任务）</td></tr>';
  } else {
    $.voiceLogBody.innerHTML = '';
    const frag = document.createDocumentFragment();
    for (const item of VOICE.logs) {
      const tr = document.createElement('tr');
      const values = [
        voiceClock(item.at),
        shortRid(item.requestId || '—'),
        item.phase,
        item.detail || '—',
        item.elapsedMs === null || item.elapsedMs === undefined
          ? '—' : Math.round(item.elapsedMs) + ' ms',
      ];
      values.forEach((value, idx) => {
        const td = document.createElement('td');
        td.textContent = value;
        if (idx === 1 && item.requestId) td.title = item.requestId;
        tr.appendChild(td);
      });
      frag.appendChild(tr);
    }
    $.voiceLogBody.appendChild(frag);
  }
  if ($.voiceLogInfo) {
    $.voiceLogInfo.textContent = '· 已记录 ' + VOICE.logs.length + ' 条';
  }
}

function voiceLog(phase, detail, requestId) {
  VOICE.logs.unshift({
    at: Date.now(),
    requestId: requestId || VOICE.requestId || '',
    phase: phase,
    detail: detail || '',
    elapsedMs: voiceElapsedMs(),
  });
  if (VOICE.logs.length > VOICE_LOG_MAX) VOICE.logs.length = VOICE_LOG_MAX;
  voiceRenderLog();
}

function voicePersistSession() {
  try {
    if (VOICE.sessionId) {
      window.sessionStorage.setItem(VOICE_SESSION_KEY, VOICE.sessionId);
    } else {
      window.sessionStorage.removeItem(VOICE_SESSION_KEY);
    }
  } catch (e) {
    /* 隐私模式忽略 */
  }
}

function voiceStopSpeech(silent) {
  const hadSpeech = !!(window.speechSynthesis &&
                       (window.speechSynthesis.speaking ||
                        window.speechSynthesis.pending));
  if (window.speechSynthesis) window.speechSynthesis.cancel();
  if ($.voiceSpeechState) $.voiceSpeechState.textContent = '已停止';
  if (hadSpeech && !silent) {
    voiceLog('播报停止', '用户停止浏览器语音合成');
  }
  voiceRefreshButtons();
}

function voiceSpeak(text) {
  if (!text || !window.speechSynthesis || !window.SpeechSynthesisUtterance) {
    if ($.voiceSpeechState) $.voiceSpeechState.textContent = '当前浏览器不支持播报';
    return;
  }
  voiceStopSpeech(true);
  const speechText = String(text).replace(/[`*#]/g, '').trim();
  if (!speechText) return;
  const utter = new SpeechSynthesisUtterance(speechText);
  utter.lang = 'zh-CN';
  utter.rate = 1.02;
  utter.onstart = () => {
    if ($.voiceSpeechState) $.voiceSpeechState.textContent = '播报中';
    voiceLog('播报开始', '浏览器语音合成');
    voiceRefreshButtons();
  };
  utter.onend = () => {
    if ($.voiceSpeechState) $.voiceSpeechState.textContent = '已完成';
    voiceLog('播报完成', '浏览器语音合成');
    voiceRefreshButtons();
  };
  utter.onerror = (ev) => {
    if ($.voiceSpeechState) $.voiceSpeechState.textContent = '播报失败';
    voiceLog('播报异常', ev.error || 'unknown');
    voiceRefreshButtons();
  };
  if ($.voiceSpeechState) $.voiceSpeechState.textContent = '准备播报';
  window.speechSynthesis.speak(utter);
  voiceRefreshButtons();
}

function voiceStopRecognition(abort) {
  const rec = VOICE.recognition;
  VOICE.listening = false;
  // graceful stop 仍允许浏览器补发最后一帧 final result 和 onend；
  // abort 才递增 token，让已经切走/清空的回调彻底失效。
  if (abort) VOICE.recognitionToken++;
  if (rec) {
    try {
      if (abort) rec.abort(); else rec.stop();
    } catch (e) {
      /* 已经结束 */
    }
  }
  voiceRefreshButtons();
}

function voiceStartRecognition() {
  if (VOICE.busy || VOICE.stopping) return;
  if (!SpeechRecognitionCtor) {
    voiceSetStage('不支持识别', 'warn',
                  '当前浏览器不支持语音识别，请继续使用文字输入。');
    voiceLog('识别不可用', '缺少 SpeechRecognition 或 webkitSpeechRecognition');
    return;
  }
  if (!window.isSecureContext) {
    voiceSetStage('需要 HTTPS', 'warn',
                  '当前页面不是安全上下文。请在本机使用 localhost，或通过 HTTPS 访问后再录音。');
    voiceLog('识别不可用', '页面不是 secure context');
    return;
  }
  if (VOICE.listening) {
    voiceStopRecognition(false);
    return;
  }

  VOICE.captureRequestId = VOICE.captureRequestId || voiceNewRequestId('speech');
  VOICE.requestId = VOICE.captureRequestId;
  if ($.voiceRequestId) {
    $.voiceRequestId.textContent = shortRid(VOICE.requestId);
    $.voiceRequestId.title = VOICE.requestId;
  }
  VOICE.transcript = '';
  VOICE.interimLogged = false;
  VOICE.recognitionError = '';
  VOICE.recognitionToken++;
  const token = VOICE.recognitionToken;
  const rec = new SpeechRecognitionCtor();
  VOICE.recognition = rec;
  rec.lang = 'zh-CN';
  rec.interimResults = true;
  rec.continuous = false;
  rec.maxAlternatives = 1;

  rec.onstart = () => {
    if (token !== VOICE.recognitionToken) return;
    VOICE.listening = true;
    voiceSetStage('录音 / 识别中', 'warn',
                  '正在使用浏览器麦克风录音并识别，再点一次按钮可停止。');
    voiceLog('录音开始', '音频源=浏览器麦克风；识别=Web Speech API',
             VOICE.captureRequestId);
    voiceRefreshButtons();
  };

  rec.onresult = (event) => {
    if (token !== VOICE.recognitionToken) return;
    let finalText = '';
    let interimText = '';
    for (let i = event.resultIndex; i < event.results.length; i++) {
      const part = event.results[i];
      if (part.isFinal) finalText += part[0].transcript;
      else interimText += part[0].transcript;
    }
    if (finalText) {
      VOICE.transcript += finalText;
      voiceLog('识别结果', finalText.trim(), VOICE.captureRequestId);
    } else if (interimText && !VOICE.interimLogged) {
      VOICE.interimLogged = true;
      voiceLog('识别中间结果', interimText.trim(), VOICE.captureRequestId);
    }
    if ($.voiceInput) {
      $.voiceInput.value = (VOICE.transcript + interimText).trim();
    }
    voiceSetStage('识别中', 'warn',
                  interimText ? '识别中：' + interimText.trim()
                              : '已识别，等待录音结束。');
    voiceRefreshButtons();
  };

  rec.onerror = (event) => {
    if (token !== VOICE.recognitionToken) return;
    VOICE.listening = false;
    VOICE.recognitionError = event.error || 'unknown';
    const message = event.error === 'not-allowed'
      ? '麦克风权限被拒绝，请在浏览器地址栏允许麦克风后重试。'
      : '语音识别失败（' + (event.error || 'unknown') + '），可改用文字输入。';
    voiceSetStage('识别异常', 'err', message);
    voiceLog('识别异常', event.error || 'unknown', VOICE.captureRequestId);
    voiceRefreshButtons();
  };

  rec.onend = () => {
    if (token !== VOICE.recognitionToken) return;
    VOICE.listening = false;
    VOICE.recognition = null;
    if (VOICE.recognitionError) {
      voiceRefreshButtons();
      return;
    }
    const text = ($.voiceInput && $.voiceInput.value || VOICE.transcript).trim();
    if (text) {
      voiceSetStage('识别完成', 'ok',
                    '识别文字已填入输入框；确认无误后点击“发送任务”。');
      voiceLog('录音结束', '识别文字已进入输入框', VOICE.captureRequestId);
    } else {
      voiceSetStage('空闲', 'off',
                    '没有识别到文字，请重试录音或直接输入文字。');
      voiceLog('录音结束', '未得到识别文字', VOICE.captureRequestId);
    }
    voiceRefreshButtons();
  };

  try {
    rec.start();
  } catch (e) {
    VOICE.listening = false;
    voiceSetStage('识别异常', 'err', '无法启动麦克风：' + e.message);
    voiceLog('识别异常', e.message, VOICE.captureRequestId);
    voiceRefreshButtons();
  }
}

async function voiceCancelOnServer(requestId) {
  if (!requestId) return { cancelled: false, missing: true };
  let res;
  try {
    const options = {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ request_id: requestId }),
      cache: 'no-store',
      keepalive: true,
    };
    if (window.AbortSignal && AbortSignal.timeout) {
      options.signal = AbortSignal.timeout(3000);
    }
    res = await fetch('/api/v1/agent/cancel', options);
  } catch (e) {
    throw new Error('取消请求未送达：' + e.message);
  }
  let data = null;
  try { data = await res.json(); } catch (e) { data = null; }
  if (!res.ok) {
    throw new Error((data && data.error) || ('取消接口返回 ' + res.status));
  }
  return data || { cancelled: false };
}

async function voiceStopCurrent(reason) {
  if (VOICE.stopping) return;
  VOICE.stopping = true;
  VOICE.cancellationWarning = '';
  const requestId = VOICE.requestId;
  const taskActive = VOICE.busy || VOICE.listening;
  const speechActive = !!(window.speechSynthesis &&
                          window.speechSynthesis.speaking);
  VOICE.generation++;
  VOICE.busy = false;
  voiceStopRecognition(true);
  voiceStopSpeech(true);
  if (VOICE.abortController) {
    try { VOICE.abortController.abort(); } catch (e) { /* 忽略 */ }
    VOICE.abortController = null;
  }
  voiceSetOptions([]);
  if (taskActive) {
    voiceLog('取消中', reason || '用户停止当前任务', requestId);
    voiceSetStage('正在取消', 'warn', '界面已停止；正在等待服务端确认取消…');
  } else if (speechActive) {
    voiceLog('播报停止', reason || '用户停止播报', requestId);
    voiceAddBubble('system', '已停止播报。');
  }
  let cancellationNote = '';
  if (taskActive && requestId) {
    try {
      const data = await voiceCancelOnServer(requestId);
      if (data.cancelled) {
        voiceLog('取消确认', '服务端已取消等待并处理设备任务', requestId);
        voiceAddBubble('system',
          '服务端已确认取消，迟到的结果不会再覆盖当前界面。');
        cancellationNote = '服务端已确认取消，已回到空闲。';
      } else {
        voiceLog('取消未确认', '请求可能已完成或已不存在', requestId);
        voiceAddBubble('system',
          '界面已停止；该请求可能已经完成，因此服务端没有可取消的任务。');
        cancellationNote = '界面已停止；请求可能已经完成。';
      }
    } catch (e) {
      voiceLog('取消异常', e.message, requestId);
      voiceAddBubble('system',
        '界面已停止，但服务端取消未确认；设备任务可能仍在执行。');
      cancellationNote = '界面已停止；服务端取消未确认，设备任务可能继续。';
      VOICE.cancellationWarning = cancellationNote;
    }
  }
  VOICE.firstFeedbackMs = null;
  VOICE.agentResponseMs = null;
  VOICE.startedAt = 0;
  VOICE.stopping = false;
  voiceSetLatency();
  voiceSetStage('空闲', 'off',
                cancellationNote || '已回到空闲，可重新输入或录音。');
  voiceRefreshButtons();
}

async function voiceClearAssistant() {
  await voiceStopCurrent('清空并重新输入');
  const cancellationWarning = VOICE.cancellationWarning;
  VOICE.generation++;
  VOICE.requestId = '';
  VOICE.taskRequestId = '';
  VOICE.sessionId = '';
  VOICE.captureRequestId = '';
  VOICE.transcript = '';
  VOICE.logs = [];
  VOICE.pendingOptions = [];
  voicePersistSession();
  if ($.voiceInput) $.voiceInput.value = '';
  if ($.voiceThread) {
    $.voiceThread.innerHTML = '';
    voiceAddBubble('system',
      '会话已清空。可以输入文字，或点击“开始录音”发起一次新任务。');
    if (cancellationWarning) {
      voiceAddBubble('system', cancellationWarning);
    }
  }
  if ($.voiceRequestId) $.voiceRequestId.textContent = '—';
  if ($.voiceTaskRid) $.voiceTaskRid.textContent = '—';
  if ($.voiceLatency) $.voiceLatency.textContent = '—';
  if ($.voiceSpeechState) $.voiceSpeechState.textContent = '空闲';
  voiceSetOptions([]);
  voiceRenderLog();
  voiceSetStage('空闲', cancellationWarning ? 'warn' : 'off',
                cancellationWarning ||
                '会话已清空，等待新的文字或语音输入。');
  VOICE.cancellationWarning = '';
  voiceRefreshButtons();
}

async function voiceSubmit(text, options) {
  options = options || {};
  const message = String(text || '').trim();
  if (!message) {
    voiceSetStage('等待输入', 'warn', '请输入任务文字，或先点击“开始录音”。');
    if ($.voiceInput) $.voiceInput.focus();
    return;
  }
  if (VOICE.busy) {
    voiceSetStage('任务进行中', 'warn',
                  '当前任务仍在执行。先点“停止当前任务”，再输入下一条。');
    return;
  }
  if (VOICE.stopping) {
    voiceSetStage('正在取消', 'warn', '请等待服务端取消结果后再发送下一条任务。');
    return;
  }
  if (options.clarify && !VOICE.sessionId) {
    voiceSetStage('澄清上下文失效', 'err', '没有待澄清会话，请重新输入任务。');
    return;
  }

  voiceStopSpeech(true);
  if (VOICE.listening) voiceStopRecognition(false);
  const requestId = VOICE.captureRequestId || voiceNewRequestId('voice');
  VOICE.captureRequestId = '';
  VOICE.requestId = requestId;
  VOICE.taskRequestId = '';
  VOICE.generation++;
  const generation = VOICE.generation;
  const controller = new AbortController();
  VOICE.abortController = controller;
  VOICE.busy = true;
  VOICE.startedAt = performance.now();
  VOICE.firstFeedbackMs = null;
  VOICE.abortController = controller;
  voiceSetOptions([]);
  if ($.voiceRequestId) {
    $.voiceRequestId.textContent = shortRid(requestId);
    $.voiceRequestId.title = requestId;
  }
  if ($.voiceTaskRid) $.voiceTaskRid.textContent = '—';
  voiceAddBubble('user', message);
  voiceLog('输入完成', (options.inputSource || '文字输入') +
           ' · ' + message, requestId);
  voiceSetStage('任务执行中', 'warn',
                '已收到输入，正在解析意图并等待任务结果…');
  VOICE.firstFeedbackMs = voiceElapsedMs();
  voiceLog('首次反馈', '界面进入“任务执行中”', requestId);
  voiceSetLatency();
  voiceRefreshButtons();

  const endpoint = options.clarify ? '/api/v1/agent/clarify' : '/api/v1/agent/chat';
  const body = options.clarify
    ? { session_id: VOICE.sessionId, choice: message, request_id: requestId }
    : { message: message, session_id: VOICE.sessionId,
        device_id: state.current || '', request_id: requestId };

  try {
    let res;
    try {
      res = await fetch(endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
        cache: 'no-store',
        signal: controller.signal,
      });
    } catch (e) {
      if (e.name === 'AbortError') throw e;
      throw new Error('无法连接 Agent 服务：' + e.message);
    }
    let data = null;
    try { data = await res.json(); } catch (e) { data = null; }
    if (generation !== VOICE.generation) return;
    if (!res.ok) {
      throw new Error((data && data.error) || ('接口返回 ' + res.status));
    }
    if (!data) throw new Error('Agent 响应不是合法 JSON');
    VOICE.agentResponseMs = voiceElapsedMs();

    if (data.session_id) {
      VOICE.sessionId = data.session_id;
      voicePersistSession();
    }
    if (data.request_id && data.request_id !== requestId) {
      voiceLog('请求号异常', '响应 request_id=' + data.request_id, requestId);
    }

    const status = data.status || 'failure';
    const reply = data.message || 'Agent 未返回消息。';
    const task = data.data && data.data.task ? data.data.task : null;
    const taskRequestId = (task && task.request_id) ||
                          (data.data && data.data.task_request_id) || '';
    if (taskRequestId) {
      VOICE.taskRequestId = taskRequestId;
      if ($.voiceTaskRid) {
        $.voiceTaskRid.textContent = shortRid(taskRequestId);
        $.voiceTaskRid.title = taskRequestId;
      }
    }

    if (data.type === 'clarification' || status === 'ambiguous') {
      voiceSetStage('需要澄清', 'warn', '请选择一项，或重新输入更明确的任务。');
      voiceSetOptions(data.options || []);
    } else if (status === 'cancelled') {
      voiceSetStage('已取消', 'off', reply);
    } else if (status === 'success') {
      voiceSetStage('已完成', 'ok', reply);
    } else if (status === 'no_evidence') {
      voiceSetStage('等待设备证据', 'warn', reply);
    } else if (status === 'not_found') {
      voiceSetStage('未找到数据', 'warn', reply);
    } else {
      voiceSetStage('执行失败', 'err', reply);
    }
    voiceAddBubble('assistant', reply);
    voiceLog('任务响应', status + (data.elapsed_ms !== undefined
      ? ' · 服务端 ' + data.elapsed_ms + ' ms' : ''), requestId);
    voiceSetLatency();
    if ($.voiceAutoSpeak && $.voiceAutoSpeak.checked) voiceSpeak(reply);
  } catch (e) {
    if (generation !== VOICE.generation || e.name === 'AbortError') return;
    VOICE.agentResponseMs = voiceElapsedMs();
    const reply = '请求失败：' + e.message;
    voiceSetStage('执行失败', 'err', reply);
    voiceAddBubble('assistant', reply);
    voiceLog('请求异常', e.message, requestId);
  } finally {
    if (generation === VOICE.generation) {
      VOICE.busy = false;
      VOICE.abortController = null;
      voiceSetLatency();
      voiceRefreshButtons();
    }
  }
}

function initVoiceAssistant() {
  if (!$.voiceInput) return;
  const ttsSupported = !!(window.speechSynthesis &&
                          window.SpeechSynthesisUtterance);
  if ($.voiceRecognizer) {
    $.voiceRecognizer.textContent = SpeechRecognitionCtor
      ? 'Web Speech（浏览器）' : '不支持，使用文字';
  }
  if ($.voiceSupport) {
    $.voiceSupport.textContent =
      '语音识别：' + (SpeechRecognitionCtor ? '可用（zh-CN）' : '当前浏览器不支持') +
      '；语音播报：' + (ttsSupported ? '可用' : '当前浏览器不支持') +
      '。首次录音时浏览器会请求麦克风权限。' +
      (window.isSecureContext
        ? ' 当前页面满足安全上下文要求。'
        : ' 当前页面不是安全上下文；请使用 localhost 或 HTTPS。');
  }
  if (!ttsSupported && $.voiceAutoSpeak) $.voiceAutoSpeak.checked = false;

  $.voiceRecordBtn.addEventListener('click', voiceStartRecognition);
  $.voiceSendBtn.addEventListener('click', () => {
    voiceSubmit($.voiceInput.value, { inputSource: '文字输入' });
  });
  $.voiceStopBtn.addEventListener('click', () => {
    void voiceStopCurrent('用户停止');
  });
  $.voiceClearBtn.addEventListener('click', () => {
    void voiceClearAssistant();
  });
  $.voiceSpeakStopBtn.addEventListener('click', () => voiceStopSpeech(false));
  $.voiceInput.addEventListener('input', voiceRefreshButtons);
  $.voiceInput.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter' && (ev.ctrlKey || ev.metaKey)) {
      ev.preventDefault();
      voiceSubmit($.voiceInput.value, { inputSource: '文字输入' });
    }
  });
  window.addEventListener('beforeunload', () => {
    if (VOICE.requestId && navigator.sendBeacon) {
      const payload = new Blob(
        [JSON.stringify({ request_id: VOICE.requestId })],
        { type: 'application/json' });
      const queued = navigator.sendBeacon('/api/v1/agent/cancel', payload);
      if (!queued) void voiceCancelOnServer(VOICE.requestId);
    } else if (VOICE.requestId) {
      void voiceCancelOnServer(VOICE.requestId);
    }
    voiceStopRecognition(true);
    voiceStopSpeech(true);
  });
  voiceRenderLog();
  voiceSetStage('空闲', 'off', '可以输入文字，或点击“开始录音”');
}

initVoiceAssistant();

/* ------------------------------------------------------------------ *
 *  功能分区（同一页面内切换，避免 9 张卡片竖着堆成一长条）
 *
 *  6 个分区按钮 ↔ 6 个 .tab-panel：按钮 data-tab="x" 对应面板 id="panel-x"。
 *  所有卡片/控件的元素 id 都没改，隐藏只是给面板加/去 .active；DOM 一直在，
 *  所以 800 ms 主轮询照常把数据写进"看不见"的卡片，切过去看到的就是最新的。
 *
 *  两个必须注意的点：
 *   1) canvas 在 display:none 的父节点里 clientWidth = 0，fitCanvas 会退回 900px
 *      兜底宽度 —— 所以切回「实时数据」时要按真实尺寸重画一次（refreshTab 做）。
 *   2) 顶层 const/let（LIVE / SCENE / TABS 等）在脚本求值阶段处于 TDZ，本模块的
 *      同步代码只碰 DOM；所有刷新调用都发生在初始化之后或事件回调里。
 * ------------------------------------------------------------------ */

const TABS = ['data', 'history', 'voice', 'task', 'control', 'camera', 'event'];
const TAB_KEY = 'imu_web_tab';    // localStorage：记住上次停留的分区，下次打开还停在那页
let activeTab = 'data';

/** 切换分区。opts.silent 不写 hash；opts.noRefresh 不补刷数据（首屏用，避免重复打接口） */
function switchTab(id, opts) {
  opts = opts || {};
  if (TABS.indexOf(id) < 0) id = 'data';
  activeTab = id;

  TABS.forEach((t) => {
    const panel = el('panel-' + t);
    if (panel) panel.classList.toggle('active', t === id);
    const btn = el('tab-' + t);
    if (btn) {
      btn.setAttribute('aria-selected', t === id ? 'true' : 'false');
      btn.tabIndex = (t === id) ? 0 : -1;     // 只有当前分区留在 Tab 键序列里
    }
  });

  if (!opts.silent) {
    try { window.location.hash = 'tab=' + id; } catch (e) { /* 某些环境忽略 */ }
  }
  try { window.localStorage.setItem(TAB_KEY, id); } catch (e) { /* 隐私模式忽略 */ }
  if (!opts.noRefresh) refreshTab(id);
}

/** 切过去时补刷一次：数据不用等下一轮 800 ms 轮询；canvas 按真实尺寸重画 */
function refreshTab(id) {
  if (id === 'data') {
    drawChart(state.lastPoints || []);
    drawScene3D(state.lastPoints || []);
  } else if (id === 'history') {
    refreshHistory();          // 采集历史列表（新批次 / 换设备 / 换过滤条件）
  } else if (id === 'voice') {
    voiceRefreshButtons();     // 输入框、录音、停止按钮按当前状态重新启用
  } else if (id === 'task') {
    refreshLatestTask();       // 任务卡片 + 任务历史表（同一次 /api/v1/tasks 请求）
  } else if (id === 'control') {
    refreshControl();          // device_control 真相源
    renderCompare();           // 手动批次 vs 周期批次
  } else if (id === 'camera') {
    refreshLive();             // 直播最新一帧
    refreshGallery();          // 照片画廊
  } else if (id === 'event') {
    refreshEvents(true);       // 闭环事件卡片 + 流水表
  }
}

/** #tab=camera 这类深链（也便于自动化自检直达某个分区；只认已知分区名） */
function tabFromHash() {
  try {
    const m = /(?:^#|&)tab=([a-z]+)/.exec(window.location.hash || '');
    if (m && TABS.indexOf(m[1]) >= 0) return m[1];
  } catch (e) { /* 忽略 */ }
  return '';
}

// 分区条：点击切换；左右方向键在分区之间移动（WAI-ARIA tabs 的惯例操作）
document.querySelectorAll('#tabNav .tab').forEach((btn) => {
  btn.addEventListener('click', () => switchTab(btn.dataset.tab));
  btn.addEventListener('keydown', (ev) => {
    const step = ev.key === 'ArrowRight' ? 1 : (ev.key === 'ArrowLeft' ? -1 : 0);
    if (!step) return;
    ev.preventDefault();
    const next = TABS[(TABS.indexOf(btn.dataset.tab) + step + TABS.length) % TABS.length];
    switchTab(next);
    const nextBtn = el('tab-' + next);
    if (nextBtn) nextBtn.focus();
  });
});

// 浏览器前进 / 后退（只变 hash，不会重新加载页面）
window.addEventListener('hashchange', () => {
  const t = tabFromHash();
  if (t && t !== activeTab) switchTab(t);
});

/** ?upload=<批号>：直达某一批历史数据的详情（可分享的链接，也便于自动化自检） */
function uploadFromQuery() {
  try {
    const v = new URLSearchParams(window.location.search).get('upload') || '';
    return /^\d+$/.test(v) ? v : '';
  } catch (e) {
    return '';
  }
}

// 首屏分区优先级：URL hash（#tab=camera）> ?upload=（历史详情）> 上次停留的分区 > 默认「实时数据」。
// noRefresh：首屏数据由下面的 poll() / refreshSidePanels() 统一拉取，别重复打接口。
(function initTabs() {
  const autoUpload = uploadFromQuery();
  let t = tabFromHash();
  if (!t && autoUpload) t = 'history';
  if (!t) {
    try {
      const saved = window.localStorage.getItem(TAB_KEY);
      if (saved && TABS.indexOf(saved) >= 0) t = saved;
    } catch (e) { /* 忽略 */ }
  }
  switchTab(t || 'data', { silent: true, noRefresh: true });
  // 等首屏设备列表落地再打开详情（列表要按设备过滤，晚了才不会先闪一次「全部设备」）
  if (autoUpload) setTimeout(() => openUploadDetail(autoUpload), 800);
})();

