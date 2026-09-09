import test from 'node:test';
import assert from 'node:assert/strict';
import {renderWorkspaceMarkdown} from '../ui/static/workspace-markdown.mjs';

test('formats markdown without admitting HTML or executable links', () => {
  const html = renderWorkspaceMarkdown('## Heading\n\n**bold**\n\n<script>alert(1)</script>\n\n[bad](javascript:alert(1))\n\n[local](file:///etc/passwd)');
  assert.match(html, /<h2>Heading<\/h2>/);
  assert.match(html, /<strong>bold<\/strong>/);
  assert.doesNotMatch(html, /<script|href="(?:javascript|file):/);
  assert.match(html, /&lt;script&gt;/);
});

test('links are explicit external navigation and images never fetch', () => {
  const html = renderWorkspaceMarkdown('[docs](https://example.com) ![remote](https://example.com/track.png)');
  assert.match(html, /href="https:\/\/example.com"/);
  assert.match(html, /rel="noopener noreferrer"/);
  assert.match(html, /target="_blank"/);
  assert.doesNotMatch(html, /<img/);
  assert.match(html, /\[Image: remote\]/);
});
