#!/usr/bin/env bash
# Called only by the successful push workflow on the production branch.
set -Eeuo pipefail
: "${DEPLOY_COMMIT:?}" "${DEPLOY_BRANCH:?}" "${DEPLOY_REPOSITORY:?}" "${DEPLOY_RUN_URL:?}"
[[ "$DEPLOY_COMMIT" =~ ^[0-9a-f]{40}$ ]]
[[ "$(git rev-parse HEAD)" == "$DEPLOY_COMMIT" ]]
tag="lab-ci/$DEPLOY_COMMIT"
if [[ -n "$(git ls-remote origin "refs/tags/$tag")" ]]; then
    git fetch --no-tags origin "refs/tags/$tag:refs/tags/$tag"
    [[ "$(git rev-parse "$tag^{commit}")" == "$DEPLOY_COMMIT" ]]
    printf 'Deployment authorization already exists: %s\n' "$tag"
    exit 0
fi
message="$(mktemp)"
trap 'rm -f "$message"' EXIT
python3 - "$message" <<'PY'
import json, os, sys
from pathlib import Path
Path(sys.argv[1]).write_text(json.dumps({
    'format': 1, 'commit': os.environ['DEPLOY_COMMIT'],
    'repository': os.environ['DEPLOY_REPOSITORY'], 'branch': os.environ['DEPLOY_BRANCH'],
    'workflow': '.github/workflows/ci.yml', 'run_url': os.environ['DEPLOY_RUN_URL'],
}) + '\n')
PY
git -c user.name=github-actions -c user.email=github-actions@users.noreply.github.com \
    tag --annotate "$tag" "$DEPLOY_COMMIT" --file "$message"
git push origin "refs/tags/$tag"
printf 'Authorized commit %s after successful CI.\n' "$DEPLOY_COMMIT"
