/* Experimental first-display-position baseline. No network, storage or real call claims. */
(function (root) {
  "use strict";
  const POLICY = "first_position_experiment_v1", HISTORY_POLICY = "first_position_history_live_experiment_v2", MAX_POINTS = 2048;
  const WINDOWS = [10, 20, 30], MIN_SPAN = 5, MAX_GAP = 120000, MAX_AGE = 90000;
  const FIELDS = {ordinary: "mixedQueue", reservation: "reservationQueue"};
  const OFFICIAL = "sapi_miniapp_gateway", SINGLE = "one_official_detail_response_not_two_query_pair";
  const requestedSource = options => options?.source === undefined ? "crm_remote_v1_1" : options.source;
  function sourceMatches(detail, source) {
    return ["crm_remote_v1_1", OFFICIAL].includes(source) && detail?.source === source &&
      (source !== OFFICIAL || detail.api_profile === "miniapp_gateway" &&
        (detail.summary === null || detail.summary?.success_semantics === SINGLE));
  }
  function stamp(value) {
    if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)$/.test(value)) return NaN;
    const ms = Date.parse(value);
    return Number.isFinite(ms) && new Date(ms).toISOString().slice(0, 19) === value.slice(0, 19) ? ms : NaN;
  }
  function localDay(ms) { return Number.isFinite(ms) ? new Date(ms + 28800000).toISOString().slice(0, 10) : null; }
  function number(label) { return typeof label === "string" && /^[0-9]{1,7}$/.test(label) ? Number(label) : null; }
  function median(values) {
    const sorted = values.slice().sort((a, b) => a - b), m = Math.floor(sorted.length / 2);
    return sorted.length % 2 ? sorted[m] : (sorted[m - 1] + sorted[m]) / 2;
  }
  function point(row, field, day, source) {
    const at = stamp(row?.queue_received_at), start = stamp(row?.request_started_at);
    const labels = row?.queues?.[field], position = number(labels?.[0]);
    if (!Number.isFinite(at) || !Number.isFinite(start) || start > at || localDay(at) !== day ||
        row.pair_ok !== true || row.scheduled_pause !== false || position === null ||
        source === OFFICIAL && row.success_semantics !== SINGLE ||
        !Array.isArray(labels) || labels.length > 3 || labels.some(v => number(v) === null) ||
        row.error_codes && Object.keys(row.error_codes).length) return null;
    return {at, position};
  }
  function connected(row, previous, current) {
    return row.comparison_state === "comparable_display_sets" && current.at > previous.at &&
      current.at - previous.at <= MAX_GAP && current.position >= previous.position;
  }
  function slope(points) {
    const slopes = [];
    for (let i = 0; i < points.length; i++) for (let j = i + 1; j < points.length; j++)
      slopes.push((points[j].position - points[i].position) * 60000 / (points[j].at - points[i].at));
    return median(slopes);
  }
  function unavailable(reason, extra) {
    return Object.assign({policy: POLICY, available: false, reason, eta_available: false,
      actual_call_verified: false, calibrated_interval: false}, extra);
  }
  function currentContext(detail, options) {
    const {storeId, queueType, ticket, issuedAt, now, confirmedScope} = options || {}, field = FIELDS[queueType];
    if (!field) return unavailable("unsupported_queue");
    if (!Number.isFinite(now) || localDay(now) !== detail?.local_date) return unavailable("today_only");
    const source = requestedSource(options);
    if (detail?.daily_schema_version !== 1 || !sourceMatches(detail, source) ||
        detail.requested_store_id !== storeId || detail.network_performed_by_read !== false ||
        detail.eta_available !== false || detail.first_label_is_confirmed_call !== false ||
        detail.call_reference_semantics !== "user_assumed_first_displayed_label") return unavailable("scope_mismatch");
    if (!Array.isArray(detail.points) || detail.points.length > MAX_POINTS) return unavailable("invalid_projection");
    if (!confirmedScope) return unavailable("confirm_ticket_scope");
    const target = number(ticket), issued = stamp(issuedAt);
    if (target === null) return unavailable("numeric_ticket_required");
    if (!Number.isFinite(issued) || localDay(issued) !== detail.local_date || issued > now) return unavailable("issue_time_required");
    const generated = stamp(detail.generated_at);
    if (!Number.isFinite(generated) || generated > now) return unavailable("future_or_invalid_clock");
    let segment = [], boundary = null, reversalAt = null, lastSeen = null;
    for (const row of detail.points) {
      const current = point(row, field, detail.local_date, source), previous = segment[segment.length - 1];
      if (!current) {segment = []; boundary = "invalid_or_failed_sample"; continue;}
      if (current.at > now || current.at > generated) return unavailable("future_or_invalid_clock");
      if (lastSeen && current.position < lastSeen.position) reversalAt = current.at;
      lastSeen = current;
      if (previous && !connected(row, previous, current)) {
        boundary = current.position < previous.position ? "reference_reversed" : "sampling_boundary";
        if (current.position < previous.position) reversalAt = current.at;
        segment = [];
      }
      segment.push(current);
    }
    const last = segment[segment.length - 1];
    if (!last) return unavailable("no_current_sample", {boundary});
    const age = now - last.at;
    if (age > MAX_AGE) return unavailable("stale_sample", {sample_at: new Date(last.at).toISOString(), sample_age_seconds: age / 1000});
    if (issued > last.at) return unavailable("awaiting_post_issue_sample");
    if (reversalAt !== null && issued <= reversalAt) return unavailable("ticket_cycle_uncertain");
    if (target <= last.position) return unavailable("reference_at_or_beyond_ticket", {reference: last.position});
    // A reported issue time establishes the user's assumption, not a verified ticket cycle.
    if (segment.some(p => p.at >= issued && p.position >= target)) return unavailable("ticket_cycle_uncertain");
    segment = segment.filter(p => p.at >= last.at - 30 * 60000);
    if (segment.length > 64) return unavailable("sampling_density_unsupported");
    return {segment, last, age, issued, gap: target - last.position, boundary, field, source};
  }
  function forecast(detail, options) {
    const context = currentContext(detail, options);
    if (context.reason) return context;
    const {segment, last, age, gap, boundary, field, source} = context, now = options.now, candidates = [];
    for (const windowMinutes of WINDOWS) {
      const rows = segment.filter(p => p.at >= last.at - windowMinutes * 60000);
      if (rows.length < 6 || (last.at - rows[0].at) / 60000 < MIN_SPAN) continue;
      const robust = slope(rows), endpoint = (last.position - rows[0].position) * 60000 / (last.at - rows[0].at);
      for (const [method, rate] of [["pairwise_median", robust], ["endpoint", endpoint]]) {
        if (Number.isFinite(rate) && rate > 0) candidates.push({window_minutes: windowMinutes, method,
          rate_positions_per_minute: rate, minutes_from_sample: gap / rate});
      }
    }
    if (!candidates.some(c => c.method === "pairwise_median")) return unavailable(
      source === OFFICIAL && boundary === "sampling_boundary" ? "sampling_continuity_unavailable" :
        "insufficient_positive_trend", {boundary});
    const previous = segment[segment.length - 2], lastRate = previous ?
      (last.position - previous.position) * 60000 / (last.at - previous.at) : 0;
    const typical = median(candidates.map(c => c.rate_positions_per_minute));
    const accelerating = lastRate > Math.max(20, typical * 3);
    const stalled = lastRate === 0;
    if (accelerating) candidates.push({window_minutes: (last.at - previous.at) / 60000,
      method: "latest_jump_scenario", rate_positions_per_minute: lastRate, minutes_from_sample: gap / lastRate});
    const central = median(candidates.filter(c => c.method === "pairwise_median").map(c => c.minutes_from_sample));
    const slowest = Math.max(...candidates.map(c => c.minutes_from_sample));
    if (slowest > 120 || central <= age / 60000) return unavailable("forecast_horizon_unsupported", {boundary});
    const earliest = Math.min(...candidates.map(c => c.minutes_from_sample));
    return {policy: POLICY, source, available: true, eta_available: false, actual_call_verified: false,
      calibrated_interval: false, target_semantics: "first_display_position_reaches_target",
      ticket_cycle_verified: false, queue_field: field, sample_at: new Date(last.at).toISOString(),
      sample_age_seconds: age / 1000, reference: last.position, position_difference: gap,
      sample_count: segment.length, continuous_minutes: (last.at - segment[0].at) / 60000,
      reference_time: new Date(last.at + central * 60000).toISOString(),
      scenario_earliest: new Date(Math.max(now, last.at + earliest * 60000)).toISOString(),
      scenario_latest: new Date(last.at + slowest * 60000).toISOString(),
      recent_rate_positions_per_minute: lastRate, typical_rate_positions_per_minute: typical,
      acceleration_signal: accelerating, latest_interval_stalled: stalled, boundary, candidates};
  }
  // These are interval bounds for an empirical reference-position distribution, not actual-call calibration.
  function endpointQuantile(samples, endpoint, q) {
    const rows = samples.filter(s => s[endpoint] !== null).slice().sort((a, b) => a[endpoint] - b[endpoint]);
    let mass = 0;
    for (const row of rows) {mass += row.weight; if (mass + 1e-12 >= q) return row[endpoint];}
    return null; // Censored upper endpoints retain their mass at an unknown time.
  }
  function calendarClass(detail) {
    const date = Date.parse(`${detail.local_date}T00:00:00Z`), month = new Date(date).getUTCMonth() + 1;
    const raw = detail.summary?.date_type;
    return {type: ["ordinary_workday", "ordinary_weekend", "holiday", "makeup_workday", "unknown"].includes(raw) ? raw : "unknown",
      weekday: new Date(date).getUTCDay(), month, season: Math.floor((month % 12) / 3)};
  }
  function minuteOfDay(at) {return ((at + 28800000) % 86400000) / 60000;}
  function historySamples(detail, options, context, days) {
    const diagnostics = {state: "insufficient_history", received_days: Array.isArray(days) ? days.length : 0,
      accepted_days: 0, excluded_days: 0, selected_days: 0, samples: 0, interval_observed: 0,
      right_censored: 0, boundary_censored: 0, cohort: null, actual_call_verified: false};
    if (!Array.isArray(days) || days.length > 7) return {diagnostics};
    const currentClass = calendarClass(detail), valid = [], seen = new Set();
    for (const past of days) {
      const dateAt = Date.parse(`${past?.local_date}T00:00:00Z`), ageDays =
        (Date.parse(`${detail.local_date}T00:00:00Z`) - dateAt) / 86400000;
      const generated = stamp(past?.generated_at);
      if (!Number.isInteger(ageDays) || ageDays < 1 || ageDays > 31 || localDay(dateAt - 28800000) !== past.local_date ||
          seen.has(past.local_date) || past.requested_store_id !== options.storeId || past.daily_schema_version !== 1 ||
          !sourceMatches(past, context.source) || past.network_performed_by_read !== false || past.eta_available !== false ||
          past.first_label_is_confirmed_call !== false || past.call_reference_semantics !== detail.call_reference_semantics ||
          past.graph_truncated !== false || !Array.isArray(past.points) || past.points.length > MAX_POINTS ||
          !Number.isFinite(generated) || generated > options.now ||
          past.points.some(r => Number.isFinite(stamp(r?.queue_received_at)) && stamp(r.queue_received_at) > generated)) {
        diagnostics.excluded_days++; continue;
      }
      seen.add(past.local_date); diagnostics.accepted_days++;
      const cls = calendarClass(past), buckets = new Set();
      for (let i = 0; i < past.points.length; i++) {
        const origin = point(past.points[i], context.field, past.local_date, context.source);
        if (!origin || origin.position + context.gap > 9999999) continue;
        const bucket = Math.floor(minuteOfDay(origin.at) / 120);
        if (buckets.has(bucket)) continue;
        buckets.add(bucket); // Choose origins on a fixed time grid, before looking at their outcomes.
        let previous = origin, lower = 0, upper = null, state = "right_censored";
        for (let j = i + 1; j < past.points.length; j++) {
          const next = point(past.points[j], context.field, past.local_date, context.source);
          if (!next || !connected(past.points[j], previous, next)) {state = "boundary_censored"; break;}
          if (next.at - origin.at > 120 * 60000) break;
          if (next.position >= origin.position + context.gap) {
            upper = (next.at - origin.at) / 60000; state = "interval_observed"; break;
          }
          previous = next; lower = (previous.at - origin.at) / 60000;
        }
        valid.push({day: past.local_date, cls, near: Math.abs(minuteOfDay(origin.at) - minuteOfDay(context.last.at)) <= 90,
          lower, upper, state});
      }
    }
    // Holiday names are not present in this projection; do not pool different months as one holiday.
    const sameType = s => s.cls.type === currentClass.type &&
      (currentClass.type !== "holiday" || s.cls.month === currentClass.month);
    const cohorts = [
      ["weekday_month_time", s => sameType(s) && s.cls.weekday === currentClass.weekday && s.cls.month === currentClass.month && s.near],
      ["date_type_season_time", s => sameType(s) && s.cls.season === currentClass.season && s.near],
      ["date_type_time", s => sameType(s) && s.near],
      ["date_type_all_hours", sameType]
    ];
    for (const [name, select] of cohorts) {
      const selected = valid.filter(select), dates = new Set(selected.map(s => s.day));
      if (selected.length < 3 || dates.size < 2) continue;
      const counts = new Map(); selected.forEach(s => counts.set(s.day, (counts.get(s.day) || 0) + 1));
      selected.forEach(s => {s.weight = 1 / (dates.size * counts.get(s.day)); diagnostics[s.state]++;});
      Object.assign(diagnostics, {state: "available", selected_days: dates.size, samples: selected.length, cohort: name});
      return {samples: selected, diagnostics};
    }
    return {diagnostics};
  }
  function forecastWithHistory(detail, options, days) {
    const context = currentContext(detail, options);
    if (context.reason) return context;
    if (detail.graph_truncated === true) return unavailable("truncated_current_projection");
    const live = forecast(detail, options), history = historySamples(detail, options, context, days);
    if (!history.samples) return Object.assign({}, live, {mode: "recent_only", history: history.diagnostics});
    const postIssue = context.segment.filter(p => p.at >= context.issued);
    const span = postIssue.length ? (context.last.at - postIssue[0].at) / 60000 : 0;
    let liveWeight = live.available ? Math.min(.85, span * .85 / 20) : 0;
    if (live.available && live.acceleration_signal) liveWeight = Math.max(.75, liveWeight);
    const samples = history.samples.map(s => ({lower: s.lower, upper: s.upper, weight: s.weight * (1 - liveWeight)}));
    if (liveWeight) samples.push({lower: (stamp(live.scenario_earliest) - context.last.at) / 60000,
      upper: (stamp(live.scenario_latest) - context.last.at) / 60000, weight: liveWeight});
    const bounds = {};
    for (const q of [.1, .5, .9]) bounds[String(q)] = {lower: endpointQuantile(samples, "lower", q), upper: endpointQuantile(samples, "upper", q)};
    const mid = bounds["0.5"];
    if (mid.upper === null) {
      const diagnostic = Object.assign({}, history.diagnostics, {state: "median_upper_unknown"});
      return Object.assign({}, live, {mode: "recent_only", history: diagnostic});
    }
    const central = (mid.lower + mid.upper) / 2;
    if (central <= context.age / 60000 || central > 120) return Object.assign({}, live,
      {mode: "recent_only", history: Object.assign({}, history.diagnostics, {state: "median_outside_horizon"})});
    const latest = bounds["0.9"].upper;
    return {policy: HISTORY_POLICY, source: context.source, available: true, eta_available: false, actual_call_verified: false,
      calibrated_interval: false, ticket_cycle_verified: false, target_semantics: "first_display_position_reaches_target",
      mode: liveWeight ? "history_live_mix" : "history_initial", history: history.diagnostics,
      live_weight: liveWeight, post_issue_observed_minutes: span, queue_field: context.field,
      sample_at: new Date(context.last.at).toISOString(), sample_age_seconds: context.age / 1000,
      sample_count: context.segment.length, continuous_minutes: (context.last.at - context.segment[0].at) / 60000,
      reference: context.last.position, position_difference: context.gap,
      reference_time: new Date(context.last.at + central * 60000).toISOString(),
      central_semantics: "midpoint_of_empirical_median_bounds",
      scenario_earliest: new Date(Math.max(options.now, context.last.at + bounds["0.1"].lower * 60000)).toISOString(),
      scenario_latest: latest === null ? null : new Date(context.last.at + latest * 60000).toISOString(),
      empirical_quantile_bounds_minutes_from_sample: bounds,
      acceleration_signal: live.available && live.acceleration_signal,
      latest_interval_stalled: live.available && live.latest_interval_stalled,
      boundary: context.boundary};
  }
  function replay(detail, options) {
    const {storeId, queueType, offset = 50, stride = 20} = options || {}, field = FIELDS[queueType];
    const source = requestedSource(options);
    if (!field || !Number.isInteger(offset) || offset < 1 || offset > 100000 ||
        !Number.isInteger(stride) || stride < 10 || stride > 2048 ||
        !Array.isArray(detail?.points) || detail.points.length > MAX_POINTS) return {policy: POLICY, error: "invalid_replay_request"};
    const cases = [];
    for (let i = 0; i < detail.points.length; i += stride) {
      const origin = point(detail.points[i], field, detail.local_date, source);
      if (!origin || origin.position + offset > 9999999) {cases.push({state: "invalid_origin"}); continue;}
      const cutoff = new Date(origin.at).toISOString(), prefix = Object.assign({}, detail,
        {generated_at: cutoff, points: detail.points.slice(0, i + 1)});
      const prediction = forecast(prefix, {source, storeId, queueType, ticket: String(origin.position + offset),
        issuedAt: cutoff, now: origin.at, confirmedScope: true});
      const record = {origin_at: cutoff, available: prediction.available, state: "unavailable", reason: prediction.reason};
      if (prediction.available) {
        record.predicted_reference_time = prediction.reference_time;
        record.state = "right_censored";
        let previous = origin;
        for (let j = i + 1; j < detail.points.length; j++) {
          const next = point(detail.points[j], field, detail.local_date, source);
          if (!next || !connected(detail.points[j], previous, next)) {record.state = "boundary_censored"; break;}
          if (next.at - origin.at > 120 * 60000) break;
          if (next.position >= origin.position + offset) {
            const predicted = stamp(prediction.reference_time);
            Object.assign(record, {state: "reference_crossing_observed", crossing_lower: new Date(previous.at).toISOString(),
              crossing_upper: new Date(next.at).toISOString(),
              scenario_overlaps_crossing_interval: stamp(prediction.scenario_earliest) <= next.at &&
                stamp(prediction.scenario_latest) >= previous.at,
              signed_error_to_interval_minutes: predicted < previous.at ? (predicted - previous.at) / 60000 :
                predicted > next.at ? (predicted - next.at) / 60000 : 0});
            break;
          }
          previous = next;
        }
      }
      cases.push(record);
    }
    return {policy: POLICY, target_semantics: "first_display_position_reaches_target", actual_call_verified: false,
      calibration_for_actual_wait: false, offset_positions: offset, stride_points: stride, cases};
  }
  const ARRIVAL_POLICY = 'display_top_two_or_all_three_passed_v3';
  function displayState(ticket, labels) {
    const target = number(ticket);
    if (target === null || target < 1) return 'unsupported_ticket';
    if (!Array.isArray(labels) || labels.length > 3 || labels.some(v => number(v) === null || number(v) < 1)) return 'unavailable';
    if (!labels.length) return 'empty_display';
    const values = labels.map(number);
    if (values.slice(0, 2).includes(target)) return 'display_top_two';
    if (values.length === 3 && values.every(v => v > target)) return 'display_all_three_passed';
    return values.includes(target) ? 'display_third' : 'not_reached';
  }
  function arrivalScope(detail, options) {
    const field = FIELDS[options?.queueType], source = requestedSource(options), target = number(options?.ticket), issued = stamp(options?.issuedAt);
    if (!field) return {reason:'unsupported_queue'};
    if (!Number.isFinite(options?.now)) return {reason:'future_or_invalid_clock'};
    if (!options?.confirmedScope) return {reason:'confirm_ticket_scope'};
    if (target === null || target < 1) return {reason:'numeric_ticket_required'};
    if (!Number.isFinite(issued) || issued > options.now || localDay(issued) !== detail?.local_date) return {reason:'issue_time_required'};
    if (!sourceMatches(detail, source) || detail?.daily_schema_version !== 1 || detail.requested_store_id !== options.storeId ||
        detail.network_performed_by_read !== false || detail.graph_truncated !== false || !Array.isArray(detail.points) || detail.points.length > MAX_POINTS) return {reason:'scope_mismatch'};
    const generated = stamp(detail.generated_at);
    if (!Number.isFinite(generated) || generated > options.now) return {reason:'future_or_invalid_clock'};
    return {field, source, target, issued, generated};
  }
  function arrivalPoint(row, context, day) {
    const at = stamp(row?.queue_received_at), started = stamp(row?.request_started_at), labels = row?.queues?.[context.field];
    if (!Number.isFinite(at) || !Number.isFinite(started) || started > at || at > context.generated || localDay(at) !== day ||
        row?.pair_ok !== true || row.scheduled_pause !== false || row.error_codes && Object.keys(row.error_codes).length ||
        context.source === OFFICIAL && (row.success_semantics !== SINGLE || row.queue_field_presence?.[context.field] !== 'present') ||
        !Array.isArray(labels) || !labels.length || labels.length > 3 || labels.some(v => number(v) === null || number(v) < 1)) return null;
    return {at, position:number(labels[0]), labels, row};
  }
  function arrivalConnected(a, b, maxGap) {
    if (!(b.at > a.at && b.at-a.at <= maxGap)) return false;
    if(b.position<a.position&&!(a.labels.length===3&&b.labels.length===3&&Math.max(...b.labels.map(number))>=Math.min(...a.labels.map(number))))return false;
    const link = b.row.continuity, before = a.row.continuity;
    if (link || before) return Boolean(link?.writer_id && link.writer_id === before?.writer_id &&
      link.predecessor_writer_id === before.writer_id && link.predecessor_id === a.row.observation_id);
    return b.row.comparison_state === 'comparable_display_sets';
  }
  function arrival(detail, options) {
    const c = arrivalScope(detail, options);
    if (c.reason) return {policy:ARRIVAL_POLICY, available:false, state:'invalid', reason:c.reason};
    const prior=detail.points.map(row=>arrivalPoint(row,c,detail.local_date)).filter(p=>p&&p.at<c.issued).at(-1);
    const conflict=Boolean(prior&&displayState(options.ticket,prior.labels)==='display_all_three_passed');
    let previous=null, negative=null, suspended=false, result={policy:ARRIVAL_POLICY, available:false, state:'waiting', quality_flags:[]};
    const flags=new Set();
    for (const row of detail.points) {
      const p=arrivalPoint(row,c,detail.local_date);
      if (p && p.at < c.issued) continue;
      if (!p) {negative=null;flags.add('observation_gap');result.state='awaiting_observation';continue;}
      if (previous && !arrivalConnected(previous,p,660000)) {negative=null;flags.add('sampling_boundary');}
      if (previous && p.position < previous.position) {
        const backfill=previous.labels.length===3&&p.labels.length===3&&Math.max(...p.labels.map(number))>=Math.min(...previous.labels.map(number));
        if(backfill)flags.add('display_position_backfill');
        else{negative=null;suspended=true;flags.add('number_reversal');}
      }
      previous=p;
      const state=displayState(options.ticket,p.labels);
      if(state==='display_all_three_passed'&&conflict){flags.add('display_passed_before_reported_issue');continue;}
      if (state==='display_top_two' || state==='display_all_three_passed' && !suspended) {
        const lower=negative?.at??c.issued;
        if (!negative) flags.add('no_adjacent_before_arrival_sample');
        return {policy:ARRIVAL_POLICY, available:true, state:state==='display_top_two'?'arrived':'inferred_arrived',
          label_kind:state, detected_at:new Date(p.at).toISOString(), lower_at:new Date(lower).toISOString(),
          upper_at:new Date(p.at).toISOString(), wait_lower_minutes:(lower-c.issued)/60000,
          wait_upper_minutes:(p.at-c.issued)/60000, display_numbers:p.labels.slice(),
          observation_id:row.observation_id??null, checked_in_reported:options.checkedIn??null,
          actual_call_verified:false, quality_flags:[...flags]};
      }
      result.state=suspended?'number_cycle_uncertain':state==='display_third'?'upcoming':'waiting';
      result.sample_at=new Date(p.at).toISOString();
      if (!suspended) negative=p;
    }
    if(conflict)Object.assign(result,{state:'input_conflict',reason:'ticket_precedes_issue',prior_sample_at:new Date(prior.at).toISOString(),prior_display_numbers:prior.labels.slice()});
    result.quality_flags=[...flags];return result;
  }
  function trackedForecast(detail, options, days=[], advice=null) {
    const c=arrivalScope(detail,options), arrived=arrival(detail,options);
    if (c.reason) return unavailable(c.reason);
    if (arrived.available) return {available:false, reason:'arrival_complete', arrival:arrived};
    if (arrived.state==='input_conflict') return {available:false,reason:arrived.reason,arrival:arrived};
    if (localDay(options.now)!==detail.local_date) return unavailable('today_only');
    if (arrived.state==='number_cycle_uncertain') return unavailable('ticket_cycle_uncertain');
    let segment=[],last=null;
    for(const row of detail.points){const p=arrivalPoint(row,c,detail.local_date);if(!p){segment=[];last=null;continue;}
      if(last&&!arrivalConnected(last,p,660000))segment=[];segment.push(p);last=p;}
    if(!last)return unavailable('no_current_sample');
    const cadence=Number.isInteger(options.samplingSeconds)?Math.max(180,Math.min(600,options.samplingSeconds)):600;
    const maxAge=(cadence+60)*1000,age=options.now-last.at;
    if(age<0||age>maxAge)return unavailable('stale_sample',{sample_at:new Date(last.at).toISOString()});
    const gap=c.target-last.position;
    if(gap<=0)return unavailable('awaiting_arrival_evidence');
    segment=segment.filter(p=>p.at>=last.at-3600000);
    if(segment.length>128)return unavailable('sampling_density_unsupported');
    const rates=[];
    for(const windowMinutes of [30,60]){const rows=segment.filter(p=>p.at>=last.at-windowMinutes*60000);
      if(rows.length<4||rows.at(-1).at-rows[0].at<900000)continue;
      const robust=slope(rows),end=(rows.at(-1).position-rows[0].position)*60000/(rows.at(-1).at-rows[0].at);
      if(robust>0)rates.push(robust);if(end>0)rates.push(end);}
    const lastRate=segment.length>1?(last.position-segment.at(-2).position)*60000/(last.at-segment.at(-2).at):0;
    // A short but genuine positive segment can provide an explicitly broad
    // provisional estimate instead of treating sparse observations as no data.
    let provisional=false;
    if(!rates.length&&segment.length>=2&&last.at-segment[0].at>=180000){
      const short=slope(segment);if(short>0){rates.push(short);provisional=true;}
    }
    const stable=rates.length?median(rates):null;
    const live=stable?{steady:stable,fast:Math.max(...rates,lastRate,provisional?stable*1.5:stable),slow:Math.min(...rates)*(provisional?.5:1)}:{};
    // Reconstruct fixed historical origins with the same arrival rule and preserve censoring.
    const samples=[],acceptedDays=new Set();
    for(const past of Array.isArray(days)?days.slice(0,7):[]){
      if(past?.local_date>=detail.local_date||past?.requested_store_id!==options.storeId||!sourceMatches(past,c.source)||past.graph_truncated!==false||!Array.isArray(past.points)||past.points.length>MAX_POINTS)continue;
      const generated=stamp(past.generated_at);if(!Number.isFinite(generated)||generated>options.now)continue;
      const pc={...c,generated},buckets=new Set(),sameClass=calendarClass(past).type===calendarClass(detail).type;
      if(!sameClass)continue;
      for(let i=0;i<past.points.length;i++){const start=arrivalPoint(past.points[i],pc,past.local_date);if(!start)continue;
        const bucket=Math.floor(minuteOfDay(start.at)/60);if(buckets.has(bucket))continue;buckets.add(bucket);
        if(Math.abs(minuteOfDay(start.at)-minuteOfDay(last.at))>120)continue;
        const target=String(start.position+gap);let prior=start,lower=0,upper=null;
        for(const row of past.points.slice(i+1)){const next=arrivalPoint(row,pc,past.local_date);
          if(!next||!arrivalConnected(prior,next,660000)||next.at-start.at>7200000)break;
          if(['display_top_two','display_all_three_passed'].includes(displayState(target,next.labels))){upper=(next.at-start.at)/60000;break;}
          prior=next;lower=(next.at-start.at)/60000;}
        samples.push({day:past.local_date,lower,upper});acceptedDays.add(past.local_date);
      }
    }
    const useHistory=acceptedDays.size>=1&&samples.length>=1;
    provisional=provisional||(useHistory&&(acceptedDays.size<2||samples.length<3));
    const available={history:useHistory,steady:Boolean(live.steady),fast:Boolean(live.fast),slow:Boolean(live.slow)};
    if(!Object.values(available).some(Boolean))return unavailable('insufficient_sparse_trend');
    const sinceIssue=segment.filter(p=>p.at>=c.issued),span=sinceIssue.length?(last.at-sinceIssue[0].at)/60000:0;
    const liveShare=stable?useHistory?Math.min(.85,Math.max(.15,span*.85/30)):1:0;
    let weights={history:useHistory?1-liveShare:0,steady:liveShare*.6,fast:liveShare*.25,slow:liveShare*.15};
    const validAdvice=advice?.schema_version===1&&advice.policy==='public_display_scenarios_v1'&&advice.source===c.source&&
      advice.store_id===options.storeId&&advice.queue_type===options.queueType&&advice.local_date===detail.local_date&&
      stamp(advice.sample_at)===last.at&&stamp(advice.expires_at)>options.now&&stamp(advice.generated_at)<=options.now&&
      ['thinking','nonthinking'].includes(advice.thinking_mode)&&advice.model==='deepseek-flash'&&
      advice.weights_ppm&&Object.keys(advice.weights_ppm).sort().join(',')==='fast,history,slow,steady'&&
      Object.values(advice.weights_ppm).every(v=>Number.isInteger(v)&&v>=0&&v<=1000000)&&Object.values(advice.weights_ppm).reduce((a,b)=>a+b,0)===1000000;
    if(validAdvice){let total=0;for(const key of Object.keys(weights)){weights[key]=available[key]?weights[key]*.7+advice.weights_ppm[key]/1000000*.3:0;total+=weights[key];}
      if(total>0)for(const key of Object.keys(weights))weights[key]/=total;}
    const distribution=[];
    if(useHistory){const counts=new Map();samples.forEach(s=>counts.set(s.day,(counts.get(s.day)||0)+1));
      samples.forEach(s=>distribution.push({...s,weight:weights.history/(acceptedDays.size*counts.get(s.day))}));}
    for(const key of ['steady','fast','slow'])if(live[key]){const minutes=gap/live[key],error=cadence/60+(provisional?minutes*.5:0);distribution.push({lower:Math.max(0,Math.min(120,minutes-error)),upper:minutes>120?null:minutes+error,weight:weights[key]});}
    const total=distribution.reduce((n,s)=>n+s.weight,0);if(total<=0)return unavailable('forecast_horizon_unsupported');
    distribution.forEach(s=>s.weight/=total);
    const lower=endpointQuantile(distribution,'lower',.1),upper=endpointQuantile(distribution,'upper',.9),ml=endpointQuantile(distribution,'lower',.5),mu=endpointQuantile(distribution,'upper',.5);
    if(mu===null)return unavailable('median_upper_unknown');
    const central=(ml+mu)/2,reference=last.at+central*60000;
    if(central>120||reference<=options.now)return unavailable('forecast_horizon_unsupported');
    return {policy:'display_arrival_history_live_v5',available:true,eta_available:false,actual_call_verified:false,provisional,
      calibrated_interval:false,mode:stable?useHistory?'history_live_mix':'recent_only':'history_initial',
      reference_time:new Date(reference).toISOString(),scenario_earliest:new Date(Math.max(options.now,last.at+lower*60000)).toISOString(),
      scenario_latest:upper===null?null:new Date(last.at+upper*60000).toISOString(),sample_at:new Date(last.at).toISOString(),sample_age_seconds:age/1000,
      reference:last.position,position_difference:gap,sample_count:segment.length,continuous_minutes:segment.length?(last.at-segment[0].at)/60000:0,
      acceleration_signal:Boolean(stable&&lastRate>Math.max(20,stable*3)),latest_interval_stalled:segment.length>1&&lastRate===0,
      history:{state:useHistory?'available':'insufficient_history',selected_days:acceptedDays.size,samples:samples.length},
      ai_participated:Boolean(validAdvice),ai_mode:validAdvice?advice.thinking_mode:null,weights,arrival:arrived};
  }
  function storeWait(detail, options, cohort, advice=null) {
    const field=FIELDS[options?.queueType],source=requestedSource(options);
    if(!field||!sourceMatches(detail,source)||detail.requested_store_id!==options.storeId||!Array.isArray(detail.points)||detail.points.length>MAX_POINTS||detail.graph_truncated!==false)return unavailable('scope_mismatch');
    if(!Number.isFinite(options.now)||localDay(options.now)!==detail.local_date)return unavailable('today_only');
    if(!cohort||!Number.isInteger(cohort.sample_count)||cohort.sample_count<5||cohort.distinct_days<2)return unavailable('waiting_for_test_cohort');
    const numbers=['median_lower','median_upper','early_lower','late_upper'].map(k=>cohort[k]);
    if(numbers.some(n=>!Number.isFinite(n)||n<0||n>720)||cohort.median_lower>cohort.median_upper||cohort.early_lower>cohort.late_upper)return unavailable('invalid_wait_cohort');
    const context={field,source,generated:stamp(detail.generated_at)};let segment=[],last=null;
    for(const row of detail.points){const p=arrivalPoint(row,context,detail.local_date);if(!p){segment=[];last=null;continue;}if(last&&!arrivalConnected(last,p,660000))segment=[];segment.push(p);last=p;}
    if(!last||options.now-last.at<0||options.now-last.at>((options.samplingSeconds||600)+60)*1000)return unavailable('no_current_sample');
    segment=segment.filter(p=>p.at>=last.at-3600000);const recent=segment.filter(p=>p.at>=last.at-1800000);
    const rate=recent.length>=4&&last.at-recent[0].at>=900000?slope(recent):null;
    const ratio=rate>0&&cohort.issue_rate_median>0?Math.max(.5,Math.min(2,cohort.issue_rate_median/rate)):1;
    let weights={history:rate>0?.4:1,steady:rate>0?.4:0,fast:rate>0?.15:0,slow:rate>0?.05:0};
    const valid=advice?.source===source&&advice.store_id===options.storeId&&advice.queue_type===options.queueType&&stamp(advice.sample_at)===last.at&&stamp(advice.expires_at)>options.now&&stamp(advice.generated_at)<=options.now&&advice.model==='deepseek-flash'&&advice.policy==='public_display_scenarios_v1'&&advice.weights_ppm&&Object.keys(advice.weights_ppm).sort().join(',')==='fast,history,slow,steady'&&Object.values(advice.weights_ppm).every(v=>Number.isInteger(v)&&v>=0)&&Object.values(advice.weights_ppm).reduce((a,b)=>a+b,0)===1000000;
    if(valid&&rate>0)for(const k of Object.keys(weights))weights[k]=weights[k]*.7+advice.weights_ppm[k]/1000000*.3;
    const factors={history:1,steady:ratio,fast:ratio*.75,slow:ratio*1.25};
    const center=(cohort.median_lower+cohort.median_upper)/2;
    const estimate=Object.keys(weights).reduce((n,k)=>n+weights[k]*factors[k]*center,0);
    return {available:true,policy:'store_wait_test_cohort_v1',wait_minutes:estimate,early_minutes:Math.max(0,cohort.early_lower*Math.min(1,ratio*.75)),late_minutes:cohort.late_upper*Math.max(1,ratio*1.25),sample_count:cohort.sample_count,distinct_days:cohort.distinct_days,ai_participated:Boolean(valid&&rate>0),actual_call_verified:false,calibrated_interval:false,weights,position_known:false};
  }
  const api = {POLICY, HISTORY_POLICY, ARRIVAL_POLICY, forecast, forecastWithHistory, replay, displayState, arrival, trackedForecast, storeWait};
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.SushiWaitReference = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
