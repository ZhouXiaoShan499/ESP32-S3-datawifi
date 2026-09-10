/*
 * ESP32-S3 IMU 实时监控面板
 *
 * 定时轮询查询接口：
 *   GET /api/v1/devices              -> 设备下拉
 *   GET /api/v1/latest?device_id=..  -> 选中设备的最新数据
 * 能力：设备下拉选择、最新数据展示、无数据/无设备提示、更新状态（更新中/未更新）。
 */

'use strict';

const POLL_MS = 2000;          // 轮询周期
const STALE_S = 30;            // 超过该秒数视为「未更新」（与服务端 / 保持一致）
const NUM = 5;                 // 展示头部/尾部样本条数

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
};

const state = { busy: false, devices: [], current: '' };

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

// 立即轮询一次，再周期轮询
poll();
setInterval(poll, POLL_MS);


