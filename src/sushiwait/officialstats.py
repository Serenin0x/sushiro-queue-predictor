"""Read-only calendar projections from validated official public packets.

One official detail GET is one observation. This never converts that response
to an anonymous two-query pair or infers continuity across omitted records.
The caller must preserve the full validated packet alongside these projections.
No credentials, database, network, source renewal or automatic upload is used.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
import math
from zoneinfo import ZoneInfo

from .businesshours import BusinessHours
from .monitoring import _stamp, _time
from .observations import QUEUE_NAMES
from .receipts import validate_packet

SOURCE = "sapi_miniapp_gateway"
SUCCESS_SEMANTICS = "one_official_detail_response_not_two_query_pair"


class OfficialStatisticsError(ValueError):
    pass


def project_packet(packet, *, as_of, hours, base_interval=60, store_names=None):
    """Return bounded month/day contracts plus the full public input packet.

    A <=1000-record export is a partial selection, never a full-day coverage
    claim. Missing queue fields and counts remain unknown. Cross-record run
    continuity cannot be proven by this public format, so display removals and
    velocities are deliberately unavailable rather than inferred across gaps.
    """
    if type(base_interval) is not int or not 60 <= base_interval <= 3600:
        raise OfficialStatisticsError("official_stats_invalid_interval")
    safe = validate_packet(packet, as_of=as_of)
    if safe["api_profile"] != "miniapp_gateway":
        raise OfficialStatisticsError("official_stats_profile_mismatch")
    now = _time(as_of)
    rules = BusinessHours(hours)
    ids = safe["store_ids"]
    names = {s: s for s in ids} if store_names is None else store_names
    if (type(names) is not dict or set(names) != set(ids)
            or any(type(v) is not str or not 1 <= len(v) <= 128
                   or any(ord(c) < 32 for c in v) for v in names.values())):
        raise OfficialStatisticsError("official_stats_invalid_names")
    grouped = defaultdict(list)
    for record in safe["records"]:
        timing = record["timing"]
        started = timing.get("request_started_at", timing.get("checked_at"))
        day = _time(started).astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
        grouped[(record["store_id"], day)].append(record)
    months, days = {}, {}
    for (store, day), records in sorted(grouped.items()):
        records.sort(key=lambda r: (r["timing"].get("request_started_at", r["recorded_at"]),
                                    r["recorded_at"], r["observation_id"]))
        spans, metadata = rules.intervals_for(store, date.fromisoformat(day), as_of=now)
        planned = sum(math.ceil((b-a).total_seconds()/base_interval) for a,b in spans)
        through = min(now, _time(day+"T00:00:00+08:00")+timedelta(days=1))
        elapsed = sum(math.ceil(max(0, (min(b, through)-a).total_seconds())/base_interval)
                      for a,b in spans if through>a)
        covered, points, starts = set(), [], []
        successes = attempts = queue_successes = 0
        for record in records:
            timing = record["timing"]
            started = timing.get("request_started_at", timing.get("checked_at"))
            starts.append(started)
            attempted = timing["semantics"] == "http_response_received"
            attempts += attempted
            successes += record["ok"]
            received = timing.get("received_at")
            fields = record.get("display", {}).get("groupQueues", {}).get("groups")
            available = bool(fields and all(fields[q]["presence"] == "present"
                                            for q in ("mixedQueue", "reservationQueue")))
            queue_successes += available
            queues = {q: fields[q]["value"][:3] if fields[q]["presence"] == "present"
                      else None for q in QUEUE_NAMES} if fields else None
            presence = {q: fields[q]["presence"] for q in QUEUE_NAMES} if fields else None
            count = record.get("display", {}).get("groupQueuesCount", {"presence":"unknown", "value":None})
            count_value = count["value"] if count["presence"] == "present" and count["value"] >= 0 else None
            if available:
                t = _time(started)
                for i,(a,b) in enumerate(spans):
                    if a<=t<b: covered.add((i, int((t-a).total_seconds()//base_interval)))
            points.append({"request_started_at":started, "queue_received_at":received if record["ok"] else None,
                "count_received_at":received if count_value is not None else None,
                "pair_ok":record["ok"], "success_semantics":SUCCESS_SEMANTICS, "scheduled_pause":False,
                "queues":queues if available else None, "queue_field_presence":presence,
                "observed_queue_fields":queues, "count_raw":count_value, "count_field":count,
                "call_reference_labels":{q:queues[q][0] if queues[q] else None for q in QUEUE_NAMES} if available else None,
                "display_sizes":{q:len(fields[q]["value"]) if fields[q]["presence"]=="present" else None
                                 for q in QUEUE_NAMES} if fields else None,
                "comparison_state":"packet_continuity_unknown", "interval_seconds":None,
                "removed_labels":None, "error_codes":{} if record["ok"] else {"detail":record["error_code"]}})
        summary = {**metadata, "store_id":store, "local_date":day, "observations":len(records),
            "successful_pairs":successes, "successful_detail_responses":successes,
            "failed_pairs":len(records)-successes, "success_semantics":SUCCESS_SEMANTICS,
            "scheduled_pause_slots":0, "recorded_http_attempts":attempts, "group_successes":queue_successes,
            "first_observation_at":starts[0], "last_observation_at":starts[-1],
            "declared_intervals":[[_stamp(a),_stamp(b)] for a,b in spans],
            "expected_background_slots_full_day":planned, "expected_background_slots_so_far":elapsed,
            "observed_background_slots":len(covered),
            "observed_slot_fraction_so_far":min(1,len(covered)/elapsed) if elapsed else None,
            "slot_fraction_semantics":"successful_queue_response_in_declared_background_bin",
            "returned_graph_points":len(points), "graph_truncated":False,
            "actual_called_count":None, "no_show_rate":None, "count_unit":"unknown",
            "display_removed_labels":{q:None for q in QUEUE_NAMES},
            "full_source_arrays_persisted":False, "full_source_arrays_in_input_packet":True,
            "source_freshness":"unknown",
            "selection_scope":"bounded_public_packet_not_full_day"}
        common = {"daily_schema_version":1, "source":SOURCE, "api_profile":"miniapp_gateway",
            "data_origin":safe["data_origin"], "generated_at":_stamp(now), "network_performed_by_read":False,
            "eta_available":False, "selection_scope":"bounded_public_packet_not_full_day"}
        days[(store, day)] = {**common, "requested_store_id":store, "local_date":day, "summary":summary,
            "points":points, "returned_graph_points":len(points), "graph_truncated":False,
            "labels_per_array_in_graph":3, "full_source_arrays_persisted":False,
            "full_source_arrays_in_input_packet":True,
            "call_reference_semantics":"user_assumed_first_displayed_label", "first_label_is_confirmed_call":False,
            "display_turnover_is_no_show_rate":False, "actual_called_count":None}
        month = day[:7]
        if month not in months:
            months[month] = {**common, "month":month, "configured_store_ids":list(ids), "store_names":dict(names),
                "unavailable_store_ids":[], "days":{}, "timezone":"Asia/Shanghai",
                "heatmap_semantics":"coverage_only_traffic_not_calibrated"}
        months[month]["days"].setdefault(day, {})[store] = summary
    return {"months":months, "days":days, "source_packet":safe,
            "credentials_included":False, "network_performed":False}
