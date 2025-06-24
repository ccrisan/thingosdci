FROM ubuntu:24.04

RUN apt-get update && apt-get install -y \
    git subversion build-essential gcc-multilib \
    wget curl rsync tar cpio bc file python3 unzip \
    nano dosfstools lib32z1

ADD ./builder.sh /
ADD ./ssh_config /etc/ssh/ssh_config

USER root

CMD /builder.sh

