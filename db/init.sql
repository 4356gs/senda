-- Senda: esquema de base de datos (PostgreSQL + pgvector)
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS contacts (
    id               BIGSERIAL PRIMARY KEY,
    wa_id            TEXT UNIQUE NOT NULL,          -- teléfono E.164 sin '+'
    display_name     TEXT,
    channel          TEXT NOT NULL DEFAULT 'whatsapp', -- whatsapp | simulator
    consent_state    TEXT NOT NULL DEFAULT 'unknown',  -- unknown | requested | granted | denied | revoked
    stage            TEXT NOT NULL DEFAULT 'nuevo',    -- nuevo | frio | caliente | objecion | pago_pendiente | cliente | perdido
    status           TEXT NOT NULL DEFAULT 'active',   -- active | handoff | opted_out | erased
    preferred_format TEXT,                             -- texto | audio
    last_inbound_at  TIMESTAMPTZ,
    last_outbound_at TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Registro RGPD inmutable (solo INSERT). Prueba de consentimiento: art. 7.1 RGPD.
CREATE TABLE IF NOT EXISTS consent_events (
    id            BIGSERIAL PRIMARY KEY,
    contact_id    BIGINT NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    event         TEXT NOT NULL,   -- requested | granted | denied | revoked | erased | exported
    purpose       TEXT NOT NULL,
    legal_basis   TEXT NOT NULL,   -- consentimiento (6.1.a) | precontractual (6.1.b)
    evidence_text TEXT,            -- texto literal del cliente o del sistema
    message_id    BIGINT,
    policy_version TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS consent_events_contact ON consent_events(contact_id, created_at);

CREATE TABLE IF NOT EXISTS messages (
    id            BIGSERIAL PRIMARY KEY,
    contact_id    BIGINT NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    direction     TEXT NOT NULL,          -- in | out
    kind          TEXT NOT NULL,          -- text | audio | image | template | system
    body          TEXT,
    transcript    TEXT,
    media_ref     TEXT,
    wa_message_id TEXT UNIQUE,
    origin        TEXT,                   -- agent | followup | gdpr | human | payment | reminder
    pricing_category TEXT,                -- service | utility | marketing
    cost_eur      NUMERIC(10,4) NOT NULL DEFAULT 0,
    created_at    TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_contact ON messages(contact_id, created_at);

-- Hechos estructurados que el agente recuerda (día de cobro, objeción, etc.)
CREATE TABLE IF NOT EXISTS contact_facts (
    contact_id  BIGINT NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    key         TEXT NOT NULL,
    value       TEXT NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (contact_id, key)
);

-- Memoria semántica de largo plazo (pgvector)
CREATE TABLE IF NOT EXISTS memory_items (
    id          BIGSERIAL PRIMARY KEY,
    contact_id  BIGINT NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,            -- message | fact | summary
    content     TEXT NOT NULL,
    embedding   vector(384) NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS memory_items_contact ON memory_items(contact_id);
CREATE INDEX IF NOT EXISTS memory_items_hnsw ON memory_items USING hnsw (embedding vector_cosine_ops);

CREATE TABLE IF NOT EXISTS followups (
    id            BIGSERIAL PRIMARY KEY,
    contact_id    BIGINT NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    sequence      TEXT NOT NULL,          -- clave en prompts/followups.json
    step          INT  NOT NULL,
    reason        TEXT NOT NULL,
    note          TEXT,
    due_at        TIMESTAMPTZ NOT NULL,
    status        TEXT NOT NULL DEFAULT 'scheduled', -- scheduled | sent | cancelled | skipped
    outcome       TEXT,
    message_id    BIGINT,
    created_at    TIMESTAMPTZ NOT NULL,
    executed_at   TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS followups_due ON followups(status, due_at);

CREATE TABLE IF NOT EXISTS appointments (
    id             BIGSERIAL PRIMARY KEY,
    contact_id     BIGINT NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    service        TEXT NOT NULL,
    patient_name   TEXT,
    start_at       TIMESTAMPTZ NOT NULL,
    end_at         TIMESTAMPTZ NOT NULL,
    gcal_event_id  TEXT,
    status         TEXT NOT NULL DEFAULT 'confirmed', -- confirmed | cancelled
    created_at     TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS appointments_start ON appointments(status, start_at);

CREATE TABLE IF NOT EXISTS payments (
    id             BIGSERIAL PRIMARY KEY,
    contact_id     BIGINT NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    appointment_id BIGINT REFERENCES appointments(id) ON DELETE SET NULL,
    concept        TEXT NOT NULL,
    amount_eur     NUMERIC(10,2) NOT NULL,
    provider       TEXT NOT NULL,          -- bizum_mock
    token          TEXT UNIQUE NOT NULL,
    status         TEXT NOT NULL DEFAULT 'pending', -- pending | paid | expired
    created_at     TIMESTAMPTZ NOT NULL,
    paid_at        TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS handoffs (
    id           BIGSERIAL PRIMARY KEY,
    contact_id   BIGINT NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    reason       TEXT NOT NULL,
    summary      TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL,
    resolved_at  TIMESTAMPTZ
);

-- Auditoría de cada ejecución del agente (trazabilidad para el negocio y jueces)
CREATE TABLE IF NOT EXISTS agent_runs (
    id           BIGSERIAL PRIMARY KEY,
    contact_id   BIGINT REFERENCES contacts(id) ON DELETE CASCADE,
    trigger      TEXT NOT NULL,           -- inbound | followup
    model        TEXT NOT NULL,
    latency_ms   INT,
    tool_calls   JSONB NOT NULL DEFAULT '[]',
    input_tokens INT,
    output_tokens INT,
    error        TEXT,
    created_at   TIMESTAMPTZ NOT NULL
);
