"""Private seat metadata; no scheduling or school side effects."""
import re

from .models import AppError


def validate_preferences(data, catalog, *, previous=None):
    def invalid(message='收藏或分组内容无效，请重新检查'):
        raise AppError('invalid_seat_preferences', message)

    if not isinstance(data, dict) or set(data) != {'favorites', 'groups'}:
        invalid()
    favorites, groups = data['favorites'], data['groups']
    if not isinstance(favorites, list) or len(favorites) > 100:
        invalid('最多保存100个收藏座位')
    if not isinstance(groups, list) or len(groups) > 20:
        invalid('最多保存20个偏好分组')
    # A catalog change must not prevent independent cleanup of saved entries.
    # Unknown references may survive only in the same previously saved entity.
    previous = previous or {'favorites': [], 'groups': []}
    previous_favorites = {item['seat_id'] for item in previous['favorites']}
    previous_groups = {item['id']: set(item['seat_ids']) for item in previous['groups']}
    normalized_favorites, normalized_groups = [], []
    seen_seats, seen_groups, seen_names = set(), set(), set()
    for favorite in favorites:
        if not isinstance(favorite, dict) or set(favorite) != {'seat_id', 'tags'}:
            invalid()
        seat_id, tags = favorite['seat_id'], favorite['tags']
        if (not isinstance(seat_id, str) or seat_id in seen_seats
                or (seat_id not in catalog and seat_id not in previous_favorites)):
            invalid('收藏须选择目录中不同的座位')
        if not isinstance(tags, list) or len(tags) > 5:
            invalid('每个座位最多填写5个自定义标签')
        normalized_tags = []
        for tag in tags:
            if not isinstance(tag, str) or len(tag.strip()) > 20:
                invalid('每个标签最多20个字符')
            tag = tag.strip()
            if tag and tag not in normalized_tags:
                normalized_tags.append(tag)
        seen_seats.add(seat_id)
        normalized_favorites.append({'seat_id': seat_id, 'tags': normalized_tags})
    for group in groups:
        if not isinstance(group, dict) or set(group) != {'id', 'name', 'seat_ids'}:
            invalid()
        key, name, seats = group['id'], group['name'], group['seat_ids']
        if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', key) or key in seen_groups:
            invalid('分组编号无效或重复')
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 40 or name.strip() in seen_names:
            invalid('分组名称须为1–40个字符，且不能重复')
        if (not isinstance(seats, list) or not 1 <= len(seats) <= 5
                or any(not isinstance(seat, str)
                       or (seat not in catalog and seat not in previous_groups.get(key, set())) for seat in seats)
                or len(set(seats)) != len(seats)):
            invalid('每组请选择1–5个不同的目录座位')
        seen_groups.add(key)
        seen_names.add(name.strip())
        normalized_groups.append({'id': key, 'name': name.strip(), 'seat_ids': list(seats)})
    return {'favorites': normalized_favorites, 'groups': normalized_groups}
