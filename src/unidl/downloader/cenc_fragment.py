from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

CTR_SCHEMES = {b"cenc", b"cens"}
CBC_SCHEMES = {b"cbcs", b"cbc1"}
PIFF_SAMPLE_ENCRYPTION_UUID = bytes.fromhex("a2394f525a9b4f14a2446c427c648df4")
_AES_ECB_THREAD_LOCAL = threading.local()


@dataclass(frozen=True, slots=True)
class _SeigGroup:
    kid: str
    iv_size: int
    scheme: bytes = b"cenc"
    constant_iv: bytes | None = None
    crypt_byte_block: int = 0
    skip_byte_block: int = 0


@dataclass(frozen=True, slots=True)
class _TencDefault:
    kid: str
    iv_size: int
    scheme: bytes
    constant_iv: bytes | None = None
    crypt_byte_block: int = 0
    skip_byte_block: int = 0


@dataclass(frozen=True, slots=True)
class _SampleEncryption:
    iv: bytes
    subsamples: tuple[tuple[int, int], ...]


@dataclass(frozen=True, slots=True)
class _TrunInfo:
    sample_sizes: tuple[int, ...]
    data_offset: int | None


@dataclass(frozen=True, slots=True)
class CencInitMetadata:
    schemes: frozenset[bytes]
    default_groups_by_track: dict[int, _SeigGroup]
    sample_entry_encrypted_by_track: dict[int, tuple[bool, ...]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _TfhdInfo:
    track_id: int
    track_id_position: int
    sample_description_index: int
    default_sample_size: int | None


@dataclass(frozen=True, slots=True)
class _FragmentDecryptResult:
    changed: bool = False
    handled_clear: bool = False


class CencFragmentKeyError(RuntimeError):
    def __init__(self, kid: str) -> None:
        self.kid = kid
        super().__init__(f"no matching decryption key for fragment KID {kid}")


def fragment_cenc_key_ids(input_path: str | Path) -> list[str]:
    try:
        data = Path(input_path).read_bytes()
    except OSError:
        return []
    return fragment_cenc_key_ids_from_bytes(data)


def fragment_cenc_key_ids_from_bytes(data: bytes | bytearray) -> list[str]:
    kids: list[str] = []
    for moof_position, moof_size, box_type, moof_header in _mp4_boxes(data):
        if box_type != b"moof":
            continue
        moof_end = moof_position + moof_size
        for traf_position, traf_size, traf_type, traf_header in _mp4_boxes(data, moof_position + moof_header, moof_end):
            if traf_type != b"traf":
                continue
            traf_end = traf_position + traf_size
            for position, size, child_type, header_size in _mp4_boxes(data, traf_position + traf_header, traf_end):
                if child_type != b"sgpd":
                    continue
                for group in _parse_sgpd_seig(data, position, size, header_size):
                    if group.kid and group.kid not in kids:
                        kids.append(group.kid)
    return kids


def decrypt_cenc_fragment(
    input_path: str | Path,
    keys: Iterable[object],
    output_path: str | Path,
    expected_kids: Iterable[str | None] | None = None,
    init_path: str | Path | None = None,
    default_constant_iv: bytes | None = None,
    data_callback: Callable[[bytearray], None] | None = None,
    init_metadata: CencInitMetadata | None = None,
) -> Path | None:
    """Decrypt a fragmented MP4 media fragment that carries CENC info in sgpd/senc.

    Some live DASH/CMAF services keep the init segment's tenc default_KID as all
    zeros and put the actual KID in each fragment's seig sample group. This
    helper returns None when the fragment layout is outside the internal
    decrypter's supported CENC/CBCS surface.
    """

    source = Path(input_path)
    output = Path(output_path)
    data = bytearray(source.read_bytes())
    has_fragment_markers = _has_fragment_cenc_markers(data)
    default_groups_by_track: dict[int, _SeigGroup] = {}
    if init_metadata is None and init_path is not None:
        init = Path(init_path)
        init_metadata = parse_cenc_init_metadata(data if init == source else init.read_bytes(), expected_kids)
    sample_entry_encrypted_by_track: dict[int, tuple[bool, ...]] = {}
    if init_metadata is not None:
        schemes = init_metadata.schemes
        if schemes and not any(scheme in CTR_SCHEMES or scheme in CBC_SCHEMES for scheme in schemes):
            return None
        default_groups_by_track = dict(init_metadata.default_groups_by_track)
        sample_entry_encrypted_by_track = dict(init_metadata.sample_entry_encrypted_by_track)
    if default_constant_iv:
        default_groups_by_track = _with_default_constant_iv(default_groups_by_track, default_constant_iv)
    has_constant_iv_defaults = any(group.constant_iv for group in default_groups_by_track.values())
    if not has_fragment_markers and not (has_constant_iv_defaults and _has_box_type(data, b"moof") and _has_box_type(data, b"mdat")):
        return None

    key_map, fallback_key = _fragment_key_map(keys, expected_kids)
    result = _decrypt_fragment_data(data, key_map, fallback_key, default_groups_by_track, sample_entry_encrypted_by_track)
    if not result.changed and not result.handled_clear:
        return None
    if result.changed:
        _mark_fragment_encryption_boxes_clear(data)
    if data_callback:
        data_callback(data)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(data)
    return output


def parse_cenc_init_metadata(data: bytes | bytearray, expected_kids: Iterable[str | None] | None = None) -> CencInitMetadata:
    return CencInitMetadata(
        schemes=frozenset(_mp4_schemes(data)),
        default_groups_by_track=_default_groups_from_init(data, expected_kids),
        sample_entry_encrypted_by_track=_sample_entry_encryption_states_from_init(data),
    )


def load_cenc_init_metadata(init_path: str | Path, expected_kids: Iterable[str | None] | None = None) -> CencInitMetadata:
    return parse_cenc_init_metadata(Path(init_path).read_bytes(), expected_kids)


def _decrypt_fragment_data(
    data: bytearray,
    key_map: dict[str, bytes],
    fallback_key: bytes | None,
    default_groups_by_track: dict[int, _SeigGroup] | None = None,
    sample_entry_encrypted_by_track: dict[int, tuple[bool, ...]] | None = None,
) -> _FragmentDecryptResult:
    mdat_ranges = _top_level_mdat_payload_ranges(data)
    if not mdat_ranges:
        return _FragmentDecryptResult()
    changed = False
    handled_clear = False
    for moof_position, moof_size, _box_type, moof_header in _mp4_boxes(data):
        if data[moof_position + 4 : moof_position + 8] != b"moof":
            continue
        moof_start = moof_position
        moof_end = moof_position + moof_size
        for traf_position, traf_size, box_type, traf_header in _mp4_boxes(data, moof_position + moof_header, moof_end):
            if box_type != b"traf":
                continue
            result = _decrypt_traf(
                data,
                moof_start,
                traf_position + traf_header,
                traf_position + traf_size,
                mdat_ranges,
                key_map,
                fallback_key,
                default_groups_by_track or {},
                sample_entry_encrypted_by_track or {},
            )
            if result.changed:
                changed = True
            if result.handled_clear:
                handled_clear = True
    return _FragmentDecryptResult(changed=changed, handled_clear=handled_clear)


def _decrypt_traf(
    data: bytearray,
    moof_start: int,
    traf_start: int,
    traf_end: int,
    mdat_ranges: list[tuple[int, int]],
    key_map: dict[str, bytes],
    fallback_key: bytes | None,
    default_groups_by_track: dict[int, _SeigGroup],
    sample_entry_encrypted_by_track: dict[int, tuple[bool, ...]],
) -> _FragmentDecryptResult:
    default_sample_size: int | None = None
    track_id: int | None = None
    track_id_position: int | None = None
    sample_description_index = 1
    groups: list[_SeigGroup] = []
    sbgp_entries: list[tuple[int, int]] = []
    senc_by_iv_size: dict[int, list[_SampleEncryption]] = {}
    aux_by_iv_size: dict[int, list[_SampleEncryption]] = {}
    truns: list[_TrunInfo] = []

    for position, size, box_type, header_size in _mp4_boxes(data, traf_start, traf_end):
        if box_type == b"tfhd":
            tfhd = _parse_tfhd(data, position, size, header_size)
            if tfhd:
                track_id = tfhd.track_id
                track_id_position = tfhd.track_id_position
                sample_description_index = tfhd.sample_description_index
                default_sample_size = tfhd.default_sample_size
        elif box_type == b"sgpd":
            parsed_groups = _parse_sgpd_seig(data, position, size, header_size)
            if parsed_groups:
                groups = parsed_groups
        elif box_type == b"sbgp":
            parsed_sbgp = _parse_sbgp_seig(data, position, size, header_size)
            if parsed_sbgp:
                sbgp_entries = parsed_sbgp
        elif box_type == b"trun":
            trun = _parse_trun(data, position, size, header_size, default_sample_size)
            if trun and trun.sample_sizes:
                truns.append(trun)

    metadata_track_id = _matching_init_track_id(track_id, default_groups_by_track, sample_entry_encrypted_by_track)
    if metadata_track_id is not None and track_id_position is not None and metadata_track_id != track_id:
        data[track_id_position : track_id_position + 4] = metadata_track_id.to_bytes(4, "big")

    if not groups and metadata_track_id is not None:
        group = default_groups_by_track.get(metadata_track_id)
        if group:
            groups = [group]

    if not groups or not truns:
        return _FragmentDecryptResult()

    sample_entry_states = sample_entry_encrypted_by_track.get(metadata_track_id or -1)
    if sample_entry_states and 1 <= sample_description_index <= len(sample_entry_states):
        if not sample_entry_states[sample_description_index - 1]:
            return _FragmentDecryptResult(handled_clear=True)

    sample_count = sum(len(trun.sample_sizes) for trun in truns)
    sample_groups = _expand_sample_groups(groups, sbgp_entries, sample_count)
    if not sample_groups:
        return _FragmentDecryptResult()

    for group in groups:
        senc_by_iv_size.setdefault(group.iv_size, _parse_senc(data, traf_start, traf_end, group.iv_size))

    changed = False
    sample_index = 0
    next_sample_start: int | None = None
    for trun in truns:
        sample_start = moof_start + trun.data_offset if trun.data_offset is not None else next_sample_start
        if sample_start is None:
            sample_start = _first_mdat_payload_start(mdat_ranges)
        for sample_size in trun.sample_sizes:
            if sample_index >= len(sample_groups):
                return _FragmentDecryptResult(changed=changed)
            group = sample_groups[sample_index]
            key = key_map.get(group.kid) or fallback_key
            if key is None:
                raise CencFragmentKeyError(group.kid)
            sample_encryptions = senc_by_iv_size.get(group.iv_size) or []
            if not sample_encryptions:
                sample_encryptions = aux_by_iv_size.setdefault(
                    group.iv_size,
                    _parse_saiz_saio_encryption(data, moof_start, traf_start, traf_end, group.iv_size),
                )
            if sample_encryptions:
                if sample_index >= len(sample_encryptions):
                    return _FragmentDecryptResult(changed=changed)
                enc = sample_encryptions[sample_index]
                if not enc.iv and group.constant_iv is not None:
                    enc = _SampleEncryption(group.constant_iv, enc.subsamples)
            elif group.constant_iv is not None:
                enc = _SampleEncryption(group.constant_iv, ())
            else:
                return _FragmentDecryptResult(changed=changed)
            if _decrypt_sample(data, sample_start, sample_size, enc, key, mdat_ranges, group):
                changed = True
            sample_start += sample_size
            next_sample_start = sample_start
            sample_index += 1
    return _FragmentDecryptResult(changed=changed)


def _decrypt_sample(
    data: bytearray,
    sample_start: int,
    sample_size: int,
    enc: _SampleEncryption,
    key: bytes,
    mdat_ranges: list[tuple[int, int]],
    group: _SeigGroup,
) -> bool:
    if sample_size <= 0 or not _range_inside_any(sample_start, sample_start + sample_size, mdat_ranges):
        return False
    ranges: list[tuple[int, int]] = []
    if enc.subsamples:
        cursor = sample_start
        sample_end = sample_start + sample_size
        for clear_size, encrypted_size in enc.subsamples:
            cursor += clear_size
            encrypted_end = cursor + encrypted_size
            if encrypted_size > 0:
                if encrypted_end > sample_end:
                    return False
                ranges.append((cursor, encrypted_end))
            cursor = encrypted_end
    else:
        ranges.append((sample_start, sample_start + sample_size))
    if not ranges:
        return False
    if len(ranges) == 1:
        start, end = ranges[0]
        encrypted = data[start:end]
        if group.scheme in CBC_SCHEMES:
            clear = _aes_cbc_decrypt_pattern(key, enc.iv, encrypted, group.crypt_byte_block, group.skip_byte_block)
        else:
            clear = _aes_ctr_crypt(key, enc.iv, encrypted)
        data[start:end] = clear
        return True
    if group.scheme in CBC_SCHEMES:
        for start, end in ranges:
            data[start:end] = _aes_cbc_decrypt_pattern(key, enc.iv, data[start:end], group.crypt_byte_block, group.skip_byte_block)
        return True
    else:
        encrypted = b"".join(data[start:end] for start, end in ranges)
        clear = _aes_ctr_crypt(key, enc.iv, encrypted)
        cursor = 0
        for start, end in ranges:
            size = end - start
            data[start:end] = clear[cursor : cursor + size]
            cursor += size
        return True


def _parse_tfhd(data: bytes | bytearray, position: int, size: int, header_size: int) -> _TfhdInfo | None:
    payload = position + header_size
    if payload + 8 > position + size:
        return None
    flags = int.from_bytes(data[payload + 1 : payload + 4], "big")
    cursor = payload + 4
    if cursor + 4 > position + size:
        return None
    track_id_position = cursor
    track_id = int.from_bytes(data[cursor : cursor + 4], "big")
    cursor += 4
    sample_description_index = 1
    if flags & 0x000001:
        cursor += 8
    if flags & 0x000002:
        if cursor + 4 <= position + size:
            sample_description_index = int.from_bytes(data[cursor : cursor + 4], "big")
        cursor += 4
    if flags & 0x000008:
        cursor += 4
    default_sample_size: int | None = None
    if flags & 0x000010:
        if cursor + 4 <= position + size:
            default_sample_size = int.from_bytes(data[cursor : cursor + 4], "big")
    return _TfhdInfo(
        track_id=track_id,
        track_id_position=track_id_position,
        sample_description_index=sample_description_index,
        default_sample_size=default_sample_size,
    )


def _matching_init_track_id(
    fragment_track_id: int | None,
    default_groups_by_track: dict[int, _SeigGroup],
    sample_entry_encrypted_by_track: dict[int, tuple[bool, ...]],
) -> int | None:
    if fragment_track_id is None:
        return None
    init_track_ids = set(default_groups_by_track) | set(sample_entry_encrypted_by_track)
    if fragment_track_id in init_track_ids or len(init_track_ids) != 1:
        return fragment_track_id
    return next(iter(init_track_ids))


def _sample_entry_encryption_states_from_init(data: bytes | bytearray) -> dict[int, tuple[bool, ...]]:
    states: dict[int, tuple[bool, ...]] = {}
    for moov_position, moov_size, moov_type, moov_header in _mp4_boxes(data):
        if moov_type != b"moov":
            continue
        moov_end = moov_position + moov_size
        for trak_position, trak_size, trak_type, trak_header in _mp4_boxes(data, moov_position + moov_header, moov_end):
            if trak_type != b"trak":
                continue
            trak_end = trak_position + trak_size
            track_id = _trak_track_id(data, trak_position + trak_header, trak_end)
            track_states = _trak_sample_entry_encryption_states(data, trak_position + trak_header, trak_end)
            if track_id is not None and track_states is not None:
                states[track_id] = track_states
    return states


def _trak_sample_entry_encryption_states(data: bytes | bytearray, start: int, end: int) -> tuple[bool, ...] | None:
    for position, size, _box_type, header_size in _find_boxes(data, start, end, b"stsd", {b"mdia", b"minf", b"stbl"}):
        payload = position + header_size
        if payload + 8 > position + size:
            return None
        cursor = payload + 4
        entry_count = int.from_bytes(data[cursor : cursor + 4], "big")
        cursor += 4
        states: list[bool] = []
        for _ in range(entry_count):
            if cursor + 8 > position + size:
                return tuple(states)
            entry_size = int.from_bytes(data[cursor : cursor + 4], "big")
            if entry_size < 8 or cursor + entry_size > position + size:
                return tuple(states)
            entry = bytes(data[cursor : cursor + entry_size])
            states.append(_sample_entry_is_encrypted(entry))
            cursor += entry_size
        return tuple(states)
    return None


def _sample_entry_is_encrypted(entry: bytes) -> bool:
    if len(entry) < 8:
        return False
    entry_type = bytes(entry[4:8]).lower()
    if entry_type.startswith(b"enc"):
        return True
    return _has_box_type(entry, b"sinf") or _has_box_type(entry, b"tenc")


def _find_boxes(
    data: bytes | bytearray,
    start: int,
    end: int,
    wanted: bytes,
    containers: set[bytes],
):
    for position, size, box_type, header_size in _mp4_boxes(data, start, end):
        if box_type == wanted:
            yield position, size, box_type, header_size
        elif box_type in containers:
            yield from _find_boxes(data, position + header_size, position + size, wanted, containers)


def _parse_trun(
    data: bytes | bytearray,
    position: int,
    size: int,
    header_size: int,
    default_sample_size: int | None,
) -> _TrunInfo | None:
    payload = position + header_size
    if payload + 8 > position + size:
        return None
    flags = int.from_bytes(data[payload + 1 : payload + 4], "big")
    cursor = payload + 4
    sample_count = int.from_bytes(data[cursor : cursor + 4], "big")
    cursor += 4
    data_offset: int | None = None
    if flags & 0x000001:
        if cursor + 4 > position + size:
            return None
        data_offset = int.from_bytes(data[cursor : cursor + 4], "big", signed=True)
        cursor += 4
    if flags & 0x000004:
        cursor += 4
    sample_sizes: list[int] = []
    for _ in range(sample_count):
        if flags & 0x000100:
            cursor += 4
        if flags & 0x000200:
            if cursor + 4 > position + size:
                return None
            sample_sizes.append(int.from_bytes(data[cursor : cursor + 4], "big"))
            cursor += 4
        elif default_sample_size is not None:
            sample_sizes.append(default_sample_size)
        else:
            return None
        if flags & 0x000400:
            cursor += 4
        if flags & 0x000800:
            cursor += 4
    return _TrunInfo(tuple(sample_sizes), data_offset)


def _parse_sgpd_seig(data: bytes | bytearray, position: int, size: int, header_size: int) -> list[_SeigGroup]:
    payload = position + header_size
    if payload + 12 > position + size:
        return []
    version = data[payload]
    cursor = payload + 4
    if data[cursor : cursor + 4] != b"seig":
        return []
    cursor += 4
    default_length: int | None = None
    if version == 1:
        if cursor + 4 > position + size:
            return []
        default_length = int.from_bytes(data[cursor : cursor + 4], "big")
        cursor += 4
    if version >= 2:
        cursor += 4
    if cursor + 4 > position + size:
        return []
    entry_count = int.from_bytes(data[cursor : cursor + 4], "big")
    cursor += 4
    groups: list[_SeigGroup] = []
    for index in range(entry_count):
        if version == 1 and default_length == 0:
            if cursor + 4 > position + size:
                return groups
            entry_length = int.from_bytes(data[cursor : cursor + 4], "big")
            cursor += 4
        elif default_length is not None:
            entry_length = default_length
        else:
            remaining = position + size - cursor
            entry_length = remaining // max(1, entry_count - index)
        if entry_length < 20 or cursor + entry_length > position + size:
            return groups
        entry = bytes(data[cursor : cursor + entry_length])
        cursor += entry_length
        iv_size = entry[3] & 0x7F
        if iv_size not in {8, 16}:
            continue
        groups.append(_SeigGroup(kid=entry[4:20].hex(), iv_size=iv_size))
    return groups


def _parse_sbgp_seig(data: bytes | bytearray, position: int, size: int, header_size: int) -> list[tuple[int, int]]:
    payload = position + header_size
    if payload + 12 > position + size:
        return []
    version = data[payload]
    cursor = payload + 4
    if data[cursor : cursor + 4] != b"seig":
        return []
    cursor += 4
    if version == 1:
        cursor += 4
    if cursor + 4 > position + size:
        return []
    entry_count = int.from_bytes(data[cursor : cursor + 4], "big")
    cursor += 4
    entries: list[tuple[int, int]] = []
    for _ in range(entry_count):
        if cursor + 8 > position + size:
            return entries
        sample_count = int.from_bytes(data[cursor : cursor + 4], "big")
        group_description_index = int.from_bytes(data[cursor + 4 : cursor + 8], "big")
        entries.append((sample_count, group_description_index))
        cursor += 8
    return entries


def _expand_sample_groups(groups: list[_SeigGroup], sbgp_entries: list[tuple[int, int]], sample_count: int) -> list[_SeigGroup]:
    if sample_count <= 0:
        return []
    if not sbgp_entries:
        return [groups[0]] * sample_count
    expanded: list[_SeigGroup] = []
    for count, index in sbgp_entries:
        group_index = index & 0xFFFF
        if group_index <= 0 or group_index > len(groups):
            group = groups[0]
        else:
            group = groups[group_index - 1]
        expanded.extend([group] * count)
    if len(expanded) < sample_count:
        expanded.extend([groups[0]] * (sample_count - len(expanded)))
    return expanded[:sample_count]


def _parse_senc(data: bytes | bytearray, traf_start: int, traf_end: int, iv_size: int) -> list[_SampleEncryption]:
    for position, size, box_type, header_size in _mp4_boxes(data, traf_start, traf_end):
        if box_type == b"uuid" and _is_piff_sample_encryption_box(data, position, size, header_size):
            payload = position + header_size + len(PIFF_SAMPLE_ENCRYPTION_UUID)
        elif box_type == b"senc":
            payload = position + header_size
        else:
            continue
        if payload + 8 > position + size:
            return []
        flags = int.from_bytes(data[payload + 1 : payload + 4], "big")
        cursor = payload + 4
        sample_count = int.from_bytes(data[cursor : cursor + 4], "big")
        cursor += 4
        samples: list[_SampleEncryption] = []
        for _ in range(sample_count):
            if cursor + iv_size > position + size:
                return samples
            iv = bytes(data[cursor : cursor + iv_size])
            cursor += iv_size
            subsamples: list[tuple[int, int]] = []
            if flags & 0x000002:
                if cursor + 2 > position + size:
                    return samples
                subsample_count = int.from_bytes(data[cursor : cursor + 2], "big")
                cursor += 2
                for _sub in range(subsample_count):
                    if cursor + 6 > position + size:
                        return samples
                    clear_size = int.from_bytes(data[cursor : cursor + 2], "big")
                    encrypted_size = int.from_bytes(data[cursor + 2 : cursor + 6], "big")
                    subsamples.append((clear_size, encrypted_size))
                    cursor += 6
            samples.append(_SampleEncryption(iv, tuple(subsamples)))
        return samples
    return []


def _parse_saiz_saio_encryption(
    data: bytes | bytearray,
    moof_start: int,
    traf_start: int,
    traf_end: int,
    iv_size: int,
) -> list[_SampleEncryption]:
    sizes = _parse_saiz(data, traf_start, traf_end)
    offsets = _parse_saio(data, traf_start, traf_end)
    if not sizes or not offsets:
        return []
    samples: list[_SampleEncryption] = []
    size_index = 0
    for offset in offsets:
        cursor = moof_start + offset
        while size_index < len(sizes):
            info_size = sizes[size_index]
            size_index += 1
            if info_size <= 0:
                return samples
            end = cursor + info_size
            if cursor < 0 or end > len(data) or info_size < iv_size:
                return samples
            iv = bytes(data[cursor : cursor + iv_size])
            cursor += iv_size
            subsamples: list[tuple[int, int]] = []
            remaining = info_size - iv_size
            if remaining:
                if remaining < 2:
                    return samples
                subsample_count = int.from_bytes(data[cursor : cursor + 2], "big")
                cursor += 2
                if remaining < 2 + subsample_count * 6:
                    return samples
                for _ in range(subsample_count):
                    clear_size = int.from_bytes(data[cursor : cursor + 2], "big")
                    encrypted_size = int.from_bytes(data[cursor + 2 : cursor + 6], "big")
                    subsamples.append((clear_size, encrypted_size))
                    cursor += 6
            samples.append(_SampleEncryption(iv, tuple(subsamples)))
            cursor = end
    return samples


def _parse_saiz(data: bytes | bytearray, traf_start: int, traf_end: int) -> list[int]:
    for position, size, box_type, header_size in _mp4_boxes(data, traf_start, traf_end):
        if box_type != b"saiz":
            continue
        payload = position + header_size
        if payload + 9 > position + size:
            return []
        flags = int.from_bytes(data[payload + 1 : payload + 4], "big")
        cursor = payload + 4
        if flags & 0x000001:
            cursor += 8
        if cursor + 5 > position + size:
            return []
        default_size = data[cursor]
        cursor += 1
        sample_count = int.from_bytes(data[cursor : cursor + 4], "big")
        cursor += 4
        if default_size:
            return [default_size] * sample_count
        if cursor + sample_count > position + size:
            return []
        return [int(value) for value in data[cursor : cursor + sample_count]]
    return []


def _parse_saio(data: bytes | bytearray, traf_start: int, traf_end: int) -> list[int]:
    for position, size, box_type, header_size in _mp4_boxes(data, traf_start, traf_end):
        if box_type != b"saio":
            continue
        payload = position + header_size
        if payload + 8 > position + size:
            return []
        version = data[payload]
        flags = int.from_bytes(data[payload + 1 : payload + 4], "big")
        cursor = payload + 4
        if flags & 0x000001:
            cursor += 8
        if cursor + 4 > position + size:
            return []
        entry_count = int.from_bytes(data[cursor : cursor + 4], "big")
        cursor += 4
        field_size = 8 if version == 1 else 4
        offsets: list[int] = []
        for _ in range(entry_count):
            if cursor + field_size > position + size:
                return offsets
            offsets.append(int.from_bytes(data[cursor : cursor + field_size], "big"))
            cursor += field_size
        return offsets
    return []


def _mark_fragment_encryption_boxes_clear(data: bytearray) -> None:
    for position, size, box_type, header_size in _mp4_boxes(data):
        if box_type != b"moof":
            continue
        _mark_fragment_encryption_boxes_clear_in_range(data, position + header_size, position + size)


def _mark_fragment_encryption_boxes_clear_in_range(data: bytearray, start: int, end: int) -> None:
    for position, size, box_type, header_size in _mp4_boxes(data, start, end):
        box_end = position + size
        if box_type in {b"traf", b"moof"}:
            _mark_fragment_encryption_boxes_clear_in_range(data, position + header_size, box_end)
        elif box_type in {b"senc", b"saiz", b"saio", b"sbgp", b"sgpd"} or (
            box_type == b"uuid" and _is_piff_sample_encryption_box(data, position, size, header_size)
        ):
            _free_box(data, position)


def _free_box(data: bytearray, position: int) -> None:
    if position + 8 <= len(data):
        data[position + 4 : position + 8] = b"free"


def _has_fragment_cenc_markers(data: bytes | bytearray) -> bool:
    return (
        _has_box_type(data, b"senc")
        or _has_piff_sample_encryption_box(data)
        or (_has_box_type(data, b"saiz") and _has_box_type(data, b"saio"))
    ) and _has_box_type(data, b"mdat")


def _has_piff_sample_encryption_box(data: bytes | bytearray) -> bool:
    for moof_position, moof_size, moof_type, moof_header in _mp4_boxes(data):
        if moof_type != b"moof":
            continue
        moof_end = moof_position + moof_size
        for traf_position, traf_size, traf_type, traf_header in _mp4_boxes(data, moof_position + moof_header, moof_end):
            if traf_type != b"traf":
                continue
            traf_end = traf_position + traf_size
            for position, size, box_type, header_size in _mp4_boxes(data, traf_position + traf_header, traf_end):
                if box_type == b"uuid" and _is_piff_sample_encryption_box(data, position, size, header_size):
                    return True
    return False


def _is_piff_sample_encryption_box(
    data: bytes | bytearray,
    position: int,
    size: int,
    header_size: int,
) -> bool:
    uuid_start = position + header_size
    uuid_end = uuid_start + len(PIFF_SAMPLE_ENCRYPTION_UUID)
    return uuid_end <= position + size and bytes(data[uuid_start:uuid_end]) == PIFF_SAMPLE_ENCRYPTION_UUID


def _default_groups_from_init(data: bytes | bytearray, expected_kids: Iterable[str | None] | None) -> dict[int, _SeigGroup]:
    expected = [kid for kid in (_normalize_kid(value) for value in expected_kids or []) if kid]
    fallback_kid = expected[0] if len(expected) == 1 else None
    groups: dict[int, _SeigGroup] = {}
    for moov_position, moov_size, moov_type, moov_header in _mp4_boxes(data):
        if moov_type != b"moov":
            continue
        moov_end = moov_position + moov_size
        for trak_position, trak_size, trak_type, trak_header in _mp4_boxes(data, moov_position + moov_header, moov_end):
            if trak_type != b"trak":
                continue
            trak_end = trak_position + trak_size
            track_id = _trak_track_id(data, trak_position + trak_header, trak_end)
            default = _trak_tenc_default(data, trak_position + trak_header, trak_end, fallback_kid)
            if track_id is not None and default is not None:
                groups[track_id] = _group_from_tenc_default(default)
    if groups:
        return groups
    schemes = _mp4_schemes(data)
    fallback_scheme = next(iter(schemes), b"cenc")
    defaults = _all_tenc_defaults(data, fallback_kid, fallback_scheme)
    if len(defaults) == 1:
        return {1: _group_from_tenc_default(defaults[0])}
    return {}


def _with_default_constant_iv(groups: dict[int, _SeigGroup], constant_iv: bytes) -> dict[int, _SeigGroup]:
    if len(constant_iv) not in {8, 16}:
        return groups
    updated: dict[int, _SeigGroup] = {}
    for track_id, group in groups.items():
        if group.constant_iv is None and group.iv_size == 0 and group.scheme in CBC_SCHEMES:
            updated[track_id] = _SeigGroup(
                kid=group.kid,
                iv_size=group.iv_size,
                scheme=group.scheme,
                constant_iv=constant_iv if len(constant_iv) == 16 else constant_iv + b"\x00" * 8,
                crypt_byte_block=group.crypt_byte_block,
                skip_byte_block=group.skip_byte_block,
            )
        else:
            updated[track_id] = group
    return updated


def _trak_track_id(data: bytes | bytearray, start: int, end: int) -> int | None:
    for position, size, box_type, header_size in _mp4_boxes(data, start, end):
        if box_type == b"tkhd":
            payload = position + header_size
            if payload + 8 > position + size:
                return None
            version = data[payload]
            cursor = payload + 4
            cursor += 16 if version == 1 else 8
            if cursor + 4 <= position + size:
                return int.from_bytes(data[cursor : cursor + 4], "big")
            return None
    return None


def _trak_tenc_default(data: bytes | bytearray, start: int, end: int, fallback_kid: str | None) -> _TencDefault | None:
    trak_data = data[start:end]
    schemes = _mp4_schemes(trak_data)
    scheme = next(iter(schemes), b"cenc")
    defaults = _all_tenc_defaults(trak_data, fallback_kid, scheme)
    return defaults[0] if defaults else None


def _group_from_tenc_default(default: _TencDefault) -> _SeigGroup:
    return _SeigGroup(
        kid=default.kid,
        iv_size=default.iv_size,
        scheme=default.scheme,
        constant_iv=default.constant_iv,
        crypt_byte_block=default.crypt_byte_block,
        skip_byte_block=default.skip_byte_block,
    )


def _all_tenc_defaults(data: bytes | bytearray, fallback_kid: str | None, scheme: bytes = b"cenc") -> list[_TencDefault]:
    defaults: list[_TencDefault] = []
    for position, size in _scan_box_ranges(data, b"tenc"):
        parsed = _parse_tenc_default(data[position : position + size], fallback_kid, scheme)
        if parsed:
            defaults.append(parsed)
    return defaults


def _parse_tenc_default(box: bytes | bytearray, fallback_kid: str | None, scheme: bytes = b"cenc") -> _TencDefault | None:
    version = box[8] if len(box) > 8 else 0
    candidates = [(16, 14, 15, 13), (15, 13, 14, 12)] if version == 1 else [(15, 13, 14, None), (16, 14, 15, None)]
    for kid_offset, protected_offset, iv_size_offset, pattern_offset in candidates:
        if len(box) < kid_offset + 16:
            continue
        is_protected = box[protected_offset] != 0
        iv_size = box[iv_size_offset]
        if not is_protected:
            continue
        kid = bytes(box[kid_offset : kid_offset + 16]).hex()
        if kid == "0" * 32 and fallback_kid:
            kid = fallback_kid
        cursor = kid_offset + 16
        constant_iv: bytes | None = None
        if iv_size == 0:
            if cursor < len(box):
                constant_iv_size = box[cursor]
                cursor += 1
                if constant_iv_size not in {8, 16} or cursor + constant_iv_size > len(box):
                    continue
                constant_iv = bytes(box[cursor : cursor + constant_iv_size])
        elif iv_size not in {8, 16}:
            continue
        pattern = box[pattern_offset] if pattern_offset is not None and pattern_offset < len(box) else 0
        return _TencDefault(
            kid=kid,
            iv_size=iv_size,
            scheme=scheme,
            constant_iv=constant_iv,
            crypt_byte_block=(pattern >> 4) & 0x0F,
            skip_byte_block=pattern & 0x0F,
        )
    return None


def _mp4_schemes(data: bytes | bytearray) -> set[bytes]:
    schemes: set[bytes] = set()
    for position, size in _scan_box_ranges(data, b"schm"):
        payload = position + 8
        if payload + 12 <= position + size:
            schemes.add(bytes(data[payload + 4 : payload + 8]))
    return schemes


def _fragment_key_map(keys: Iterable[object], expected_kids: Iterable[str | None] | None = None) -> tuple[dict[str, bytes], bytes | None]:
    key_map: dict[str, bytes] = {}
    raw_fallback_key: bytes | None = None
    for raw_key in keys:
        key_text = str(getattr(raw_key, "key", "")).lower().replace("-", "")
        if len(key_text) != 32:
            continue
        key_bytes = bytes.fromhex(key_text)
        kid = _normalize_kid(getattr(raw_key, "kid", None))
        if kid:
            key_map[kid] = key_bytes
        elif raw_fallback_key is None:
            raw_fallback_key = key_bytes
    return key_map, raw_fallback_key


def _normalize_kid(value: object | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip().lower().replace("-", "")
    if len(text) == 32 and all(char in "0123456789abcdef" for char in text):
        return text
    return None


def _top_level_mdat_payload_ranges(data: bytes | bytearray) -> list[tuple[int, int]]:
    return [(position + header_size, position + size) for position, size, box_type, header_size in _mp4_boxes(data) if box_type == b"mdat"]


def _first_mdat_payload_start(ranges: list[tuple[int, int]]) -> int:
    return ranges[0][0]


def _range_inside_any(start: int, end: int, ranges: list[tuple[int, int]]) -> bool:
    return any(start >= range_start and end <= range_end for range_start, range_end in ranges)


def _mp4_boxes(data: bytes | bytearray, start: int = 0, end: int | None = None):
    end = len(data) if end is None else end
    position = start
    while position + 8 <= end:
        size = int.from_bytes(data[position : position + 4], "big")
        box_type = bytes(data[position + 4 : position + 8])
        header_size = 8
        if size == 1:
            if position + 16 > end:
                break
            size = int.from_bytes(data[position + 8 : position + 16], "big")
            header_size = 16
        elif size == 0:
            size = end - position
        if size < header_size or position + size > end:
            break
        yield position, size, box_type, header_size
        position += size


def _has_box_type(data: bytes | bytearray, box_type: bytes) -> bool:
    return bool(_scan_box_ranges(data, box_type))


def _scan_box_ranges(data: bytes | bytearray, box_type: bytes) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    start = 0
    while True:
        type_at = data.find(box_type, start)
        if type_at < 4:
            return ranges
        box_start = type_at - 4
        size = int.from_bytes(data[box_start:type_at], "big")
        header = 8
        if size == 1 and box_start + 16 <= len(data):
            size = int.from_bytes(data[box_start + 8 : box_start + 16], "big")
            header = 16
        if size >= header and box_start + size <= len(data):
            ranges.append((box_start, size))
            start = box_start + size
        else:
            start = type_at + 4


_SBOX = [
    0x63, 0x7C, 0x77, 0x7B, 0xF2, 0x6B, 0x6F, 0xC5, 0x30, 0x01, 0x67, 0x2B, 0xFE, 0xD7, 0xAB, 0x76,
    0xCA, 0x82, 0xC9, 0x7D, 0xFA, 0x59, 0x47, 0xF0, 0xAD, 0xD4, 0xA2, 0xAF, 0x9C, 0xA4, 0x72, 0xC0,
    0xB7, 0xFD, 0x93, 0x26, 0x36, 0x3F, 0xF7, 0xCC, 0x34, 0xA5, 0xE5, 0xF1, 0x71, 0xD8, 0x31, 0x15,
    0x04, 0xC7, 0x23, 0xC3, 0x18, 0x96, 0x05, 0x9A, 0x07, 0x12, 0x80, 0xE2, 0xEB, 0x27, 0xB2, 0x75,
    0x09, 0x83, 0x2C, 0x1A, 0x1B, 0x6E, 0x5A, 0xA0, 0x52, 0x3B, 0xD6, 0xB3, 0x29, 0xE3, 0x2F, 0x84,
    0x53, 0xD1, 0x00, 0xED, 0x20, 0xFC, 0xB1, 0x5B, 0x6A, 0xCB, 0xBE, 0x39, 0x4A, 0x4C, 0x58, 0xCF,
    0xD0, 0xEF, 0xAA, 0xFB, 0x43, 0x4D, 0x33, 0x85, 0x45, 0xF9, 0x02, 0x7F, 0x50, 0x3C, 0x9F, 0xA8,
    0x51, 0xA3, 0x40, 0x8F, 0x92, 0x9D, 0x38, 0xF5, 0xBC, 0xB6, 0xDA, 0x21, 0x10, 0xFF, 0xF3, 0xD2,
    0xCD, 0x0C, 0x13, 0xEC, 0x5F, 0x97, 0x44, 0x17, 0xC4, 0xA7, 0x7E, 0x3D, 0x64, 0x5D, 0x19, 0x73,
    0x60, 0x81, 0x4F, 0xDC, 0x22, 0x2A, 0x90, 0x88, 0x46, 0xEE, 0xB8, 0x14, 0xDE, 0x5E, 0x0B, 0xDB,
    0xE0, 0x32, 0x3A, 0x0A, 0x49, 0x06, 0x24, 0x5C, 0xC2, 0xD3, 0xAC, 0x62, 0x91, 0x95, 0xE4, 0x79,
    0xE7, 0xC8, 0x37, 0x6D, 0x8D, 0xD5, 0x4E, 0xA9, 0x6C, 0x56, 0xF4, 0xEA, 0x65, 0x7A, 0xAE, 0x08,
    0xBA, 0x78, 0x25, 0x2E, 0x1C, 0xA6, 0xB4, 0xC6, 0xE8, 0xDD, 0x74, 0x1F, 0x4B, 0xBD, 0x8B, 0x8A,
    0x70, 0x3E, 0xB5, 0x66, 0x48, 0x03, 0xF6, 0x0E, 0x61, 0x35, 0x57, 0xB9, 0x86, 0xC1, 0x1D, 0x9E,
    0xE1, 0xF8, 0x98, 0x11, 0x69, 0xD9, 0x8E, 0x94, 0x9B, 0x1E, 0x87, 0xE9, 0xCE, 0x55, 0x28, 0xDF,
    0x8C, 0xA1, 0x89, 0x0D, 0xBF, 0xE6, 0x42, 0x68, 0x41, 0x99, 0x2D, 0x0F, 0xB0, 0x54, 0xBB, 0x16,
]

_RCON = [0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36]
_INV_SBOX: list[int] | None = None


def _aes_ctr_crypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    if not data:
        return b""
    if len(key) != 16:
        raise ValueError("AES-128 key must be 16 bytes.")
    counter = _ctr_initial_counter(iv)
    counter_block = counter.to_bytes(16, "big")
    cryptography_backend = _cryptography_cipher_backend()
    if cryptography_backend is not None:
        Cipher, algorithms, modes = cryptography_backend
        decryptor = Cipher(algorithms.AES(key), modes.CTR(counter_block)).decryptor()
        return decryptor.update(data) + decryptor.finalize()
    crypto_aes = _crypto_aes_module()
    if crypto_aes is not None:
        AES = crypto_aes
        cipher = AES.new(key, AES.MODE_CTR, nonce=b"", initial_value=counter)
        return cipher.decrypt(data)
    expanded = _aes128_expand_key(key)
    output = bytearray()
    for offset in range(0, len(data), 16):
        block = data[offset : offset + 16]
        keystream = _aes128_encrypt_block(counter.to_bytes(16, "big"), expanded)
        output.extend(bytes(value ^ keystream[index] for index, value in enumerate(block)))
        counter = (counter + 1) & ((1 << 128) - 1)
    return bytes(output)


def _aes_cbc_decrypt_pattern(
    key: bytes,
    iv: bytes,
    data: bytes,
    crypt_byte_block: int = 0,
    skip_byte_block: int = 0,
) -> bytes:
    if not data:
        return b""
    if len(key) != 16:
        raise ValueError("AES-128 key must be 16 bytes.")
    if len(iv) != 16:
        raise ValueError(f"Unsupported CBCS IV size: {len(iv)}")
    full_length = (len(data) // 16) * 16
    if full_length <= 0:
        return data
    encrypted = data[:full_length]
    tail = data[full_length:]
    if crypt_byte_block <= 0 or skip_byte_block <= 0:
        return _aes_cbc_decrypt_full_blocks(key, iv, encrypted) + tail

    native_clear = _aes_cbc_decrypt_pattern_native(key, iv, encrypted, crypt_byte_block, skip_byte_block)
    if native_clear is not None:
        return native_clear + tail

    expanded = _aes128_expand_key(key)
    output = bytearray()
    previous_cipher = iv
    offset = 0
    while offset < len(encrypted):
        crypt_length = min(crypt_byte_block * 16, len(encrypted) - offset)
        if crypt_length:
            crypt_piece = encrypted[offset : offset + crypt_length]
            clear_piece, previous_cipher = _aes_cbc_decrypt_blocks_with_chain(crypt_piece, previous_cipher, expanded)
            output.extend(clear_piece)
            offset += crypt_length
        skip_length = min(skip_byte_block * 16, len(encrypted) - offset)
        if skip_length:
            output.extend(encrypted[offset : offset + skip_length])
            offset += skip_length
    return bytes(output) + tail


def _aes_cbc_decrypt_pattern_native(
    key: bytes,
    iv: bytes,
    data: bytes,
    crypt_byte_block: int,
    skip_byte_block: int,
) -> bytes | None:
    decrypt_ecb = _make_aes_ecb_decryptor(key)
    if decrypt_ecb is None:
        return None
    encrypted_chunks: list[bytes] = []
    previous_chunks: list[bytes] = []
    positions: list[tuple[int, int]] = []
    output = bytearray(data)
    previous_cipher = iv
    offset = 0
    while offset < len(data):
        crypt_length = min(crypt_byte_block * 16, len(data) - offset)
        if crypt_length:
            crypt_piece = data[offset : offset + crypt_length]
            encrypted_chunks.append(crypt_piece)
            previous_chunks.append(previous_cipher + crypt_piece[:-16])
            positions.append((offset, crypt_length))
            previous_cipher = crypt_piece[-16:]
            offset += crypt_length
        skip_length = min(skip_byte_block * 16, len(data) - offset)
        if skip_length:
            offset += skip_length
    if not encrypted_chunks:
        return bytes(output)
    decrypted = decrypt_ecb(b"".join(encrypted_chunks))
    clear = _xor_bytes(decrypted, b"".join(previous_chunks))
    cursor = 0
    for start, size in positions:
        output[start : start + size] = clear[cursor : cursor + size]
        cursor += size
    return bytes(output)


def _aes_cbc_decrypt_full_blocks(key: bytes, iv: bytes, data: bytes) -> bytes:
    if not data:
        return b""
    cryptography_backend = _cryptography_cipher_backend()
    if cryptography_backend is not None:
        Cipher, algorithms, modes = cryptography_backend
        decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        return decryptor.update(data) + decryptor.finalize()
    crypto_aes = _crypto_aes_module()
    if crypto_aes is not None:
        AES = crypto_aes
        cipher = AES.new(key, AES.MODE_CBC, iv=iv)
        return cipher.decrypt(data)
    clear, _previous_cipher = _aes_cbc_decrypt_blocks_with_chain(data, iv, _aes128_expand_key(key))
    return clear


def _make_aes_ecb_decryptor(key: bytes):
    cache = getattr(_AES_ECB_THREAD_LOCAL, "decryptors", None)
    if cache is None:
        cache = {}
        _AES_ECB_THREAD_LOCAL.decryptors = cache
    cached = cache.get(key)
    if cached is not None:
        return cached
    crypto_aes = _crypto_aes_module()
    if crypto_aes is not None:
        AES = crypto_aes
        cipher = AES.new(key, AES.MODE_ECB)
        result = cipher.decrypt
    else:
        cryptography_backend = _cryptography_cipher_backend()
        if cryptography_backend is None:
            return None
        Cipher, algorithms, modes = cryptography_backend
        decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
        result = decryptor.update
    cache[key] = result
    return result


def _xor_bytes(left: bytes, right: bytes) -> bytes:
    strxor = _crypto_strxor()
    if strxor is not None:
        return strxor(left, right)
    return bytes(a ^ b for a, b in zip(left, right, strict=False))


@lru_cache(maxsize=1)
def _cryptography_cipher_backend():
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except Exception:
        return None
    return Cipher, algorithms, modes


@lru_cache(maxsize=1)
def _crypto_aes_module():
    try:
        from Crypto.Cipher import AES
    except Exception:
        return None
    return AES


@lru_cache(maxsize=1)
def _crypto_strxor():
    try:
        from Crypto.Util.strxor import strxor
    except Exception:
        return None
    return strxor


def _aes_cbc_decrypt_blocks_with_chain(data: bytes, iv: bytes, expanded_key: list[int]) -> tuple[bytes, bytes]:
    output = bytearray()
    previous = iv
    for offset in range(0, len(data), 16):
        block = data[offset : offset + 16]
        if len(block) < 16:
            output.extend(block)
            break
        decrypted = _aes128_decrypt_block(block, expanded_key)
        output.extend(bytes(value ^ previous[index] for index, value in enumerate(decrypted)))
        previous = block
    return bytes(output), previous


def _ctr_initial_counter(iv: bytes) -> int:
    if len(iv) == 16:
        return int.from_bytes(iv, "big")
    if len(iv) == 8:
        return int.from_bytes(iv + b"\x00" * 8, "big")
    raise ValueError(f"Unsupported CENC IV size: {len(iv)}")


def _aes128_expand_key(key: bytes) -> list[int]:
    expanded = list(key)
    rcon_index = 0
    while len(expanded) < 176:
        temp = expanded[-4:]
        if len(expanded) % 16 == 0:
            temp = temp[1:] + temp[:1]
            temp = [_SBOX[value] for value in temp]
            temp[0] ^= _RCON[rcon_index]
            rcon_index += 1
        for value in temp:
            expanded.append(expanded[-16] ^ value)
    return expanded


def _aes128_encrypt_block(block: bytes, expanded_key: list[int]) -> bytes:
    state = list(block)
    _aes_add_round_key(state, expanded_key, 0)
    for round_index in range(1, 10):
        _aes_sub_bytes(state)
        _aes_shift_rows(state)
        _aes_mix_columns(state)
        _aes_add_round_key(state, expanded_key, round_index)
    _aes_sub_bytes(state)
    _aes_shift_rows(state)
    _aes_add_round_key(state, expanded_key, 10)
    return bytes(state)


def _aes128_decrypt_block(block: bytes, expanded_key: list[int]) -> bytes:
    state = list(block)
    _aes_add_round_key(state, expanded_key, 10)
    for round_index in range(9, 0, -1):
        _aes_inv_shift_rows(state)
        _aes_inv_sub_bytes(state)
        _aes_add_round_key(state, expanded_key, round_index)
        _aes_inv_mix_columns(state)
    _aes_inv_shift_rows(state)
    _aes_inv_sub_bytes(state)
    _aes_add_round_key(state, expanded_key, 0)
    return bytes(state)


def _aes_add_round_key(state: list[int], expanded_key: list[int], round_index: int) -> None:
    offset = round_index * 16
    for index in range(16):
        state[index] ^= expanded_key[offset + index]


def _aes_sub_bytes(state: list[int]) -> None:
    for index, value in enumerate(state):
        state[index] = _SBOX[value]


def _aes_inv_sub_bytes(state: list[int]) -> None:
    inverse = _inverse_sbox()
    for index, value in enumerate(state):
        state[index] = inverse[value]


def _aes_shift_rows(state: list[int]) -> None:
    state[1], state[5], state[9], state[13] = state[5], state[9], state[13], state[1]
    state[2], state[6], state[10], state[14] = state[10], state[14], state[2], state[6]
    state[3], state[7], state[11], state[15] = state[15], state[3], state[7], state[11]


def _aes_inv_shift_rows(state: list[int]) -> None:
    state[1], state[5], state[9], state[13] = state[13], state[1], state[5], state[9]
    state[2], state[6], state[10], state[14] = state[10], state[14], state[2], state[6]
    state[3], state[7], state[11], state[15] = state[7], state[11], state[15], state[3]


def _aes_mix_columns(state: list[int]) -> None:
    for column in range(4):
        offset = column * 4
        a0, a1, a2, a3 = state[offset : offset + 4]
        state[offset] = _xtime(a0) ^ (_xtime(a1) ^ a1) ^ a2 ^ a3
        state[offset + 1] = a0 ^ _xtime(a1) ^ (_xtime(a2) ^ a2) ^ a3
        state[offset + 2] = a0 ^ a1 ^ _xtime(a2) ^ (_xtime(a3) ^ a3)
        state[offset + 3] = (_xtime(a0) ^ a0) ^ a1 ^ a2 ^ _xtime(a3)


def _aes_inv_mix_columns(state: list[int]) -> None:
    for column in range(4):
        offset = column * 4
        a0, a1, a2, a3 = state[offset : offset + 4]
        state[offset] = _gf_mul(a0, 14) ^ _gf_mul(a1, 11) ^ _gf_mul(a2, 13) ^ _gf_mul(a3, 9)
        state[offset + 1] = _gf_mul(a0, 9) ^ _gf_mul(a1, 14) ^ _gf_mul(a2, 11) ^ _gf_mul(a3, 13)
        state[offset + 2] = _gf_mul(a0, 13) ^ _gf_mul(a1, 9) ^ _gf_mul(a2, 14) ^ _gf_mul(a3, 11)
        state[offset + 3] = _gf_mul(a0, 11) ^ _gf_mul(a1, 13) ^ _gf_mul(a2, 9) ^ _gf_mul(a3, 14)


def _xtime(value: int) -> int:
    value <<= 1
    if value & 0x100:
        value ^= 0x11B
    return value & 0xFF


def _gf_mul(value: int, factor: int) -> int:
    result = 0
    current = value
    multiplier = factor
    while multiplier:
        if multiplier & 1:
            result ^= current
        current = _xtime(current)
        multiplier >>= 1
    return result & 0xFF


def _inverse_sbox() -> list[int]:
    global _INV_SBOX
    if _INV_SBOX is not None:
        return _INV_SBOX
    inverse = [0] * 256
    for index, value in enumerate(_SBOX):
        inverse[value] = index
    _INV_SBOX = inverse
    return inverse
