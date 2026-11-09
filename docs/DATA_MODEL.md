# Data model

SQLite schema defined in [`apimon/db.py`](../apimon/db.py). Timestamps are epoch seconds (REAL).

```mermaid
erDiagram
    projects ||--o{ api_keys : "has (hashed)"
    projects ||--o{ endpoints : registers
    projects ||--o{ requests : receives
    endpoints ||--o{ requests : "route template"
    projects ||--o{ check_results : "uptime probes"
    projects ||--o{ alert_state : "one row per rule"
    projects ||--o{ alert_events : "state transitions"

    projects {
        int id PK
        text name UK
        real slo_target "default 0.99"
        real apdex_t_ms "default 300"
        real created_at
    }
    api_keys {
        int id PK
        int project_id FK
        text key_hash UK "sha256, plaintext never stored"
        text key_prefix "first 8 chars, for identification"
        real created_at
    }
    endpoints {
        int id PK
        int project_id FK
        text method
        text route "template, e.g. /users/<int:id>"
        real first_seen
    }
    requests {
        int id PK
        int project_id FK
        int endpoint_id FK
        real ts
        int status
        real latency_ms
        int request_bytes
        int response_bytes
        text error "exception type, nullable"
        text request_id
        real sample_rate "1.0 = not sampled"
    }
    check_results {
        int id PK
        int project_id FK
        text check_name
        real ts
        int ok
        int status
        real latency_ms
        int attempts
        text error
    }
    alert_state {
        int project_id PK
        text rule PK
        text state "ok|pending|firing|resolved"
        int consecutive
        real since
        real last_notified
        int firing_notified
        real last_value
    }
    alert_events {
        int id PK
        int project_id FK
        text rule
        real ts
        text from_state
        text to_state
        real value
        int notified
        text message
    }
```

Notes: `requests` is indexed on `(project_id, ts)`. Alert rules and uptime check definitions live in JSON config files, not in the database.
