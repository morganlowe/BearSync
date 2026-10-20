#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PKGNAME="gdrive-sync"
PKGVER="1.0.0"
BUILD_DIR="$(mktemp -d)"
trap 'rm -rf "${BUILD_DIR}"' EXIT

mkdir -p "${PROJECT_ROOT}/dist"

# Create clean source archive for makepkg
mkdir -p "${BUILD_DIR}/${PKGNAME}-${PKGVER}"
cp -r "${PROJECT_ROOT}/src" \
      "${PROJECT_ROOT}/assets" \
      "${PROJECT_ROOT}/systemd" \
      "${PROJECT_ROOT}/pyproject.toml" \
      "${PROJECT_ROOT}/gdrive-sync.desktop" \
      "${PROJECT_ROOT}/README.md" \
      "${PROJECT_ROOT}/LICENSE" \
      "${BUILD_DIR}/${PKGNAME}-${PKGVER}/"

tar -czf "${BUILD_DIR}/${PKGNAME}-${PKGVER}.tar.gz" -C "${BUILD_DIR}" "${PKGNAME}-${PKGVER}"
cp "${PROJECT_ROOT}/packaging/arch/PKGBUILD" "${BUILD_DIR}/PKGBUILD"

(
    cd "${BUILD_DIR}"
    makepkg -f --nodeps
    cp -v ${PKGNAME}-${PKGVER}-*.pkg.tar.* "${PROJECT_ROOT}/dist/"
)
echo "Arch Linux package built in ${PROJECT_ROOT}/dist/"
