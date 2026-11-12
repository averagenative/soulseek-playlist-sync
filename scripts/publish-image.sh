#!/bin/sh
# Build the image for amd64 (Intel and AMD) and arm64 (Apple Silicon, Raspberry Pi) and push both
# under one tag to the GitHub container registry. It builds the current commit, not the working
# tree, and only after that commit is on GitHub, so every image matches published code.
#
#   podman login ghcr.io                        # once, with a GitHub token that has write:packages
#   scripts/publish-image.sh                    # arm64 under emulation (Fedora: qemu-user-static)
#   ARM_HOST=my-pi scripts/publish-image.sh     # arm64 natively on an ARM machine with Docker, over SSH
#
# Pushes $IMAGE:latest and $IMAGE:<short commit>. Needs podman.
set -eu

IMAGE=${IMAGE:-ghcr.io/averagenative/soulseek-playlist-sync}
cd "$(git rev-parse --show-toplevel)"

if [ -n "$(git status --porcelain)" ]; then
  echo "Commit your changes first: the image is built from the commit, not the working tree." >&2
  exit 1
fi
git fetch -q origin
if [ -z "$(git branch -r --contains HEAD)" ]; then
  echo "Push this commit to GitHub first, so the image matches the published code." >&2
  exit 1
fi

SHA=$(git rev-parse --short HEAD)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
git archive --format=tar HEAD > "$work/context.tar"
mkdir "$work/ctx"
tar -xf "$work/context.tar" -C "$work/ctx"

# Docker format, not OCI: OCI images drop the Dockerfile's HEALTHCHECK.
echo "== Building $IMAGE:$SHA for amd64"
podman build --format docker --platform linux/amd64 -t "$IMAGE:$SHA-amd64" "$work/ctx"

if [ -n "${ARM_HOST:-}" ]; then
  echo "== Building $IMAGE:$SHA for arm64 on $ARM_HOST"
  ssh "$ARM_HOST" "docker build --provenance=false -t $IMAGE:$SHA-arm64 -" < "$work/context.tar"
  ssh "$ARM_HOST" "docker save $IMAGE:$SHA-arm64" | podman load
  ssh "$ARM_HOST" "docker image rm $IMAGE:$SHA-arm64" > /dev/null
else
  echo "== Building $IMAGE:$SHA for arm64 under emulation"
  podman build --format docker --platform linux/arm64 -t "$IMAGE:$SHA-arm64" "$work/ctx"
fi

for arch in amd64 arm64; do
  got=$(podman image inspect --format '{{.Architecture}}' "$IMAGE:$SHA-$arch")
  if [ "$got" != "$arch" ]; then
    echo "$IMAGE:$SHA-$arch is $got, not $arch." >&2
    exit 1
  fi
done

podman manifest rm "$IMAGE:$SHA" > /dev/null 2>&1 || true
podman manifest create "$IMAGE:$SHA" > /dev/null
for arch in amd64 arm64; do
  podman manifest add "$IMAGE:$SHA" "containers-storage:$IMAGE:$SHA-$arch" > /dev/null
done
for tag in "$SHA" latest; do
  echo "== Pushing $IMAGE:$tag"
  podman manifest push --all --format v2s2 "$IMAGE:$SHA" "docker://$IMAGE:$tag"
done
echo "Published $IMAGE:$SHA and $IMAGE:latest (amd64, arm64)."
