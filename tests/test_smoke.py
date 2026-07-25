"""冒烟测试: 每个脚本都必须能被导入、能跑到主逻辑。

为什么需要这层:
  db_util 重构时 probe_timeshift.py 漏了 `import db_util`,而该调用在探测循环**之后**,
  于是 --timeshift-only 模式每次都把活干完再 NameError 崩掉,又被 pipeline 的
  `|| echo "(继续)"` 吞掉 → 报告成功、实际零效果。持续了好几次提交没人发现。
  这类"引用未定义/顶层语法/缺依赖"的错误,一次全量导入就能全部拦住。
"""
import os
import subprocess
import sys

import pytest

from conftest import SRC

SCRIPTS = sorted(
    f for f in os.listdir(SRC)
    if f.endswith('.py') and not f.startswith('_')
)


@pytest.mark.parametrize('script', SCRIPTS)
def test_脚本可导入(script):
    """顶层导入不得报错(能抓语法错误、缺依赖、循环导入)。"""
    r = subprocess.run(
        [sys.executable, '-c',
         f"import sys; sys.path.insert(0, {SRC!r}); "
         f"import importlib; importlib.import_module({script[:-3]!r})"],
        capture_output=True, text=True)
    assert r.returncode == 0, f"{script} 导入失败:\n{r.stderr}"


# 能安全跑到"连上库/读完参数"这一步的脚本(不联网、不扫流、不写生产库)
_RUNNABLE = ['db_schema.py', 'etl_process.py', 'gen_m3u.py', 'gen_dashboard.py',
             'gen_channels_page.py', 'link_sources.py', 'orphan_export.py',
             'orphan_import.py', 'probe_timeshift.py', 'scan_multicast.py',
             'scan_rtsp.py', 'seed.py']


@pytest.mark.parametrize('script', _RUNNABLE)
def test_脚本_help不报错(script):
    """--help 走完 argparse 构建,能抓到参数定义期的引用错误。"""
    r = subprocess.run([sys.executable, os.path.join(SRC, script), '--help'],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"{script} --help 失败:\n{r.stderr}"


def test_全部脚本的模块级引用都能解析():
    """静态检查: 每个脚本里被调用的 `模块.属性` 形式,该模块必须已导入。

    这是 probe_timeshift 那个 bug 的直接防线 —— 它的 db_util.connect() 在函数体内,
    只有真正执行到才会炸,--help 和导入都抓不到。
    """
    import ast
    problems = []
    for script in SCRIPTS:
        path = os.path.join(SRC, script)
        tree = ast.parse(open(path, encoding='utf-8').read(), filename=script)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    imported.add((a.asname or a.name).split('.')[0])
            elif isinstance(node, ast.ImportFrom):
                for a in node.names:
                    imported.add(a.asname or a.name)
        # 收集所有本地绑定的名字(赋值/函数/类/参数/for/with/except/comprehension)
        import builtins
        bound = set(dir(builtins))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                bound.add(node.name)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    ar = node.args
                    for a in (ar.posonlyargs + ar.args + ar.kwonlyargs):
                        bound.add(a.arg)
                    for a in (ar.vararg, ar.kwarg):
                        if a: bound.add(a.arg)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
                bound.add(node.id)
            elif isinstance(node, ast.ExceptHandler) and node.name:
                bound.add(node.name)
            elif isinstance(node, ast.Global):
                bound.update(node.names)
        # 检查 `X.attr` 里的 X
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)):
                name = node.value.id
                if name not in imported and name not in bound:
                    problems.append(f"{script}:{node.lineno} 用了未定义的 `{name}.{node.attr}`")
    assert not problems, "存在未导入即使用的模块引用:\n  " + "\n  ".join(sorted(set(problems)))
