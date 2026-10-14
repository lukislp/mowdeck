// Stylelint config of the web frontend: standard rules plus `mowdeck/use-tokens` (raw colours,
// font stacks and radii only in libs/mowdeck-ui/src/lib/styles/tokens.css).
import useTokens from './libs/mowdeck-ui/lint/stylelint-tokens.js';

export default {
  extends: ['stylelint-config-standard', 'stylelint-config-html/svelte'],
  plugins: [useTokens],
  rules: {
    'mowdeck/use-tokens': true,
    'selector-pseudo-class-no-unknown': [true, { ignorePseudoClasses: ['global'] }],
    'custom-property-pattern': null,
    'selector-class-pattern': null,
    'keyframes-name-pattern': null,
    'no-descending-specificity': null,
    'declaration-empty-line-before': null,
    'declaration-block-no-redundant-longhand-properties': [
      true,
      { ignoreShorthands: ['grid-template'] },
    ],
    'media-feature-range-notation': 'prefix',
    'custom-property-empty-line-before': null,
    'import-notation': 'string',
    'length-zero-no-unit': [true, { ignore: ['custom-properties'] }],
  },
};
