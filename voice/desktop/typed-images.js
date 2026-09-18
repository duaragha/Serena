(function exposeTypedImageHelpers(root, factory) {
  const helpers = factory();
  if (typeof module === 'object' && module.exports) {
    module.exports = helpers;
  } else {
    root.SerenaTypedImages = helpers;
  }
})(typeof globalThis === 'object' ? globalThis : this, () => {
  'use strict';

  const MAX_IMAGE_BYTES = 5 * 1024 * 1024;
  const SUPPORTED_MEDIA_TYPES = new Set([
    'image/png',
    'image/jpeg',
    'image/gif',
    'image/webp',
  ]);

  function cleanMediaType(value) {
    const mediaType = String(value || '').trim().toLowerCase();
    return mediaType === 'image/jpg' ? 'image/jpeg' : mediaType;
  }

  function imageError(message) {
    return { ok: false, error: message };
  }

  function base64ByteLength(value) {
    const data = String(value || '');
    if (!data || data.length % 4 !== 0 || !/^[A-Za-z0-9+/]*={0,2}$/.test(data)) {
      return -1;
    }
    const padding = data.endsWith('==') ? 2 : data.endsWith('=') ? 1 : 0;
    return (data.length / 4) * 3 - padding;
  }

  function decodePrefix(data) {
    try {
      if (typeof Buffer !== 'undefined') {
        return Array.from(Buffer.from(data.slice(0, 32), 'base64'));
      }
      const decoded = atob(data.slice(0, 32));
      return Array.from(decoded, (character) => character.charCodeAt(0));
    } catch {
      return [];
    }
  }

  function detectedMediaType(data) {
    const bytes = decodePrefix(data);
    if (
      bytes.length >= 8
      && bytes.slice(0, 8).join(',') === '137,80,78,71,13,10,26,10'
    ) return 'image/png';
    if (bytes.length >= 3 && bytes[0] === 0xff && bytes[1] === 0xd8 && bytes[2] === 0xff) {
      return 'image/jpeg';
    }
    if (bytes.length >= 6) {
      const header = String.fromCharCode(...bytes.slice(0, 6));
      if (header === 'GIF87a' || header === 'GIF89a') return 'image/gif';
    }
    if (
      bytes.length >= 12
      && String.fromCharCode(...bytes.slice(0, 4)) === 'RIFF'
      && String.fromCharCode(...bytes.slice(8, 12)) === 'WEBP'
    ) return 'image/webp';
    return '';
  }

  function validateClipboardFile(file) {
    if (!file) return imageError('that clipboard image could not be read.');
    const mediaType = cleanMediaType(file.type);
    if (!SUPPORTED_MEDIA_TYPES.has(mediaType)) {
      return imageError('that image type is not supported. use png, jpeg, gif, or webp.');
    }
    const size = Number(file.size);
    if (!Number.isFinite(size) || size <= 0) {
      return imageError('that clipboard image is empty.');
    }
    if (size > MAX_IMAGE_BYTES) {
      return imageError('that image is too large. keep it under 5 MB.');
    }
    return { ok: true, mediaType };
  }

  function validateImage(image) {
    if (!image || typeof image !== 'object' || Array.isArray(image)) {
      return imageError('that clipboard image is invalid.');
    }
    const keys = Object.keys(image).sort().join(',');
    if (keys !== 'data,media_type') {
      return imageError('that clipboard image is invalid.');
    }
    const mediaType = cleanMediaType(image.media_type);
    if (!SUPPORTED_MEDIA_TYPES.has(mediaType)) {
      return imageError('that image type is not supported. use png, jpeg, gif, or webp.');
    }
    const data = typeof image.data === 'string' ? image.data : '';
    const size = base64ByteLength(data);
    if (size < 1) return imageError('that clipboard image is invalid.');
    if (size > MAX_IMAGE_BYTES) {
      return imageError('that image is too large. keep it under 5 MB.');
    }
    if (detectedMediaType(data) !== mediaType) {
      return imageError('that clipboard image does not match its image type.');
    }
    return { ok: true, image: { media_type: mediaType, data }, size };
  }

  function attachmentFromDataUrl(dataUrl, file) {
    const fileCheck = validateClipboardFile(file);
    if (!fileCheck.ok) return fileCheck;
    const match = /^data:([^;,]+);base64,([A-Za-z0-9+/]*={0,2})$/.exec(String(dataUrl || ''));
    if (!match) return imageError('that clipboard image could not be read.');
    return validateImage({ media_type: fileCheck.mediaType, data: match[2] });
  }

  function normaliseTypedPayload(value) {
    const source = typeof value === 'string' ? { text: value } : value;
    if (!source || typeof source !== 'object' || Array.isArray(source)) {
      return { ok: false, error: 'that message could not be sent.' };
    }
    const allowed = new Set(['text', 'image']);
    if (Object.keys(source).some((key) => !allowed.has(key))) {
      return { ok: false, error: 'that message could not be sent.' };
    }
    if (source.text != null && typeof source.text !== 'string') {
      return { ok: false, error: 'that message could not be sent.' };
    }
    const text = String(source.text || '').trim();
    if (text.length > 4000) {
      return { ok: false, error: 'that message is too long.' };
    }
    let image = null;
    if (source.image != null) {
      const checked = validateImage(source.image);
      if (!checked.ok) return checked;
      image = checked.image;
    }
    if (!text && !image) {
      return { ok: false, error: 'type a message or paste an image first.' };
    }
    return {
      ok: true,
      payload: image ? { text, image } : { text },
    };
  }

  return {
    MAX_IMAGE_BYTES,
    SUPPORTED_MEDIA_TYPES,
    attachmentFromDataUrl,
    normaliseTypedPayload,
    validateClipboardFile,
    validateImage,
  };
});
