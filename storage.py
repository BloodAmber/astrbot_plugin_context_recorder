"""Byte-bounded JSONL storage. No AstrBot dependency."""
import json
import re
import threading
import time
from pathlib import Path


class RollingStore:
    def __init__(self, directory, max_total_bytes, segment_bytes):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.limit = max(1024, int(max_total_bytes))
        self.segment = min(self.limit, max(1024, int(segment_bytes)))
        self.lock = threading.RLock()
        self.current = None
        with self.lock:
            self._prune(0)

    def _files(self):
        return sorted(p for p in self.directory.glob('context-[0-9]*.jsonl')
                      if re.fullmatch(r'context-[0-9]+\.jsonl', p.name)
                      and p.is_file() and not p.is_symlink())

    def _prune(self, incoming):
        files = self._files()
        total = sum(p.stat().st_size for p in files)
        for path in files:
            if total + incoming <= self.limit:
                break
            size = path.stat().st_size
            path.unlink()
            total -= size
            if path == self.current:
                self.current = None

    @staticmethod
    def encode(entry):
        return (json.dumps(entry, ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf-8')

    def write(self, entry):
        data = self.encode(entry)
        if len(data) > self.limit:
            entry = {
                'time': entry.get('time'), 'id': entry.get('id'),
                'api': entry.get('api'), 'model': str(entry.get('model', ''))[:128],
                'payload_omitted': True, 'reason': 'record_exceeds_total_limit',
                'original_record_bytes': len(data), 'limit_bytes': self.limit,
            }
            data = self.encode(entry)
        with self.lock:
            self._prune(len(data))
            if (self.current is None or not self.current.exists()
                    or self.current.stat().st_size + len(data) > self.segment):
                sequence = max(time.time_ns(), max(
                    (int(p.stem.split('-')[1]) + 1 for p in self._files()
                     if p.stem.split('-')[1].isdigit()), default=0))
                self.current = self.directory / f'context-{sequence:020d}.jsonl'
                # Never follow a pre-existing link or overwrite an unrelated file.
                with self.current.open('xb'):
                    pass
            try:
                with self.current.open('ab') as stream:
                    stream.write(data)
                    stream.flush()
            except OSError:
                # Keep a potentially incomplete line isolated from future records.
                self.current = None
                raise

    def status(self):
        with self.lock:
            files = self._files()
            return {'files': len(files), 'bytes': sum(p.stat().st_size for p in files),
                    'limit_bytes': self.limit, 'directory': str(self.directory.resolve())}
