
import datetime
import calendar
import hashlib
import logging
import os
import re
import shlex
import subprocess
import time

from tornado import gen
from tornado import ioloop

from thingosdci import settings


CONTAINER_STATE_RUNNING = 'running'
CONTAINER_STATE_EXITED = 'exited'
CONTAINER_STATE_REMOVED = 'removed'

_CONTAINER_NAME_PREFIX = 'thingosdci-{repo}-'

logger = logging.getLogger(__name__)

_containers_by_id = {}


class ContainerException(Exception):
    pass


class Container:
    def __init__(self, _id, name, created_time):
        self.id = _id
        self.name = name
        self.created_time = created_time

        self.exit_code = None

        self._removed = False
        self._state_change_callbacks = []

    def __str__(self):
        return 'container {}'.format(self.id)

    def get_age(self):
        return int(time.time() - self.created_time)

    def get_state(self):
        if self.exit_code is None:
            return CONTAINER_STATE_RUNNING

        if not self._removed:
            return CONTAINER_STATE_EXITED

        return CONTAINER_STATE_REMOVED

    def set_exited(self, exit_code):
        if self.exit_code is not None:
            raise ContainerException('container already exited')

        self.exit_code = exit_code
        logger.debug('%s has exited (exit_code=%d)', self, exit_code)

        self._run_state_change_callbacks()

    def set_removed(self):
        if self._removed:
            raise ContainerException('container already removed')

        self._removed = True
        logger.debug('%s has been removed', self)

        self._run_state_change_callbacks()

    def add_state_change_callback(self, callback):
        self._state_change_callbacks.append(callback)

    def _run_state_change_callbacks(self):
        io_loop = ioloop.IOLoop.current()
        state = self.get_state()

        for callback in self._state_change_callbacks:
            io_loop.spawn_callback(callback, state)


class DockerException(Exception):
    pass


def _cmd(cmd, pipe_stdio=True):
    if isinstance(cmd, str):
        cmd = shlex.split(cmd)

    docker_base_cmd = shlex.split(settings.DOCKER_COMMAND)

    cmd = docker_base_cmd + cmd
    # logger.debug('executing "%s"', cmd)

    stdin = stdout = stderr = None
    if pipe_stdio:
        stdin = stdout = stderr = subprocess.PIPE

    p = subprocess.Popen(cmd, stdin=stdin, stdout=stdout, stderr=stderr, universal_newlines=True)
    stdout, stderr = p.communicate()

    if p.returncode and stderr is not None:
        raise DockerException(stderr.strip())

    return stdout


def _make_log_path(container_id):
    return os.path.join(settings.BUILD_LOGS_DIR, 'build-{}.log'.format(container_id))


def _save_log(container_id):
    log_path = _make_log_path(container_id)
    log = _cmd(['logs', container_id])

    with open(log_path, 'w') as f:
        f.write(log)

    return log


def _remove_container(container_id):
    _cmd(['container', 'rm', container_id])


def _kill_container(container_id):
    _cmd(['container', 'kill', container_id])


def _list_containers():
    name_prefix = _CONTAINER_NAME_PREFIX.format(repo=re.sub('[^a-z0-9]', '-', settings.REPO, re.IGNORECASE))
    container_info_list = []
    s = _cmd(['container', 'ls', '-a', '--no-trunc', '--format',
              '{{.ID}}%{{.Names}}%{{.CreatedAt}}%{{.Status}}'])

    lines = s.split('\n')
    lines = [l.strip() for l in lines if l.strip()]

    for line in lines:
        parts = line.split('%')

        container_id = parts[0]
        name = parts[1]
        created_at = parts[2]
        running = parts[3].startswith('Up')
        exit_code = None

        if not name.startswith(name_prefix):
            continue  # not ours

        if not running:  # determine exit code
            try:
                exit_code = int(_cmd(['wait', container_id]).strip())

            except Exception as e:
                logger.error('failed to retrieve exit code of container %s: %s', container_id, e,
                             exc_info=True)

                exit_code = 1

        created_at = ' '.join(created_at.split()[:2])
        created_at = datetime.datetime.strptime(created_at, '%Y-%m-%d %H:%M:%S')
        created_time = calendar.timegm(created_at.timetuple())

        container_info_list.append({
            'id': container_id,
            'name': name,
            'created_time': created_time,
            'running': running,
            'exit_code': exit_code
        })

    return container_info_list


@gen.coroutine
def _status_loop():
    while True:
        yield gen.sleep(1)

        try:
            container_info_list = _list_containers()

        except Exception as e:
            logger.error('failed to list docker containers: %s', e, exc_info=True)
            continue

        # see if any of the containers has exited
        seen_container_ids = set()
        for container_info in container_info_list:
            container_id = container_info['id']
            exit_code = container_info['exit_code']

            seen_container_ids.add(container_id)

            container = _containers_by_id.get(container_id)
            if not container:
                continue

            if container.get_state() == CONTAINER_STATE_RUNNING and not container_info['running']:
                container.set_exited(exit_code)

        # remove old references from _containers_by_id
        for container_id in list(_containers_by_id.keys()):
            if container_id not in seen_container_ids:
                _containers_by_id.pop(container_id)


@gen.coroutine
def _cleanup_loop():
    while True:
        yield gen.sleep(60)

        for container in _containers_by_id.values():
            # kill old containers
            state = container.get_state()
            if (state == CONTAINER_STATE_RUNNING and
                container.get_age() > settings.DOCKER_CONTAINER_MAX_AGE):

                logger.warning('%s still running after %s seconds, killing it', container, container.get_age())

                try:
                    _kill_container(container.id)

                except Exception as e:
                    logger.error('failed to kill %s: %s', container, e, exc_info=True)

            # remove the container if it's not running anymore
            elif state == CONTAINER_STATE_EXITED:
                logger.debug('saving log of %s', container)

                try:
                    _save_log(container.id)

                except Exception as e:
                    logger.error('failed to save logs of %s: %s', container, e, exc_info=True)

                logger.debug('removing %s', container)

                try:
                    _remove_container(container.id)

                except Exception as e:
                    logger.error('failed to remove %s: %s', container, e, exc_info=True)

                container.set_removed()

        # remove old logs
        for file in os.listdir(settings.BUILD_LOGS_DIR):
            path = os.path.join(settings.BUILD_LOGS_DIR, file)
            s = os.stat(path)
            age = time.time() - s.st_mtime
            if age > settings.DOCKER_LOGS_MAX_AGE:
                logger.debug('removing old log %s', path)
                try:
                    os.remove(path)

                except Exception as e:
                    logger.error('failed to remove old log %s: %s', path, e)


def run_container(env, vol, interactive=False):
    ssh_private_key_file = settings.DOCKER_COPY_SSH_PRIVATE_KEY
    if ssh_private_key_file is True:
        ssh_private_key_file = os.path.join(os.getenv('HOME'), '.ssh', 'id_rsa')

    name = _CONTAINER_NAME_PREFIX + hashlib.sha1(str(int(time.time() * 1000)).encode()).hexdigest()[:8]
    name = name.format(repo=re.sub('[^a-z0-9]', '-', settings.REPO, re.IGNORECASE))

    cmd = ['run']

    if interactive:
        cmd.append('-it')

    else:
        cmd.append('-td')

    cmd += [
        '--privileged',
        '--name', name
    ]

    if settings.DOCKER_ENV_FILE:
        cmd.append('--env-file={}'.format(settings.DOCKER_ENV_FILE))

    for k, v in env.items():
        cmd.append('-e')
        cmd.append('{}={}'.format(k, v))

    for k, v in vol.items():
        cmd.append('-v')
        cmd.append('{}:{}'.format(k, v))

    if ssh_private_key_file:
        cmd.append('-v')
        cmd.append('{}:/root/.ssh/id_rsa'.format(ssh_private_key_file))

    cmd += [
        '--cap-add=SYS_ADMIN',
        '--cap-add=MKNOD',
        settings.DOCKER_IMAGE_NAME
    ]

    if interactive:
        _cmd(cmd, pipe_stdio=not interactive)
        return None

    else:
        container_id = _cmd(cmd, pipe_stdio=not interactive).strip()

    container_info_list = _list_containers()
    for container_info in container_info_list:
        if container_info['id'] == container_id:
            container = Container(container_id, container_info['name'], container_info['created_time'])
            _containers_by_id[container_id] = container

            return container

    raise DockerException('container not present after run')


def get_container_log(container_id, last_lines=None):
    try:
        log = _save_log(container_id)

    except DockerException:
        log_path = _make_log_path(container_id)

        try:
            with open(log_path) as f:
                log = f.read()

        except Exception as e:
            logger.error('error opening log file %s: %s', log_path, e)
            log = ''

    if last_lines:
        partial_log = ''
        while last_lines > 0:
            last_lines -= 1
            p = log.rfind('\n')
            if p < 0:
                partial_log = log + partial_log
                log = ''

            else:
                partial_log = log[p:] + partial_log
                log = log[:p]

        return partial_log

    return log


def init():
    io_loop = ioloop.IOLoop.current()

    io_loop.spawn_callback(_status_loop)
    io_loop.spawn_callback(_cleanup_loop)
