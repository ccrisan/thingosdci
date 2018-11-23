#!/bin/bash

cd $(dirname $0)
prog=$(basename $0)
prog=${prog%.*}
prog_dir=$(pwd)
venv=${venv:-~/venvs/thingosdci}

trap '' HUP

source ${venv}/bin/activate

if [[ -z "$1" ]]; then
    echo "Usage: $0 start|stop|kill|restart|status"
    echo "       $0 shell [board]"
    echo "       $0 trigger nightly <branch>"
    echo "       $0 trigger tag <tag>"
    exit 1
fi

logfile=${prog_dir}/${prog}.log
pidfile=${prog_dir}/${prog}.pid

function start() {
    export PYTHONPATH=${PYTHONPATH}:${prog_dir}
    thingosdci &>> ${logfile} &
    echo $! > ${pidfile}
}

function stop() {
    if [[ -r "${pidfile}" ]]; then
        pid=$(cat ${pidfile}) || return 1
        kill ${pid} &>/dev/null || return 1
        count="0"
        while kill -0 ${pid} &>/dev/null; do
            sleep 1
            count=$((count + 1))
            if [[ ${count} -gt "10" ]]; then
                break
            fi
        done

        if [[ ${count} -le "10" ]]; then
            rm -f ${pidfile}
        fi
    fi

    if ps aux | grep python | grep ${prog} | grep $(basename ${prog_dir}) &>/dev/null; then
        return 1
    else
        return 0
    fi
}

function killit() {
    if ps aux | grep python | grep ${prog} | grep $(basename ${prog_dir}) | \
        tr -s ' ' | cut -d ' ' -f 2 | xargs kill -9; then

        return 0
    else
        return 1
    fi
}

function status() {
    if [[ -r "${pidfile}" ]]; then
        kill -0 $(cat ${pidfile}) &>/dev/null && return 0
    fi

    if ps aux | grep python | grep ${prog} | grep $(basename ${prog_dir}) &>/dev/null; then
        return 0
    fi

    return 1
}

case "$1" in
    start)
        if status; then
            echo "${prog} already started"
            exit 0
        fi

        if start; then
            echo "${prog} started"
        else
            echo "${prog} failed to start"
            exit 1
        fi

        ;;

    stop)
        if ! status; then
            echo "${prog} not running"
            exit 0
        fi

        if stop; then
            echo "${prog} stopped"
        else
            echo "${prog} failed to stop"
            exit 1
        fi

        ;;

    kill)
        if ! status; then
            echo "${prog} not running"
            exit 0
        fi

        if killit; then
            echo "${prog} killed"
        else
            echo "failed to kill ${prog}"
            exit 1
        fi

        ;;

    restart)
        if status; then
            if stop; then
                echo "${prog} stopped"
            else
                echo "${prog} failed to stop"
                exit 1
            fi
        fi

        if start; then
            echo "${prog} started"
        else
            echo "${prog} failed to start"
            exit 1
        fi

        ;;

    status)
        if status; then
            echo "${prog} running"
        else
            echo "${prog} stopped"
        fi

        ;;

    shell)
        export PYTHONPATH=${PYTHONPATH}:${prog_dir}
        thingosdci $*
        ;;

    trigger)
        port=$(cat settingslocal.py | grep WEB_PORT | tr -d ' ' | cut -d '=' -f 2)
        if [[ -z "${port}" ]]; then
            port=4567
        fi

        if [[ "$2" == "nightly" ]]; then
            query="type=nightly&branch=$3"
        elif [[ "$2" == "tag" ]]; then
            query="type=tag&tag=$3"
        else
            echo "unknown type: $2"
            exit 1
        fi

        curl -Ss -X POST "http://127.0.0.1:${port}/trigger?${query}"
        ;;

    *)
        echo "unknown option: $1"
        exit 1

esac
