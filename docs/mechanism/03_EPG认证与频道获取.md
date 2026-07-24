# EPG 认证与频道获取

> 讲清楚: 华为HWCTC中间件的认证算法、如何模拟机顶盒拿到官方频道表、DES密钥如何爆破。
> 对应脚本: `legacy/epg_client.py`(认证核心)、`legacy/find_key.py`(密钥爆破)
> ⚠️ 凭证已脱敏,真实值配置在 .env

---

## 一、认证流程(华为 HWCTC)

```
Step1  GET  <EPG>/EPG/jsp/AuthenticationURL?UserID=<USERID>&Action=Login
            → 返回 EncryptToken(种子)
Step2  POST <EPG>/EPG/jsp/authLoginHWCTC.jsp?UserID=<USERID>&SampleId=
            → 登录会话
Step3  POST <EPG>/EPG/jsp/ValidAuthenticationHWCTC.jsp
            提交 Authenticator(DES加密的认证串) + 设备信息
            → 返回 JSESSIONID(认证成功标志)
Step4  POST <EPG>/EPG/jsp/getchannellistHWCTC.jsp
            → 返回频道表(169频道: 名称+组播IP+RTSP地址+时移标志)
```

## 二、认证核心: Authenticator 算法

```
Authenticator = DES-ECB加密(plaintext, key=<DES_KEY>)
plaintext = "99999$<TOKEN>$<USERID>$<STBID>$<IP>$<MAC>$$CTC"
```
- `99999`: 固定前缀
- `<TOKEN>`: Step1返回的EncryptToken
- `<USERID>/<STBID>/<MAC>`: 设备凭证
- `<IP>`: 当前IPTV网段IP
- `DES_KEY`: 8位密钥(本例 `00000000`)

**纯Python DES实现**: epg_client.py 内置了DES,不依赖pycryptodome,可在OpenWRT裸奔。

## 三、DES密钥如何得到(find_key.py 爆破)

密钥是运营商定制的8位字符。用抓包里已知的一组
(plaintext + Authenticator密文),爆破8位密钥空间:
```bash
python3 legacy/find_key.py   # 本例结果: 00000000
```
爆破一次即可,得到后写入 .env 的 DES_KEY,后续不用重跑。

## 四、ValidAuthentication 提交的字段(抓包还原)
```
UserID:        <USERID>
Authenticator: <DES加密串>
STBType:       EC6108V9U_pub_zjzdx
STBVersion:    V100R003C82LZJD11SPC002B012
STBID:         <STBID>
mac:           <STB_MAC>
templateName:  epg30
areaId:        57104           # 杭州区域ID
userToken:     <token>
userGroupId:   12
```

## 五、频道表返回内容
getchannellist 返回169个频道,每个含:
```
ChannelName        频道名(如"中央一套高清")
ChannelID          频道ID
ChannelURL         组播地址(igmp://233.50.201.118:5140)
                   + RTSP单播地址(rtsp://115.233.40.137/PLTV/...)
TimeShift          时移标志(1=支持)
TimeShiftLength    时移窗口(7200=2小时/14400=4小时)
```
- 一个频道**同时有组播和RTSP两个地址**
- 时移: 97频道支持(详见知识库时移研究)

## 六、使用方法

### 配置
复制 `.env.example` 为 `.env`,填入你的 EPG_USERID/STB_ID/STB_MAC/DES_KEY。

### 运行(旧脚本,新版见 src/)
```bash
# 从软路由/能访问IPTV专网的机器运行
python3 legacy/epg_client.py --ip <你的IPTV_IP>
# 输出: channels.json(频道表), channels.m3u
```

### 凭证怎么抓(如果不知道自己的)
抓包机顶盒开机过程(见 reference/pcap/stb_boot_and_play.pcap 说明):
```bash
# UserID: DHCP或EPG请求里
tshark -r stb_boot_and_play.pcap -Y 'http.request.uri contains "UserID"' -T fields -e http.request.full_uri
# STBID/MAC: ValidAuthentication请求体
tshark -r stb_boot_and_play.pcap -Y 'http.request.uri contains "ValidAuthentication"' -V
```

---

## 相关
- `01_浙江IPTV工作原理.md` — 认证在整体流程中的位置
- 脚本: `legacy/epg_client.py`, `legacy/find_key.py`
- 新采集层: `src/scan_*.py`(扫描), EPG认证逻辑将并入新采集层
