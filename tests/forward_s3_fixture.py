"""In-memory conditional S3 fixture; never consult SDK credentials or the network."""

import hashlib
import io
from threading import RLock

from botocore.exceptions import ClientError


class MemoryS3:
    def __init__(self):
        self.objects = {}
        self.calls = []
        self.lock = RLock()
        self.unknown_write_key = None

    def get_object(self, *, Bucket, Key):
        with self.lock:
            self.calls.append(("get", Key))
            raw = self.objects.get((Bucket, Key))
            if raw is None:
                raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
            return {
                "Body": io.BytesIO(raw),
                "ContentLength": len(raw),
                "ETag": '"' + hashlib.sha256(raw).hexdigest() + '"',
            }

    def put_object(self, *, Bucket, Key, Body, IfNoneMatch=None, IfMatch=None, **kwargs):
        with self.lock:
            self.calls.append(("put", Key))
            prior = self.objects.get((Bucket, Key))
            if IfNoneMatch == "*" and prior is not None:
                raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
            if IfMatch is not None and (
                prior is None or IfMatch != '"' + hashlib.sha256(prior).hexdigest() + '"'
            ):
                raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
            assert IfNoneMatch == "*" or IfMatch is not None, "unconditional write"
            self.objects[Bucket, Key] = Body
            if Key == self.unknown_write_key:
                self.unknown_write_key = None
                raise ClientError({"Error": {"Code": "InternalError"}}, "PutObject")
            return {}
