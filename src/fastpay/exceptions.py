"""Exceptions raised by the FastPay client.

Kept dependency-free so ``client.py`` can be copied into any project.
"""


class FastPayError(Exception):
    """Base class for every FastPay payment error."""


class FastPayAuthError(FastPayError):
    """Raised when the client has no store credentials to send."""


class FastPayAPIError(FastPayError):
    """Raised when a FastPay call fails or FastPay refuses it.

    ``code`` is the ``code`` in FastPay's answer (``422``, ``404``, ...), or
    None when FastPay gave no answer at all (a network error, a page that is not
    FastPay's JSON); ``status_code`` is the HTTP status.
    """

    def __init__(self, message, status_code=None, code=None, payload=None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.payload = payload
