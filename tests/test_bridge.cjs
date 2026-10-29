'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');
function signal() {
  const handlers = [];
  return {connect(fn) { handlers.push(fn); }, emit(value) { handlers.forEach(fn => fn(value)); }};
}
(async function () {
  const signals = {rpc_result: signal(), reply_event: signal(), customer_contact_changed: signal(), stream_chunk: signal()};
  const calls = [], dispatched = [];
  const raw = Object.assign({}, signals, {call(id, method, argument, ack) {
    calls.push({id, method, argument});
    ack(JSON.stringify({code: 1, data: {pending: true, request_id: id}}));
  }});
  let channelCount = 0;
  const initial = {objects: {backend: raw, mac_backend: raw}};
  function OriginalChannel(transport, callback) { channelCount++; callback(initial); }
  const window = {QWebChannel: OriginalChannel, qt: {webChannelTransport: {}},
    dispatchEvent(event) { dispatched.push(event); }};
  window.top = window;
  const context = {window, qt: window.qt, Promise, Date, JSON,
    CustomEvent: class {constructor(type, options) { this.type = type; this.detail = options.detail; }},
    setTimeout, clearTimeout, setInterval() { return 1; }, clearInterval() {}};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../resources/bridge.js'), 'utf8'), context);
  assert(window.wechatAssistantBridgeReady());
  let response = null;
  const task = window.backend.get_wechat_friend(value => { response = JSON.parse(value); });
  await Promise.resolve();
  assert.strictEqual(response, null, 'Queue acknowledgement cannot become a friend result');
  const call = calls.pop();
  assert.strictEqual(call.method, 'get_wechat_friend');
  signals.rpc_result.emit(JSON.stringify({request_id: 'unrelated', result: {code: 0}}));
  signals.rpc_result.emit(JSON.stringify({request_id: call.id,
    result: {code: 1, data: {name: '客户'}}}));
  assert.strictEqual(JSON.parse(await task).data.name, '客户');
  await Promise.resolve();
  assert.strictEqual(response.data.name, '客户');
  signals.reply_event.emit(JSON.stringify({type: 'assist_ready'}));
  assert(dispatched.some(event => event.type === 'wechat-assistant-reply-event'));
  // A second qwebchannel.js from the hosted site must keep the shared transport.
  window.QWebChannel = function Replacement() { throw new Error('unexpected second channel'); };
  let hostedBackend;
  new window.QWebChannel(window.qt.webChannelTransport, channel => { hostedBackend = channel.objects.backend; });
  assert.strictEqual(hostedBackend, window.backend);
  assert.strictEqual(channelCount, 1);
  const unsafe = JSON.parse(await window.sendMessage('没有session的裸文本'));
  assert.strictEqual(unsafe.code, 0);
  assert.strictEqual(calls.length, 0);
  console.log('PASS: final-result RPC, callback+Promise, request isolation, business events, shared channel, session guard');
})().catch(error => { console.error(error); process.exitCode = 1; });
