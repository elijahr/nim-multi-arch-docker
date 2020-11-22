#!/bin/bash

set -euxo pipefail

EXPECTED_NIM_VERSION=${1:-}
EXPECTED_ARCH=${2:-}

case $(uname -m) in
    *amd*64* | *x86*64* | x64 ) test "$EXPECTED_ARCH" == amd64 ;;
    *x86* | *i*86* | x32 ) test "$EXPECTED_ARCH" == i386 ;;
    *aarch64*|*arm64* ) test "$EXPECTED_ARCH" == arm64v8 ;;
    *arm* )
        test "$EXPECTED_ARCH" == arm32v5 ||
        test "$EXPECTED_ARCH" == arm32v6 ||
        test "$EXPECTED_ARCH" == arm32v7 ;;
    *ppc64le* | powerpc64el ) test "$EXPECTED_ARCH" == ppc64le ;;
    s390x ) test "$EXPECTED_ARCH" == s390x ;;
    *)
        echo "Unhandled arch $(uname -m)"
        exit 1 ;;
esac


NIM_VERSION=$(nim -v | head -n 1 | awk '{ print $4 }')
test "$NIM_VERSION" == "$EXPECTED_NIM_VERSION"
