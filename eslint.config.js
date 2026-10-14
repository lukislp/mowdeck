// ESLint flat config of the web frontend workspace (apps/web, apps/mobile, libs/mowdeck-ui,
// tests/e2e-web, tools/ts-codegen). Prettier owns formatting; this config owns correctness and the
// i18n rule.
import js from '@eslint/js';
import prettier from 'eslint-config-prettier';
import svelte from 'eslint-plugin-svelte';
import globals from 'globals';
import ts from 'typescript-eslint';

import mowdeck from './libs/mowdeck-ui/lint/eslint-no-literal-text.js';

export default ts.config(
  {
    ignores: [
      '**/node_modules/',
      '**/.svelte-kit/',
      '**/build/',
      '**/coverage/',
      '**/playwright-report/',
      '**/test-results/',
      'apps/web/src/lib/generated/',
      'apps/web/src/lib/paraglide/',
      'apps/mobile/ios/',
      'apps/mobile/android/',
      'apps/mobile/www/',
      '.venv/',
      'site/',
      'services/',
      'libs/mowdeck-contracts/',
      'docs/',
    ],
  },
  js.configs.recommended,
  ...ts.configs.recommended,
  ...svelte.configs.recommended,
  prettier,
  ...svelte.configs.prettier,
  {
    languageOptions: { globals: { ...globals.browser, ...globals.node } },
    rules: {
      '@typescript-eslint/no-unused-vars': ['error', { argsIgnorePattern: '^_' }],
      'svelte/no-navigation-without-resolve': 'off',
      // Private bookkeeping (ref counts, caches, listeners) is non-reactive on purpose; state
      // that the UI renders uses SvelteMap/SvelteSet explicitly.
      'svelte/prefer-svelte-reactivity': 'off',
    },
  },
  {
    files: ['**/*.svelte', '**/*.svelte.ts'],
    languageOptions: { parserOptions: { parser: ts.parser } },
    // TypeScript checks undefined names (DOM lib types such as AutoFill included).
    rules: { 'no-undef': 'off' },
  },
  {
    // UI texts of the app come from the Paraglide messages only.
    files: ['apps/web/src/**/*.svelte'],
    plugins: { mowdeck },
    rules: { 'mowdeck/no-literal-text': 'error' },
  },
);
