from __future__ import annotations
import asyncio, base64, hashlib, json, logging, os, secrets, time
from typing import Any, Dict, List, Optional
import aiohttp
from .公共 import ProviderError
from .组B公共 import identify_id, book_id as _book_id, first, cover, metadata, validate_catalog_count, session as _session, retry as _retry, checked
logger = logging.getLogger(__name__)
import binascii, gzip, io, tarfile, threading, zlib
from typing import BinaryIO, Callable, Iterator, Union
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit
from Crypto.Cipher import AES, DES
from Crypto.Hash import MD2, MD4
from Crypto.Util import Counter
import bcrypt as _bcrypt
PLATFORM = {'id': 'qqread', 'name': 'QQ阅读', 'hosts': ['book.qq.com', 'reader.qq.com'], 'aliases': ['QQ阅读', 'qq阅读'], 'coverHosts': ['wfqqreader-1252317822.image.myqcloud.com', 'bookcover.yuewen.com', 'qidian.qpic.cn'], 'credentials': [{'env': 'NOVEL_QQREAD_YWGUID', 'label': 'QQ阅读账号标识', 'showInAdmin': False}, {'env': 'NOVEL_QQREAD_YWKEY', 'label': 'QQ阅读账号密钥', 'showInAdmin': False}, {'env': 'NOVEL_QQREAD_FUID', 'label': 'QQ阅读正文标识', 'required': False, 'defaultAvailable': True, 'showInAdmin': False}, {'env': 'NOVEL_QQREAD_PHONE', 'label': 'QQ阅读手机号', 'required': False, 'showInAdmin': False}, {'env': 'NOVEL_QQREAD_NICKNAME', 'label': 'QQ阅读昵称', 'required': False, 'showInAdmin': False}]}
KNVA_AES_KEY = b'c9ajudte0zb21ksg'
KNVA_AES_IV = b'58jb6v2lzcspwymg'
KNVA_CIPHERTEXT = bytes.fromhex('8f400c5fcec88186569c7c407e35d2895495f9025321cd94976e786a65f18550')
MODE_AES_AES = 21123123
MODE_DES_AES = 21132184
MODE_CTR_DES = 29344484
MODE_AES_CTR = 29859828
MODE_DES_CTR = 31344423
MODE_AES_DES = 31932881
MODE_CTR_CTR = 34232881
MODE_DES_DES = 34941028
MODE_CTR_AES = 94859123

def derive_knva(ciphertext: bytes=KNVA_CIPHERTEXT, key: bytes=KNVA_AES_KEY, iv: bytes=KNVA_AES_IV) -> bytes:
    """Recover knva from libfock embedded AES-128-CBC blob.

    Matches get_master @ runtime 0x1200ca48:
      key = "c9ajudte0zb21ksg", iv = "58jb6v2lzcspwymg", AES-128-CBC.
    """
    if len(ciphertext) % 16:
        raise ValueError('knva ciphertext length must be multiple of 16')
    pt = AES.new(key, AES.MODE_CBC, iv=iv).decrypt(ciphertext)
    return pt[:16]

def _strip_padding(data: bytes, block_size: int) -> bytes:
    """按 Go 版 stripPadding 规则移除一个有效的块填充。"""
    if not data:
        return data
    pad = data[-1]
    if 1 <= pad <= block_size and data.endswith(bytes([pad]) * pad):
        return data[:-pad]
    return data

def _pkcs7_unpad(data: bytes) -> bytes:
    return _strip_padding(data, 16)

def master_key(fuid: str | bytes, knva: bytes | None=None) -> bytes:
    if knva is None:
        knva = derive_knva()
    if isinstance(fuid, str):
        fuid_b = fuid.encode('utf-8')
    else:
        fuid_b = fuid
    if isinstance(knva, str):
        knva = knva.encode('ascii')
    return hashlib.sha256(fuid_b + knva).digest()

def decrypt_keypool(keypool: bytes, aes_key: bytes) -> bytes:
    """解密 Go 版 DecryptChapter 使用的二进制密钥池。"""
    if len(keypool) % 16:
        raise ValueError('keypool length must be multiple of 16')
    return _pkcs7_unpad(AES.new(aes_key, AES.MODE_CBC, iv=aes_key[:16]).decrypt(keypool))

def decrypt_header(enc: bytes, aes_key: bytes) -> bytes:
    if len(enc) < 256:
        raise ValueError('chapter too short')
    return AES.new(aes_key, AES.MODE_CBC, iv=aes_key[:16]).decrypt(enc[:256])

def content_key(pool_decrypted: bytes, param: int, fuid: str | bytes, additional_key: str | bytes) -> bytes:
    """按 Go 版规则从 param 对应的 17 字节密钥槽派生正文密钥。"""
    offset = int(param) * 17
    if offset < 0 or offset + 16 > len(pool_decrypted):
        raise ValueError(f'pool entry index {param} out of range (need offset {offset}+16, pool has {len(pool_decrypted)} bytes)')
    pool_entry = pool_decrypted[offset:offset + 16]
    if isinstance(fuid, str):
        fuid = fuid.encode('utf-8')
    if isinstance(additional_key, str):
        additional_key = additional_key.encode('utf-8')
    return hashlib.sha256(pool_entry + fuid + additional_key).digest()

def _gunzip_loose(data: bytes) -> bytes:
    """匹配 Go maybeGunzip：允许 gzip 头前存在少量前缀。"""
    if len(data) < 2:
        return data
    start = data.find(b'\x1f\x8b')
    if start < 0:
        return data
    try:
        return gzip.decompress(data[start:])
    except (OSError, EOFError, zlib.error):
        return data

def _body(enc: bytes, header_plain: bytes) -> bytes:
    inline_body = header_plain[128:256]
    if not any(inline_body):
        return enc[256:]
    return inline_body + enc[256:]

def _aes_cbc(data: bytes, key32: bytes) -> bytes:
    if len(data) % 16:
        data += b'\x00' * (16 - len(data) % 16)
    return _strip_padding(AES.new(key32, AES.MODE_CBC, iv=key32[:16]).decrypt(data), 16)

def _des_cbc(data: bytes, key32: bytes) -> bytes:
    if len(data) % 8:
        data += b'\x00' * (8 - len(data) % 8)
    return _strip_padding(DES.new(key32[:8], DES.MODE_CBC, iv=key32[:8]).decrypt(data), 8)

def _aes_ctr(data: bytes, key32: bytes, initial_value: int=2) -> bytes:
    ctr = Counter.new(32, prefix=key32[:12], initial_value=initial_value, little_endian=False)
    return AES.new(key32, AES.MODE_CTR, counter=ctr).decrypt(data)

def _parse_header_field(raw: bytes) -> int:
    """解析以 NUL 结尾的 ASCII 十进制字段。"""
    field = raw.split(b'\x00', 1)[0].strip()
    if not field or any((byte < 48 or byte > 57 for byte in field)):
        raise ValueError(f'invalid integer header field: {field!r}')
    return int(field)

def _is_all_zeros(data: bytes) -> bool:
    return not any(data)

def decrypt_mode_aes_aes(enc: bytes, key32: bytes, header_plain: bytes) -> bytes:
    body = _body(enc, header_plain)
    mid = _aes_cbc(_aes_cbc(body, key32), key32)
    return mid

def decrypt_mode_des_aes(enc: bytes, key32: bytes, header_plain: bytes) -> bytes:
    body = _body(enc, header_plain)
    mid = _aes_cbc(_des_cbc(body, key32), key32)
    return mid

def decrypt_mode_ctr_des(enc: bytes, key32: bytes, header_plain: bytes) -> bytes:
    body = _body(enc, header_plain)
    mid = _des_cbc(_aes_ctr(body, key32), key32)
    return mid

def decrypt_mode_aes_ctr(enc: bytes, key32: bytes, header_plain: bytes) -> bytes:
    body = _body(enc, header_plain)
    mid = _aes_ctr(_aes_cbc(body, key32), key32)
    return mid

def decrypt_mode_des_ctr(enc: bytes, key32: bytes, header_plain: bytes) -> bytes:
    body = _body(enc, header_plain)
    mid = _aes_ctr(_des_cbc(body, key32), key32)
    return mid

def decrypt_mode_aes_des(enc: bytes, key32: bytes, header_plain: bytes) -> bytes:
    body = _body(enc, header_plain)
    mid = _des_cbc(_aes_cbc(body, key32), key32)
    return mid

def decrypt_mode_ctr_ctr(enc: bytes, key32: bytes, header_plain: bytes) -> bytes:
    body = _body(enc, header_plain)
    mid = _aes_ctr(_aes_ctr(body, key32), key32)
    return mid

def decrypt_mode_des_des(enc: bytes, key32: bytes, header_plain: bytes) -> bytes:
    body = _body(enc, header_plain)
    mid = _des_cbc(_des_cbc(body, key32), key32)
    return mid

def decrypt_mode_ctr_aes(enc: bytes, key32: bytes, header_plain: bytes) -> bytes:
    body = _body(enc, header_plain)
    mid = _aes_cbc(_aes_ctr(body, key32), key32)
    return mid
_MODE_HANDLERS: dict[int, Callable[[bytes, bytes, bytes], bytes]] = {MODE_AES_AES: decrypt_mode_aes_aes, MODE_DES_AES: decrypt_mode_des_aes, MODE_CTR_DES: decrypt_mode_ctr_des, MODE_AES_CTR: decrypt_mode_aes_ctr, MODE_DES_CTR: decrypt_mode_des_ctr, MODE_AES_DES: decrypt_mode_aes_des, MODE_CTR_CTR: decrypt_mode_ctr_ctr, MODE_DES_DES: decrypt_mode_des_des, MODE_CTR_AES: decrypt_mode_ctr_aes}

def decrypt_chapter(enc: bytes, additional_key: str | bytes, fuid: str | bytes, pool_base64: str, knva: bytes | None=None, *, aes_key: bytes | None=None, pool_decrypted: bytes | None=None) -> bytes:
    """移植 qqread/crypto.go 的 DecryptChapter，返回解压前后的正文字节。"""
    if isinstance(fuid, bytes):
        fuid_text = fuid.decode('utf-8', 'replace')
    else:
        fuid_text = str(fuid)
    if not fuid_text:
        raise ValueError('FUID not set')
    if len(enc) < 256:
        raise ValueError(f'cipher data too small: {len(enc)}')
    if pool_decrypted is None and (not pool_base64):
        raise ValueError('pool_base64 is required')
    if knva is None:
        knva = derive_knva()
    if aes_key is None:
        aes_key = master_key(fuid_text, knva)
    header = decrypt_header(enc, aes_key)
    key1 = header[:128]
    mode = _parse_header_field(key1[:8])
    param = _parse_header_field(key1[8:16])
    content_hash = key1[27:43]
    fuid_hash = key1[43:59]
    if not _is_all_zeros(fuid_hash):
        expected = hashlib.md5(fuid_text.encode('utf-8')).digest()
        if expected != fuid_hash:
            raise ValueError('FUID hash mismatch')
    if pool_decrypted is None:
        try:
            pool_bytes = base64.b64decode(pool_base64, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError('invalid pool base64') from exc
        pool_decrypted = decrypt_keypool(pool_bytes, aes_key)
    key32 = content_key(pool_decrypted, param, fuid_text, additional_key)
    handler = _MODE_HANDLERS.get(mode)
    if handler is None:
        raise NotImplementedError(f'unsupported encryption mode: {mode}')
    step2 = handler(enc, key32, header)
    if not _is_all_zeros(content_hash):
        expected = hashlib.md5(step2).digest()
        if expected != content_hash:
            raise ValueError('content hash mismatch')
    return _gunzip_loose(step2)

def try_decrypt_chapter(enc: bytes, additional_key: str | bytes, fuid: str | bytes, pool_base64: str, knva: bytes | None=None, *, aes_key: bytes | None=None, pool_decrypted: bytes | None=None) -> Optional[bytes]:
    try:
        return decrypt_chapter(enc, additional_key, fuid, pool_base64, knva, aes_key=aes_key, pool_decrypted=pool_decrypted)
    except Exception:
        return None
MASK32 = 4294967295
BCRYPT_ALPHABET = './ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789'
DEC_TABLE = [-1] * 128
P_INIT = [608135816, -2052912941, 320440878, 57701188, -1542899678, 698298832, 137296536, -330404727, 1160258022, 953160567, -1101764913, 887688300, -1062458953, -914599715, 1065670069, -1253635817, -1843997223, -1988494565]
S_INIT = [-785314906, -1730169428, 805139163, -803545161, -1193168915, 1780907670, -1166241723, -248741991, 614570311, -1282315017, 134345442, -2054226922, 1667834072, 1901547113, -1537671517, -191677058, 227898511, 1921955416, 1904987480, -2112533778, 2069144605, -1034266187, -1674521287, 720527379, -976113629, 677414384, -901678824, -1193592593, -1904616272, 1614419982, 1822297739, -1340175810, -686458943, -1120842969, 2024746970, 1432378464, -430627341, -1437226092, 1464375394, 1676153920, 1439316330, 715854006, -1261675468, 289532110, -1588296017, 2087905683, -1276242927, 1668267050, 732546397, 1947742710, -832815594, -1685613794, -1344882125, 1814351708, 2050118529, 680887927, 999245976, 1800124847, -994056165, 1713906067, 1641548236, -81679983, 1216130144, 1575780402, -276538019, -377129551, -601480446, -345695352, 596196993, -745100091, 258830323, -2081144263, 772490370, -1534844924, 1774776394, -1642095778, 566650946, -152474470, 1728879713, -1412200208, 1783734482, -665571480, -1777359064, -1420741725, 1861159788, 326777828, -1170476976, 2130389656, -1578015459, 967770486, 1724537150, -2109534584, -1930525159, 1164943284, 2105845187, 998989502, -529566248, -2050940813, 1075463327, 1455516326, 1322494562, 910128902, 469688178, 1117454909, 936433444, -804646328, -619713837, 1240580251, 122909385, -2137449605, 634681816, -152510729, -469872614, -1233564613, -1754472259, 79693498, -1045868618, 1084186820, 1583128258, 426386531, 1761308591, 1047286709, 322548459, 995290223, 1845252383, -1691314900, -863943356, -1352745719, -1092366332, -567063811, 1712269319, 422464435, -1060394921, 1170764815, -771006663, -1177289765, 1434042557, 442511882, -694091578, 1076654713, 1738483198, -81812532, -1901729288, -617471240, 1014306527, -43947243, 793779912, -1392160085, 842905082, -48003232, 1395751752, 1040244610, -1638115397, -898659168, 445077038, -552113701, -717051658, 679411651, -1402522938, -1940957837, 1767581616, -1144366904, -503340195, -1192226400, 284835224, -48135240, 1258075500, 768725851, -1705778055, -1225243291, -762426948, 1274779536, -505548070, -1530167757, 1660621633, -823867672, -283063590, 913787905, -797008130, 737222580, -1780753843, -1366257256, -357724559, 1804850592, -795946544, -1345903136, -1908647121, -1904896841, -1879645445, -233690268, -2004305902, -1878134756, 1336762016, 1754252060, -774901359, -1280786003, 791618072, -1106372745, -361419266, -1962795103, -442446833, -1250986776, 413987798, -829824359, -1264037920, -49028937, 2093235073, -760370983, 375366246, -2137688315, -1815317740, 555357303, -424861595, 2008414854, -950779147, -73583153, -338841844, 2067696032, -700376109, -1373733303, 2428461, 544322398, 577241275, 1471733935, 610547355, -267798242, 1432588573, 1507829418, 2025931657, -648391809, 545086370, 48609733, -2094660746, 1653985193, 298326376, 1316178497, -1287180854, 2064951626, 458293330, -1705826027, -703637697, -1130641692, 727753846, -2115603456, 146436021, 1461446943, -224990101, 705550613, -1235000031, -407242314, -13368018, -981117340, 1404054877, -1449160799, 146425753, 1854211946, 1266315497, -1246549692, -613086930, -1004984797, -1385257296, 1235738493, -1662099272, -1880247706, -324367247, 1771706367, 1449415276, -1028546847, 422970021, 1963543593, -1604775104, -468174274, 1062508698, 1531092325, 1804592342, -1711849514, -1580033017, -269995787, 1294809318, -265986623, 1289560198, -2072974554, 1669523910, 35572830, 157838143, 1052438473, 1016535060, 1802137761, 1753167236, 1386275462, -1214491899, -1437595849, 1040679964, 2145300060, -1904392980, 1461121720, -1338320329, -263189491, -266592508, 33600511, -1374882534, 1018524850, 629373528, -603381315, -779021319, 2091462646, -1808644237, 586499841, 988145025, 935516892, -927631820, -1695294041, -1455136442, 265290510, -322386114, -1535828415, -499593831, 1005194799, 847297441, 406762289, 1314163512, 1332590856, 1866599683, -167115585, 750260880, 613907577, 1450815602, -1129346641, -560302305, -644675568, -1282691566, -590397650, 1427272223, 778793252, 1343938022, -1618686585, 2052605720, 1946737175, -1130390852, -380928628, -327488454, -612033030, 1661551462, -1000029230, -283371449, 840292616, -582796489, 616741398, 312560963, 711312465, 1351876610, 322626781, 1910503582, 271666773, -2119403562, 1594956187, 70604529, -677132437, 1007753275, 1495573769, -225450259, -1745748998, -1631928532, 504708206, -2031925904, -353800271, -2045878774, 1514023603, 1998579484, 1312622330, 694541497, -1712906993, -2143385130, 1382467621, 776784248, -1676627094, -971698502, -1797068168, -1510196141, 503983604, -218673497, 907881277, 423175695, 432175456, 1378068232, -149744970, -340918674, -356311194, -474200683, -1501837181, -1317062703, 26017576, -1020076561, -1100195163, 1700274565, 1756076034, -288447217, -617638597, 720338349, 1533947780, 354530856, 688349552, -321042571, 1637815568, 332179504, -345916010, 53804574, -1442618417, -1250730864, 1282449977, -711025141, -877994476, -288586052, 1617046695, -1666491221, -1292663698, 1686838959, 431878346, -1608291911, 1700445008, 1080580658, 1009431731, 832498133, -1071531785, -1688990951, -2023776103, -1778935426, 1648197032, -130578278, -1746719369, 300782431, 375919233, 238389289, -941219882, -1763778655, 2019080857, 1475708069, 455242339, -1685863425, 448939670, -843904277, 1395535956, -1881585436, 1841049896, 1491858159, 885456874, -30872223, -293847949, 1565136089, -396052509, 1108368660, 540939232, 1173283510, -1549095958, -613658859, -87339056, -951913406, -278217803, 1699691293, 1103962373, -669091426, -2038084153, -464828566, 1031889488, -815619598, 1535977030, -58162272, -1043876189, 2132092099, 1774941330, 1199868427, 1452454533, 157007616, -1390851939, 342012276, 595725824, 1480756522, 206960106, 497939518, 591360097, 863170706, -1919713727, -698356495, 1814182875, 2094937945, -873565088, 1082520231, -831049106, -1509457788, 435703966, -386934699, 1641649973, -1452693590, -989067582, 1510255612, -2146710820, -1639679442, -1018874748, -36346107, 236887753, -613164077, 274041037, 1734335097, -479771840, -976997275, 1899903192, 1026095262, -244449504, 356393447, -1884275382, -421290197, -612127241, -381855128, -1803468553, -162781668, -1805047500, 1091903735, 1979897079, -1124832466, -727580568, -737663887, 857797738, 1136121015, 1342202287, 507115054, -1759230650, 337727348, -1081374656, 1301675037, -1766485585, 1895095763, 1721773893, -1078195732, 62756741, 2142006736, 835421444, -1762973773, 1442658625, -635090970, -1412822374, 676362277, 1392781812, 170690266, -373920261, 1759253602, -683120384, 1745797284, 664899054, 1329594018, -393761396, -1249058810, 2062866102, -1429332356, -751345684, -830954599, 1080764994, 553557557, -638351943, -298199125, 991055499, 499776247, 1265440854, 648242737, -354183246, 980351604, -581221582, 1749149687, -898096901, -83167922, -654396521, 1161844396, -1169648345, 1431517754, 545492359, -26498633, -795437749, 1437099964, -1592419752, -861329053, -1713251533, -1507177898, 1060185593, 1593081372, -1876348548, -34019326, 69676912, -2135222948, 86519011, -1782508216, -456757982, 1220612927, -955283748, 133810670, 1090789135, 1078426020, 1569222167, 845107691, -711212847, -222510705, 1091646820, 628848692, 1613405280, -537335645, 526609435, 236106946, 48312990, -1352249391, -892239595, 1797494240, 859738849, 992217954, -289490654, -2051890674, -424014439, -562951028, 765654824, -804095931, -1783130883, 1685915746, -405998096, 1414112111, -2021832454, -1013056217, -214004450, 172450625, -1724973196, 980381355, -185008841, -1475158944, -1578377736, -1726226100, -613520627, -964995824, 1835478071, 660984891, -590288892, -248967737, -872349789, -1254551662, 1762651403, 1719377915, -824476260, -1601057013, -652910941, -1156370552, 1364962596, 2073328063, 1983633131, 926494387, -871278215, -2144935273, -198299347, 1749200295, -966120645, 309677260, 2016342300, 1779581495, -1215147545, 111262694, 1274766160, 443224088, 298511866, 1025883608, -488520759, 1145181785, 168956806, -653464466, -710153686, 1689216846, -628709281, -1094719096, 1692713982, -1648590761, -252198778, 1618508792, 1610833997, -771914938, -164094032, 2001055236, -684262196, -2092799181, -266425487, -1333771897, 1006657119, 2006996926, -1108824540, 1430667929, -1084739999, 1314452623, -220332638, -193663176, -2021016126, 1399257539, -927756684, -1267338667, 1190975929, 2062231137, -1960976508, -2073424263, -1856006686, 1181637006, 548689776, -1932175983, -922558900, -1190417183, -1149106736, 296247880, 1970579870, -1216407114, -525738999, 1714227617, -1003338189, -396747006, 166772364, 1251581989, 493813264, 448347421, 195405023, -1584991729, 677966185, -591930749, 1463355134, -1578971493, 1338867538, 1343315457, -1492745222, -1610435132, 233230375, -1694987225, 2000651841, -1017099258, 1638401717, -266896856, -1057650976, 6314154, 819756386, 300326615, 590932579, 1405279636, -1027467724, -1144263082, -1866680610, -335774303, -833020554, 1862657033, 1266418056, 963775037, 2089974820, -2031914401, 1917689273, 448879540, -744572676, -313240200, 150775221, -667058989, 1303187396, 508620638, -1318983944, -1568336679, 1817252668, 1876281319, 1457606340, 908771278, -574175177, -677760460, -1838972398, 1729034894, 1080033504, 976866871, -738527793, -1413318857, 1522871579, 1555064734, 1336096578, -746444992, -1715692610, -720269667, -1089506539, -701686658, -956251013, -1215554709, 564236357, -1301368386, 1781952180, 1464380207, -1131123079, -962365742, 1699332808, 1393555694, 1183702653, -713881059, 1288719814, 691649499, -1447410096, -1399511320, -1101077756, -1577396752, 1781354906, 1676643554, -1702433246, -1064713544, 1126444790, -1524759638, -1661808476, -2084544070, -1679201715, -1880812208, -1167828010, 673620729, -1489356063, 1269405062, -279616791, -953159725, -145557542, 1057255273, 2012875353, -2132498155, -2018474495, -1693849939, 993977747, -376373926, -1640704105, 753973209, 36408145, -1764381638, 25011837, -774947114, 2088578344, 530523599, -1376601957, 1524020338, 1518925132, -534139791, -535190042, 1202760957, -309069157, -388774771, 674977740, -120232407, 2031300136, 2019492241, -311074731, -141160892, -472686964, 352677332, -1997247046, 60907813, 90501309, -1007968747, 1016092578, -1759044884, -1455814870, 457141659, 509813237, -174299397, 652014361, 1966332200, -1319764491, 55981186, -1967506245, 676427537, -1039476232, -1412673177, -861040033, 1307055953, 942726286, 933058658, -1826555503, -361066302, -79791154, 1361170020, 2001714738, -1464409218, -1020707514, 1222529897, 1679025792, -1565652976, -580013532, 1770335741, 151462246, -1281735158, 1682292957, 1483529935, 471910574, 1539241949, 458788160, -858652289, 1807016891, -576558466, 978976581, 1043663428, -1129001515, 1927990952, -94075717, -1922690386, -1086558393, -761535389, 1412390302, -1362987237, -162634896, 1947078029, -413461673, -126740879, -1353482915, 1077988104, 1320477388, 886195818, 18198404, -508558296, -1785185763, 112762804, -831610808, 1866414978, 891333506, 18488651, 661792760, 1628790961, -409780260, -1153795797, 876946877, -1601685023, 1372485963, 791857591, -1608533303, -534984578, -1127755274, -822013501, -1578587449, 445679433, -732971622, -790962485, -720709064, 54117162, -963561881, -1913048708, -525259953, -140617289, 1140177722, -220915201, 668550556, -1080614356, 367459370, 261225585, -1684794075, -85617823, -826893077, -1029151655, 314222801, -1228863650, -486184436, 282218597, -888953790, -521376242, 379116347, 1285071038, 846784868, -1625320142, -523005217, -744475605, -1989021154, 453669953, 1268987020, -977374944, -1015663912, -550133875, -1684459730, -435458233, 266596637, -447948204, 517658769, -832407089, -851542417, 370717030, -47440635, -2070949179, -151313767, -182193321, -1506642397, -1817692879, 1456262402, -1393524382, 1517677493, 1846949527, -1999473716, -560569710, -2118563376, 1280348187, 1908823572, -423180355, 846861322, 1172426758, -1007518822, -911584259, 1655181056, -1155153950, 901632758, 1897031941, -1308360158, -1228157060, -847864789, 1393639104, 373351379, 950779232, 625454576, -1170726756, -146354570, 2007998917, 544563296, -2050228658, -1964470824, 2058025392, 1291430526, 424198748, 50039436, 29584100, -689184263, -1865090967, -1503863136, 1057563949, -1039604065, -1219600078, -831004069, 1469046755, 985887462]
CIHAI_INIT = [1332899944, 1700884034, 1701343084, 1684370003, 1668446532, 1869963892]

def _u32(x: int) -> int:
    return x & MASK32

def _i32(x: int) -> int:
    x &= MASK32
    return x if x < 2147483648 else x - 4294967296

def sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode('utf-8')).hexdigest()

def generate_salt(rounds: int=4, random_bytes: Optional[bytes]=None) -> str:
    if rounds < 4 or rounds > 30:
        raise ValueError('log_rounds exceeds maximum (30)')
    salt_bytes = random_bytes if random_bytes is not None else secrets.token_bytes(16)
    if len(salt_bytes) != 16:
        raise ValueError('salt must be 16 bytes')
    salt_b64 = magic_b64_encode(salt_bytes, len(salt_bytes))
    return f'$2a${rounds:02d}${salt_b64}'

def magic_b64_decode(s: str, max_len: int) -> bytes:
    L = len(s)
    tmp = bytearray(max_len)
    out_len = 0
    i = 0
    while i < L - 1 and out_len < max_len:
        c1 = s[i]
        c2 = s[i + 1]
        if ord(c1) >= 128 or ord(c2) >= 128:
            break
        b1 = DEC_TABLE[ord(c1)]
        b2 = DEC_TABLE[ord(c2)]
        if b1 == -1 or b2 == -1:
            break
        tmp[out_len] = (b1 << 2 | (b2 & 48) >> 4) & 255
        out_len += 1
        if out_len >= max_len or i + 2 >= L:
            break
        c3 = s[i + 2]
        if ord(c3) >= 128:
            break
        b3 = DEC_TABLE[ord(c3)]
        if b3 == -1:
            break
        tmp[out_len] = ((b2 & 15) << 4 | (b3 & 60) >> 2) & 255
        out_len += 1
        if out_len >= max_len or i + 3 >= L:
            break
        c4 = s[i + 3]
        if ord(c4) >= 128:
            break
        b4 = DEC_TABLE[ord(c4)]
        if b4 == -1:
            break
        tmp[out_len] = ((b3 & 3) << 6 | b4) & 255
        out_len += 1
        i += 4
    return bytes(tmp[:out_len])

def magic_b64_encode(b: bytes, length: int) -> str:
    sb = []
    i = 0
    while i < length:
        c1 = b[i] & 255
        sb.append(BCRYPT_ALPHABET[c1 >> 2 & 63])
        c1 = (c1 & 3) << 4
        i += 1
        if i >= length:
            sb.append(BCRYPT_ALPHABET[c1 & 63])
            break
        c2 = b[i] & 255
        sb.append(BCRYPT_ALPHABET[(c1 | c2 >> 4 & 15) & 63])
        c1 = (c2 & 15) << 2
        i += 1
        if i >= length:
            sb.append(BCRYPT_ALPHABET[c1 & 63])
            break
        c3 = b[i] & 255
        sb.append(BCRYPT_ALPHABET[(c1 | c3 >> 6 & 3) & 63])
        sb.append(BCRYPT_ALPHABET[c3 & 63])
        i += 1
    return ''.join(sb)

def stream_to_word(data: bytes, idx_ref: List[int]) -> int:
    if not data:
        return 0
    idx = idx_ref[0]
    w = 0
    for _ in range(4):
        w = (w << 8 | data[idx] & 255) & MASK32
        idx = (idx + 1) % len(data)
    idx_ref[0] = idx
    return w

def blowfish_encrypt_block(P: List[int], S: List[int], l: int, r: int):
    i3 = _u32(l)
    i5 = _u32(r)
    i6 = 0
    i7 = _u32(P[0])
    while True:
        i3 = _u32(i3 ^ i7)
        if i6 > 14:
            l_out = _u32(i5 ^ P[17])
            r_out = i3
            return (l_out, r_out)
        s0 = S[i3 >> 24 & 255]
        s1 = S[i3 >> 16 & 255 | 256]
        s2 = S[i3 >> 8 & 255 | 512]
        s3 = S[i3 & 255 | 768]
        f = _u32((_u32(s0 + s1) ^ s2) + s3)
        i9 = i6 + 1
        i6 = i9 + 1
        i5 = _u32(i5 ^ _u32(f ^ P[i9]))
        s0 = S[i5 >> 24 & 255]
        s1 = S[i5 >> 16 & 255 | 256]
        s2 = S[i5 >> 8 & 255 | 512]
        s3 = S[i5 & 255 | 768]
        f2 = _u32((_u32(s0 + s1) ^ s2) + s3)
        i7 = _u32(P[i6] ^ f2)

def expand_with_key(P: List[int], S: List[int], key_bytes: bytes) -> None:
    idx_ref = [0]
    for i in range(len(P)):
        P[i] = _i32(P[i] ^ stream_to_word(key_bytes, idx_ref))
    block_l = 0
    block_r = 0
    for i in range(0, len(P), 2):
        block_l, block_r = blowfish_encrypt_block(P, S, block_l, block_r)
        P[i] = _i32(block_l)
        P[i + 1] = _i32(block_r)
    for i in range(0, len(S), 2):
        block_l, block_r = blowfish_encrypt_block(P, S, block_l, block_r)
        S[i] = _i32(block_l)
        S[i + 1] = _i32(block_r)

def expand_with_salt_and_key(P: List[int], S: List[int], salt: bytes, key: bytes) -> None:
    idx_key = [0]
    idx_salt = [0]
    for i in range(len(P)):
        P[i] = _i32(P[i] ^ stream_to_word(key, idx_key))
    block_l = 0
    block_r = 0
    for i in range(0, len(P), 2):
        block_l = _u32(block_l ^ stream_to_word(salt, idx_salt))
        block_r = _u32(block_r ^ stream_to_word(salt, idx_salt))
        block_l, block_r = blowfish_encrypt_block(P, S, block_l, block_r)
        P[i] = _i32(block_l)
        P[i + 1] = _i32(block_r)
    for i in range(0, len(S), 2):
        block_l = _u32(block_l ^ stream_to_word(salt, idx_salt))
        block_r = _u32(block_r ^ stream_to_word(salt, idx_salt))
        block_l, block_r = blowfish_encrypt_block(P, S, block_l, block_r)
        S[i] = _i32(block_l)
        S[i + 1] = _i32(block_r)

def magic_search_final(password_bytes: bytes, salt_bytes: bytes, rounds_log2: int, cihai_init: List[int]) -> bytes:
    if rounds_log2 < 4 or rounds_log2 > 30:
        raise ValueError('Bad number of rounds')
    if len(salt_bytes) != 16:
        raise ValueError('Bad salt length')
    P = list(P_INIT)
    S = list(S_INIT)
    i_arr = list(cihai_init)
    expand_with_salt_and_key(P, S, salt_bytes, password_bytes)
    loops = 1 << rounds_log2
    for _ in range(loops):
        expand_with_key(P, S, password_bytes)
        expand_with_key(P, S, salt_bytes)
    half_len = len(i_arr) >> 1
    for _round in range(64):
        for j in range(half_len):
            idx = j * 2
            l = i_arr[idx]
            r = i_arr[idx + 1]
            lr0, lr1 = blowfish_encrypt_block(P, S, l, r)
            i_arr[idx] = _i32(lr0)
            i_arr[idx + 1] = _i32(lr1)
    out = bytearray(len(i_arr) * 4)
    k = 0
    for v in i_arr:
        vv = _u32(v)
        out[k] = vv >> 24 & 255
        out[k + 1] = vv >> 16 & 255
        out[k + 2] = vv >> 8 & 255
        out[k + 3] = vv & 255
        k += 4
    return bytes(out)

def search(password: str, salt_str: Optional[str]=None) -> str:
    if salt_str is None:
        salt_str = generate_salt(4)
    if len(salt_str) < 4 or salt_str[0] != '$' or salt_str[1] != '2':
        raise ValueError('Invalid salt version')
    c_rev = '\x00'
    i2 = 3
    if salt_str[2] != '$':
        c_rev = salt_str[2]
        if c_rev != 'a' or salt_str[3] != '$':
            raise ValueError('Invalid salt revision')
        i2 = 4
    i3 = i2 + 2
    if salt_str[i3] != '$':
        raise ValueError('Missing salt rounds')
    rounds_log2 = int(salt_str[i2:i3])
    if rounds_log2 < 4:
        raise ValueError('Bad number of rounds')
    if rounds_log2 > 30:
        raise ValueError('rounds exceeds maximum (30)')
    salt_b64 = salt_str[i2 + 3:i2 + 25]
    if len(salt_b64) != 22:
        raise ValueError('Bad bcrypt-like salt length')
    pwd_bytes = password.encode('utf-8')
    if c_rev >= 'a':
        pwd_bytes = password.encode('utf-8') + b'\x00'
    salt_bytes = magic_b64_decode(salt_b64, 16)
    if _bcrypt is not None and c_rev == 'a' and (len(password.encode('utf-8')) <= 72) and (len(salt_bytes) == 16):
        try:
            return _bcrypt.hashpw(password.encode('utf-8'), salt_str.encode('ascii')).decode('ascii')
        except (TypeError, ValueError, UnicodeError):
            pass
    out_bytes = magic_search_final(pwd_bytes, salt_bytes, rounds_log2, CIHAI_INIT)
    sb = ['$2']
    if c_rev >= 'a':
        sb.append(c_rev)
    sb.append('$')
    if rounds_log2 < 10:
        sb.append('0')
    if rounds_log2 > 30:
        raise ValueError('rounds exceeds maximum (30)')
    sb.append(str(rounds_log2))
    sb.append('$')
    sb.append(magic_b64_encode(salt_bytes, len(salt_bytes)))
    sb.append(magic_b64_encode(out_bytes, len(CIHAI_INIT) * 4 - 1))
    return ''.join(sb)

def tar_decrypt(stream: Union[BinaryIO, bytes]) -> Dict[str, object]:
    """Port of TarDecompressor.decrypt. Returns map of filename -> bytes, plus code."""
    result: Dict[str, object] = {}
    try:
        if isinstance(stream, (bytes, bytearray)):
            bio = io.BytesIO(stream)
        else:
            data = stream.read()
            bio = io.BytesIO(data)
        with tarfile.open(fileobj=bio, mode='r|*') as tar:
            for member in tar:
                if member.isdir():
                    continue
                f = tar.extractfile(member)
                if f is None:
                    continue
                result[member.name] = f.read()
        result['code'] = 0
        return result
    except Exception as e:
        logger.debug(f'QQ阅读参考 tar 解析失败：错误={type(e).__name__}')
        result['code'] = -1
        return result
QQ阅读默认正文标识 = '89306811035542cd868d49def7d3857d'
CONFIG = {'loginType': '50', 'c_platform': 'android', 'c_version': 'qqreader_8.3.3.0888_android', 'channel': '10005136'} | {'uid': os.environ.get('NOVEL_QQREAD_YWGUID', '').strip(), 'usid': os.environ.get('NOVEL_QQREAD_YWKEY', '').strip(), 'fuid': os.environ.get('NOVEL_QQREAD_FUID', '').strip() or QQ阅读默认正文标识, 'qrsn': secrets.token_hex(8)}
_固定配置已加载 = False
_固定配置加载锁 = threading.Lock()

class ConfigManager:
    _instance: Optional['ConfigManager'] = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        self.login_type = '50'
        self.c_platform = 'android'
        self.c_version = ''
        self.channel = ''
        self.qrsn = ''
        self.usid = ''
        self.uid = ''
        self.fuid = ''
        self.key_pool: Optional[str] = None
        self._knva_cache: bytes | None = None
        self._decryption_cache: tuple[str, str, bytes, bytes, bytes] | None = None
        self._decryption_cache_lock = threading.RLock()

    @classmethod
    def get_instance(cls) -> 'ConfigManager':
        with cls._lock:
            if cls._instance is None:
                cls._instance = ConfigManager()
            return cls._instance

    def _knva_bytes(self) -> bytes:
        with self._decryption_cache_lock:
            if self._knva_cache is None:
                self._knva_cache = derive_knva()
            return self._knva_cache

    def 获取解密材料(self) -> tuple[bytes, bytes, bytes]:
        """按当前 fuid 和密钥池缓存 KNVA、主密钥及已解密密钥池。"""
        with self._decryption_cache_lock:
            fuid = str(self.fuid or '')
            pool_b64 = str(self.key_pool or '')
            if not fuid or not pool_b64:
                raise ValueError('QQ阅读解密材料不完整')
            cached = self._decryption_cache
            if cached is not None and cached[0] == fuid and (cached[1] == pool_b64):
                return (cached[2], cached[3], cached[4])
            knva = self._knva_bytes()
            aes_key = master_key(fuid, knva)
            try:
                pool_bytes = base64.b64decode(pool_b64, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise ValueError('invalid pool base64') from exc
            pool_decrypted = decrypt_keypool(pool_bytes, aes_key)
            if len(pool_decrypted) < 16:
                raise ValueError('QQ阅读解密密钥池为空')
            self._decryption_cache = (fuid, pool_b64, knva, aes_key, pool_decrypted)
            return (knva, aes_key, pool_decrypted)

    def _cache_valid(self, pool_b64: str) -> bool:
        if not pool_b64 or not self.fuid:
            return False
        try:
            fuid = str(self.fuid)
            with self._decryption_cache_lock:
                cached = self._decryption_cache
                if cached is not None and cached[0] == fuid and (cached[1] == str(pool_b64)):
                    return len(cached[4]) >= 16
            raw = base64.b64decode(pool_b64, validate=True)
            pool = decrypt_keypool(raw, master_key(fuid, self._knva_bytes()))
            return len(pool) >= 16
        except Exception:
            return False

    def _load_key_pool_cache(self) -> Optional[str]:
        return self.key_pool

    def _save_key_pool_cache(self, pool_b64: str) -> None:
        with self._decryption_cache_lock:
            self.key_pool = pool_b64
            if not (self._decryption_cache is not None and self._decryption_cache[0] == str(self.fuid or '') and (self._decryption_cache[1] == str(pool_b64 or ''))):
                self._decryption_cache = None

    def apply(self, m: Dict[str, Any]) -> None:
        if 'loginType' in m:
            self.login_type = str(m['loginType'])
        if 'c_platform' in m:
            self.c_platform = str(m['c_platform'])
        if 'c_version' in m:
            self.c_version = str(m['c_version'])
        if 'channel' in m:
            self.channel = str(m['channel'])
        if 'qrsn' in m:
            self.qrsn = str(m['qrsn'])
        if 'usid' in m:
            self.usid = str(m['usid'])
        if 'uid' in m:
            self.uid = str(m['uid'])
        if 'fuid' in m:
            new_fuid = str(m['fuid'])
            if new_fuid != self.fuid:
                with self._decryption_cache_lock:
                    self.fuid = new_fuid
                    self._decryption_cache = None
            else:
                self.fuid = new_fuid

def load_config_once() -> None:
    global _固定配置已加载
    if _固定配置已加载:
        return
    with _固定配置加载锁:
        if _固定配置已加载:
            return
        ConfigManager.get_instance().apply(CONFIG)
        _固定配置已加载 = True
UA = 'okhttp/3.12.13'
SIGN_TAIL = 'B74H5a2Yh73gfu8F'
QQ阅读批量最大动态并发数 = 100
_QQ阅读密钥池异步锁: asyncio.Lock | None = None

def 创建QQ阅读HTTP会话(*, concurrency: int=QQ阅读批量最大动态并发数) -> aiohttp.ClientSession:
    """创建下载期间复用的异步连接池。"""
    limit = max(1, int(concurrency or 1))
    connector = aiohttp.TCPConnector(limit=limit, limit_per_host=limit, ttl_dns_cache=300, keepalive_timeout=30)
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=90)
    return aiohttp.ClientSession(headers={'User-Agent': UA}, timeout=timeout, connector=connector)

async def _异步QQ阅读CPU函数(函数, *参数):
    return await asyncio.to_thread(函数, *参数)

async def 异步构造QQ阅读鉴权请求头(timestamp_ms: int, request_url: str | None=None) -> Dict[str, str]:
    return await asyncio.to_thread(构造QQ阅读鉴权请求头, timestamp_ms, request_url)

def _获取QQ阅读密钥池异步锁() -> asyncio.Lock:
    global _QQ阅读密钥池异步锁
    if _QQ阅读密钥池异步锁 is None:
        _QQ阅读密钥池异步锁 = asyncio.Lock()
    return _QQ阅读密钥池异步锁

async def 确保QQ阅读密钥池(session: aiohttp.ClientSession, *, force: bool=False) -> bool:
    """异步刷新正文解密所需密钥池，避免在解密线程中发起网络请求。"""
    load_config_once()
    config = ConfigManager.get_instance()
    if not config.fuid:
        return False
    if not force and config.key_pool and config._cache_valid(config.key_pool):
        return True
    async with _获取QQ阅读密钥池异步锁():
        if not force and config.key_pool and config._cache_valid(config.key_pool):
            return True
        try:
            params = {'fuid': config.fuid, 'type': '1'}
            request_url = _构造QQ阅读请求地址('https://newminerva-tgw.reader.qq.com/sk', params)
            async with session.get('https://newminerva-tgw.reader.qq.com/sk', params=params, headers=await 异步构造QQ阅读鉴权请求头(int(time.time() * 1000), request_url)) as response:
                response.raise_for_status()
                data = await response.json(content_type=None)
        except Exception as exc:
            logger.debug(f'QQ阅读密钥池获取失败：错误={type(exc).__name__}')
            return False
        pool = str((data or {}).get('pool') or '').strip() if isinstance(data, dict) else ''
        if not pool or not config._cache_valid(pool):
            logger.debug('QQ阅读密钥池响应无效')
            return False
        config._save_key_pool_cache(pool)
        return True
QQ阅读网关签名版本 = '1'
QQ阅读网关设备标识 = '0'
QQ阅读可信标识盐 = ').#@!U_*#@DxL09V'
QQ阅读网关MD5密钥编码 = bytes((191, 184, 181, 214, 183, 195, 201, 188, 181, 214, 210, 238, 218, 166, 175, 192))

def _QQ阅读网关MD5密钥() -> str:
    return bytes((value ^ 150 for value in QQ阅读网关MD5密钥编码)).decode('ascii')

def _QQ阅读网关摘要(data: bytes, index: int) -> bytes:
    if index == 0:
        return MD2.new(data).digest()
    if index == 1:
        return MD4.new(data).digest()
    return hashlib.md5(data).digest()

def 计算QQ阅读SSign(规范参数: str) -> str:
    """按 QQ 阅读网关规则计算随请求变化的 ssign。"""
    payload = 规范参数.encode('utf-8')
    checksum = zlib.crc32(payload) & 4294967295
    first = _QQ阅读网关摘要(payload, checksum % 3)
    index = (checksum >> 8) % 3
    return _QQ阅读网关摘要(_QQ阅读网关摘要(first, index), index).hex()

def _构造QQ阅读网关规范参数(request_url: str, headers: dict[str, str]) -> str:
    items: dict[str, str] = {}
    try:
        pairs = parse_qsl(urlsplit(request_url).query, keep_blank_values=True)
    except ValueError:
        pairs = []
    for key, value in pairs:
        items.setdefault(str(key), str(value))
    items['qrsn'] = str(headers.get('qrsn') or '')
    items['c_version'] = str(headers.get('c_version') or '')
    items['ttime'] = str(headers.get('ttime') or '')
    return '&'.join((f'{key}={items[key]}' for key in sorted(items)))

def _构造QQ阅读请求地址(url: str, params: dict[str, Any]) -> str:
    query = urlencode(params, doseq=True)
    if not query:
        return url
    return f"{url}{('&' if '?' in url else '?')}{query}"

def _生成QQ阅读YWToken(value: str) -> str:
    raw = str(value or '').encode('utf-8')
    padding = 8 - len(raw) % 8
    padded = raw + bytes([padding]) * padding
    return DES.new(b'1R8SH560', DES.MODE_ECB).encrypt(padded).hex().upper()

def _稳定QQ阅读随机标识(seed: str, length: int) -> str:
    value = str(seed or '').strip() or secrets.token_hex(16)
    result = hashlib.sha256(value.encode('utf-8')).hexdigest()
    while len(result) < length:
        result += hashlib.sha256((result + value).encode('utf-8')).hexdigest()
    return result[:length]

def _补充QQ阅读网关签名(headers: dict[str, str], request_url: str) -> dict[str, str]:
    """为 /sk、目录和正文网关请求补齐动态签名字段。"""
    signed = dict(headers)
    timestamp_ms = str(signed.get('ttime') or int(time.time() * 1000))
    signed['ttime'] = timestamp_ms
    signed['qrtm'] = str(int(timestamp_ms) // 1000)
    config = ConfigManager.get_instance()
    if not signed.get('qrsn'):
        signed['qrsn'] = _稳定QQ阅读随机标识('|'.join((config.channel, config.c_version, config.login_type, config.uid, config.usid)), 16)
    if not signed.get('qrsn_new'):
        signed['qrsn_new'] = _稳定QQ阅读随机标识('|'.join((signed['qrsn'], config.c_platform, config.uid, config.usid)), 36)
    try:
        fuid = next(iter(parse_qs(urlsplit(request_url).query).get('fuid', [])), '')
    except ValueError:
        fuid = ''
    signed['logid'] = f'{fuid}_{timestamp_ms}' if fuid else f'{secrets.token_hex(16)}_{timestamp_ms}'
    login_uin = str(signed.get('uid') or '').strip()
    channel = str(signed.get('channel') or '').strip()
    c_version = str(signed.get('c_version') or '').strip()
    qrtm = signed['qrtm']
    if login_uin and channel and c_version:
        safe_source = '|'.join((c_version, channel, login_uin, _QQ阅读网关MD5密钥(), qrtm, QQ阅读网关设备标识))
        signed['safekey'] = hashlib.md5(safe_source.encode('utf-8')).hexdigest().upper()
    qrsn = str(signed.get('qrsn') or '').strip()
    if login_uin and channel and c_version and qrsn:
        existing_trustedid = str(signed.get('trustedid') or '').strip()
        suffix = existing_trustedid[-1:] if len(existing_trustedid) >= 33 else '1'
        trusted_source = '|'.join((login_uin, qrsn, QQ阅读网关设备标识, c_version, channel, qrtm, QQ阅读可信标识盐, ''))
        signed['trustedid'] = hashlib.md5(trusted_source.encode('utf-8')).hexdigest().upper() + suffix
    login_type = str(signed.get('loginType') or '')
    login_key = str(signed.get('usid') or '')
    if login_type not in {'50', '52'}:
        login_key = str(signed.get('ywkey') or login_key)
    if login_key:
        signed['ywtoken'] = _生成QQ阅读YWToken(login_key)
    signed['ssign'] = 计算QQ阅读SSign(_构造QQ阅读网关规范参数(request_url, signed))
    signed['ssign_version'] = QQ阅读网关签名版本
    return signed

def 构造QQ阅读鉴权请求头(timestamp_ms: int, request_url: str | None=None) -> Dict[str, str]:
    config = ConfigManager.get_instance()
    pwd = f'{config.login_type}|||{config.c_version}|{config.c_platform}|{config.channel}|{config.qrsn}|{config.qrsn}||||0|{timestamp_ms}|{SIGN_TAIL}'
    headers = {'User-Agent': UA, 'loginType': config.login_type, 'c_platform': config.c_platform, 'c_version': config.c_version, 'channel': config.channel, 'qrsn': config.qrsn, 'usid': config.usid, 'uid': config.uid, 'qqnum': config.uid, 'youngerMode': '0', 'qrsn_new': config.qrsn, 'ttime': str(timestamp_ms), 'csigs': search(sha256_hex(pwd), generate_salt())}
    if request_url:
        return _补充QQ阅读网关签名(headers, request_url)
    return headers

def _提取QQ阅读章节号(value: str) -> int:
    first = value.find('_')
    second = value.find('_', first + 1)
    if first < 0 or second < 0:
        raise ValueError('章节文件名无效')
    return int(value[first + 1:second])

def 构造QQ阅读正文章节参数(chapter_ids: list[str]) -> str:
    normalized = [str(chapter_id).strip() for chapter_id in chapter_ids if str(chapter_id).strip()]
    if not normalized:
        return ''
    numbers = [int(chapter_id) for chapter_id in normalized if chapter_id.isdigit()]
    if len(numbers) == len(normalized) and all((current == numbers[0] + offset for offset, current in enumerate(numbers))):
        return f'{numbers[0]}-{numbers[-1]}'
    return ','.join(normalized)

def 解密QQ阅读章节数据(data: bytes, stt: str | bytes, *, allow_refresh: bool=True, 解密材料: tuple[bytes, bytes, bytes] | None=None) -> Optional[str]:
    config = ConfigManager.get_instance()
    if not config.key_pool:
        return None
    try:
        if 解密材料 is None:
            解密材料 = config.获取解密材料()
        knva, aes_key, pool_decrypted = 解密材料
        text = try_decrypt_chapter(data, stt, config.fuid, config.key_pool, knva, aes_key=aes_key, pool_decrypted=pool_decrypted)
    except Exception:
        text = None
    if text is None and allow_refresh:
        logger.debug('QQ阅读章节解密未命中当前密钥池')
    return text.decode('utf-8', 'replace') if text else None

def _展开QQ阅读正文信息(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, list):
        for item in value:
            yield from _展开QQ阅读正文信息(item)
        return
    if not isinstance(value, dict):
        return
    if any((key in value for key in ('chapter_id', 'chapterId', 'cid', 'scid'))):
        yield value
    for key in ('items', 'data', 'list', 'chapters'):
        nested = value.get(key)
        if isinstance(nested, (dict, list)):
            yield from _展开QQ阅读正文信息(nested)

def _解析QQ阅读正文信息(members: dict[str, object]) -> dict[str, str]:
    """从正文包 info 文件建立真实章节 ID 与 UUID 的映射。"""
    mapping: dict[str, str] = {}
    for name, raw in members.items():
        normalized_name = str(name).replace('\\', '/').lower()
        if not normalized_name.endswith(('info.txt', 'info.json')):
            continue
        if not isinstance(raw, (bytes, bytearray)):
            continue
        try:
            payload = json.loads(bytes(raw).decode('utf-8-sig', 'replace'))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        for item in _展开QQ阅读正文信息(payload):
            chapter_id = str(item.get('chapter_id') or item.get('chapterId') or item.get('cid') or item.get('scid') or '').strip()
            if not chapter_id:
                continue
            for key in (chapter_id, item.get('chapter_uuid'), item.get('chapterUuid'), item.get('uuid')):
                normalized_key = str(key or '').strip()
                if normalized_key:
                    mapping[normalized_key] = chapter_id
    return mapping

def _QQ阅读章节文件候选键(name: str) -> list[str]:
    normalized = str(name).replace('\\', '/').rsplit('/', 1)[-1]
    values = [normalized]
    if normalized.endswith('_s'):
        stem = normalized[:-2]
        values.append(stem)
        if '_' in stem:
            values.append(stem.rsplit('_', 1)[-1])
    return values

def _匹配QQ阅读正文章节文件(members: dict[str, object], requested_ids: list[str]) -> dict[str, tuple[str, object]]:
    requested_set = set(requested_ids)
    info_mapping = _解析QQ阅读正文信息(members)
    matched: dict[str, tuple[str, object]] = {}
    for name, value in members.items():
        normalized_name = str(name).replace('\\', '/').lower()
        if normalized_name.endswith(('info.txt', 'info.json')) or name == 'code':
            continue
        chapter_id = ''
        候选键 = _QQ阅读章节文件候选键(str(name))
        for candidate in 候选键:
            if candidate.isdigit() and candidate in requested_set:
                chapter_id = candidate
                break
        if chapter_id:
            if chapter_id not in matched:
                matched[chapter_id] = (str(name), value)
            continue
        for candidate in 候选键:
            target = info_mapping.get(candidate) or (candidate if candidate in requested_set else '')
            if target in requested_set:
                chapter_id = target
                break
        if not chapter_id:
            try:
                legacy_id = str(_提取QQ阅读章节号(str(name)))
            except (TypeError, ValueError):
                legacy_id = ''
            if legacy_id in requested_set:
                chapter_id = legacy_id
        if chapter_id and chapter_id not in matched:
            matched[chapter_id] = (str(name), value)
    return matched

def 解析QQ阅读正文批次带统计(package: bytes, chapter_ids: list[str], 解密材料: tuple[bytes, bytes, bytes] | None=None) -> tuple[list[Any], int, int]:
    """解析正文包，并返回实际匹配数和解密失败数。"""
    members = tar_decrypt(package)
    requested_ids = [str(chapter_id).strip() for chapter_id in chapter_ids if str(chapter_id).strip()]
    if 解密材料 is None:
        try:
            解密材料 = ConfigManager.get_instance().获取解密材料()
        except Exception:
            解密材料 = None
    chapter_map: Dict[str, Any] = {}
    for chapter_id, (name, value) in _匹配QQ阅读正文章节文件(members, requested_ids).items():
        if isinstance(value, (bytes, bytearray)):
            try:
                text = 解密QQ阅读章节数据(bytes(value), name, allow_refresh=False, 解密材料=解密材料)
            except Exception as exc:
                logger.debug(f'QQ阅读参考正文解密失败：错误={type(exc).__name__}')
                text = None
            value = text if text else '章节解密失败'
        elif not isinstance(value, str):
            value = str(value)
        current = chapter_map.get(chapter_id)
        if current is None or current == '章节解密失败':
            chapter_map[chapter_id] = value
    result = [chapter_map.get(chapter_id, '章节解密失败') for chapter_id in requested_ids]
    decrypt_failed = sum((1 for value in chapter_map.values() if value == '章节解密失败'))
    return (result, len(chapter_map), decrypt_failed)
QQ阅读详情地址 = 'https://commontgw.reader.qq.com/book/queryBookInfo'
QQ阅读目录地址 = 'https://newminerva-tgw.reader.qq.com/ChapBatAuthWithPD'

def _遍历详情对象(data: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    stack = [data]
    seen: set[int] = set()
    while stack:
        item = stack.pop(0)
        if isinstance(item, dict):
            marker = id(item)
            if marker in seen:
                continue
            seen.add(marker)
            result.append(item)
            stack.extend((value for value in item.values() if isinstance(value, (dict, list))))
        elif isinstance(item, list):
            stack.extend(item)
    return result

def _读取详情字段(objects: list[dict[str, Any]], *names: str, default: Any='') -> Any:
    for item in objects:
        for name in names:
            value = item.get(name)
            if value not in (None, ''):
                return value
    return default

def _安全整数(value: Any, default: int=0) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default

def _真值(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value or '').strip().lower() in {'1', 'true', 'yes', 'on'}

def _是真值(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value > 0
    return str(value or '').strip().lower() in {'1', 'true', 'yes', 'on'}

def _详情支持VIP免费(objects: list[dict[str, Any]]) -> bool:
    vip_free = _读取详情字段(objects, 'vipFree', 'vip_free', 'isVipFree', 'is_vip_free', default=False)
    if _是真值(vip_free):
        return True
    message = str(_读取详情字段(objects, 'vipFreeMsg', 'vip_free_msg', 'vipTips', 'vip_tips', 'vipdisc', 'vipDisc', default='') or '').strip()
    lowered = message.lower()
    has_vip_marker = 'vip' in lowered or '会员' in message or '包月' in message
    has_free_marker = '免费' in message or '专享' in message or '开通' in message
    if has_vip_marker and has_free_marker:
        return True
    need_open_vip = _读取详情字段(objects, 'needOpenVip', 'need_open_vip', default=False)
    return _是真值(need_open_vip) and has_vip_marker

def _规范状态(value: Any) -> str:
    text = str(value or '').strip()
    lowered = text.lower()
    if text in {'完结', '完本'} or '完结' in text or lowered in {'1', 'true', 'yes'}:
        return '完结'
    if text in {'连载'} or '连载' in text or lowered in {'0', 'false', 'no'}:
        return '连载'
    return '连载'

def 解析参考书籍详情(data: Any, book_id: str) -> dict[str, Any]:
    if isinstance(data, dict) and 'retCode' in data and (_安全整数(data.get('retCode'), 0) != 0):
        raise RuntimeError('书籍详情不可用')
    objects = _遍历详情对象(data)
    if not _读取详情字段(objects, 'title', 'bookName', 'book_name'):
        raise ProviderError('book_info_unavailable')
    status = _规范状态(_读取详情字段(objects, 'isfinished', 'isFinished', 'finished', 'finishstate', 'status'))
    chapters = _读取详情字段(objects, 'totalChapters', 'chapterNum', 'chapters', 'totalChapter', 'chapter_count', default=0)
    max_free_chapter = _读取详情字段(objects, 'maxfreechapter', 'maxFreeChapter', 'max_free_chapter', default=0)
    vip_state = _读取详情字段(objects, 'isVip', 'is_vip', 'vipStatus', 'vip_status', default=False)
    free_value = _读取详情字段(objects, 'free', default=None)
    free = None
    if free_value not in (None, ''):
        parsed_free = _安全整数(free_value, default=-1)
        if parsed_free >= 0:
            free = parsed_free
    return {'title': str(_读取详情字段(objects, 'title', 'bookName', 'book_name') or '').strip(), 'author': str(_读取详情字段(objects, 'author', 'authorName', 'author_name', default='未知') or '未知').strip(), 'status': status, 'words_num': str(_读取详情字段(objects, 'wordscount', 'wordCount', 'words', 'allwords', 'totalWords', 'word_count', default='') or '').strip(), 'chapters': _安全整数(chapters), 'total_chapters': _安全整数(chapters), 'coverUrl': next((cover(item) for item in objects if cover(item)), ''), 'max_free_chapter': _安全整数(max_free_chapter), 'is_vip': _真值(vip_state), 'free': free, 'vip_free': _详情支持VIP免费(objects), 'intro': str(_读取详情字段(objects, 'intro', 'desc', 'summary', 'description', default='') or '').strip()}

async def 获取参考书籍详情(book_id: str, session: aiohttp.ClientSession | None=None) -> dict[str, Any]:
    if session is None:
        async with 创建QQ阅读HTTP会话(concurrency=2) as local_session:
            return await 获取参考书籍详情(book_id, local_session)
    await 确保QQ阅读密钥池(session)
    async with session.get(QQ阅读详情地址, params={'bid': book_id, 'types': '1,2,3,4,5'}, headers=await 异步构造QQ阅读鉴权请求头(int(time.time() * 1000))) as response:
        response.raise_for_status()
        data = await response.json(content_type=None)
    return 解析参考书籍详情(data, book_id)

def 解析参考目录包(package: bytes, book_id: str) -> list[dict[str, Any]]:
    members = tar_decrypt(package)
    candidates: list[tuple[str, bytes]] = []
    for name, data in members.items():
        if name == 'code' or not isinstance(data, (bytes, bytearray)):
            continue
        candidates.append((str(name), bytes(data)))
    if not candidates:
        return []
    candidates.sort(key=lambda item: (item[0] == f'{book_id}_ALL_s', item[0].endswith('_ALL_s'), len(item[1])), reverse=True)
    text = candidates[0][1].decode('utf-8', 'replace')
    rows: list[dict[str, Any]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip().lstrip('\ufeff')
        if not line:
            continue
        parts = line.split(',')
        cid = parts[0].strip() if parts else ''
        if not cid.isdigit():
            continue
        if len(parts) >= 15:
            title = ','.join(parts[1:-13]).strip()
        else:
            title = parts[1].strip() if len(parts) > 1 else ''
        metadata = parts[-13:] if len(parts) >= 15 else []
        chapter_fee = max(_安全整数(metadata[1]), _安全整数(metadata[3])) if len(metadata) == 13 else 0
        rows.append({'cid': cid, 'title': title or f'第{cid}章', 'chapter_fee': chapter_fee})
    rows.sort(key=lambda row: int(row['cid']))
    return [{'cid': row['cid'], 'index': index, 'title': row['title'], 'chapter_fee': row['chapter_fee']} for index, row in enumerate(rows, start=1)]

async def 获取参考书籍目录(book_id: str, session: aiohttp.ClientSession | None=None) -> list[dict[str, Any]]:
    if session is None:
        async with 创建QQ阅读HTTP会话(concurrency=2) as local_session:
            return await 获取参考书籍目录(book_id, local_session)
    await 确保QQ阅读密钥池(session)
    params = {'bookId': book_id, 'type': '0', 'tafauth': '1', 'scids': '0', 'text_type': '0', 'useindex': '1'}
    async with session.get(QQ阅读目录地址, params=params, headers=await 异步构造QQ阅读鉴权请求头(int(time.time() * 1000), _构造QQ阅读请求地址(QQ阅读目录地址, params))) as response:
        response.raise_for_status()
        package = await response.read()
    return await _异步QQ阅读CPU函数(解析参考目录包, package, book_id)

async def 异步获取QQ阅读正文批次(session: aiohttp.ClientSession, book_id: str, chapter_ids: list[str], 解密信号量: asyncio.Semaphore, *, 请求信号量: asyncio.Semaphore | None=None, 解密材料: tuple[bytes, bytes, bytes] | None=None) -> tuple[list[Any], int, int]:
    config = ConfigManager.get_instance()
    params = {'bookId': str(book_id), 'type': '2', 'scids': 构造QQ阅读正文章节参数(chapter_ids), 'fuid': config.fuid}
    headers = await 异步构造QQ阅读鉴权请求头(int(time.time() * 1000), _构造QQ阅读请求地址(QQ阅读目录地址, params))
    request_started = time.perf_counter()
    if 请求信号量 is None:
        请求上下文 = session.get(QQ阅读目录地址, params=params, headers=headers)
        async with 请求上下文 as response:
            response.raise_for_status()
            package = await response.read()
    else:
        async with 请求信号量:
            async with session.get(QQ阅读目录地址, params=params, headers=headers) as response:
                response.raise_for_status()
                package = await response.read()
    request_elapsed = time.perf_counter() - request_started
    decrypt_started = time.perf_counter()
    async with 解密信号量:
        result = await _异步QQ阅读CPU函数(解析QQ阅读正文批次带统计, package, chapter_ids, 解密材料)
    decrypt_elapsed = time.perf_counter() - decrypt_started
    chapter_span = f'{chapter_ids[0]}-{chapter_ids[-1]}' if chapter_ids else ''
    logger.debug(f'QQ阅读批次耗时：章节范围={chapter_span}, 章节数={len(chapter_ids)}, 响应字节={len(package)}, 请求={request_elapsed:.3f}s, 解包解密={decrypt_elapsed:.3f}s')
    return result

def identify(value):
    return identify_id(value, PLATFORM['hosts'], ['bid', 'bookid', 'bookId', 'book_id'], ['/book-detail/(\\d+)', '/book/(\\d+)', '/(\\d+)(?:\\.html)?$'])

def _credentials():
    ywguid = os.environ.get('NOVEL_QQREAD_YWGUID', '').strip()
    ywkey = os.environ.get('NOVEL_QQREAD_YWKEY', '').strip()
    fuid = os.environ.get('NOVEL_QQREAD_FUID', '').strip() or QQ阅读默认正文标识
    if not ywguid or not ywkey:
        raise ProviderError('credentials_required')
    load_config_once()
    ConfigManager.get_instance().apply({'uid': ywguid, 'usid': ywkey, 'fuid': fuid})

async def _load_book(http, identity):
    raw = await _retry(lambda: 获取参考书籍详情(identity, http))
    return metadata(identity, raw.get('title'), raw.get('author'), raw.get('status'), raw.get('words_num'), raw.get('chapters'), raw.get('intro'), raw.get('coverUrl'))

async def get_book(book_id):
    _credentials()
    async with _session() as http:
        return await _load_book(http, _book_id(book_id))

async def download_book(book_id, on_progress=None):
    _credentials()
    identity = _book_id(book_id)
    async with _session() as http:
        book = await _load_book(http, identity)
        catalog = await _retry(lambda: 获取参考书籍目录(identity, http))
        if not catalog:
            raise ProviderError('chapter_unavailable')
        validate_catalog_count(book, len(catalog))
        if not await 确保QQ阅读密钥池(http):
            raise ProviderError('credentials_invalid')
        try:
            material = ConfigManager.get_instance().获取解密材料()
        except Exception:
            raise ProviderError('credentials_invalid') from None
        chapters = []
        semaphore = asyncio.Semaphore(2)
        if on_progress:
            on_progress(len(catalog), 0)
        for start in range(0, len(catalog), 30):
            batch = catalog[start:start + 30]
            ids = [str(chapter.get('cid') or '') for chapter in batch]
            if not all(ids):
                raise ProviderError('chapter_unavailable')
            result = await _retry(lambda: 异步获取QQ阅读正文批次(http, identity, ids, semaphore, 解密材料=material))
            contents, matched, failed = result
            if failed or matched != len(batch) or len(contents) != len(batch):
                raise ProviderError('chapter_unavailable')
            for row, content in zip(batch, contents):
                if isinstance(content, bytes):
                    content = content.decode('utf-8', 'replace')
                if content == '章节解密失败':
                    raise ProviderError('chapter_unavailable')
                chapters.append(checked({'title': row.get('title'), 'content': content}))
                if on_progress:
                    on_progress(len(catalog), len(chapters))
        return {'book': book, 'chapters': chapters}
