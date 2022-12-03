
WEB_PORT = 4567
WEB_SECRET = 'deadbeef'
WEB_BASE_URL = 'http://www.example.com/thingosdci'

LOG_LEVEL = 'DEBUG'

GIT_URL = 'git@github.com:owner/project.git'
GIT_CLONE_DEPTH = -1
REPO = 'owner/project'
BOARDS = ('raspberrypi', 'raspberrypi2')
IMAGE_FILE_FORMATS = ('.gz', '.xz')

NIGHTLY_BRANCHES = ('master', 'dev')
NIGHTLY_TAG = 'nightly-{branch}'
NIGHTLY_NAME = 'Nightly {Branch}'
NIGHTLY_VERSION = '{branch}%Y%m%d'
NIGHTLY_FIXED_HOUR = None
RELEASE_TAG_REGEX = r'\d{8}'
PULL_REQUESTS = False
CLEAN_TARGET_ONLY = False

DL_DIR = '/var/lib/thingosdci/dl'
CCACHE_DIR = '/var/lib/thingosdci/ccache'
OUTPUT_DIR = '/var/lib/thingosdci/output'
BUILD_LOGS_DIR = '/var/lib/thingosdci/logs'
PERSIST_DIR = '/var/lib/thingosdci/persist'

UPLOAD_REQUEST_TIMEOUT = 600  # seconds
UPLOAD_SERVICE_BUILD_TYPES = ('nightly', 'tag')

REPO_SERVICE = 'github'

GITHUB_ACCESS_TOKEN = 'deadbeef'
GITHUB_REQUEST_TIMEOUT = 20  # seconds

BITBUCKET_USERNAME = 'owner'
BITBUCKET_PASSWORD = 'deadbeef'
BITBUCKET_REQUEST_TIMEOUT = 20  # seconds

GITLAB_PROJECT_ID = 1234
GITLAB_REQUEST_TIMEOUT = 20  # seconds
GITLAB_ACCESS_TOKEN = 'deadbeef'

DOCKER_MAX_PARALLEL = 4
DOCKER_CONTAINER_MAX_AGE = 3600 * 12  # seconds
DOCKER_LOGS_MAX_AGE = 86400 * 31  # seconds
DOCKER_IMAGE_NAME = 'ccrisan/thingos-builder'
DOCKER_COMMAND = 'docker'
DOCKER_COPY_SSH_PRIVATE_KEY = False
DOCKER_ENV_FILE = None

LOOP_DEV_RANGE = (10, 19)  # from /dev/loop10 to /dev/loop19

S3_UPLOAD_BUILD_TYPES = ()  # 'nightly', 'tag'
S3_UPLOAD_ACCESS_KEY = 'deadbeef'
S3_UPLOAD_SECRET_KEY = 'secret'
S3_UPLOAD_BUCKET = 'bucket'
S3_UPLOAD_PATH = 'thingos'
S3_UPLOAD_FILENAME_MAP = lambda n: n
S3_UPLOAD_ADD_RELEASE_LINK = False
S3_UPLOAD_STORAGE_CLASS = 'STANDARD'

# Called with <image_file> <board> <fmt> <build_type>
RELEASE_SCRIPT = None


try:
    from settingslocal import *

except ImportError:
    pass
