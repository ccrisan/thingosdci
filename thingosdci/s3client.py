
import binascii
import hashlib
import hmac
import logging
import mimetypes
import time

from calendar import timegm
from datetime import datetime
from email.utils import formatdate
from urllib.parse import urlparse, quote

from tornado import gen
from tornado.httpclient import AsyncHTTPClient, HTTPError

from thingosdci import settings


AWS_S3_BUCKET_URL = 'http://{bucket}.s3.amazonaws.com/{path}'

logger = logging.getLogger(__name__)


class S3Client(object):
    def __init__(self, access_key, secret_key, bucket, region='us-east-1'):
        self._access_key = access_key
        self._secret_key = secret_key
        self.bucket = bucket
        self._region = region
        self._service = 's3'
        self._request_scope = 'aws4_request'

    def aws_quote(self, text_bytes):
        return quote(text_bytes, safe="").replace('%7E', '~')

    def generate_url(self, path):
        """ Generates a URL for the given file path. """
        return AWS_S3_BUCKET_URL.format(bucket=self.bucket, path=path)

    def _guess_mimetype(self, filename, default='application/octet-stream'):
        if '.' not in filename:
            return default

        prefix, extension = filename.lower().rsplit(".", 1)

        if extension == 'jpg':
            extension = "jpeg"

        return mimetypes.guess_type(prefix + "." + extension)[0] or default

    def _rfc822_datetime(self, t=None):
        if t is None:
            t = datetime.utcnow()

        return formatdate(timegm(t.timetuple()), usegmt=True)

    def _get_credential_scope(self, request_date):
        return '{key}/{date}/{region}/{service}/{scope}'.format(
                key=self._access_key, date=request_date,
                region=self._region, service=self._service,
                scope=self._request_scope)

    def get_canonical_string(self, request_date, host, endpoint, params, headers, method, payload=''):
        request_date_simple = request_date[:8]

        params['AWSAccessKeyId'] = self._access_key
        params['Timestamp'] = request_date
        params['X-Amz-Credential'] = self._get_credential_scope(request_date_simple)
        params['X-Amz-Algorithm'] = params['SignatureMethod']
        params['X-Amz-Date'] = request_date
        params['X-Amz-Expires'] = '86400'

        # create canonical headers
        lowered_headers = {key.lower(): value.strip() for key, value in headers.items()}
        canonicalized_headers = [key + ':' + lowered_headers[key] for key in sorted(lowered_headers.keys())]
        canonicalized_headers = '\n'.join(canonicalized_headers)

        # create signed headers
        canonicalized_signed_headers = [key for key in sorted(lowered_headers.keys())]
        canonicalized_signed_headers = ';'.join(canonicalized_signed_headers)

        params['X-Amz-SignedHeaders'] = canonicalized_signed_headers

        # create canonical query
        canonicalized_query = [
            self.aws_quote(param) + '=' + self.aws_quote(params[param])
            for param in sorted(params.keys())
        ]
        canonicalized_query = '&'.join(canonicalized_query)

        payload_hasher = hashlib.sha256()
        payload_hasher.update(payload)
        payload = binascii.hexlify(payload_hasher.digest())

        canonical_request = (
            method + '\n' +
            endpoint + '\n' +
            canonicalized_query + '\n' +
            canonicalized_headers + '\n\n' +
            canonicalized_signed_headers + '\n' +
            payload.decode()
        )

        return canonical_request

    def get_string_to_sign(self, algorithm, request_date, canonical_request):
        credential_scope = self._get_credential_scope(request_date[:8])
        credential_scope = credential_scope[credential_scope.find('/') + 1:]

        string_to_sign = [algorithm, request_date, credential_scope]

        hasher = hashlib.sha256()
        hasher.update(canonical_request.encode())
        canonical_request = binascii.hexlify(hasher.digest())

        string_to_sign.append(canonical_request.decode())

        return '\n'.join(string_to_sign)

    def calculate_signature(self, request_date, host, endpoint, params, headers, method, payload=''):
        algorithm = params['SignatureMethod']

        canonical_request = self.get_canonical_string(request_date, host, endpoint, params, headers, method, payload)
        string_to_sign = self.get_string_to_sign(algorithm, request_date, canonical_request)

        request_date_simple = request_date[:8]

        digestmod = hashlib.sha256
        kdate = hmac.new(('AWS4' + self._secret_key).encode(), request_date_simple.encode(), digestmod).digest()
        kregion = hmac.new(kdate, self._region.encode(), digestmod).digest()
        kservice = hmac.new(kregion, self._service.encode(), digestmod).digest()
        ksigning = hmac.new(kservice, self._request_scope.encode(), digestmod).digest()

        signature = hmac.new(ksigning, string_to_sign.encode(), digestmod).digest()

        return binascii.hexlify(signature)

    def sign_request(self, host, endpoint, params, headers, method, payload=''):
        request_date = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())

        signature = self.calculate_signature(request_date, host, endpoint, params, headers, method, payload)

        canonical_query = [
            self.aws_quote(param) + '=' + self.aws_quote(params[param])
            for param in sorted(params.keys())
        ]
        canonical_query = '&'.join(canonical_query)

        return 'http://{host}{endpoint}?{query}&X-Amz-Signature={signature}'.format(
                host=host, endpoint=endpoint, query=canonical_query, signature=self.aws_quote(signature))

    @gen.coroutine
    def upload(self, path, data, headers=None):
        if isinstance(data, str):
            data = data.encode('utf8')

        headers = headers or {}

        client = AsyncHTTPClient()
        method = 'PUT'
        url = self.generate_url(path)
        url_object = urlparse(url)
        params = {
            'SignatureMethod': 'AWS4-HMAC-SHA256'
        }

        headers.update({
            'Content-Length': str(len(data)),
            'Content-Type': self._guess_mimetype(path),
            'Date': self._rfc822_datetime(),
            'Host': url_object.hostname,
            'X-Amz-Content-sha256': hashlib.sha256(data).hexdigest(),
        })

        try:
            response = yield client.fetch(
                self.sign_request(
                    url_object.hostname,
                    url_object.path,
                    params,
                    headers,
                    method,
                    data
                ),
                method=method,
                body=data,
                connect_timeout=settings.UPLOAD_REQUEST_TIMEOUT,
                request_timeout=settings.UPLOAD_REQUEST_TIMEOUT,
                headers=headers
            )

        except HTTPError as error:
            logger.error(error)
            if error.response:
                logger.error(error.response.body)

            raise gen.Return(None)

        raise gen.Return(response)
