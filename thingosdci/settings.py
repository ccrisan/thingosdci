
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
PULL_REQUESTS = False

DL_DIR = '/var/lib/thingosdci/dl'
CCACHE_DIR = '/var/lib/thingosdci/ccache'
OUTPUT_DIR = '/var/lib/thingosdci/output'
BUILD_LOGS_DIR = '/var/lib/thingosdci/logs'

REPO_SERVICE = 'github'

GITHUB_ACCESS_TOKEN = 'deadbeef'
GITHUB_REQUEST_TIMEOUT = 20  # seconds
GITHUB_UPLOAD_REQUEST_TIMEOUT = 600  # seconds

BITBUCKET_USERNAME = 'owner'
BITBUCKET_PASSWORD = 'deadbeef'
BITBUCKET_REQUEST_TIMEOUT = 20  # seconds
BITBUCKET_UPLOAD_REQUEST_TIMEOUT = 600  # seconds

DOCKER_MAX_PARALLEL = 4
DOCKER_CONTAINER_MAX_AGE = 7200  # seconds
DOCKER_LOGS_MAX_AGE = 86400 * 31  # seconds
DOCKER_IMAGE_NAME = 'ccrisan/thingos-builder'
DOCKER_COMMAND = 'docker'
DOCKER_COPY_SSH_PRIVATE_KEY = False

try:
    from settingslocal import *

except ImportError:
    pass

