import io
import logging
import socket
import threading
import time


class OSSUploadHandler(logging.Handler):
    def __init__(self, endpoint: str, access_key: str, secret_key: str, bucket: str, prefix: str):
        super().__init__()
        import oss2

        auth = oss2.Auth(access_key, secret_key)
        self.bucket = oss2.Bucket(auth, endpoint, bucket)
        self.prefix = prefix
        self.buffer = io.StringIO()
        self.buffer_size = 0
        self.lock = threading.Lock()
        self.host = socket.gethostname()
        threading.Thread(target=self._worker, daemon=True).start()

    def emit(self, record: logging.LogRecord):
        try:
            msg = self.format(record) + "\n"
            with self.lock:
                self.buffer.write(msg)
                self.buffer_size += len(msg.encode("utf-8"))
                if self.buffer_size >= 1_000_000:
                    self._flush_locked()
        except Exception:
            return

    def _worker(self):
        while True:
            time.sleep(60)
            with self.lock:
                self._flush_locked()

    def _flush_locked(self):
        if self.buffer_size == 0:
            return
        data = self.buffer.getvalue()
        self.buffer = io.StringIO()
        self.buffer_size = 0
        timestamp = time.strftime("%Y%m%d/%H%M%S", time.localtime())
        key = f"{self.prefix}{timestamp}-{self.host}.log"
        try:
            self.bucket.put_object(key, data)
        except Exception:
            return
