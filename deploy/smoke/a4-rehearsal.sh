#!/usr/bin/env bash
# A4 operational rehearsal, local part (stabilization PLAN, A4).
#
#   bash deploy/smoke/a4-rehearsal.sh [results.tsv]      (or: just rehearse-a4)
#
# Runs, each in its own Compose project with placeholder secrets and synthetic
# data, and times every step:
#   backup-smoke.sh       backup/upload failure, catch-up, journal shipping failures
#   recovery-smoke.sh     failed release: expand recovery and breaking forward-fix stop
#   restore-rehearsal.sh  safe restore, journal replay, stale tail + complete purge
# Writes content-free TSV rows `suite  step-or-check  PASS|FAIL  milliseconds`
# to the results file (default: a temporary file, path printed at the end).
# Never touches production, a real token or key, or the developer stack.
# A4_PROJECT_PREFIX names the Compose projects (default alviro-a4).
# A4_SUITES selects suites (default "backup recovery restore").
set -uo pipefail

root="$(cd "$(dirname "$0")/../.." && pwd)"
prefix="${A4_PROJECT_PREFIX:-alviro-a4}"
suites=" ${A4_SUITES:-backup recovery restore} "
results="${1:-$(mktemp)}"
# Each suite's EXIT trap runs `down -v` on "$prefix-<suite>": refuse anything
# that is not a rehearsal prefix before any project exists.
case "$prefix" in
  assistant-dev* | assistant-prod* | prod*) echo "refusing A4_PROJECT_PREFIX $prefix" >&2; exit 2 ;;
esac
[[ "$prefix" =~ ^(alviro|st)-[a-z0-9][a-z0-9-]*$ ]] \
  || { echo "A4_PROJECT_PREFIX must match (alviro|st)-<lowercase name>" >&2; exit 2; }
if git -C "$root" ls-files --error-unmatch -- "$(realpath -m "$results")" > /dev/null 2>&1; then
  echo "refusing to overwrite tracked file $results" >&2
  exit 2
fi
: > "$results"
# Ctrl-C reaches the whole process group, so the running suite's own EXIT trap
# tears its stack down; this only reports and stops the remaining suites.
trap 'echo "A4 LOCAL REHEARSAL INTERRUPTED (results so far: $results)" >&2; exit 130' INT TERM
ms() { date +%s%3N; }
row() { printf '%s\t%s\t%s\t%s\n' "$@" >> "$results"; }
overall=0

# timed SUITE COMMAND...: one timing row per `== step` header (its duration up
# to the next header), one per `PASS:` line, and one for the whole suite.
timed() {
  local suite="$1" begin last now line label="" rc rcfile verdict
  shift
  rcfile="$(mktemp)"
  begin="$(ms)"
  last="$begin"
  printf '\n######## %s\n' "$suite"
  while IFS= read -r line; do
    printf '%s\n' "$line"
    case "$line" in
      "== "*)
        now="$(ms)"
        if [ -n "$label" ]; then row "$suite" "$label" PASS "$((now - last))"; fi
        label="${line#== }"
        last="$now"
        ;;
      "PASS: "*)
        now="$(ms)"
        row "$suite" "${line#PASS: }" PASS "$((now - last))"
        last="$now"
        ;;
    esac
  done < <("$@" 2>&1; echo "$?" > "$rcfile")
  rc="$(cat "$rcfile")"
  rm -f -- "$rcfile"
  verdict=PASS
  [ "$rc" = 0 ] || verdict=FAIL
  now="$(ms)"
  if [ -n "$label" ]; then row "$suite" "$label" "$verdict" "$((now - last))"; fi
  row "$suite" "(suite total)" "$verdict" "$((now - begin))"
  [ "$rc" = 0 ] || overall=1
}

if [[ "$suites" == *" backup "* ]]; then
  timed backup-smoke env SMOKE_PROJECT="$prefix-smoke" bash "$root/deploy/smoke/backup-smoke.sh"
fi
if [[ "$suites" == *" recovery "* ]]; then
  timed recovery-smoke env RECOVERY_PROJECT="$prefix-recovery" bash "$root/deploy/smoke/recovery-smoke.sh"
fi
if [[ "$suites" == *" restore "* ]]; then
  timed restore-rehearsal env REHEARSAL_PROJECT="$prefix-restore" REHEARSAL_RESULTS="$results" \
    bash "$root/deploy/smoke/restore-rehearsal.sh"
fi

printf '\n######## results (%s)\n' "$results"
column -t -s $'\t' "$results" 2> /dev/null || cat "$results"
if [ "$overall" = 0 ]; then echo "A4 LOCAL REHEARSAL OK"; else echo "A4 LOCAL REHEARSAL FAIL" >&2; fi
exit "$overall"
