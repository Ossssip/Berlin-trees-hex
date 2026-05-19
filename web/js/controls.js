import { HEX_RESOLUTIONS } from './config.js';

const DETAIL_FOREST_ZOOM = 15;

// Matches the expression in layers.js for initial layer setup.
const fcp = ['coalesce', ['get', 'forest_cover_pct'], 0];
const DUAL_FOREST_BAND = ['all', ['>=', fcp, 33], ['<=', fcp, 67]];

let forestsEnabled = true;
let _onForestChange = null;

function refreshForests(map) {
  const zoom = map.getZoom();
  const showDetailed = zoom >= DETAIL_FOREST_ZOOM;

  map.setLayoutProperty('forests-fill', 'visibility', forestsEnabled && showDetailed ? 'visible' : 'none');
  map.setLayoutProperty('forests-outline', 'visibility', forestsEnabled && showDetailed ? 'visible' : 'none');
  map.setLayoutProperty('forests-union-fill', 'visibility', forestsEnabled && !showDetailed ? 'visible' : 'none');
  map.setLayoutProperty('forests-union-outline', 'visibility', forestsEnabled && !showDetailed ? 'visible' : 'none');

  for (const resolution of HEX_RESOLUTIONS) {
    const sourceLayer = `hexes_res${resolution}`;

    map.setPaintProperty(`${sourceLayer}-icon-trees`, 'icon-opacity',
      ['case', ['>', fcp, 67], 0, 0.85],
    );
    map.setPaintProperty(`${sourceLayer}-icon-forest`, 'icon-opacity',
      forestsEnabled ? ['case', ['<', fcp, 33], 0, 0.85] : 0,
    );
    map.setLayoutProperty(`${sourceLayer}-icon-trees`, 'icon-offset',
      forestsEnabled
        ? ['case', DUAL_FOREST_BAND, ['literal', [-32, 0]], ['literal', [0, 0]]]
        : ['literal', [0, 0]],
    );
  }
}

export function setupControls(map, setActiveMode, getActiveMode, onModeChange, initialState = {}) {
  // Mode buttons inside seg-track
  const track = document.getElementById('seg-track');
  track.querySelectorAll('.seg-btn').forEach((button) => {
    button.addEventListener('click', () => {
      setActiveMode(button.dataset.mode);
      onModeChange?.(button.dataset.mode);
    });
  });

  document.getElementById('btn-auto').addEventListener('click', () => {
    setActiveMode('auto');
    onModeChange?.('auto');
  });

  // Position and reveal the hex-block highlight and auto button
  requestAnimationFrame(() => {
    const h6Btn    = track.querySelector('[data-mode="6"]');
    const treesBtn = track.querySelector('[data-mode="trees"]');
    const highlight = document.getElementById('seg-hex-highlight');
    const autoBtn   = document.getElementById('btn-auto');
    const header    = document.getElementById('ctrl-detail-header');

    if (h6Btn && treesBtn && highlight) {
      const highlightPad = 3;
      highlight.style.left  = `${h6Btn.offsetLeft - highlightPad}px`;
      highlight.style.width = `${(treesBtn.offsetLeft + treesBtn.offsetWidth) - h6Btn.offsetLeft + highlightPad * 2}px`;
      highlight.style.opacity = '1';
    }

    if (h6Btn && treesBtn && autoBtn && header) {
      const h6Rect    = h6Btn.getBoundingClientRect();
      const treesRect = treesBtn.getBoundingClientRect();
      const hexMidX   = (h6Rect.left + treesRect.right) / 2;
      autoBtn.style.left    = `${hexMidX - header.getBoundingClientRect().left}px`;
      autoBtn.style.opacity = '1';
    }

    const startMode = initialState.mode ?? 'auto';
    setActiveMode(startMode);
    onModeChange?.(startMode);
  });

  // Restore forest state before wiring the toggle button
  if (initialState.forestEnabled === false) {
    forestsEnabled = false;
    const btn = document.getElementById('btn-forest-toggle');
    btn?.classList.remove('active');
    btn?.setAttribute('aria-pressed', 'false');
    refreshForests(map);
  }
  _onForestChange = initialState.onForestChange ?? null;

  // Detail expand/collapse (mobile only — button is display:none on desktop)
  document.getElementById('btn-detail-expand')?.addEventListener('click', () => {
    const detailEl = document.getElementById('ctrl-detail');
    const expanded = detailEl.classList.toggle('detail-expanded');
    document.getElementById('btn-detail-expand').textContent = expanded ? 'less ↑' : 'more ↓';
    document.getElementById('btn-detail-expand').setAttribute('aria-expanded', String(expanded));
  });

  // Forest master toggle
  document.getElementById('btn-forest-toggle').addEventListener('click', () => {
    forestsEnabled = !forestsEnabled;
    const btn = document.getElementById('btn-forest-toggle');
    btn.classList.toggle('active', forestsEnabled);
    btn.setAttribute('aria-pressed', String(forestsEnabled));
    refreshForests(map);
    _onForestChange?.(forestsEnabled);
  });

  map.on('zoom', () => refreshForests(map));
}
