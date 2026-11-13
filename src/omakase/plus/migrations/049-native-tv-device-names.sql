ALTER TABLE native_tv_devices ADD COLUMN display_name TEXT NOT NULL DEFAULT 'Apple TV'
    CHECK(length(display_name) BETWEEN 1 AND 40);
