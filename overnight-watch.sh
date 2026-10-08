#!/bin/bash
# Overnight watch for the ebook processor's first write-mode run (2026-10-07/08).
# Checks the 6 tables the pipeline touches, compares against baselines,
# and flags anomalies. Called by cron every 30 min; quiet unless something's off.
#
# Baselines (captured 2026-10-07 ~19:40 ADT, before the run):
#   book_trope_claims=513, trope_proposals=0, book_characters=0,
#   book_trigger_claims=0, works_with_spice=0, book_meta=52

set -euo pipefail
SB="$HOME/workspace/skills/supabase/bin/sb-query"
REF="dvhimjkrroxuatthiizc"
STATE="$HOME/workspace/ebook-processor/.overnight-watch.json"
LOG="$HOME/workspace/ebook-processor/overnight-watch.log"

log() { echo "[$(date '+%F %T %Z')] $*" | tee -a "$LOG"; }

# --- 1. Current counts ---
counts=$($SB "$REF" "
SELECT 'book_trope_claims' as tbl, COUNT(*) as n FROM book_trope_claims
UNION ALL SELECT 'trope_proposals', COUNT(*) FROM trope_proposals
UNION ALL SELECT 'book_characters', COUNT(*) FROM book_characters
UNION ALL SELECT 'book_trigger_claims', COUNT(*) FROM book_trigger_claims
UNION ALL SELECT 'works_with_spice', COUNT(*) FROM works WHERE spice_detected IS NOT NULL
UNION ALL SELECT 'book_meta', COUNT(*) FROM book_meta;
" 2>&1) || { log "ERROR: sb-query failed: $counts"; exit 1; }

# --- 2. New ebook-processor rows this run ---
new_rows=$($SB "$REF" "
SELECT 'claims' as k, COUNT(*) as n FROM book_trope_claims WHERE source_type='ebook-processor'
UNION ALL SELECT 'characters', COUNT(*) FROM book_characters WHERE source_type='ebook-processor'
UNION ALL SELECT 'triggers', COUNT(*) FROM book_trigger_claims WHERE source_type='ebook-processor';
" 2>&1) || { log "ERROR: new-rows query failed"; exit 1; }

# --- 3. Anomaly checks ---
# 3a. spice_detected out of range (CHECK constraint should prevent, verify anyway)
bad_spice=$($SB "$REF" "SELECT COUNT(*) as n FROM works WHERE spice_detected IS NOT NULL AND (spice_detected < 0 OR spice_detected > 5);" 2>&1)

# 3b. Duplicate trope claims (same work + trope_id, ignoring id)
dupes=$($SB "$REF" "
SELECT COUNT(*) as n FROM (
  SELECT work_id, trope_id, COUNT(*) c FROM book_trope_claims
  GROUP BY work_id, trope_id HAVING COUNT(*) > 1
) d;" 2>&1)

# 3c. Claims with unexpected source_type values
bad_source=$($SB "$REF" "
SELECT COUNT(*) as n FROM book_trope_claims
WHERE source_type NOT IN ('provider','ai','community','admin','user','import','ebook-processor');" 2>&1)

# 3d. Proposals missing book_key provenance
no_prov=$($SB "$REF" "SELECT COUNT(*) as n FROM trope_proposals WHERE book_key IS NULL OR book_key = '';" 2>&1)

# --- 4. Evaluate ---
alert=""
echo "$bad_spice" | grep -q '"n": 0' || alert="${alert} BAD_SPICE_RANGE;"
echo "$dupes" | grep -q '"n": 0' || alert="${alert} DUPLICATE_CLAIMS;"
echo "$bad_source" | grep -q '"n": 0' || alert="${alert} BAD_SOURCE_TYPE;"
echo "$no_prov" | grep -q '"n": 0' || alert="${alert} PROPOSALS_MISSING_BOOK_KEY;"

# Persist state for the morning summary
python3 - "$counts" "$new_rows" "$alert" <<'PYEOF'
import json, sys, os
state_path = os.path.expanduser("~/workspace/ebook-processor/.overnight-watch.json")
def parse(q):
    try: return {r['tbl' if 'tbl' in r else 'k']: r['n'] for r in json.loads(q)}
    except Exception: return {}
entry = {
    "ts": __import__('datetime').datetime.now().isoformat(),
    "counts": parse(sys.argv[1]),
    "new_rows": parse(sys.argv[2]),
    "alert": sys.argv[3].strip(),
}
hist = []
if os.path.exists(state_path):
    try: hist = json.load(open(state_path))
    except Exception: hist = []
hist.append(entry)
json.dump(hist[-48:], open(state_path, 'w'))  # keep last 24h at 30-min cadence
PYEOF

if [ -n "$alert" ]; then
  log "ALERT:$alert"
  log "counts: $counts"
  echo "WATCH_ALERT:$alert"
else
  log "OK — counts: $(echo "$counts" | python3 -c "import json,sys; print(json.loads(sys.stdin.read()))")"
fi
