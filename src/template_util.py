#!/usr/bin/env python3
"""
iptv-radar: template_util.py — Jinja2 模板渲染辅助(gen_dashboard/gen_channels_page共用)

模板在 src/templates/。数据层(gen_*.py)准备好数据dict,调 render_template 渲染成HTML。
数据与界面分离: Python只查库/备数据,HTML/CSS/JS 在 templates/*.html 里。
"""
import os
from jinja2 import Environment, FileSystemLoader

_TPL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'templates')
_env = Environment(
    loader=FileSystemLoader(_TPL_DIR),
    autoescape=False,   # 我们的数据层已用esc()处理,行内含预格式化HTML片段(带|safe)
)


def render_template(name, **ctx):
    """渲染 templates/<name>,ctx为模板变量。返回HTML字符串。"""
    return _env.get_template(name).render(**ctx)
