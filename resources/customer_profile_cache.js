/* Names-only history and live customer queries. Customer details are never cached. */
(function () {
  'use strict';
  if (window.top !== window || window.__customerProfileCacheInstalled) return;
  if (!/\/wxassistant\/mobile\//.test(location.pathname)) return;
  window.__customerProfileCacheInstalled = true;
  window.__customerProfileCacheStatus = {version: 5, mode: 'names-only', nameCount: 0, installed: true, applied: false,
    layout: 'pending', hits: 0, misses: 0, saves: 0, queries: 0, errors: 0, lastError: ''};

  function csContactName(value) {
    return String(value || '').trim().replace(/[?？\uFFFD\s]+$/u, '').trim();
  }
  function csTimingNow() {
    return typeof performance !== 'undefined' && typeof performance.now === 'function' ? performance.now() : Date.now();
  }
  function csTimingFlush() {
    clearTimeout(csTimingRetryTimer);
    csTimingRetryTimer = null;
    const backend = window.backend || (window.top && window.top.backend);
    if (backend && typeof backend.log_customer_timing === 'function') {
      while (csTimingQueue.length) {
        try {
          backend.log_customer_timing(JSON.stringify(csTimingQueue[0]), function () {});
          csTimingQueue.shift();
        } catch (_) { break; }
      }
    }
    // Retry only a bounded burst. Later events and bridge-ready events resume delivery.
    if (csTimingQueue.length && csTimingRetryCount++ < 30) csTimingRetryTimer = setTimeout(csTimingFlush, 200);
    if (!csTimingQueue.length) csTimingRetryCount = 0;
  }
  function csTimingEmit(timing, stage, duration, ok, extra) {
    if (!timing) return;
    const payload = {request_id: timing.request_id, generation: timing.generation, source: timing.source,
      stage: stage, duration_ms: Math.round(Math.max(0, Number(duration) || 0) * 100) / 100,
      ok: !!ok, stale: timing.generation !== csGeneration};
    if (timing.name) payload.name = timing.name;
    // Only route identifiers and readiness metadata are allowed; never params or API data.
    ['hash', 'alias', 'display_method', 'recognition_id'].forEach(function (key) {
      if (extra && extra[key] != null) payload[key] = String(extra[key]).slice(0, 96);
    });
    if (csTimingQueue.length >= 64) csTimingQueue.shift();
    csTimingQueue.push(payload);
    if (!csTimingRetryTimer) { csTimingRetryCount = 0; csTimingFlush(); }
  }
  function csTimingStart(generation, source, name) {
    const timing = {request_id: csTimingPageId + '-' + generation, generation: generation, source: source,
      name: name, started: csTimingNow(), pending: new Set(), failed: false, body: false,
      background: false, backgroundDone: false, backgroundTimer: null};
    csCurrentTiming = timing;
    csTimingEmit(timing, 'request_start', 0, true);
    return timing;
  }
  function csCaptureFriendTiming(timing, response) {
    const id = response && response.timing && response.timing.recognition_id;
    if (timing && (typeof id === 'string' || typeof id === 'number')) timing.recognition_id = String(id).slice(0, 96);
  }
  function csTimingBackground(timing) {
    if (!timing || !timing.body || !timing.background || timing.backgroundDone || timing.backgroundTimer) return;
    // Wait through promise continuations, including a list read following its column read.
    timing.backgroundTimer = setTimeout(function () {
      timing.backgroundTimer = null;
      if (timing.pending.size || timing.backgroundDone) return;
      timing.backgroundDone = true;
      csTimingEmit(timing, 'background_complete', csTimingNow() - timing.started, !timing.failed);
    }, 0);
  }
  function csTimingBodyReady(timing, ok) {
    if (!timing || timing.body) return;
    timing.body = true;
    timing.failed = timing.failed || !ok;
    csTimingEmit(timing, 'body_ready', csTimingNow() - timing.started, ok);
    let displayDone = false, fallback = null;
    const display = function (method) {
      if (displayDone) return;
      displayDone = true; clearTimeout(fallback);
      csTimingEmit(timing, 'display_ready', csTimingNow() - timing.started,
        ok && timing.generation === csGeneration, {display_method: method});
    };
    Promise.resolve(typeof Vue.nextTick === 'function' ? Vue.nextTick() : null).then(function () {
      if (typeof window.requestAnimationFrame === 'function') {
        fallback = setTimeout(function () { display('frame-timeout'); }, 250);
        window.requestAnimationFrame(function () {
          window.requestAnimationFrame(function () { display('two-animation-frames'); });
        });
      } else fallback = setTimeout(function () { display('render-flush'); }, 0);
    }).catch(function () { display('render-flush-error'); });
    csTimingBackground(timing);
  }
  function csCacheRefs() {
    return {}; // Live page refs are not historical customer-detail snapshots.
  }
  function csCacheReport(kind, error) {
    const status = window.__customerProfileCacheStatus;
    if (!status) return;
    status[kind] = Number(status[kind] || 0) + 1;
    if (error) status.lastError = String(error.message || error).slice(0, 160);
  }
  function csCacheRead() {
    return csCacheDocument || (csCacheDocument = {version: 2, names: []});
  }
  function csCacheWrite() {
    const status = window.__customerProfileCacheStatus;
    if (status) status.nameCount = csCacheRead().names.length;
  }
  function csCacheDelete(name) {
    // Explicit detail refresh does not expire the permanent name history.
  }
  function csCacheClear() {
    // Remove old detail snapshots from previous versions, not the names file.
    try {
      const keys = [];
      for (let i = 0; i < sessionStorage.length; i++) keys.push(sessionStorage.key(i));
      keys.forEach(function (key) {
        if (key && key.indexOf('aiwechat:customer-profile:') === 0) sessionStorage.removeItem(key);
      });
    } catch (_) {}
  }
  function csCacheSave() {
    const name = String(csFriend.value || '').trim();
    if (!name) return;
    if (csState.value !== 'ready' && csState.value !== 'unbound') return;
    try {
      const document = csCacheRead();
      const names = [name];
      if (csBound.value && typeof csBound.value.name === 'string' && csBound.value.name.trim()) {
        names.push(csBound.value.name.trim());
      }
      const backend = window.backend || (window.top && window.top.backend);
      names.forEach(function (value) {
        if (document.names.indexOf(value) >= 0 || csPendingNameWrites.has(value)) return;
        if (backend && typeof backend.remember_customer_name === 'function') {
          const timing = csCurrentTiming, started = csTimingNow();
          csPendingNameWrites.add(value);
          backend.remember_customer_name(value, function (raw) {
            csPendingNameWrites.delete(value);
            let result = raw;
            try { if (typeof raw === 'string') result = JSON.parse(raw); } catch (_) {}
            csTimingEmit(timing, 'name_persist', csTimingNow() - started, !!result && result.code == 1);
            if (!result || result.code != 1) csCacheReport('errors', '名称写入本地文件失败');
            else {
              if (document.names.indexOf(value) < 0) document.names.push(value);
              csCacheWrite();
            }
          });
        }
      });
      csCacheWrite();
      csCacheReport('saves');
    } catch (error) { csCacheReport('errors', error); }
  }
  function csCacheRestore(name) {
    csCacheReport('misses');
    return false;
  }
  function csReadApi(hash, params, method) {
    return csReadApiAt(csGeneration, csCurrentTiming, hash, params, method);
  }
  function csReadApiAt(generation, timing, hash, params, method) {
    // Coalesce only simultaneous reads in this live query, never another visit.
    const key = String(generation) + '|' + String(hash) + '|' + String(method || 'POST') + '|' + JSON.stringify(params);
    if (csPendingReads.has(key)) return csPendingReads.get(key);
    const started = csTimingNow();
    const task = Promise.resolve().then(function () { return apiPost(hash, params, method); });
    csPendingReads.set(key, task);
    if (timing) timing.pending.add(task);
    const finish = function (ok) {
      if (csPendingReads.get(key) === task) csPendingReads.delete(key);
      if (timing) { timing.pending.delete(task); timing.failed = timing.failed || !ok; }
      const extra = {hash: hash, alias: params && params.alias};
      csTimingEmit(timing, 'api_read', csTimingNow() - started, ok, extra);
      if (hash === API.hash.csBound) csTimingEmit(timing, 'binding_query', csTimingNow() - started, ok, extra);
      csTimingBackground(timing);
    };
    task.then(function () { finish(true); }, function () { finish(false); });
    return task;
  }
  function csResumeCachedLoads(generation) {
    const pending = csCachePending;
    csCachePending = null;
    const cid = csBound.value && Number(csBound.value.id);
    if (!pending || csState.value !== 'ready' || !cid) return;
    const tasks = [];
    if (pending.notes) tasks.push(loadNotes(cid));
    if (csCacheLayout === 'dynamic-menus') {
      if (pending.counts) tasks.push(loadCdCounts(cid));
      (pending.subLists || []).forEach(function (alias) {
        const item = cdMenuList.value.find(function (menu) { return menu && menu.alias === alias; });
        if (item) tasks.push(loadSubList(item, cid));
      });
      if (pending.opLog) tasks.push(loadOpLog(cid));
    } else {
      if (pending.counts) tasks.push(loadCounts(cid, generation));
      if (pending.contracts) tasks.push(loadContracts());
      if (pending.payments) tasks.push(loadPayments());
    }
    Promise.allSettled(tasks).then(function () {
      if (generation === csGeneration) csCacheSave();
    });
  }
  function csClearPreviousCustomer() {
    csCacheFetchedAt = 0;
    csCountsLoading = false;
    csCachePending = null;
    csBound.value = null;
    csFriendRec.value = csMenuId.value = csTabId.value = 0;
    csDetailFields.value = []; csEditFields.value = [];
    notes.value = []; notesTotal.value = 0;
    if (csCacheLayout === 'dynamic-menus') {
      cdMenuList.value = [];
      Object.keys(subMap).forEach(function (key) { delete subMap[key]; });
      opLogs.value = []; opLogErr.value = ''; opLogLoaded.value = opLogLoading.value = false;
    } else {
      contracts.value = []; payments.value = [];
      contractCols.value = []; paymentCols.value = [];
      contractTotal.value = paymentTotal.value = 0;
      contractLoading.value = paymentLoading.value = false;
      contractErr.value = paymentErr.value = '';
    }
    lastSummaryTime.value = '';
    notesLoading.value = false;
    cdEditing.value = ''; editingNoteId.value = null;
  }
  function csLoad(options) {
    options = options || {};
    const force = options === true || options.force === true ||
      (typeof options.type === 'string' && 'isTrusted' in Object(options));
    if (force) csPendingReads.clear();
    const name = csContactName(options.name);
    if (!force && csLoadRunning && (!name || name === csTargetName)) return csLoadRunning;
    csCacheSave();
    const generation = ++csGeneration;
    const source = force ? 'manual-refresh' : ['init', 'contact-change', 'profile-entry'].indexOf(options.reason) >= 0 ? options.reason : 'direct';
    const timing = csTimingStart(generation, source, name);
    csTargetName = name;
    if (name && !force && csCacheRestore(name)) {
      csLoadRunning = null;
      csResumeCachedLoads(generation);
      return Promise.resolve();
    }
    const task = csLoadOnce(generation, name, force, timing);
    csLoadRunning = task;
    task.finally(function () {
      if (generation === csGeneration) {
        csLoadRunning = null;
        csCacheSave();
      }
    });
    return task;
  }
  async function csLoadOnce(generation, requestedName, force, timing) {
    try {
      // Keep the current view while checking identity. Never clear a known customer first.
      let friend;
      if (requestedName) friend = {name: requestedName};
      else {
        const started = csTimingNow();
        try {
          friend = await csReadFriend();
          timing.name = csContactName(friend && friend.name);
          csTimingEmit(timing, 'friend_lookup', csTimingNow() - started, true, {recognition_id: timing.recognition_id});
        } catch (error) {
          csTimingEmit(timing, 'friend_lookup', csTimingNow() - started, false, {recognition_id: timing.recognition_id});
          throw error;
        }
      }
      if (generation !== csGeneration) return;
      const name = csContactName(friend && friend.name);
      if (!name) throw new Error('没读到当前联系人，请先把微信停在与好友的聊天界面');
      csTargetName = name;
      if (!force && csCacheRestore(name)) { csResumeCachedLoads(generation); return; }
      if (force) csCacheDelete(name);
      csClearPreviousCustomer();
      csFriend.value = name;
      csState.value = 'loading';
      csError.value = '';
      csCacheReport('queries');
      const result = await csReadApiAt(generation, timing, API.hash.csBound, {friend_name: name});
      if (generation !== csGeneration) return;
      csFriendRec.value = Number(result.wxchatfriend_id || 0);
      csMenuId.value = Number(result.menu_id || 0);
      csTabId.value = Number(result.tab_id || 0);
      const cid = Number(result.freekehuguanli_id || 0);
      if (cid > 0) await csShowDetail(cid, generation, timing);
      else {
        csState.value = 'unbound'; timing.background = true;
        csTimingBodyReady(timing, true);
      }
      if (generation === csGeneration && (csState.value === 'ready' || csState.value === 'unbound')) csCacheFetchedAt = Date.now();
    } catch (error) {
      if (generation !== csGeneration) return;
      csState.value = 'error';
      csError.value = (error && error.message) || '读取失败';
      timing.background = true;
      csTimingBodyReady(timing, false);
    }
  }
  async function csShowDetail(cid, generation, timing) {
    generation = typeof generation === 'number' ? generation : csGeneration;
    timing = timing || csCurrentTiming;
    if (generation !== csGeneration) return;
    csBound.value = {id: Number(cid), name: '', phone: '', mobile: '', code: '', company: '', weixin: ''};
    cdTab.value = csCacheLayout === 'dynamic-menus' ? '' : 'note';
    aiNoteDone.value = false;
    if (csCacheLayout === 'dynamic-menus') {
      cdMenuList.value = [];
      Object.keys(subMap).forEach(function (key) { delete subMap[key]; });
      opLogs.value = []; opLogErr.value = ''; opLogLoaded.value = opLogLoading.value = false;
    } else {
      contracts.value = []; payments.value = [];
      contractTotal.value = paymentTotal.value = 0;
      contractErr.value = paymentErr.value = '';
      contractLoading.value = paymentLoading.value = false;
    }
    csDetailFields.value = []; csEditFields.value = []; cdEditing.value = '';
    csState.value = 'loading';
    try { await loadCustomerFields(cid, generation, timing); }
    catch (error) {
      if (generation !== csGeneration) return;
      csState.value = 'error'; csError.value = (error && error.message) || '客户资料加载失败';
      if (timing) timing.background = true;
      csTimingBodyReady(timing, false);
      return;
    }
    if (generation !== csGeneration) return;
    if (csCacheLayout === 'dynamic-menus') {
      const first = cdMenuList.value[0];
      cdTab.value = first ? cdMenuKey(first, 0) : 'note';
    }
    csCacheFetchedAt = Date.now();
    csState.value = 'ready';
    if (timing) timing.background = true;
    csTimingBodyReady(timing, true);
    if (csCacheLayout === 'dynamic-menus') {
      if (cdKind.value !== 'note') cdLoadCurrent();
      await Promise.all([loadNotes(cid), loadCdCounts(cid)]);
    } else {
      await Promise.all([loadNotes(cid, generation), loadCounts(cid, generation, timing)]);
    }
  }
  async function loadCustomerFields(cid, generation, timing) {
    generation = typeof generation === 'number' ? generation : csGeneration;
    timing = timing || csCurrentTiming;
    const params = {aid: cid, alias: FREE_ALIAS.customer, menu_id: csMenuId.value, tab_id: csTabId.value};
    const results = await Promise.allSettled([
      csReadApiAt(generation, timing, API.hash.csDetailCfg, params), csReadApiAt(generation, timing, API.hash.freeEdit, params, 'GET'),
    ]);
    if (generation !== csGeneration) return;
    const detail = results[0].status === 'fulfilled' ? results[0].value : null;
    const edit = results[1].status === 'fulfilled' ? results[1].value : null;
    const fields = detail && Array.isArray(detail.from_fields) ? detail.from_fields : [];
    if (!fields.length && !(results[1].status === 'fulfilled' && Array.isArray(edit))) throw new Error('客户资料不存在或无权查看');
    csDetailFields.value = fields.filter(function (field) { return Number(field.is_detail_draw) !== 0; })
      .map(function (field) { return {name: field.name, title: field.title, type: field.type, value: field.value, extra: field.extra || []}; });
    csEditFields.value = (Array.isArray(edit) ? edit : []).map(function (field) {
      return {name: field.name, title: field.title, type: field.type, value: field.value, readonly: Number(field.readonly) === 1, extra: field.extra || []};
    });
    if (csCacheLayout === 'dynamic-menus') applyCdMenus(detail && detail.pm_list);
    fillBoundFromFields();
  }
  async function loadCounts(cid, generation, timing) {
    generation = typeof generation === 'number' ? generation : csGeneration;
    timing = timing || csCurrentTiming;
    if (!cid) return;
    csCountsLoading = true;
    const values = await Promise.allSettled([
      csReadApiAt(generation, timing, API.hash.freeList, {alias: FREE_ALIAS.contract, page: 1, list_rows: 1, freekehuguanli_id: cid}),
      csReadApiAt(generation, timing, API.hash.freeList, {alias: FREE_ALIAS.payment, page: 1, list_rows: 1, freekehuguanli_id: cid}),
    ]);
    if (generation !== csGeneration) return;
    csCountsLoading = false;
    contractTotal.value = values[0].status === 'fulfilled' ? Number(values[0].value.total || 0) : 0;
    paymentTotal.value = values[1].status === 'fulfilled' ? Number(values[1].value.total || 0) : 0;
  }
  function csInit(reason) {
    if (WX && WX.ready()) { csLoad({reason: reason || 'init'}); return; }
    if (WX) WX.onReady(function () { csLoad({reason: reason || 'init'}); });
    clearTimeout(csInitTimer);
    csInitTimer = setTimeout(function () {
      if (!(WX && WX.ready()) && (csState.value === 'idle' || csState.value === 'loading')) {
        csState.value = 'error'; csError.value = '没接上微信助手，无法读取当前微信好友';
      }
    }, 3000);
  }
  function goTab(key) {
    navMenu.value = false; sheet.value = '';
    const target = TAB_HREF[key];
    if (!target) return;
    csCacheSave();
    page.value = 'main'; tab.value = key;
    const url = location.pathname.replace(/[^/]*$/, '') + target;
    if (location.pathname !== url) history.pushState({aiwechatTab: key}, '', url);
    if (key === 'profile') csInit('profile-entry');
    // Keep the other lists' existing refresh behavior without destroying the customer view.
    if (key === 'kb') loadKb();
    if (key === 'moments') loadMoments();
  }
  function csHandleNavigation(event) {
    if (event.defaultPrevented || (typeof event.button === 'number' && event.button !== 0) ||
        event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    const target = event.target && (event.target.closest ? event.target : event.target.parentElement);
    const link = target && target.closest && target.closest('a.tab[href], a.nav-menu-item[href]');
    if (!link || link.hasAttribute('download') ||
        (link.target && link.target !== '_self')) return;
    let url;
    try { url = new URL(link.getAttribute('href'), location.href); } catch (_) { return; }
    if (url.origin !== location.origin || url.search || url.hash) return;
    const directory = location.pathname.replace(/[^/]*$/, '');
    const key = Object.keys(TAB_HREF).find(function (item) { return directory + TAB_HREF[item] === url.pathname; });
    if (!key) return;
    event.preventDefault();
    goTab(key);
  }

  function functionRange(source, name) {
    const expression = new RegExp('(?:^|\\n)\\s*(?:async\\s+)?function\\s+' + name + '\\s*\\(');
    const match = expression.exec(source);
    if (!match) throw new Error('missing function: ' + name);
    const start = match.index + (source[match.index] === '\n' ? 1 : 0);
    const begin = source.indexOf('{', match.index + match[0].length);
    let depth = 0, quote = '', line = false, block = false;
    for (let i = begin; i < source.length; i++) {
      const ch = source[i], next = source[i + 1];
      if (line) { if (ch === '\n') line = false; continue; }
      if (block) { if (ch === '*' && next === '/') { block = false; i++; } continue; }
      if (quote) { if (ch === '\\') i++; else if (ch === quote) quote = ''; continue; }
      if (ch === '/' && next === '/') { line = true; i++; continue; }
      if (ch === '/' && next === '*') { block = true; i++; continue; }
      if (ch === '"' || ch === "'" || ch === '`') { quote = ch; continue; }
      if (ch === '{') depth++;
      if (ch === '}' && --depth === 0) return {start, end: i + 1};
    }
    throw new Error('unbalanced function: ' + name);
  }
  function replaceFunction(source, name, fn) {
    const range = functionRange(source, name);
    return source.slice(0, range.start) + '    ' + fn.toString() + source.slice(range.end);
  }
  function hasFunction(source, name) {
    try { functionRange(source, name); return true; } catch (_) { return false; }
  }
  function guardAsync(source, name) {
    const range = functionRange(source, name);
    let body = source.slice(range.start, range.end);
    body = body.replace(/\bapiPost\s*\(/g, 'csReadApiAt(csEpoch, csTiming, ');
    body = body.replace('{', '{\n      const csEpoch = csGeneration, csTiming = csCurrentTiming;');
    // These known list helpers perform one list request. Guard every response write.
    const assignments = {loadNotes: 'notes.value', loadContracts: 'contracts.value',
      loadPayments: 'payments.value', loadSubList: 'st.rows', loadCdCounts: 'st.total',
      loadOpLog: 'opLogs.value'};
    const assignment = name === 'loadCols' ? (body.includes('st.cols =') ? 'st.cols' : 'target.value') : assignments[name];
    if (assignment) {
      const marker = new RegExp('(^[ \\t]*)(' + assignment.replace('.', '\\.') + '\\s*=)', 'gm');
      body = body.replace(marker, '$1if (csEpoch !== csGeneration) return;\n$1$2');
    }
    body = body.replace(/catch\s*\(\s*([A-Za-z_$][\w$]*)\s*\)\s*\{/g,
      'catch ($1) {\n        if (csEpoch !== csGeneration) return;');
    body = body.replace(/}\s*finally\s*\{/g, '} finally {\n        if (csEpoch !== csGeneration) return;');
    if (name === 'loadSubList') {
      body = body.replace(/await loadCols\(([^;\n]+)\);/g,
        'await loadCols($1);\n        if (csEpoch !== csGeneration) return;');
    }
    if (name === 'loadCdCounts') {
      body = body.replace('const csEpoch = csGeneration, csTiming = csCurrentTiming;',
        'const csEpoch = csGeneration, csTiming = csCurrentTiming;\n      csCountsLoading = true;\n      try {');
      const end = body.lastIndexOf('}');
      body = body.slice(0, end) + '\n      } finally { if (csEpoch === csGeneration) csCountsLoading = false; }\n' + body.slice(end);
    }
    return source.slice(0, range.start) + body + source.slice(range.end);
  }
  function captureFriendTiming(source) {
    if (!hasFunction(source, 'csReadFriend')) return source;
    const range = functionRange(source, 'csReadFriend');
    let body = source.slice(range.start, range.end);
    body = body.replace('{', '{\n      const csFriendTiming = csCurrentTiming;');
    const capture = '$1\n          csCaptureFriendTiming(csFriendTiming, $2);';
    body = body.replace(/(\bWX\.friend\s*\(\s*function\s*\(\s*[\w$]+\s*,\s*([\w$]+)\s*\)\s*\{)/g, capture);
    body = body.replace(/(\bWX\.friend\s*\(\s*\(\s*[\w$]+\s*,\s*([\w$]+)\s*\)\s*=>\s*\{)/g, capture);
    return source.slice(0, range.start) + body + source.slice(range.end);
  }
  function transform(source) {
    if (!source.includes('function csLoadOnce(') || !source.includes('function csShowDetail(')) return null;
    const layout = hasFunction(source, 'loadCdCounts') ? 'dynamic-menus' : 'legacy';
    if (layout === 'legacy' && !hasFunction(source, 'loadCounts')) throw new Error('unsupported customer list controller');
    const replacements = {csLoad, csLoadOnce, csShowDetail, loadCustomerFields, loadCounts, csInit, goTab};
    Object.keys(replacements).forEach(function (name) {
      if (name !== 'loadCounts' || hasFunction(source, name)) source = replaceFunction(source, name, replacements[name]);
    });
    ['loadNotes', 'loadContracts', 'loadPayments', 'loadCols', 'loadSubList', 'loadCdCounts', 'loadOpLog'].forEach(function (name) {
      if (hasFunction(source, name)) source = guardAsync(source, name);
    });
    source = captureFriendTiming(source);
    const marker = '    let csLoadRunning = null;';
    if (!source.includes(marker)) throw new Error('unsupported customer load controller');
    const helpers = [csContactName, csTimingNow, csTimingFlush, csTimingEmit, csTimingStart, csCaptureFriendTiming, csTimingBackground, csTimingBodyReady,
      csCacheRefs, csCacheReport, csCacheRead, csCacheWrite, csCacheDelete, csCacheClear,
      csCacheSave, csCacheRestore, csClearPreviousCustomer, csReadApi, csReadApiAt, csResumeCachedLoads,
      csHandleNavigation].map(function (fn) { return fn.toString(); }).join('\n');
    source = source.replace(marker, '    let csGeneration = 0, csTargetName = "", csCacheDocument = null, csCacheFetchedAt = 0;\n' +
      '    let csCountsLoading = false, csCachePending = null, csIdentityRead = null;\n' +
      '    let csCurrentTiming = null, csTimingRetryTimer = null, csTimingRetryCount = 0;\n' +
      '    const csTimingQueue = [], csTimingPageId = Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 8);\n' +
      '    const csCacheLayout = ' + JSON.stringify(layout) + ';\n' +
      '    const csPendingReads = new Map();\n' +
      '    const csPendingNameWrites = new Set();\n' +
      '    const csCacheStorageKey = "aiwechat:customer-profile:v1:" + String(ME.member_id || "") + ":" + String(ME.uid || "");\n' + helpers + '\n' + marker);
    const contactStart = source.indexOf('    // 收到微信联系人变化通知后只更新客户数据');
    const contactEnd = source.indexOf('    /* 首屏：', contactStart);
    if (contactStart < 0 || contactEnd < 0) throw new Error('unsupported contact subscription');
    source = source.slice(0, contactStart) + `
    window.addEventListener('wx-contact-changed', function (event) {
      const name = csContactName(event.detail && event.detail.name);
      if (name && name === csFriend.value && (csState.value === 'ready' || csState.value === 'unbound')) return;
      if (page.value === 'main' && tab.value === 'profile' && WX && WX.ready()) csLoad({name: name, reason: 'contact-change'});
      else { ++csGeneration; csTargetName = name; csLoadRunning = null; }
    });
` + source.slice(contactEnd);
    source = source.replace('function refresh() { location.reload(); }', 'function refresh() { csCacheClear(); location.reload(); }');
    source = source.replace('function doLogout() {', 'function doLogout() { csCacheClear();');
    source = source.replace("        csQuery.value = '';\n        await csShowDetail(cid);", "        csCacheDelete(csFriend.value);\n        csQuery.value = '';\n        await csShowDetail(cid);");
    // Only names are retained. Previous versions' customer-detail storage is removed.
    const initialize = '    /* ── 首屏把接口数据拉起来';
    if (!source.includes(initialize)) throw new Error('unsupported page initialization');
    source = source.replace(initialize, `
    csCacheClear();
    let csSnapshotTimer = null;
    Vue.watch(function () { return [csState.value, csFriend.value, csBound.value && csBound.value.name]; }, function () {
      clearTimeout(csSnapshotTimer); csSnapshotTimer = setTimeout(csCacheSave, 40);
    }, {deep: true});
    window.addEventListener('pagehide', csCacheSave);
    window.addEventListener('pywebviewready', csTimingFlush);
    if (typeof document !== 'undefined') {
      if (window.__customerProfileNavigationHandler) document.removeEventListener('click', window.__customerProfileNavigationHandler, true);
      window.__customerProfileNavigationHandler = csHandleNavigation;
      document.addEventListener('click', csHandleNavigation, true);
    }
    if (typeof performance !== 'undefined' && performance.navigation && performance.navigation.type === 1) csCacheClear();
    window.addEventListener('popstate', function () {
      const action = location.pathname.split('/').pop();
      const key = Object.keys(TAB_HREF).find(function (value) { return TAB_HREF[value] === action; });
      if (key) { page.value = 'main'; tab.value = key; if (key === 'profile') csInit('profile-entry'); }
    });
` + initialize);
    return source;
  }
  function install(vue) {
    if (!vue || !vue.createApp || vue.__customerProfileCachePatched) return;
    const create = vue.createApp;
    vue.createApp = function (options) {
      if (options && typeof options.setup === 'function') {
        try {
          const source = transform(options.setup.toString());
          if (source) {
            options.setup = Function('return ({' + source + '}).setup')();
            window.__customerProfileCacheApplied = true;
            window.__customerProfileCacheStatus.applied = true;
            window.__customerProfileCacheStatus.layout = hasFunction(source, 'loadCdCounts') ? 'dynamic-menus' : 'legacy';
            console.info('[CustomerProfileCache] loaded');
          }
        } catch (error) {
          window.__customerProfileCacheStatus.errors++;
          window.__customerProfileCacheStatus.lastError = String(error.message || error).slice(0, 160);
          console.warn('[CustomerProfileCache] unsupported page version: ' + error.message);
        }
      }
      return create.apply(this, arguments);
    };
    vue.__customerProfileCachePatched = true;
  }
  let current = window.Vue;
  try {
    Object.defineProperty(window, 'Vue', {
      configurable: true, enumerable: true,
      get: function () { return current; },
      set: function (value) { current = value; install(value); },
    });
    install(current);
  } catch (_) { install(current); }
  // Exposed only for deterministic offline regression checks; holds no customer data.
  window.__customerProfileCacheTransform = transform;
})();
