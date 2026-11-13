ALTER TABLE native_tv_devices ADD COLUMN device_kind TEXT NOT NULL DEFAULT 'apple-tv'
    CHECK(device_kind IN ('apple-tv', 'iphone'));
