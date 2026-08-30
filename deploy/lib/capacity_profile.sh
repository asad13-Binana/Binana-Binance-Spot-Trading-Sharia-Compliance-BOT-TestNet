#!/usr/bin/env bash
# Called only by root-owned setup/installer after immutable instance identity.
# This is not a claim that measured resource consumption or host soak passed.
apply_capacity_profile(){
  local profile=${DEPLOYMENT_PROFILE:-four-bot-oracle}
  local ram=11264 total=14336 disk=80 other
  case "$profile" in
    four-bot-oracle) ;;
    single-bot-testnet-experiment)
      [[ "$INSTANCE_MODE" == testnet ]] || { echo 'ERROR: experimental profile is TestNet-only' >&2; return 1; }
      ram=7168; total=11264; disk=20
      if command -v docker >/dev/null 2>&1; then
        other=$(docker ps --format '{{.ID}} {{.Label "com.docker.compose.project"}}') || { echo 'ERROR: cannot establish host occupancy' >&2; return 1; }
        # Unlabelled containers also count; never mistake an unknown neighbour
        # for proof that the machine is dedicated to this single experiment.
        other=$(printf '%s\n' "$other" | awk -v project="$COMPOSE_PROJECT_NAME" 'NF && $2 != project {print $1}')
        [[ -z "$other" ]] || { echo 'ERROR: single-bot profile refuses another running Compose project' >&2; return 1; }
      fi
      ;;
    *) echo 'ERROR: unsupported DEPLOYMENT_PROFILE' >&2; return 1 ;;
  esac
  # These floors cannot be reduced by a private config override. Experimental
  # values are explicit, not a weakening of the shared four-bot contract.
  MIN_PHYSICAL_MEMORY_MIB=$ram
  MIN_TOTAL_MEMORY_MIB=$total
  MIN_FREE_DISK_GIB=$disk
  MIN_CPU_COUNT=2
  export DEPLOYMENT_PROFILE="$profile"
}
