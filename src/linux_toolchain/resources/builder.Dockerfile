# The generator invokes Docker with the native producer platform and supplies a
# release-specific, digest-pinned multi-platform Ubuntu base. Both build roles
# use the same architecture so their host tools run without emulation.
ARG BASE_IMAGE=ubuntu:22.04@sha256:0e0a0fc6d18feda9db1590da249ac93e8d5abfea8f4c3c0c849ce512b5ef8982
FROM ${BASE_IMAGE} AS managed

ARG UBUNTU_SNAPSHOT=
ENV DEBIAN_FRONTEND=noninteractive
# Install the complete producer dependency set once. The crosstool-NG target
# extends this exact filesystem layer, and the managed target uses it directly.
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

FROM managed AS crosstool-ng

ARG CROSSTOOL_NG_VERSION
ARG CROSSTOOL_NG_SHA256
ARG CROSSTOOL_NG_ARCHIVE

COPY ${CROSSTOOL_NG_ARCHIVE} /tmp/crosstool-ng.tar.xz

RUN test -n "${CROSSTOOL_NG_VERSION}" \
    && test -n "${CROSSTOOL_NG_SHA256}" \
    && test -n "${CROSSTOOL_NG_ARCHIVE}" \
    && echo "${CROSSTOOL_NG_SHA256}  /tmp/crosstool-ng.tar.xz" | sha256sum --check --strict \
    && mkdir /tmp/crosstool-ng \
    && tar -xf /tmp/crosstool-ng.tar.xz -C /tmp/crosstool-ng --strip-components=1 \
    && cd /tmp/crosstool-ng \
    && ./configure --prefix=/opt/crosstool-ng \
    && make -j2 \
    && make install \
    && rm -rf /tmp/crosstool-ng /tmp/crosstool-ng.tar.xz

ENV PATH=/opt/crosstool-ng/bin:${PATH}
