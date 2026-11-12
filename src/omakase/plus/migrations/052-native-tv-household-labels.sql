ALTER TABLE native_tv_devices ADD COLUMN shared_household_id TEXT REFERENCES libraries(id) ON DELETE SET NULL;
CREATE INDEX native_tv_devices_household ON native_tv_devices(shared_household_id, expires_at)
    WHERE shared_household_id IS NOT NULL;

CREATE TRIGGER native_tv_device_hide_on_member_revoke
AFTER UPDATE OF state ON household_memberships
WHEN OLD.state='active' AND NEW.state='revoked'
BEGIN
    UPDATE native_tv_devices SET shared_household_id=NULL
        WHERE shared_household_id=OLD.library_id AND user_id=OLD.user_id;
END;

CREATE TRIGGER native_tv_device_hide_on_member_delete
AFTER DELETE ON household_memberships
BEGIN
    UPDATE native_tv_devices SET shared_household_id=NULL
        WHERE shared_household_id=OLD.library_id AND user_id=OLD.user_id;
END;
