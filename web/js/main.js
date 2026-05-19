import { setupControls } from './controls.js';
import { registerMissingImagePlaceholder, loadPhylopicIcons, loadPhylopicIndex } from './icons.js';
import { addMapLayers, updateGenusLabelFilter } from './layers.js';
import { createModeController } from './mode.js';
import { setupInfoCard, updateColorbar, clearSelection, restoreLatched, setForestEnabled } from './info.js';
import { loadState, saveState } from './mapState.js';

const protocol = new pmtiles.Protocol();
maplibregl.addProtocol('pmtiles', protocol.tile.bind(protocol));

const DATA_SOURCES_URL = 'https://github.com/Ossssip/Berlin-trees-hex/blob/main/docs/data_sources.md';
const ATTRIBUTION = [
  `Street &amp; park trees, forests, admin boundaries: <a href="${DATA_SOURCES_URL}" target="_blank" rel="noopener">Senatsverwaltung Berlin</a>, <a href="https://www.govdata.de/dl-de/zero-2-0" target="_blank" rel="noopener">dl-de/zero-2-0</a>`,
  `Grün Berlin trees: <a href="${DATA_SOURCES_URL}" target="_blank" rel="noopener">Grün Berlin GmbH</a>, <a href="https://www.govdata.de/dl-de/by-2-0" target="_blank" rel="noopener">dl-de/by-2-0</a>`,
  `Tree silhouettes: <a href="https://www.phylopic.org" target="_blank" rel="noopener">PhyloPic</a>, <a href="${DATA_SOURCES_URL}#tree-silhouettes" target="_blank" rel="noopener">attributions</a>`,
];

const _saved = loadState();

const map = new maplibregl.Map({
  container: 'map',
  style: 'https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json',
  center: _saved.center ?? [13.405, 52.52],
  zoom:   _saved.zoom   ?? 10,
  attributionControl: false,
});

map.addControl(new maplibregl.AttributionControl({ customAttribution: ATTRIBUTION, compact: true }), 'bottom-right');
map.addControl(new maplibregl.NavigationControl(), 'top-left');
registerMissingImagePlaceholder(map);

let phylopicIndex = {};
const getPhylopicIndex = () => phylopicIndex;

map.on('load', () => {
  const TILES_RELEASE_URL = 'https://berlin-trees-pmtiles.ossssip.workers.dev/';
  const tilesUrl = `pmtiles://${TILES_RELEASE_URL}`;
  const tilesRawUrl = TILES_RELEASE_URL;

  addMapLayers(map, tilesUrl);

  loadPhylopicIndex()
    .then((index) => {
      phylopicIndex = index;
      return loadPhylopicIcons(map, index).then(() => {
        const withIcon = Object.keys(index).filter(g => index[g] !== null);
        updateGenusLabelFilter(map, withIcon);
      });
    })
    .catch(() => {});

  const { getActiveMode, setActiveMode, syncToZoom } = createModeController(map);

  setupInfoCard(map, getPhylopicIndex, tilesRawUrl, {
    onLatchChange: (latchState) => saveState({ latched: latchState }),
  });
  if (_saved.forestEnabled === false) setForestEnabled(false);

  // onModeChange: update colorbar and density colour scale when user switches modes.
  function onModeChange(mode) {
    updateColorbar(mode);
    clearSelection();
    saveState({ mode });
  }

  setupControls(map, setActiveMode, getActiveMode, onModeChange, {
    mode: _saved.mode,
    forestEnabled: _saved.forestEnabled,
    onForestChange: (enabled) => { saveState({ forestEnabled: enabled }); setForestEnabled(enabled); },
  });

  // Save position on every pan/zoom end
  map.on('moveend', () => {
    const c = map.getCenter();
    saveState({ zoom: map.getZoom(), center: [c.lng, c.lat] });
  });

  map.on('zoom', syncToZoom);

  // Restore latched selection after tiles have had a chance to load
  if (_saved.latched) {
    const { layerId, props, type } = _saved.latched;
    map.once('idle', () => restoreLatched(layerId, props, type));
  }

  // Show attribution expanded on load, auto-collapse after 10s
  const attrEl = document.querySelector('.maplibregl-ctrl-attrib');
  if (attrEl) {
    attrEl.classList.add('maplibregl-compact-show');
    setTimeout(() => attrEl.classList.remove('maplibregl-compact-show'), 10000);
  }
});
