S=/tmp/agent-tmp/-Users-tester-Development-acme-reports/11111111-2222-4333-8444-555555555555/scratchpad
cat > $S/job.sh <<'EOF'
set -euo pipefail
ROOT=$HOME/acme-reports-state
T=/tmp/acme-job; rm -rf $T; mkdir -p $T/in $T/work
cp $ROOT/data/a.json $ROOT/data/b.html $T/in/
source ./env.sh
cd ~/acme-reports
export TZ=UTC LANG=C.UTF-8 JOB_IN=$T/in JOB_WORK=$T/work
$ROOT/venv/bin/python - <<'PY'
import json
d = json.load(open("a.json"))
print("n", len(d))
PY
EOF
B64=$(base64 < $S/job.sh)
timeout 60 ssh buildvm -- bash -lc "echo $B64 | base64 -d > /tmp/acme-job.sh && (nohup bash /tmp/acme-job.sh > /tmp/acme-job.out 2>&1 &); echo started" 2>&1 | grep -v warning | tail -2
