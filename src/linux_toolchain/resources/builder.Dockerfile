# The generator invokes Docker with the native producer platform and supplies a
# release-specific, digest-pinned multi-platform Ubuntu base. Both build roles
# use the same architecture so their host tools run without emulation.
ARG BASE_IMAGE=ubuntu:22.04@sha256:0e0a0fc6d18feda9db1590da249ac93e8d5abfea8f4c3c0c849ce512b5ef8982
FROM ${BASE_IMAGE} AS builder

ARG UBUNTU_SNAPSHOT=
ENV DEBIAN_FRONTEND=noninteractive
# Install the complete producer dependency set once. SDK and compiler/runtime
# containers execute this same image.
# Normal builds use Ubuntu's configured archive mirrors. An explicit snapshot
# switches the same layer to snapshot.ubuntu.com; the minimal image then needs
# one archive-authenticated CA bootstrap before ordinary TLS verification works.
RUN set -eu; \
    if test -n "${UBUNTU_SNAPSHOT}"; then \
      sed -i -E \
        -e "s@https?://(archive.ubuntu.com|security.ubuntu.com)/ubuntu/?@https://snapshot.ubuntu.com/ubuntu/${UBUNTU_SNAPSHOT}/@" \
        -e "s@https?://ports.ubuntu.com/ubuntu-ports/?@https://snapshot.ubuntu.com/ubuntu/${UBUNTU_SNAPSHOT}/@" \
        /etc/apt/sources.list; \
      apt-get -o Acquire::Check-Valid-Until=false \
        -o Acquire::https::Verify-Peer=false update; \
      apt-get -o Acquire::https::Verify-Peer=false install -y \
        --no-install-recommends ca-certificates; \
    else \
      apt-get update; \
    fi; \
    apt-get install -y --no-install-recommends \
      autoconf automake bash bison bzip2 cmake curl file flex g++ gawk gcc git \
      gperf help2man libncurses5-dev libtool libtool-bin make ninja-build \
      patch patchelf perl python3 python3-dev rsync texinfo unzip wget xz-utils \
    && rm -rf /var/lib/apt/lists/*

ENV LC_ALL=C LANG=C TZ=UTC

ARG CROSSTOOL_NG_VERSION
ARG CROSSTOOL_NG_SHA256
ARG CROSSTOOL_NG_ARCHIVE
ARG CROSSTOOL_NG_PATCH
ARG CROSSTOOL_NG_PATCH_SHA256
ARG LINUX_TOOLCHAIN_JOBS

COPY ${CROSSTOOL_NG_ARCHIVE} /tmp/crosstool-ng.tar.xz
COPY ${CROSSTOOL_NG_PATCH} /tmp/crosstool-ng.patch

# The patch updates both verbatim-data.mk and its generated Makefile.in. Touch
# the generated file last so make does not invoke the builder's Automake.
RUN test -n "${CROSSTOOL_NG_VERSION}" \
    && test -n "${CROSSTOOL_NG_SHA256}" \
    && test -n "${CROSSTOOL_NG_ARCHIVE}" \
    && test -n "${CROSSTOOL_NG_PATCH}" \
    && test -n "${CROSSTOOL_NG_PATCH_SHA256}" \
    && test "${LINUX_TOOLCHAIN_JOBS}" -ge 1 \
    && echo "${CROSSTOOL_NG_SHA256}  /tmp/crosstool-ng.tar.xz" | sha256sum --check --strict \
    && echo "${CROSSTOOL_NG_PATCH_SHA256}  /tmp/crosstool-ng.patch" | sha256sum --check --strict \
    && mkdir /tmp/crosstool-ng \
    && tar -xf /tmp/crosstool-ng.tar.xz -C /tmp/crosstool-ng --strip-components=1 \
    && cd /tmp/crosstool-ng \
    && patch --batch -p1 < /tmp/crosstool-ng.patch \
    && touch Makefile.in \
    && ./configure --prefix=/opt/crosstool-ng \
    && make -j"${LINUX_TOOLCHAIN_JOBS}" \
    && make install \
    && rm -rf /tmp/crosstool-ng /tmp/crosstool-ng.tar.xz /tmp/crosstool-ng.patch

ENV PATH=/opt/crosstool-ng/bin:${PATH}
