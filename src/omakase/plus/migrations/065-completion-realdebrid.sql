CREATE TABLE completion_realdebrid_preferences (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    enabled INTEGER NOT NULL CHECK(enabled IN (0,1))
);

CREATE TABLE completion_realdebrid_removals (
    removal_id INTEGER PRIMARY KEY REFERENCES media_title_removals(id) ON DELETE CASCADE,
    account_binding TEXT NOT NULL CHECK(length(account_binding)=64)
);
