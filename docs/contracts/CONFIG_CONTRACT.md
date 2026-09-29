# Dynamic App Configuration V1

This contract is the small remote-config surface for V1.0.5. It is not a
server-driven UI engine.

## Runtime envelope

`GET /api/v1/public/bootstrap?schemaVersion=1&appVersionCode=37` returns the
existing `flags`, `announcements`, and update-policy fields, plus:

- `schemaVersion`: `1`;
- `configVersion`: a positive, monotonically increasing integer;
- `minAppVersionCode` / `maxAppVersionCode`: compatibility range;
- `issuedAt` / `expiresAt`: server timestamps (`expiresAt` may be null);
- `configHash`: uppercase SHA-256 of the canonical UTF-8 `config` JSON;
- `config`: the allow-listed navigation/card/remote-page/resource payload.

Requests without both query parameters keep the legacy response shape and do
not require an App Config client.

Canonical JSON uses UTF-8, sorted object keys, compact separators, stable
integer values, and `allow_nan=false`. The hash is an integrity check, not an
authentication mechanism.

## Safety boundary

Navigation entries may refer only to registered native `actionId` values and
registered `iconKey` values. URLs are restricted to HTTPS paths on
`mychangliapp.cn`, without userinfo or non-443 ports. The configuration cannot
provide an Intent, class name, JavaScript, HTML, native library, or executable
rule.

The backend validates response size and item counts before activation. The
Android client validates the same limits and hash before replacing its state.
The `schedule` entry is mandatory; the bottom home/features/settings tabs and
local business/API authorization are not controlled by this document.

## Local admin lifecycle

- `GET /api/v1/admin/config` includes `appConfig.active` and recent revisions.
- `POST /api/v1/admin/app-config/validate` validates a payload without writing.
- `POST /api/v1/admin/app-config/revisions` creates a numbered draft.
- `POST /api/v1/admin/app-config/activate` activates a draft with
  `expectedActiveRevision`.
- `POST /api/v1/admin/app-config/rollback` copies an earlier revision into a
  new, higher `configVersion` and activates it.

Every activation/rollback is transactional and audited. A rollback never
decrements `configVersion` and never deletes the historical target revision.

## Client fallback

The Android order is `server -> valid cache/last-known-good -> built-in
default`. A malformed, unsafe, stale, or lower-version response cannot replace
the last valid snapshot. Existing account, timetable, skin, and legacy MMKV
stores are outside this namespace.
