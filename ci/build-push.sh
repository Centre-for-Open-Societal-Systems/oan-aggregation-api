#!/usr/bin/env bash
# Build one image, push it to ECR and record the pushed digest.
#
#   ci/build-push.sh <name> <ecr-repo> <dockerfile> <context> <commit12> <extra-tag|-> <buildinfo yes|no>
#
# Tags pushed: dev-<commit12> (immutable, what the deploy pins by digest) and,
# when given, one extra tag: `develop` (develop branch; skipped for a repository with immutable
# tags) or the release version
# (git tag build) that staging/UAT pin.
# Writes .build/digest-<name> containing sha256:<digest>.
set -euo pipefail

NAME=$1; REPO=$2; DOCKERFILE=$3; CONTEXT=$4; COMMIT12=$5; EXTRA=${6:--}; BUILDINFO=${7:-no}
: "${ECR_REGISTRY:?ECR_REGISTRY not set}"

IMAGE="${ECR_REGISTRY}/${REPO}:dev-${COMMIT12}"
ARGS=()
if [ "$BUILDINFO" = "yes" ]; then
    ARGS+=(--build-arg "GIT_COMMIT=${GIT_COMMIT:-$COMMIT12}" --build-arg "BUILD_TIME=$(date -u +%Y-%m-%dT%H:%M:%SZ)")
fi
# `develop` is a moving alias. An ECR repository with immutable tags rejects a second push of it,
# and nothing depends on it (the deploy pins dev-<commit12> by digest), so skip it there. A release
# version tag is never skipped: pushing one that already exists must fail.
if [ "$EXTRA" = "develop" ]; then
    MUTABILITY=$(aws ecr describe-repositories --repository-names "$REPO" \
        --query 'repositories[0].imageTagMutability' --output text 2>/dev/null || echo MUTABLE)
    if [ "$MUTABILITY" = "IMMUTABLE" ]; then
        echo "ECR repository ${REPO} has immutable tags: not pushing the moving tag '${EXTRA}'"
        EXTRA=-
    fi
fi
TAGS=(-t "$IMAGE")
if [ "$EXTRA" != "-" ] && [ -n "$EXTRA" ]; then
    TAGS+=(-t "${ECR_REGISTRY}/${REPO}:${EXTRA}")
fi

# ${ARGS[@]+...} keeps an empty array legal under `set -u` on bash < 4.4
docker build ${ARGS[@]+"${ARGS[@]}"} -f "$DOCKERFILE" "${TAGS[@]}" "$CONTEXT"
docker push "$IMAGE"
if [ "$EXTRA" != "-" ] && [ -n "$EXTRA" ]; then
    docker push "${ECR_REGISTRY}/${REPO}:${EXTRA}"
fi

# Digest of the manifest that was just pushed (RepoDigests is filled by the push).
DIGEST=$(docker inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "$IMAGE" \
    | grep "^${ECR_REGISTRY}/${REPO}@" | head -1 | cut -d@ -f2)
[ -n "$DIGEST" ] || { echo "ERROR: no digest recorded for $IMAGE" >&2; exit 1; }
mkdir -p .build
echo "$DIGEST" > ".build/digest-${NAME}"
echo "pushed ${IMAGE}@${DIGEST}"
