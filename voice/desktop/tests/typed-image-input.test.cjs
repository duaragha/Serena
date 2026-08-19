const test = require('node:test');
const assert = require('node:assert/strict');

const {
  MAX_IMAGE_BYTES,
  attachmentFromDataUrl,
  normaliseTypedPayload,
  validateClipboardFile,
  validateImage,
} = require('../typed-images.js');

const PNG = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=',
  'base64',
);
const image = { media_type: 'image/png', data: PNG.toString('base64') };

test('text-only submissions keep their existing payload', () => {
  assert.deepEqual(normaliseTypedPayload('  ordinary pasted text  '), {
    ok: true,
    payload: { text: 'ordinary pasted text' },
  });
});

test('a clipboard png becomes a validated typed image', () => {
  const file = { type: 'image/png', size: PNG.length };
  const result = attachmentFromDataUrl(`data:image/png;base64,${image.data}`, file);
  assert.equal(result.ok, true);
  assert.deepEqual(result.image, image);
  assert.deepEqual(normaliseTypedPayload({ text: 'what is this?', image }), {
    ok: true,
    payload: { text: 'what is this?', image },
  });
  assert.equal(normaliseTypedPayload({ text: '', image }).ok, true);
});

test('unsupported, mismatched, and oversized clipboard images are readable errors', () => {
  assert.match(validateClipboardFile({ type: 'image/bmp', size: 20 }).error, /not supported/);
  assert.match(
    validateImage({ media_type: 'image/jpeg', data: image.data }).error,
    /does not match/,
  );
  assert.match(
    validateClipboardFile({ type: 'image/png', size: MAX_IMAGE_BYTES + 1 }).error,
    /under 5 MB/,
  );
});
