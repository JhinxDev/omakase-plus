"""Accepted old-source recovery against populated current-schema synthetic data."""

import hashlib
import importlib.util
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
from pathlib import Path

from omakase.plus.db import run_migrations

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "deploy/player-recovery"
BASE = "c9d491ccbdfa68aedd31692028da5820fcd6638d"


def generator():
    spec = importlib.util.spec_from_file_location("recovery_generator", PACKAGE / "generate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_recovery_is_deterministic_pinned_and_minimal():
    module = generator()
    manifest = module.generate(check=True)
    assert manifest["accepted_commit"] == BASE
    assert len(manifest["files"]) == 3
    for item in manifest["files"]:
        source = (PACKAGE / item["overlay_path"]).read_bytes()
        assert hashlib.sha256(source).hexdigest() == item["overlay_sha256"]
        assert b"web_playback" not in source
        assert item["container_target"].startswith(
            "/usr/local/lib/python3.12/site-packages/omakase/plus/"
        )
    try:
        module.generate(check=True, base="0" * 40)
    except ValueError:
        pass
    else:
        raise AssertionError("Unapproved base accepted")


def seed(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    run_migrations(db)
    for uid in (1, 2):
        db.execute(
            "INSERT INTO users(id,email,password_hash) VALUES(?,?,?)",
            (uid, f"synthetic-{uid}@example.invalid", "fixture-hash"),
        )
        db.execute(
            "INSERT INTO tracker_bridge_preferences(user_id,enabled,updated_at) VALUES(?,1,1)",
            (uid,),
        )
    stamp = "2026-10-31T12:00:00Z"
    for mid, uid, aid, source in (
        (1, 1, 101, "manual"),
        (2, 1, 102, "web_player"),
        (3, 2, 103, "web_player"),
    ):
        db.execute(
            "INSERT INTO media_monitors(id,user_id,anilist_id,title,season_number,expected_episodes) VALUES(?,?,?,?,1,12)",
            (mid, uid, aid, f"Synthetic {aid}"),
        )
        db.execute(
            "INSERT INTO watch_entries(id,user_id,monitor_id,anilist_id,season_number,status,progress_episodes,episode_total,last_source,last_observed_at) VALUES(?,?,?,?,1,'watching',2,12,?,?)",
            (mid, uid, mid, aid, source, stamp),
        )
        db.execute(
            "INSERT INTO watch_events(id,watch_entry_id,user_id,source,event_key,kind,episode,confidence,disposition,reason,observed_at,payload_json) VALUES(?,?,?,?,?,'progress',2,'exact','accepted','state_updated',?,'{}')",
            (mid, mid, uid, source, f"{source}:synthetic-{mid}", stamp),
        )
    db.execute(
        "INSERT INTO watch_confirmations(id,watch_entry_id,watch_event_id,user_id,proposed_episode,prompt) VALUES(1,2,2,1,2,'Synthetic retained confirmation')"
    )
    db.execute(
        "INSERT INTO tracker_sync_outbox(id,watch_entry_id,watch_event_id,user_id,action,progress_episodes,sync_state,attempt_count,last_error) VALUES(1,2,2,1,'progress',2,'failed',2,'Synthetic retained retry')"
    )
    library = db.execute("SELECT library_id FROM personal_libraries WHERE user_id=1").fetchone()[0]
    target, version = "d" * 64, "a" * 64
    db.execute(
        "INSERT INTO web_playback_resumes(target_key,user_id,library_id,domain,canonical_id,season_number,episode_number,media_version,duration_seconds,position_seconds,revision,intervals_json,audio_id,text_id,updated_at) VALUES(?,1,?,'anime','102',1,2,?,100,44,3,'[[0,44]]','2','3',1)",
        (target, library, version),
    )
    db.execute(
        "INSERT INTO web_playback_sessions(session_id,target_key,user_id,binding_digest,snapshot_json,revision,created_at,expires_at,last_seen) VALUES('retained-player-session',?,1,?,'{}',3,1,9999999999,2)",
        (target, "b" * 64),
    )
    db.execute(
        "INSERT INTO web_playback_events(session_id,seq,fingerprint,result_json) VALUES('retained-player-session',1,?,'{\"retained\":true}')",
        ("c" * 64,),
    )
    db.execute(
        "INSERT INTO web_playback_watch(user_id,library_id,anilist_id,season_number,episode_number,media_version,covered_seconds,duration_seconds,revision,last_played_at) VALUES(1,?,102,1,2,?,44,100,3,?)",
        (library, version, stamp),
    )
    db.execute(
        "INSERT INTO deferred_completion_cleanup(monitor_id,user_id,requested_at) VALUES(2,1,?)",
        (stamp,),
    )
    db.execute(
        "INSERT INTO web_playback_remux(job_id,media_version,actor_user_id,state,created,expires,phase,artifact_json) VALUES(?,?,1,'READY',1,9999999999,'complete','{}')",
        ("e" * 32, version),
    )
    db.execute(
        "INSERT INTO web_playback_cycle_coverage(target_key,barrier,intervals_json) VALUES(?,0,'[[0,44]]')",
        (target,),
    )
    db.execute(
        "INSERT INTO web_playback_completions(user_id,anilist_id,season_number,episode_number,media_version,barrier,completed_at) VALUES(1,102,1,1,?,0,?)",
        (version, stamp),
    )
    db.execute(
        "INSERT INTO sessions(id,user_id,expires_at) VALUES(?,1,'2035-01-01T00:00:00+00:00')",
        ("f" * 64,),
    )
    db.commit()
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    before = "\n".join(db.iterdump())
    db.close()
    return before


CHILD = r"""
import hashlib,json,pathlib,sqlite3,sys
source,dbfile,mode = sys.argv[1:]
sys.path.insert(0,source)
from omakase.plus.watch.repository import get_watch_entry
db = sqlite3.connect(pathlib.Path(dbfile).as_uri()+'?mode=ro',uri=True,check_same_thread=False)
db.row_factory = sqlite3.Row
before = '\n'.join(db.iterdump())
if mode == 'unpatched':
    try:
        get_watch_entry(db,user_id=1,monitor_id=2)
    except ValueError as error:
        assert str(error) == 'last_source is invalid'
        print(json.dumps({'old_binary_rejects_web_player':True}))
        sys.exit(0)
    raise AssertionError('Unpatched accepted model unexpectedly accepts web_player')
from omakase.plus.watch.models import WatchEvent
from omakase.plus import product_views,viewing_history
assert get_watch_entry(db,user_id=1,monitor_id=2).last_source == 'web_player'
assert get_watch_entry(db,user_id=1,monitor_id=1).last_source == 'manual'
assert get_watch_entry(db,user_id=1,monitor_id=3) is None
assert WatchEvent.progress(source='web_player',episode=2,event_key='synthetic').source == 'web_player'
titles = product_views.library_titles(db,user_id=1)
assert next(t for t in titles if t['id']==2)['last_source_label'] == 'Web player'
assert next(t for t in titles if t['id']==1)['last_source_label'] == 'Manual'
assert all(t['id'] != 3 for t in titles)
detail = product_views.title_detail(db,user_id=1,monitor_id=2)
assert any(e['source_label']=='Web player' for e in detail['watch_timeline'])
assert any(e['source_label']=='Web player' for e in detail['pending_confirmations'])
activity = product_views.activity_view(db,user_id=1)
assert any(e['source_label']=='Web player' for e in activity['watch_activity'])
history = viewing_history.viewing_history(db,1)
assert any(e['source_label']=='Web player' for e in history['items'])
assert not any(e['anilist_id']==103 for e in history['items'])
assert all(not e['watch_time_known'] for e in history['items'])
from fastapi import FastAPI
from fastapi.testclient import TestClient
from omakase.plus import routes
from omakase.plus.deps import get_db
from omakase.plus.anime_synopsis import cached_synopsis
import httpx
outbound=[]
def deny(*args,**kwargs):
    outbound.append(True)
    raise AssertionError('Recovery read tried provider HTTP')
httpx.HTTPTransport.handle_request=deny
httpx.AsyncHTTPTransport.handle_async_request=deny
async def synopsis(db,*,user_id,anilist_id):
    return cached_synopsis(db,anilist_id)
routes.ensure_anime_synopsis=synopsis
app=FastAPI()
app.dependency_overrides[get_db]=lambda:db
app.include_router(routes.router)
statuses={}
with TestClient(app) as client:
    client.cookies.set('omakase_session','f'*64)
    for path in ('/plus/home','/plus/library','/plus/library/2','/plus/activity'):
        response=client.get(path,follow_redirects=False)
        statuses[path]=response.status_code
        assert response.status_code==200,(path,response.status_code,response.text[:500])
        assert 'Synthetic 103' not in response.text
assert not outbound
assert '\n'.join(db.iterdump()) == before
assert db.total_changes == 0
assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
assert db.execute('PRAGMA foreign_key_check').fetchall()==[]
counts={name:db.execute('SELECT COUNT(*) FROM '+name).fetchone()[0] for name in
 ('web_playback_resumes','web_playback_sessions','web_playback_events','web_playback_watch',
  'web_playback_remux','web_playback_cycle_coverage','web_playback_completions','tracker_sync_outbox','tracker_bridge_preferences')}
assert all(counts.values())
assert db.execute('SELECT SUM(enabled) FROM tracker_bridge_preferences').fetchone()[0]==2
assert not any('web_playback' in name for name in sys.modules)
for name,module in tuple(sys.modules.items()):
    if (name=='omakase' or name.startswith('omakase.')) and getattr(module,'__file__',None):
        assert pathlib.Path(module.__file__).resolve().is_relative_to(pathlib.Path(source).resolve()),name
print(json.dumps({'old_source':source,'http':statuses,'provider_requests':0,'db_total_changes':0,
 'preserved_counts':counts,'logical_sha256':hashlib.sha256(before.encode()).hexdigest()}))
"""


def test_accepted_old_source_overlay_reads_populated_current_db_without_writes(tmp_path):
    dbfile = tmp_path / "current.db"
    before = seed(dbfile)
    file_hash = hashlib.sha256(dbfile.read_bytes()).hexdigest()
    accepted = tmp_path / "accepted"
    archive = subprocess.check_output(["git", "archive", BASE, "src/omakase"], cwd=ROOT)
    total = 0
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        for member in tar:
            if not member.isfile():
                continue
            path = Path(member.name)
            assert not path.is_absolute() and ".." not in path.parts and member.size <= 8 * 1024**2
            total += member.size
            assert total <= 64 * 1024**2
            destination = accepted / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(tar.extractfile(member).read())
    script = tmp_path / "verify.py"
    script.write_text(CHILD)
    environment = {key: os.environ[key] for key in ("SystemRoot", "PATH") if key in os.environ}
    environment.update(
        TEMP=str(tmp_path),
        TMP=str(tmp_path),
        OMAKASE_PLUS_PRIVATE="true",
        OMAKASE_PLUS_WEB_PLAYER_ENABLED="false",
        OMAKASE_PLUS_REVIEW_MODE="true",
        OMAKASE_ACQUISITION_MODE="import-only",
    )

    def execute(mode):
        result = subprocess.run(
            [sys.executable, "-I", str(script), str(accepted / "src"), str(dbfile), mode],
            cwd=tmp_path,
            env=environment,
            capture_output=True,
            text=True,
            timeout=35,
        )
        assert result.returncode == 0, result.stderr[-8000:]
        return json.loads(result.stdout)

    assert execute("unpatched")["old_binary_rejects_web_player"]
    manifest = generator().generate(check=True)
    for item in manifest["files"]:
        shutil.copyfile(PACKAGE / item["overlay_path"], accepted / item["source_path"])
    result = execute("overlay")
    assert all(status == 200 for status in result["http"].values())
    assert result["db_total_changes"] == result["provider_requests"] == 0
    assert hashlib.sha256(dbfile.read_bytes()).hexdigest() == file_hash
    db = sqlite3.connect(dbfile)
    assert "\n".join(db.iterdump()) == before
    db.close()
