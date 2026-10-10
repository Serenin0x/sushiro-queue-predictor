/** Framework-neutral public statistics adapter. No ticket or upstream requests. */
export class StatisticsError extends Error {
  constructor(code, status = null) {
    super(code);
    this.name = "StatisticsError";
    this.code = code;
    this.httpStatus = status;
  }
}

const fail = code => { throw new StatisticsError(code); };
const object = v => v !== null && typeof v === "object" && !Array.isArray(v);
const integer = v => Number.isSafeInteger(v) && v >= 0;
const store = v => typeof v === "string" && /^[1-9][0-9]{0,9}$/.test(v);
const stamp = v => typeof v === "string" && Number.isFinite(Date.parse(v));
const queues = new Set(["mixedQueue", "reservationQueue"]);
const sources = new Set(["crm_remote_v1_1", "sapi_miniapp_gateway"]);
const officialSuccess = "one_official_detail_response_not_two_query_pair";
const freeze = v => {
  if (v && typeof v === "object") { Object.values(v).forEach(freeze); Object.freeze(v); }
  return v;
};
function monthValue(v) {
  if (typeof v !== "string" || !/^20[0-9]{2}-(0[1-9]|1[0-2])$/.test(v)) fail("invalid_month");
  return v;
}
function dateValue(v) {
  if (typeof v !== "string" || !/^20[0-9]{2}-[0-9]{2}-[0-9]{2}$/.test(v)) fail("invalid_date");
  const d = new Date(`${v}T00:00:00Z`);
  if (!Number.isFinite(d.getTime()) || d.toISOString().slice(0, 10) !== v) fail("invalid_date");
  return v;
}
function list(v, allowed = null) {
  if (!Array.isArray(v) || v.some(s => !store(s) || allowed && !allowed.includes(s)) || new Set(v).size !== v.length) fail("invalid_directory");
  return v;
}
function summary(v, id, date) {
  if (!object(v) || v.store_id !== id || v.local_date !== date) fail("summary_scope_mismatch");
  for (const k of ["observations", "successful_pairs", "failed_pairs", "scheduled_pause_slots", "expected_background_slots_so_far", "observed_background_slots"]) {
    if (!integer(v[k])) fail("invalid_summary");
  }
  const fraction = v.observed_slot_fraction_so_far;
  if (fraction !== null && !(Number.isFinite(fraction) && fraction >= 0 && fraction <= 1)) fail("invalid_summary");
  if (v.last_observation_at !== null && !stamp(v.last_observation_at)) fail("invalid_summary");
}
export function validateMonth(value, month, {source = "crm_remote_v1_1"} = {}) {
  if (!sources.has(source)) fail("invalid_source");
  monthValue(month);
  if (!object(value) || value.daily_schema_version !== 1 || value.source !== source || value.month !== month || value.network_performed_by_read !== false || value.eta_available !== false || value.heatmap_semantics !== "coverage_only_traffic_not_calibrated") fail("month_scope_mismatch");
  if (source === "sapi_miniapp_gateway" && value.api_profile !== "miniapp_gateway") fail("month_scope_mismatch");
  const ids = list(value.configured_store_ids);
  if (ids.length < 1 || ids.length > 256) fail("invalid_directory");
  if (!object(value.store_names) || Object.keys(value.store_names).length !== ids.length || ids.some(id => typeof value.store_names[id] !== "string")) fail("invalid_directory");
  list(value.unavailable_store_ids, ids);
  if (!object(value.days) || Object.keys(value.days).length > 31) fail("invalid_month_days");
  for (const [date, rows] of Object.entries(value.days)) {
    if (!dateValue(date).startsWith(`${month}-`) || !object(rows)) fail("month_scope_mismatch");
    for (const [id, row] of Object.entries(rows)) {
      if (!ids.includes(id)) fail("summary_scope_mismatch");
      summary(row, id, date);
      if (source === "sapi_miniapp_gateway" && row.success_semantics !== officialSuccess) fail("invalid_summary");
    }
  }
  if (value.calendar_index_state !== undefined) {
    const pending = list(value.calendar_pending_store_ids, value.unavailable_store_ids);
    if (value.calendar_index_state !== (pending.length ? "preparing" : "ready") || value.calendar_verified_store_count !== ids.length - pending.length) fail("invalid_loading_state");
  }
  return value;
}
export function validateDay(value, id, date, {source = "crm_remote_v1_1"} = {}) {
  if (!sources.has(source)) fail("invalid_source");
  dateValue(date);
  if (!object(value) || value.daily_schema_version !== 1 || value.source !== source || value.requested_store_id !== id || value.local_date !== date || value.network_performed_by_read !== false || value.eta_available !== false || value.first_label_is_confirmed_call !== false || value.call_reference_semantics !== "user_assumed_first_displayed_label") fail("day_scope_mismatch");
  if (source === "sapi_miniapp_gateway" && (value.api_profile !== "miniapp_gateway" || value.summary !== null && value.summary.success_semantics !== officialSuccess)) fail("day_scope_mismatch");
  if (!Array.isArray(value.points) || value.points.length > 2048 || value.returned_graph_points !== value.points.length || typeof value.graph_truncated !== "boolean") fail("invalid_points");
  if (value.summary !== null) summary(value.summary, id, date);
  for (const p of value.points) {
    if (source === "sapi_miniapp_gateway" && p.success_semantics !== officialSuccess) fail("invalid_point");
    if (!object(p) || !stamp(p.request_started_at) || typeof p.pair_ok !== "boolean" || typeof p.scheduled_pause !== "boolean" || !object(p.error_codes)) fail("invalid_point");
    for (const key of ["queue_received_at", "count_received_at"]) if (p[key] !== null && !stamp(p[key])) fail("invalid_point");
    if (p.count_raw !== null && !integer(p.count_raw)) fail("invalid_point");
    if (p.queues !== null) {
      if (!object(p.queues) || p.queue_received_at === null) fail("invalid_point");
      for (const q of queues) if (!Array.isArray(p.queues[q]) || p.queues[q].length > 3 || p.queues[q].some(n => typeof n !== "string" || n.length > 128)) fail("invalid_point");
    }
    if (p.comparison_state === "comparable_display_sets" && (!(Number.isFinite(p.interval_seconds) && p.interval_seconds > 0) || !object(p.removed_labels) || [...queues].some(q => !integer(p.removed_labels[q])))) fail("invalid_comparison");
  }
  return value;
}

export function createStatisticsClient({fetchImpl = globalThis.fetch, timeoutMs = 15000, source = "crm_remote_v1_1"} = {}) {
  if (!sources.has(source)) fail("invalid_source");
  const prefix = source === "sapi_miniapp_gateway" ? "/api/v1/official" : "/api/v1";
  if (typeof fetchImpl !== "function" || !integer(timeoutMs) || timeoutMs < 1 || timeoutMs > 60000) fail("invalid_client_options");
  async function read(path, externalSignal, limit) {
    if (externalSignal?.aborted) fail("read_cancelled");
    const controller = new AbortController();
    const cancel = () => controller.abort();
    if (externalSignal?.aborted) controller.abort();
    else externalSignal?.addEventListener("abort", cancel, {once: true});
    let expired = false;
    const timer = setTimeout(() => { expired = true; controller.abort(); }, timeoutMs);
    try {
      const response = await fetchImpl(path, {method: "GET", cache: "no-store", signal: controller.signal});
      if (controller.signal.aborted) fail(expired ? "read_timeout" : "read_cancelled");
      if (!response.ok) throw new StatisticsError("http_error", response.status);
      const body = await response.text();
      if (controller.signal.aborted) fail(expired ? "read_timeout" : "read_cancelled");
      if (body.length > limit || new TextEncoder().encode(body).length > limit) fail("response_too_large");
      try { return JSON.parse(body); } catch (_) { fail("invalid_json"); }
    } catch (e) {
      if (e instanceof StatisticsError) throw e;
      throw new StatisticsError(expired ? "read_timeout" : controller.signal.aborted ? "read_cancelled" : "read_failed");
    } finally {
      clearTimeout(timer);
      externalSignal?.removeEventListener("abort", cancel);
    }
  }
  return Object.freeze({
    async readMonth(month, {signal} = {}) {
      monthValue(month);
      return freeze(validateMonth(await read(`${prefix}/months/${month}`, signal, 8 * 1024 * 1024), month, {source}));
    },
    async readDay(id, date, directory, {signal} = {}) {
      dateValue(date); list(directory);
      if (!store(id) || !directory.includes(id)) fail("unknown_store");
      return freeze(validateDay(await read(`${prefix}/stores/${id}/days/${date}`, signal, 2 * 1024 * 1024), id, date, {source}));
    }
  });
}

/** Month and day changes supersede old requests; queue changes only re-render. */
export function createStatisticsController({client, onChange = () => {}, pollMs = 30000, setIntervalImpl = setInterval, clearIntervalImpl = clearInterval} = {}) {
  if (!client?.readMonth || !client?.readDay || typeof onChange !== "function" || !integer(pollMs) || pollMs < 30000 || pollMs > 3600000) fail("invalid_controller_options");
  let revision = 0, active = null, task = null, interval = null;
  let state = freeze({selection: null, phase: "idle", index: null, detail: null, detailState: "unselected", error: null});
  const emit = patch => { state = freeze({...state, ...patch}); onChange(state); };
  async function run(token, signal) {
    const {month, date, storeId} = state.selection;
    try {
      const index = await client.readMonth(month, {signal});
      if (token !== revision) return;
      emit({index});
      if (token !== revision) return;
      if (storeId !== null && !index.configured_store_ids.includes(storeId)) fail("unknown_store");
      let detail = null;
      if (storeId !== null) {
        try {
          detail = await client.readDay(storeId, date, index.configured_store_ids, {signal});
          if (detail.source !== index.source) fail("day_scope_mismatch");
        }
        catch (e) {
          if (token === revision) emit({phase: "partial", detail: null, detailState: "error", error: {code: e.code || "read_failed", httpStatus: e.httpStatus ?? null}});
          return;
        }
      }
      if (token !== revision) return;
      emit({phase: index.unavailable_store_ids.length ? "partial" : "ready", detail, detailState: storeId === null ? "unselected" : "ready", error: null});
    } catch (e) {
      if (token === revision) emit({phase: "error", index: null, detail: null, detailState: "error", error: {code: e.code || "read_failed", httpStatus: e.httpStatus ?? null}});
    }
  }
  function refresh() {
    if (!state.selection) fail("selection_required");
    if (task) return task;
    const token = ++revision;
    active = new AbortController();
    emit({phase: "loading", detail: null, detailState: state.selection.storeId === null ? "unselected" : "loading", error: null});
    task = run(token, active.signal).finally(() => { if (token === revision) { task = null; active = null; } });
    return task;
  }
  return Object.freeze({
    snapshot: () => state,
    select(selection) {
      if (!object(selection) || Object.keys(selection).some(k => !["month", "date", "storeId", "queue"].includes(k))) fail("invalid_selection");
      const next = {month: monthValue(selection.month), date: dateValue(selection.date), storeId: selection.storeId ?? null, queue: selection.queue ?? "mixedQueue"};
      if (!next.date.startsWith(`${next.month}-`) || next.storeId !== null && !store(next.storeId) || !queues.has(next.queue)) fail("invalid_selection");
      const old = state.selection;
      if (old && old.month === next.month && old.date === next.date && old.storeId === next.storeId) { emit({selection: next}); return task ?? Promise.resolve(); }
      ++revision; active?.abort(); task = null; active = null;
      emit({selection: next, phase: "idle", index: null, detail: null, detailState: "unselected", error: null});
      return refresh();
    },
    refresh,
    start() { if (interval === null) interval = setIntervalImpl(() => { if (state.selection) refresh(); }, pollMs); },
    stop() { if (interval !== null) clearIntervalImpl(interval); interval = null; ++revision; active?.abort(); active = null; task = null; emit({phase: "idle", detail: null, detailState: "unselected", error: null}); }
  });
}

export function shanghaiDate(now = Date.now()) {
  const parts = new Intl.DateTimeFormat("en-CA", {timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit"}).formatToParts(new Date(now));
  const v = Object.fromEntries(parts.map(p => [p.type, p.value]));
  return `${v.year}-${v.month}-${v.day}`;
}

export function calendarCells(index, month, storeId = null) {
  validateMonth(index, month);
  if (storeId !== null && !index.configured_store_ids.includes(storeId)) fail("unknown_store");
  const ids = storeId === null ? index.configured_store_ids : [storeId];
  const pending = index.calendar_pending_store_ids || [];
  const n = new Date(Date.UTC(Number(month.slice(0, 4)), Number(month.slice(5)), 0)).getUTCDate();
  return Array.from({length: n}, (_, i) => {
    const date = `${month}-${String(i + 1).padStart(2, "0")}`;
    const rows = ids.map(id => index.days[date]?.[id]).filter(Boolean);
    const expected = rows.reduce((v, r) => v + r.expected_background_slots_so_far, 0);
    const observed = rows.reduce((v, r) => v + r.observed_background_slots, 0);
    const incomplete = ids.some(id => index.unavailable_store_ids.includes(id));
    return freeze({date, weekdayMondayFirst: (new Date(`${date}T00:00:00Z`).getUTCDay() + 6) % 7,
      observations: rows.length ? rows.reduce((v, r) => v + r.observations, 0) : null,
      observedStoreCount: rows.length, configuredStoreCount: ids.length,
      coverage: expected ? Math.min(1, observed / expected) : null,
      state: ids.some(id => pending.includes(id)) ? "pending" : incomplete ? "unavailable" : rows.length ? "observed" : "no_data",
      coverageIsPartial: incomplete || rows.length !== ids.length, heatmapSemantics: "coverage_only_traffic_not_calibrated"});
  });
}

/** Last response only: a failed final sample must not revive older labels. */
export function latestQueue(detail, queue, {now = Date.now(), today = shanghaiDate(now), maxAgeSeconds = 90} = {}) {
  if (!queues.has(queue) || !Number.isFinite(now) || !Number.isFinite(maxAgeSeconds) || maxAgeSeconds < 0) fail("invalid_queue_options");
  const p = detail?.points?.at(-1);
  if (!p || p.scheduled_pause || !p.queues || !stamp(p.queue_received_at)) return freeze({state: "unavailable", labels: null, observedAt: null, isConfirmedCall: false});
  const ageSeconds = (now - Date.parse(p.queue_received_at)) / 1000;
  const state = ageSeconds < 0 ? "unavailable" : detail.local_date !== today ? "historical" : ageSeconds > maxAgeSeconds ? "stale" : "fresh";
  return freeze({state, labels: [...p.queues[queue]], observedAt: p.queue_received_at, ageSeconds, isEmpty: p.queues[queue].length === 0, isConfirmedCall: false});
}

/** Separate segments prevent lines across gaps, resets, failures and run changes. */
export function chartSeries(detail, queue) {
  if (!queues.has(queue)) fail("unsupported_queue");
  const reference = [], turnover = [], rawCount = [];
  let segment = null, previous = null;
  for (const p of detail?.points || []) {
    const at = stamp(p.queue_received_at) ? Date.parse(p.queue_received_at) : null;
    const label = p.queues?.[queue]?.[0];
    const position = typeof label === "string" && /^[0-9]{1,7}$/.test(label) ? Number(label) : null;
    if (!p.scheduled_pause && at !== null && position !== null) {
      if (!previous || p.comparison_state !== "comparable_display_sets" || at <= previous.at || position < previous.position) { segment = []; reference.push(segment); }
      segment.push({at, position, label}); previous = {at, position};
    } else { previous = null; segment = null; }
    if (!p.scheduled_pause && p.queues && at !== null && p.comparison_state === "comparable_display_sets" && Number.isFinite(p.interval_seconds) && p.interval_seconds > 0 && integer(p.removed_labels?.[queue])) turnover.push({at, value: p.removed_labels[queue] * 60 / p.interval_seconds});
    if (!p.scheduled_pause && stamp(p.count_received_at) && integer(p.count_raw)) rawCount.push({at: Date.parse(p.count_received_at), value: p.count_raw});
  }
  return freeze({reference, turnover, rawCount, graphTruncated: detail?.graph_truncated ?? null,
    referenceSemantics: "user_assumed_first_displayed_label", turnoverUnit: "display_labels_per_minute", countUnit: "unknown", actualCalledCount: null, noShowRate: null});
}
