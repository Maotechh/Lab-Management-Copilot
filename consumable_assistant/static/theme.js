/* The upstream DSH theme switches its semantic tokens with this body attribute. */
(() => {
  const system = matchMedia('(prefers-color-scheme: dark)');
  window.applyLabTheme = function(preference = localStorage.getItem('lab-theme') || 'system') {
    const dark = preference === 'dark' || (preference === 'system' && system.matches);
    document.body.toggleAttribute('data-ds-dark-theme', dark);
    document.documentElement.style.colorScheme = dark ? 'dark' : 'light';
    document.querySelector('meta[name="theme-color"]').content = dark ? '#151517' : '#ffffff';
    localStorage.setItem('lab-theme', preference);
  };
  system.addEventListener('change', () => applyLabTheme());
  applyLabTheme();
})();
