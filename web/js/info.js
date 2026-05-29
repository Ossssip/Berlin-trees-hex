/**
 * info.js — Info card, colorbar, and feature selection.
 *
 * Replaces hover.js. Manages three display states:
 *   idle    — no feature targeted; shows dataset overview
 *   hover   — cursor over a feature; shows feature card (temporary)
 *   latched — user clicked a feature; card stays until clicked again or map clicked
 */

import { updateDensityScale } from './layers.js';
import { getGenusDataUri } from './icons.js';

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
let _forestEnabled = true;    // mirrors the forest toggle state

export function setForestEnabled(enabled) {
  _forestEnabled = enabled;
  if (_currentCard) _renderCurrentCard();
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
// Info card state attribute (used by CSS for mobile visibility)
// ---------------------------------------------------------------------------

function _setInfoState(s) {
  document.getElementById('ctrl-info')?.setAttribute('data-state', s);
  document.getElementById('ctrl-colorbar')?.setAttribute('data-state', s);
}

// ---------------------------------------------------------------------------
// Hover highlight
// ---------------------------------------------------------------------------

function _clearHoverHighlight() {
  if (!_map) return;
  for (const r of [6, 7, 8, 9]) {
    try { _map.setFilter(`hexes_res${r}-hover`, ['==', ['get', 'h3_index'], '']); } catch (_) {}
  }
  for (const a of ['bezirke', 'ortsteile']) {
    try { _map.setFilter(`admin_${a}-hover`, ['==', ['get', 'area_id'], -1]); } catch (_) {}
  }
}

function _highlightHover(layerId, props) {
  _clearHoverHighlight();
  if (!_map) return;
  if (layerId.startsWith('hexes_res')) {
    const h3 = props.h3_index ?? '';
    for (const r of [6, 7, 8, 9]) {
      try { _map.setFilter(`hexes_res${r}-hover`, ['==', ['get', 'h3_index'], h3]); } catch (_) {}
    }
  } else if (layerId === 'admin_bezirke-fill') {
    try { _map.setFilter('admin_bezirke-hover', ['==', ['get', 'area_id'], props.area_id]); } catch (_) {}
  } else if (layerId === 'admin_ortsteile-fill') {
    try { _map.setFilter('admin_ortsteile-hover', ['==', ['get', 'area_id'], props.area_id]); } catch (_) {}
  }
}

// ---------------------------------------------------------------------------
// Icon / letter fallback
// ---------------------------------------------------------------------------

function genusIcon(genus, phylopicIndex) {
  const lower = genus?.toLowerCase?.();
  if (lower && phylopicIndex?.[lower]) {
    const uri = getGenusDataUri(lower);
    const src = uri ?? `public/icons/${lower}.svg`;
    return `<img class="bar-icon" src="${src}" alt="${lower}">`;
  }
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
  const treeSection = genera
    ? `<div class="card-section"><div class="card-section-title">Top genera</div>${genera}${otherRow}</div>` : '';

  let forestSection = '';
  if (_forestEnabled && summary.forest_cover_pct > 0 && summary.forest_genera?.length) {
    const fRows = summary.forest_genera.map(g =>
      _barRow(g.genus, g.share, null, phylopicIndex, true)
    ).join('');
    const fOther = summary.forest_genus_other_share ?? 0;
    const fOtherRow = (fOther > 0.5)
      ? `<div class="bar-row bar-other bar-forest">
          <div class="bar-label"><span class="bar-icon bar-icon-letter">…</span><span class="bar-name">other</span></div>
          <div class="bar-track"><div class="bar-fill bar-fill-forest" style="width:${fOther}%"></div></div>
          <div class="bar-pct">${Number(fOther).toFixed(1)}%</div>
         </div>` : '';
    const fcpBadge = `<span class="fcp-badge">${Number(summary.forest_cover_pct).toFixed(0)}% of area</span>`;
    forestSection = `<div class="card-section card-forest"><div class="card-section-title">Forest composition ${fcpBadge}</div>${fRows}${fOtherRow}</div>`;
  }

  return `
    <div class="card-title">Berlin Trees</div>
    <div class="card-stats">
      <span>${(summary.tree_count || 0).toLocaleString()} ${summary.tree_count === 1 ? 'tree' : 'trees'}</span>
      <span>${summary.genus_count || 0} genera</span>
      <span>${summary.berlin_area_km2 || 0} km²</span>
    </div>
    ${treeSection}${forestSection}`;
}

function renderHexCard(props, layerType, phylopicIndex, expanded, latched) {
  const trees = Number(props.tree_count) || 0;
  const density = props.tree_density_km2 != null
    ? `${Number(props.tree_density_km2).toFixed(0)} trees/km²` : '';
  const stats = [
    trees ? `${trees.toLocaleString()} ${trees === 1 ? 'tree' : 'trees'}` : null,
    density || null,
    (() => {
      const fcp = Number(props.forest_cover_pct) || 0;
      if (fcp > 10 && props.non_forest_area_km2 != null)
        return `${Number(props.non_forest_area_km2).toFixed(2)} km² non-forest`;
      return props.berlin_area_km2 ? `${Number(props.berlin_area_km2).toFixed(2)} km²` : null;
    })(),
  ].filter(Boolean);

  // Count how many genera slots are populated (1–10)
  let totalGenera = 0;
  for (let i = 1; i <= 10; i++) {
    if (!props[`tree_genus_${i}`]) break;
    totalGenera++;
  }
  const moreThan10 = totalGenera === 10 && Number(props.tree_genus_other_share ?? 0) > 0.5;

  // Show expand control only when there are more than 5 genera
  const hasMore = totalGenera > 5;
  const expandBtn = latched && hasMore
    ? `<button class="expand-btn">${expanded ? 'less ↑' : 'more ↓'}</button>`
    : '';

  const header = props.area_name
    ? `<div class="card-header"><div class="card-title">${props.area_name}</div>${expandBtn}</div>`
    : expandBtn ? `<div class="card-header card-header-end">${expandBtn}</div>` : '';

  // maxRows: ≤5 genera → show all; short → 4+other; expanded → all or 9+other if >10
  const maxRows = !hasMore ? totalGenera
    : !expanded ? 4
    : moreThan10 ? 9 : totalGenera;

  return `${header}
    <div class="card-stats">${stats.map(s => `<span>${s}</span>`).join('')}</div>
    ${renderTreeChart(props, phylopicIndex, maxRows)}
    ${_forestEnabled ? renderForestChart(props, phylopicIndex) : ''}`;
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
    try { _map.setFilter(`hexes_res${r}-selected`, ['==', ['get', 'h3_index'], '']); } catch (_) {}
  }
  for (const a of ['bezirke', 'ortsteile']) {
    try { _map.setFilter(`admin_${a}-selected`, ['==', ['get', 'area_id'], -1]); } catch (_) {}
  }
  const src = _map.getSource('selected-tree');
  if (src) src.setData({ type: 'FeatureCollection', features: [] });
  _clearHoverHighlight();
}

function _highlightFeature(layerId, props, feature) {
  _clearAllSelections();
  if (!_map) return;

  if (layerId.startsWith('hexes_res')) {
    const h3 = props.h3_index ?? '';
    for (const r of [6, 7, 8, 9]) {
      try { _map.setFilter(`hexes_res${r}-selected`, ['==', ['get', 'h3_index'], h3]); } catch (_) {}
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

function showCard(html, { showClose = false } = {}) {
  const el = document.getElementById('ctrl-info');
  if (!el) return;
  el.innerHTML = html;
  if (showClose) {
    const closeBtn = document.createElement('button');
    closeBtn.className = 'close-btn';
    closeBtn.setAttribute('aria-label', 'Close');
    closeBtn.textContent = '✕';

    const expandBtn = el.querySelector('.expand-btn');
    if (expandBtn) {
      // Keep expand + close always adjacent regardless of title width
      const controls = document.createElement('div');
      controls.className = 'card-controls';
      expandBtn.parentNode.insertBefore(controls, expandBtn);
      controls.appendChild(expandBtn);
      controls.appendChild(closeBtn);
    } else {
      const header = el.querySelector('.card-header');
      if (header) {
        header.appendChild(closeBtn);
      } else {
        const div = document.createElement('div');
        div.className = 'card-header card-header-end';
        div.appendChild(closeBtn);
        el.insertBefore(div, el.firstChild);
      }
    }
  }
}

function showIdle() {
  _state = 'idle';
  _currentCard = null;
  _setInfoState('idle');
  showCard(renderDatasetCard(_summary, _getPhylopicIndex?.() ?? {}));
}

export function restoreLatched(layerId, props, type) {
  _state = 'latched';
  _latchedId = _featureId(props);
  _currentCard = { props, layerId, type };
  _highlightFeature(layerId, props, null);
  _setInfoState('latched');
  _renderCurrentCard();
}

export function clearSelection() {
  if (_state !== 'latched') return;
  _latchedId = null;
  _currentCard = null;
  _state = 'idle';
  _clearAllSelections();
  showIdle();
}

function _featureId(props) {
  return props.h3_index ?? props.area_id ?? `${props.genus_latin}:${props.species_latin}`;
}

function _renderCurrentCard() {
  if (!_currentCard) return;
  const { props, type } = _currentCard;
  const phi = _getPhylopicIndex?.() ?? {};
  showCard(
    type === 'tree' ? renderTreePointCard(props, phi) : renderHexCard(props, type, phi, _expanded, true),
    { showClose: true },
  );
}

// ---------------------------------------------------------------------------
// Setup
// ---------------------------------------------------------------------------

function _parseSummaryFromTileProps(props) {
  const top_genera = [];
  for (let i = 1; i <= 10; i++) {
    const genus = props[`tree_genus_${i}`];
    if (!genus) break;
    top_genera.push({
      genus,
      count: Number(props[`tree_genus_${i}_count`]),
      share: Number(props[`tree_genus_${i}_share`]),
    });
  }
  return {
    tree_count:             Number(props.tree_count),
    genus_count:            Number(props.genus_count),
    berlin_area_km2:        Number(props.berlin_area_km2),
    forest_area_km2:        Number(props.forest_area_km2),
    non_forest_area_km2:    Number(props.non_forest_area_km2),
    forest_cover_pct:       Number(props.forest_cover_pct),
    tree_density_km2:       Number(props.tree_density_km2),
    top_genera,
    top_genera_other_count: Number(props.tree_genus_other_count),
    top_genera_other_share: Number(props.tree_genus_other_share),
    forest_cover_pct:       Number(props.forest_cover_pct),
    forest_genera: [1, 2, 3, 4, 5].map(i => ({
      genus: props[`forest_genus_${i}`],
      share: Number(props[`forest_genus_${i}_share`]),
    })).filter(g => g.genus),
    forest_genus_other_share: Number(props.forest_genus_other_share ?? 0),
    density_ranges: {
      res6:      { p50: Number(props.density_res6_p50),      p95: Number(props.density_res6_p95) },
      res7:      { p50: Number(props.density_res7_p50),      p95: Number(props.density_res7_p95) },
      res8:      { p50: Number(props.density_res8_p50),      p95: Number(props.density_res8_p95) },
      res9:      { p50: Number(props.density_res9_p50),      p95: Number(props.density_res9_p95) },
      bezirke:   { p50: Number(props.density_bezirke_p50),   p95: Number(props.density_bezirke_p95) },
      ortsteile: { p50: Number(props.density_ortsteile_p50), p95: Number(props.density_ortsteile_p95) },
    },
  };
}

function _querySummaryFromTiles() {
  const features = _map?.querySourceFeatures('berlin-trees', { sourceLayer: 'agg_berlin' });
  if (!features?.length) return null;
  return _parseSummaryFromTileProps(features[0].properties);
}

export function setupInfoCard(map, getPhylopicIndex, { onLatchChange } = {}) {
  _map = map;
  _getPhylopicIndex = getPhylopicIndex;

  showCard('<div class="card-loading">Loading…</div>');
  _setInfoState('idle');

  map.once('idle', () => {
    const summary = _querySummaryFromTiles();
    _summary = summary;
    _densityRanges = summary?.density_ranges ?? null;
    if (_state === 'idle') showIdle();
  });

  const hexLayers = [
    'admin_bezirke-fill', 'admin_ortsteile-fill',
    'hexes_res6-fill', 'hexes_res7-fill', 'hexes_res8-fill', 'hexes_res9-fill',
  ];
  const centroidLayers = [6, 7, 8, 9].flatMap(r => [
    `hexes_res${r}-label-genus`,
    `hexes_res${r}-icon-trees`,
    `hexes_res${r}-icon-forest`,
  ]);

  // Centroid symbol layers intercept pointer events. Resolve them to the
  // underlying fill feature so the card and selection work normally.
  function _resolveCentroidFeature(point) {
    const hits = map.queryRenderedFeatures(point, { layers: hexLayers });
    return hits[0] ?? null;
  }

  centroidLayers.forEach((centroidId) => {
    map.on('mousemove', centroidId, (e) => {
      if (_state === 'latched') return;
      const feature = _resolveCentroidFeature(e.point);
      if (!feature) return;
      map.getCanvas().style.cursor = 'pointer';
      const props = feature.properties;
      const id = _featureId(props);
      if (_state === 'hover' && _latchedId === id) return;
      _latchedId = id;
      const layerId = feature.layer.id;
      const type = _layerType(layerId);
      _state = 'hover';
      _currentCard = { props, layerId, type };
      _highlightHover(layerId, props);
      _setInfoState('hover');
      requestAnimationFrame(() => {
        if (_state !== 'hover' || _latchedId !== id) return;
        showCard(renderHexCard(props, type, _getPhylopicIndex?.() ?? {}, _expanded, true));
      });
    });

    map.on('mouseleave', centroidId, () => {
      if (_state === 'latched') return;
      map.getCanvas().style.cursor = '';
      _latchedId = null;
      _clearHoverHighlight();
      showIdle();
    });

    map.on('click', centroidId, (e) => {
      e.preventDefault();
      const feature = _resolveCentroidFeature(e.point);
      if (!feature) return;
      const props = feature.properties;
      const layerId = feature.layer.id;
      const type = _layerType(layerId);
      const id = _featureId(props);

      if (_state === 'latched' && _latchedId === id) {
        _latchedId = null;
        _currentCard = null;
        _state = 'idle';
        _clearAllSelections();
        onLatchChange?.(null);
        showIdle();
      } else {
        _state = 'latched';
        _latchedId = id;
        _currentCard = { props, layerId, type };
        _clearHoverHighlight();
        _highlightFeature(layerId, props, feature);
        _setInfoState('latched');
        onLatchChange?.({ layerId, props, type });
        _renderCurrentCard();
      }
    });
  });

  [...hexLayers, 'trees-circle'].forEach((layerId) => {
    map.on('mousemove', layerId, (e) => {
      if (_state === 'latched') return;
      map.getCanvas().style.cursor = 'pointer';
      const feature = e.features[0];
      const props = feature.properties;
      const id = _featureId(props);
      if (_state === 'hover' && _latchedId === id) return;
      _latchedId = id;
      const type = _layerType(layerId);
      _state = 'hover';
      _currentCard = { props, layerId, type };
      // Highlight first so MapLibre can paint it on the next frame
      _highlightHover(layerId, props);
      _setInfoState('hover');
      // Defer card DOM update so it doesn't block the map repaint
      requestAnimationFrame(() => {
        if (_state !== 'hover' || _latchedId !== id) return;
        showCard(type === 'tree'
          ? renderTreePointCard(props, _getPhylopicIndex?.() ?? {})
          : renderHexCard(props, type, _getPhylopicIndex?.() ?? {}, _expanded, true));
      });
    });

    map.on('mouseleave', layerId, () => {
      if (_state === 'latched') return;
      map.getCanvas().style.cursor = '';
      _latchedId = null;
      _clearHoverHighlight();
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
        onLatchChange?.(null);
        showIdle();
      } else {
        _state = 'latched';
        _latchedId = id;
        _currentCard = { props, layerId, type };
        _clearHoverHighlight();
        _highlightFeature(layerId, props, feature);
        _setInfoState('latched');
        onLatchChange?.({ layerId, props, type });
        _renderCurrentCard();
      }
    });
  });

  // Click on empty space → unlatch
  map.on('click', (e) => {
    if (_state !== 'latched') return;
    const hits = map.queryRenderedFeatures(e.point, { layers: [...hexLayers, ...centroidLayers, 'trees-circle'] });
    if (hits.length === 0) {
      _latchedId = null;
      _currentCard = null;
      _state = 'idle';
      _clearAllSelections();
      onLatchChange?.(null);
      showIdle();
    }
  });

  // Expand/collapse button — delegate from info card container
  document.getElementById('ctrl-info')?.addEventListener('click', (e) => {
    if (e.target.classList.contains('close-btn')) {
      _latchedId = null;
      _currentCard = null;
      _state = 'idle';
      _clearAllSelections();
      onLatchChange?.(null);
      showIdle();
      return;
    }
    if (!e.target.classList.contains('expand-btn')) return;
    _expanded = !_expanded;
    if (_currentCard) _renderCurrentCard();
  });
}
