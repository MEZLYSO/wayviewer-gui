# Template file for 'wayviewer'
pkgname=wayviewer
version=0.1.0
revision=1
build_style=gnu-makefile
hostmakedepends="pkg-config"
makedepends="python3 python3-tkinter ffmpeg libsrt-devel wayland-devel mesa libdrm-devel gtk+3-devel"
depends="python3 python3-tkinter ffmpeg libsrt wayland mesa libdrm gtk+3"
short_desc="Transmisión de pantalla Wayland por SRT con chat y archivos"
maintainer="Tu Nombre <tu@email>"
license="custom"
homepage="https://github.com/<usuario>/app_monitoreo"
distfiles="https://github.com/<usuario>/app_monitoreo/archive/v${version}.tar.gz"
checksum=SKIP

# El código fuente del binario C++ no está en este repo. Compílalo aparte
# y colócalo en build/wayviewer antes de empaquetar.

do_install() {
    vinstall build/wayviewer 755 usr/bin wayviewer
    vmkdir opt/wayviewer/python
    vcopy "python/*.py" opt/wayviewer/python/
    vbin run.sh wayviewer-gui
}
" path="/home/maury/git/app_monitoreo/packaging/void/template