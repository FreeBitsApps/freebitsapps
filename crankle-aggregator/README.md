# Crankle aggregator

Turns Crankle's anonymous per-user votes into one public `AlbumStats` record per album and
ranking mode. It runs on a schedule (GitHub Actions, see
`.github/workflows/crankle-aggregate.yml`) and talks to CloudKit Web Services with a
server-to-server key. It's plain Python 3 and can run anywhere:

```sh
pip install -r requirements.txt
python -m unittest
CK_KEY_ID=... CK_PRIVATE_KEY="$(cat eckey.pem)" DRY_RUN=1 python aggregate.py   # add CK_ENV=development to test against dev
```

## Privacy model

- Apps write a `PublicVote` record, readable and writable **only by its creator**. Other users
  can't read anyone's individual vote.
- This job reads the votes, counts them, and publishes only totals (`AlbumStats`). It never stores
  or logs who voted: the CloudKit creator ID is not read into any field.
- Stats are only published once an album and mode have at least `AGG_MIN_VOTES` (default 5) valid
  votes, and are removed again if votes are withdrawn below that.

## Record types (public database, container `iCloud.com.freebits.Crankle`)

### `PublicVote`

One per user, album, and mode. Written by the app.

| Field | Type | Notes |
|---|---|---|
| `albumKey` | String | Queryable. UPC when available, otherwise the Apple Music album ID. |
| `mode` | String | `straight`, `tier`, or `bracket`. |
| `trackCount` | Int(64) | Songs on the album. Only complete rankings are submitted. |
| `songKeys` | String list | ISRC when available, otherwise the Apple Music song ID. |
| `positions` | Int(64) list | Aligned with `songKeys`. Straight and bracket: 1...trackCount (1 = best). Tier: 1...6 for S, A, B, C, D, F. |
| `schemaVersion` | Int(64) | Currently 1. |

Record name: `sha256("<albumKey>|<mode>|<userRecordName>")` as hex, so saving again replaces the
user's vote and nobody can vote twice.

### `AlbumStats`

One per album and mode. Written only by this job.

| Field | Type | Notes |
|---|---|---|
| `albumKey` | String | Queryable. |
| `mode` | String | |
| `voteCount` | Int(64) | Valid votes counted. |
| `bucketCount` | Int(64) | trackCount, or 6 for tier. |
| `songKeys` | String list | Sorted. |
| `buckets` | Int(64) list | Row-major: `songKeys.count` rows of `bucketCount` counts. Entry `i * bucketCount + (position - 1)` is how many voters put song `i` at that position. |
| `updatedAt` | Date/Time | |

Record name: `<albumKey>_<mode>`.

## One-time CloudKit Dashboard setup

Create the schema in the **Development** environment, then use Deploy Schema Changes to Production. The server key you create in step 4 should be for the Production environment (create one per environment if you also want to test in Development).

1. **Record types:** create `PublicVote` and `AlbumStats` with the fields above.
2. **Indexes** (Schema, Indexes):
   - `PublicVote`: `recordName` Queryable; `albumKey` Queryable; `modifiedTimestamp` Queryable and Sortable;
     `createdUserRecordName` Queryable (CloudKit needs it to query a creator-only record type; the API
     error calls it `createdBy`).
   - `AlbumStats`: `recordName` Queryable; `albumKey` Queryable.
3. **Security roles** (Schema, Security Roles):
   - `PublicVote`: `_icloud` can Create; `_creator` can Read and Write; `_world` nothing.
   - `AlbumStats`: `_world` can Read; `_icloud` can Create and Write. CloudKit treats the server key like a
     signed-in user, so without this its writes are denied (`CREATE operation not permitted`). The catch is that
     a modified client could also write `AlbumStats`, so the totals could be faked. The job rewrites every
     album it touches, so a forgery lasts about an hour, and individual votes stay private either way.
4. **Server-to-server key:**
   ```sh
   openssl ecparam -name prime256v1 -genkey -noout -out eckey.pem
   openssl ec -in eckey.pem -pubout
   ```
   Dashboard, Tokens & Keys, Server-to-Server Keys: add a key and paste the public key. Keep
   `eckey.pem` private and out of git. Note the key ID.
5. **GitHub:** repo Settings, Secrets and variables, Actions:
   - Secrets `CK_KEY_ID` and `CK_PRIVATE_KEY` (the full contents of `eckey.pem`).
   - Optional variable `CK_ENV` = `development` to point the job at the Development environment. It defaults to `production`, so deploy the schema to Production first (the job fails until you do).
6. Run the workflow once by hand (Actions, Crankle aggregate, Run workflow). It defaults to a dry run.

## Schedules

- Hourly: incremental. Recomputes albums that have a vote modified in the last `AGG_WINDOW_HOURS` (3).
- Daily: full. Recomputes everything and removes stats with no votes left. Needed because
  CloudKit's public database can't report deleted records.

GitHub disables scheduled workflows after 60 days without repo activity, so if stats stop
updating, re-enable it in the Actions tab.

## Not yet verified

Full runs against the Development environment read the votes and write `AlbumStats`. Still to confirm: that `___modTime` filtering works with the index above (the full run skipped it), and that the
job can read votes written by real app users, not just records created by hand in the Dashboard.
