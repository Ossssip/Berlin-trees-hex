// HEX_COLOR: subtle fill tint for hex/admin polygons (used at fill-opacity 0.001 — visual only).
export const HEX_COLOR = ['interpolate', ['linear'], ['coalesce', ['get', 'tree_density_km2'], 0],
  0, '#edf8e9', 50, '#bae4b3', 200, '#74c476', 600, '#31a354', 1500, '#006d2c'];

export const HEX_RESOLUTIONS = [6, 7, 8, 9];

export const HEX_ICON_SIZES = {
  6: ['interpolate', ['exponential', 2], ['zoom'], 6, 0.058, 14, 14.9],
  7: ['interpolate', ['exponential', 2], ['zoom'], 8, 0.089, 14, 5.7],
  8: ['interpolate', ['exponential', 2], ['zoom'], 10, 0.133, 14, 2.13],
  9: ['interpolate', ['exponential', 2], ['zoom'], 11, 0.101, 14, 0.81],
};

export const ADMIN_ICON_SIZES = {
  bezirke: ['interpolate', ['exponential', 2], ['zoom'], 6, 0.1, 12, 3.2],
  ortsteile: ['interpolate', ['exponential', 2], ['zoom'], 8, 0.089, 14, 5.7],
};

export const RES_LAYERS = {
  bezirke:   ['admin_bezirke-fill', 'admin_bezirke-outline', 'admin_bezirke-label', 'admin_bezirke-icon'],
  ortsteile: ['admin_ortsteile-fill', 'admin_ortsteile-outline', 'admin_ortsteile-label', 'admin_ortsteile-icon'],
  6: ['hexes_res6-fill', 'hexes_res6-outline', 'hexes_res6-label-genus', 'hexes_res6-icon-trees', 'hexes_res6-icon-forest'],
  7: ['hexes_res7-fill', 'hexes_res7-outline', 'hexes_res7-label-genus', 'hexes_res7-icon-trees', 'hexes_res7-icon-forest'],
  8: ['hexes_res8-fill', 'hexes_res8-outline', 'hexes_res8-label-genus', 'hexes_res8-icon-trees', 'hexes_res8-icon-forest'],
  9: ['hexes_res9-fill', 'hexes_res9-outline', 'hexes_res9-label-genus', 'hexes_res9-icon-trees', 'hexes_res9-icon-forest'],
  trees: ['trees-circle'],
};
