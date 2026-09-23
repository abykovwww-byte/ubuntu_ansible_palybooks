"""Bounded request-error contract shared by endpoint and host runner."""
import http.client
import json
import socket

ERROR_CLASSES = (TimeoutError, ConnectionRefusedError, ConnectionResetError,
                 ConnectionAbortedError, BrokenPipeError, OSError, ValueError,
                 socket.gaierror, socket.herror, http.client.HTTPException,
                 http.client.RemoteDisconnected, http.client.BadStatusLine,
                 http.client.IncompleteRead, http.client.CannotSendRequest,
                 http.client.ResponseNotReady, http.client.LineTooLong)
STAGES = ('connect', 'send', 'recv', 'http', 'dns', 'unknown')


class RequestFailure:
    """Only fixed metadata crosses the workload boundary, never exception text."""
    def __init__(self, stage, error):
        self.stage = stage if stage in STAGES else 'unknown'
        self.exception_class = (type(error).__name__ if type(error) in ERROR_CLASSES
                                else 'OSError' if isinstance(error, OSError)
                                else 'HTTPException' if isinstance(error, http.client.HTTPException)
                                else 'ValueError')
        number = getattr(error, 'errno', None)
        self.errno = number if type(number) is int and -65535 <= number <= 65535 else None


def failure_payload(stage, error):
    return {'schema_version': 1, 'error': vars(RequestFailure(stage, error))}


def decode_failure(raw):
    """Reject unstructured, oversized or unexpected command output completely."""
    if not isinstance(raw, str) or len(raw) > 1024:
        return None
    try:
        payload = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    if (not isinstance(payload, dict) or set(payload) != {'schema_version', 'error'}
            or type(payload['schema_version']) is not int or payload['schema_version'] != 1):
        return None
    error = payload['error']
    if not isinstance(error, dict) or set(error) != {'stage', 'exception_class', 'errno'}:
        return None
    if error['stage'] not in STAGES or error['exception_class'] not in tuple(c.__name__ for c in ERROR_CLASSES):
        return None
    if error['errno'] is not None and (type(error['errno']) is not int or not -65535 <= error['errno'] <= 65535):
        return None
    return error.copy()
