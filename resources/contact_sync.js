/* Same hosted contact-change contract as Windows. Never reload the main shell. */
(function () {
  'use strict';
  if (window.top !== window || window.__macContactSync) return;
  window.__macContactSync = true;
  var bound = null;
  function notify(name) {
    var detail = {name: String(name || '')};
    // The current mobile customer page listens directly to the native signal.
    // Legacy embedded customer documents need their existing lookup method.
    document.querySelectorAll('iframe').forEach(function (frame) {
      try {
        if (!frame.getBoundingClientRect().width) return;
        var doc = frame.contentDocument, root = doc && doc.querySelector('#app');
        var app = root && root.__vue_app__, state = app && app._instance && app._instance.setupState;
        if (state && typeof state.loadCustomerDetail === 'function') state.loadCustomerDetail();
        if (frame.contentWindow) frame.contentWindow.dispatchEvent(new CustomEvent('wechat-assistant-contact-changed', {detail: detail}));
      } catch (_) {}
    });
  }
  function bind() {
    var backend = window.backend;
    if (backend && backend !== bound && backend.customer_contact_changed && backend.customer_contact_changed.connect) {
      backend.customer_contact_changed.connect(notify); bound = backend;
    }
  }
  window.addEventListener('wechat-assistant-bridge-ready', bind);
  bind();
})();
