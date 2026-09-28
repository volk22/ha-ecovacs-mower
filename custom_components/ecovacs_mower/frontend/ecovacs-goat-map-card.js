/*
 * ecovacs-goat-map-card — interactive map card for ECOVACS GOAT robotic mowers.
 * Plain ES module, vanilla Web Component, no build step, no external imports.
 * Data: GET /api/ecovacs_mower/map?entity_id=<entity> (millimetres, dock at 0,0, y up).
 */

const CARD_VERSION = '1.0.0';
const FETCH_THROTTLE_MS = 2000;
const DP_TOLERANCE = 75; // mm
const TAP_SLOP_PX = 6;
const DOUBLE_TAP_MS = 280;

// ---------------------------------------------------------------- i18n
const STRINGS = {
  en: {
    docked: 'Docked', mowing: 'Mowing', paused: 'Paused', returning: 'Returning', error: 'Error',
    idle: 'Idle', unavailable: 'Unavailable', unknown: 'Unknown',
    refresh: 'Refresh map', start: 'Start', pause: 'Pause', dock: 'Return to dock', fit: 'Fit map',
    cancel: 'Cancel', confirm: 'Confirm',
    confirmStart: 'Start mowing?', confirmPause: 'Pause mowing?', confirmDock: 'Send the mower back to the dock?',
    deleteAvoid: 'Delete avoid point #%s?', deleteAvoidText: 'The mower will stop routing around this spot.',
    delete: 'Delete', zone: 'Zone %s', mowZone: 'Mow this zone', confirmMowZone: 'Start mowing zone "%s"?',
    obstacles: 'Obstacles', edges: 'Edges', islands: 'Islands', avoid: 'Avoid points', track: 'Track',
    loading: 'Loading map…', fetchError: 'Map update failed', noEntity: 'Entity not found: %s',
    callError: 'Command failed', noMap: 'No map data yet', battery: 'Battery',
  },
  ru: {
    docked: 'Докована', mowing: 'Косит', paused: 'Пауза', returning: 'Возвращается', error: 'Ошибка',
    idle: 'Ожидание', unavailable: 'Недоступна', unknown: 'Неизвестно',
    refresh: 'Обновить карту', start: 'Старт', pause: 'Пауза', dock: 'На базу', fit: 'Вписать карту',
    cancel: 'Отмена', confirm: 'Подтвердить',
    confirmStart: 'Начать кошение?', confirmPause: 'Приостановить кошение?', confirmDock: 'Отправить косилку на базу?',
    deleteAvoid: 'Удалить точку объезда #%s?', deleteAvoidText: 'Косилка перестанет объезжать это место.',
    delete: 'Удалить', zone: 'Зона %s', mowZone: 'Косить эту зону', confirmMowZone: 'Начать кошение зоны «%s»?',
    obstacles: 'Препятствия', edges: 'Кромки', islands: 'Острова', avoid: 'Точки объезда', track: 'След',
    loading: 'Загрузка карты…', fetchError: 'Не удалось обновить карту', noEntity: 'Сущность не найдена: %s',
    callError: 'Команда не выполнена', noMap: 'Карты пока нет', battery: 'Батарея',
  },
};

// ---------------------------------------------------------------- icons (24x24 paths)
const ICONS = {
  refresh: 'M17.65 6.35A7.96 7.96 0 0 0 12 4a8 8 0 1 0 7.73 10h-2.08A6 6 0 1 1 12 6c1.66 0 3.14.69 4.22 1.78L13 11h7V4l-2.35 2.35z',
  start: 'M8 5v14l11-7z',
  pause: 'M6 19h4V5H6v14zm8-14v14h4V5h-4z',
  dock: 'M10 20v-6h4v6h5v-8h3L12 3 2 12h3v8z',
  fit: 'M4 4h6v2H6v4H4V4zm10 0h6v6h-2V6h-4V4zM4 14h2v4h4v2H4v-6zm14 0h2v6h-6v-2h4v-4z',
  battery: 'M16 4h-2V2h-4v2H8a1 1 0 0 0-1 1v16a1 1 0 0 0 1 1h8a1 1 0 0 0 1-1V5a1 1 0 0 0-1-1z',
};
const icon = (name) => `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="${ICONS[name]}"/></svg>`;

// Legend layers: key -> [string key, swatch color]
const LAYERS = [
  ['obstacles', '#ff9800'],
  ['edges', '#ffffff'],
  ['islands', '#a47b4f'],
  ['avoid', '#ff5a36'],
  ['track', '#1e88e5'],
];

// ---------------------------------------------------------------- helpers
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const flip = (p) => [Number(p[0]), -Number(p[1])]; // data frame (y up) -> SVG frame (y down)
const isPt = (p) => Array.isArray(p) && p.length >= 2 && isFinite(p[0]) && isFinite(p[1]);
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));

// ---------------------------------------------------------------- geometry
/** Douglas–Peucker on an open polyline (iterative). */
function douglasPeucker(pts, tol) {
  const n = pts.length;
  if (n < 3) return pts.slice();
  const keep = new Uint8Array(n);
  keep[0] = keep[n - 1] = 1;
  const t2 = tol * tol;
  const stack = [[0, n - 1]];
  while (stack.length) {
    const [a, b] = stack.pop();
    const ax = pts[a][0], ay = pts[a][1];
    const dx = pts[b][0] - ax, dy = pts[b][1] - ay;
    const len2 = dx * dx + dy * dy;
    let maxD = -1, maxI = -1;
    for (let i = a + 1; i < b; i++) {
      const px = pts[i][0] - ax, py = pts[i][1] - ay;
      let d;
      if (len2 === 0) d = px * px + py * py;
      else {
        const t = clamp((px * dx + py * dy) / len2, 0, 1);
        const qx = t * dx - px, qy = t * dy - py;
        d = qx * qx + qy * qy;
      }
      if (d > maxD) { maxD = d; maxI = i; }
    }
    if (maxD > t2) { keep[maxI] = 1; stack.push([a, maxI], [maxI, b]); }
  }
  return pts.filter((_, i) => keep[i]);
}

/** Douglas–Peucker for a closed ring: split at the vertex farthest from the first one. */
function douglasPeuckerClosed(pts, tol) {
  let p = pts;
  if (p.length > 1 && p[0][0] === p[p.length - 1][0] && p[0][1] === p[p.length - 1][1]) p = p.slice(0, -1);
  if (p.length < 4) return p.slice();
  let fi = 0, fd = -1;
  for (let i = 1; i < p.length; i++) {
    const d = (p[i][0] - p[0][0]) ** 2 + (p[i][1] - p[0][1]) ** 2;
    if (d > fd) { fd = d; fi = i; }
  }
  const a = douglasPeucker(p.slice(0, fi + 1), tol);
  const b = douglasPeucker(p.slice(fi).concat([p[0]]), tol);
  return a.concat(b.slice(1, -1));
}

/** Chaikin corner cutting. Open variant keeps both endpoints. */
function chaikin(pts, closed, passes = 2) {
  let p = pts;
  for (let k = 0; k < passes; k++) {
    const n = p.length;
    if (n < 3) break;
    const out = [];
    if (closed) {
      for (let i = 0; i < n; i++) {
        const a = p[i], b = p[(i + 1) % n];
        out.push([0.75 * a[0] + 0.25 * b[0], 0.75 * a[1] + 0.25 * b[1]],
                 [0.25 * a[0] + 0.75 * b[0], 0.25 * a[1] + 0.75 * b[1]]);
      }
    } else {
      out.push(p[0]);
      for (let i = 0; i < n - 1; i++) {
        const a = p[i], b = p[i + 1];
        if (i > 0) out.push([0.75 * a[0] + 0.25 * b[0], 0.75 * a[1] + 0.25 * b[1]]);
        if (i < n - 2) out.push([0.25 * a[0] + 0.75 * b[0], 0.25 * a[1] + 0.75 * b[1]]);
      }
      out.push(p[n - 1]);
    }
    p = out;
  }
  return p;
}

// Only dense chain-code outlines are smoothed; hand-drawn polygons (few vertices) keep sharp corners.
const smoothClosed = (pts, on) => (on && pts.length > 20 ? chaikin(douglasPeuckerClosed(pts, DP_TOLERANCE), true) : pts);
const smoothOpen = (pts, on) => (on ? chaikin(douglasPeucker(pts, DP_TOLERANCE), false) : pts);

function pathD(pts, closed) {
  if (!pts.length) return '';
  let d = `M${Math.round(pts[0][0])} ${Math.round(pts[0][1])}`;
  for (let i = 1; i < pts.length; i++) d += `L${Math.round(pts[i][0])} ${Math.round(pts[i][1])}`;
  return closed ? d + 'Z' : d;
}

function pointInPoly(x, y, poly) {
  let inside = false;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const xi = poly[i][0], yi = poly[i][1], xj = poly[j][0], yj = poly[j][1];
    if ((yi > y) !== (yj > y) && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}

function bbox(pts) {
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (const p of pts) {
    if (p[0] < x0) x0 = p[0]; if (p[0] > x1) x1 = p[0];
    if (p[1] < y0) y0 = p[1]; if (p[1] > y1) y1 = p[1];
  }
  return { x0, y0, x1, y1 };
}

const ptsOf = (item) => (Array.isArray(item?.points) ? item.points.filter(isPt).map(flip) : []);

// ---------------------------------------------------------------- styles
const STYLE = `
:host { display: block; }
ha-card { display: block; position: relative; overflow: hidden; color: var(--primary-text-color);
  background: var(--ha-card-background, var(--card-background-color, #fff));
  border-radius: var(--ha-card-border-radius, 12px); -webkit-tap-highlight-color: transparent; }
.hdr { display: flex; align-items: center; gap: 8px; padding: 12px 16px 8px; min-height: 28px; }
.title { flex: 1; min-width: 0; font-size: 16px; font-weight: 500; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.state { font-size: 12px; font-weight: 500; padding: 3px 9px; border-radius: 999px; white-space: nowrap;
  background: var(--secondary-background-color, rgba(127,127,127,.12)); color: var(--secondary-text-color); }
.state.mowing { background: rgba(76,175,80,.18); color: #3e9142; }
.state.returning, .state.paused { background: rgba(255,152,0,.18); color: #d9820a; }
.state.error { background: rgba(229,57,53,.16); color: #e53935; }
.bat { display: flex; align-items: center; gap: 2px; font-size: 12px; color: var(--secondary-text-color); white-space: nowrap; }
.bat svg { width: 14px; height: 14px; fill: currentColor; }
.map { position: relative; margin: 0 8px; border-radius: 10px; overflow: hidden;
  background: var(--secondary-background-color, rgba(127,127,127,.08)); }
svg.world { position: absolute; left: 0; top: 0; width: 100%; height: 100%; display: block;
  touch-action: none; -webkit-user-select: none; user-select: none; cursor: grab; }
svg.world.drag { cursor: grabbing; }
.ns { vector-effect: non-scaling-stroke; }
.lawn { fill: #9fd26b; stroke: #5a9a37; stroke-width: 2; fill-rule: evenodd; }
/* During a job onMapTrack carries the rows still to mow (A1600): the lawn is
   painted as mowed and the remaining rows as long grass. */
.L-lawn.job .lawn { fill: #cfe8ad; }
.lane { stroke: #6aa33f; stroke-width: 260; stroke-linecap: butt; fill: none; }
.swath { fill: none; stroke: rgba(255,255,255,.18); stroke-width: 230; stroke-linejoin: round; stroke-linecap: round; }
.bed { fill: #b89068; stroke: #8a6743; stroke-width: 1.5; fill-opacity: .9; }
.nogo { fill: url(#hatch); stroke: #e53935; stroke-width: 2; }
.edge { fill: none; stroke: rgba(255,255,255,.95); stroke-linecap: round; stroke-linejoin: round; }
.obst { fill: #ffa726; fill-opacity: .85; stroke: #e65100; stroke-width: 1.2; }
.track { fill: none; stroke: #1e88e5; stroke-width: 2; stroke-linejoin: round; stroke-linecap: round; opacity: .9; }
.avoid { fill: rgba(255,90,54,.14); stroke: #ff5a36; }
.hit { fill: transparent; stroke: none; cursor: pointer; }
.zl { font: 600 13px/1 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; fill: #1f3d12;
  stroke: rgba(255,255,255,.85); stroke-width: 3px; paint-order: stroke; text-anchor: middle; dominant-baseline: central; cursor: pointer; }
.toolbar { position: absolute; top: 8px; right: 8px; display: flex; gap: 2px; padding: 3px; border-radius: 12px; z-index: 2;
  background: var(--card-background-color, #fff); box-shadow: 0 1px 4px rgba(0,0,0,.22); }
.toolbar button, .dlg button, .chip { font: inherit; -webkit-appearance: none; appearance: none; }
.toolbar button { width: 36px; height: 36px; border: 0; border-radius: 9px; background: transparent; color: var(--primary-text-color);
  display: flex; align-items: center; justify-content: center; cursor: pointer; padding: 0; }
.toolbar button:hover { background: rgba(127,127,127,.14); }
.toolbar button:active { background: rgba(127,127,127,.26); }
.toolbar button svg { width: 20px; height: 20px; fill: currentColor; }
.toolbar .sep { width: 1px; margin: 6px 2px; background: var(--divider-color, rgba(127,127,127,.3)); }
.status { position: absolute; left: 8px; bottom: 8px; max-width: calc(100% - 110px); z-index: 2; font-size: 12px; padding: 4px 10px;
  border-radius: 999px; background: var(--card-background-color, #fff); color: var(--secondary-text-color); box-shadow: 0 1px 3px rgba(0,0,0,.2);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.status.err { color: #e53935; }
.status[hidden] { display: none; }
.scale { position: absolute; right: 10px; bottom: 8px; z-index: 2; font-size: 11px; color: var(--primary-text-color);
  text-align: center; pointer-events: none; text-shadow: 0 0 3px var(--card-background-color, #fff); }
.scale i { display: block; height: 5px; border: 2px solid currentColor; border-top: 0; margin-top: 2px; }
.chips { display: flex; gap: 6px; padding: 10px 12px 12px; overflow-x: auto; -webkit-overflow-scrolling: touch; scrollbar-width: none; }
.chips::-webkit-scrollbar { display: none; }
.chip { display: flex; align-items: center; gap: 6px; flex: none; height: 30px; padding: 0 12px; border-radius: 999px; cursor: pointer;
  font-size: 13px; color: var(--primary-text-color); background: transparent; border: 1px solid var(--divider-color, rgba(127,127,127,.35)); }
.chip .sw { width: 10px; height: 10px; border-radius: 50%; box-shadow: 0 0 0 1px rgba(0,0,0,.25) inset; }
.chip.on { border-color: var(--primary-color, #03a9f4); background: rgba(127,127,127,.1); }
.chip:not(.on) { opacity: .5; }
.ov { position: absolute; top: 0; left: 0; right: 0; bottom: 0; z-index: 10; display: none; align-items: center; justify-content: center;
  background: rgba(0,0,0,.38); }
.ov.show { display: flex; }
.ov.sheet { align-items: flex-end; }
.dlg { width: calc(100% - 32px); max-width: 340px; box-sizing: border-box; padding: 18px 18px 12px; border-radius: 16px;
  background: var(--card-background-color, #fff); color: var(--primary-text-color); box-shadow: 0 8px 28px rgba(0,0,0,.35); }
.ov.sheet .dlg { max-width: 460px; margin-bottom: 12px; }
.dt { font-size: 17px; font-weight: 600; margin-bottom: 6px; }
.dx { font-size: 14px; color: var(--secondary-text-color); margin-bottom: 8px; line-height: 1.35; }
.db { display: flex; justify-content: flex-end; gap: 8px; margin-top: 12px; flex-wrap: wrap; }
.dlg button { min-height: 38px; padding: 0 16px; border-radius: 10px; border: 0; cursor: pointer; font-size: 14px; font-weight: 500;
  background: transparent; color: var(--primary-color, #03a9f4); }
.dlg button.pri { background: var(--primary-color, #03a9f4); color: var(--text-primary-color, #fff); }
.dlg button.dan { background: #e53935; color: #fff; }
`;

// ---------------------------------------------------------------- card
class EcovacsGoatMapCard extends HTMLElement {
  static getStubConfig(hass) {
    const ent = hass && Object.keys(hass.states).find((e) => e.startsWith('lawn_mower.'));
    return { entity: ent || 'lawn_mower.goat' };
  }

  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this._data = null;
    this._geo = null; // cached smoothed static geometry
    this._geoSig = '';
    this._vb = null; // {x,y,w,h} in SVG frame (mm)
    this._fitW = 0;
    this._size = { w: 0, h: 0 };
    this._pointers = new Map();
    this._lastFetch = 0;
    this._lastKey = null;
    this._err = null;
    this._rafPending = false;
  }

  setConfig(config) {
    if (!config || !config.entity) throw new Error('entity is required (lawn_mower.*)');
    this._config = {
      height: 420, refresh_interval: 0, show_obstacles: true, smoothing: true,
      map_entity: String(config.entity).replace(/^lawn_mower\./, 'image.') + '_map',
      ...config,
    };
    this._layers = { obstacles: this._config.show_obstacles !== false, edges: true, islands: true, avoid: true, track: true };
    try {
      const saved = JSON.parse(localStorage.getItem('ecovacs-goat-map-card:' + this._config.entity) || 'null');
      if (saved && typeof saved === 'object') Object.assign(this._layers, saved);
    } catch (e) { /* storage unavailable */ }
    this._build();
    if (this._data) this._render();
    this._setupTimer();
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._config) return;
    if (this._langKey() !== this._builtLang) { // labels were built for another language
      this._build();
      if (this._data) this._render();
    }
    this._updateHeader();
    const st = hass.states[this._config.entity];
    const mt = hass.states[this._config.map_entity];
    const key = (st ? st.last_updated : '-') + '|' + (mt ? mt.last_updated : '-');
    if (key !== this._lastKey) {
      this._lastKey = key;
      this._fetch();
    }
  }

  get hass() { return this._hass; }

  getCardSize() {
    return Math.ceil(((this._config && this._config.height) || 420) / 50) + 2;
  }

  connectedCallback() {
    this._setupTimer();
    if (this._ro && this._mapEl) this._ro.observe(this._mapEl);
  }

  disconnectedCallback() {
    clearInterval(this._timer);
    this._timer = null;
    if (this._ro) this._ro.disconnect();
  }

  // ------------------------------------------------------------ strings
  _langKey() {
    return this._hass && String(this._hass.language || '').toLowerCase().startsWith('ru') ? 'ru' : 'en';
  }

  _t(key, arg) {
    const s = STRINGS[this._langKey()][key] ?? STRINGS.en[key] ?? key;
    return arg === undefined ? s : s.replace('%s', arg);
  }

  $(sel) { return this.shadowRoot.querySelector(sel); }

  // ------------------------------------------------------------ DOM skeleton
  _build() {
    const c = this._config;
    this._builtLang = this._langKey();
    this._geoSig = '';
    this.shadowRoot.innerHTML = `
      <style>${STYLE}</style>
      <ha-card>
        <div class="hdr">
          <div class="title"></div>
          <div class="state"></div>
          <div class="bat">${icon('battery')}<span></span></div>
        </div>
        <div class="map" style="height:${Number(c.height) || 420}px">
          <svg class="world" xmlns="http://www.w3.org/2000/svg" preserveAspectRatio="none">
            <defs>
              <pattern id="hatch" patternUnits="userSpaceOnUse" width="300" height="300" patternTransform="rotate(45)">
                <rect width="300" height="300" fill="rgba(229,57,53,.12)"/>
                <line x1="0" y1="0" x2="0" y2="300" stroke="#e53935" stroke-width="90" stroke-opacity=".45"/>
              </pattern>
            </defs>
            <g class="L-lawn"></g><g class="L-lanes"></g><g class="L-islands"></g><g class="L-nogo"></g>
            <g class="L-edges"></g><g class="L-obstacles"></g><g class="L-track"></g><g class="L-avoid"></g>
            <g class="L-zones"></g><g class="L-dock"></g>
          </svg>
          <div class="toolbar">
            <button data-act="refresh" title="${esc(this._t('refresh'))}" aria-label="${esc(this._t('refresh'))}">${icon('refresh')}</button>
            <span class="sep"></span>
            <button data-act="start" title="${esc(this._t('start'))}" aria-label="${esc(this._t('start'))}">${icon('start')}</button>
            <button data-act="pause" title="${esc(this._t('pause'))}" aria-label="${esc(this._t('pause'))}">${icon('pause')}</button>
            <button data-act="dock" title="${esc(this._t('dock'))}" aria-label="${esc(this._t('dock'))}">${icon('dock')}</button>
            <span class="sep"></span>
            <button data-act="fit" title="${esc(this._t('fit'))}" aria-label="${esc(this._t('fit'))}">${icon('fit')}</button>
          </div>
          <div class="status" hidden></div>
          <div class="scale" hidden><span></span><i></i></div>
        </div>
        <div class="chips">
          ${LAYERS.map(([k, col]) => `<button class="chip" data-layer="${k}"><span class="sw" style="background:${col}"></span>${esc(this._t(k))}</button>`).join('')}
        </div>
        <div class="ov"></div>
      </ha-card>`;
    this._svg = this.$('svg.world');
    this._mapEl = this.$('.map');
    this._g = {};
    for (const g of this._svg.querySelectorAll('g[class^="L-"]')) this._g[g.getAttribute('class').slice(2)] = g;
    this._vb = null;

    this.$('.toolbar').addEventListener('click', (e) => {
      const b = e.target.closest('button');
      if (b) this._onToolbar(b.dataset.act);
    });
    this.$('.chips').addEventListener('click', (e) => {
      const b = e.target.closest('.chip');
      if (!b) return;
      const k = b.dataset.layer;
      this._layers[k] = !this._layers[k];
      try { localStorage.setItem('ecovacs-goat-map-card:' + this._config.entity, JSON.stringify(this._layers)); } catch (err) { /* ignore */ }
      this._applyLayers();
    });
    this._bindGestures();

    if (this._ro) this._ro.disconnect();
    this._ro = new ResizeObserver(() => this._onResize());
    this._ro.observe(this._mapEl);
    this._applyLayers();
    this._updateHeader();
    this._renderStatus();
  }

  // ------------------------------------------------------------ header
  _updateHeader() {
    if (!this._hass || !this._config || !this.shadowRoot.querySelector('.hdr')) return;
    const st = this._hass.states[this._config.entity];
    const d = this._data || {};
    this.$('.title').textContent = this._config.title || (st && st.attributes.friendly_name) || this._config.entity;
    const state = st ? st.state : d.state || 'unknown';
    const el = this.$('.state');
    el.textContent = this._t(STRINGS.en[state] ? state : 'unknown');
    el.className = 'state ' + state;
    const a = (st && st.attributes) || {};
    const bat = a.battery_level ?? a.battery ?? d.battery;
    const be = this.$('.bat');
    be.hidden = bat === undefined || bat === null;
    be.title = this._t('battery');
    be.querySelector('span').textContent = be.hidden ? '' : `${Math.round(bat)}%`;
  }

  _renderStatus(msg, isErr) {
    const el = this.$('.status');
    if (!el) return;
    let text = msg;
    let err = isErr;
    if (text === undefined) {
      if (this._hass && this._config && !this._hass.states[this._config.entity]) { text = this._t('noEntity', this._config.entity); err = true; }
      else if (this._err) { text = `${this._t('fetchError')}: ${this._err}`; err = true; }
      else if (!this._data) text = this._t('loading');
    }
    el.hidden = !text;
    el.textContent = text || '';
    el.classList.toggle('err', !!err);
  }

  _toast(msg, isErr) {
    this._renderStatus(msg, isErr);
    clearTimeout(this._toastT);
    this._toastT = setTimeout(() => this._renderStatus(), 4000);
  }

  // ------------------------------------------------------------ data fetch
  _setupTimer() {
    clearInterval(this._timer);
    this._timer = null;
    const s = Number(this._config && this._config.refresh_interval);
    if (s > 0 && this.isConnected) this._timer = setInterval(() => this._fetch(), Math.max(2, s) * 1000);
  }

  async _fetch() {
    if (!this._hass || !this._config) return;
    if (this._inflight) { this._pending = true; return; }
    const wait = this._lastFetch + FETCH_THROTTLE_MS - Date.now();
    if (wait > 0) { // trailing fetch so a change during cooldown is not lost
      if (!this._trail) this._trail = setTimeout(() => { this._trail = null; this._fetch(); }, wait);
      return;
    }
    this._inflight = true;
    this._lastFetch = Date.now();
    try {
      const data = await this._hass.callApi('GET', 'ecovacs_mower/map?entity_id=' + encodeURIComponent(this._config.entity));
      if (!data || typeof data !== 'object') throw new Error('empty response');
      this._err = null;
      this._data = data;
      this._render();
      this._updateHeader();
    } catch (e) {
      this._err = (e && (e.body && e.body.message || e.message || e.error)) || String(e);
    } finally {
      this._inflight = false;
      this._renderStatus();
      if (this._pending) { this._pending = false; this._fetch(); }
    }
  }

  // ------------------------------------------------------------ geometry cache
  _buildGeo(d) {
    const sm = this._config.smoothing !== false;
    const boundaryRaw = (Array.isArray(d.boundary) ? d.boundary : []).filter(isPt).map(flip);
    const islands = (d.islands || []).map((it) => ({ id: it.id, raw: ptsOf(it) })).filter((i) => i.raw.length > 2);
    islands.forEach((i) => { i.pts = smoothClosed(i.raw, sm); });
    const edges = (d.edges || []).map((it) => ({ id: it.id, pts: smoothOpen(ptsOf(it), sm) })).filter((e) => e.pts.length > 1);
    const nogo = (d.nogo || []).map((it) => ({ id: it.id, pts: smoothClosed(ptsOf(it), sm) })).filter((n) => n.pts.length > 2);
    const obstacles = (d.obstacles || []).map((it) => {
      const raw = ptsOf(it);
      return { id: it.id, pts: smoothClosed(raw, sm) };
    }).filter((o) => o.pts.length);
    return {
      boundaryRaw, boundary: boundaryRaw.length > 2 ? smoothClosed(boundaryRaw, sm) : [],
      islands, edges, nogo, obstacles,
      bbox: boundaryRaw.length ? bbox(boundaryRaw) : null,
    };
  }

  _render() {
    const d = this._data;
    if (!d || !this._g) return;
    const sig = JSON.stringify([d.boundary, d.islands, d.edges, d.obstacles, d.nogo, this._config.smoothing]);
    if (sig !== this._geoSig) {
      this._geoSig = sig;
      this._geo = this._buildGeo(d);
      this._renderStatic();
    }
    this._renderDynamic();
    if (!this._vb) this._fit();
    else this._scheduleApply();
  }

  _renderStatic() {
    const g = this._geo;
    this._renderLawn();
    this._g.islands.innerHTML = g.islands.map((i) => `<path class="bed ns" d="${pathD(i.pts, true)}"/>`).join('');
    this._g.nogo.innerHTML = g.nogo.map((n) => `<path class="nogo ns" d="${pathD(n.pts, true)}"/>`).join('');
    this._g.edges.innerHTML = g.edges.map((e) => `<path class="edge dash" data-sw="2" data-da="7 5" d="${pathD(e.pts, false)}"/>`).join('');
    this._g.obstacles.innerHTML = g.obstacles.map((o) => (o.pts.length < 3
      ? `<circle class="obst ns" cx="${Math.round(o.pts[0][0])}" cy="${Math.round(o.pts[0][1])}" r="120"/>`
      : `<path class="obst ns" d="${pathD(o.pts, true)}"/>`)).join('');
  }

  // Lawn = boundary with islands punched out (evenodd); holes disappear when islands layer is off.
  _renderLawn() {
    const g = this._geo;
    if (!g) return;
    let d = pathD(g.boundary, true);
    if (this._layers.islands) d += g.islands.map((i) => pathD(i.pts, true)).join('');
    this._g.lawn.innerHTML = d ? `<path class="lawn ns" d="${d}"/>` : '';
  }

  _renderDynamic() {
    const d = this._data;
    // Remaining rows of the running job: physical stripes in mm (scale with zoom on purpose).
    const lanes = Array.isArray(d.lanes) ? d.lanes : [];
    const job = lanes.length > 0 && d.state !== 'docked';
    this._g.lawn.classList.toggle('job', job);
    this._g.lanes.innerHTML = (job ? lanes : []).filter((l) => Array.isArray(l) && isPt(l[0]) && isPt(l[1])).map((l) => {
      const a = flip(l[0]), b = flip(l[1]);
      return `<path class="lane" d="M${a[0]} ${a[1]}L${b[0]} ${b[1]}"/>`;
    }).join('');

    const track = (Array.isArray(d.track) ? d.track : []).filter(isPt).map(flip);
    // The mowed swath: the live track drawn at blade width (~23 cm, in mm so it
    // scales with the map). onMapTrack lanes look like the remaining plan on the
    // A1600, so the swath is what "mowed" means here.
    const trackD = track.length > 1 ? pathD(douglasPeucker(track, 20), false) : '';
    this._g.lanes.insertAdjacentHTML('beforeend', trackD ? `<path class="swath" d="${trackD}"/>` : '');
    this._g.track.innerHTML = trackD ? `<path class="track ns" d="${trackD}"/>` : '';

    // Avoid points: 1 m dashed circle in world units + screen-sized icon and hit target.
    this._g.avoid.innerHTML = (d.avoid_points || []).filter((p) => isFinite(p.x) && isFinite(p.y)).map((p) => {
      const x = Math.round(p.x), y = -Math.round(p.y), id = esc(p.mssid);
      return `<g data-mssid="${id}">
        <circle class="avoid dash" data-sw="1.6" data-da="5 4" cx="${x}" cy="${y}" r="500"/>
        <g data-px data-x="${x}" data-y="${y}">
          <circle r="9" fill="#ff5a36" stroke="#fff" stroke-width="1.8"/><rect x="-4.5" y="-1.3" width="9" height="2.6" rx="1" fill="#fff"/>
        </g>
        <circle class="hit" data-hit data-x="${x}" data-y="${y}" cx="${x}" cy="${y}" r="500"/>
      </g>`;
    }).join('');

    // Zone labels.
    this._g.zones.innerHTML = (d.zones || []).filter((z) => isFinite(z.x) && isFinite(z.y)).map((z) => `
      <g data-px data-x="${Math.round(z.x)}" data-y="${-Math.round(z.y)}" data-zone="${esc(z.id)}">
        <text class="zl">${esc(this._zoneName(z))}</text></g>`).join('');

    // Dock + mower.
    let html = '';
    const dock = isPt(d.dock) ? flip(d.dock) : null;
    const pos = isPt(d.position) ? flip(d.position) : null;
    const state = (this._hass && this._hass.states[this._config.entity] || {}).state || d.state;
    const atDock = dock && pos && Math.hypot(pos[0] - dock[0], pos[1] - dock[1]) < 400 && state === 'docked';
    if (dock) {
      html += `<g data-px data-x="${dock[0]}" data-y="${dock[1]}">
        ${atDock ? '<circle r="16" fill="none" stroke="#43a047" stroke-width="2.5"/>' : ''}
        <rect x="-12" y="-12" width="24" height="24" rx="7" fill="#1e88e5" stroke="#fff" stroke-width="2"/>
        <path d="M-6.5 1.5L0 -5l6.5 6.5V7h-13z" fill="#fff"/></g>`;
    }
    if (pos && !atDock) {
      const rot = -(Number(d.heading) || 0); // heading: CCW degrees from +x in data frame
      html += `<g data-px data-x="${pos[0]}" data-y="${pos[1]}"><g transform="rotate(${rot})">
        <circle r="14" fill="rgba(0,0,0,.18)"/>
        <circle r="11.5" fill="#fff" stroke="#2e7d32" stroke-width="2.2"/>
        <path d="M-4.5 -6.5L7.5 0l-12 6.5 3-6.5z" fill="#2e7d32"/></g></g>`;
    }
    this._g.dock.innerHTML = html;
    this._pxEls = Array.from(this._svg.querySelectorAll('[data-px]'));
    this._dashEls = Array.from(this._svg.querySelectorAll('.dash'));
    this._hitEls = Array.from(this._svg.querySelectorAll('[data-hit]'));
    this._applyLayers();
  }

  _zoneName(z) {
    return z.name && String(z.name).trim() ? z.name : this._t('zone', z.id);
  }

  _applyLayers() {
    if (!this._g) return;
    const L = this._layers;
    this._g.obstacles.style.display = L.obstacles ? '' : 'none';
    this._g.edges.style.display = L.edges ? '' : 'none';
    this._g.islands.style.display = L.islands ? '' : 'none';
    this._g.avoid.style.display = L.avoid ? '' : 'none';
    this._g.track.style.display = L.track ? '' : 'none';
    this._renderLawn();
    for (const b of this.shadowRoot.querySelectorAll('.chip')) b.classList.toggle('on', !!L[b.dataset.layer]);
  }

  // ------------------------------------------------------------ viewport
  _onResize() {
    const r = this._mapEl.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return;
    const old = this._size;
    this._size = { w: r.width, h: r.height };
    if (!this._vb) { this._fit(); return; }
    // keep center and scale, adapt to new aspect
    const u = this._vb.w / (old.w || r.width);
    const cx = this._vb.x + this._vb.w / 2, cy = this._vb.y + this._vb.h / 2;
    const w = u * r.width, h = u * r.height;
    this._vb = { x: cx - w / 2, y: cy - h / 2, w, h };
    this._scheduleApply();
  }

  _fit() {
    const g = this._geo;
    const { w: cw, h: ch } = this._size;
    if (!g || !cw || !ch) return;
    const pts = g.boundaryRaw.slice();
    if (isPt(this._data.dock)) pts.push(flip(this._data.dock));
    if (!pts.length) pts.push([0, 0]);
    const b = bbox(pts);
    let w = Math.max(2000, (b.x1 - b.x0) * 1.1), h = Math.max(2000, (b.y1 - b.y0) * 1.1);
    const top = ch > 200 ? 44 : 0; // keep the toolbar strip free
    const aspect = (ch - top) / cw;
    if (h / w > aspect) w = h / aspect; else h = w * aspect;
    const u = w / cw;
    const cx = (b.x0 + b.x1) / 2, cy = (b.y0 + b.y1) / 2;
    this._vb = { x: cx - w / 2, y: cy - h / 2 - top * u, w, h: ch * u };
    this._fitW = w;
    this._scheduleApply();
  }

  _scheduleApply() {
    if (this._rafPending) return;
    this._rafPending = true;
    requestAnimationFrame(() => { this._rafPending = false; this._applyViewBox(); });
  }

  // Updates viewBox and everything that must stay constant in screen pixels.
  _applyViewBox() {
    const vb = this._vb;
    if (!vb || !this._size.w) return;
    this._svg.setAttribute('viewBox', `${vb.x} ${vb.y} ${vb.w} ${vb.h}`);
    const u = vb.w / this._size.w; // mm per screen px
    for (const el of this._pxEls || []) {
      el.setAttribute('transform', `translate(${el.dataset.x} ${el.dataset.y}) scale(${u})`);
    }
    for (const el of this._dashEls || []) {
      el.setAttribute('stroke-width', el.dataset.sw * u);
      el.setAttribute('stroke-dasharray', el.dataset.da.split(' ').map((v) => v * u).join(' '));
    }
    const hitR = Math.max(500, 20 * u); // >= 40 px diameter target
    for (const el of this._hitEls || []) el.setAttribute('r', hitR);
    this._updateScale(u);
  }

  _updateScale(u) {
    const el = this.$('.scale');
    const target = 70 * u; // mm for ~70 px
    const pow = Math.pow(10, Math.floor(Math.log10(target)));
    const nice = [1, 2, 5, 10].map((m) => m * pow).filter((v) => v <= target).pop() || pow;
    el.hidden = false;
    el.querySelector('i').style.width = `${nice / u}px`;
    el.querySelector('span').textContent = nice >= 1000 ? `${nice / 1000} m` : `${nice / 10} cm`;
  }

  _zoomAt(sx, sy, factor) {
    const vb = this._vb;
    if (!vb) return;
    const minW = 1500, maxW = Math.max(this._fitW * 4, 5000);
    const nw = clamp(vb.w * factor, minW, maxW);
    const f = nw / vb.w;
    const wx = vb.x + (sx / this._size.w) * vb.w, wy = vb.y + (sy / this._size.h) * vb.h;
    this._vb = { x: wx - (wx - vb.x) * f, y: wy - (wy - vb.y) * f, w: nw, h: vb.h * f };
    this._scheduleApply();
  }

  _toWorld(sx, sy) {
    const vb = this._vb;
    return [vb.x + (sx / this._size.w) * vb.w, vb.y + (sy / this._size.h) * vb.h];
  }

  // ------------------------------------------------------------ gestures
  _bindGestures() {
    const svg = this._svg;
    const local = (e) => { const r = svg.getBoundingClientRect(); return { x: e.clientX - r.left, y: e.clientY - r.top }; };

    svg.addEventListener('wheel', (e) => {
      if (!this._vb) return;
      e.preventDefault();
      const p = local(e);
      const dy = e.deltaMode === 1 ? e.deltaY * 33 : e.deltaMode === 2 ? e.deltaY * 400 : e.deltaY;
      this._zoomAt(p.x, p.y, Math.exp(clamp(dy, -300, 300) * 0.0012));
    }, { passive: false });

    // Block Safari page-level pinch zoom over the map.
    for (const ev of ['gesturestart', 'gesturechange']) svg.addEventListener(ev, (e) => e.preventDefault());

    const startGesture = () => {
      const pts = [...this._pointers.values()];
      this._gs = { vb: { ...this._vb }, pts: pts.map((p) => ({ ...p })) };
    };

    svg.addEventListener('pointerdown', (e) => {
      if (!this._vb) return;
      if (e.pointerType === 'mouse' && e.button !== 0) return;
      try { svg.setPointerCapture(e.pointerId); } catch (err) { /* ignore */ }
      const p = local(e);
      this._pointers.set(e.pointerId, p);
      if (this._pointers.size === 1) this._tap = { x: p.x, y: p.y, t: Date.now(), moved: false };
      else if (this._tap) this._tap.moved = true; // multi-touch is never a tap
      startGesture();
      svg.classList.add('drag');
    });

    svg.addEventListener('pointermove', (e) => {
      if (!this._pointers.has(e.pointerId) || !this._gs) return;
      const p = local(e);
      this._pointers.set(e.pointerId, p);
      const pts = [...this._pointers.values()];
      const g = this._gs;
      const { w: cw, h: ch } = this._size;
      if (pts.length === 1 && g.pts.length === 1) {
        const dx = p.x - g.pts[0].x, dy = p.y - g.pts[0].y;
        if (this._tap && Math.hypot(p.x - this._tap.x, p.y - this._tap.y) > TAP_SLOP_PX) this._tap.moved = true;
        this._vb = { ...g.vb, x: g.vb.x - (dx / cw) * g.vb.w, y: g.vb.y - (dy / ch) * g.vb.h };
      } else if (pts.length >= 2 && g.pts.length >= 2) {
        // pinch-zoom around the midpoint + two-finger pan
        const d0 = Math.hypot(g.pts[0].x - g.pts[1].x, g.pts[0].y - g.pts[1].y) || 1;
        const d1 = Math.hypot(pts[0].x - pts[1].x, pts[0].y - pts[1].y) || 1;
        const m0 = { x: (g.pts[0].x + g.pts[1].x) / 2, y: (g.pts[0].y + g.pts[1].y) / 2 };
        const m1 = { x: (pts[0].x + pts[1].x) / 2, y: (pts[0].y + pts[1].y) / 2 };
        const minW = 1500, maxW = Math.max(this._fitW * 4, 5000);
        const nw = clamp(g.vb.w * (d0 / d1), minW, maxW);
        const nh = g.vb.h * (nw / g.vb.w);
        const wx = g.vb.x + (m0.x / cw) * g.vb.w, wy = g.vb.y + (m0.y / ch) * g.vb.h;
        this._vb = { x: wx - (m1.x / cw) * nw, y: wy - (m1.y / ch) * nh, w: nw, h: nh };
      } else return;
      this._scheduleApply();
    });

    const end = (e) => {
      if (!this._pointers.has(e.pointerId)) return;
      const p = local(e);
      this._pointers.delete(e.pointerId);
      if (this._pointers.size) { startGesture(); return; } // pinch -> pan continues with remaining finger
      svg.classList.remove('drag');
      this._gs = null;
      const tap = this._tap;
      this._tap = null;
      if (e.type !== 'pointerup' || !tap || tap.moved || Date.now() - tap.t > 600) return;
      this._onTapRaw(p.x, p.y);
    };
    svg.addEventListener('pointerup', end);
    svg.addEventListener('pointercancel', end);
  }

  // Single tap is delayed briefly so a double tap can reset the view instead.
  _onTapRaw(sx, sy) {
    const now = Date.now();
    const last = this._lastTap;
    if (last && now - last.t < DOUBLE_TAP_MS * 1.3 && Math.hypot(sx - last.x, sy - last.y) < 30) {
      clearTimeout(this._tapTimer);
      this._lastTap = null;
      this._fit();
      return;
    }
    this._lastTap = { x: sx, y: sy, t: now };
    clearTimeout(this._tapTimer);
    this._tapTimer = setTimeout(() => { this._lastTap = null; this._onTap(sx, sy); }, DOUBLE_TAP_MS);
  }

  _onTap(sx, sy) {
    const d = this._data;
    if (!d || !this._vb) return;
    const u = this._vb.w / this._size.w;
    const [wx, wy] = this._toWorld(sx, sy);
    // 1) avoid points (screen-space radius >= 20 px)
    if (this._layers.avoid) {
      let best = null, bestD = Infinity;
      for (const p of d.avoid_points || []) {
        const dist = Math.hypot(wx - p.x, wy + p.y);
        if (dist < bestD) { bestD = dist; best = p; }
      }
      if (best && bestD <= Math.max(500, 20 * u)) { this._askDeleteAvoid(best); return; }
    }
    // 2) zone labels
    const zones = (d.zones || []).filter((z) => isFinite(z.x) && isFinite(z.y));
    for (const z of zones) {
      if (Math.hypot(wx - z.x, wy + z.y) <= 30 * u) { this._askMowZone(z); return; }
    }
    // 3) inside lawn (outside islands) -> nearest zone anchor
    const g = this._geo;
    if (!g || !zones.length || !pointInPoly(wx, wy, g.boundaryRaw)) return;
    if (g.islands.some((i) => pointInPoly(wx, wy, i.raw))) return;
    let nz = zones[0], nd = Infinity;
    for (const z of zones) {
      const dist = Math.hypot(wx - z.x, wy + z.y);
      if (dist < nd) { nd = dist; nz = z; }
    }
    this._askMowZone(nz);
  }

  // ------------------------------------------------------------ dialogs
  _dialog({ title, text, buttons, sheet }) {
    return new Promise((resolve) => {
      const ov = this.$('.ov');
      if (this._dlgResolve) this._dlgResolve(null);
      ov.className = 'ov show' + (sheet ? ' sheet' : '');
      ov.innerHTML = `<div class="dlg" role="dialog" aria-modal="true">
        <div class="dt">${esc(title)}</div>${text ? `<div class="dx">${esc(text)}</div>` : ''}
        <div class="db">${buttons.map((b, i) => `<button data-i="${i}" class="${b.cls || ''}">${esc(b.label)}</button>`).join('')}</div></div>`;
      const close = (v) => {
        this._dlgResolve = null;
        ov.className = 'ov';
        ov.innerHTML = '';
        ov.onclick = null;
        resolve(v);
      };
      this._dlgResolve = close;
      ov.onclick = (e) => {
        const b = e.target.closest('button');
        if (b) close(buttons[Number(b.dataset.i)].value);
        else if (e.target === ov) close(null);
      };
      const primary = ov.querySelector('button.pri, button.dan');
      if (primary) primary.focus({ preventScroll: true });
    });
  }

  _confirm(title, text, okLabel, danger) {
    return this._dialog({
      title, text,
      buttons: [
        { label: this._t('cancel'), value: false },
        { label: okLabel || this._t('confirm'), value: true, cls: danger ? 'dan' : 'pri' },
      ],
    }).then((v) => v === true);
  }

  async _askDeleteAvoid(p) {
    const ok = await this._confirm(this._t('deleteAvoid', p.mssid), this._t('deleteAvoidText'), this._t('delete'), true);
    if (!ok) return;
    if (await this._call('ecovacs_mower', 'delete_avoid_point', { mssid: p.mssid })) {
      setTimeout(() => this._fetch(), 2000);
    }
  }

  async _askMowZone(z) {
    const name = this._zoneName(z);
    const v = await this._dialog({
      title: name, sheet: true,
      buttons: [
        { label: this._t('cancel'), value: false },
        { label: this._t('mowZone'), value: true, cls: 'pri' },
      ],
    });
    if (v !== true) return;
    if (!(await this._confirm(this._t('confirmMowZone', name)))) return;
    const id = Number(z.id);
    await this._call('ecovacs_mower', 'mow_area', { area_ids: [isFinite(id) ? id : z.id] });
  }

  async _onToolbar(act) {
    if (act === 'fit') { this._fit(); return; }
    if (act === 'refresh') {
      if (await this._call('ecovacs_mower', 'refresh_map', {})) setTimeout(() => this._fetch(), 2000);
      return;
    }
    const map = {
      start: ['confirmStart', 'start_mowing'],
      pause: ['confirmPause', 'pause'],
      dock: ['confirmDock', 'dock'],
    }[act];
    if (!map) return;
    if (await this._confirm(this._t(map[0]))) await this._call('lawn_mower', map[1], {});
  }

  async _call(domain, service, data) {
    if (!this._hass) return false;
    try {
      await this._hass.callService(domain, service, { entity_id: this._config.entity, ...data });
      return true;
    } catch (e) {
      this._toast(`${this._t('callError')}: ${(e && e.message) || e}`, true);
      return false;
    }
  }
}

if (!customElements.get('ecovacs-goat-map-card')) {
  customElements.define('ecovacs-goat-map-card', EcovacsGoatMapCard);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: 'ecovacs-goat-map-card',
    name: 'Ecovacs GOAT map',
    description: 'Interactive lawn map for ECOVACS GOAT mowers: zones, avoid points, edges, live position.',
    preview: false,
  });
  console.info(`%c ECOVACS-GOAT-MAP-CARD %c v${CARD_VERSION} `, 'background:#43a047;color:#fff', 'background:#eee;color:#333');
}
