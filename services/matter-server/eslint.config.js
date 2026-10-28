// ESLint flat config of matter-server, the one TypeScript service. The workspace config
// (../../eslint.config.js) leaves services/ to the Python tooling, so this package brings its own
// with the same rules for plain TypeScript. Prettier owns formatting.
import js from '@eslint/js';
import prettier from 'eslint-config-prettier';
import globals from 'globals';
import ts from 'typescript-eslint';

export default ts.config(
  { ignores: ['node_modules/'] },
  js.configs.recommended,
  ...ts.configs.recommended,
  prettier,
  {
    languageOptions: { globals: { ...globals.node } },
    rules: {
      '@typescript-eslint/no-unused-vars': ['error', { argsIgnorePattern: '^_' }],
    },
  },
);
