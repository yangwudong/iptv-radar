"""run_pipeline.sh 的行为测试(参数解析 + 发布安全闸)。

用一个假的 python3 挡在 PATH 前面,只记录被调用的参数、不真的干活。
这样能在不碰生产库/不联网的前提下,测 pipeline 的编排逻辑。
"""
import os
import subprocess
import sys

import pytest

from conftest import SRC


def _stub_env(tmp_path, extra=None):
    """造一个 stub python3(记录调用) + 最小 .env,返回 (env, calls_log)。"""
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    log = tmp_path / 'calls.log'
    stub = bindir / 'python3'
    stub.write_text(
        '#!/bin/sh\n'
        f'echo "$@" >> "{log}"\n'
        # -c 用于 pipeline 内部校验JSON,要真的执行,否则逻辑判断不对
        'if [ "$1" = "-c" ]; then exec ' + sys.executable + ' "$@"; fi\n'
        'exit 0\n', encoding='utf-8')
    stub.chmod(0o755)
    env = dict(os.environ)
    env['PATH'] = f"{bindir}:{env['PATH']}"
    if extra:
        env.update(extra)
    return env, log


def _run(args, env, cwd=SRC):
    return subprocess.run(['bash', 'run_pipeline.sh', *args],
                          cwd=cwd, env=env, capture_output=True, text=True, timeout=120)


# ============================================================
# 参数解析: 拼错的参数曾会静默触发全量扫描(20分钟 + 刷token)
# ============================================================

def test_未知参数必须报错且不执行任何脚本(tmp_path):
    env, log = _stub_env(tmp_path)
    r = _run(['--gen-onl'], env)
    assert r.returncode == 2, f"未知参数应退出2,实际 {r.returncode}"
    assert '未知参数' in r.stdout + r.stderr
    assert not log.exists(), "拼错参数却已经开始执行脚本(会跑全量扫描/刷token)"


def test_gen_only不刷token不扫描且只生成三套m3u(tmp_path):
    env, log = _stub_env(tmp_path, {'NGINX_M3U_DIR': str(tmp_path / 'nodir')})
    r = _run(['--gen-only'], env)
    assert r.returncode == 0, r.stdout + r.stderr
    calls = log.read_text(encoding='utf-8')
    assert 'fetch_channels.py' not in calls, "--gen-only 不该刷token"
    assert 'scan_multicast.py' not in calls and 'scan_rtsp.py' not in calls, "--gen-only 不该扫描"
    assert 'etl_process.py' not in calls, "--gen-only 不该跑ETL"
    assert calls.count('gen_m3u.py') == 3, f"应生成三套m3u,实际:\n{calls}"


def test_三套m3u的参数契约(tmp_path):
    """标准版/兼容版带FCC,直通版不带;直通版用 direct 模式;兼容版 prefer-multicast。

    注意 run_pipeline.sh 用 `set -a; . ../.env` 加载配置,会**覆盖**外部传入的同名
    环境变量,所以这里按项目 .env 的真实值断言,而不是自己塞一个值。
    """
    sys.path.insert(0, SRC)
    import db_util
    fcc = db_util.load_env().get('FCC_SERVER', '')
    if not fcc:
        pytest.skip('.env 未配 FCC_SERVER,跳过FCC契约断言')
    env, log = _stub_env(tmp_path)
    _run(['--gen-only'], env)
    lines = [l for l in log.read_text(encoding='utf-8').splitlines() if 'gen_m3u.py' in l]
    assert len(lines) == 3, lines
    std = [l for l in lines if 'iptv.m3u' in l and 'direct' not in l and 'prefer' not in l]
    direct = [l for l in lines if 'iptv_direct.m3u' in l]
    compat = [l for l in lines if 'iptv_compat.m3u' in l]
    assert std and direct and compat, lines
    assert f'--fcc {fcc}' in std[0], f"标准版应带FCC: {std[0]}"
    assert '--fcc' not in direct[0], f"直通版不该带FCC(rtp://@不支持): {direct[0]}"
    assert '--multicast-mode direct' in direct[0]
    assert f'--fcc {fcc}' in compat[0] and '--prefer-multicast' in compat[0]


def test_timeshift_only在只有脱敏样例时必须拒跑(tmp_path):
    """用假token探测会把 playback_days 全写成0,清空所有回看数据(与用户本意相反)。"""
    env, log = _stub_env(tmp_path)
    r = _run(['--timeshift-only'], env)
    assert r.returncode == 8, f"应退出8拒跑,实际 {r.returncode}\n{r.stdout}{r.stderr}"
    calls = log.read_text(encoding='utf-8') if log.exists() else ''
    assert 'probe_timeshift.py' not in calls, "已经拿假token去探测了"


# ============================================================
# 发布安全闸: 防止空的/大幅缩水的播放列表覆盖正常发布的文件
# ============================================================

def test_发布闸函数_直接测判定逻辑(tmp_path):
    """直接调 shell 函数,避免依赖仓库真实 output/ 目录。"""
    nginx = tmp_path / 'nginx'; nginx.mkdir()
    good = tmp_path / 'good.m3u'
    good.write_text('#EXTM3U\n' + ''.join(
        f'#EXTINF:-1,频道{i}\nhttp://h/{i}\n' for i in range(100)), encoding='utf-8')
    tiny = tmp_path / 'tiny.m3u'
    tiny.write_text('#EXTM3U\n#EXTINF:-1,只有一个\nhttp://h/1\n', encoding='utf-8')
    empty = tmp_path / 'empty.m3u'; empty.write_text('#EXTM3U\n', encoding='utf-8')
    notm3u = tmp_path / 'bad.m3u'; notm3u.write_text('<html>404</html>\n', encoding='utf-8')
    published = nginx / 'iptv.m3u'
    published.write_text(good.read_text(encoding='utf-8'), encoding='utf-8')

    def gate(f, dst=''):
        script = f'''
        set -e
        NGINX_M3U_DIR="{nginx}"
        PUBLISH_MIN_ENTRIES=50
        PUBLISH_MAX_SHRINK_PCT=20
        {_extract_gate()}
        check_m3u_sane "{f}" "{dst}"
        '''
        return subprocess.run(['bash', '-c', script], capture_output=True, text=True)

    assert gate(good).returncode == 0, "正常文件被拒了"
    assert gate(tiny).returncode != 0, "只有1个频道竟然通过了闸"
    assert gate(empty).returncode != 0, "空播放列表竟然通过了闸"
    assert gate(notm3u).returncode != 0, "非m3u内容竟然通过了闸"
    # 缩水: 100 → 60 是 40% 降幅,超过 20% 上限
    shrunk = tmp_path / 'shrunk.m3u'
    shrunk.write_text('#EXTM3U\n' + ''.join(
        f'#EXTINF:-1,频道{i}\nhttp://h/{i}\n' for i in range(60)), encoding='utf-8')
    assert gate(shrunk, str(published)).returncode != 0, "40%缩水竟然通过了闸"
    # 轻微变化应放行(100 → 95 = 5%)
    ok = tmp_path / 'ok.m3u'
    ok.write_text('#EXTM3U\n' + ''.join(
        f'#EXTINF:-1,频道{i}\nhttp://h/{i}\n' for i in range(95)), encoding='utf-8')
    assert gate(ok, str(published)).returncode == 0, "正常的5%波动被误拒"


def _extract_gate():
    """从 run_pipeline.sh 抽出 check_m3u_sane 函数体,保证测的是真代码而非副本。"""
    src = open(os.path.join(SRC, 'run_pipeline.sh'), encoding='utf-8').read()
    start = src.index('check_m3u_sane() {')
    end = src.index('\n}\n', start) + 3
    return src[start:end]
