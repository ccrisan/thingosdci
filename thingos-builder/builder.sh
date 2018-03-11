#!/bin/bash

OS_DIR=/os
test -n "${TB_PR}" && TB_VERSION=${TB_PR}

# do we have required input vars?
test -z "${TB_REPO}"    && echo "environment variable TB_REPO must be set"             && exit 1
test -z "${TB_VERSION}" && echo "environment variable TB_VERSION or TB_PR must be set" && exit 1
test -z "${TB_BOARD}"   && echo "environment variable TB_BOARD must be set"            && exit 1

# exit on first error
set -e

# ssh private key
if [ -n "${TB_SSH_PRIVATE_KEY}" ]; then
    mkdir -p ~/.ssh
    echo "${TB_SSH_PRIVATE_KEY}" > ~/.ssh/id_rsa
fi

if [ -n "${TB_GIT_CREDENTIALS}" ]; then
    TB_REPO=$(echo ${TB_REPO} | sed -r "s,(https?://),\1${TB_GIT_CREDENTIALS}@,")
fi

# it appears this is not set
export USER=root

# tell git not to prompt for credentials
export GIT_TERMINAL_PROMPT=0

# git clone
git clone ${TB_REPO} ${OS_DIR}
cd ${OS_DIR}

if [ -n "${TB_PR}" ]; then
    git fetch origin pull/${TB_PR}/head:pr${TB_PR}
    git checkout pr${TB_PR}
fi

git checkout ${TB_VERSION}

# prepare working dirs
mkdir -p /mnt/dl/${TB_BOARD}
mkdir -p /mnt/ccache/.buildroot-ccache-${TB_BOARD}

ln -s /mnt/dl/${TB_BOARD} ${OS_DIR}/dl
ln -s /mnt/ccache/.buildroot-ccache-${TB_BOARD} ${OS_DIR}
ln -s /mnt/output ${OS_DIR}/output

bash -ls
#${OS_DIR}/build.sh ${TB_BOARD} clean-target
#${OS_DIR}/build.sh ${TB_BOARD} all
#${OS_DIR}/build.sh ${TB_BOARD} mkimage

