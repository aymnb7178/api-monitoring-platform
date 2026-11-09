# Collector API

Every example below was run against a live local server (`python -m apimon serve`, `127.0.0.1:8765`) and the
responses are copied from that run (key and timestamps shortened; JSON key order as returned by Flask).

```bash
export APIMON_ADMIN_TOKEN=dev-admin-token        # demo value, choose your own
python -m apimon create-project seed --db build/api.db
python -m apimon serve --db build/api.db --port 8765 \
    --alerts examples/alerts.json --checks examples/checks.json
B=http://127.0.0.1:8765
```
(`examples/checks.json` probes `127.0.0.1:8001`; start the local probe app there first, see `apimon/probe_app.py`.)

## Conventions
- **Auth:** `X-API-Key: <key>` (or `Authorization: Bearer <key>`, or `?key=` for the dashboard). Keys are stored as SHA-256 hashes.
  Project creation uses `X-Admin-Token` and is disabled (403) unless `APIMON_ADMIN_TOKEN` is set.
- **Request IDs:** send `X-Request-ID` (`[A-Za-z0-9._-]{1,64}`) or one is generated; it is echoed in the response header and in error bodies.
- **Errors:** `{"error": {"code", "message", ...}, "request_id"}` with 400/401/403/404/409/422/429/500.
- **Rate limit:** in-memory token bucket per API key (defaults: burst 60, refill 30/s); 429 plus `Retry-After`.
- **Time:** analytics endpoints accept `window` (seconds), `bucket` (seconds) and `at` (epoch, default now, useful for replaying demo data).

## Health
```bash
curl -s $B/healthz
# {"status":"ok"}
```

## Create a project (admin)
```bash
curl -s -X POST $B/api/projects -H 'X-Admin-Token: dev-admin-token' -H 'Content-Type: application/json' \
  -d '{"name":"my-service","slo_target":0.995,"apdex_t_ms":200}'
# {"api_key":"apm_ru5a...","note":"store this key now; only its hash is kept","project":{"apdex_t_ms":200.0,"id":2,"name":"my-service","slo_target":0.995}}
```
Without the header: `403 {"error":{"code":"forbidden",...}}`. Duplicate name: 409.

```bash
KEY=apm_ru5a...   # the key returned above
```

## Ingest events
`POST /api/ingest`: 1..1000 events. Required: `ts`, `method`, `route`, `status` (100-599), `latency_ms`. Optional:
`request_bytes`, `response_bytes`, `error`, `request_id`, `sample_rate` (0-1].
```bash
NOW=$(date +%s)
curl -si -X POST $B/api/ingest -H "X-API-Key: $KEY" -H 'Content-Type: application/json' -H 'X-Request-ID: demo-req-1' \
  -d "{\"events\":[{\"ts\":$((NOW-30)),\"method\":\"GET\",\"route\":\"/users/<int:id>\",\"status\":200,\"latency_ms\":42.5,\"response_bytes\":512},
                  {\"ts\":$((NOW-20)),\"method\":\"POST\",\"route\":\"/orders\",\"status\":500,\"latency_ms\":310,\"error\":\"TimeoutError\"},
                  {\"ts\":$((NOW-10)),\"method\":\"GET\",\"route\":\"/users/<int:id>\",\"status\":200,\"latency_ms\":38}]}"
# HTTP/1.1 202 ACCEPTED
# X-Request-ID: demo-req-1
# {"accepted":3}
```
Validation failure (422):
```bash
curl -s -X POST $B/api/ingest -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"events":[{"ts":1,"method":"GET","route":"/x","status":999,"latency_ms":1}]}'
# {"error":{"code":"validation_error","details":[{"loc":["events",0,"status"],"msg":"Input should be less than or equal to 599"}],"message":"invalid request body"},"request_id":"08c2..."}
```
Missing key: `curl -s $B/api/stats` gives `401 {"error":{"code":"unauthorized",...}}`.

## Endpoint registry
```bash
curl -s $B/api/endpoints -H "X-API-Key: $KEY"
# {"endpoints":[{"first_seen":1794170753.0,"method":"POST","recorded_events":1,"route":"/orders"},{"first_seen":1794170743.0,"method":"GET","recorded_events":2,"route":"/users/<int:id>"}]}
curl -s -X POST $B/api/endpoints -H "X-API-Key: $KEY" -H 'Content-Type: application/json' -d '{"method":"get","route":"/health"}'
# {"id":3,"method":"GET","route":"/health"}
```

## Analytics
```bash
curl -s "$B/api/stats?window=300" -H "X-API-Key: $KEY"
# {"apdex":0.6667,"avg_request_bytes":0.0,"avg_response_bytes":170.7,"error_rate":0.33333,"errors":1,"p50_ms":42.5,"p95_ms":283.25,"p99_ms":304.65,"recorded_events":3,"requests":3,"throughput_rps":0.01,"window_s":300.0}

curl -s "$B/api/endpoints/stats?window=300&slow_ms=100&rank_by=p95_ms" -H "X-API-Key: $KEY"
# {"endpoints":[{"apdex":0.0,"error_rate":1.0,"errors":1,"method":"POST","p50_ms":310.0,...,"route":"/orders","slow":false,"time_share":0.7939}, {...GET /users/<int:id>...}],"rank_by":"p95_ms","window_s":300.0}
# slow=false because a route needs at least 20 requests in the window to be flagged

curl -s "$B/api/slo?window=300" -H "X-API-Key: $KEY"
# {"bad":1,"budget_remaining":-65.6667,"budget_spent":66.6667,"period_s":300.0,"policies":[{"burn_long":66.667,"burn_short":66.667,"factor":14.4,"firing":true,"long_s":3600,"name":"fast",...}, ...],"requests":3,"target":0.995}

curl -s "$B/api/anomalies?window=300" -H "X-API-Key: $KEY"
# {"anomalies":[],"method":"robust z-score (MAD), upward, threshold 3.5","window_s":300.0}

curl -s "$B/api/report?window=300" -H "X-API-Key: $KEY"   # everything the dashboard shows, as JSON
# keys: alert_events, alerts, anomalies, apdex_t_ms, bucket_s, burn_series, endpoints, now, project, series, slo, slow_ms, summary, uptime, window_s
```
(Three events is far too little data for meaningful numbers; the 66x burn rate is arithmetic on one failure in three requests.)

## Alerts (rules come from the JSON file passed to `serve`)
```bash
curl -s -X POST $B/api/alerts/evaluate -H "X-API-Key: $KEY"
# {"transitions":[]}        (rules need min_requests=20 in their window, so nothing fires on 3 events)
curl -s $B/api/alerts -H "X-API-Key: $KEY"
# {"events":[],"rules":[{"consecutive":0,"last_value":null,"rule":"error_burst","since":1794170773.29,"state":"ok"}, ...]}
```
Without an alert config the evaluate call returns 409 `not_configured`.

## Uptime (check URLs come from server config only, never from the request)
```bash
curl -s -X POST $B/api/uptime/run -H "X-API-Key: $KEY"
# {"results":[{"attempts":1,"error":null,"latency_ms":11.09,"name":"local-ok","ok":true,"status":200,"ts":...,"url":"http://127.0.0.1:8001/ok"},
#             {"attempts":2,"error":null,"latency_ms":1.05,"name":"local-flaky","ok":true,"status":200,...}]}
curl -s $B/api/uptime -H "X-API-Key: $KEY"      # latest result per check
```

## Dashboard
```bash
curl -s -o /dev/null -w "%{http_code} %{content_type}\n" "$B/dashboard?window=300&key=$KEY"
# 200 text/html; charset=utf-8
```
Open the same URL in a browser. It is server-rendered with inline PNG charts and no JavaScript.

## Rate limiting
Behaviour is covered by unit tests (`tests/test_core.py::test_token_bucket_refill_and_deny`,
`tests/test_services.py::test_ingest_rate_limited_with_retry_after`: 429 with a `Retry-After` header). A live run of 70 sequential
curl calls did **not** trip the default limit (curl is slower than the 30/s refill), so no live 429 output is shown here.
