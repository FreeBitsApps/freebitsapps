#!/usr/bin/env python3
"""Aggregates Crankle's anonymous `PublicVote` records into `AlbumStats` records.

Runs once and exits, so it works the same on GitHub Actions, a Mac mini, or anywhere
else with Python 3. See README.md for the record schema and setup.

Environment:
  CK_KEY_ID          server-to-server key ID from the CloudKit Dashboard (required)
  CK_PRIVATE_KEY     PEM contents of the matching EC private key (required)
  CK_CONTAINER       default iCloud.com.freebits.Crankle
  CK_ENV             "development" or "production" (default production)
  AGG_WINDOW_HOURS   incremental mode: recompute albums with votes modified this recently (default 3)
  AGG_FULL           "1" recomputes every album and removes orphaned stats (catches withdrawn votes)
  AGG_MIN_VOTES      don't publish stats below this many valid votes (default 5)
  DRY_RUN            "1" prints what would be written instead of writing
"""

import base64
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone

TIER_BUCKETS = 6  # S, A, B, C, D, F
MODES = {"straight", "tier", "bracket"}
BATCH_SIZE = 200  # CloudKit's per-request operation limit


# MARK: - Aggregation (pure, no network)

def valid_vote(fields):
    """Returns (songKeys, positions, trackCount) for a well-formed vote, else None."""
    song_keys = fields.get("songKeys")
    positions = fields.get("positions")
    track_count = fields.get("trackCount")
    mode = fields.get("mode")
    if mode not in MODES or not isinstance(track_count, int) or track_count < 1:
        return None
    if not isinstance(song_keys, list) or not isinstance(positions, list):
        return None
    if len(song_keys) != len(positions) or len(song_keys) != track_count:
        return None
    if len(set(song_keys)) != len(song_keys):
        return None
    upper = TIER_BUCKETS if mode == "tier" else track_count
    if not all(isinstance(p, int) and 1 <= p <= upper for p in positions):
        return None
    return song_keys, positions, track_count


def aggregate(album_key, mode, votes):
    """Builds the `AlbumStats` fields for one album and mode, or None if there are no valid votes.

    `votes` is a list of vote field dictionaries. Votes cast against a different track count than
    the most common one (e.g. a deluxe edition sharing the album key) are ignored.
    """
    parsed = [v for v in (valid_vote(f) for f in votes) if v is not None]
    if not parsed:
        return None

    track_count = Counter(p[2] for p in parsed).most_common(1)[0][0]
    parsed = [p for p in parsed if p[2] == track_count]
    bucket_count = TIER_BUCKETS if mode == "tier" else track_count

    song_keys = sorted({key for keys, _, _ in parsed for key in keys})
    index = {key: i for i, key in enumerate(song_keys)}
    buckets = [0] * (len(song_keys) * bucket_count)
    for keys, positions, _ in parsed:
        for key, position in zip(keys, positions):
            buckets[index[key] * bucket_count + (position - 1)] += 1

    return {
        "albumKey": album_key,
        "mode": mode,
        "voteCount": len(parsed),
        "bucketCount": bucket_count,
        "songKeys": song_keys,
        "buckets": buckets,
    }


def stats_record_name(album_key, mode):
    return f"{album_key}_{mode}"


# MARK: - CloudKit Web Services

class CloudKit:
    def __init__(self, container, environment, key_id, private_key_pem):
        from cryptography.hazmat.primitives import serialization

        self.base = f"/database/1/{container}/{environment}/public"
        self.key_id = key_id
        self.key = serialization.load_pem_private_key(private_key_pem.encode(), password=None)

    def _post(self, subpath, payload):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec

        body = json.dumps(payload).encode()
        date = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        body_hash = base64.b64encode(hashlib.sha256(body).digest()).decode()
        path = self.base + subpath
        signature = self.key.sign(f"{date}:{body_hash}:{path}".encode(), ec.ECDSA(hashes.SHA256()))
        request = urllib.request.Request(
            "https://api.apple-cloudkit.com" + path,
            data=body,
            headers={
                "Content-Type": "text/plain",
                "X-Apple-CloudKit-Request-KeyID": self.key_id,
                "X-Apple-CloudKit-Request-ISO8601Date": date,
                "X-Apple-CloudKit-Request-SignatureV1": base64.b64encode(signature).decode(),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            raise RuntimeError(f"CloudKit {subpath} failed: {error.code} {error.read().decode()}") from error

    def query(self, record_type, filters=None):
        """Yields the `fields` of every record matching the filters, following pagination."""
        marker = None
        while True:
            payload = {"query": {"recordType": record_type}, "resultsLimit": 200}
            if filters:
                payload["query"]["filterBy"] = filters
            if marker:
                payload["continuationMarker"] = marker
            result = self._post("/records/query", payload)
            if "serverErrorCode" in result:
                raise RuntimeError(f"CloudKit query error: {result}")
            for record in result.get("records", []):
                if "serverErrorCode" in record:
                    continue
                fields = {name: field.get("value") for name, field in record.get("fields", {}).items()}
                fields["_recordName"] = record["recordName"]
                yield fields
            marker = result.get("continuationMarker")
            if not marker:
                return

    def modify(self, operations):
        for start in range(0, len(operations), BATCH_SIZE):
            result = self._post("/records/modify", {
                "operations": operations[start:start + BATCH_SIZE],
                "atomic": False,
            })
            if "serverErrorCode" in result:
                raise RuntimeError(f"CloudKit modify error: {result}")
            for record in result.get("records", []):
                if "serverErrorCode" in record:
                    raise RuntimeError(f"CloudKit record error: {record}")


def stats_operation(stats):
    fields = {
        "albumKey": {"value": stats["albumKey"], "type": "STRING"},
        "mode": {"value": stats["mode"], "type": "STRING"},
        "voteCount": {"value": stats["voteCount"], "type": "INT64"},
        "bucketCount": {"value": stats["bucketCount"], "type": "INT64"},
        "songKeys": {"value": stats["songKeys"], "type": "STRING_LIST"},
        "buckets": {"value": stats["buckets"], "type": "INT64_LIST"},
        "updatedAt": {"value": int(time.time() * 1000), "type": "TIMESTAMP"},
    }
    return {
        "operationType": "forceReplace",
        "record": {
            "recordType": "AlbumStats",
            "recordName": stats_record_name(stats["albumKey"], stats["mode"]),
            "fields": fields,
        },
    }


def delete_operation(record_name):
    return {
        "operationType": "forceDelete",
        "record": {"recordType": "AlbumStats", "recordName": record_name},
    }


# MARK: - Run

def pairs_to_recompute(client, full, window_hours):
    """Returns ({(albumKey, mode)}, votes grouped by pair or None when only some pairs were loaded)."""
    if full:
        grouped = defaultdict(list)
        for fields in client.query("PublicVote"):
            grouped[(fields.get("albumKey"), fields.get("mode"))].append(fields)
        return set(grouped), grouped

    since = int((time.time() - window_hours * 3600) * 1000)
    recent = client.query("PublicVote", [{
        "fieldName": "___modTime",
        "comparator": "GREATER_THAN",
        "fieldValue": {"value": since, "type": "TIMESTAMP"},
    }])
    return {(f.get("albumKey"), f.get("mode")) for f in recent}, None


def main():
    min_votes = int(os.environ.get("AGG_MIN_VOTES", "5"))
    full = os.environ.get("AGG_FULL") == "1"
    window_hours = float(os.environ.get("AGG_WINDOW_HOURS", "3"))
    dry_run = os.environ.get("DRY_RUN") == "1"

    client = CloudKit(
        os.environ.get("CK_CONTAINER", "iCloud.com.freebits.Crankle"),
        os.environ.get("CK_ENV", "production"),
        os.environ["CK_KEY_ID"],
        os.environ["CK_PRIVATE_KEY"],
    )

    pairs, grouped = pairs_to_recompute(client, full, window_hours)
    print(f"{'Full' if full else 'Incremental'} run: {len(pairs)} album/mode pair(s) to recompute")

    operations = []
    for album_key, mode in sorted(p for p in pairs if p[0] and p[1]):
        if grouped is not None:
            votes = grouped[(album_key, mode)]
        else:
            filters = [{"fieldName": "albumKey", "comparator": "EQUALS", "fieldValue": {"value": album_key}}]
            votes = [v for v in client.query("PublicVote", filters) if v.get("mode") == mode]

        stats = aggregate(album_key, mode, votes)
        if stats and stats["voteCount"] >= min_votes:
            operations.append(stats_operation(stats))
            print(f"  publish {album_key} [{mode}]: {stats['voteCount']} votes")
        else:
            # Too few votes (or all withdrawn): make sure no stale stats stay public.
            operations.append(delete_operation(stats_record_name(album_key, mode)))
            print(f"  unpublish {album_key} [{mode}]")

    if full:
        # Remove stats whose votes are all gone, which an incremental run can't notice.
        live = {stats_record_name(*pair) for pair in pairs}
        for fields in client.query("AlbumStats"):
            if fields["_recordName"] not in live:
                operations.append(delete_operation(fields["_recordName"]))
                print(f"  remove orphan {fields['_recordName']}")

    if dry_run:
        print(json.dumps(operations, indent=2))
    elif operations:
        client.modify(operations)
    print("Done")


if __name__ == "__main__":
    try:
        main()
    except KeyError as missing:
        sys.exit(f"Missing environment variable {missing}")
