
WEB_PORT = 4567
WEB_SECRET = 'deadbeef'
WEB_BASE_URL = 'http://www.example.com/thingosdci'

LOG_LEVEL = 'DEBUG'

GIT_URL = 'git@github.com:author/project.git'
BOARDS = ('raspberrypi', 'raspberrypi2')
IMAGE_FILE_FORMATS = ('.gz', '.xz')

BRANCHES_RELEASE = ('master', 'dev')
BRANCHES_LATEST_TAG = 'latest-{branch}'
BRANCHES_LATEST_RELEASE_NAME = 'Nightly {Branch}'
BRANCHES_LATEST_VERSION = '{branch}%Y%m%d'

REDIS_HOST = '127.0.0.1'
REDIS_PORT = 6379
REDIS_PASSWORD = None
REDIS_DB = 1

DL_DIR = '/var/lib/thingosdci/dl'
CCACHE_DIR = '/var/lib/thingosdci/ccache'
OUTPUT_DIR = '/var/lib/thingosdci/output'

GITHUB_ACCESS_TOKEN = 'deadbeef'

DOCKER_MAX_PARALLEL = 4
DOCKER_IMAGE_NAME = 'thingos-builder'
DOCKER_COMMAND = 'docker'
DOCKER_COPY_SSH_PRIVATE_KEY = False

try:
    from settingslocal import *

except ImportError:
    pass
