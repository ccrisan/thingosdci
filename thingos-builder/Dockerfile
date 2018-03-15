FROM ubuntu:latest

RUN apt-get update && apt-get install -y \
    git subversion build-essential wget rsync tar cpio bc \
    file python unzip nano dosfstools

ADD ./builder.sh /
ADD ./ssh_config /etc/ssh/ssh_config

USER root

CMD /builder.sh
