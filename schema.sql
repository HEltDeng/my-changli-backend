PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    password_salt TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'USER' CHECK (role IN ('USER', 'ADMIN')),
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
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
