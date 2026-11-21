"""Private review notes and independent ratings for finished TV seasons."""

from __future__ import annotations

import math

MAX_COMMENT = 500


def clean_comment(value: str) -> str:
    value = value.strip()
    if len(value) > MAX_COMMENT or any(ord(char) < 32 and char not in "\n\r\t" for char in value):
        raise ValueError("Keep your comment to 500 characters.")
    return value


def read(db, user_id, domain, canonical_id):
    return {
        row["season_number"]: {"rating": row["rating"], "comment": row["comment"]}
        for row in db.execute(
            """SELECT season_number,rating,comment FROM title_reviews
               WHERE user_id=? AND domain=? AND canonical_id=?""",
            (user_id, domain, str(canonical_id)),
        )
    }


def save(db, user_id, domain, canonical_id, *, comment, season=0, rating=None):
    comment = clean_comment(comment)
    if rating is not None and (
        not math.isfinite(rating) or not 1 <= rating <= 10 or rating * 2 != int(rating * 2)
    ):
        raise ValueError("Choose a rating from 1 to 10 in half steps.")
    keys = (user_id, domain, str(canonical_id), season)
    if rating is None and not comment:
        db.execute(
            "DELETE FROM title_reviews WHERE user_id=? AND domain=? AND canonical_id=? AND season_number=?",
            keys,
        )
        return
    db.execute(
        """INSERT INTO title_reviews(user_id,domain,canonical_id,season_number,rating,comment)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(user_id,domain,canonical_id,season_number) DO UPDATE SET
             rating=excluded.rating, comment=excluded.comment,
             updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')""",
        (*keys, rating, comment),
    )


def completed_seasons(card):
    """Require a whole known season and a watched record for every episode."""
    finished = set()
    for season in card.get("seasons", ()):
        number = season["season_number"]
        count = season.get("episode_count")
        episodes = season.get("episodes") or ()
        if not number or not season.get("episodes_loaded") or not count or len(episodes) != count:
            continue
        if all(episode["watched"] and episode["released"] for episode in episodes):
            finished.add(number)
    return finished
