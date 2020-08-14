"""
Utility functions for PDF library.
Taken from PyPDF2 with modifications (see LICENSE.PyPDF2).
"""


def pair_iter(lst):
    i = iter(lst)
    while True:
        try:
            x1 = next(i)
        except StopIteration:
            return
        try:
            x2 = next(i)
        except StopIteration:
            raise ValueError('List has odd number of elements')
        yield x1, x2


def read_until_whitespace(stream, maxchars=None):
    """
    Reads non-whitespace characters and returns them.
    Stops upon encountering whitespace or when maxchars is reached.
    """
    if maxchars == 0:
        return b''

    def _build():
        stop_at = None if maxchars is None else stream.tell() + maxchars
        while maxchars is None or stream.tell() < stop_at:
            tok = stream.read(1)
            if tok.isspace() or not tok:
                break
            yield tok
    return b''.join(_build())


PDF_WHITESPACE = b' \n\r\t\x00'


def read_non_whitespace(stream):
    """
    Finds and reads the next non-whitespace character (ignores whitespace).
    """
    tok = PDF_WHITESPACE[0]
    while tok in PDF_WHITESPACE:
        tok = stream.read(1)
    return tok


def skip_over_whitespace(stream):
    """
    Similar to readNonWhitespace, but returns a Boolean if more than
    one whitespace character was read.
    """
    tok = PDF_WHITESPACE[0]
    cnt = 0
    while tok in PDF_WHITESPACE:
        tok = stream.read(1)
        cnt += 1
    return cnt > 1


def skip_over_comment(stream):
    tok = stream.read(1)
    stream.seek(-1, 1)
    if tok == b'%':
        while tok not in (b'\n', b'\r'):
            tok = stream.read(1)


def read_until_regex(stream, regex, ignore_eof=False):
    """
    Reads until the regular expression pattern matched (ignore the match)
    Raise PdfStreamError on premature end-of-file.
    :param bool ignore_eof: If true, ignore end-of-line and return immediately
    :param regex: regex to match
    :param stream: stream to search
    """
    name = b''
    while True:
        tok = stream.read(16)
        if not tok:
            # stream has truncated prematurely
            if ignore_eof:
                return name
            else:
                raise PdfStreamError("Stream has ended unexpectedly")
        m = regex.search(tok)
        if m is not None:
            name += tok[:m.start()]
            stream.seek(m.start()-len(tok), 1)
            break
        name += tok
    return name


class RC4:

    def __init__(self, key):
        sigma = bytearray(range(256))
        j = 0
        for i in range(256):
            j = (j + sigma[i] + key[i % len(key)]) % 256
            sigma[i], sigma[j] = sigma[j], sigma[i]

        self.sigma = sigma
        self.i = self.j = 0

    def __next__(self):
        sigma = self.sigma
        self.i = i = (self.i + 1) % 256
        self.j = j = (self.j + sigma[i]) % 256
        sigma[i], sigma[j] = sigma[j], sigma[i]
        t = sigma[(sigma[i] + sigma[j]) % 256]
        return t

    def __iter__(self):
        return self

    def crypt(self, data):
        return bytearray(b ^ t for b, t in zip(data, self))


def rc4_encrypt(key, plaintext):
    return RC4(key).crypt(plaintext)


class PyPdfError(Exception):
    pass


class PdfReadError(PyPdfError):
    pass


class PageSizeNotDefinedError(PyPdfError):
    pass


class PdfReadWarning(UserWarning):
    pass


class PdfStreamError(PdfReadError):
    pass