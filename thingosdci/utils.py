
import mimetypes


def branches_format(s, branch, now=None):
    s = s.format(branch=branch, Branch=branch.title(), BRANCH=branch.upper())
    if now:
        s = now.strftime(s)

    return s


def encode_multipart_formdata(fields=None, files=None):
    boundary = '----multi-part-form-data-boundary----'
    lines = []

    fields = fields or {}
    files = files or {}

    for key, value in fields.items():
        lines.append('--' + boundary)
        lines.append('Content-Disposition: form-data; name="%s"' % key)
        lines.append('')
        lines.append(value)

    for key, (filename, content) in files.items():
        content_type = mimetypes.guess_type(filename)[0] or 'application/octet-stream'
        lines.append('--' + boundary)
        lines.append('Content-Disposition: form-data; name="%s"; filename="%s"' % (key, filename))
        lines.append('Content-Type: %s' % content_type)
        lines.append('')
        lines.append(content)

    lines.append('--' + boundary + '--')
    lines.append('')

    # transform all lines into bytes
    lines = [l.encode() if isinstance(l, str) else l for l in lines]

    body = b'\r\n'.join(lines)
    content_type = 'multipart/form-data; boundary=%s' % boundary

    return content_type, body

