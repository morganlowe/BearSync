#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PKGNAME="gdrive-sync"
PKGVER="1.0.0"
PKGREL="1"
DEB_ARCH="all"
DEB_NAME="${PKGNAME}_${PKGVER}-${PKGREL}_${DEB_ARCH}"
STAGE_DIR="$(mktemp -d)"
trap 'rm -rf "${STAGE_DIR}"' EXIT

PKG_ROOT="${STAGE_DIR}/${DEB_NAME}"
mkdir -p "${PKG_ROOT}/DEBIAN"
mkdir -p "${PROJECT_ROOT}/dist"

# Build wheel in an isolated source copy and install into Debian stage layout (/usr/lib/python3/dist-packages)
SRC_COPY="${STAGE_DIR}/src-copy"
mkdir -p "${SRC_COPY}"
cp -r "${PROJECT_ROOT}/src" "${PROJECT_ROOT}/pyproject.toml" "${PROJECT_ROOT}/README.md" "${PROJECT_ROOT}/LICENSE" "${SRC_COPY}/"
WHEEL_TMP="$(mktemp -d)"
python3 -m build --wheel --no-isolation --outdir "${WHEEL_TMP}" "${SRC_COPY}"
python3 -m installer --destdir="${PKG_ROOT}" "${WHEEL_TMP}"/*.whl
rm -rf "${WHEEL_TMP}" "${SRC_COPY}"

# Normalize Python site-packages path to Debian's /usr/lib/python3/dist-packages
if [ -d "${PKG_ROOT}/usr/lib"/python3.*/site-packages ]; then
    mkdir -p "${PKG_ROOT}/usr/lib/python3/dist-packages"
    cp -r "${PKG_ROOT}/usr/lib"/python3.*/site-packages/* "${PKG_ROOT}/usr/lib/python3/dist-packages/"
    rm -rf "${PKG_ROOT}/usr/lib"/python3.*
fi

# Install desktop launcher, systemd user service, icons, docs
install -Dm644 "${PROJECT_ROOT}/gdrive-sync.desktop" \
    "${PKG_ROOT}/usr/share/applications/org.bearsync.gdrive-sync.desktop"
install -Dm644 "${PROJECT_ROOT}/systemd/gdrive-sync.service" \
    "${PKG_ROOT}/usr/lib/systemd/user/gdrive-sync.service"
install -Dm644 "${PROJECT_ROOT}/assets/gdrive-sync.svg" \
    "${PKG_ROOT}/usr/share/icons/hicolor/scalable/apps/gdrive-sync.svg"
install -dm755 "${PKG_ROOT}/usr/share/gdrive-sync/assets"
install -Dm644 "${PROJECT_ROOT}/assets/"*.svg "${PROJECT_ROOT}/assets/"*.png "${PKG_ROOT}/usr/share/gdrive-sync/assets/"
install -Dm644 "${PROJECT_ROOT}/README.md" \
    "${PKG_ROOT}/usr/share/doc/${PKGNAME}/README.md"
install -Dm644 "${PROJECT_ROOT}/debian/copyright" \
    "${PKG_ROOT}/usr/share/doc/${PKGNAME}/copyright"
gzip -9n -c "${PROJECT_ROOT}/debian/changelog" > \
    "${PKG_ROOT}/usr/share/doc/${PKGNAME}/changelog.Debian.gz"

INSTALLED_SIZE="$(du -sk "${PKG_ROOT}/usr" | cut -f1)"

cat > "${PKG_ROOT}/DEBIAN/control" <<EOF
Package: ${PKGNAME}
Version: ${PKGVER}-${PKGREL}
Section: net
Priority: optional
Architecture: ${DEB_ARCH}
Installed-Size: ${INSTALLED_SIZE}
Depends: python3 (>= 3.10), python3-pyqt6, qt6-wayland, libqt6svg6, python3-watchdog, python3-googleapi, python3-google-auth, python3-google-auth-oauthlib, python3-google-auth-httplib2
Recommends: xdg-desktop-portal-kde
Maintainer: BearSync Contributors <maintainers@bearsync.org>
Homepage: https://github.com/morganlowe/BearSync
Description: Native Wayland & KDE Plasma 6 bi-directional Google Drive sync client
 BearSync (gdrive-sync) is a lightweight, native Linux bi-directional Google
 Drive synchronization client designed for Wayland and KDE Plasma 6.
EOF

chmod 0755 "${PKG_ROOT}/DEBIAN"
chmod 0644 "${PKG_ROOT}/DEBIAN/control"

dpkg-deb --root-owner-group --build "${PKG_ROOT}" "${PROJECT_ROOT}/dist/${DEB_NAME}.deb"
echo "Debian package built: ${PROJECT_ROOT}/dist/${DEB_NAME}.deb"
