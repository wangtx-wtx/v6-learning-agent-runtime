/**
 * 轻量内联图标集（lucide 风格，24×24 线性描边）。
 * 只存 path 片段，由 Icon.vue 统一渲染，避免引入图标库依赖。
 */
export const ICON_PATHS: Record<string, string> = {
  /* —— 导航 —— */
  dashboard:
    '<rect width="7" height="9" x="3" y="3" rx="1.6"/><rect width="7" height="5" x="14" y="3" rx="1.6"/><rect width="7" height="9" x="14" y="12" rx="1.6"/><rect width="7" height="5" x="3" y="16" rx="1.6"/>',
  'circle-x':
    '<circle cx="12" cy="12" r="9.2"/><path d="m14.8 9.2-5.6 5.6"/><path d="m9.2 9.2 5.6 5.6"/>',
  'book-open':
    '<path d="M12 6.6v13.8"/><path d="M3 17.6a1 1 0 0 1-1-1V4.4a1 1 0 0 1 1-1h5.2a3.8 3.8 0 0 1 3.8 3.8A3.8 3.8 0 0 1 15.8 3.4H21a1 1 0 0 1 1 1v12.2a1 1 0 0 1-1 1h-5.8a2.9 2.9 0 0 0-2.9 2.9 2.9 2.9 0 0 0-2.9-2.9z"/>',
  layers:
    '<path d="M12.83 2.18a2 2 0 0 0-1.66 0L2.6 6.08a1 1 0 0 0 0 1.83l8.58 3.91a2 2 0 0 0 1.66 0l8.58-3.9a1 1 0 0 0 0-1.83Z"/><path d="m22 12.65-9.17 4.16a2 2 0 0 1-1.66 0L2 12.65"/><path d="m22 17.65-9.17 4.16a2 2 0 0 1-1.66 0L2 17.65"/>',
  headphones:
    '<path d="M3 14h3a2 2 0 0 1 2 2v3a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-7a9 9 0 0 1 18 0v7a2 2 0 0 1-2 2h-1a2 2 0 0 1-2-2v-3a2 2 0 0 1 2-2h3"/>',
  inbox:
    '<path d="M22 12h-5.2l-1.6 2.6H8.8L7.2 12H2"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/>',
  'file-pen':
    '<path d="M12.5 22H18a2 2 0 0 0 2-2V7l-5-5H6a2 2 0 0 0-2 2v9.5"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/><path d="M13.38 15.63a1 1 0 1 0-3.01-3.01l-5.01 5.01a2 2 0 0 0-.5.86l-.84 2.87a.5.5 0 0 0 .62.62l2.87-.84a2 2 0 0 0 .86-.5z"/>',
  repeat:
    '<path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/>',
  bot:
    '<path d="M12 8V4H8"/><rect width="16" height="12" x="4" y="8" rx="2.4"/><path d="M2 14h2"/><path d="M20 14h2"/><path d="M15 13v2"/><path d="M9 13v2"/>',
  refresh:
    '<path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/><path d="M8 16H3v5"/>',
  scroll:
    '<path d="M15 12h-5"/><path d="M15 8h-5"/><path d="M19 17V5a2 2 0 0 0-2-2H4"/><path d="M8 21h12a2 2 0 0 0 2-2v-1a1 1 0 0 0-1-1H11a1 1 0 0 0-1 1v1a2 2 0 1 1-4 0V5a2 2 0 1 0-4 0v2a1 1 0 0 0 1 1h3"/>',
  key:
    '<path d="m15.5 7.5 2.3 2.3a1 1 0 0 0 1.4 0l2.1-2.1a1 1 0 0 0 0-1.4L19 4"/><path d="m21 2-9.6 9.6"/><circle cx="7.5" cy="15.5" r="5.5"/>',
  smartphone:
    '<rect width="14" height="20" x="5" y="2" rx="2.6"/><path d="M12 18h.01"/>',
  network:
    '<rect x="9" y="2.5" width="6" height="5.5" rx="1.4"/><rect x="2" y="16" width="6" height="5.5" rx="1.4"/><rect x="16" y="16" width="6" height="5.5" rx="1.4"/><path d="M12 8v3.5"/><path d="M5 16v-2.5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2V16"/>',
  compass:
    '<circle cx="12" cy="12" r="9.2"/><path d="m15.9 8.1-2 5.8-5.8 2 2-5.8z"/>',

  /* —— 功能 —— */
  menu: '<path d="M4 6h16"/><path d="M4 12h16"/><path d="M4 18h16"/>',
  close: '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
  plus: '<path d="M12 5v14"/><path d="M5 12h14"/>',
  check: '<path d="M20 6 9 17l-5-5"/>',
  alert:
    '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/><path d="M12 9v4"/><path d="M12 17h.01"/>',
  calendar:
    '<path d="M8 2v4"/><path d="M16 2v4"/><rect width="18" height="18" x="3" y="4" rx="2.4"/><path d="M3 10h18"/>',
  search: '<circle cx="11" cy="11" r="7.5"/><path d="m21 21-4.3-4.3"/>',
  'search-x': '<path d="m13.5 13.5 5 5"/><path d="m18.5 13.5-5 5"/><circle cx="11" cy="11" r="7.5"/>',
  activity:
    '<path d="M22 12h-2.48a2 2 0 0 0-1.93 1.46l-2.35 8.36a.25.25 0 0 1-.48 0L9.24 2.18a.25.25 0 0 0-.48 0l-2.35 8.36A2 2 0 0 1 4.49 12H2"/>',
  gauge: '<path d="m12 14 4-4"/><path d="M3.34 19a10 10 0 1 1 17.32 0"/>',
  wallet:
    '<path d="M19 7V5a2 2 0 0 0-2-2H5a2 2 0 0 0 0 4h14a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5"/><path d="M16 12h.01"/>',
  plug: '<path d="M12 22v-5"/><path d="M9 7V2"/><path d="M15 7V2"/><path d="M6 13V9h12v4a6 6 0 0 1-12 0Z"/>',
  server:
    '<rect width="20" height="7" x="2" y="3" rx="2"/><rect width="20" height="7" x="2" y="14" rx="2"/><path d="M6 6.5h.01"/><path d="M6 17.5h.01"/>',
  cpu: '<rect width="16" height="16" x="4" y="4" rx="2.4"/><rect width="6" height="6" x="9" y="9" rx="1.2"/><path d="M9 2v2"/><path d="M15 2v2"/><path d="M9 20v2"/><path d="M15 20v2"/><path d="M2 9h2"/><path d="M2 15h2"/><path d="M20 9h2"/><path d="M20 15h2"/>',
  graduation:
    '<path d="M21.42 10.92a1 1 0 0 0-.02-1.84L12.83 5.18a2 2 0 0 0-1.66 0L2.6 9.08a1 1 0 0 0 0 1.83l8.57 3.91a2 2 0 0 0 1.66 0z"/><path d="M22 10v6"/><path d="M6 12.5V16a6 3 0 0 0 12 0v-3.5"/>',
  brain:
    '<path d="M12 5a3 3 0 1 0-5.997.125 4 4 0 0 0-2.526 5.77 4 4 0 0 0 .556 6.588A4 4 0 1 0 12 18Z"/><path d="M12 5a3 3 0 1 1 5.997.125 4 4 0 0 1 2.526 5.77 4 4 0 0 1-.556 6.588A4 4 0 1 1 12 18Z"/><path d="M12 5v14"/>',
  sparkles:
    '<path d="M9.94 15.5A2 2 0 0 0 8.5 14.06l-6.14-1.58a.5.5 0 0 1 0-.96L8.5 9.94A2 2 0 0 0 9.94 8.5l1.58-6.14a.5.5 0 0 1 .96 0l1.58 6.14a2 2 0 0 0 1.44 1.44l6.14 1.58a.5.5 0 0 1 0 .96l-6.14 1.58a2 2 0 0 0-1.44 1.44l-1.58 6.14a.5.5 0 0 1-.96 0z"/>',
  target:
    '<circle cx="12" cy="12" r="9.2"/><circle cx="12" cy="12" r="5.2"/><circle cx="12" cy="12" r="1.4"/>',
  clock: '<circle cx="12" cy="12" r="9.2"/><path d="M12 7v5.3l3.4 2"/>',
  dot: '<circle cx="12" cy="12" r="4"/>',

  /* —— 文件类型 —— */
  file: '<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7z"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/>',
  'file-text':
    '<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7z"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/><path d="M10 13h6"/><path d="M10 17h4"/>',
  'file-image':
    '<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7z"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/><circle cx="10.5" cy="12.5" r="1.6"/><path d="m20 17-2.6-2.6a2 2 0 0 0-2.8 0L9 20"/>',
  'chart-bar':
    '<path d="M3 3v16.5A1.5 1.5 0 0 0 4.5 21H21"/><path d="M7 16v-4"/><path d="M12 16V8"/><path d="M17 16v-7"/>',
  music:
    '<path d="M9 18V5.5l11-2V16"/><circle cx="6" cy="18" r="3"/><circle cx="17" cy="16" r="3"/>',
  package:
    '<path d="m7.5 4.27 9 5.15"/><path d="M21 8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16Z"/><path d="m3.3 7 8.7 5 8.7-5"/><path d="M12 22V12"/>',
  upload:
    '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m7 9 5-5 5 5"/><path d="M12 4v12"/>',
  camera:
    '<path d="M14.5 4h-5L7.5 7H4a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V9a2 2 0 0 0-2-2h-3.5z"/><circle cx="12" cy="13" r="3.5"/>',
  mic: '<rect x="9" y="2" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0"/><path d="M12 18v4"/>',
  folder:
    '<path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/>',
  trash:
    '<path d="M3 6h18"/><path d="M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M10 11v6"/><path d="M14 11v6"/>',
  database:
    '<ellipse cx="12" cy="5.5" rx="8" ry="3.2"/><path d="M4 5.5v13c0 1.77 3.58 3.2 8 3.2s8-1.43 8-3.2v-13"/><path d="M4 12c0 1.77 3.58 3.2 8 3.2s8-1.43 8-3.2"/>',
  link: '<path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/>',

  /* —— 主题 —— */
  sun:
    '<circle cx="12" cy="12" r="4"/><path d="M12 2v2"/><path d="M12 20v2"/><path d="m4.93 4.93 1.41 1.41"/><path d="m17.66 17.66 1.41 1.41"/><path d="M2 12h2"/><path d="M20 12h2"/><path d="m4.93 19.07 1.41-1.41"/><path d="m17.66 6.34 1.41-1.41"/>',
  moon: '<path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9"/>',
  monitor:
    '<rect width="20" height="14" x="2" y="3" rx="2.4"/><path d="M8 21h8"/><path d="M12 17v4"/>',

  /* —— 液态玻璃 —— */
  droplet:
    '<path d="M12 22a6.5 6.5 0 0 0 6.5-6.5c0-3.5-6.5-13.5-6.5-13.5S5.5 12 5.5 15.5A6.5 6.5 0 0 0 12 22z"/>',
  sliders:
    '<path d="M21 4h-7"/><path d="M10 4H3"/><path d="M21 12h-9"/><path d="M8 12H3"/><path d="M21 20h-5"/><path d="M12 20H3"/><path d="M14 2v4"/><path d="M8 10v4"/><path d="M16 18v4"/>',
}

export type IconName = keyof typeof ICON_PATHS

export function hasIcon(name?: string | null): boolean {
  return !!name && Object.prototype.hasOwnProperty.call(ICON_PATHS, name)
}

export function iconPath(name?: string | null): string {
  return (name && ICON_PATHS[name]) || ICON_PATHS.dot
}
