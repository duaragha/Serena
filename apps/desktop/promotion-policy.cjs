'use strict';

const STABLE = /^v\d+\.\d+\.\d+$/;
const DEV = /^v\d+\.\d+\.\d+-dev\.\d+$/;
const SHA = /^[a-f0-9]{40}$/;
const REQUEST = /^[a-f0-9]{32}$/;

function catalogFeatures(catalog) {
  if (catalog?.schema !== 1 || !STABLE.test(catalog.initialStable) || !Array.isArray(catalog.features)
      || catalog.features.length > 100) throw new Error('Unsupported feature catalog');
  const seen = new Set();
  for (const f of catalog.features) {
    if (!/^[a-z][a-z0-9-]{0,63}$/.test(f.id) || seen.has(f.id) || !SHA.test(f.commit)
        || (f.base !== undefined && (!SHA.test(f.base) || f.base === f.commit))
        || !DEV.test(f.devTag) || typeof f.title !== 'string' || !f.title || f.title.length > 160
        || !Array.isArray(f.requires) || f.requires.some(id => !seen.has(id))
        || !Array.isArray(f.paths) || !f.paths.length
        || f.paths.some(p => typeof p !== 'string' || !/^[a-zA-Z0-9_./-]+$/.test(p)
          || p.startsWith('/') || p.split('/').some(s => s === '..' || s === '.') || p.startsWith('.git')))
      throw new Error('Invalid or unordered feature catalog');
    seen.add(f.id);
  }
  return catalog.features;
}

function selection(catalog, selected, tested, installed = []) {
  const features = catalogFeatures(catalog);
  const known = new Map(features.map(f => [f.id, f]));
  for (const ids of [selected, tested, installed]) {
    if (!Array.isArray(ids) || ids.length > features.length || new Set(ids).size !== ids.length
        || ids.some(id => !known.has(id))) throw new Error('Unknown or duplicate feature selection');
  }
  const chosen = new Set([...installed, ...selected]);
  const added = features.filter(f => chosen.has(f.id) && !installed.includes(f.id));
  if (!added.length) throw new Error('Select at least one feature not already in main');
  for (const f of features.filter(f => chosen.has(f.id))) {
    const missing = f.requires.filter(id => !chosen.has(id));
    if (missing.length) throw new Error(`${f.title} requires: ${missing.map(id => known.get(id).title).join(', ')}`);
    if (!installed.includes(f.id) && !tested.includes(f.id)) throw new Error(`Mark as tested: ${f.title}`);
  }
  return { added, all: features.filter(f => chosen.has(f.id)) };
}

function installedFeatures(catalog, tag, receipt) {
  catalogFeatures(catalog);
  if (!STABLE.test(tag)) throw new Error('Invalid stable release');
  if (tag === catalog.initialStable && !receipt) return [];
  if (receipt?.schema !== 1 || receipt.version !== tag || !Array.isArray(receipt.features))
    throw new Error('Main has no compatible promotion receipt; review its baseline before promoting');
  const known = new Map(catalog.features.map(f => [f.id, f]));
  const ids = receipt.features.map(f => {
    if (known.get(f.id)?.commit !== f.commit || known.get(f.id)?.base !== f.base)
      throw new Error('A previously shipped feature revision changed');
    return f.id;
  });
  if (new Set(ids).size !== ids.length) throw new Error('Duplicate shipped feature');
  return ids;
}

function nextVersion(tag) {
  if (!STABLE.test(tag)) throw new Error('Invalid stable version');
  const parts = tag.slice(1).split('.').map(Number);
  parts[2] += 1;
  return `v${parts.join('.')}`;
}

module.exports = { STABLE, DEV, SHA, REQUEST, catalogFeatures, selection, installedFeatures, nextVersion };
