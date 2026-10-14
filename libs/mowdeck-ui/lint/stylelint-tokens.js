// Stylelint rule `mowdeck/use-tokens`: raw colours, font stacks and radii belong in tokens.css.
// Everywhere else styles reference the design tokens (`var(--md-...)`), so no default theme can
// leak in and both themes stay consistent.
import stylelint from 'stylelint';

const {
  createPlugin,
  utils: { report, ruleMessages, validateOptions },
} = stylelint;

export const ruleName = 'mowdeck/use-tokens';

const messages = ruleMessages(ruleName, {
  colour: (value) =>
    `Use a colour token (var(--md-color-*), var(--md-state-*)) instead of "${value}"`,
  font: (value) => `Use var(--md-font-sans) or var(--md-font-mono) instead of "${value}"`,
  radius: (value) => `Use a radius token (var(--md-radius-*)) instead of "${value}"`,
});

const COLOUR = /#[0-9a-f]{3,8}\b|\b(?:rgba?|hsla?|hwb|lab|lch|oklab|oklch|color)\(/i;
const NAMED =
  /(?:^|[\s,(])(?:red|blue|green|white|black|gr[ae]y|yellow|orange|purple|pink|navy|teal)(?:$|[\s,)])/i;
const RADIUS = /radius$/i;

const rule = (enabled) => (root, result) => {
  if (!validateOptions(result, ruleName, { actual: enabled })) return;
  const file = root.source?.input.file ?? '';
  if (file.replaceAll('\\', '/').endsWith('/styles/tokens.css')) return;
  root.walkDecls((decl) => {
    const value = decl.value;
    if (COLOUR.test(value) || NAMED.test(value)) {
      report({ message: messages.colour(value), node: decl, result, ruleName });
    }
    if (/^font(-family)?$/i.test(decl.prop) && !/^(var\(|inherit|unset|initial)/.test(value)) {
      report({ message: messages.font(value), node: decl, result, ruleName });
    }
    if (RADIUS.test(decl.prop) && !/^(var\(|0$|inherit)/.test(value)) {
      report({ message: messages.radius(value), node: decl, result, ruleName });
    }
  });
};

rule.ruleName = ruleName;
rule.messages = messages;

export default createPlugin(ruleName, rule);
