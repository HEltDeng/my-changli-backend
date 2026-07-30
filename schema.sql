PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    password_salt TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'USER' CHECK (role IN ('USER', 'ADMIN')),
    account_type TEXT NOT NULL DEFAULT 'DEVICE',
    nickname TEXT NOT NULL DEFAULT '长理同学',
    phone_hash TEXT UNIQUE,
    phone_masked TEXT,
    student_id TEXT,
    real_name TEXT,
    campus_verified INTEGER NOT NULL DEFAULT 0,
    campus_verified_at INTEGER,
    account_status TEXT NOT NULL DEFAULT 'ACTIVE',
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at INTEGER NOT NULL,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS venues (
    id TEXT PRIMARY KEY,
    sport TEXT NOT NULL UNIQUE CHECK (sport IN ('BADMINTON', 'TABLE_TENNIS')),
    name TEXT NOT NULL,
    unit_label TEXT NOT NULL,
    court_count INTEGER NOT NULL CHECK (court_count > 0),
    price_yuan INTEGER NOT NULL CHECK (price_yuan >= 0),
    enabled INTEGER NOT NULL DEFAULT 1,
    operating_status TEXT NOT NULL DEFAULT 'OPEN'
        CHECK (operating_status IN ('OPEN', 'CLOSED', 'MAINTENANCE')),
    maintenance_note TEXT NOT NULL DEFAULT '',
    expected_restore_at INTEGER,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS venue_time_slots (
    id TEXT PRIMARY KEY,
    venue_id TEXT NOT NULL REFERENCES venues(id) ON DELETE CASCADE,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    UNIQUE (venue_id, start_time, end_time)
);

CREATE TABLE IF NOT EXISTS blocked_inventory (
    id TEXT PRIMARY KEY,
    venue_id TEXT NOT NULL REFERENCES venues(id) ON DELETE CASCADE,
    booking_date TEXT NOT NULL,
    time_slot TEXT NOT NULL,
    court_number INTEGER NOT NULL,
    reason TEXT NOT NULL DEFAULT '场馆暂不可用',
    UNIQUE (venue_id, booking_date, time_slot, court_number)
);

CREATE TABLE IF NOT EXISTS reservations (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    venue_id TEXT NOT NULL REFERENCES venues(id),
    booking_date TEXT NOT NULL,
    time_slot TEXT NOT NULL,
    court_number INTEGER NOT NULL,
    price_yuan INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'CANCELLED')),
    payment_status TEXT NOT NULL DEFAULT 'SIMULATED' CHECK (payment_status IN ('SIMULATED')),
    refund_status TEXT NOT NULL DEFAULT 'NOT_APPLICABLE',
    reservation_code TEXT,
    verification_status TEXT NOT NULL DEFAULT 'NOT_REQUIRED',
    cancelled_at INTEGER,
    cancelled_by TEXT,
    cancel_reason TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS reservations_unique_active_inventory
ON reservations(venue_id, booking_date, time_slot, court_number)
WHERE status = 'ACTIVE';

CREATE INDEX IF NOT EXISTS reservations_user_created
ON reservations(user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS lost_found_posts (
    id TEXT PRIMARY KEY,
    owner_user_id TEXT NOT NULL REFERENCES users(id),
    client_local_id TEXT,
    type TEXT NOT NULL CHECK (type IN ('LOST', 'FOUND')),
    item_name TEXT NOT NULL,
    category TEXT NOT NULL,
    location TEXT NOT NULL,
    event_date TEXT NOT NULL,
    approximate_time TEXT,
    description TEXT NOT NULL DEFAULT '',
    claim_note TEXT NOT NULL DEFAULT '',
    image_name TEXT,
    status TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE(owner_user_id, client_local_id)
);

CREATE INDEX IF NOT EXISTS lost_found_updated ON lost_found_posts(updated_at DESC);
CREATE INDEX IF NOT EXISTS lost_found_owner ON lost_found_posts(owner_user_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS lost_found_favorites (
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    post_id TEXT NOT NULL REFERENCES lost_found_posts(id) ON DELETE CASCADE,
    created_at INTEGER NOT NULL,
    PRIMARY KEY(user_id, post_id)
);

CREATE TABLE IF NOT EXISTS lost_found_claims (
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    post_id TEXT NOT NULL REFERENCES lost_found_posts(id) ON DELETE CASCADE,
    created_at INTEGER NOT NULL,
    PRIMARY KEY(user_id, post_id)
);

CREATE TABLE IF NOT EXISTS lost_found_reports (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    post_id TEXT NOT NULL REFERENCES lost_found_posts(id) ON DELETE CASCADE,
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN', 'REVIEWED', 'CLOSED')),
    created_at INTEGER NOT NULL,
    UNIQUE(user_id, post_id)
);

CREATE TABLE IF NOT EXISTS announcements (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    published_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS app_versions (
    platform TEXT PRIMARY KEY,
    latest_version_code INTEGER NOT NULL,
    minimum_version_code INTEGER NOT NULL,
    latest_version_name TEXT NOT NULL,
    update_message TEXT NOT NULL DEFAULT '',
    download_url TEXT,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS remote_flags (
    key TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS feedback (
    id TEXT PRIMARY KEY,
    user_id TEXT REFERENCES users(id),
    category TEXT NOT NULL,
    content TEXT NOT NULL,
    app_version TEXT,
    status TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN', 'PROCESSING', 'CLOSED')),
    workflow_status TEXT NOT NULL DEFAULT 'OPEN',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS reservation_rules (
    id TEXT PRIMARY KEY CHECK (id = 'default'),
    max_active_per_day INTEGER NOT NULL DEFAULT 2,
    max_user_cancellations_per_day INTEGER NOT NULL DEFAULT 2,
    cancel_cutoff_minutes INTEGER NOT NULL DEFAULT 30,
    restriction_hours_after_excess_cancel INTEGER NOT NULL DEFAULT 24,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS user_restrictions (
    user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    reservation_blocked_until INTEGER,
    cancellation_blocked_until INTEGER,
    reason TEXT NOT NULL DEFAULT '',
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS reservation_events (
    id TEXT PRIMARY KEY,
    reservation_id TEXT REFERENCES reservations(id) ON DELETE SET NULL,
    user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
    actor_user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
    action TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS reservation_events_reservation
ON reservation_events(reservation_id, created_at DESC);

CREATE TABLE IF NOT EXISTS admin_audit_logs (
    id TEXT PRIMARY KEY,
    actor_user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT,
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS admin_audit_created
ON admin_audit_logs(created_at DESC);
