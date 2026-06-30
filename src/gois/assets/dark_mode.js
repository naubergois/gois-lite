  // Dark mode toggle with localStorage
  function qcmApplyTheme(t) {
    document.documentElement.setAttribute('data-theme', t);
    var btn = document.getElementById('qcm-theme-btn');
    if (btn) btn.textContent = t === 'dark' ? '\u{1F319}' : '\u{2600}\u{FE0F}';
    try { localStorage.setItem('qcm-theme', t); } catch(e) {}
  }
  window.qcmToggleTheme = function qcmToggleTheme() {
    var cur = document.documentElement.getAttribute('data-theme') || 'dark';
    qcmApplyTheme(cur === 'dark' ? 'light' : 'dark');
    window.qcmNavToast(cur === 'dark' ? 'Tema claro ativado' : 'Tema escuro ativado', 'info');
  };
  (function() {
    var saved = localStorage.getItem('qcm-theme');
    if (saved === 'dark' || saved === 'light') {
      qcmApplyTheme(saved);
    }
  })();
