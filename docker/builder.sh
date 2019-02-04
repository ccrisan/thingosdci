#!/bin/bash

OS_DIR=/os

if [[ -n "${TB_BRANCH}" ]]; then
    CHECKOUT=${TB_BRANCH}
elif [[ -n "${TB_PR}" ]]; then
    CHECKOUT=pr${TB_PR}
elif [[ -n "${TB_TAG}" ]]; then
    CHECKOUT=${TB_TAG}
elif [[ -n "${TB_COMMIT}" ]]; then
    CHECKOUT=${TB_COMMIT}
fi

# do we have required input vars?
test -z "${TB_REPO}"    && echo "environment variable TB_REPO must be set"  && exit 1
test -z "${TB_BOARD}"   && echo "environment variable TB_BOARD must be set" && exit 1

# exit on first error
set -e

if [[ -n "${TB_GIT_CREDENTIALS}" ]]; then
    TB_REPO=$(echo ${TB_REPO} | sed -r "s,(https?://),\1${TB_GIT_CREDENTIALS}@,")
fi

# it appears this is not set
export USER=root

# tell git not to prompt for credentials
export GIT_TERMINAL_PROMPT=0

# git clone
git clone ${TB_GIT_CLONE_ARGS} ${TB_REPO} ${OS_DIR}
cd ${OS_DIR}

if [[ -n "${TB_PR}" ]]; then
    git fetch origin pull/${TB_PR}/head:pr${TB_PR}
fi

if [[ -n "${CHECKOUT}" ]]; then
    git checkout ${CHECKOUT}
fi

# prepare working dirs
mkdir -p /mnt/dl/${TB_BOARD}
mkdir -p /mnt/ccache/.buildroot-ccache-${TB_BOARD}

mkdir -p ${OS_DIR}/dl
mkdir -p ${OS_DIR}/.buildroot-ccache-${TB_BOARD}
mkdir -p ${OS_DIR}/output

mount --bind /mnt/dl/${TB_BOARD} ${OS_DIR}/dl
mount --bind /mnt/ccache/.buildroot-ccache-${TB_BOARD} ${OS_DIR}/.buildroot-ccache-${TB_BOARD}
mount --bind /mnt/output ${OS_DIR}/output

if [[ -n "${TB_CUSTOM_CMD}" ]]; then
    echo "executing ${TB_CUSTOM_CMD}"
    ${TB_CUSTOM_CMD}
    exit $?
fi

# decide image version
if [[ -z "${TB_VERSION}" ]]; then
    TB_VERSION=${CHECKOUT}
fi

if [[ "$TB_VERSION" =~ ^[a-f0-9]{40}$ ]]; then  # special commit id case
    TB_VERSION=git${TB_VERSION::7}
fi

export THINGOS_VERSION=${TB_VERSION}

# preserve download dir by temporarily unmounting it
umount ${OS_DIR}/dl

# clean any existing built target
if [[ "${TB_CLEAN_TARGET_ONLY}" == "true" ]]; then
    ${OS_DIR}/build.sh ${TB_BOARD} clean-target
else
    ${OS_DIR}/build.sh ${TB_BOARD} distclean
fi

# remount download dir after clean
mkdir -p ${OS_DIR}/dl
mount --bind /mnt/dl/${TB_BOARD} ${OS_DIR}/dl

# actual building
${OS_DIR}/build.sh ${TB_BOARD} all

# create images
${OS_DIR}/build.sh ${TB_BOARD} mkrelease

# write down image names
OS_NAME=$(source ${OS_DIR}/board/common/overlay/etc/version && echo ${OS_SHORT_NAME})
gz_image=${OS_NAME}-${TB_BOARD}-${THINGOS_VERSION}.img.gz
xz_image=${OS_NAME}-${TB_BOARD}-${THINGOS_VERSION}.img.xz

echo "${gz_image}" >  ${OS_DIR}/output/${TB_BOARD}/.image_files
echo "${xz_image}" >> ${OS_DIR}/output/${TB_BOARD}/.image_files

