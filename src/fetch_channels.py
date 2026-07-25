#!/usr/bin/env python3
"""
浙江电信IPTV EPG频道列表获取工具 (OpenWRT版)
纯Python实现,无额外依赖
"""
import re, socket, json, os, sys
from urllib.request import Request, urlopen, build_opener, HTTPCookieProcessor, HTTPRedirectHandler
from urllib.parse import urlencode
from http.cookiejar import CookieJar

# ==================== 纯Python DES实现 ====================
_IP = [58,50,42,34,26,18,10,2,60,52,44,36,28,20,12,4,62,54,46,38,30,22,14,6,64,56,48,40,32,24,16,8,57,49,41,33,25,17,9,1,59,51,43,35,27,19,11,3,61,53,45,37,29,21,13,5,63,55,47,39,31,23,15,7]
_FP = [40,8,48,16,56,24,64,32,39,7,47,15,55,23,63,31,38,6,46,14,54,22,62,30,37,5,45,13,53,21,61,29,36,4,44,12,52,20,60,28,35,3,43,11,51,19,59,27,34,2,42,10,50,18,58,26,33,1,41,9,49,17,57,25]
_E = [32,1,2,3,4,5,4,5,6,7,8,9,8,9,10,11,12,13,12,13,14,15,16,17,16,17,18,19,20,21,20,21,22,23,24,25,24,25,26,27,28,29,28,29,30,31,32,1]
_P = [16,7,20,21,29,12,28,17,1,15,23,26,5,18,31,10,2,8,24,14,32,27,3,9,19,13,30,6,22,11,4,25]
_PC1 = [57,49,41,33,25,17,9,1,58,50,42,34,26,18,10,2,59,51,43,35,27,19,11,3,60,52,44,36,63,55,47,39,31,23,15,7,62,54,46,38,30,22,14,6,61,53,45,37,29,21,13,5,28,20,12,4]
_PC2 = [14,17,11,24,1,5,3,28,15,6,21,10,23,19,12,4,26,8,16,7,27,20,13,2,41,52,31,37,47,55,30,40,51,45,33,48,44,49,39,56,34,53,46,42,50,36,29,32]
_SHIFTS = [1,1,2,2,2,2,2,2,1,2,2,2,2,2,2,1]
_S = [
 [[14,4,13,1,2,15,11,8,3,10,6,12,5,9,0,7],[0,15,7,4,14,2,13,1,10,6,12,11,9,5,3,8],[4,1,14,8,13,6,2,11,15,12,9,7,3,10,5,0],[15,12,8,2,4,9,1,7,5,11,3,14,10,0,6,13]],
 [[15,1,8,14,6,11,3,4,9,7,2,13,12,0,5,10],[3,13,4,7,15,2,8,14,12,0,1,10,6,9,11,5],[0,14,7,11,10,4,13,1,5,8,12,6,9,3,2,15],[13,8,10,1,3,15,4,2,11,6,7,12,0,5,14,9]],
 [[10,0,9,14,6,3,15,5,1,13,12,7,11,4,2,8],[13,7,0,9,3,4,6,10,2,8,5,14,12,11,15,1],[13,6,4,9,8,15,3,0,11,1,2,12,5,10,14,7],[1,10,13,0,6,9,8,7,4,15,14,3,11,5,2,12]],
 [[7,13,14,3,0,6,9,10,1,2,8,5,11,12,4,15],[13,8,11,5,6,15,0,3,4,7,2,12,1,10,14,9],[10,6,9,0,12,11,7,13,15,1,3,14,5,2,8,4],[3,15,0,6,10,1,13,8,9,4,5,11,12,7,2,14]],
 [[2,12,4,1,7,10,11,6,8,5,3,15,13,0,14,9],[14,11,2,12,4,7,13,1,5,0,15,10,3,9,8,6],[4,2,1,11,10,13,7,8,15,9,12,5,6,3,0,14],[11,8,12,7,1,14,2,13,6,15,0,9,10,4,5,3]],
 [[12,1,10,15,9,2,6,8,0,13,3,4,14,7,5,11],[10,15,4,2,7,12,9,5,6,1,13,14,0,11,3,8],[9,14,15,5,2,8,12,3,7,0,4,10,1,13,11,6],[4,3,2,12,9,5,15,10,11,14,1,7,6,0,8,13]],
 [[4,11,2,14,15,0,8,13,3,12,9,7,5,10,6,1],[13,0,11,7,4,9,1,10,14,3,5,12,2,15,8,6],[1,4,11,13,12,3,7,14,10,15,6,8,0,5,9,2],[6,11,13,8,1,4,10,7,9,5,0,15,14,2,3,12]],
 [[13,2,8,4,6,15,11,1,10,9,3,14,5,0,12,7],[1,15,13,8,10,3,7,4,12,5,6,11,0,14,9,2],[7,11,4,1,9,12,14,2,0,6,10,13,15,3,5,8],[2,1,14,7,4,10,8,13,15,12,9,0,3,5,6,11]]
]
def _perm(t, tbl): return [t[x-1] for x in tbl]
def _shift(b, n): return b[n:]+b[:n]
def _xor(a, b): return [x^y for x,y in zip(a,b)]
def _bits(d): return [(byte>>i)&1 for byte in d for i in range(7,-1,-1)]
def _unbits(b): return bytes(int(''.join(map(str,b[i:i+8])),2) for i in range(0,len(b),8))
def _subkeys(k):
    pk=_perm(_bits(k),_PC1); l,r=pk[:28],pk[28:]; sk=[]
    for s in _SHIFTS:
        l=_shift(l,s); r=_shift(r,s); sk.append(_perm(l+r,_PC2))
    return sk
def _feistel(r, sk):
    x=_xor(_perm(r,_E),sk); out=[]
    for i in range(8):
        c=x[i*6:(i+1)*6]; row=(c[0]<<1)|c[5]; col=(c[1]<<3)|(c[2]<<2)|(c[3]<<1)|c[4]
        v=_S[i][row][col]
        for j in range(3,-1,-1): out.append((v>>j)&1)
    return _perm(out,_P)
def des_ecb_encrypt(data, key):
    sk=_subkeys(key)
    pad=8-(len(data)%8); data+=bytes([pad])*pad
    res=b''
    for i in range(0,len(data),8):
        b=_perm(_bits(data[i:i+8]),_IP); l,r=b[:32],b[32:]
        for j in range(16): l,r=r,_xor(l,_feistel(r,sk[j]))
        res+=_unbits(_perm(r+l,_FP))
    return res.hex()

# ==================== 配置(从 .env 读,不硬编码凭证) ====================
def _load_env():
    """从项目根 .env 读配置。返回 dict。"""
    env = {}
    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
    envpath = os.path.join(root, '.env')
    if os.path.exists(envpath):
        for line in open(envpath, encoding='utf-8'):
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                env[k.strip()] = v.strip()
    return env

_ENV = _load_env()
EDS_SERVER = _ENV.get('EDS_SERVER', os.environ.get('EDS_SERVER', ''))
EPG_SERVER = _ENV.get('EPG_SERVER', os.environ.get('EPG_SERVER', ''))
USERID = _ENV.get('EPG_USERID', os.environ.get('EPG_USERID', ''))
STBID = _ENV.get('STB_ID', os.environ.get('STB_ID', ''))
MAC = _ENV.get('STB_MAC', os.environ.get('STB_MAC', ''))
DES_KEY = _ENV.get('DES_KEY', os.environ.get('DES_KEY', '00000000'))
STB_TYPE = _ENV.get('STB_TYPE', 'EC6108V9U_pub_zjzdx')
STB_VERSION = _ENV.get('STB_VERSION', 'V100R003C82LZJD11SPC002B012')
AREA_ID = _ENV.get('AREA_ID', '57104')
TEMPLATE_NAME = "epg30"
USER_GROUP_ID = "12"
UA = "Mozilla/5.0 (X11; U; Linux i686; en-US) AppleWebKit/534.0 (KHTML, like Gecko)"
# 本机IP探测目标(仅用于 socket.connect 反查本机出口IP,不发送任何数据)。
# 可用 .env 覆盖,避免把具体网络地址写死在代码里。
EPG_HOST = _ENV.get('EPG_PROBE_HOST', '115.233.40.140')
EPG_PORT_PROBE = int(_ENV.get('EPG_PROBE_PORT', '33200'))
LAN_PROBE_HOST = _ENV.get('LAN_PROBE_HOST', '10.225.136.1')
FALLBACK_LOCAL_IP = _ENV.get('FALLBACK_LOCAL_IP', '10.225.140.58')

# ==================== 工具函数 ====================
def _mask(secret, keep=4):
    """日志脱敏: token 是有效凭证,完整打印会随 cron 日志/日志聚合泄露。"""
    if not secret:
        return '(empty)'
    s = str(secret)
    if len(s) <= keep * 2:
        return '*' * len(s)
    return f"{s[:keep]}...{s[-keep:]}(len={len(s)})"


def get_local_ip():
    # 如果命令行指定了IP,直接用
    for i, arg in enumerate(sys.argv):
        if arg == '--ip' and i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    # 否则自动检测(从OpenWRT上跑时用本机IP)
    # 用裸 except 会连 KeyboardInterrupt 都吞掉,且网络故障时静默返回兜底IP,
    # 导致认证串里带错IP、排查无线索。这里只捕 OSError 并打印告警。
    probe_targets = [(EPG_HOST, EPG_PORT_PROBE), (LAN_PROBE_HOST, 80)]
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for host, port in probe_targets:
            try:
                s.connect((host, port))
                return s.getsockname()[0]
            except OSError as e:
                print(f"  ⚠ 本机IP探测失败({host}:{port}): {e}")
        print(f"  ⚠ 全部探测失败,回退硬编码IP {FALLBACK_LOCAL_IP}(认证可能失败,建议用 --ip 显式指定)")
        return FALLBACK_LOCAL_IP
    finally:
        s.close()

def des_encrypt(plaintext):
    """DES-ECB加密(纯Python)"""
    return des_ecb_encrypt(plaintext.encode('utf-8'), DES_KEY.encode('ascii'))

def http_get(url, opener, extra_headers=None):
    req = Request(url)
    req.add_header('User-Agent', UA)
    if extra_headers:
        for k, v in extra_headers.items():
            req.add_header(k, v)
    resp = opener.open(req, timeout=15)
    return resp, resp.read().decode('utf-8', errors='replace')

def http_post(url, data, opener, extra_headers=None):
    post_data = urlencode(data).encode('utf-8')
    req = Request(url, data=post_data, method='POST')
    req.add_header('User-Agent', UA)
    req.add_header('Content-Type', 'application/x-www-form-urlencoded')
    if extra_headers:
        for k, v in extra_headers.items():
            req.add_header(k, v)
    resp = opener.open(req, timeout=15)
    return resp, resp.read().decode('utf-8', errors='replace')

# ==================== EPG认证 ====================
def authenticate():
    ip = get_local_ip()
    print(f"本机IP: {ip}")
    cj = CookieJar()
    opener = build_opener(HTTPCookieProcessor(cj), HTTPRedirectHandler())
    epg = f"http://{EPG_SERVER}"

    # Step 1: AuthenticationURL
    print("[1/5] 获取EncryptToken...")
    try:
        resp, html = http_get(f"{epg}/EPG/jsp/AuthenticationURL?UserID={USERID}&Action=Login", opener)
    except Exception as e:
        print(f"  EPG连接失败: {e}")
        return None, None, None

    # 提取EncryptToken
    token = ""
    m = re.search(r'EncryptToken\s*=\s*"([^"]+)"', html)
    if m:
        token = m.group(1)
    m2 = re.search(r'userToken\.value\s*=\s*"([^"]+)"', html)
    if m2 and not token:
        token = m2.group(1)
    print(f"  Token: {_mask(token)}")

    # Step 2: authLogin
    print("[2/5] authLogin...")
    try:
        resp, html2 = http_post(
            f"{epg}/EPG/jsp/authLoginHWCTC.jsp?UserID={USERID}&SampleId=",
            {'UserID': USERID, 'VIP': ''},
            opener,
            {'Referer': f'{epg}/EPG/jsp/AuthenticationURL?UserID={USERID}&Action=Login',
             'Origin': epg}
        )
    except Exception as e:
        print(f"  authLogin失败: {e}")
        return None, None, None

    # 重新提取token
    if not token:
        m = re.search(r'EncryptToken\s*=\s*"([^"]+)"', html2)
        if m:
            token = m.group(1)
            print(f"  Token(from authLogin): {_mask(token)}")

    # Step 3: ValidAuthentication
    print("[3/5] ValidAuthentication...")
    authenticator = des_encrypt(f"99999${token}${USERID}${STBID}${ip}${MAC}$$CTC")
    if not authenticator:
        print("  加密失败!")
        return None, None, None

    data = {
        'UserID': USERID, 'Lang': '1', 'SupportHD': '1', 'NetUserID': '',
        'Authenticator': authenticator, 'STBType': STB_TYPE,
        'STBVersion': STB_VERSION, 'conntype': '2', 'STBID': STBID,
        'templateName': TEMPLATE_NAME, 'areaId': AREA_ID,
        'userToken': token, 'userGroupId': USER_GROUP_ID,
        'productPackageId': '-1', 'mac': MAC, 'UserField': '2',
        'SoftwareVersion': STB_VERSION, 'IsSmartStb': '0',
        'desktopId': '', 'stbmaker': '', 'VIP': '',
    }
    try:
        resp, html3 = http_post(
            f"{epg}/EPG/jsp/ValidAuthenticationHWCTC.jsp",
            data, opener,
            {'Referer': f'{epg}/EPG/jsp/authLoginHWCTC.jsp', 'Origin': epg}
        )
    except Exception as e:
        print(f"  ValidAuthentication失败: {e}")
        return None, None, None

    # 检查结果
    has_session = any(c.name == 'JSESSIONID' for c in cj)
    if has_session:
        print("  认证成功!(有JSESSIONID)")
    elif 'UserStatus' in html3 and "'1'" in html3:
        print("  认证成功!(UserStatus=1)")
    else:
        print(f"  ⚠ 认证状态不确定,继续尝试...")
        print(f"  响应片段: {html3[:200]}")

    # 提取UserToken
    ut = re.search(r"UserToken['\"].*?value\s*=\s*['\"]([^'\"]+)", html3)
    user_token = ut.group(1) if ut else token

    # 提取STBID
    sid = re.search(r"stbid['\"].*?value\s*=\s*['\"]([^'\"]+)", html3)
    stbid = sid.group(1) if sid else STBID

    return opener, user_token, stbid

# ==================== 获取频道列表 ====================
def get_channels(opener, user_token, stbid):
    print("\n[4/5] 获取频道列表...")
    epg = f"http://{EPG_SERVER}"
    data = {
        'conntype': '', 'UserToken': user_token, 'tempKey': '',
        'stbid': stbid, 'SupportHD': '1', 'UserID': USERID, 'Lang': '1',
    }
    try:
        resp, html = http_post(f"{epg}/EPG/jsp/getchannellistHWCTC.jsp", data, opener)
    except Exception as e:
        print(f"  获取失败: {e}")
        return [], ""

    print(f"  响应: {len(html)} 字节")

    # 保存原始响应
    out = os.path.dirname(os.path.abspath(__file__))
    raw_path = os.path.join(out, 'epg_raw_response.txt')
    with open(raw_path, 'w', encoding='utf-8') as f:
        f.write(html)

    # 解析频道 - 尝试多种格式
    channels = []

    # 格式1: XML属性
    for m in re.finditer(r'ChannelID="(\d+)"[^>]*?(?:ChannelName|Name)="(.+?)"[^>]*?(?:UserChannelID="\d+")?[^>]*?ChannelURL="(.*?)"', html, re.DOTALL):
        channels.append({'id': m.group(1), 'name': m.group(2), 'url': m.group(3)})

    # 格式2: 更宽松
    if not channels:
        for m in re.finditer(r'ChannelName="(.+?)"[^>]*?ChannelURL="(.*?)"', html, re.DOTALL):
            channels.append({'id': '', 'name': m.group(1), 'url': m.group(2)})

    # 提取TimeShiftURL
    for m in re.finditer(r'ChannelID="(\d+)"[^>]*?TimeShiftURL="(.*?)"', html, re.DOTALL):
        cid, ts_url = m.group(1), m.group(2)
        for ch in channels:
            if ch['id'] == cid:
                ch['timeshift_url'] = ts_url

    # 找所有包含115.233的URL(单播地址)
    unicast_urls = re.findall(r'(\w+)="(rtsp://115\.233\.[^"]+|http://115\.233\.[^"]+)"', html)
    if unicast_urls:
        print(f"\n  发现单播URL字段({len(unicast_urls)}个):")
        for field, url in unicast_urls[:10]:
            print(f"    {field}: {url}")

    # 找所有包含233.50的地址
    multicast_addrs = re.findall(r'233\.50\.\d+\.\d+:\d+', html)
    if multicast_addrs:
        print(f"\n  发现组播地址: {len(set(multicast_addrs))} 个")
        for addr in sorted(set(multicast_addrs)):
            if '202' in addr:
                print(f"    📌 {addr}")

    return channels, html

# ==================== 主程序 ====================
def main():
    print("=" * 50)
    print("  浙江电信IPTV EPG频道列表获取(刷新token)")
    print("=" * 50)
    if not USERID or not STBID or not MAC or not EPG_SERVER:
        print("\n❌ .env 缺 EPG_USERID/STB_ID/STB_MAC/EPG_SERVER,无法认证")
        return

    opener, user_token, stbid = authenticate()
    if not opener:
        print("\n❌ 认证失败")
        return

    channels, raw = get_channels(opener, user_token, stbid)

    print(f"\n[5/5] 保存结果...")
    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
    # 刷新出的 channels.json 含 token,是**可变运行时状态**,放 data/(挂载的可写目录),
    # 不能放 reference/ —— 那是镜像里的静态资源目录:
    #   ① 非root运行时容器写不进镜像层
    #   ② 用单文件 bind mount 绕过时,原子写(tmp+rename)会因跨挂载点/目录不可写而失败
    #   ③ 宿主机上该文件不存在时 docker 会把挂载点创建成目录,静默坏掉
    # 兼容: 旧部署若已有 reference/channels.json 且 data/ 下没有,则继续写旧位置。
    out_json = os.path.join(root, 'data', 'channels.json')
    legacy = os.path.join(root, 'reference', 'channels.json')
    if not os.path.exists(out_json) and os.path.isfile(legacy):
        out_json = legacy

    if channels:
        mc = [c for c in channels if 'igmp://' in c.get('url','') or '233.50.' in c.get('url','')]
        uc = [c for c in channels if 'rtsp://' in c.get('url','') or '115.233.' in c.get('url','')]
        ts = [c for c in channels if c.get('timeshift_url')]
        print(f"\n  共 {len(channels)} 频道 (组播{len(mc)} 单播{len(uc)} 有时移{len(ts)})")
        # 只存 channels.json(带新token),供 link_sources/scan_rtsp/probe_timeshift/gen 用
        # 原子写: 该文件是下游全部环节的输入(归并/扫描/回看query),写一半被杀会让
        # 下游拿到损坏JSON。先写 .tmp 再 rename。
        if os.path.isdir(out_json):
            # compose 单文件挂载时,若宿主机上该文件还不存在,docker 会创建成目录,
            # 导致 open(...,'w') 抛 IsADirectoryError。这里显式报错,别静默失败。
            print(f"\n❌ {out_json} 是一个目录(通常是 docker 单文件挂载时宿主机文件不存在导致)。")
            print(f"   解决: 在宿主机执行 touch {out_json} 后重新部署。")
            sys.exit(2)
        tmp_json = out_json + '.tmp'
        try:
            with open(tmp_json, 'w', encoding='utf-8') as f:
                json.dump(channels, f, ensure_ascii=False, indent=1)
            os.replace(tmp_json, out_json)
        except OSError as e:
            # 兜底: 某些部署下目标是单文件 bind mount(目录不可写/跨挂载点),
            # tmp+rename 会失败。退化成原地写 —— 失去原子性但不至于整条流水线挂掉。
            print(f"  ⚠ 原子写失败({e.strerror}),退化为原地写: {out_json}")
            try:
                os.unlink(tmp_json)
            except OSError:
                pass
            with open(out_json, 'w', encoding='utf-8') as f:
                json.dump(channels, f, ensure_ascii=False, indent=1)
        print(f"  已保存(含新token): {out_json}")
    else:
        print(f"\n⚠ 未解析到频道。原始响应前500字符:\n{raw[:500]}")

if __name__ == '__main__':
    main()
