/* Existing hosted WX APIs, backed by serialized asynchronous macOS RPC. */
(function () {
  'use strict';
  if (window.top !== window || window.__macAssistantBridge) return;
  window.__macAssistantBridge = true;
  var pending = Object.create(null), rawBackend = null, channel = null;
  var waiters = [], connecting = false, connected = false, sequence = 0;
  var methods = [
    'get_wechat_friend', 'check_wechat_unread_message', 'prepare_assist_reply',
    'send_assist_reply', 'start_auto_reply_task', 'start_assist_reply_task',
    'get_wechat_window_message', 'send_message', 'send_wechat_message',
    'get_chat_message', 'read_messages', 'check_unread', 'auto_reply',
    'remember_customer_name', 'log_customer_timing', 'stop', 'stop_reply_task',
    'stop_auto_reply_task', 'stop_assist_reply_task', 'status', 'get_status',
    'permissions', 'save_assist_reply_to_knowledge', 'send_file',
    'send_wechat_moments', 'get_moments_stats', 'add_wechat_friend'
  ];
  function stringify(value) { return typeof value === 'string' ? value : JSON.stringify(value == null ? {} : value); }
  function fail(message) { return JSON.stringify({code: 0, msg: message, data: {}}); }
  function emit(type, detail) { window.dispatchEvent(new CustomEvent(type, {detail: detail})); }
  function complete(id, result) {
    var entry = pending[id];
    if (!entry) return;
    delete pending[id]; clearTimeout(entry.timer);
    entry.resolve(stringify(result));
  }
  function call(method, argument, callback) {
    var promise = new Promise(function (resolve) {
      if (!rawBackend) { resolve(fail('本地后端尚未连接')); return; }
      var id = 'mac-' + Date.now() + '-' + (++sequence);
      pending[id] = {resolve: resolve, timer: setTimeout(function () {
        complete(id, {code: 0, msg: '本地任务等待超时，请停止后重试', data: {reason: 'rpc_timeout'}});
      }, 180000)};
      try {
        rawBackend.call(id, method, argument == null ? '' : stringify(argument), function (ack) {
          try { ack = typeof ack === 'string' ? JSON.parse(ack) : ack; } catch (_) { ack = null; }
          if (!ack || ack.code !== 1) complete(id, ack || {code: 0, msg: '本地调用失败', data: {}});
        });
      } catch (_) { complete(id, {code: 0, msg: '本地调用失败', data: {}}); }
    });
    if (typeof callback === 'function') promise.then(callback);
    return promise;
  }
  function proxyFor(raw) {
    if (window.backend && window.backend.__macProxy) return window.backend;
    rawBackend = raw;
    var proxy = {__macProxy: true};
    methods.forEach(function (method) {
      proxy[method] = function (argument, callback) {
        if (typeof argument === 'function') { callback = argument; argument = undefined; }
        return call(method, argument, callback);
      };
    });
    ['reply_event', 'customer_contact_changed', 'stream_chunk'].forEach(function (name) { proxy[name] = raw[name]; });
    raw.rpc_result.connect(function (payload) {
      try {
        var envelope = typeof payload === 'string' ? JSON.parse(payload) : payload;
        complete(envelope.request_id, envelope.result);
      } catch (_) {}
    });
    raw.reply_event.connect(function (payload) { emit('wechat-assistant-reply-event', payload); });
    raw.customer_contact_changed.connect(function (name) {
      emit('wechat-assistant-contact-changed', {name: name});
    });
    return proxy;
  }
  function channelReady(actual) {
    channel = actual; connecting = false; connected = true;
    var raw = actual.objects.mac_backend || actual.objects.backend;
    if (!raw) return;
    actual.objects.backend = proxyFor(raw);
    window.backend = actual.objects.backend;
    var callbacks = waiters.splice(0);
    callbacks.forEach(function (callback) { if (callback) callback(actual); });
    emit('wechat-assistant-bridge-ready', true);
  }
  var nativeConstructor = window.QWebChannel;
  function sharedChannel(transport, callback, converters) {
    if (connected) { if (callback) callback(channel); return channel; }
    if (callback) waiters.push(callback);
    if (connecting) return channel;
    if (typeof nativeConstructor !== 'function') return null;
    connecting = true;
    try { return new nativeConstructor(transport, channelReady, converters); }
    catch (error) { connecting = false; throw error; }
  }
  // A hosted page may load qwebchannel.js again. Keep a single transport owner
  // and apply the final-result proxy to every page-created channel callback.
  Object.defineProperty(window, 'QWebChannel', {
    configurable: true,
    get: function () { return sharedChannel; },
    set: function (value) { if (value !== sharedChannel) nativeConstructor = value; }
  });
  function connect() {
    if (!connected && !connecting && window.qt && qt.webChannelTransport && typeof nativeConstructor === 'function') {
      sharedChannel(qt.webChannelTransport, function () {});
    }
  }
  connect();
  var retries = 0;
  var timer = setInterval(function () { connect(); if (connected || ++retries >= 50) clearInterval(timer); }, 100);
  window.wechatAssistantBridgeReady = function () { return connected && !!rawBackend; };
  window.wechatAssistantBridge = function (method, argument, success) {
    if (methods.indexOf(method) < 0) { if (success) success(fail('不支持的本地方法')); return; }
    return call(method, argument, success);
  };
  window.getIsWechatTip = function () { return true; };
  window.getWechatFriend = function (callback) { return call('get_wechat_friend', '', callback); };
  window.prepareAssistReply = function (data, callback) { return call('prepare_assist_reply', data || '{}', callback); };
  window.sendAssistReply = function (data, callback) { return call('send_assist_reply', data || '{}', callback); };
  window.checkWechatUnreadMessage = function (callback) { return call('check_wechat_unread_message', '', callback); };
  window.getWechatUserMessage = window.checkWechatUnreadMessage;
  var currentSession = '';
  window.getWeChatWindowMessage = function (data, callback) {
    return call('prepare_assist_reply', data || '{}').then(function (raw) {
      var result;
      try { result = JSON.parse(raw); } catch (_) { result = {code: 0, msg: '本地结果无效'}; }
      if (result.code === 1 && result.data) {
        currentSession = result.data.session_id || '';
        result.data = JSON.stringify([{question: result.data.question || '', answer: result.data.answer || '',
          session_id: currentSession, contact_name: result.data.contact_name || ''}]);
      }
      var value = JSON.stringify(result); if (callback) callback(value); return value;
    });
  };
  window.sendMessage = function (text, callback) {
    if (!currentSession) { var result = fail('请先获取回复，确认联系人后再发送'); if (callback) callback(result); return Promise.resolve(result); }
    return call('send_assist_reply', {session_id: currentSession, answer: text}).then(function (value) {
      try { if (JSON.parse(value).code === 1) currentSession = ''; } catch (_) {}
      if (callback) callback(value); return value;
    });
  };
})();
