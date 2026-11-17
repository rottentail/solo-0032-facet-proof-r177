"""独立的 8-bit RGB PNG 解码器，用于离线核验，不依赖服务或第三方库。

仅接受非隔行、无透明通道的 RGB PNG，与 facetreader 的契约一致。
"""
import struct
import zlib

from .errors import require

SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_SIDE = 512


def _paeth(left, up, upper_left):
    predictor = left + up - upper_left
    distances = (abs(predictor - left), abs(predictor - up),
                 abs(predictor - upper_left))
    return (left, up, upper_left)[distances.index(min(distances))]


def decode_rgb(raw):
    require(isinstance(raw, (bytes, bytearray)), "png", "PNG 输入必须是字节")
    raw = bytes(raw)
    require(raw.startswith(SIGNATURE), "png_signature", "PNG 签名非法")
    offset = len(SIGNATURE)
    width = height = None
    seen_data = False
    finished = False
    compressed = bytearray()
    chunks = 0
    while offset < len(raw):
        require(offset + 12 <= len(raw), "png_chunks", "PNG 数据块被截断")
        length = struct.unpack_from(">I", raw, offset)[0]
        require(offset + length + 12 <= len(raw), "png_chunks", "PNG 数据块越界")
        kind = raw[offset + 4:offset + 8]
        data = raw[offset + 8:offset + 8 + length]
        crc = struct.unpack_from(">I", raw, offset + 8 + length)[0]
        require((zlib.crc32(kind + data) & 0xffffffff) == crc,
                "png_crc", "PNG 数据块校验和不匹配")
        chunks += 1
        require(chunks <= 4096, "png_chunks", "PNG 数据块过多")
        if kind == b"IHDR":
            require(length == 13 and width is None, "png_header", "IHDR 非法或重复")
            width, height, depth, color, compression, filtering, interlace = \
                struct.unpack(">IIBBBBB", data)
            require(1 <= width <= MAX_SIDE and 1 <= height <= MAX_SIDE,
                    "png_extent", "PNG 尺寸超出限制")
            require((depth, color, compression, filtering, interlace) == (8, 2, 0, 0, 0),
                    "png_format", "仅支持非隔行 8 位 RGB PNG")
        elif kind == b"IDAT":
            require(not finished, "png_chunks", "IDAT 必须连续")
            seen_data = True
            compressed.extend(data)
        elif kind == b"IEND":
            require(seen_data and length == 0, "png_chunks", "IEND 非法")
            finished = True
            offset += 12
            require(offset == len(raw), "png_chunks", "IEND 之后存在多余字节")
            break
        else:
            require(kind[0] & 32, "png_format", "存在不支持的关键数据块")
        offset += length + 12
    require(finished and width and height, "png_chunks", "PNG 缺少 IEND 或 IHDR")
    stride = width * 3
    expected_len = height * (stride + 1)
    try:
        inflater = zlib.decompressobj()
        decoded = inflater.decompress(bytes(compressed), expected_len + 1)
    except zlib.error as exc:
        require(False, "png_deflate", "PNG 压缩流损坏")
        raise exc
    require(len(decoded) == expected_len and inflater.eof
            and not inflater.unconsumed_tail and not inflater.unused_data,
            "png_deflate", "PNG 解码长度或流终止非法")
    pixels = bytearray(height * stride)
    previous = bytearray(stride)
    for y in range(height):
        start = y * (stride + 1)
        filtering = decoded[start]
        require(filtering <= 4, "png_filter", "存在不支持的行过滤器")
        row = bytearray(decoded[start + 1:start + 1 + stride])
        for x in range(stride):
            left = row[x - 3] if x >= 3 else 0
            up = previous[x]
            upper_left = previous[x - 3] if x >= 3 else 0
            if filtering == 1:
                predictor = left
            elif filtering == 2:
                predictor = up
            elif filtering == 3:
                predictor = (left + up) // 2
            elif filtering == 4:
                predictor = _paeth(left, up, upper_left)
            else:
                predictor = 0
            row[x] = (row[x] + predictor) & 255
        pixels[y * stride:(y + 1) * stride] = row
        previous = row
    return width, height, bytes(pixels)
