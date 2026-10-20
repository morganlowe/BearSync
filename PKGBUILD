# Maintainer: BearSync Contributors <maintainers@bearsync.org>
pkgname=gdrive-sync
pkgver=1.0.0
pkgrel=1
pkgdesc="Lightweight native Wayland & KDE Plasma 6 bi-directional Google Drive sync client"
arch=('any')
url="https://github.com/morganlowe/BearSync"
license=('GPL-3.0-or-later')
depends=(
    'python'
    'python-pyqt6'
    'qt6-wayland'
    'qt6-svg'
    'python-watchdog'
    'python-google-api-python-client'
    'python-google-auth'
    'python-google-auth-oauthlib'
    'python-google-auth-httplib2'
)
makedepends=(
    'python-build'
    'python-installer'
    'python-wheel'
    'python-setuptools'
)
optdepends=(
    'xdg-desktop-portal-kde: Native KDE Plasma 6 dark/light & accent color scheme tracking'
)
source=("${pkgname}-${pkgver}.tar.gz")
sha256sums=('SKIP')

build() {
    cd "${srcdir}/${pkgname}-${pkgver}"
    python -m build --wheel --no-isolation
}

package() {
    cd "${srcdir}/${pkgname}-${pkgver}"
    python -m installer --destdir="${pkgdir}" dist/*.whl

    # Install Wayland-compliant desktop entry (matching StartupWMClass=org.bearsync.gdrive-sync)
    install -Dm644 gdrive-sync.desktop \
        "${pkgdir}/usr/share/applications/org.bearsync.gdrive-sync.desktop"

    # Install systemd user service
    install -Dm644 systemd/gdrive-sync.service \
        "${pkgdir}/usr/lib/systemd/user/gdrive-sync.service"

    # Install scalable application icon & StatusNotifierItem 22x22 SVG tray icons
    install -Dm644 assets/gdrive-sync.svg \
        "${pkgdir}/usr/share/icons/hicolor/scalable/apps/gdrive-sync.svg"
    install -dm755 "${pkgdir}/usr/share/gdrive-sync/assets"
    install -Dm644 assets/*.svg assets/*.png "${pkgdir}/usr/share/gdrive-sync/assets/"

    # Install license and documentation
    install -Dm644 LICENSE "${pkgdir}/usr/share/licenses/${pkgname}/LICENSE"
    install -Dm644 README.md "${pkgdir}/usr/share/doc/${pkgname}/README.md"
}
