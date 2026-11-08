-- Every table as app/db.py first created it since the migrations baseline (cd2c74a): schema
-- only, no rows. Built once at each commit that changed app/db.py, keeping each table's
-- earliest CREATE and that build's indexes. A database older than any class field is the
-- worst case for migrations, so tests/test_security.py migrates this and expects the
-- columns of a new database. History doesn't change: never regenerate it.

-- cause: cd2c74a
CREATE TABLE [cause] (
   [slug] TEXT PRIMARY KEY,
   [label] TEXT,
   [icon] TEXT,
   [sort] INTEGER,
   [active] INTEGER
);

-- org: cd2c74a
CREATE TABLE [org] (
   [id] INTEGER PRIMARY KEY,
   [name] TEXT,
   [ein] TEXT,
   [verified_at] TEXT,
   [causes] TEXT,
   [lat] FLOAT,
   [lon] FLOAT,
   [about] TEXT,
   [intro] TEXT,
   [created_at] TEXT,
   [deactivated_at] TEXT,
   [city] TEXT,
   [zip] TEXT,
   [ntee_code] TEXT
);
CREATE UNIQUE INDEX [idx_org_ein]
    ON [org] ([ein]);

-- opening: cd2c74a
CREATE TABLE [opening] (
   [id] INTEGER PRIMARY KEY,
   [org_id] INTEGER,
   [title] TEXT,
   [about] TEXT,
   [causes] TEXT,
   [skills] TEXT,
   [lat] FLOAT,
   [lon] FLOAT,
   [neighborhood] TEXT,
   [remote] INTEGER,
   [starts_at] TEXT,
   [recurrence] TEXT,
   [slots] INTEGER,
   [orientation_required] TEXT,
   [status] TEXT,
   [notes] TEXT,
   [reply_channel] TEXT,
   [adult_confirmed] INTEGER,
   [created_at] TEXT
);

-- profile: cd2c74a
CREATE TABLE [profile] (
   [id] INTEGER PRIMARY KEY,
   [role] TEXT,
   [phone] TEXT,
   [email] TEXT,
   [display_name] TEXT,
   [contact_phone] TEXT,
   [adult_attested] INTEGER,
   [session_version] INTEGER,
   [lat] FLOAT,
   [lon] FLOAT,
   [neighborhood] TEXT,
   [radius_km] INTEGER,
   [causes] TEXT,
   [skills] TEXT,
   [language_other] TEXT,
   [volunteered_last_year] TEXT,
   [availability] TEXT,
   [frequency] TEXT,
   [heard_from] TEXT,
   [other_causes] TEXT,
   [onboarded_at] TEXT,
   [created_at] TEXT,
   [deleted_at] TEXT,
   [suspended_at] TEXT,
   [sms_opted_out] INTEGER
);
CREATE UNIQUE INDEX admin_email_live ON profile(lower(email)) WHERE role = 'admin' AND deleted_at IS NULL;
CREATE UNIQUE INDEX coordinator_email_live ON profile(lower(email)) WHERE role = 'coordinator' AND deleted_at IS NULL;
CREATE UNIQUE INDEX profile_phone_live ON profile(phone) WHERE deleted_at IS NULL;

-- org_member: cd2c74a
CREATE TABLE [org_member] (
   [id] INTEGER PRIMARY KEY,
   [org_id] INTEGER,
   [user_id] INTEGER,
   [role] TEXT
);
CREATE UNIQUE INDEX [idx_org_member_org_id_user_id]
    ON [org_member] ([org_id], [user_id]);

-- login_token: cd2c74a
CREATE TABLE [login_token] (
   [token_hash] TEXT PRIMARY KEY,
   [user_id] INTEGER,
   [expires_at] TEXT,
   [used_at] TEXT
);

-- swipe: cd2c74a
CREATE TABLE [swipe] (
   [volunteer_id] INTEGER,
   [opening_id] INTEGER,
   [liked] INTEGER,
   [created_at] TEXT,
   PRIMARY KEY ([volunteer_id], [opening_id])
);

-- match: cd2c74a
CREATE TABLE [match] (
   [id] INTEGER PRIMARY KEY,
   [opening_id] INTEGER,
   [volunteer_id] INTEGER,
   [status] TEXT,
   [created_at] TEXT,
   [decided_at] TEXT,
   [decided_by] INTEGER,
   [withdrawn_at] TEXT,
   [org_blind] INTEGER
);
CREATE UNIQUE INDEX [idx_match_opening_id_volunteer_id]
    ON [match] ([opening_id], [volunteer_id]);

-- message: cd2c74a
CREATE TABLE [message] (
   [id] INTEGER PRIMARY KEY,
   [match_id] INTEGER,
   [sender_id] INTEGER,
   [body] TEXT,
   [created_at] TEXT
);
CREATE INDEX [idx_message_match_id_created_at]
    ON [message] ([match_id], [created_at]);

-- shift: cd2c74a
CREATE TABLE [shift] (
   [id] INTEGER PRIMARY KEY,
   [match_id] INTEGER,
   [starts_at] TEXT,
   [status] TEXT,
   [kind] TEXT,
   [created_by] INTEGER,
   [marked_at] TEXT,
   [marked_by] INTEGER,
   [corrected_at] TEXT,
   [corrected_by] INTEGER,
   [cancelled_at] TEXT,
   [cancelled_by] INTEGER,
   [created_at] TEXT
);
CREATE UNIQUE INDEX shift_match_start_live ON shift(match_id, starts_at) WHERE status != 'cancelled';

-- notification: cd2c74a
CREATE TABLE [notification] (
   [id] INTEGER PRIMARY KEY,
   [kind] TEXT,
   [key] TEXT,
   [recipient_id] INTEGER,
   [sent_at] TEXT,
   [status] TEXT,
   [attempts] INTEGER
);
CREATE UNIQUE INDEX [idx_notification_key]
    ON [notification] ([key]);

-- report: cd2c74a
CREATE TABLE [report] (
   [id] INTEGER PRIMARY KEY,
   [reporter_id] INTEGER,
   [target_user_id] INTEGER,
   [target_org_id] INTEGER,
   [match_id] INTEGER,
   [reason] TEXT,
   [details] TEXT,
   [status] TEXT,
   [created_at] TEXT,
   [resolved_by] INTEGER,
   [resolved_at] TEXT
);

-- retained_phone_hash: cd2c74a
CREATE TABLE [retained_phone_hash] (
   [hash] TEXT PRIMARY KEY,
   [profile_id] INTEGER,
   [reason] TEXT,
   [blocks_json] TEXT,
   [suspended] INTEGER,
   [created_at] TEXT
);

-- block: cd2c74a
CREATE TABLE [block] (
   [id] INTEGER PRIMARY KEY,
   [blocker_id] INTEGER,
   [blocked_user_id] INTEGER,
   [blocked_org_id] INTEGER,
   [created_at] TEXT
);
CREATE UNIQUE INDEX block_once_v2 ON block(coalesce(blocked_user_id, 0), coalesce(blocked_org_id, 0), CASE WHEN blocked_user_id IS NULL THEN blocker_id ELSE 0 END);

-- claim_request: cd2c74a
CREATE TABLE [claim_request] (
   [id] INTEGER PRIMARY KEY,
   [ein] TEXT,
   [org_name] TEXT,
   [name] TEXT,
   [email] TEXT,
   [phone] TEXT,
   [status] TEXT,
   [org_id] INTEGER,
   [created_at] TEXT,
   [decided_at] TEXT,
   [decided_by] INTEGER
);

-- rate_event: cd2c74a
CREATE TABLE [rate_event] (
   [id] INTEGER PRIMARY KEY,
   [key] TEXT,
   [at] FLOAT
);
CREATE INDEX [idx_rate_event_key_at]
    ON [rate_event] ([key], [at]);

-- photo: a23bfff
CREATE TABLE [photo] (
   [key] TEXT PRIMARY KEY,
   [owner_kind] TEXT,
   [owner_id] INTEGER,
   [full] BLOB,
   [thumb] BLOB,
   [sha256] TEXT,
   [created_at] TEXT
);

-- place: 4543b3c
CREATE TABLE [place] (
   [id] INTEGER PRIMARY KEY,
   [user_id] INTEGER,
   [label] TEXT,
   [lat] FLOAT,
   [lon] FLOAT,
   [area] TEXT,
   [mode] TEXT,
   [max_minutes] INTEGER
);
CREATE INDEX [idx_place_user_id]
    ON [place] ([user_id]);

-- deck_empty: e671898
CREATE TABLE [deck_empty] (
   [id] INTEGER PRIMARY KEY,
   [volunteer_id] INTEGER,
   [today] INTEGER,
   [at] TEXT
);

-- urgent_call: 4fc598f
CREATE TABLE [urgent_call] (
   [id] INTEGER PRIMARY KEY,
   [opening_id] INTEGER,
   [org_id] INTEGER,
   [starts_at] TEXT,
   [needed] INTEGER,
   [auto_accept] INTEGER,
   [status] TEXT,
   [created_by] INTEGER,
   [created_at] TEXT,
   [ended_at] TEXT
);
CREATE INDEX [idx_urgent_call_org_id_created_at]
    ON [urgent_call] ([org_id], [created_at]);

-- identity: 5eb997b
CREATE TABLE [identity] (
   [id] INTEGER PRIMARY KEY,
   [user_id] INTEGER,
   [provider] TEXT,
   [subject] TEXT,
   [email] TEXT,
   [created_at] TEXT
);
CREATE UNIQUE INDEX identity_provider_subject ON identity(provider, subject);
CREATE INDEX [idx_identity_user_id]
    ON [identity] ([user_id]);

-- retained_identity_hash: 5eb997b
CREATE TABLE [retained_identity_hash] (
   [hash] TEXT PRIMARY KEY,
   [profile_id] INTEGER,
   [reason] TEXT,
   [blocks_json] TEXT,
   [suspended] INTEGER,
   [created_at] TEXT
);
CREATE INDEX [idx_retained_identity_hash_profile_id]
    ON [retained_identity_hash] ([profile_id]);

-- certificate: fe051ed
CREATE TABLE [certificate] (
   [id] TEXT PRIMARY KEY,
   [volunteer_id] INTEGER,
   [org_id] INTEGER,
   [period] TEXT,
   [minutes] INTEGER,
   [issued_at] TEXT,
   [withdrawn_at] TEXT,
   [snapshot] TEXT,
   [signature] TEXT
);
CREATE INDEX [idx_certificate_volunteer_id_issued_at]
    ON [certificate] ([volunteer_id], [issued_at]);
