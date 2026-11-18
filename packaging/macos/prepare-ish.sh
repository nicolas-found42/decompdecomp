#!/bin/bash
# Build the optional ARM64 process runner for Sony's i386 compilers.
set -euo pipefail
umask 077
runtime_dir=${1:?Usage: prepare-ish.sh /absolute/path/to/new/runtime}
[[ "$runtime_dir" = /* && ! -e "$runtime_dir" && "$(uname -s)-$(uname -m)" = Darwin-arm64 ]]
mkdir -p "$(dirname "$runtime_dir")"
build_dir=$(mktemp -d "$(dirname "$runtime_dir")/.ish-build.XXXXXX")
trap 'rm -rf "$build_dir"' EXIT
cd "$build_dir"
git init -q ish
git -C ish fetch -q --depth=1 https://github.com/ish-app/ish.git ce96e698f1820b9d8db47e87c501ecb89119d7ce
git -C ish checkout -q --detach FETCH_HEAD
git -C ish submodule update --init --recursive --depth 1
export PATH="$(brew --prefix llvm)/bin:$(brew --prefix lld)/bin:$PATH"
CC="$(brew --prefix llvm)/bin/clang" uvx --from meson==1.12.1 meson setup --buildtype=release ish/build ish
ninja -C ish/build ish
curl -fsSLo libc.deb 'https://deb.debian.org/debian/pool/main/g/glibc/libc6_2.31-13+deb11u11_i386.deb'
echo '8198abd817cbcd615ce3207fb924317b53cc0b522bfe57d689757de13fe00d66  libc.deb' | shasum -a 256 -c -
ar -x libc.deb
tar -xf data.tar.xz
mkdir -p runtime/lib/i386-linux-gnu
cp ish/build/ish runtime/
cp -L lib/i386-linux-gnu/ld-linux.so.2 runtime/lib/
for library in libc.so.6 libm.so.6 libdl.so.2 libpthread.so.0; do
  cp -L "lib/i386-linux-gnu/$library" runtime/lib/i386-linux-gnu/
done
mv runtime "$runtime_dir"
