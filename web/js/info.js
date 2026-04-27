/**
 * info.js — Info card, colorbar, and feature selection.
 *
 * Replaces hover.js. Manages three display states:
 *   idle    — no feature targeted; shows dataset overview
 *   hover   — cursor over a feature; shows feature card (temporary)
 *   latched — user clicked a feature; card stays until clicked again or map clicked
 */

import { updateDensityScale } from './layers.js';

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

let _map = null;
let _getPhylopicIndex = null;
let _summary = null;          // dataset_summary JSON
let _densityRanges = null;    // per-resolution { p95, p50 }
let _state = 'idle';          // 'idle' | 'hover' | 'latched'
let _latchedId = null;        // string ID of the latched feature
let _expanded = false;        // whether the genus chart is expanded to 10 rows
let _currentCard = null;      // { props, layerId, type } for re-render on expand

// ---------------------------------------------------------------------------
// Marching-ants dash animation
// ---------------------------------------------------------------------------

// 10 frames shifting phase by 0.5 units per step through a [3 dash, 2 gap] pattern
const _DASH_SEQ = [
  [3, 2],
  [2.5, 2, 0.5],
  [2, 2, 1],
  [1.5, 2, 1.5],
  [1, 2, 2],
  [0.5, 2, 2.5],
  [0, 2, 3],
  [0, 1.5, 3, 0.5],
  [0, 1, 3, 1],
  [0, 0.5, 3, 1.5],
];
const _DASH_INTERVAL = 160; // ms per step
let _animFrame = null;
let _dashStep = 0;
let _lastDashTime = 0;

function _setDashArray(da) {
  if (!_map) return;
  for (const r of [6, 7, 8, 9]) {
    try { _map.setPaintProperty(`hexes_res${r}-selected`, 'line-dasharray', da); } catch (_) {}
  }
  for (const a of ['bezirke', 'ortsteile']) {
    try { _map.setPaintProperty(`admin_${a}-selected`, 'line-dasharray', da); } catch (_) {}
  }
}

function _animateDash(ts) {
  if (ts - _lastDashTime >= _DASH_INTERVAL) {
    _dashStep = (_dashStep + 1) % _DASH_SEQ.length;
    _lastDashTime = ts;
    _setDashArray(_DASH_SEQ[_dashStep]);
  }
  _animFrame = requestAnimationFrame(_animateDash);
}

function _startDashAnim() {
  if (_animFrame) return;
  _dashStep = 0;
  _lastDashTime = 0;
  _animFrame = requestAnimationFrame(_animateDash);
}

function _stopDashAnim() {
  if (_animFrame) { cancelAnimationFrame(_animFrame); _animFrame = null; }
  _setDashArray([3, 2]);
}

// ---------------------------------------------------------------------------
// Density scale helpers
// ---------------------------------------------------------------------------

const MODE_RANGE_KEY = {
  bezirke: 'bezirke', ortsteile: 'ortsteile',
  6: 'res6', 7: 'res7', 8: 'res8', 9: 'res9',
  auto: 'res7', trees: null,
};

export function updateColorbar(mode) {
  const key = MODE_RANGE_KEY[String(mode)] ?? null;
  const bar = document.getElementById('ctrl-colorbar');
  if (!bar) return;

  if (!key) {
    bar.style.display = 'none';
    return;
  }
  bar.style.display = '';

  const range = _densityRanges?.[key];
  const p95 = range?.p95 ?? 1000;

  if (_map) updateDensityScale(_map, p95);

  const ticks = bar.querySelectorAll('.cb-tick span');
  if (ticks.length >= 4) {
    ticks[0].textContent = '0';
    ticks[1].textContent = _fmt(Math.round(p95 * 0.10));
    ticks[2].textContent = _fmt(Math.round(p95 * 0.40));
    ticks[3].textContent = _fmt(p95) + '+';
  }
}

function _fmt(n) { return n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n); }

// ---------------------------------------------------------------------------
// Icon / letter fallback
// ---------------------------------------------------------------------------

function genusIcon(genus, phylopicIndex) {
  const lower = genus?.toLowerCase?.();
  if (lower && phylopicIndex?.[lower]) {
    return `<img class="bar-icon" src="public/icons/${lower}.svg" alt="${lower}">`;
  }
  // Letter fallback — plain HTML, no WebGL text rendering involved.
  const letter = genus ? genus[0].toUpperCase() : '?';
  return `<span class="bar-icon bar-icon-letter">${letter}</span>`;
}

// ---------------------------------------------------------------------------
// Card renderers
// ---------------------------------------------------------------------------

function _barRow(genus, share, count, phylopicIndex, isForest) {
  const pct = share != null ? Number(share).toFixed(1) : '?';
  const countStr = count != null ? `<span class="bar-count">${Number(count).toLocaleString()}</span>` : '';
  const fillClass = isForest ? 'bar-fill bar-fill-forest' : 'bar-fill';
  return `
    <div class="bar-row${isForest ? ' bar-forest' : ''}">
      <div class="bar-label">
        ${genusIcon(genus, phylopicIndex)}
        <span class="bar-name">${genus ?? '—'}</span>
      </div>
      <div class="bar-track"><div class="${fillClass}" style="width:${pct}%"></div></div>
      <div class="bar-pct">${pct}%${countStr}</div>
    </div>`;
}

function renderTreeChart(props, phylopicIndex, maxRows) {
  const rows = [];
  for (let i = 1; i <= maxRows; i++) {
    const genus = props[`tree_genus_${i}`];
    if (!genus) break;
    rows.push(_barRow(genus, props[`tree_genus_${i}_share`], props[`tree_genus_${i}_count`], phylopicIndex, false));
  }
  if (rows.length === 0) return '';

  // Determine "others" row content:
  // If collapsed (maxRows=5), others = genera 6-10 + stored other (genera 11+)
  // If expanded (maxRows=10), others = stored other (genera 11+)
  let otherShare, otherCount;
  if (maxRows < 10) {
    // Sum shares and counts for hidden rows (6..10) plus stored other
    let hiddenShare = 0;
    let hiddenCount = 0;
    for (let i = maxRows + 1; i <= 10; i++) {
      const genus = props[`tree_genus_${i}`];
      if (!genus) break;
      hiddenShare += Number(props[`tree_genus_${i}_share`] ?? 0);
      hiddenCount += Number(props[`tree_genus_${i}_count`] ?? 0);
    }
    otherShare = hiddenShare + Number(props.tree_genus_other_share ?? 0);
    otherCount = hiddenCount + Number(props.tree_genus_other_count ?? 0);
  } else {
    otherShare = Number(props.tree_genus_other_share ?? 0);
    otherCount = Number(props.tree_genus_other_count ?? 0);
  }

  const otherRow = (otherShare > 0.5)
    ? `<div class="bar-row bar-other">
        <div class="bar-label"><span class="bar-icon bar-icon-letter">…</span><span class="bar-name">other</span></div>
        <div class="bar-track"><div class="bar-fill" style="width:${otherShare}%"></div></div>
        <div class="bar-pct">${Number(otherShare).toFixed(1)}%<span class="bar-count">${otherCount.toLocaleString()}</span></div>
       </div>` : '';

  // Count how many extra genera exist beyond maxRows
  let extraCount = 0;
  for (let i = maxRows + 1; i <= 10; i++) {
    if (!props[`tree_genus_${i}`]) break;
    extraCount++;
  }

  return `<div class="card-section"><div class="card-section-title">Tree genera</div>${rows.join('')}${otherRow}</div>`;
}

function renderForestChart(props, phylopicIndex) {
  const fcp = props.forest_cover_pct;
  if (!fcp || fcp <= 0) return '';
  const rows = [];
  for (let i = 1; i <= 5; i++) {
    const genus = props[`forest_genus_${i}`];
    if (!genus) break;
    rows.push(_barRow(genus, props[`forest_genus_${i}_share`], null, phylopicIndex, true));
  }
  if (rows.length === 0) return '';
  const otherShare = props.forest_genus_other_share;
  const otherRow = (otherShare > 0.5)
    ? `<div class="bar-row bar-other bar-forest">
        <div class="bar-label"><span class="bar-icon bar-icon-letter">…</span><span class="bar-name">other</span></div>
        <div class="bar-track"><div class="bar-fill bar-fill-forest" style="width:${otherShare}%"></div></div>
        <div class="bar-pct">${Number(otherShare).toFixed(1)}%</div>
       </div>` : '';
  const fcpBadge = `<span class="fcp-badge">${Number(fcp).toFixed(0)}% of area</span>`;
  return `<div class="card-section card-forest"><div class="card-section-title">Forest composition ${fcpBadge}</div>${rows.join('')}${otherRow}</div>`;
}

function renderDatasetCard(summary, phylopicIndex) {
  if (!summary) return '<div class="card-loading">Loading…</div>';
  const genera = (summary.top_genera || []).map(g =>
    _barRow(g.genus, g.share, g.count, phylopicIndex, false)
  ).join('');
  const otherShare = summary.top_genera_other_share ?? 0;
  const otherCount = summary.top_genera_other_count ?? 0;
  const otherRow = (otherShare > 0.5)
    ? `<div class="bar-row bar-other">
        <div class="bar-label"><span class="bar-icon bar-icon-letter">…</span><span class="bar-name">other</span></div>
        <div class="bar-track"><div class="bar-fill" style="width:${otherShare}%"></div></div>
        <div class="bar-pct">${Number(otherShare).toFixed(1)}%<span class="bar-count">${otherCount.toLocaleString()}</span></div>
       </div>` : '';
  const genreSection = genera
    ? `<div class="card-section"><div class="card-section-title">Top genera</div>${genera}${otherRow}</div>` : '';
  return `
    <div class="card-title">Berlin Trees</div>
    <div class="card-stats">
      <span>${(summary.tree_count || 0).toLocaleString()} ${summary.tree_count === 1 ? 'tree' : 'trees'}</span>
      <span>${summary.genus_count || 0} genera</span>
      <span>${summary.berlin_area_km2 || 0} km² · ${summary.forest_cover_pct || 0}% forest</span>
    </div>
    ${genreSection}`;
}

function renderHexCard(props, layerType, phylopicIndex, expanded, latched) {
  const trees = Number(props.tree_count) || 0;
  const density = props.tree_density_km2 != null
    ? `${Number(props.tree_density_km2).toFixed(0)} trees/km²` : '';
  const stats = [
    trees ? `${trees.toLocaleString()} ${trees === 1 ? 'tree' : 'trees'}` : null,
    density || null,
    props.berlin_area_km2 ? `${Number(props.berlin_area_km2).toFixed(2)} km²` : null,
  ].filter(Boolean);

  // Count extra genera beyond 5 to label the expand button
  let extraCount = 0;
  for (let i = 6; i <= 10; i++) {
    if (!props[`tree_genus_${i}`]) break;
    extraCount++;
  }
  const expandBtn = latched && extraCount > 0
    ? `<button class="expand-btn">${expanded ? 'less ↑' : 'more ↓'}</button>`
    : '';

  const header = props.area_name
    ? `<div class="card-header"><div class="card-title">${props.area_name}</div>${expandBtn}</div>`
    : expandBtn ? `<div class="card-header card-header-end">${expandBtn}</div>` : '';

  const maxRows = expanded ? 10 : 5;
  return `${header}
    <div class="card-stats">${stats.map(s => `<span>${s}</span>`).join('')}</div>
    ${renderTreeChart(props, phylopicIndex, maxRows)}
    ${renderForestChart(props, phylopicIndex)}`;
}

function renderTreePointCard(props, phylopicIndex) {
  const genus = props.genus_latin || '';
  const species = props.species_latin || '';
  const sourceLabel = {
    strassenbaeume: 'Street tree', anlagenbaeume: 'Park tree', gruen_berlin: 'Grün Berlin',
  }[props.source] || props.source || '';
  return `
    <div class="card-title card-title-tree">
      ${genusIcon(genus, phylopicIndex)}
      <span>${species || genus || 'Unknown tree'}</span>
    </div>
    ${sourceLabel ? `<div class="card-stats"><span>${sourceLabel}</span></div>` : ''}
    ${props.planting_year ? `<div class="card-stats"><span>Planted ${props.planting_year}</span></div>` : ''}`;
}

// ---------------------------------------------------------------------------
// Selection highlight
// ---------------------------------------------------------------------------

// Polygon selections use setFilter on the tile source layers so the outline
// matches the actual full hex/admin geometry, not the tile-clipped fragment
// that would come from e.features[0].geometry.

function _clearAllSelections() {
  if (!_map) return;
  for (const r of [6, 7, 8, 9]) {
    try { _map.setFilter(`hexes_res${r}-selected`, ['==', ['get', 'h3_index_str'], '']); } catch (_) {}
  }
  for (const a of ['bezirke', 'ortsteile']) {
    try { _map.setFilter(`admin_${a}-selected`, ['==', ['get', 'area_id'], -1]); } catch (_) {}
  }
  const src = _map.getSource('selected-tree');
  if (src) src.setData({ type: 'FeatureCollection', features: [] });
}

function _highlightFeature(layerId, props, feature) {
  _clearAllSelections();
  if (!_map) return;

  if (layerId.startsWith('hexes_res')) {
    const h3 = props.h3_index_str ?? '';
    for (const r of [6, 7, 8, 9]) {
      try { _map.setFilter(`hexes_res${r}-selected`, ['==', ['get', 'h3_index_str'], h3]); } catch (_) {}
    }
  } else if (layerId === 'admin_bezirke-fill') {
    try { _map.setFilter('admin_bezirke-selected', ['==', ['get', 'area_id'], props.area_id]); } catch (_) {}
  } else if (layerId === 'admin_ortsteile-fill') {
    try { _map.setFilter('admin_ortsteile-selected', ['==', ['get', 'area_id'], props.area_id]); } catch (_) {}
  } else if (layerId === 'trees-circle') {
    const src = _map.getSource('selected-tree');
    if (src) src.setData({ type: 'FeatureCollection', features: [feature] });
  }
}

// ---------------------------------------------------------------------------
// Card display
// ---------------------------------------------------------------------------

function _layerType(layerId) {
  if (layerId.startsWith('hexes_res')) return 'hex';
  if (layerId.startsWith('admin_')) return 'admin';
  if (layerId === 'trees-circle') return 'tree';
  return 'unknown';
}

function showCard(html) {
  const el = document.getElementById('ctrl-info');
  if (el) el.innerHTML = html;
}

function showIdle() {
  _state = 'idle';
  _currentCard = null;
  _stopDashAnim();
  showCard(renderDatasetCard(_summary, _getPhylopicIndex?.() ?? {}));
}

export function restoreLatched(layerId, props, type) {
  _state = 'latched';
  _latchedId = _featureId(props);
  _currentCard = { props, layerId, type };
  _highlightFeature(layerId, props, null);
  _startDashAnim();
  _renderCurrentCard();
}

export function clearSelection() {
  if (_state !== 'latched') return;
  _latchedId = null;
  _currentCard = null;
  _state = 'idle';
  _clearAllSelections();
  _stopDashAnim();
  showIdle();
}

function _featureId(props) {
  return props.h3_index_str ?? props.area_id ?? `${props.genus_latin}:${props.species_latin}`;
}

function _renderCurrentCard() {
  if (!_currentCard) return;
  const { props, type } = _currentCard;
  const phi = _getPhylopicIndex?.() ?? {};
  showCard(type === 'tree'
    ? renderTreePointCard(props, phi)
    : renderHexCard(props, type, phi, _expanded, true));
}

// ---------------------------------------------------------------------------
// Setup
// ---------------------------------------------------------------------------

async function loadSummary(tilesRawUrl) {
  try {
    const p = new pmtiles.PMTiles(tilesRawUrl);
    const meta = await p.getMetadata();
    if (meta?.dataset_summary) return meta.dataset_summary;
  } catch (_) {}
  return null;
}

export function setupInfoCard(map, getPhylopicIndex, tilesRawUrl, { onLatchChange } = {}) {
  _map = map;
  _getPhylopicIndex = getPhylopicIndex;

  showCard('<div class="card-loading">Loading…</div>');

  loadSummary(tilesRawUrl).then((summary) => {
    _summary = summary;
    _densityRanges = summary?.density_ranges ?? null;
    if (_state === 'idle') showIdle();
  });

  const hexLayers = [
    'admin_bezirke-fill', 'admin_ortsteile-fill',
    'hexes_res6-fill', 'hexes_res7-fill', 'hexes_res8-fill', 'hexes_res9-fill',
  ];

  [...hexLayers, 'trees-circle'].forEach((layerId) => {
    map.on('mousemove', layerId, (e) => {
      if (_state === 'latched') return;
      map.getCanvas().style.cursor = 'pointer';
      const feature = e.features[0];
      const props = feature.properties;
      const type = _layerType(layerId);
      _state = 'hover';
      _currentCard = { props, layerId, type };
      showCard(type === 'tree'
        ? renderTreePointCard(props, _getPhylopicIndex?.() ?? {})
        : renderHexCard(props, type, _getPhylopicIndex?.() ?? {}, _expanded, true));
    });

    map.on('mouseleave', layerId, () => {
      if (_state === 'latched') return;
      map.getCanvas().style.cursor = '';
      showIdle();
    });

    map.on('click', layerId, (e) => {
      e.preventDefault();
      const feature = e.features[0];
      const props = feature.properties;
      const type = _layerType(layerId);
      const id = _featureId(props);

      if (_state === 'latched' && _latchedId === id) {
        _latchedId = null;
        _currentCard = null;
        _state = 'idle';
        _clearAllSelections();
        _stopDashAnim();
        onLatchChange?.(null);
        showIdle();
      } else {
        _state = 'latched';
        _latchedId = id;
        _currentCard = { props, layerId, type };
        _highlightFeature(layerId, props, feature);
        _startDashAnim();
        onLatchChange?.({ layerId, props, type });
        _renderCurrentCard();
      }
    });
  });

  // Click on empty space → unlatch
  map.on('click', (e) => {
    if (_state !== 'latched') return;
    const hits = map.queryRenderedFeatures(e.point, { layers: [...hexLayers, 'trees-circle'] });
    if (hits.length === 0) {
      _latchedId = null;
      _currentCard = null;
      _state = 'idle';
      _clearAllSelections();
      _stopDashAnim();
      onLatchChange?.(null);
      showIdle();
    }
  });

  // Expand/collapse button — delegate from info card container
  document.getElementById('ctrl-info')?.addEventListener('click', (e) => {
    if (!e.target.classList.contains('expand-btn')) return;
    _expanded = !_expanded;
    if (_currentCard) _renderCurrentCard();
  });
}
