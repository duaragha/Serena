import MarkdownIt from './vendor/markdown-it/markdown-it.mjs';

const markdown = new MarkdownIt({html:false, linkify:true, breaks:false});
const defaultValidation = markdown.validateLink.bind(markdown);
markdown.validateLink = href => {
  if (!defaultValidation(href)) return false;
  try { return ['http:', 'https:', 'mailto:'].includes(new URL(href).protocol); }
  catch { return false; }
};
markdown.renderer.rules.link_open = (tokens, index, options, env, renderer) => {
  tokens[index].attrSet('target', '_blank');
  tokens[index].attrSet('rel', 'noopener noreferrer');
  return renderer.renderToken(tokens, index, options);
};
// Provider text must not silently contact image hosts or local endpoints.
markdown.renderer.rules.image = (tokens, index) => markdown.utils.escapeHtml(
  `[Image: ${tokens[index].content || 'attachment'}]`
);

export function renderWorkspaceMarkdown(text) {
  return markdown.render(String(text || ''));
}
