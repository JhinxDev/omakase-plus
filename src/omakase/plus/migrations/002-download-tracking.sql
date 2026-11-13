ALTER TABLE anilist_plannings ADD COLUMN download_status TEXT NOT NULL DEFAULT '';
ALTER TABLE anilist_plannings ADD COLUMN download_info TEXT NOT NULL DEFAULT '';
ALTER TABLE anilist_plannings ADD COLUMN rd_torrent_id TEXT NOT NULL DEFAULT '';
