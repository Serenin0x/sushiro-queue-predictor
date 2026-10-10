/* Private, page-visible precautions. Display membership is never a verified call. */
(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.SushiWaitReferenceAlerts = api;
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";
  const fields = {ordinary: "mixedQueue", reservation: "reservationQueue"};
  const stamp = value => typeof value === "string" && value.length <= 64 &&
    /^\d{4}-\d{2}-\d{2}T.*(?:Z|[+-]\d{2}:\d{2})$/.test(value) ? Date.parse(value) : NaN;
  const day = value => new Date(value + 28800000).toISOString().slice(0, 10);
  const label = value => typeof value === "string" && /^\d{1,7}$/.test(value);
  function createTracker() {
    let scope = null, seenAt = null, previousSample = null, previousEstimate = null;
    function reset() {scope = null; seenAt = previousSample = previousEstimate = null;}
    function update(detail, options, forecast) {
      let interval = 60, receipt = null;
      const finish = (state, reason, extra = {}) => Object.freeze({
        state, reason, sample_at: receipt, requested_source_interval_seconds: interval,
        source_interval_applied: false, notification_sent: false,
        background_delivery_available: false, actual_call_verified: false,
        no_show_verified: false, eta_available: false, calibrated_risk: false,
        network_performed: false, private_input_uploaded: false, ...extra
      });
      const unavailable = reason => {previousEstimate = null; return finish("data_unavailable", reason);};
      if (options?.confirmedScope !== true || !options.ticket) {reset(); return finish("idle", "confirm_ticket_scope");}
      const {storeId, queueType, ticket, issuedAt, now} = options;
      const field = fields[queueType], issued = stamp(issuedAt), arrival = options.desiredArrivalAt == null
        ? null : stamp(options.desiredArrivalAt), offset = options.callOffsetMinutes ?? 0;
      if (!field || typeof storeId !== "string" || !/^[1-9]\d{0,9}$/.test(storeId) ||
          !label(ticket) || !Number.isSafeInteger(now) || !Number.isFinite(new Date(now).getTime()) ||
          !Number.isFinite(issued) || issued > now ||
          day(issued) !== day(now) || !Number.isInteger(offset) || Math.abs(offset) > 120 ||
          arrival !== null && (!Number.isFinite(arrival) || day(arrival) !== day(now) || arrival < issued) ||
          arrival === null && offset !== 0) {reset(); return unavailable("invalid_private_plan");}
      const key = JSON.stringify([storeId, queueType, ticket, issuedAt, arrival, offset]);
      if (scope !== key) {reset(); scope = key;}
      const target = arrival === null ? null : arrival + offset * 60000;
      const requested = earliest => {
        const horizons = [arrival, target, earliest].filter(Number.isFinite);
        if (!horizons.length) return 60;
        return Math.min(...horizons) - now <= 900000 ? 30 : 60;
      };
      interval = requested(null);
      if (!detail && options.readPending === true) return finish("loading", "current_read_pending");
      if (!detail || detail.daily_schema_version !== 1 || detail.source !== "crm_remote_v1_1" ||
          detail.requested_store_id !== storeId || detail.local_date !== day(now) ||
          detail.network_performed_by_read !== false || detail.eta_available !== false ||
          detail.first_label_is_confirmed_call !== false ||
          detail.call_reference_semantics !== "user_assumed_first_displayed_label" ||
          !Array.isArray(detail.points) || detail.points.length > 2048) return unavailable("current_data_unavailable");
      const generated = stamp(detail.generated_at), row = detail.points.at(-1);
      const at = stamp(row?.queue_received_at), start = stamp(row?.request_started_at);
      const labels = row?.queues?.[field];
      if (!Number.isFinite(generated) || generated > now || !Number.isFinite(at) || !Number.isFinite(start) ||
          start > at || at > generated || at > now || day(at) !== day(now)) return unavailable("current_time_invalid");
      receipt = new Date(at).toISOString();
      if (now - at > 90000) return unavailable("source_stale");
      if (at < issued) return unavailable("awaiting_post_issue_sample");
      if (row.scheduled_pause !== false || !row.error_codes || typeof row.error_codes !== "object" ||
          Array.isArray(row.error_codes) || row.error_codes.groupqueues || !Array.isArray(labels) ||
          labels.length > 3 || labels.some(v => !label(v))) return unavailable("queue_unavailable");
      const signature = JSON.stringify(labels);
      if (previousSample && (at < previousSample.at || at === previousSample.at && signature !== previousSample.signature))
        return unavailable("receipt_regressed_or_conflicted");
      const isNew = !previousSample || at > previousSample.at;
      const reversal = Boolean(isNew && previousSample && previousSample.first !== null && labels.length &&
        Number(labels[0]) < previousSample.first);
      const boundary = isNew && previousSample && (reversal || at - previousSample.at > 120000 ||
        row.comparison_state !== "comparable_display_sets");
      if (boundary) {seenAt = null; previousEstimate = null;}
      // Preserve exact strings. A padded look-alike is not evidence of the user's number.
      const displayed = labels.includes(ticket), wasSeen = seenAt !== null;
      if (displayed && seenAt === null) seenAt = receipt;
      previousSample = {at, signature, first: labels.length ? Number(labels[0]) : null};
      if (displayed) {interval = 30; previousEstimate = null; return finish("attention", "number_displayed", {first_seen_at: seenAt});}
      if (wasSeen && !boundary) {interval = 30; previousEstimate = null;
        return finish("attention", "previously_displayed_now_absent", {first_seen_at: seenAt});}
      if (reversal || forecast?.reason === "ticket_cycle_uncertain") return unavailable("ticket_cycle_uncertain");
      if (forecast?.reason === "reference_at_or_beyond_ticket") {interval = 30;
        previousEstimate = null; return finish("attention", "reference_at_or_beyond_ticket");}
      if (!forecast?.available) {previousEstimate = null; return finish("tracking", "model_unavailable");}
      const sample = stamp(forecast.sample_at), reference = stamp(forecast.reference_time), earliest = stamp(forecast.scenario_earliest);
      const latest = forecast.scenario_latest === null ? null : stamp(forecast.scenario_latest);
      if (forecast.eta_available !== false || forecast.actual_call_verified !== false ||
          forecast.calibrated_interval !== false || forecast.queue_field !== field || sample !== at ||
          !Number.isFinite(reference) || reference <= now || !Number.isFinite(earliest) || earliest < now ||
          latest !== null && (!Number.isFinite(latest) || latest < earliest) ||
          typeof forecast.acceleration_signal !== "boolean") return unavailable("forecast_binding_invalid");
      interval = requested(earliest);
      const earlierBy = isNew ? previousEstimate ? Math.max(0, (previousEstimate.reference - reference) / 60000) : 0
        : previousEstimate?.earlierBy || 0;
      if (isNew) previousEstimate = {reference, earlierBy};
      const extra = {reference_time: forecast.reference_time, scenario_earliest: forecast.scenario_earliest,
        scenario_latest: forecast.scenario_latest, earlier_shift_minutes: earlierBy,
        early_arrival_scenario: arrival !== null && earliest < arrival,
        target_call_at: target === null ? null : new Date(target).toISOString()};
      if (extra.early_arrival_scenario) {interval = 30; return finish("attention", "possible_before_arrival", extra);}
      if (forecast.acceleration_signal) {interval = 30; return finish("attention", "reference_acceleration", extra);}
      if (earlierBy >= 5) {interval = 30; return finish("attention", "reference_moved_earlier", extra);}
      return finish("tracking", "reference_tracking", extra);
    }
    return Object.freeze({update, reset});
  }
  return Object.freeze({createTracker});
});
