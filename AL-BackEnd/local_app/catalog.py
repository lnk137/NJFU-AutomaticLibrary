"""Read the bundled seat directory without writing the legacy database."""
from .models import Seat, AppError


def load_catalog(directory):
    result = {}
    for path in sorted(directory.glob('*.txt')):
        for line in path.read_text(encoding='utf-8-sig').splitlines():
            if not line.strip(): continue
            fields = dict(part.strip().split(':', 1) for part in line.split(','))
            seat = Seat(fields['devId'].strip(), fields['devName'].strip(), path.stem.removesuffix('座位'))
            if seat.id in result and result[seat.id] != seat: raise AppError('catalog_conflict', '座位目录存在冲突')
            result[seat.id] = seat
    if not result: raise AppError('catalog_empty', '随附座位目录为空')
    return result
