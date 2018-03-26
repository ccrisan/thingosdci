
WEB_PORT = 4567
WEB_SECRET = 'deadbeef'
WEB_BASE_URL = 'http://www.example.com/thingosdci'

LOG_LEVEL = 'DEBUG'

GIT_URL = 'git@github.com:owner/project.git'
REPO = 'owner/project'
BOARDS = ('raspberrypi', 'raspberrypi2')
IMAGE_FILE_FORMATS = ('.gz', '.xz')

NIGHTLY_BRANCHES = ('master', 'dev')
NIGHTLY_TAG = 'nightly-{branch}'
NIGHTLY_NAME = 'Nightly {Branch}'
NIGHTLY_VERSION = '{branch}%Y%m%d'
RELEASE_TAG_REGEX = r'\d{8}'

REDIS_HOST = '127.0.0.1'
REDIS_PORT = 6379
REDIS_PASSWORD = None
REDIS_DB = 0

DL_DIR = '/var/lib/thingosdci/dl'
CCACHE_DIR = '/var/lib/thingosdci/ccache'
OUTPUT_DIR = '/var/lib/thingosdci/output'
BUILD_LOGS_DIR = '/var/lib/thingosdci/logs'

GITHUB_ACCESS_TOKEN = 'deadbeef'

DOCKER_MAX_PARALLEL = 4
DOCKER_CONTAINER_MAX_AGE = 7200
DOCKER_LOGS_MAX_AGE = 86400 * 31
DOCKER_IMAGE_NAME = 'ccrisan/thingos-builder'
DOCKER_COMMAND = 'docker'
DOCKER_COPY_SSH_PRIVATE_KEY = False

try:
    from settingslocal import *

except ImportError:
    pass

