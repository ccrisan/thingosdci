FROM ubuntu:latest

RUN apt-get update && apt-get install -y \
    git subversion build-essential wget rsync tar cpio bc \
    file python unzip nano dosfstools

ADD ./builder.sh /

USER root

CMD /builder.sh

