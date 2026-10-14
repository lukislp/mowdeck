// ESLint rule `mowdeck/no-literal-text`: user-visible text in Svelte markup must come from the
// Paraglide messages (`m.some_key()`), never be written into a component. Reports text nodes
// and the attributes aria-label, title, placeholder and alt when they contain letters.
// Units and symbols (mm, m/s, %, km/h) are allowed.

const ATTRIBUTES = new Set(['aria-label', 'title', 'placeholder', 'alt']);
const ALLOWED = new Set(['mm', 'm/s', '%', 'km/h', 'V', 'A']);
const LETTER = /\p{L}/u;

function hasText(value) {
  const text = value.trim();
  return text.length > 0 && LETTER.test(text) && !ALLOWED.has(text);
}

/** @type {import('eslint').Rule.RuleModule} */
const noLiteralText = {
  meta: {
    type: 'problem',
    docs: { description: 'UI text comes from the Paraglide messages, not from markup' },
    messages: {
      text: 'Literal text "{{text}}" in markup: add a message to messages/de.json and en.json.',
      attribute: 'Literal {{name}}="{{text}}": use a message instead.',
    },
    schema: [],
  },
  create(context) {
    return {
      SvelteText(node) {
        const parent = node.parent?.type;
        if (parent === 'SvelteStyleElement' || parent === 'SvelteScriptElement') return;
        if (hasText(node.value)) {
          context.report({ node, messageId: 'text', data: { text: node.value.trim() } });
        }
      },
      SvelteAttribute(node) {
        if (node.key.type !== 'SvelteName' || !ATTRIBUTES.has(node.key.name)) return;
        for (const part of node.value) {
          if (part.type === 'SvelteLiteral' && hasText(part.value)) {
            context.report({
              node: part,
              messageId: 'attribute',
              data: { name: node.key.name, text: part.value.trim() },
            });
          }
        }
      },
    };
  },
};

export default { rules: { 'no-literal-text': noLiteralText } };
