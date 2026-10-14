// Sets the theme before the first paint (external file, so the CSP needs no hash for it).
// Keep in step with applyTheme() in src/lib/stores/ui.svelte.ts.
(function () {
  try {
    var choice = localStorage.getItem('mowdeck.theme');
    if (choice === 'light' || choice === 'dark') document.documentElement.dataset.theme = choice;
    var locale = localStorage.getItem('PARAGLIDE_LOCALE');
    if (locale === 'en' || locale === 'de') document.documentElement.lang = locale;
  } catch {
    // storage blocked: follow the system
  }
})();
